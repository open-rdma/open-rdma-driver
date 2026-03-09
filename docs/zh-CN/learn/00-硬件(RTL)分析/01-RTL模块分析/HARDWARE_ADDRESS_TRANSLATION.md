# 硬件地址转换详解：VA → PA 转换流程与缓存架构

## 概述

RDMA 硬件使用**两级表 + BRAM 缓存**机制实现虚拟地址到物理地址的转换。

```
虚拟地址 (VA)
    ↓
[Level 1] MTT (Memory Translation Table)
    → BRAM Cache (1-way split)
    → 返回 {pgtOffset, baseVA, length, ...}
    ↓
[Level 2] PGT (Page Table)
    → BRAM Cache (4-way split)
    → 返回 {PageNumber}
    ↓
物理地址 (PA) = PageNumber * PAGE_SIZE + PageOffset
```

## 一、硬件缓存架构

### 1. BramCache 通用缓存模块

从 `MemRegionAndAddressTranslate.bsv:28-90`：

```bsv
interface BramCache#(type addrType, type dataType, numeric type splitCntExp);
    interface BramRead #(addrType, dataType)   read;
    interface BramWrite#(addrType, dataType)   write;
endinterface

module mkBramCache(BramCache#(addrType, dataType, splitCntExp));
    // 创建 2^splitCntExp 个子 BRAM 块
    Vector#(TExp#(splitCntExp), AutoInferBram#(subAddrType, dataType))
        subBramVec <- replicateM(mkAutoInferBramUG(...));

    // 读请求队列
    PipeInAdapterB0#(addrType) bramReadReqQ <- mkPipeInAdapterB0;
    FIFOF#(dataType) bramReadRespQ <- mkLFIFOF;

    // 写请求队列
    PipeInAdapterB0#(Tuple2#(addrType, dataType)) bramWriteReqQ <- mkPipeInAdapterB0;
    FIFOF#(Bool) bramWriteRespQ <- mkLFIFOF;
endmodule
```

**关键特性：**
- **分块 BRAM（Split BRAM）**：将大表分割为 `2^splitCntExp` 个子块
- **流水线访问**：通过 FIFO 队列支持连续请求
- **自动推断 BRAM**：硬件综合工具自动生成 Block RAM

### 2. MTT 缓存配置

从 `MemRegionAndAddressTranslate.bsv:100`：

```bsv
module mkMemRegionTable(MemRegionTable);
    BramCache#(IndexMR, Maybe#(MemRegionTableEntry), 1) mrTableStorage <- mkBramCache;
    //           ↑ 索引     ↑ 数据类型                  ↑ 1-way split (2^1 = 2 个子块)
endmodule
```

**MTT 缓存配置：**
- **容量**：`MAX_MR_CNT = 8192` 个 MR 条目
- **分块**：2 个子 BRAM（1-way split）
- **每个子块**：4096 个条目
- **条目大小**：~128 bits（包含 pgtOffset, baseVA, len, flags 等）

### 3. PGT 缓存配置

从 `MemRegionAndAddressTranslate.bsv:269`：

```bsv
module mkAddressTranslate(AddressTranslate);
    BramCache#(PTEIndex, PageTableEntry, 4) pageTableStorage <- mkBramCache;
    //           ↑ 索引    ↑ 数据类型        ↑ 4-way split (2^4 = 16 个子块)
endmodule
```

**PGT 缓存配置：**
- **容量**：`MAX_PTE_ENTRY_CNT = 2^17 = 131,072` 个页表条目
- **分块**：16 个子 BRAM（4-way split）
- **每个子块**：8,192 个条目
- **条目大小**：27 bits（物理页号，不包含页内偏移）

### 4. TLB 缓存定义（未实现 TLB，使用 BRAM）

从 `BasicDataTypes.bsv:111-113`：

```bsv
typedef TExp#(14) TLB_CACHE_SIZE;        // TLB 缓存大小 = 16K 条目
typedef TLog#(TLB_CACHE_SIZE) TLB_CACHE_INDEX_WIDTH;  // 14 bits
typedef TSub#(TSub#(ADDR_WIDTH, TLB_CACHE_INDEX_WIDTH), PAGE_OFFSET_WIDTH) TLB_CACHE_TAG_WIDTH;
// 64 - 14 - 21 = 29 bits
```

