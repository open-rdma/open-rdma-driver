# PayloadGenAndCon.bsv 文件详细分析

## 文件概述

`PayloadGenAndCon.bsv` 是 RDMA 硬件实现中负责**有效负载数据读写**的核心模块。它通过 DMA（Direct Memory Access）直接访问主存储器，实现 RDMA 的零拷贝数据传输。

## 核心概念

### Payload 在 RDMA 中的含义

**Payload（有效负载）** 是指实际要传输的用户数据：

- **发送端**：从本地内存读取的数据，要发送给远程节点
- **接收端**：从网络接收的数据，要写入本地内存

### Gen vs Con 的含义

- **Gen (Generator - 生成器)**：读取内存数据，生成要发送的 payload 数据流
- **Con (Consumer - 消费器)**：消费接收到的 payload 数据流，写入内存

```
发送路径：内存 ──[PayloadGen]──> Payload数据流 ──> 网络
接收路径：网络 ──> Payload数据流 ──[PayloadCon]──> 内存
```

---

## 主要模块

### 1. PayloadGen（Payload 生成器）

#### 功能概述
从主存储器中读取用户数据，转换为可以发送的数据流。

#### 核心流程

```
PayloadGenReq → 地址分块 → 虚拟地址转换 → DMA读请求 → 数据流拼接 → 输出
```

#### 详细处理步骤

##### 步骤1：接收生成请求并分块

```bsv
// 位置：PayloadGenAndCon.bsv:146-166
rule handleInReq;
    let req = genReqPipeInQ.first;  // PayloadGenReq
    genReqPipeInQ.deq;

    // 将请求按照 PCIe 最大突发大小分块
    let chunkReq = AddressChunkReq{
        startAddr: req.addr,        // 虚拟地址
        len: req.len,               // 总长度
        chunk: TLog#(PCIE_MAX_BYTE_IN_BURST)  // 分块大小对数（如 128 字节）
    };

    rawReqToBurstChunkerRequestPipeInAdapter.enq(chunkReq);
endrule
```

**为什么要分块？**
- PCIe 有最大突发传输大小限制（通常 128-256 字节）
- 虚拟地址可能跨越多个物理页
- 分块可以提高并行度和吞吐量

**PayloadGenReq 结构**：
```bsv
typedef struct {
    ADDR   addr;        // 虚拟地址（要读取的起始地址）
    Length len;         // 长度（总共要读取的字节数）
    PTEIndex pgtOffset; // 页表偏移（用于地址转换）
    ADDR baseVA;        // 基虚拟地址（内存区域的起始地址）
} PayloadGenReq;
```

##### 步骤2：虚拟地址转换

```bsv
// 位置：PayloadGenAndCon.bsv:169-192
rule getBurstChunRespAndIssueAddrTranslateReq;
    // 获取分块后的地址边界信息
    let burstAddrBoundry = rawReqToBurstChunker.responsePipeOut.first;

    // 发起地址转换请求（虚拟地址 → 物理地址）
    let addrTranslateReq = PgtAddrTranslateReq {
        pgtOffset: pgtOffset,                    // 页表偏移
        baseVA: baseVA,                          // 基虚拟地址
        addrToTrans: burstAddrBoundry.startAddr  // 要转换的虚拟地址
    };
    addrTranslateCltInst.putReq(addrTranslateReq);
endrule
```

**虚拟地址转换流程**：
```
虚拟地址 ─┐
         │
页表偏移 ├──> [页表查询] ──> 物理地址
         │
基虚拟地址┘
```

**为什么需要地址转换？**
- 用户程序使用虚拟地址
- DMA 引擎需要物理地址
- RDMA 硬件必须自己完成地址转换（内核旁路）

##### 步骤3：发起 DMA 读请求

```bsv
// 位置：PayloadGenAndCon.bsv:194-217
rule issueDmaRead;
    // 获取转换后的物理地址
    let translatedAddr <- addrTranslateCltInst.getResp;
    let {len, isLast, fpDebugTime} = issueDmaReadPipelineQ.first;

    // 构造 DMA 读请求元数据
    let readReq = DtldStreamMemAccessMeta {
        addr: translatedAddr,           // 物理地址
        totalLen: len,                  // 读取长度
        accessType: MemAccessTypeNormalReadWrite,
        operand_1: 0,
        operand_2: 0,
        noSnoop: False                  // 是否绕过 CPU cache
    };
    dmaReadReqPipeOutGuardQ.enq(readReq);

    // 标记是否是最后一个突发
    dsConcatorIsLastStreamFlagPipeInConverterGuardQueue.enq(isLast);
endrule
```

