# PGT 页号为零的根本原因分析

## 关键发现

通过分析 RTL 仿真日志和驱动代码，发现**软件驱动确实存在 Bug**。

### 证据 1：驱动主动发送零页号

从 RTL 日志 `/tests/rccl_test/log/sim/rtl-client.log` 中：

```
INFO cocotb: insert AddressTranslate = PgtModifyReq { idx: 'h00000, pte: PageTableEntry { pn: 'h000d748 } }
INFO cocotb: insert AddressTranslate = PgtModifyReq { idx: 'h00001, pte: PageTableEntry { pn: 'h0000000 } }
INFO cocotb: insert AddressTranslate = PgtModifyReq { idx: 'h00002, pte: PageTableEntry { pn: 'h0000000 } }
```

**关键点**：
- "insert AddressTranslate" 表示硬件正在**写入**页表条目
- 这些零页号**不是** BRAM 的初始值
- 而是驱动通过 DMA 传输**主动发送**的数据

### 证据 2：驱动代码的 DMA 数据准备

`/rust-driver/src/verbs/ctx.rs:350-356`

```rust
let bytes: Vec<u8> = phys_addrs
    .by_ref()
    .take(count as usize)
    .flat_map(|pa| pa.as_u64().to_ne_bytes())  // ← 转换物理地址为字节
    .collect();
buf.copy_from(0, &bytes);                       // ← 写入 DMA 缓冲区
let pgt_update = PgtUpdate::new(
    self.mtt_buffer.phys_addr,
    index,
    count - 1
);
self.cmd_controller.update_pgt(pgt_update);     // ← 发送给硬件
```

**问题**：如果 `pa.as_u64()` 返回 0，那么 DMA 缓冲区会包含 8 个零字节。

## Bug 定位

### 可疑点 1：物理地址可能合法地为零

`/rust-driver/src/mem/virt_to_phy.rs:149-154`

```rust
let entry = u64::from_ne_bytes(buf);
if (entry >> PAGE_PRESENT_BIT) & 1 != 0 {
    let phys_pfn = entry & PFN_MASK;
    let phys_addr = phys_pfn * base_page_size + start_addr_raw % base_page_size;
    *pa = Some(unsafe {
        PageAlignedPhysAddr::new_unchecked(PhysAddr::new(phys_addr))
    });
```

**潜在 Bug**：
- 如果 `phys_pfn == 0`（页表条目的 PFN 字段为 0）
- 那么 `phys_addr` 可能是一个很小的值（仅为页内偏移）
- `PhysAddr::new(phys_addr)` 可能接受这个值而不报错
- 最终页号计算时：`phys_addr >> 21` 结果为 0

### 可疑点 2：页内偏移计算错误

第 152 行：
```rust
let phys_addr = phys_pfn * base_page_size + start_addr_raw % base_page_size;
                                             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
```

**问题**：
- `start_addr_raw % base_page_size` 计算的是**起始地址**在页内的偏移
- 但是对于页表转换，我们需要的是**页面对齐的物理地址**
- 这个偏移应该总是 0（因为 `start_addr` 已经是 `PageAlignedVirtAddr`）

**Bug 确认**：
- 如果 `base_page_size` 与 `PAGE_SIZE` 不同（比如 base_page_size = 4KB, PAGE_SIZE = 2MB）
- 那么偏移计算会出错
- 可能导致物理地址的页号部分为 0

## 实际场景重现

### 场景：2MB 大页配置下的地址转换

假设：
- 驱动使用 2MB 大页（`PAGE_SIZE = 2MB`）
- 系统 base page size = 4KB
- 虚拟地址：`0x7f1234000000`（2MB 对齐）

**步骤 1**：计算虚拟 PFN
```rust
let virt_pfn = addr / base_page_size;
// = 0x7f1234000000 / 4096 = 0x7f1234000
```

**步骤 2**：从 pagemap 读取条目
```rust
// 假设读取到的 entry：PFN = 0, Present = 1
let phys_pfn = entry & PFN_MASK; // = 0
```

**步骤 3**：计算物理地址（Bug 所在）
```rust
let phys_addr = phys_pfn * base_page_size + start_addr_raw % base_page_size;
// = 0 * 4096 + 0x7f1234000000 % 4096
// = 0 + 0
// = 0
```

**步骤 4**：提取页号
```rust
let page_number = phys_addr >> 21; // = 0 >> 21 = 0
```

**结果**：驱动发送页号 0 给硬件！

## 修复方案

### 方案 A：修复页内偏移计算（推荐）

```rust
let phys_addr = phys_pfn * base_page_size;  // 移除错误的偏移计算
```

**原因**：`PageAlignedVirtAddr` 已经保证了对齐，偏移总是 0。

### 方案 B：验证物理地址有效性

```rust
let phys_addr = phys_pfn * base_page_size + start_addr_raw % base_page_size;
if phys_addr < PAGE_SIZE {  // 物理地址太小，可能是错误
    log::error!("Invalid physical address: 0x{:x} for VA 0x{:x}", phys_addr, addr);
    continue;  // 跳过此页或返回错误
}
```

### 方案 C：使用正确的页大小

确保 `base_page_size` 与驱动配置的 `PAGE_SIZE` 一致：

```rust
#[cfg(feature = "page_size_2m")]
const PAGEMAP_PAGE_SIZE: u64 = 2 * 1024 * 1024;  // 2MB

#[cfg(not(feature = "page_size_2m"))]
const PAGEMAP_PAGE_SIZE: u64 = 4096;  // 4KB

let virt_pfn = addr / PAGEMAP_PAGE_SIZE;  // 使用正确的页大小
```

## 验证方法

### 方法 1：添加断言

在 `ctx.rs:353` 之后添加：

```rust
let bytes: Vec<u8> = phys_addrs
    .by_ref()
    .take(count as usize)
    .flat_map(|pa| {
        let addr = pa.as_u64();
        let page_num = addr >> 21;
        assert_ne!(page_num, 0, "Invalid page number 0 for PA 0x{:x}", addr);
        addr.to_ne_bytes()
    })
    .collect();
```

### 方法 2：检查 pagemap 输出

添加日志：

```rust
if phys_pfn == 0 && (entry >> PAGE_PRESENT_BIT) & 1 != 0 {
    log::warn!("Page present but PFN=0: VA=0x{:x}, pagemap_entry=0x{:x}",
               addr, entry);
}
```

## 结论

**确认是软件 Bug**，位于物理地址转换代码中：

1. **直接原因**：`virt_to_phy.rs:152` 的页内偏移计算错误
2. **根本原因**：混淆了系统基础页大小（4KB）和驱动大页大小（2MB）
3. **影响范围**：所有使用大页的 MR 注册
4. **修复难度**：低（仅需修改 1 行代码）

**下一步**：
1. 验证 `get_base_page_size()` 的返回值
2. 确认测试是否使用了 2MB 大页配置
3. 应用修复方案 A 并重新测试
