# PacketGen 实现与分包机制详解

## 1. 概述

本文档详细介绍 **mkPacketGen** 模块的实现细节，特别聚焦于 RDMA 数据包的**分包机制**。PacketGen 是 RDMA 发送路径的核心模块，负责将工作队列元素（WorkQueueElem）转换为可发送的 RDMA 数据包。

**文件位置**: `/home/peng/projects/rdma_all/open-rdma-rtl/src/PacketGenAndParse.bsv:447`

**核心功能**:
1. 根据 **PMTU (Path MTU)** 将大消息拆分成多个小包
2. 为每个包生成正确的 RDMA 协议头
3. 处理地址对齐和数据流转换
4. 管理包序列号（PSN）

## 2. 模块结构和组件

### 2.1 模块实例化代码

```bsv
module mkPacketGen(PacketGen);
    // ========== 输入/输出队列 ==========
    PipeInAdapterB0#(WorkQueueElem)   wqePipeInQ      <- mkPipeInAdapterB0;
    FIFOF#(PayloadGenReq)           genReqPipeOutQ  <- mkFIFOF;
    FIFOF#(DataStream)              genRespPipeInQ  <- mkFIFOF;

    // ========== 核心分包组件 ==========
    // 1. 地址分块器：根据 PMTU 拆分地址范围
    AddressChunker#(ADDR, Length, ChunkAlignLogValue) wqeToPacketChunker <- mkAddressChunker;

    // 2. 数据流对齐器：本地地址 → 远程地址对齐
    StreamShifterG#(DATA) payloadStreamShifter <- mkBiDirectionStreamShifterLsbRightG;
    mkConnection(toPipeOut(genRespPipeInQ), payloadStreamShifter.streamPipeIn);

    // 3. 数据流拆分器：按 PMTU 边界拆分数据流
    DtldStreamSplitor#(DATA, AlignBlockCntInPmtu, LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE)
        payloadSplitor <- mkDtldStreamSplitor(DebugConf{name: "mkPacketGen payloadSplitor", enableDebug: False});
    mkConnection(payloadStreamShifter.streamPipeOut, payloadSplitor.dataPipeIn);

    // ========== 输出队列 ==========
    // 重要：深度必须足够大以容纳 PCIe 读延迟
    FIFOF#(DataStream) perPacketPayloadDataStreamQ <-
        mkSizedFIFOFWithFullAssert(8, DebugConf{name: "mkPacketGen perPacketPayloadDataStreamQ", enableDebug: False});

    // ========== PSN 寄存器 ==========
    Reg#(PSN) psnReg <- mkRegU;  // 用于递增包序列号

    // ========== MR Table 查询客户端 ==========
    QueuedClientP#(MrTableQueryReq, Maybe#(MemRegionTableEntry)) mrTableQueryCltInst <-
        mkQueuedClientPWithDebug(DebugConf{name: "mkPacketGen mrTableQueryCltInst", enableDebug: False});

    // ========== 流水线队列 ==========
    FIFOF#(SendChunkByRemoteAddrReqAndPayloadGenReqPipelineEntry)
        sendChunkByRemoteAddrReqAndPayloadGenReqPipelineQ <- mkSizedFIFOF(5);
    FIFOF#(GenPacketHeaderStep1PipelineEntry)
        genPacketHeaderStep1PipelineQ <- mkSizedFIFOF(4);
    FIFOF#(GenPacketHeaderStep2PipelineEntry)
        genPacketHeaderStep2PipelineQ <- mkLFIFOF;

    // ========== 输出队列（大容量以容纳 PCIe 延迟）==========
    FIFOF#(ThinMacIpUdpMetaDataForSend) macIpUdpMetaPipeOutQueue <- mkSizedFIFOF(256);
    FIFOF#(RdmaSendPacketMeta) rdmaPacketMetaPipeOutQueue <- mkSizedFIFOF(256);

    // ... 流水线规则实现 ...
endmodule
```

### 2.2 组件连接关系

```
                           DMA 读取
                              ↓
                      genRespPipeInQ
                              ↓
                    ┌──────────────────┐
                    │ StreamShifter    │ ← 接收对齐偏移量
                    │ (地址对齐转换)   │
                    └────────┬─────────┘
                             │ 已对齐的数据流
                             ↓
                    ┌──────────────────┐
                    │ DtldStreamSplitor│ ← 接收 AlignBlock 数量
                    │ (数据流拆分)     │
                    └────────┬─────────┘
                             │ 拆分后的子流
                             ↓
              perPacketPayloadDataStreamQ
                             ↓
                        输出数据流
```

## 3. 分包机制的三个核心步骤

PacketGen 的分包机制涉及三个紧密协作的步骤：

### 步骤 1: 地址分块 (Address Chunking)
- **组件**: AddressChunker
- **输入**: 远程地址、总长度、PMTU
- **输出**: 多个地址块（每个块对应一个包）

### 步骤 2: 地址对齐 (Address Alignment)
- **组件**: StreamShifter
- **输入**: 本地-远程地址偏移量
- **输出**: 按远程地址对齐的数据流

### 步骤 3: 数据流拆分 (Stream Splitting)
- **组件**: DtldStreamSplitor
- **输入**: AlignBlock 数量（每个包）
- **输出**: 多个子数据流（每个流对应一个包）

## 4. 五阶段流水线详解

PacketGen 采用 5 级流水线架构，每个阶段处理分包的不同方面：

```
Stage 1          Stage 2              Stage 3              Stage 4              Stage 5
queryMrTable → sendChunkByAddr → genPacketHdrStep1 → genPacketHdrStep2 → forwardStream
    ↓                ↓                   ↓                    ↓                   ↓
查询 MR         地址分块             确定包边界            生成协议头           转发数据流
                地址对齐计算         PSN 管理             计算拆分参数
                发起 DMA 读取       UDP 长度计算
```

### Stage 1: queryMrTable (行 493-521)

**功能**: 判断是否需要载荷，并发起 MR Table 查询

```bsv
rule queryMrTable;
    let wqe = wqePipeInQ.first;
    wqePipeInQ.deq;

    // 判断是否需要有效载荷
    Bool isZeroPayload = isZeroR(wqe.len);
    Bool hasPayload = workReqNeedPayloadGen(wqe.opcode) && !isZeroPayload;

    if (hasPayload) begin
        // 发起 MR Table 查询
        let mrTableQueryReq = MrTableQueryReq{
            idx: lkey2IndexMR(wqe.lkey)  // 从 lkey 提取 MR 表索引
        };
        mrTableQueryCltInst.putReq(mrTableQueryReq);
    end

    // 传递到下一级流水线
    sendChunkByRemoteAddrReqAndPayloadGenReqPipelineQ.enq(
        SendChunkByRemoteAddrReqAndPayloadGenReqPipelineEntry{
            wqe: wqe,
            hasPayload: hasPayload
        }
    );
endrule
```