**DMA 读请求的关键参数**：
- `addr`：物理地址
- `totalLen`：本次突发读取的字节数
- `accessType`：访问类型（普通读写、原子操作等）
- `noSnoop`：是否需要 cache 一致性维护

##### 步骤4：数据流拼接

```bsv
// 数据流拼接器（在模块初始化中）
DtldStreamConcator#(DATA, LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE) dsConcator
    <- mkDtldStreamConcator;
```

**作用**：
- DMA 读操作返回多个数据流片段（每个突发一个）
- 拼接器将这些片段合并成连续的数据流
- 提供给上层模块（PacketGen）使用

**数据流拼接示例**：
```
输入：
  [数据流1: 128字节, isLast=False]
  [数据流2: 128字节, isLast=False]
  [数据流3:  64字节, isLast=True]

输出：
  [连续数据流: 320字节, isLast=True]
```

#### 接口定义

```bsv
interface PayloadGen;
    // 地址转换客户端接口
    interface ClientP#(PgtAddrTranslateReq, ADDR) addrTranslateClt;

    // 输入：Payload 生成请求
    interface PipeInB0#(PayloadGenReq) genReqPipeIn;

    // 输出：生成的 Payload 数据流
    interface PipeOut#(IoChannelMemoryAccessDataStream) payloadGenStreamPipeOut;

    // DMA 读主控接口
    interface IoChannelMemoryReadMasterPipeB0In dmaReadMasterPipe;
        interface PipeOut#(IoChannelMemoryAccessMeta) readMetaPipeOut;  // 读请求元数据
        interface PipeIn#(IoChannelMemoryAccessDataStream) readDataPipeIn;  // 读数据输入
    endinterface;
endinterface
```

---

### 2. PayloadCon（Payload 消费器）

#### 功能概述
接收网络传来的 payload 数据流，写入主存储器。

#### 核心流程

```
PayloadConReq → 地址分块 → 虚拟地址转换 → 数据流分割 → DMA写请求 → 写完成通知
```

#### 详细处理步骤

##### 步骤1：接收消费请求并分块

```bsv
// 位置：PayloadGenAndCon.bsv:266-286
rule handleInReq;
    let req = conReqPipeInQ.first;  // PayloadConReq

    // 按 PCIe 突发大小分块
    let chunkReq = AddressChunkReq{
        startAddr: req.addr,
        len: req.len,
        chunk: TLog#(PCIE_MAX_BYTE_IN_BURST)
    };

    rawReqToBurstChunkerRequestPipeInAdapter.enq(chunkReq);
endrule
```

**PayloadConReq 结构**：
```bsv
typedef struct {
    ADDR            addr;           // 虚拟地址（写入目标地址）
    Length          len;            // 长度
    PTEIndex        pgtOffset;      // 页表偏移
    ADDR            baseVA;         // 基虚拟地址
    SimulationTime  fpDebugTime;    // 调试时间戳
} PayloadConReq;
```

##### 步骤2：虚拟地址转换

```bsv
// 位置：PayloadGenAndCon.bsv:288-314
rule getBurstChunRespAndIssueAddrTranslateReq;
    let burstAddrBoundry = rawReqToBurstChunker.responsePipeOut.first;

    // 地址转换请求（与 PayloadGen 相同）
    let addrTranslateReq = PgtAddrTranslateReq {
        pgtOffset: pgtOffset,
        baseVA: baseVA,
        addrToTrans: burstAddrBoundry.startAddr
    };
    addrTranslateCltInst.putReq(addrTranslateReq);
endrule
```

##### 步骤3：发起 DMA 写请求并计算分割元数据

