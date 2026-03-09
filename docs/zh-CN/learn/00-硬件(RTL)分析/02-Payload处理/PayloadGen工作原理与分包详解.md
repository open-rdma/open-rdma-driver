# PayloadGen 工作原理与分包机制详解

## 1. 概述

**PayloadGen** (Payload Generator) 是 RDMA 发送路径中负责**从主机内存读取数据**的模块。它与 PacketGen 协作，但处理的是不同层次的分包问题。

**位置**: `/home/peng/projects/rdma_all/open-rdma-rtl/src/PayloadGenAndCon.bsv:100`

**核心职责**:
1. 从主机内存通过 DMA 读取 RDMA 有效载荷
2. 根据 **PCIe 最大突发大小** 分包（而非 PMTU）
3. 虚拟地址到物理地址转换
4. 合并多个 DMA 响应成连续数据流

## 2. PayloadGen 与 PacketGen 的分包区别

### 2.1 两层分包机制

RDMA 发送路径实际上有**两层分包**：

```
                软件层
                  ↓
            Work Queue Element (WQE)
            (len = 8192, laddr, raddr)
                  ↓
        ┌─────────────────────────┐
        │   PayloadGen 分包       │ ← DMA 层分包
        │   (按 PCIe 突发大小)    │
        └───────────┬─────────────┘
                    ↓
        DMA Burst 1: 512 字节
        DMA Burst 2: 512 字节
        ...
        DMA Burst 16: 512 字节
                    ↓
            连续数据流 (8192 字节)
                    ↓
        ┌─────────────────────────┐
        │   PacketGen 分包        │ ← 网络层分包
        │   (按 PMTU)             │
        └───────────┬─────────────┘
                    ↓
        RDMA Packet 1: 4096 字节
        RDMA Packet 2: 4096 字节
                    ↓
               Ethernet 帧
```

### 2.2 对比表

| 特性 | PayloadGen | PacketGen |
|------|------------|-----------|
| **分包依据** | PCIe 最大突发大小 (PCIE_MAX_BYTE_IN_BURST) | PMTU (Path MTU) |
| **典型大小** | 512 字节 (可配置) | 256/512/1024/2048/4096 字节 |
| **目的** | 满足 PCIe DMA 限制 | 满足网络 MTU 限制 |
| **层次** | DMA 层 | 网络层 |
| **数据处理** | 分块读取 → 合并成连续流 | 连续流 → 拆分成多个包 |
| **地址对齐** | 本地地址对齐 | 远程地址对齐 |
| **输出** | 单个长数据流 | 多个短数据流（每个对应一个 RDMA 包） |

### 2.3 为什么需要两层分包？

**PayloadGen 分包的原因**:
1. **PCIe 限制**: PCIe 总线有最大突发传输大小限制
2. **页表边界**: 虚拟地址可能跨越多个物理页
3. **DMA 效率**: 小块 DMA 传输更灵活，延迟更低
4. **资源限制**: PCIe Tag 数量有限（通常 32-64 个）

**PacketGen 分包的原因**:
1. **网络 MTU**: 以太网帧有最大传输单元限制
2. **RDMA 协议**: RDMA 规范定义了 PMTU
3. **可靠传输**: 小包便于重传
4. **流控**: 小包便于拥塞控制

## 3. PayloadGen 接口定义

### 3.1 接口结构

```bsv
interface PayloadGen;
    // 地址转换客户端接口
    interface ClientP#(PgtAddrTranslateReq, ADDR) addrTranslateClt;

    // Payload 生成请求输入
    interface PipeInB0#(PayloadGenReq) genReqPipeIn;

    // Payload 数据流输出
    interface PipeOut#(IoChannelMemoryAccessDataStream) payloadGenStreamPipeOut;

    // DMA 读取主接口
    interface IoChannelMemoryReadMasterPipeB0In dmaReadMasterPipe;
endinterface
```

### 3.2 请求数据结构

```bsv
typedef struct {
    ADDR     addr;        // 本地虚拟地址 (64 位)
    Length   len;         // 数据长度 (32 位)
    PTEIndex pgtOffset;   // 页表偏移（用于地址转换）
    ADDR     baseVA;      // 基虚拟地址（用于地址转换）
} PayloadGenReq deriving(Bits, FShow);
```