**需要载荷的操作类型**:
- `IBV_WR_SEND`
- `IBV_WR_RDMA_WRITE`
- `IBV_WR_RDMA_READ_RESP`

**不需要载荷的操作类型**:
- `IBV_WR_RDMA_READ` (请求包无载荷)
- `IBV_WR_ATOMIC_*` (只有 8 字节，包含在扩展头中)

### Stage 2: sendChunkByRemoteAddrReqAndPayloadGenReq (行 524-580)

**功能**: 分包机制的核心阶段，完成四个关键任务

#### 任务 1: 发起地址分块请求

```bsv
if (hasPayload) begin
    let remoteAddrChunkReq = AddressChunkReq{
        startAddr: wqe.raddr,      // 远程起始地址
        len: wqe.len,              // 总长度
        chunk: getPmtuSizeByPmtuEnum(wqe.pmtu)  // PMTU 大小（对数值）
    };
    wqeToPacketChunkerRequestPipeInAdapter.enq(remoteAddrChunkReq);
end
```

**PMTU 枚举转换**:
```bsv
typedef enum {
    IBV_MTU_256  = 1,   // 256  字节 → log2(256)  = 8
    IBV_MTU_512  = 2,   // 512  字节 → log2(512)  = 9
    IBV_MTU_1024 = 3,   // 1024 字节 → log2(1024) = 10
    IBV_MTU_2048 = 4,   // 2048 字节 → log2(2048) = 11
    IBV_MTU_4096 = 5    // 4096 字节 → log2(4096) = 12
} PMTU;

function ChunkAlignLogValue getPmtuSizeByPmtuEnum(PMTU pmtu);
    return case (pmtu)
        IBV_MTU_256:  8;
        IBV_MTU_512:  9;
        IBV_MTU_1024: 10;
        IBV_MTU_2048: 11;
        IBV_MTU_4096: 12;
        default: 12;  // 默认 4096
    endcase;
endfunction
```

**AddressChunker 的作用** (详见 `AddressChunker.bsv:37`):

AddressChunker 将连续的地址范围按照 PMTU 边界拆分成多个块：

```
输入示例:
  startAddr = 0x1000
  len = 8192 (8K)
  chunk = 12 (2^12 = 4096 字节)

输出:
  块 1: {startAddr: 0x1000, len: 4096, isFirst: True,  isLast: False}
  块 2: {startAddr: 0x2000, len: 4096, isFirst: False, isLast: True}
```

**分块计算逻辑** (在 `AddressChunker.bsv:68-87`):

```bsv
rule preCalc;
    let chunkReq = reqQ.first;
    reqQ.deq;

    tLen chunkSize = unpack(1 << chunkReq.chunk);  // 2^chunk

    // 计算零基的块数量
    tLen zeroBasedChunkCnt = unpack(
        ((truncate(pack(chunkReq.startAddr)) + zeroExtend(pack(chunkReq.len) - 1)) >> chunkReq.chunk) -
        (truncate(pack(chunkReq.startAddr)) >> chunkReq.chunk)
    );

    // 计算第一个块的非对齐字节数
    tLen chunkRemainingMask = chunkSize - 1;
    tLen nonValidByteCntInFirstChunk = unpack(truncate(pack(chunkReq.startAddr)) & pack(chunkRemainingMask));

    // ...
endrule
```

**示例计算**:
```
startAddr = 0x1000 (对齐到 4K)
len = 8192
chunkSize = 4096

第一个块的边界地址:
  startChunkIdx = 0x1000 >> 12 = 1
  endChunkIdx = (0x1000 + 8192 - 1) >> 12 = (0x2FFF) >> 12 = 2
  zeroBasedChunkCnt = 2 - 1 = 1 (共 2 个块)

第一个块的长度:
  nonValidByteCntInFirstChunk = 0x1000 & 0xFFF = 0 (已对齐)
  outputLen = 4096 - 0 = 4096

第二个块的长度:
  remainingLen = 8192 - 4096 = 4096
```

**非对齐地址示例**:
```
startAddr = 0x1100 (未对齐到 4K，偏移 0x100 = 256 字节)
len = 8192
chunkSize = 4096

第一个块:
  nonValidByteCntInFirstChunk = 0x1100 & 0xFFF = 0x100 = 256
  outputLen = 4096 - 256 = 3840 字节
  块 1: {startAddr: 0x1100, len: 3840, isFirst: True, isLast: False}

第二个块:
  块 2: {startAddr: 0x2000, len: 4096, isFirst: False, isLast: False}

第三个块:
  块 3: {startAddr: 0x3000, len: 256, isFirst: False, isLast: True}
```

#### 任务 2: 获取 MR Table 响应并生成 Payload 读取请求

```bsv
// 获取 MR Table 查询响应
let mrTableMaybe <- mrTableQueryCltInst.getResp;
let mrTable = fromMaybe(?, mrTableMaybe);

// 生成 Payload 读取请求
let payloadGenReq = PayloadGenReq{
    addr:      wqe.laddr,          // 本地虚拟地址
    len:       wqe.len,            // 数据长度
    baseVA:    mrTable.baseVA,     // 基虚拟地址（从 MR Table）
    pgtOffset: mrTable.pgtOffset   // 页表偏移（从 MR Table）
};
genReqPipeOutQ.enq(payloadGenReq);
```

**MR Table 查询响应**:
```bsv
typedef struct {
    ADDR baseVA;                              // 内存区域的基虚拟地址
    PTEIndex pgtOffset;                       // 页表偏移（用于地址转换）
    Length len;                               // 内存区域长度
    FlagsType#(MemAccessTypeFlag) accFlags;   // 访问权限标志
} MemRegionTableEntry deriving(Bits, FShow);
```

**Payload 生成流程**:
```
PayloadGenReq
    ↓
PayloadGenAndCon 模块
    ↓
AddressChunker (按 PCIe 最大读取大小分块)
    ↓
AddressTranslator (虚拟地址 → 物理地址)
    ↓
DMA 读取引擎
    ↓
DataStream (数据流)
    ↓
genRespPipeInQ (回到 PacketGen)
```

#### 任务 3: 计算地址对齐偏移量