```bsv
// 位置：PayloadGenAndCon.bsv:316-344
rule getBeatChunkMetaCalculateRespAndIssueAxiWrite;
    let translatedAddr <- addrTranslateCltInst.getResp;

    // 计算对齐信息（用于数据流分割）
    ADDR truncatedStartAddr = translatedAddr;
    ADDR truncatedEndAddrForALignCalc = translatedAddr + zeroExtend(len - 1);

    streamSplitorMetaCalcPipelineQ.enq(
        tuple3(truncate(truncatedStartAddr),
               truncate(truncatedEndAddrForALignCalc),
               curFpDebugTime));

    // 构造 DMA 写请求
    let writeReq = DtldStreamMemAccessMeta {
        addr: translatedAddr,
        totalLen: len,
        accessType: MemAccessTypeNormalReadWrite,
        operand_1: 0,
        operand_2: 0,
        noSnoop: False
    };
    dmaWriteReqAddrPipeOutQ.enq(writeReq);
endrule
```

##### 步骤4：计算数据流分割参数

```bsv
// 位置：PayloadGenAndCon.bsv:346-365
rule calcStreamSpliterMeta;
    let {truncatedStartAddr, truncatedEndAddrForALignCalc, fpDebugTime} =
        streamSplitorMetaCalcPipelineQ.first;

    // 计算需要分割的对齐块数量
    AlignBlockCntInPayloadConAndGenBurst alignBlockCntForStreamSplit = truncate(
        (truncatedEndAddrForALignCalc >> LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE) -
        (truncatedStartAddr >> LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE)
    ) + 1;

    // 发送给数据流分割器
    dsSpliterStreamAlignBlockCountPipeInConverter.enq(alignBlockCntForStreamSplit);
endrule
```

**为什么需要数据流分割？**
- 接收到的 payload 是一个长数据流
- 需要按照 DMA 突发边界分割
- 确保每个 DMA 写操作对应正确的数据片段

**数据流分割示例**：
```
输入（长数据流）：
  [320字节连续数据]

输出（分割后）：
  DMA写1: [128字节]
  DMA写2: [128字节]
  DMA写3: [ 64字节]
```

##### 步骤5：转发 Payload 数据并分割

```bsv
// 位置：PayloadGenAndCon.bsv:367-384
rule forwardConsumedFinishedSignal;
    let ds = payloadConStreamPipeInQ.first;
    payloadConStreamPipeInQ.deq;

    // 发送给数据流分割器
    dsSpliterDataPipeInConverter.enq(ds);

    // 如果是最后一个数据，发送完成信号
    if (ds.isLast) {
        conRespPipeOutQ.enq(True);
    }
endrule

// 位置：PayloadGenAndCon.bsv:386-395
rule debugForwardSplitOutput;
    let ds = dsSpliter.dataPipeOut.first;
    dsSpliter.dataPipeOut.deq;

    // 转发分割后的数据到 DMA 写数据队列
    dmaWriteReqDataPipeOutQ.enq(ds);
endrule
```

**数据流和元数据的同步**：
```
时间轴：
t1: DMA写元数据1 → DMA写数据流1
t2: DMA写元数据2 → DMA写数据流2
t3: DMA写元数据3 → DMA写数据流3
```

两个队列必须严格同步，确保每个写请求都有对应的数据。

#### 接口定义

```bsv
interface PayloadCon;
    // 地址转换客户端接口
    interface ClientP#(PgtAddrTranslateReq, ADDR) addrTranslateClt;

    // 输入：Payload 消费请求
    interface PipeInB0#(PayloadConReq) conReqPipeIn;

    // 输出：消费完成信号
    interface PipeOut#(Bool) conRespPipeOut;

    // 输入：要消费的 Payload 数据流
    interface PipeInB0#(IoChannelMemoryAccessDataStream) payloadConStreamPipeIn;

    // DMA 写主控接口
    interface IoChannelMemoryWriteMasterPipe dmaWriteMasterPipe;
        interface PipeOut#(IoChannelMemoryAccessMeta) writeMetaPipeOut;  // 写请求元数据
        interface PipeOut#(IoChannelMemoryAccessDataStream) writeDataPipeOut;  // 写数据输出
    endinterface;
endinterface
```

---

### 3. PayloadGenAndCon（组合模块）

#### 功能
将 PayloadGen 和 PayloadCon 封装在一起，提供统一的内存访问接口。

```bsv
module mkPayloadGenAndCon#(Word channelIdx)(PayloadGenAndCon);
    PayloadGen payloadGen <- mkPayloadGen;
    PayloadCon payloadCon <- mkPayloadCon(channelIdx);

    // 组合 DMA 读写接口
    interface IoChannelMemoryMasterPipeB0In ioChannelMemoryMasterPipeIfc;
        interface writePipeIfc = payloadCon.dmaWriteMasterPipe;
        interface readPipeIfc  = payloadGen.dmaReadMasterPipe;
    endinterface;
endmodule
```