**注意：** 代码中定义了 TLB 相关常量，但实际硬件中**没有实现传统的 TLB**，而是直接使用**分块 BRAM 作为页表缓存**。

## 二、完整的 VA → PA 转换流程

### 阶段 1：MTT 查询

**输入：** `MR Key`（32 bits）

**查询流程：**

```bsv
// 1. 构造 MTT 查询请求
MrTableQueryReq req = { idx: lkey2IndexMR(mr_key) };

// 2. 发送到 MTT BRAM Cache
mrTableStorage.read.request.firstIn(req.idx);

// 3. 等待 MTT 响应（流水线延迟 2-3 个周期）
Maybe#(MemRegionTableEntry) mrEntry <- mrTableStorage.read.response.first;

// 4. 提取 MR 信息
if (mrEntry matches tagged Valid .entry) {
    PTEIndex pgtOffset = entry.pgtOffset;    // PGT 起始索引
    ADDR baseVA = entry.baseVA;              // MR 基虚拟地址
    Length len = entry.len;                  // MR 长度
    ...
}
```

**BRAM 分块寻址：**
```
IndexMR (13 bits) = mr_key >> 8
    ↓ 分解
subBlockIdx (1 bit)  = IndexMR[12]     // 选择 2 个子块中的哪一个
subAddr (12 bits)    = IndexMR[11:0]   // 子块内的地址
    ↓
访问 mrTableStorage.subBramVec[subBlockIdx][subAddr]
```

### 阶段 2：计算 PGT 索引

**输入：**
- `va`（要转换的虚拟地址）
- `baseVA`（从 MTT 获取）
- `pgtOffset`（从 MTT 获取）

**计算流程：**

```bsv
// from MemRegionAndAddressTranslate.bsv:283-284
let pageNumberOffset = getPageNumber(va) - getPageNumber(baseVA);
PTEIndex pteIdx = pgtOffset + truncate(pageNumberOffset);
```

**详细步骤：**
```
1. 提取页号
   pageNumber_va = va >> 21           // 右移 21 bits（2MB 页大小）
   pageNumber_baseVA = baseVA >> 21

2. 计算页偏移
   pageNumberOffset = pageNumber_va - pageNumber_baseVA

3. 加上 PGT 起始偏移
   pteIdx = pgtOffset + pageNumberOffset
```

**示例：**
```
va = 0x763f4e411040
baseVA = 0x763f4e011040
pgtOffset = 100

pageNumber_va = 0x763f4e411040 >> 21 = 0x3b1fa720
pageNumber_baseVA = 0x763f4e011040 >> 21 = 0x3b1fa700

pageNumberOffset = 0x3b1fa720 - 0x3b1fa700 = 0x20 = 32

pteIdx = 100 + 32 = 132
```

### 阶段 3：PGT 查询

**输入：** `pteIdx`（PGT 索引）

**查询流程：**

```bsv
// 1. 发送到 PGT BRAM Cache
pageTableStorage.read.request.firstIn(pteIdx);

// 2. 等待 PGT 响应（流水线延迟 2-3 个周期）
PageTableEntry pte <- pageTableStorage.read.response.first;

// 3. 提取物理页号
PageNumber physPageNum = pte.pn;  // 27 bits
```

**BRAM 16-way 分块寻址：**
```
PTEIndex (17 bits) = pteIdx
    ↓ 分解
subBlockIdx (4 bits)  = pteIdx[16:13]  // 选择 16 个子块中的哪一个
subAddr (13 bits)     = pteIdx[12:0]   // 子块内的地址（0-8191）
    ↓
访问 pageTableStorage.subBramVec[subBlockIdx][subAddr]
```

### 阶段 4：组装物理地址

**输入：**
- `physPageNum`（从 PGT 获取，27 bits）
- `va`（原始虚拟地址）

**组装流程：**

```bsv
// from MemRegionAndAddressTranslate.bsv:288, 300
let pageOffset = getPageOffset(va);      // 提取低 21 bits
let pa = restorePA(pte.pn, pageOffset);  // 组合

// restorePA 定义 (line 258-260)
function ADDR restorePA(PageNumber pn, PageOffset po);
    return signExtend({ pn, po });
endfunction
```

**位拼接：**
```
PageNumber pn (27 bits):  0x1234567
PageOffset po (21 bits):  0x011040
    ↓ 拼接
PA (48 bits) = {pn, po} = 0x2468ACE011040
```