```bsv
ByteIdxInDword localAddrOffset = truncate(wqe.laddr);   // 本地地址的低 2 位
ByteIdxInDword remoteAddrOffset = truncate(wqe.raddr);  // 远程地址的低 2 位

// 计算偏移量（有符号）
DataBusSignedByteShiftOffset localToRemoteAlignShiftOffset =
    zeroExtend(localAddrOffset) - zeroExtend(remoteAddrOffset);

// 发送给 StreamShifter
payloadStreamShifterOffsetPipeInConverter.enq(localToRemoteAlignShiftOffset);
```

**地址对齐示例**:

RDMA 协议要求：**有效载荷必须按照远程地址对齐**

```
示例 1: 需要右移
  本地地址: 0x1002  (偏移 2)
  远程地址: 0x2001  (偏移 1)
  偏移量: 2 - 1 = +1 (右移 1 字节)

  本地数据流: [D3 D2 D1 D0] [D7 D6 D5 D4] [-- -- -- D8]
              ↓ StreamShifter (右移 1)
  远程对齐流: [D2 D1 D0 --] [D6 D5 D4 D3] [-- -- D8 D7]

示例 2: 需要左移
  本地地址: 0x1001  (偏移 1)
  远程地址: 0x2003  (偏移 3)
  偏移量: 1 - 3 = -2 (左移 2 字节)

  本地数据流: [D3 D2 D1 D0] [D7 D6 D5 D4] [DB DA D9 D8]
              ↓ StreamShifter (左移 2)
  远程对齐流: [D1 D0 -- --] [D5 D4 D3 D2] [D9 D8 D7 D6]

示例 3: 无需移位
  本地地址: 0x1000  (偏移 0)
  远程地址: 0x2000  (偏移 0)
  偏移量: 0 - 0 = 0 (无需移位)
```

**StreamShifter 实现原理**:

StreamShifter 使用移位寄存器实现数据流对齐：

```bsv
// 简化的 StreamShifter 逻辑
Reg#(DATA) shiftReg <- mkRegU;  // 移位寄存器

rule shiftStream;
    let ds = streamPipeIn.first;
    streamPipeIn.deq;

    let offset = offsetPipeIn.first;  // 偏移量（只在第一个 beat 时使用）

    if (ds.isFirst) begin
        offsetPipeIn.deq;
    end

    // 右移操作
    DATA shiftedData = (shiftReg >> (offset * 8)) | (ds.data << ((4 - offset) * 8));
    shiftReg <= ds.data;

    streamPipeOut.enq(DataStream{
        data: shiftedData,
        byteEn: ...,  // 根据 offset 调整
        isFirst: ds.isFirst,
        isLast: ds.isLast
    });
endrule
```

#### 任务 4: 传递到下一级流水线

```bsv
genPacketHeaderStep1PipelineQ.enq(
    GenPacketHeaderStep1PipelineEntry{
        wqe: wqe,
        hasPayload: hasPayload
    }
);
```

### Stage 3: genPacketHeaderStep1 (行 582-676)

**功能**: 确定包边界、管理 PSN、计算 UDP 载荷长度

这是**分包机制的核心阶段**，每次从 AddressChunker 获取一个地址块，对应生成一个 RDMA 包。

```bsv
rule genPacketHeaderStep1;
    let pipelineEntryIn = genPacketHeaderStep1PipelineQ.first;
    let wqe = pipelineEntryIn.wqe;
    let hasPayload = pipelineEntryIn.hasPayload;

    // 初始化包标志
    Bool isFirstPacket = wqe.isFirst;  // WQE 级别的 isFirst
    Bool isLastPacket  = wqe.isLast;   // WQE 级别的 isLast

    let psn = psnReg;
    let remoteAddr = dontCareValue;
    let dlen = dontCareValue;

    UdpLength udpPayloadLen = fromInteger(valueOf(RDMA_FIXED_HEADER_BYTE_NUM));  // 12 字节 BTH

    if (hasPayload) begin
        // ========== 获取地址块信息 ==========
        packetInfo = wqeToPacketChunker.responsePipeOut.first;
        wqeToPacketChunker.responsePipeOut.deq;

        // ========== 确定包级别的 isFirst/isLast ==========
        // 包的标志 = WQE 标志 AND 块的标志
        isFirstPacket = isFirstPacket && packetInfo.isFirst;
        isLastPacket = isLastPacket && packetInfo.isLast;

        // ========== PSN 管理 ==========
        if (packetInfo.isFirst) begin
            psn = wqe.psn;          // 第一个包使用 WQE 的 PSN
            psnReg <= psn + 1;      // 递增 PSN 寄存器
        end
        else begin
            psn = psnReg;           // 后续包使用递增的 PSN
            psnReg <= psn + 1;
        end

        // ========== 设置地址和长度 ==========
        remoteAddr = packetInfo.startAddr;  // 包的远程起始地址
        dlen = isFirstPacket ? wqe.totalLen : packetInfo.len;  // 第一个包用总长度，其他包用块长度

        // ========== 计算 UDP 载荷长度 ==========
        ByteIdxInDword paddingByteNumForRemoteAddressAlign = truncate(remoteAddr);
        udpPayloadLen = udpPayloadLen
                      + truncate(packetInfo.len)                    // 有效载荷长度
                      + zeroExtend(paddingByteNumForRemoteAddressAlign);  // 对齐填充字节
    end
    else begin
        // 无载荷的包（如 RDMA READ Request）
        psn = wqe.psn;
        if (isReadReq) begin
            remoteAddr = wqe.raddr;
            dlen = wqe.totalLen;
        end
    end

    // ========== 控制流水线推进 ==========
    // 只有在最后一个块处理完后才出队
    if ((hasPayload && packetInfo.isLast) || !hasPayload) begin
        genPacketHeaderStep1PipelineQ.deq;
    end

    // 传递到下一级流水线
    genPacketHeaderStep2PipelineQ.enq(...);
endrule
```

**关键点解析**:

#### (1) 包标志的两级组合

```bsv
isFirstPacket = wqe.isFirst && packetInfo.isFirst;
isLastPacket = wqe.isLast && packetInfo.isLast;
```

**示例**:
```
假设一个大消息被拆分成 3 个 WQE，每个 WQE 又被拆分成 2 个包：

WQE 1 (isFirst=T, isLast=F):
  包 1.1: isFirstPacket = T && T = T,  isLastPacket = F && F = F  → SEND_FIRST
  包 1.2: isFirstPacket = T && F = F,  isLastPacket = F && T = F  → SEND_MIDDLE

WQE 2 (isFirst=F, isLast=F):
  包 2.1: isFirstPacket = F && T = F,  isLastPacket = F && F = F  → SEND_MIDDLE
  包 2.2: isFirstPacket = F && F = F,  isLastPacket = F && T = F  → SEND_MIDDLE

WQE 3 (isFirst=F, isLast=T):
  包 3.1: isFirstPacket = F && T = F,  isLastPacket = T && F = F  → SEND_MIDDLE
  包 3.2: isFirstPacket = F && F = F,  isLastPacket = T && T = T  → SEND_LAST
```