**为什么需要 channelIdx？**
- 支持多个 QP（Queue Pair）并发操作
- 每个通道独立处理自己的 payload 读写
- 便于调试和性能监控

---

## 关键数据结构

### 1. AddressChunkReq（地址分块请求）

```bsv
typedef struct {
    ADDR   startAddr;   // 起始地址
    Length len;         // 总长度
    Bit#(n) chunk;      // 分块大小（对数形式）
} AddressChunkReq;
```

### 2. AddressChunkResp（地址分块响应）

```bsv
typedef struct {
    ADDR   startAddr;   // 本块的起始地址
    Length len;         // 本块的长度
    Bool   isFirst;     // 是否是第一块
    Bool   isLast;      // 是否是最后一块
} AddressChunkResp;
```

### 3. PgtAddrTranslateReq（页表地址转换请求）

```bsv
typedef struct {
    PTEIndex pgtOffset;      // 页表项索引偏移
    ADDR     baseVA;         // 基虚拟地址
    ADDR     addrToTrans;    // 要转换的虚拟地址
} PgtAddrTranslateReq;
```

### 4. DtldStreamMemAccessMeta（内存访问元数据）

```bsv
typedef struct {
    ADDR     addr;           // 物理地址
    Length   totalLen;       // 访问长度
    MemAccessType accessType;  // 访问类型
    Bit#(64) operand_1;      // 操作数1（用于原子操作）
    Bit#(64) operand_2;      // 操作数2（用于原子操作）
    Bool     noSnoop;        // 是否绕过 cache
} DtldStreamMemAccessMeta;
```

---

## 数据流图

### PayloadGen 数据流

```
┌──────────────────┐
│ PayloadGenReq    │
│ (虚拟地址+长度)   │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 地址分块器        │
│ 按 PCIe 突发大小  │
└────────┬─────────┘
         │
         ├──► 块1: addr=0x1000, len=128, isFirst=True, isLast=False
         ├──► 块2: addr=0x1080, len=128, isFirst=False, isLast=False
         └──► 块3: addr=0x1100, len=64,  isFirst=False, isLast=True
                │
                ▼
         ┌──────────────────┐
         │ 虚拟地址 → 物理地址│
         │ (页表查询)        │
         └────────┬─────────┘
                  │
                  ├──► PA1 = 0x80001000
                  ├──► PA2 = 0x80001080
                  └──► PA3 = 0x80001100
                         │
                         ▼
                  ┌──────────────────┐
                  │ DMA 读请求队列    │
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ PCIe → 主存储器   │
                  │ 读取数据          │
                  └────────┬─────────┘
                           │
                           ├──► [数据流1: 128B]
                           ├──► [数据流2: 128B]
                           └──► [数据流3:  64B]
                                    │
                                    ▼
                           ┌──────────────────┐
                           │ 数据流拼接器      │
                           └────────┬─────────┘
                                    │
                                    ▼
                           ┌──────────────────┐
                           │ 连续 Payload 数据 │
                           │ (320字节)        │
                           └──────────────────┘
```

### PayloadCon 数据流

```
┌──────────────────┐        ┌──────────────────┐
│ PayloadConReq    │        │ Payload 数据流    │
│ (虚拟地址+长度)   │        │ (320字节连续)     │
└────────┬─────────┘        └─────────┬────────┘
         │                            │
         ▼                            ▼
┌──────────────────┐        ┌──────────────────┐
│ 地址分块器        │        │ 数据流分割器      │
└────────┬─────────┘        │ (根据地址边界)    │
         │                  └─────────┬────────┘
         ├──► 块1: 128B              │
         ├──► 块2: 128B              ├──► [数据片段1: 128B]
         └──► 块3:  64B              ├──► [数据片段2: 128B]
                │                    └──► [数据片段3:  64B]
                ▼                            │
         ┌──────────────────┐                │
         │ 虚拟地址 → 物理地址│                │
         └────────┬─────────┘                │
                  │                          │
                  ├──► PA1                   │
                  ├──► PA2                   │
                  └──► PA3                   │
                         │                   │
                         │                   │
                  ┌──────┴─────────┬─────────┘
                  │                │
                  ▼                ▼
         ┌────────────────────────────┐
         │ DMA 写请求（元数据+数据）   │
         └─────────────┬──────────────┘
                       │
                       ├──► 写1: PA1, 128B, [数据1]
                       ├──► 写2: PA2, 128B, [数据2]
                       └──► 写3: PA3,  64B, [数据3]
                              │
                              ▼
                       ┌──────────────┐
                       │ PCIe → 主存储 │
                       │ 写入数据      │
                       └──────┬───────┘
                              │
                              ▼
                       ┌──────────────┐
                       │ 完成信号      │
                       │ (Bool: True)  │
                       └──────────────┘
```

