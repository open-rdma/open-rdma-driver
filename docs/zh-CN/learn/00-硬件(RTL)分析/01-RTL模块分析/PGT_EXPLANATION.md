# PGT（Page Table）详解

## 什么是 PGT？

**PGT = Page Gating Table（页表）**

PGT 是 RDMA 硬件中的一个**物理页号查找表**，用于将虚拟地址转换为物理地址。它是两级地址转换机制的第二级。

## 两级地址转换架构

RDMA 使用**两级地址转换**来支持虚拟地址到物理地址的映射：

```
虚拟地址 (VA)
    ↓
[第一级] MTT (Memory Translation Table)
    → 查询 MR Key，获取 {pgtOffset, baseVA, length, ...}
    ↓
[第二级] PGT (Page Table)
    → 使用 pgtOffset + 页号偏移，查询物理页号
    ↓
物理地址 (PA)
```

## 数据结构定义

### PageTableEntry（PGT 条目）

```bsv
typedef struct {
    PageNumber pn;    // 物理页号（27 bits）
} PageTableEntry;
```

每个 PGT 条目只存储一个**物理页号**（Page Number）。

### 硬件常量

从 `open-rdma-rtl/src/Settings.bsv` 和 `BasicDataTypes.bsv`：

```bsv
PAGE_SIZE_CAP = 2^21 = 2MB              // 页大小
PAGE_OFFSET_WIDTH = 21 bits             // 页内偏移位宽
PAGE_NUMBER_WIDTH = 48 - 21 = 27 bits   // 物理页号位宽
MAX_PTE_ENTRY_CNT = 2^17 = 131,072      // PGT 最大条目数
PTEIndex = log2(131072) = 17 bits       // PGT 索引位宽
```

### 地址分解

**物理地址 (48 bits)：**
```
+---------------------------+---------------------+
|    PageNumber (27 bits)   | PageOffset (21 bits)|
+---------------------------+---------------------+
|        PGT 存储的内容      |   页内偏移（2MB）    |
+---------------------------+---------------------+
```

## PGT Index 是什么？

**PGT Index（PTEIndex）** 是 PGT 表中的索引，用于定位具体的 PageTableEntry。

### 类型定义

```bsv
typedef Bit#(TLog#(MAX_PTE_ENTRY_CNT)) PTEIndex;
// PTEIndex = Bit#(17)，范围：0 ~ 131071
```

### PGT 存储结构

PGT 是一个大数组：

```
PGT[0]      → PageTableEntry { pn: 物理页号_0 }
PGT[1]      → PageTableEntry { pn: 物理页号_1 }
PGT[2]      → PageTableEntry { pn: 物理页号_2 }
...
PGT[131071] → PageTableEntry { pn: 物理页号_131071 }
```

硬件实现（from `MemRegionAndAddressTranslate.bsv:269`）：

```bsv
BramCache#(PTEIndex, PageTableEntry, 4) pageTableStorage <- mkBramCache;
```

这是一个以 **PTEIndex 为索引，PageTableEntry 为值** 的 BRAM（块 RAM）缓存。

## 完整的地址转换流程

### 步骤 1：MTT 查询

用户访问虚拟地址 `va`，使用 MR Key 查询 MTT：

```bsv
MemRegionTableEntry mrEntry = MTT[mr_key];
// mrEntry = {
//     pgtOffset: 100,           // PGT 起始索引
//     baseVA: 0x763f4e011040,   // MR 的基虚拟地址
//     len: 0x8000,              // MR 长度
//     ...
// }
```

### 步骤 2：计算 PGT Index

从硬件代码 `MemRegionAndAddressTranslate.bsv:283-284`：

```bsv
// 计算访问地址相对于 baseVA 的页号偏移
pageNumberOffset = getPageNumber(va) - getPageNumber(mrEntry.baseVA)

// 计算最终的 PGT 索引
pteIdx = mrEntry.pgtOffset + truncate(pageNumberOffset)
```

**具体例子：**
```
va = 0x763f4e211040
baseVA = 0x763f4e011040
pgtOffset = 100

// 计算页号偏移
pageNumberOffset = (0x763f4e211040 >> 21) - (0x763f4e011040 >> 21)
                 = 0x3b1fa7108 - 0x3b1fa7008
                 = 0x100 (256 in decimal，即相差 256 个 2MB 页)

// 计算 PGT 索引
pteIdx = 100 + 256 = 356
```

### 步骤 3：PGT 查询

使用计算出的 `pteIdx` 查询 PGT：

```bsv
PageTableEntry pte = PGT[pteIdx];
// pte = { pn: 0x1234567 }  // 物理页号
```

### 步骤 4：组装物理地址

```bsv
pageOffset = getPageOffset(va)       // 提取页内偏移（低 21 bits）
pa = restorePA(pte.pn, pageOffset)   // 组合物理页号和页内偏移
```