#### (2) PSN 递增管理

```
包 1: psn = wqe.psn (100),     psnReg <= 101
包 2: psn = psnReg (101),      psnReg <= 102
包 3: psn = psnReg (102),      psnReg <= 103
...
```

PSN 是 RDMA 可靠传输的基础：
- 接收方用于检测丢包（PSN 不连续）
- 接收方用于检测重复包（PSN 已收到）
- 接收方用于重排序乱序包

#### (3) UDP 载荷长度计算

```
UDP 载荷 = BTH + 扩展头 + 填充 + 有效载荷

示例 1 (第一个包，RDMA WRITE):
  remoteAddr = 0x2001 (偏移 1)
  packetInfo.len = 4095
  paddingByteNum = 1
  udpPayloadLen = 12 (BTH) + 16 (RETH) + 1 (填充) + 4095 (数据) = 4124

示例 2 (后续包，RDMA WRITE):
  remoteAddr = 0x3000 (偏移 0，已对齐)
  packetInfo.len = 4096
  paddingByteNum = 0
  udpPayloadLen = 12 (BTH) + 0 (无扩展头) + 0 (无填充) + 4096 (数据) = 4108
```

**为什么第一个包是 4095 字节**?

当远程地址不对齐时，需要在数据前插入填充字节以满足对齐要求：
```
PMTU = 4096 字节
远程地址偏移 = 1 字节
填充字节 = 1 字节
实际数据 = 4096 - 1 = 4095 字节
```

#### (4) 流水线控制逻辑

```bsv
if ((hasPayload && packetInfo.isLast) || !hasPayload) begin
    genPacketHeaderStep1PipelineQ.deq;
end
```

**关键设计**:
- 对于有载荷的 WQE，这个规则会执行多次（每个块一次）
- 只有在处理完最后一个块后才出队
- 这样可以为同一个 WQE 生成多个包

**执行流程**:
```
循环 1: 处理块 1 (isLast=F) → 不出队 → 生成包 1 元数据
循环 2: 处理块 2 (isLast=T) → 出队   → 生成包 2 元数据
```

### Stage 4: genPacketHeaderStep2 (行 678-751)

**功能**: 生成 RDMA 协议头和 MAC/IP/UDP 元数据，计算数据流拆分参数

```bsv
rule genPacketHeaderStep2;
    let pipelineEntryIn = genPacketHeaderStep2PipelineQ.first;
    genPacketHeaderStep2PipelineQ.deq;

    // 提取流水线数据
    let wqe = pipelineEntryIn.wqe;
    let hasPayload = pipelineEntryIn.hasPayload;
    let isFirstPacket = pipelineEntryIn.isFirstPacket;
    let isLastPacket = pipelineEntryIn.isLastPacket;
    let psn = pipelineEntryIn.psn;
    let remoteAddr = pipelineEntryIn.remoteAddr;
    let dlen = pipelineEntryIn.dlen;
    let udpPayloadLen = pipelineEntryIn.udpPayloadLen;

    // ========== 生成 RDMA BTH ==========
    let padCnt = 0;  // 数据流已经对齐，无需填充

    let bthMaybe <- genRdmaBTH(
        wqe, isFirstPacket, isLastPacket, solicited,
        psn, padCnt, ackReq, remoteAddr, dlen
    );

    // ========== 生成 RDMA 扩展头 ==========
    let extendHeaderBufferMaybe <- genRdmaExtendHeader(
        wqe, isFirstPacket, isLastPacket, remoteAddr, dlen
    );

    // ========== 组装 RDMA 包元数据并输出 ==========
    let rdmaPacketMeta = RdmaSendPacketMeta {
        header: RdmaBthAndExtendHeader{
            bth: bth,
            rdmaExtendHeaderBuf: extendHeaderBuffer
        },
        hasPayload: hasPayload
    };
    rdmaPacketMetaPipeOutQueue.enq(rdmaPacketMeta);

    // ========== 计算数据流拆分参数 ==========
    if (hasPayload) begin
        // 计算需要的 AlignBlock 数量
        Length truncatedStreamSplitStartAddr = truncate(remoteAddr);
        Length truncatedStreamSplitEndAddrForAlignCalc =
            truncate(remoteAddr + zeroExtend(packetInfo.len - 1));

        AlignBlockCntInPmtu alignBlockCntForStreamSplit = truncate(
            (truncatedStreamSplitEndAddrForAlignCalc >> valueOf(LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE)) -
            (truncatedStreamSplitStartAddr >> valueOf(LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE))
        ) + 1;

        // 发送给数据流拆分器
        payloadSplitorStreamAlignBlockCountPipeInConverter.enq(alignBlockCntForStreamSplit);
    end

    // ========== 生成 MAC/IP/UDP 元数据并输出 ==========
    let macIpUdpMeta = ThinMacIpUdpMetaDataForSend{
        dstMacAddr:    wqe.macAddr,
        ipDscp:        0,
        ipEcn:         wqe.enableEcn ? pack(IpHeaderEcnFlagEnabled) : pack(IpHeaderEcnFlagNotEnabled),
        dstIpAddr:     wqe.dqpIP,
        srcPort:       truncate(wqe.sqpn),          // 源端口 = QPN
        dstPort:       fromInteger(valueOf(UDP_PORT_RDMA)),  // 4791
        udpPayloadLen: udpPayloadLen,
        ethType:       fromInteger(valueOf(ETH_TYPE_IP))
    };
    macIpUdpMetaPipeOutQueue.enq(macIpUdpMeta);
endrule
```

**关键子功能详解**:

#### (1) 生成 RDMA BTH