---

## 性能关键设计

### 1. 流水线深度

```bsv
// PayloadGen 流水线
FIFOF#(Tuple3#(...)) getBurstChunRespPipelineQ <- mkSizedFIFOF(2);
FIFOF#(Tuple3#(...)) issueDmaReadPipelineQ <- mkSizedFIFOF(16);

// PayloadCon 流水线
FIFOF#(Tuple3#(...)) getBurstChunRespPipelineQ <- mkSizedFIFOF(2);
FIFOF#(Tuple2#(...)) issueDmaWritePipelineQ <- mkSizedFIFOF(5);
```

**设计考虑**：
- `issueDmaReadPipelineQ` 深度为 16：对应 PCIe 的最大未完成读请求数（受 PCIe tag 限制）
- `issueDmaWritePipelineQ` 深度为 5：写操作通常有更好的流水线特性，不需要太深

### 2. DMA 请求队列大小

```bsv
// PayloadGen
FIFOF#(IoChannelMemoryAccessMeta) dmaReadReqPipeOutQ <- mkSizedFIFOF(256 - 16);
FIFOF#(IoChannelMemoryAccessMeta) dmaReadReqPipeOutGuardQ <- mkSizedFIFOF(16);

// PayloadCon
FIFOF#(IoChannelMemoryAccessMeta) dmaWriteReqAddrPipeOutQ <-
    mkSizedFIFOFWithFullAssert(PAYLOAD_STORAGE_CAPACITY_FOR_RQ_OUTPUT_DMA_DATA_STREAM_BUF);
FIFOF#(IoChannelMemoryAccessDataStream) dmaWriteReqDataPipeOutQ <-
    mkSizedFIFOFWithFullAssert(PAYLOAD_STORAGE_CAPACITY_FOR_RQ_OUTPUT_DMA_DATA_STREAM_BUF);
```

**为什么 PayloadGen 读请求队列这么大（256）？**
- DMA 端口由多个模块共享（如 WQE 描述符获取、Payload 读取）
- 避免因共享而产生的队头阻塞（Head-of-Line Blocking）
- Guard Queue（16）用于防止地址转换被阻塞

### 3. 完全流水线检查

```bsv
checkFullyPipeline(req.fpDebugTime, 1, 2000,
    DebugConf{name: "mkPayloadCon handleInReq", enableDebug: True});
```

**参数含义**：
- `req.fpDebugTime`：请求进入时间
- `1`：期望的流水线级数
- `2000`：超时阈值（周期数）

**作用**：确保没有流水线停顿，维持高吞吐量。

### 4. 数据流对齐

#### PayloadGen 中的对齐处理

在 `PacketGenAndParse.bsv` 中计算对齐偏移：
```bsv
ByteIdxInDword localToRemoteAlignShiftOffset =
    zeroExtend(localAddrOffset) - zeroExtend(remoteAddrOffset);
payloadStreamShifter.offsetPipeIn.enq(localToRemoteAlignShiftOffset);
```

**为什么需要对齐？**
- RDMA 要求 payload 按照**远程地址**对齐
- 但本地内存是按照**本地地址**对齐
- 需要在数据流中插入/删除字节以实现对齐

**对齐示例**：
```
本地地址：0x1002 (偏移 2 字节)
远程地址：0x2004 (偏移 4 字节)
需要右移：4 - 2 = 2 字节

原始数据：[--AB CDEF GHIJ] (-- 表示填充)
对齐后： [----ABCD EFGH IJ--]
```

---

## 与 PacketGenAndParse 的协作

### 发送路径集成