## 三、硬件流水线设计

### 1. MTT 查询流水线

```
时钟周期   操作
    0      发送 MTT 读请求（mr_key → IndexMR）
    1      BRAM 访问周期 1（选择子块）
    2      BRAM 访问周期 2（读取数据）
    3      返回 MemRegionTableEntry
```

### 2. PGT 查询流水线

```
时钟周期   操作
    0      计算 pteIdx（pgtOffset + pageNumberOffset）
    1      发送 PGT 读请求
    2      BRAM 访问周期 1（选择子块）
    3      BRAM 访问周期 2（读取数据）
    4      返回 PageTableEntry
```

### 3. 完整流水线

```
时钟周期   MTT 阶段              PGT 阶段                组装阶段
    0      请求 MR 条目           -                      -
    1      BRAM 访问 1            -                      -
    2      BRAM 访问 2            -                      -
    3      返回 mrEntry           计算 pteIdx             -
    4      -                     请求 PGT 条目           -
    5      -                     BRAM 访问 1             -
    6      -                     BRAM 访问 2             -
    7      -                     返回 pte                组装 PA

总延迟：约 7-8 个时钟周期
```

### 4. 多路查询仲裁

为了提高吞吐量，硬件支持**多路并发查询**：

**Two-Way Query（2 路查询）：**
```bsv
module mkAddressTranslateTwoWayQuery(AddressTranslateTwoWayQuery);
    AddressTranslate addressTranslate <- mkAddressTranslate;

    // 仲裁器：2 个查询端口共享 1 个 PGT 表
    let arbiter <- mkServerToClientArbitFixPriorityP(
        10,                    // 队列深度 10
        True,                  // 固定优先级
        alwaysTrue,
        alwaysTrue,
        DebugConf{...}
    );

    mkConnection(arbiter.cltIfc, addressTranslate.translateSrv);
    interface querySrvVec = arbiter.srvIfcVec;  // 2 个查询接口
endmodule
```

**Eight-Way Query（8 路查询）：**
```bsv
module mkAddressTranslateEightWayQuery(AddressTranslateEightWayQuery);
    // 4 个 Two-Way Query 实例
    Vector#(NUMERIC_TYPE_FOUR, AddressTranslateTwoWayQuery)
        twoWayAddressTranslateVec <- replicateM(mkAddressTranslateTwoWayQuery);

    // 8 个查询端口 = 4 * 2
    interface querySrvVec = querySrvVecInst;  // 8 个查询接口
endmodule
```

**架构图：**
```
8 个查询请求端口
    ↓
分配到 4 个 PGT 实例（每个有 2 路仲裁）
    ↓
每个 PGT 实例访问独立的 BRAM Cache
    ↓
并行返回查询结果
```

## 四、缓存命中与未命中

### BRAM 特性

**BRAM（Block RAM）** 是 FPGA 内部的片上存储器：
- **无缓存未命中**：所有数据都在 BRAM 中，没有"未命中"概念
- **固定延迟**：访问延迟固定为 2-3 个时钟周期
- **容量有限**：受 FPGA BRAM 资源限制

### 与传统 TLB 的对比

| 特性 | 传统 TLB | 本设计的 BRAM Cache |
|-----|---------|-------------------|
| 缓存方式 | 最近使用的页表条目 | 所有页表条目 |
| 容量 | 通常 64-512 个条目 | MTT: 8192, PGT: 131072 |
| 未命中处理 | 访问内存页表 | 不存在未命中 |
| 访问延迟 | 命中: 1 周期, 未命中: 100+ 周期 | 固定 2-3 周期 |
| 硬件资源 | 少量寄存器 + 比较器 | 大量 BRAM |

**设计理由：**
- FPGA 有丰富的 BRAM 资源
- 固定延迟简化硬件设计
- 避免 TLB 未命中导致的性能抖动
- 支持高并发的多路查询

## 五、PGT 更新流程

### 软件发起 PGT 更新

从 `ctx.rs:333-350`：

```rust
// 1. 准备 DMA 缓冲区，填充物理页号
let bytes: Vec<u8> = phys_addrs
    .take(count as usize)
    .flat_map(|pa| pa.as_u64().to_ne_bytes())  // u64 -> 字节流
    .collect();
buf.copy_from(0, &bytes);

// 2. 发送 PGT 更新命令
let pgt_update = PgtUpdate::new(
    mtt_buffer.phys_addr,  // DMA 缓冲区物理地址
    index,                 // PGT 起始索引
    count - 1              // 更新的条目数（0-based）
);
self.cmd_controller.update_pgt(pgt_update);
```