```bsv
function ActionValue#(Maybe#(BTH)) genRdmaBTH(
    WorkQueueElem wqe,
    Bool isFirstPacket,
    Bool isLastPacket,
    Bool solicited,
    PSN psn,
    PadCnt padCnt,
    Bool ackReq,
    ADDR remoteAddr,
    Length dlen
);
    // 生成 RDMA OpCode
    let rdmaOpCodeMaybe = genRdmaOpCode(wqe.opcode, isFirstPacket, isLastPacket);

    if (rdmaOpCodeMaybe matches tagged Valid .rdmaOpCode) begin
        return tagged Valid BTH {
            trans:      unpack(0),          // Transport Header Version
            opcode:     rdmaOpCode,         // RDMA OpCode
            solicited:  solicited,          // Solicited Event
            migReq:     unpack(0),          // Migration Request
            padCnt:     padCnt,             // Pad Count
            tver:       unpack(0),          // Transport Version
            pkey:       fromInteger(valueOf(DEFAULT_PKEY)),  // Partition Key
            fecn:       unpack(0),          // Forward ECN
            becn:       unpack(0),          // Backward ECN
            resv6:      unpack(0),
            dqpn:       wqe.dqpn,           // Destination QP Number
            ackReq:     ackReq,             // Acknowledge Request
            resv7:      unpack(0),
            psn:        psn                 // Packet Sequence Number
        };
    end
    else begin
        return tagged Invalid;
    end
endfunction
```

**RDMA OpCode 生成** (`PacketGenAndParse.bsv:70-117`):

```bsv
function Maybe#(RdmaOpCode) genRdmaOpCode(WorkReqOpCode wrOpCode, Bool isFirst, Bool isLast);
    return case ({pack(isFirst), pack(isLast)})
        'b11:   // ONLY (单包消息)
                case (wrOpCode)
                    IBV_WR_RDMA_WRITE:          tagged Valid RDMA_WRITE_ONLY;
                    IBV_WR_RDMA_WRITE_WITH_IMM: tagged Valid RDMA_WRITE_ONLY_WITH_IMMEDIATE;
                    IBV_WR_SEND:                tagged Valid SEND_ONLY;
                    IBV_WR_SEND_WITH_IMM:       tagged Valid SEND_ONLY_WITH_IMMEDIATE;
                    IBV_WR_RDMA_READ:           tagged Valid RDMA_READ_REQUEST;
                    ...
                endcase
        'b10:   // FIRST (第一个包)
                case (wrOpCode)
                    IBV_WR_RDMA_WRITE:          tagged Valid RDMA_WRITE_FIRST;
                    IBV_WR_SEND:                tagged Valid SEND_FIRST;
                    ...
                endcase
        'b00:   // MIDDLE (中间包)
                case (wrOpCode)
                    IBV_WR_RDMA_WRITE:          tagged Valid RDMA_WRITE_MIDDLE;
                    IBV_WR_SEND:                tagged Valid SEND_MIDDLE;
                    ...
                endcase
        'b01:   // LAST (最后一个包)
                case (wrOpCode)
                    IBV_WR_RDMA_WRITE:          tagged Valid RDMA_WRITE_LAST;
                    IBV_WR_RDMA_WRITE_WITH_IMM: tagged Valid RDMA_WRITE_LAST_WITH_IMMEDIATE;
                    IBV_WR_SEND:                tagged Valid SEND_LAST;
                    IBV_WR_SEND_WITH_IMM:       tagged Valid SEND_LAST_WITH_IMMEDIATE;
                    ...
                endcase
    endcase;
endfunction
```

#### (2) 生成 RDMA 扩展头

```bsv
function ActionValue#(Maybe#(RdmaExtendHeaderBuffer)) genRdmaExtendHeader(
    WorkQueueElem wqe,
    Bool isFirstPacket,
    Bool isLastPacket,
    ADDR remoteAddr,
    Length dlen
);
    // RETH: RDMA Extended Transport Header (16 字节)
    // 用于 RDMA READ/WRITE 的第一个包
    if (isFirstPacket && (wqe.opcode == IBV_WR_RDMA_WRITE ||
                          wqe.opcode == IBV_WR_RDMA_WRITE_WITH_IMM ||
                          wqe.opcode == IBV_WR_RDMA_READ)) begin
        RETH reth = genRETH(wqe.opcode, remoteAddr, wqe.rkey, dlen);
        return tagged Valid pack(reth);
    end

    // ImmDt: Immediate Data (4 字节)
    // 用于 SEND_WITH_IMM / WRITE_WITH_IMM
    else if (isLastPacket && workReqHasImmDt(wqe.opcode)) begin
        ImmDt immDt = genImmDt(wqe);
        return tagged Valid pack(immDt);
    end

    // 其他扩展头 (IEth, AtomicEth, AETH 等)
    ...

    // 无扩展头
    else begin
        return tagged Valid 0;
    end
endfunction
```

**RETH 结构**:
```bsv
typedef struct {
    ADDR  va;    // 远程虚拟地址 (64 位)
    RKEY  rkey;  // 远程密钥 (32 位)
    Length dlen; // DMA 长度 (32 位)
} RETH deriving(Bits, FShow);
```

**示例**:
```
包 1 (RDMA_WRITE_FIRST):
  BTH: OpCode = RDMA_WRITE_FIRST, PSN = 100
  RETH: va = 0x2001, rkey = 0x1234, dlen = 8192

包 2 (RDMA_WRITE_LAST):
  BTH: OpCode = RDMA_WRITE_LAST, PSN = 101
  (无扩展头)
```

#### (3) 计算数据流拆分的 AlignBlock 数量

**AlignBlock**: 数据流拆分的基本单位，通常为 4 字节（Dword）

```bsv
LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE = 2  // log2(4) = 2
AlignBlock 大小 = 2^2 = 4 字节
```

**计算公式**:
```bsv
AlignBlockCnt = (endAddr >> 2) - (startAddr >> 2) + 1
```

**示例 1**: 对齐地址，4095 字节
```
startAddr = 0x2001
endAddr = 0x2001 + 4095 - 1 = 0x2FFF

startBlockIdx = 0x2001 >> 2 = 0x800
endBlockIdx = 0x2FFF >> 2 = 0xBFF
AlignBlockCnt = 0xBFF - 0x800 + 1 = 0x400 = 1024 个 AlignBlock
验证: 1024 * 4 = 4096 字节 (包含 1 字节填充)
```

**示例 2**: 对齐地址，4096 字节
```
startAddr = 0x3000
endAddr = 0x3000 + 4096 - 1 = 0x3FFF

startBlockIdx = 0x3000 >> 2 = 0xC00
endBlockIdx = 0x3FFF >> 2 = 0xFFF
AlignBlockCnt = 0xFFF - 0xC00 + 1 = 0x400 = 1024 个 AlignBlock
验证: 1024 * 4 = 4096 字节
```

**DtldStreamSplitor 的工作原理**:

```bsv
interface DtldStreamSplitor;
    interface PipeInB0#(AlignBlockCntInPmtu) streamAlignBlockCountPipeIn;
    interface PipeIn#(DataStream) dataPipeIn;
    interface PipeOut#(DataStream) dataPipeOut;
endinterface
```