```
用户发送请求
    │
    ▼
┌────────────────────┐
│ PacketGen          │
│ - 生成 RDMA 头部    │
│ - 计算分包策略      │
└─────┬──────────────┘
      │ PayloadGenReq
      ▼
┌────────────────────┐
│ PayloadGen         │
│ - 从内存读取数据    │
│ - 地址对齐         │
└─────┬──────────────┘
      │ Payload 数据流
      ▼
┌────────────────────┐
│ PacketGen          │
│ - 按包大小分割      │
│ - 组装完整数据包    │
└─────┬──────────────┘
      │
      ▼
   网络发送
```

### 接收路径集成

```
网络接收
    │
    ▼
┌────────────────────┐
│ PacketParse        │
│ - 解析 RDMA 头部    │
│ - 提取 Payload     │
└─────┬──────────────┘
      │ Payload 数据流
      ▼
┌────────────────────┐
│ PayloadCon         │
│ - 写入内存         │
│ - 地址转换         │
└─────┬──────────────┘
      │
      ▼
  用户接收完成
```

---

## 地址转换详解

### 虚拟地址到物理地址的转换

#### 地址组成

```
虚拟地址 (VA) = 基虚拟地址 (baseVA) + 偏移量

物理地址 (PA) = 页表查询(baseVA, 偏移量, 页表偏移)
```

#### 页表结构（简化）

```
内存区域 (Memory Region)
    ├─ baseVA:     0x7fff0000  (基虚拟地址)
    ├─ pgtOffset:  0x100       (页表起始项索引)
    └─ 页表项 (Page Table Entries):
         [0x100] → 物理页: 0x80001000
         [0x101] → 物理页: 0x80002000
         [0x102] → 物理页: 0x80003000
         ...
```

#### 转换过程

```
假设：页大小 = 4KB (0x1000)

虚拟地址：0x7fff1234
baseVA：  0x7fff0000
页表偏移：0x100

计算：
  页内偏移 = 0x7fff1234 & 0xFFF = 0x234
  页号     = (0x7fff1234 - 0x7fff0000) >> 12 = 1
  页表索引 = 0x100 + 1 = 0x101
  物理页   = PageTable[0x101] = 0x80002000
  物理地址 = 0x80002000 + 0x234 = 0x80002234
```

### 为什么硬件需要自己做地址转换？

在传统 I/O 中：
```
应用 → 系统调用 → 内核 → 驱动 → [内核做地址转换] → 硬件 → 内存
```

在 RDMA 中（内核旁路）：
```
应用 → [硬件做地址转换] → 硬件 → 内存
```

**优势**：
- 无系统调用开销
- 无内核态/用户态切换
- 极低延迟（微秒级）

---

## 总结

### PayloadGenAndCon.bsv 的核心作用

1. **PayloadGen（生成器）**：
   - 从用户内存读取要发送的数据
   - 处理虚拟地址到物理地址的转换
   - 通过 DMA 高效读取数据
   - 生成连续的 payload 数据流

2. **PayloadCon（消费器）**：
   - 接收网络传来的 payload 数据
   - 处理虚拟地址到物理地址的转换
   - 通过 DMA 高效写入数据
   - 提供写完成通知

### Payload 的本质

```
Payload = 用户真正关心的数据

不是：
  - 协议头部（BTH, RETH, IP, UDP, Ethernet 等）
  - 控制信息（ACK, NAK 等）
  - 校验和（CRC, Checksum）

是：
  - 应用程序的实际数据
  - 从源内存复制到目标内存的内容
```

### 性能关键点

1. **零拷贝**：数据直接从源内存 → 网卡 → 目标内存，无 CPU 参与
2. **流水线**：地址转换、DMA 请求、数据传输并行进行
3. **批处理**：按 PCIe 突发大小批量处理，提高效率
4. **硬件地址转换**：避免内核介入，实现内核旁路

### 与整体架构的关系

```
应用层：    [用户数据]
           ↓        ↑
Payload层： PayloadGen  PayloadCon  ← 本文件
           ↓        ↑
Packet层：  PacketGen   PacketParse ← PacketGenAndParse.bsv
           ↓        ↑
网络层：    [以太网帧]
```

**职责分离**：
- **Payload 层**：关注数据本身的存储和传输
- **Packet 层**：关注数据包的格式和协议

这种分层设计使得每个模块职责清晰，易于维护和优化。