**示例：**
```
va = 0x763f4e211040

pageOffset = va & 0x1FFFFF = 0x011040

pa = (pte.pn << 21) | pageOffset
   = (0x1234567 << 21) | 0x011040
   = 0x2468ACE011040  // 最终物理地址
```

## PGT 的软件管理

### 软件视角的 PGT

在软件代码中（`rust-driver/src/rdma_utils/mtt.rs`）：

```rust
pub(crate) struct PgtEntry {
    pub(crate) index: u32,     // PGT 起始索引（对应硬件的 pgtOffset）
    pub(crate) count: u32,     // 连续分配的 PGT 条目数量
}
```

- `index`：对应硬件的 `PTEIndex`，表示在 PGT 表中的起始位置
- `count`：表示这个 MR 使用了多少个连续的 PGT 条目

### 软件分配 PGT 条目

从 `mtt.rs` 中的 `PgtAlloc`：

```rust
pub(crate) struct PgtAlloc {
    free_list: BitArray<[usize; PGT_LEN / 64]>,  // PGT_LEN = 0x20000 = 131072
}
```

软件维护一个位数组，跟踪 PGT 表中哪些条目是空闲的：
- `free_list[i] = false`：PGT[i] 空闲
- `free_list[i] = true`：PGT[i] 已使用

### 分配示例

```rust
// 注册一个跨越 3 个页的 MR
let num_pages = 3;
let pgt_entry = alloc.alloc_pgt(num_pages);
// pgt_entry = PgtEntry { index: 100, count: 3 }

// 这意味着分配了 PGT[100], PGT[101], PGT[102] 三个条目
```

软件随后需要将物理页号写入这些 PGT 条目：

```rust
// 假设物理地址为 [PA1, PA2, PA3]
PGT[100] = PA1 / PAGE_SIZE;  // 存储物理页号，不是完整地址
PGT[101] = PA2 / PAGE_SIZE;
PGT[102] = PA3 / PAGE_SIZE;
```

## 为什么需要 PGT？

### 1. 支持非连续物理内存

用户的虚拟地址空间可能是连续的，但对应的物理内存可能是分散的：

```
虚拟地址:  [0x1000_0000, 0x1040_0000)  ← 连续 4MB
           ↓
物理页:    PA1 (2MB) + PA2 (2MB)        ← 可能不连续
```

PGT 允许每个虚拟页独立映射到不同的物理页。

### 2. 减小 MTT 大小

如果 MTT 直接存储每个页的物理地址，对于大 MR 会非常占空间。通过两级表：
- MTT 只存储 `pgtOffset`（17 bits）而不是所有页的地址
- PGT 存储实际的物理页号

### 3. 共享物理页

我们实现的引用计数机制允许多个 MR 共享相同的 PGT 条目：

```
MR1: pgtOffset=100, baseVA=0x1000, count=2
MR2: pgtOffset=100, baseVA=0x1000, count=2  ← 共享 PGT[100], PGT[101]

PGT[100] = PA1  ← 被 MR1 和 MR2 共享
PGT[101] = PA2  ← 被 MR1 和 MR2 共享
```

## PGT 更新命令

### 软件到硬件的更新

从 `rust-driver/src/cmd/types.rs`：

```rust
pub(crate) struct PgtUpdate {
    pub(crate) dma_addr: PhysAddr,      // DMA 缓冲区物理地址（包含要写入的数据）
    pub(crate) base_pgt_offset: u32,    // PGT 起始索引
    pub(crate) num_pgt_entry: u32,      // 要更新的 PGT 条目数量
}
```

软件流程（from `ctx.rs:333-350`）：

```rust
// 准备 DMA 缓冲区，填充物理页号
let bytes: Vec<u8> = phys_addrs
    .take(count as usize)
    .flat_map(|pa| pa.as_u64().to_ne_bytes())  // 将 PhysAddr 转为字节
    .collect();
buf.copy_from(0, &bytes);

// 发送 PGT 更新命令
let pgt_update = PgtUpdate::new(
    mtt_buffer.phys_addr,  // DMA 缓冲区地址
    index,                 // PGT 起始索引
    count - 1              // 要更新的条目数（硬件使用 0-based）
);
self.cmd_controller.update_pgt(pgt_update);
```

硬件接收后，从 DMA 缓冲区读取数据并写入 PGT 表。

## 总结

**PGT 是什么？**
- 页表，存储物理页号的硬件数组
- 大小：131,072 个条目
- 每个条目存储一个 27-bit 物理页号

**PGT Index 是什么？**
- PGT 表中的索引，范围 0 ~ 131,071
- 17 bits 宽度
- 用于定位 PGT 表中的具体条目

**地址转换公式：**
```
pteIdx = pgtOffset + (getPageNumber(va) - getPageNumber(baseVA))
PA = PGT[pteIdx].pn * PAGE_SIZE + (va & 0x1FFFFF)
```

**引用计数的意义：**
- 多个 MR 可以共享相同的 pgtOffset
- 节省 PGT 条目，减少硬件更新
- 通过 refcount 安全管理 PGT 条目生命周期