**拆分逻辑** (简化):
```bsv
Reg#(AlignBlockCntInPmtu) remainingBlockCnt <- mkRegU;
Reg#(Bool) busyReg <- mkReg(False);

rule startNewSubStream if (!busyReg);
    let blockCnt = streamAlignBlockCountPipeIn.first;
    streamAlignBlockCountPipeIn.deq;
    remainingBlockCnt <= blockCnt - 1;
    busyReg <= True;

    let ds = dataPipeIn.first;
    dataPipeIn.deq;

    dataPipeOut.enq(DataStream{
        data: ds.data,
        byteEn: ds.byteEn,
        isFirst: True,  // 子流的第一个 beat
        isLast: (blockCnt == 1)  // 如果只有 1 个块，也是最后一个
    });
endrule

rule continueSubStream if (busyReg);
    let ds = dataPipeIn.first;
    dataPipeIn.deq;

    Bool isLastBeat = (remainingBlockCnt == 0);
    if (isLastBeat) begin
        busyReg <= False;
    end
    else begin
        remainingBlockCnt <= remainingBlockCnt - 1;
    end

    dataPipeOut.enq(DataStream{
        data: ds.data,
        byteEn: ds.byteEn,
        isFirst: False,
        isLast: isLastBeat  // 子流的最后一个 beat
    });
endrule
```

**拆分示例**:
```
输入数据流 (8192 字节，256 位总线 = 32 字节/beat):
  Beat 1: [32 字节], isFirst=T, isLast=F
  Beat 2: [32 字节], isFirst=F, isLast=F
  ...
  Beat 256: [32 字节], isFirst=F, isLast=T

拆分指令:
  块 1: 1024 个 AlignBlock = 4096 字节 = 128 beats
  块 2: 1024 个 AlignBlock = 4096 字节 = 128 beats

输出子流 1 (4096 字节):
  Beat 1: [32 字节], isFirst=T, isLast=F
  Beat 2: [32 字节], isFirst=F, isLast=F
  ...
  Beat 128: [32 字节], isFirst=F, isLast=T  ← 子流 1 结束

输出子流 2 (4096 字节):
  Beat 129: [32 字节], isFirst=T, isLast=F  ← 子流 2 开始
  Beat 130: [32 字节], isFirst=F, isLast=F
  ...
  Beat 256: [32 字节], isFirst=F, isLast=T  ← 子流 2 结束
```

### Stage 5: forwardSplitStream (行 753-766)

**功能**: 转发拆分后的数据流到输出队列

```bsv
rule forwardSplitStream;
    // 从数据流拆分器获取输出
    let ds = payloadSplitor.dataPipeOut.first;
    payloadSplitor.dataPipeOut.deq;

    // 转发到输出队列
    perPacketPayloadDataStreamQ.enq(ds);
endrule
```

**为什么需要这个队列**?

```bsv
// 8 个槽位，用于匹配以太网包生成器的头部处理延迟
FIFOF#(DataStream) perPacketPayloadDataStreamQ <-
    mkSizedFIFOFWithFullAssert(8, DebugConf{name: "mkPacketGen perPacketPayloadDataStreamQ", enableDebug: False});
```

**以太网包生成器的头部处理**:
- genFirstBeat: 生成 MAC/IP/UDP 头 (1 beat)
- genSecondBeat: 继续头部 (1 beat)
- genThirdBeat: 完成头部和 RDMA 头 (1 beat)
- IP 校验和计算: 额外延迟 (~5 beats)

总延迟约 8 beats，因此需要 8 个槽位来保证流水线不阻塞。

## 5. 完整分包流程示例

假设一个 RDMA WRITE 请求:
- 本地地址: `laddr = 0x100002` (偏移 2)
- 远程地址: `raddr = 0x200001` (偏移 1)
- 数据长度: `len = 8192` (8K)
- PMTU: `IBV_MTU_4096` (4096 字节)
- 数据总线宽度: 256 位 (32 字节)

### 5.1 数据流处理时间线