### 硬件处理 PGT 更新

从 `MemRegionAndAddressTranslate.bsv:519-600`：

```bsv
// 1. 接收 PGT 更新描述符
CmdQueueOpcodeUpdatePGT: begin
    let desc <- req.first;

    // 2. 发起 DMA 读取（从软件的 DMA 缓冲区读取）
    let dmaReadReq = PgtUpdateDmaReadReq {
        addr: desc.dmaAddr,           // DMA 缓冲区地址
        numBytes: ...                 // 要读取的字节数
    };
    dmaReadReqPipeOutQ.enq(dmaReadReq);

    // 3. 接收 DMA 数据并写入 PGT
    for each DMA beat:
        let physPageNum = extract_from_dma_data(data);
        let pte = PageTableEntry { pn: physPageNum };
        let pgtModifyReq = PgtModifyReq {
            idx: desc.pgtOffset + i,
            pte: pte
        };
        pgtModifyCltInst.putReq(pgtModifyReq);
end
```

**流程图：**
```
软件                         硬件
 |                            |
 |  1. 准备 DMA 缓冲区         |
 |     (物理页号数组)          |
 |                            |
 |  2. 发送 PgtUpdate 命令  →  |
 |                            |  3. 发起 DMA 读
 |                            |     ↓
 |                            |  4. 从 PCIe 读取数据
 |  5. DMA 数据传输        ←   |
 |                            |     ↓
 |                            |  6. 提取物理页号
 |                            |     ↓
 |                            |  7. 写入 PGT BRAM
 |                            |     (pageTableStorage.write)
 |                            |     ↓
 |                            |  8. 返回响应
 |  9. 完成                ←   |
```

## 六、性能分析

### 1. 访问延迟

**单次地址转换延迟：**
- MTT 查询：2-3 周期
- 计算 pteIdx：1 周期
- PGT 查询：2-3 周期
- 组装 PA：1 周期
- **总计：6-8 个时钟周期**

假设时钟频率 250 MHz：
- 每周期 = 4 ns
- 总延迟 = 24-32 ns

### 2. 吞吐量

**单个 PGT 实例：**
- 流水线化设计，可以每个周期接收 1 个新请求
- 理论吞吐量 = 250M 请求/秒

**Eight-Way Query（8 路查询）：**
- 8 个并发查询端口
- 理论吞吐量 = 8 * 250M = 2G 请求/秒

### 3. BRAM 资源消耗

以 Xilinx UltraScale+ FPGA 为例：

**MTT BRAM：**
- 容量：8192 条目 × 128 bits = 1 Mbit
- BRAM 块数：约 1-2 个 BRAM36K

**PGT BRAM（单个实例）：**
- 容量：131,072 条目 × 27 bits ≈ 3.5 Mbit
- BRAM 块数：约 98 个 BRAM36K

**Eight-Way Query（4 个 PGT 实例）：**
- 总 BRAM：约 400 个 BRAM36K
- 对于大型 FPGA（如 VU9P，有 2160 个 BRAM36K）占用约 18%

## 七、总结

### 缓存架构特点

1. **分块 BRAM 设计**：
   - MTT: 2-way split
   - PGT: 16-way split
   - 提高并行访问能力

2. **无缓存未命中**：
   - 所有页表数据存储在 BRAM 中
   - 固定延迟，无抖动
   - 简化硬件设计

3. **多路查询支持**：
   - 2-way 和 8-way 查询接口
   - 通过仲裁器共享 BRAM 资源
   - 提高系统吞吐量

4. **流水线优化**：
   - MTT 和 PGT 查询流水线化
   - 支持连续请求
   - 每周期可发起新的转换请求

### 与软件引用计数的配合

**软件优化（我们的实现）：**
- 多个 MR 共享相同的 `pgtOffset`
- 减少 PGT 条目使用
- 减少 PGT 更新命令

**对硬件的影响：**
- ✅ 减少 PGT BRAM 使用率
- ✅ 减少 DMA 传输次数
- ✅ 减少命令队列压力
- ✅ 不影响查询延迟和吞吐量

**结论：软件优化与硬件设计完美配合，既节省资源又保持性能！**