**字段说明**:
- **addr**: WQE 中的 `laddr`（本地地址）
- **len**: WQE 中的 `len`（数据长度）
- **pgtOffset**: 从 MR Table 查询得到
- **baseVA**: 从 MR Table 查询得到

### 3.3 输出数据流

```bsv
typedef struct {
    DATA data;           // 数据 (256 或 512 位)
    ByteEnInBeat byteEn; // 字节使能
    Bool isFirst;        // 是否是数据流的第一个 beat
    Bool isLast;         // 是否是数据流的最后一个 beat
} IoChannelMemoryAccessDataStream deriving(Bits, FShow);
```

**关键特性**:
- 单个长数据流（不分段）
- 按本地地址对齐
- 连续输出，无间隙

## 4. PayloadGen 的分包机制

### 4.1 PCIe 最大突发大小

**配置** (`Settings.bsv:36-40`):

```bsv
typedef `PCIE_MAX_BEAT_CNT_IN_BURST PCIE_MAX_BEAT_CNT_IN_BURST;  // 默认 16
typedef 32 PCIE_BYTE_PER_BEAT;  // 256 位总线 = 32 字节

// PCIe 最大突发大小 = 16 * 32 = 512 字节
typedef TMul#(PCIE_MAX_BEAT_CNT_IN_BURST, PCIE_BYTE_PER_BEAT) PCIE_MAX_BYTE_IN_BURST;
```

**典型值**:
```
PCIE_MAX_BEAT_CNT_IN_BURST = 16
PCIE_BYTE_PER_BEAT = 32
PCIE_MAX_BYTE_IN_BURST = 512 字节
```

**为什么是 512 字节**?
1. PCIe 协议的 TLP (Transaction Layer Packet) 大小限制
2. 平衡延迟和吞吐量
3. 避免 PCIe Tag 耗尽
4. 便于页表查询（通常一个页 4KB，512 字节不易跨页）

### 4.2 分包过程

**Stage 1: handleInReq** (行 146-166)

```bsv
rule handleInReq;
    let req = genReqPipeInQ.first;
    genReqPipeInQ.deq;

    // ========== 创建地址分块请求 ==========
    let chunkReq = AddressChunkReq{
        startAddr: req.addr,      // 本地虚拟地址
        len: req.len,             // 总长度
        chunk: fromInteger(valueOf(TLog#(PCIE_MAX_BYTE_IN_BURST)))  // log2(512) = 9
    };

    // 发送给 AddressChunker
    rawReqToBurstChunkerRequestPipeInAdapter.enq(chunkReq);

    // 保存页表信息，用于后续地址转换
    getBurstChunRespAndIssueAddrTranslateReqPipelineQ.enq(
        tuple3(req.pgtOffset, req.baseVA, curFpDebugTime)
    );
endrule
```

**AddressChunker 的作用**:

与 PacketGen 中的 AddressChunker 相同，但分块大小不同：

```
输入示例:
  startAddr = 0x100000
  len = 8192 (8K)
  chunk = 9 (2^9 = 512 字节)

输出:
  块 1: {startAddr: 0x100000, len: 512, isFirst: True,  isLast: False}
  块 2: {startAddr: 0x100200, len: 512, isFirst: False, isLast: False}
  块 3: {startAddr: 0x100400, len: 512, isFirst: False, isLast: False}
  ...
  块 16: {startAddr: 0x101E00, len: 512, isFirst: False, isLast: True}

总共 16 个块，每个 512 字节
```

**非对齐地址示例**:

```
输入:
  startAddr = 0x100100 (偏移 256 字节)
  len = 8192
  chunk = 9

输出:
  块 1: {startAddr: 0x100100, len: 256, isFirst: True,  isLast: False}  ← 第一块较短
  块 2: {startAddr: 0x100200, len: 512, isFirst: False, isLast: False}
  ...
  块 16: {startAddr: 0x101E00, len: 512, isFirst: False, isLast: False}
  块 17: {startAddr: 0x102000, len: 256, isFirst: False, isLast: True}   ← 最后一块较短

总共 17 个块
```

### 4.3 虚拟地址到物理地址转换

**Stage 2: getBurstChunRespAndIssueAddrTranslateReq** (行 169-192)

```bsv
rule getBurstChunRespAndIssueAddrTranslateReq
    if (dmaReadReqPipeOutQ.notFull && dsConcatorIsLastStreamFlagPipeInConverter.notFull);

    // ========== 获取一个地址块 ==========
    let burstAddrBoundry = rawReqToBurstChunker.responsePipeOut.first;
    rawReqToBurstChunker.responsePipeOut.deq;

    // ========== 获取页表信息 ==========
    let {pgtOffset, baseVA, fpDebugTime} = getBurstChunRespAndIssueAddrTranslateReqPipelineQ.first;
    if (burstAddrBoundry.isLast) begin
        getBurstChunRespAndIssueAddrTranslateReqPipelineQ.deq;  // 最后一个块才出队
    end

    // ========== 发起地址转换请求 ==========
    let addrTranslateReq = PgtAddrTranslateReq {
        pgtOffset: pgtOffset,              // 页表偏移
        baseVA: baseVA,                    // 基虚拟地址
        addrToTrans: burstAddrBoundry.startAddr  // 要转换的虚拟地址
    };
    addrTranslateCltInst.putReq(addrTranslateReq);

    // ========== 传递到下一级流水线 ==========
    issueDmaReadPipelineQ.enq(tuple3(burstAddrBoundry.len, burstAddrBoundry.isLast, curFpDebugTime));
endrule
```

**地址转换过程**:

```
虚拟地址 (VA)
    ↓
页表查询 (Page Table Lookup)
    ↓
物理地址 (PA)
```

**示例**:
```
VA: 0x100000 + pgtOffset + baseVA → PA: 0x8000000000
VA: 0x100200 + pgtOffset + baseVA → PA: 0x8000000200
VA: 0x100400 + pgtOffset + baseVA → PA: 0x8000000400
...
```

**关键点**:
- 每个块都需要独立的地址转换
- 虚拟地址连续，但物理地址可能不连续（跨页情况）
- 地址转换有延迟（查询页表），因此使用流水线

### 4.4 发起 DMA 读取

**Stage 3: issueDmaRead** (行 194-217)

```bsv
rule issueDmaRead;
    // ========== 获取转换后的物理地址 ==========
    let translatedAddr <- addrTranslateCltInst.getResp;

    // ========== 获取长度和标志 ==========
    let {len, isLast, fpDebugTime} = issueDmaReadPipelineQ.first;
    issueDmaReadPipelineQ.deq;

    // ========== 创建 DMA 读取请求 ==========
    let readReq = DtldStreamMemAccessMeta {
        addr: translatedAddr,     // 物理地址
        totalLen: len,            // 块长度（≤ 512 字节）
        accessType: MemAccessTypeNormalReadWrite,
        operand_1: 0,
        operand_2: 0,
        noSnoop: False
    };

    // ========== 发送 DMA 读取请求 ==========
    dmaReadReqPipeOutGuardQ.enq(readReq);

    // ========== 标记是否是最后一个块 ==========
    dsConcatorIsLastStreamFlagPipeInConverterGuardQueue.enq(isLast);
endrule
```

**DMA 读取请求**:

```
DMA Burst 1: PA=0x8000000000, len=512
DMA Burst 2: PA=0x8000000200, len=512
DMA Burst 3: PA=0x8000000400, len=512
...
DMA Burst 16: PA=0x8000001E00, len=512
```

**关键设计**:
- 每个 DMA 请求最多读取 512 字节
- 物理地址可能不连续（如果跨页）
- 使用 `isLast` 标志标记最后一个块

### 4.5 合并 DMA 响应

**DtldStreamConcator** (数据流合并器)

PayloadGen 使用 `DtldStreamConcator` 将多个 DMA 响应合并成一个连续的数据流：

```bsv
DtldStreamConcator#(DATA, LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE) dsConcator <-
    mkDtldStreamConcator(DebugConf{name: "mkPayloadGen dsConcator", enableDebug: False});
```

**工作原理**:

```
输入 (多个 DMA 响应，每个是一个子流):
  DMA Burst 1:
    Beat 1-16: [512 字节], isFirst=T/F, isLast=F/T (子流 1)

  DMA Burst 2:
    Beat 1-16: [512 字节], isFirst=T/F, isLast=F/T (子流 2)

  ...

  DMA Burst 16:
    Beat 1-16: [512 字节], isFirst=T/F, isLast=F/T (子流 16)

输出 (单个连续数据流):
  Beat 1-256: [8192 字节], isFirst=T (beat 1), isLast=T (beat 256)
```

**合并逻辑**:

```bsv
interface DtldStreamConcator;
    // 输入：isLast 标志（每个子流一个）
    interface PipeInB0#(Bool) isLastStreamFlagPipeIn;

    // 输入：数据流
    interface PipeIn#(DataStream) dataPipeIn;

    // 输出：合并后的数据流
    interface PipeOut#(DataStream) dataPipeOut;
endinterface
```

**Concator 规则** (简化):

```bsv
Reg#(Bool) isProcessingStream <- mkReg(False);
Reg#(Bool) isCurrentSubStreamLast <- mkRegU;

rule startNewSubStream if (!isProcessingStream);
    let isLastFlag = isLastStreamFlagPipeIn.first;
    isLastStreamFlagPipeIn.deq;
    isCurrentSubStreamLast <= isLastFlag;
    isProcessingStream <= True;

    let ds = dataPipeIn.first;
    dataPipeIn.deq;

    // 第一个子流的第一个 beat 标记 isFirst=True
    dataPipeOut.enq(DataStream{
        data: ds.data,
        byteEn: ds.byteEn,
        isFirst: True,        // 整个数据流的第一个 beat
        isLast: (isLastFlag && ds.isLast)  // 如果是最后子流的最后 beat
    });
endrule

rule continueSubStream if (isProcessingStream);
    let ds = dataPipeIn.first;
    dataPipeIn.deq;

    if (ds.isLast) begin
        isProcessingStream <= False;  // 当前子流结束
    end

    dataPipeOut.enq(DataStream{
        data: ds.data,
        byteEn: ds.byteEn,
        isFirst: False,
        isLast: (isCurrentSubStreamLast && ds.isLast)  // 如果是最后子流的最后 beat
    });
endrule
```

**关键点**:
- 通过 `isLastStreamFlagPipeIn` 知道哪个子流是最后一个
- 只有最后一个子流的最后一个 beat 才标记 `isLast=True`
- 中间的所有 beat 都标记 `isFirst=False, isLast=False`

## 5. 完整的数据流示例

假设一个 8192 字节的数据读取请求：

### 5.1 输入请求

```bsv
PayloadGenReq {
    addr: 0x100000,       // 本地虚拟地址
    len: 8192,            // 8K 数据
    pgtOffset: 0x1000,    // 页表偏移
    baseVA: 0x200000000   // 基虚拟地址
}
```

### 5.2 地址分块（16 个块）

```
AddressChunker 输出 (chunk=9, 512 字节):

块 1:  {startAddr: 0x100000, len: 512, isFirst: T, isLast: F}
块 2:  {startAddr: 0x100200, len: 512, isFirst: F, isLast: F}
块 3:  {startAddr: 0x100400, len: 512, isFirst: F, isLast: F}
块 4:  {startAddr: 0x100600, len: 512, isFirst: F, isLast: F}
块 5:  {startAddr: 0x100800, len: 512, isFirst: F, isLast: F}
块 6:  {startAddr: 0x100A00, len: 512, isFirst: F, isLast: F}
块 7:  {startAddr: 0x100C00, len: 512, isFirst: F, isLast: F}
块 8:  {startAddr: 0x100E00, len: 512, isFirst: F, isLast: F}
块 9:  {startAddr: 0x101000, len: 512, isFirst: F, isLast: F}
块 10: {startAddr: 0x101200, len: 512, isFirst: F, isLast: F}
块 11: {startAddr: 0x101400, len: 512, isFirst: F, isLast: F}
块 12: {startAddr: 0x101600, len: 512, isFirst: F, isLast: F}
块 13: {startAddr: 0x101800, len: 512, isFirst: F, isLast: F}
块 14: {startAddr: 0x101A00, len: 512, isFirst: F, isLast: F}
块 15: {startAddr: 0x101C00, len: 512, isFirst: F, isLast: F}
块 16: {startAddr: 0x101E00, len: 512, isFirst: F, isLast: T}  ← 最后一个块
```

### 5.3 地址转换（16 次）

```
VA: 0x100000 → 页表查询 → PA: 0x8000000000
VA: 0x100200 → 页表查询 → PA: 0x8000000200
VA: 0x100400 → 页表查询 → PA: 0x8000000400
...
VA: 0x101E00 → 页表查询 → PA: 0x8000001E00
```

### 5.4 DMA 读取（16 个请求）

```
DMA Read 1:  PA=0x8000000000, len=512, isLast=F
DMA Read 2:  PA=0x8000000200, len=512, isLast=F
DMA Read 3:  PA=0x8000000400, len=512, isLast=F
...
DMA Read 16: PA=0x8000001E00, len=512, isLast=T
```

### 5.5 DMA 响应（16 个子流）

假设 256 位总线（32 字节/beat），每个 512 字节响应包含 16 个 beats：

```
DMA Response 1 (子流 1):
  Beat 1:  [32 bytes], isFirst=T, isLast=F
  Beat 2:  [32 bytes], isFirst=F, isLast=F
  ...
  Beat 16: [32 bytes], isFirst=F, isLast=T  ← 子流 1 结束

DMA Response 2 (子流 2):
  Beat 17: [32 bytes], isFirst=T, isLast=F
  Beat 18: [32 bytes], isFirst=F, isLast=F
  ...
  Beat 32: [32 bytes], isFirst=F, isLast=T  ← 子流 2 结束

...

DMA Response 16 (子流 16):
  Beat 241: [32 bytes], isFirst=T, isLast=F
  Beat 242: [32 bytes], isFirst=F, isLast=F
  ...
  Beat 256: [32 bytes], isFirst=F, isLast=T  ← 子流 16 结束
```

### 5.6 合并后的输出（单个数据流）

```
payloadGenStreamPipeOut 输出:

  Beat 1:   [32 bytes], isFirst=T, isLast=F  ← 整个数据流开始
  Beat 2:   [32 bytes], isFirst=F, isLast=F
  Beat 3:   [32 bytes], isFirst=F, isLast=F
  ...
  Beat 255: [32 bytes], isFirst=F, isLast=F
  Beat 256: [32 bytes], isFirst=F, isLast=T  ← 整个数据流结束

总计: 256 beats × 32 bytes = 8192 bytes
```

## 6. 性能优化要点

### 6.1 流水线深度配置

```bsv
// DMA 读取请求队列（大容量）
FIFOF#(IoChannelMemoryAccessMeta) dmaReadReqPipeOutQ <- mkSizedFIFOF(256 - 16);  // 240 槽位
FIFOF#(IoChannelMemoryAccessMeta) dmaReadReqPipeOutGuardQ <- mkSizedFIFOF(16);   // 16 槽位保护

// isLast 标志队列（大容量）
let dsConcatorIsLastStreamFlagPipeInConverter <- mkPipeInB0ToPipeIn(
    dsConcator.isLastStreamFlagPipeIn, 256 - 16);  // 240 槽位
FIFOF#(Bool) dsConcatorIsLastStreamFlagPipeInConverterGuardQueue <- mkSizedFIFOF(16);  // 16 槽位保护
```

**为什么需要大容量队列**?

1. **PCIe Tag 限制**: PCIe 通常有 32-64 个 Tag，允许最多 32-64 个未完成的读请求
2. **地址转换延迟**: 页表查询需要时间（~10-50 个时钟周期）
3. **DMA 读取延迟**: PCIe 读取延迟较大（~100-200 个时钟周期）

**设计策略**:
- 主队列 240 槽位：容纳大部分请求
- 保护队列 16 槽位：防止地址转换阻塞

### 6.2 并发请求数量

**最大并发 DMA 读请求**:

```
8192 字节 ÷ 512 字节/块 = 16 个块
```

如果有多个 WQE 同时处理：
```
4 个 WQE × 16 个块 = 64 个并发请求
```

**队列容量检查**:
```
240 (主队列) + 16 (保护队列) = 256 槽位
可以容纳 256 ÷ 16 = 16 个 WQE 的请求
```

### 6.3 吞吐量分析

**理论吞吐量**:
```
数据总线宽度: 256 bits = 32 bytes
时钟频率: 250 MHz
理论带宽: 32 × 250 = 8 GB/s = 64 Gbps
```

**实际吞吐量受限于**:
1. PCIe 带宽（Gen3 x8 ≈ 6.4 GB/s，Gen4 x8 ≈ 12.8 GB/s）
2. 主机内存带宽
3. 地址转换延迟
4. DMA 引擎效率

## 7. PayloadGen 与 PacketGen 的数据流对接

### 7.1 连接关系

```bsv
// 在 PacketGen 中
FIFOF#(PayloadGenReq) genReqPipeOutQ <- mkFIFOF;
FIFOF#(DataStream) genRespPipeInQ <- mkFIFOF;

// 连接到 PayloadGenAndCon
mkConnection(packetGen.genReqPipeOut, payloadGenAndCon.genReqPipeIn);
mkConnection(payloadGenAndCon.payloadGenStreamPipeOut, packetGen.genRespPipeIn);
```

### 7.2 数据流转换

```
PacketGen 发送请求:
  PayloadGenReq{addr: 0x100002, len: 8192, ...}
    ↓
PayloadGen 处理:
  1. 分成 16 个 512 字节的块
  2. 虚拟地址 → 物理地址
  3. 16 个 DMA 读取
  4. 合并成单个 8192 字节的数据流
    ↓
PacketGen 接收数据流:
  genRespPipeInQ: 256 beats × 32 bytes = 8192 bytes
    ↓
StreamShifter 对齐:
  本地对齐 → 远程对齐 (偏移 +1)
    ↓
DtldStreamSplitor 拆分:
  8192 字节 → 2 个 4096 字节的子流
    ↓
输出 2 个 RDMA 包
```

### 7.3 时序关系

```
T0:   PacketGen Stage 2 发送 PayloadGenReq
        ↓
T1:   PayloadGen 接收请求
T2:   PayloadGen 分块（16 个块）
T3:   PayloadGen 地址转换（块 1）
T4:   PayloadGen 发起 DMA 读取（块 1）
T5:   PayloadGen 地址转换（块 2）
T6:   PayloadGen 发起 DMA 读取（块 2）
...
T18:  PayloadGen 地址转换（块 16）
T19:  PayloadGen 发起 DMA 读取（块 16）
        ↓
T100: DMA 数据开始到达（块 1）
T110: DMA 数据继续到达（块 2）
...
T250: DMA 数据全部到达（块 16）
        ↓
T251: DtldStreamConcator 合并完成
        ↓
T252: 数据流输出到 PacketGen
        ↓
T253: StreamShifter 开始对齐
...
```

## 8. PayloadCon (Payload Consumer)

PayloadGen 有一个对应的模块 **PayloadCon**，用于 RDMA 接收路径的数据写入。

### 8.1 PayloadCon 的作用

- 接收 RDMA 接收的数据流
- 根据 PCIe 最大突发大小分块
- 虚拟地址 → 物理地址转换
- 发起 DMA 写入

### 8.2 与 PayloadGen 的对比

| 特性 | PayloadGen | PayloadCon |
|------|------------|------------|
| **方向** | 读取（主机内存 → FPGA） | 写入（FPGA → 主机内存） |
| **操作** | DMA Read | DMA Write |
| **数据流处理** | Concator (合并) | Splitor (拆分) |
| **输入** | 单个请求 | 数据流 + 请求 |
| **输出** | 单个数据流 | 写入完成标志 |

## 9. 总结

### 9.1 PayloadGen 的核心作用

1. **DMA 层分包**: 根据 PCIe 最大突发大小（512 字节）分块
2. **地址转换**: 虚拟地址 → 物理地址（页表查询）
3. **并发读取**: 发起多个 DMA 读请求（最多 64 个）
4. **数据合并**: 将多个 DMA 响应合并成连续数据流

### 9.2 关键设计特点

1. **两层分包架构**:
   - PayloadGen: DMA 层（512 字节）
   - PacketGen: 网络层（PMTU，如 4096 字节）

2. **流水线处理**:
   - handleInReq: 地址分块
   - getBurstChunRespAndIssueAddrTranslateReq: 地址转换
   - issueDmaRead: DMA 读取

3. **大容量缓冲**:
   - 256 个槽位容纳 PCIe 延迟
   - 支持多个 WQE 并发处理

4. **数据流合并**:
   - DtldStreamConcator 无缝合并多个子流
   - 保证数据的连续性

### 9.3 性能优化

1. **最大化并发**: 允许最多 64 个未完成的 DMA 请求
2. **流水线深度**: 足够容纳 PCIe 延迟
3. **小块 DMA**: 512 字节平衡延迟和吞吐量
4. **地址转换缓存**: 减少页表查询次数（未在代码中明确展示）

**相关文档**:
- [PacketGen实现与分包详解.md](./PacketGen实现与分包详解.md) - PacketGen 的分包机制
- [PacketGen模块详解.md](./PacketGen模块详解.md) - PacketGen 模块架构