```
时刻 T0: Stage 1 - queryMrTable
  输入: WorkQueueElem (laddr=0x100002, raddr=0x200001, len=8192, pmtu=4096)
  操作: 发起 MR Table 查询 (lkey → MR Table Index)
  输出: → Stage 2 队列

时刻 T1: Stage 2 - sendChunkByRemoteAddrReqAndPayloadGenReq
  输入: 从 Stage 1
  操作:
    1. 发起地址分块: AddressChunkReq{startAddr: 0x200001, len: 8192, chunk: 12}
       → AddressChunker 输出 2 个块:
          块 1: {startAddr: 0x200001, len: 4095, isFirst: T, isLast: F}
          块 2: {startAddr: 0x201000, len: 4097, isFirst: F, isLast: T}

    2. 获取 MR Table 响应: {baseVA, pgtOffset}

    3. 发起 DMA 读取: PayloadGenReq{addr: 0x100002, len: 8192, baseVA, pgtOffset}
       → PayloadGenAndCon
       → DMA 读取引擎
       → genRespPipeInQ (约 100+ 时钟周期后)

    4. 计算对齐偏移: localOffset=2, remoteOffset=1, shift=+1
       → StreamShifter.offsetPipeIn

  输出: → Stage 3 队列

时刻 T2: Stage 3 - genPacketHeaderStep1 (循环 1, 处理块 1)
  输入: 从 Stage 2
  操作:
    1. 获取块 1: {startAddr: 0x200001, len: 4095, isFirst: T, isLast: F}
    2. 包标志: isFirstPacket = T && T = T, isLastPacket = F && F = F
    3. PSN 管理: psn = wqe.psn = 100, psnReg <= 101
    4. UDP 长度: 12 + 16 (RETH) + 1 (填充) + 4095 = 4124
    5. 不出队 (packetInfo.isLast = F)
  输出: → Stage 4 队列 (包 1 的元数据)

时刻 T3: Stage 3 - genPacketHeaderStep1 (循环 2, 处理块 2)
  输入: 仍在 Stage 3 队列
  操作:
    1. 获取块 2: {startAddr: 0x201000, len: 4097, isFirst: F, isLast: T}
    2. 包标志: isFirstPacket = F && F = F, isLastPacket = T && T = T
    3. PSN 管理: psn = psnReg = 101, psnReg <= 102
    4. UDP 长度: 12 + 0 + 0 + 4097 = 4109
    5. 出队 (packetInfo.isLast = T)
  输出: → Stage 4 队列 (包 2 的元数据)

时刻 T4: Stage 4 - genPacketHeaderStep2 (处理包 1)
  输入: 包 1 元数据
  操作:
    1. 生成 BTH: OpCode=RDMA_WRITE_FIRST, PSN=100
    2. 生成 RETH: va=0x200001, rkey=xxx, dlen=8192
    3. 生成 MAC/IP/UDP 元数据: udpPayloadLen=4124
    4. 计算 AlignBlock 数量:
       startBlockIdx = 0x200001 >> 2 = 0x80000
       endBlockIdx = 0x200FFF >> 2 = 0x803FF
       AlignBlockCnt = 0x803FF - 0x80000 + 1 = 1024
       → DtldStreamSplitor.streamAlignBlockCountPipeIn
  输出:
    - rdmaPacketMetaPipeOutQueue: 包 1 的 RDMA 元数据
    - macIpUdpMetaPipeOutQueue: 包 1 的 MAC/IP/UDP 元数据

时刻 T5: Stage 4 - genPacketHeaderStep2 (处理包 2)
  输入: 包 2 元数据
  操作:
    1. 生成 BTH: OpCode=RDMA_WRITE_LAST, PSN=101
    2. 无扩展头
    3. 生成 MAC/IP/UDP 元数据: udpPayloadLen=4109
    4. 计算 AlignBlock 数量:
       startBlockIdx = 0x201000 >> 2 = 0x80400
       endBlockIdx = 0x202000 >> 2 = 0x80800
       AlignBlockCnt = 0x80800 - 0x80400 + 1 = 1025
       → DtldStreamSplitor.streamAlignBlockCountPipeIn
  输出:
    - rdmaPacketMetaPipeOutQueue: 包 2 的 RDMA 元数据
    - macIpUdpMetaPipeOutQueue: 包 2 的 MAC/IP/UDP 元数据

时刻 T100+: DMA 读取完成，数据流到达
  DMA 读取 → genRespPipeInQ
  数据流 (8192 字节 = 256 beats):
    Beat 1: [32 字节], isFirst=T, isLast=F
    Beat 2: [32 字节], isFirst=F, isLast=F
    ...
    Beat 256: [32 字节], isFirst=F, isLast=T

时刻 T101+: StreamShifter 对齐转换
  输入: 本地对齐数据流, offset=+1
  操作: 右移 1 字节
  输出: 远程对齐数据流 → DtldStreamSplitor

时刻 T102+: DtldStreamSplitor 拆分数据流
  输入:
    - 数据流 (256 beats)
    - 块 1: 1024 AlignBlocks = 128 beats
    - 块 2: 1025 AlignBlocks = 128.125 beats

  输出子流 1 (4096 字节):
    Beat 1: [32 字节], isFirst=T, isLast=F
    ...
    Beat 128: [32 字节], isFirst=F, isLast=T

  输出子流 2 (4097 字节):
    Beat 129: [32 字节], isFirst=T, isLast=F
    ...
    Beat 256: [32 字节], isFirst=F, isLast=T

时刻 T102+: Stage 5 - forwardSplitStream
  操作: 转发子流到 perPacketPayloadDataStreamQ
  输出: → rdmaPayloadPipeOut
```

### 5.2 三路输出同步

PacketGen 的三路输出必须严格同步：

```
时刻 T4:
  rdmaPacketMetaPipeOutQueue.enq(包 1 RDMA 元数据)
  macIpUdpMetaPipeOutQueue.enq(包 1 MAC/IP/UDP 元数据)

时刻 T5:
  rdmaPacketMetaPipeOutQueue.enq(包 2 RDMA 元数据)
  macIpUdpMetaPipeOutQueue.enq(包 2 MAC/IP/UDP 元数据)

时刻 T102+:
  perPacketPayloadDataStreamQ.enq(包 1 数据流)
  perPacketPayloadDataStreamQ.enq(包 2 数据流)
```

**同步保证**:
1. 元数据按照流水线顺序生成（Stage 4）
2. 数据流按照拆分顺序输出（DtldStreamSplitor）
3. FIFO 队列保证先进先出

**下游消费（EthernetPacketGenerator）**:
```bsv
rule genPacket;
    // 同步消费三路数据
    let macIpUdpMeta = macIpUdpMetaPipeIn.first;
    macIpUdpMetaPipeIn.deq;

    let rdmaPacketMeta = rdmaPacketMetaPipeIn.first;
    rdmaPacketMetaPipeIn.deq;

    if (rdmaPacketMeta.hasPayload) begin
        let ds = rdmaPayloadPipeIn.first;
        rdmaPayloadPipeIn.deq;
        // 组装以太网包...
    end
endrule
```

## 6. 分包的关键设计要点

### 6.1 PMTU 对齐和边界计算

AddressChunker 的核心算法:

```bsv
// 输入
startAddr = 0x200001
len = 8192
chunkSize = 4096 (2^12)

// 第一个块的起始和结束 chunk 索引
startChunkIdx = startAddr >> chunk = 0x200001 >> 12 = 0x200
endChunkIdx = (startAddr + len - 1) >> chunk = 0x202000 >> 12 = 0x202

// 零基的块数量
zeroBasedChunkCnt = endChunkIdx - startChunkIdx = 0x202 - 0x200 = 2
// 实际块数量 = 2 + 1 = 3 个块

// 第一个块的非对齐字节数
chunkRemainingMask = chunkSize - 1 = 0xFFF
nonValidByteCntInFirstChunk = startAddr & chunkRemainingMask = 0x200001 & 0xFFF = 0x001 = 1

// 第一个块的长度
firstChunkLen = chunkSize - nonValidByteCntInFirstChunk = 4096 - 1 = 4095

// 各块的长度
块 1: 4095 字节 (0x200001 ~ 0x200FFF)
块 2: 4096 字节 (0x201000 ~ 0x201FFF)
块 3: 1 字节 (0x202000 ~ 0x202000)
总计: 4095 + 4096 + 1 = 8192 ✓
```

**边界对齐保证**:
- 除第一个块外，所有块的起始地址都对齐到 PMTU 边界
- 块 2 起始: 0x201000 (对齐到 4K)
- 块 3 起始: 0x202000 (对齐到 4K)

### 6.2 地址对齐的必要性

RDMA 协议规定：**有效载荷必须按照远程地址对齐**

**为什么**?
- 接收方需要直接 DMA 写入远程内存
- 硬件 DMA 引擎要求数据按照目标地址对齐
- 避免额外的数据拷贝和对齐操作

**示例**:
```
远程地址: 0x200001 (偏移 1)
数据: [D0 D1 D2 D3 D4 D5 D6 D7]

错误的对齐 (按 dword 边界):
  0x200000: [-- D0 D1 D2]
  0x200004: [D3 D4 D5 D6]
  0x200008: [D7 -- -- --]
  → 远程地址 0x200001 处的数据是 D0，但实际应该是 D0

正确的对齐 (按远程地址):
  0x200000: [-- -- -- --]  (填充)
  0x200000: [-- D0 D1 D2]
  0x200004: [D3 D4 D5 D6]
  0x200008: [D7 -- -- --]
  → 远程地址 0x200001 处的数据是 D0 ✓
```

**StreamShifter 的作用**:
- 输入: 本地对齐的数据流
- 偏移量: +1 字节（右移）
- 输出: 远程对齐的数据流

### 6.3 填充字节的计算

```bsv
ByteIdxInDword paddingByteNumForRemoteAddressAlign = truncate(remoteAddr);
```

**示例**:
```
远程地址 0x200001:
  remoteAddr & 0x3 = 0x200001 & 0x3 = 1
  填充字节数 = 1

远程地址 0x200000:
  remoteAddr & 0x3 = 0x200000 & 0x3 = 0
  填充字节数 = 0 (无需填充)

远程地址 0x200003:
  remoteAddr & 0x3 = 0x200003 & 0x3 = 3
  填充字节数 = 3
```

**填充字节的位置**:
- 在 BTH 和扩展头之后
- 在有效载荷之前
- 由 EthernetPacketGenerator 插入

### 6.4 PSN 的 24 位循环

PSN 是 24 位计数器，范围 0 ~ 16,777,215:

```bsv
typedef Bit#(24) PSN;

psnReg <= psn + 1;  // 自动循环（硬件特性）
```

**循环示例**:
```
psn = 16,777,215 (0xFFFFFF)
psnReg <= psn + 1 = 0 (溢出循环)
```

**接收方处理**:
- 使用模运算比较 PSN
- 处理 PSN 回绕情况

### 6.5 数据流拆分的精确性

DtldStreamSplitor 必须精确按照 AlignBlock 数量拆分：

```
AlignBlock = 4 字节
数据总线 = 32 字节/beat = 8 AlignBlocks/beat

示例: 1024 AlignBlocks = 4096 字节
  128 完整的 beats × 8 AlignBlocks/beat = 1024 AlignBlocks ✓

示例: 1025 AlignBlocks = 4100 字节
  128 完整的 beats = 1024 AlignBlocks
  + 1/8 beat (4 字节) = 1 AlignBlock
  总计: 1025 AlignBlocks ✓
```

**部分 beat 的处理**:
```bsv
DataStream {
    data: [32 字节],
    byteEn: 0x0000000F,  // 只有低 4 字节有效
    isFirst: False,
    isLast: True
}
```

## 7. 性能优化要点

### 7.1 流水线深度配置

```bsv
// 流水线队列
sendChunkByRemoteAddrReqAndPayloadGenReqPipelineQ <- mkSizedFIFOF(5);   // 5 个槽位
genPacketHeaderStep1PipelineQ <- mkSizedFIFOF(4);                        // 4 个槽位
genPacketHeaderStep2PipelineQ <- mkLFIFOF;                               // 2 个槽位（默认）

// 输出队列（大容量）
macIpUdpMetaPipeOutQueue <- mkSizedFIFOF(256);     // 容纳 PCIe 延迟
rdmaPacketMetaPipeOutQueue <- mkSizedFIFOF(256);
perPacketPayloadDataStreamQ <- mkSizedFIFOF(8);    // 匹配以太网包生成器延迟
```

**设计考虑**:
- 输出队列深度 256: 足够容纳 PCIe DMA 读取延迟（~100-200 时钟周期）
- 数据流队列深度 8: 匹配 EthernetPacketGenerator 的头部处理延迟
- 流水线队列深度 4-5: 平衡面积和性能

### 7.2 全流水线设计

PacketGen 的关键性能指标：**每时钟周期可以处理一个包**

**流水线吞吐量**:
```
理想情况下，5 个阶段同时处理 5 个不同的包：
  Stage 1: 处理包 N+4
  Stage 2: 处理包 N+3
  Stage 3: 处理包 N+2 (循环 1)
  Stage 4: 处理包 N+1
  Stage 5: 处理包 N

每个时钟周期输出 1 个包的元数据
```

**数据流吞吐量**:
```
数据总线宽度: 256 bits = 32 bytes
时钟频率: 250 MHz
理论带宽: 32 × 250 MHz = 8 GB/s = 64 Gbps
```

### 7.3 反压处理

PacketGen 使用 FIFO 队列处理反压：

```
如果 rdmaPacketMetaPipeOutQueue 满:
  → Stage 4 阻塞
  → Stage 3 阻塞
  → Stage 2 阻塞
  → Stage 1 阻塞
  → wqePipeInQ 阻塞
  → 上游停止发送 WQE
```

**队列满的原因**:
1. 下游 EthernetPacketGenerator 处理慢
2. 网络接口拥塞
3. PCIe DMA 读取延迟过大

## 8. 总结

PacketGen 的分包机制是一个精密的多级流水线系统：

**三个核心组件**:
1. **AddressChunker**: 按 PMTU 拆分地址范围
2. **StreamShifter**: 本地地址对齐 → 远程地址对齐
3. **DtldStreamSplitor**: 按 AlignBlock 数量拆分数据流

**五个流水线阶段**:
1. queryMrTable: 查询内存区域表
2. sendChunkByRemoteAddrReq: 地址分块、对齐计算、发起 DMA
3. genPacketHeaderStep1: 确定包边界、PSN 管理、UDP 长度
4. genPacketHeaderStep2: 生成协议头、计算拆分参数
5. forwardSplitStream: 转发数据流

**关键设计特性**:
- **PMTU 对齐**: 所有包（除第一个）按 PMTU 边界对齐
- **地址对齐**: 有效载荷按远程地址对齐
- **PSN 递增**: 每个包递增 PSN
- **OpCode 生成**: 根据 isFirst/isLast 生成正确的 RDMA OpCode
- **三路同步**: 协议元数据和数据流严格同步

**性能优化**:
- 全流水线设计，达到每时钟周期处理一个包
- 大容量输出队列，容纳 PCIe 延迟
- FIFO 反压机制，保证数据完整性

**相关文档**:
- [PacketGen模块详解.md](./PacketGen模块详解.md) - 接口和架构介绍
- [PacketGen分包机制详解.md](./PacketGen分包机制详解.md) - 分包算法详解
- [EthernetPacketGenerator详解.md](./EthernetPacketGenerator详解.md) - 下游模块
