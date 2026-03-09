# PacketGen 模块详解

## 1. 概述

PacketGen 是 RDMA 发送路径中的核心模块，位于 `/home/peng/projects/rdma_all/open-rdma-rtl/src/PacketGenAndParse.bsv:447`。它的主要作用是将上层的工作队列元素（WorkQueueElem）转换为可以发送的 RDMA 数据包，包括生成 MAC/IP/UDP 协议头、RDMA 协议头，以及处理有效载荷数据流。

## 2. PacketGen 在系统架构中的位置

```
软件层 WQE (Work Queue Element)
        ↓
    描述符解析
        ↓
WorkQueueDescParser
        ↓
    【PacketGen】 ← 本模块
        ↓
PacketGenReqArbiter (仲裁器)
        ↓
EthernetPacketGenerator (以太网帧生成)
        ↓
    网络接口
```

**数据流向**:
1. **输入**: 软件通过描述符提交的 RDMA 发送请求（WQE）
2. **PacketGen 处理**: 生成协议头和处理载荷数据
3. **输出**: 三路并行输出流给以太网包生成器

## 3. PacketGen 接口定义

```bsv
interface PacketGen;
    // 输入接口: 工作队列元素
    interface PipeInB0#(WorkQueueElem) wqePipeIn;

    // 三路输出接口: 协议元数据和有效载荷
    interface PipeOut#(ThinMacIpUdpMetaDataForSend) macIpUdpMetaPipeOut;
    interface PipeOut#(RdmaSendPacketMeta)          rdmaPacketMetaPipeOut;
    interface PipeOut#(DataStream)                  rdmaPayloadPipeOut;

    // MR Table 查询客户端
    interface ClientP#(MrTableQueryReq, Maybe#(MemRegionTableEntry)) mrTableQueryClt;

    // Payload 生成接口（连接到 PayloadGenAndCon 模块）
    interface PipeOut#(PayloadGenReq) genReqPipeOut;
    interface PipeIn#(DataStream) genRespPipeIn;
endinterface
```

### 3.1 输入接口详解

#### WorkQueueElem (工作队列元素)

这是 PacketGen 的唯一输入，包含了 RDMA 发送请求的所有信息：

```bsv
typedef struct {
    MSN msn;                                        // 消息序列号 (16 bits)
    WorkReqOpCode opcode;                           // 操作码 (4 bits)
        // 支持的操作: SEND, WRITE, READ, ATOMIC 等
    FlagsType#(WorkReqSendFlag) flags;              // 标志位 (5 bits)
        // IBV_SEND_SIGNALED: 需要完成通知
        // IBV_SEND_SOLICITED: 请求对方发送通知
    TypeQP qpType;                                  // QP 类型 (4 bits)
    PSN psn;                                        // 包序列号 (24 bits)
    PMTU pmtu;                                      // 路径MTU (3 bits)
        // IBV_MTU_256/512/1024/2048/4096

    // 网络地址信息
    IpAddr dqpIP;                                   // 目标 QP 的 IP 地址 (32 bits)
    EthMacAddr macAddr;                             // 目标 MAC 地址 (48 bits)

    // 本地内存地址和密钥
    ADDR   laddr;                                   // 本地虚拟地址 (64 bits)
    LKEY   lkey;                                    // 本地密钥 (32 bits)

    // 远程内存地址和密钥
    ADDR raddr;                                     // 远程虚拟地址 (64 bits)
    RKEY rkey;                                      // 远程密钥 (32 bits)

    // 数据长度
    Length len;                                     // 当前段长度 (32 bits)
    Length totalLen;                                // 总长度 (32 bits)

    // QP 编号
    QPN dqpn;                                       // 目标 QP 编号 (24 bits)
    QPN sqpn;                                       // 源 QP 编号 (24 bits)

    // 原子操作参数
    Maybe#(Long) comp;                              // Compare 值 (65 bits)
    Maybe#(Long) swap;                              // Swap 值 (65 bits)

    // 立即数或失效密钥
    Maybe#(ImmOrRKey) immDtOrInvRKey;              // 立即数或失效的 RKey (34 bits)

    // 扩展参数
    Maybe#(QPN) srqn;                              // XRC 的 SRQ 编号 (25 bits)
    Maybe#(QKEY) qkey;                             // UD 的 Q_Key (33 bits)

    // 分段标志
    Bool isFirst;                                   // 是否是第一个包
    Bool isLast;                                    // 是否是最后一个包
    Bool isRetry;                                   // 是否是重传
    Bool enableEcn;                                 // 是否启用 ECN

    SimulationTime fpDebugTime;                     // 调试时间戳
} WorkQueueElem deriving(Bits, FShow);
```

**关键字段说明**:
- **opcode**: 决定了 RDMA 操作类型，PacketGen 会据此生成不同的 RDMA 协议头
- **pmtu**: 决定了分包的大小，PacketGen 会根据 PMTU 将大数据包拆分成多个小包
- **laddr/lkey**: 用于查询 MR Table 和生成 Payload Gen 请求
- **raddr/rkey**: 用于生成 RDMA Extended Header (RETH)
- **isFirst/isLast**: 用于生成正确的 RDMA OpCode (FIRST/MIDDLE/LAST/ONLY)

### 3.2 输出接口详解

PacketGen 产生三路并行输出流，这三路流**必须同步**，每个 RDMA 包对应一组三元组：

#### (1) macIpUdpMetaPipeOut - MAC/IP/UDP 元数据

```bsv
typedef struct {
    EthMacAddr dstMacAddr;      // 目标 MAC 地址
    IpDscp     ipDscp;          // IP DSCP (服务质量标记)
    IpEcn      ipEcn;           // IP ECN (拥塞通知)
    IpAddr     dstIpAddr;       // 目标 IP 地址
    UdpPort    srcPort;         // 源 UDP 端口 (从 QP 号派生)
    UdpPort    dstPort;         // 目标 UDP 端口 (RDMA 固定端口 4791)
    UdpLength  udpPayloadLen;   // UDP 有效载荷长度
    EthType    ethType;         // 以太网类型 (IPv4 = 0x0800)
} ThinMacIpUdpMetaDataForSend deriving(Bits, FShow, Eq);
```

**生成位置**: `PacketGenAndParse.bsv:726-736`

**作用**: 提供给 EthernetPacketGenerator 模块用于组装以太网/IP/UDP 协议头。

#### (2) rdmaPacketMetaPipeOut - RDMA 包元数据

```bsv
typedef struct {
    RdmaBthAndExtendHeader header;  // RDMA 协议头
    Bool hasPayload;                 // 是否有有效载荷
} RdmaSendPacketMeta deriving(Bits, FShow);

// RDMA 协议头包含
typedef struct {
    BTH bth;                         // Base Transport Header
    RdmaExtendHeaderBuffer rdmaExtendHeaderBuf;  // 扩展头 (RETH/IMMDT 等)
} RdmaBthAndExtendHeader deriving(Bits, FShow);
```

**BTH (Base Transport Header)** 包含:
- OpCode: RDMA 操作码 (由 WQE.opcode 和 isFirst/isLast 组合生成)
- PSN: 包序列号
- DestQPN: 目标 QP 编号
- AckReq: 是否需要 ACK
- Solicited: 是否需要对方发送通知
- Pad Count: 填充字节数

**RDMA 扩展头** 根据操作类型包含:
- **RETH** (RDMA Extended Transport Header): 用于 READ/WRITE 操作
  - Virtual Address: 远程虚拟地址
  - R_Key: 远程密钥
  - DMA Length: 数据长度
- **ImmDt** (Immediate Data): 用于 SEND_WITH_IMM / WRITE_WITH_IMM
- **AETH** (ACK Extended Transport Header): 用于 ACK 包

**生成位置**: `PacketGenAndParse.bsv:698-716`

#### (3) rdmaPayloadPipeOut - RDMA 有效载荷数据流

```bsv
typedef struct {
    Bit#(DATA_BUS_WIDTH) data;      // 数据 (256 bits)
    ByteEnInBeat byteEn;            // 字节使能
    Bool isFirst;                   // 是否是第一个 beat
    Bool isLast;                    // 是否是最后一个 beat
} DataStream deriving(Bits, FShow);
```

**特点**:
- 已经按照**远程地址对齐**的数据流
- 通过 StreamShifter 进行本地地址到远程地址的对齐转换
- 通过 DtldStreamSplitor 按照 PMTU 边界拆分成多个子流
- 每个子流对应一个 RDMA 包的有效载荷

**生成位置**: `PacketGenAndParse.bsv:753-766`

### 3.3 辅助接口详解

#### (1) MR Table 查询接口

```bsv
interface ClientP#(MrTableQueryReq, Maybe#(MemRegionTableEntry)) mrTableQueryClt;
```

**作用**: 查询内存区域表，获取本地地址对应的物理地址和访问权限

**查询请求**:
```bsv
typedef struct {
    IndexMR idx;  // MR 表索引 (从 lkey 派生)
} MrTableQueryReq deriving(Bits, FShow);
```

**查询响应**:
```bsv
typedef struct {
    ADDR baseVA;        // 基虚拟地址
    PTEIndex pgtOffset; // 页表偏移
    Length len;         // 内存区域长度
    FlagsType#(MemAccessTypeFlag) accFlags;  // 访问标志
} MemRegionTableEntry deriving(Bits, FShow);
```

#### (2) Payload 生成接口

**请求接口** (genReqPipeOut):
```bsv
typedef struct {
    ADDR            addr;       // 虚拟地址
    Length          len;        // 数据长度
    PTEIndex        pgtOffset;  // 页表偏移
    ADDR            baseVA;     // 基虚拟地址
} PayloadGenReq deriving(Bits, FShow);
```

**响应接口** (genRespPipeIn):
- 接收从 DMA 读取的数据流
- 数据流格式: `DataStream`

**连接关系**:
```
PacketGen.genReqPipeOut
    → PayloadGenAndCon.genReqPipeIn
    → DMA 读取引擎
    → PayloadGenAndCon.payloadGenStreamPipeOut
    → PacketGen.genRespPipeIn
```

## 4. PacketGen 的五阶段流水线

PacketGen 内部采用 5 级流水线处理，每个阶段通过 FIFOF 队列连接，保证高吞吐量：

```
Stage 1: queryMrTable
   ↓
Stage 2: sendChunkByRemoteAddrReqAndPayloadGenReq
   ↓
Stage 3: genPacketHeaderStep1
   ↓
Stage 4: genPacketHeaderStep2
   ↓
Stage 5: forwardSplitStream
```

### Stage 1: queryMrTable (`PacketGenAndParse.bsv:493-521`)

**功能**:
- 接收 WorkQueueElem
- 判断是否需要有效载荷 (`workReqNeedPayloadGen(wqe.opcode)`)
- 如果需要载荷，向 MR Table 发起查询请求

**关键逻辑**:
```bsv
rule queryMrTable;
    let wqe = wqePipeInQ.first;
    wqePipeInQ.deq;
    Bool hasPayload = workReqNeedPayloadGen(wqe.opcode) && !isZeroR(wqe.len);

    if (hasPayload) begin
        let mrTableQueryReq = MrTableQueryReq{
            idx: lkey2IndexMR(wqe.lkey)  // 从 lkey 提取 MR 表索引
        };
        mrTableQueryCltInst.putReq(mrTableQueryReq);
    end

    sendChunkByRemoteAddrReqAndPayloadGenReqPipelineQ.enq(...);
endrule
```

**无载荷的操作类型**:
- RDMA READ Request (数据在响应包中)
- SEND_ONLY (零长度消息)
- 原子操作 (只有 8 字节)

### Stage 2: sendChunkByRemoteAddrReqAndPayloadGenReq (`PacketGenAndParse.bsv:524-580`)

**功能**:
1. 发起地址分块请求（基于 PMTU）
2. 获取 MR Table 查询响应
3. 生成 Payload 读取请求
4. 计算本地到远程地址的对齐偏移

**关键逻辑**:

**(1) 地址分块请求**:
```bsv
if (hasPayload) begin
    let remoteAddrChunkReq = AddressChunkReq{
        startAddr: wqe.raddr,      // 远程起始地址
        len: wqe.len,              // 总长度
        chunk: getPmtuSizeByPmtuEnum(wqe.pmtu)  // PMTU 大小
    };
    wqeToPacketChunkerRequestPipeInAdapter.enq(remoteAddrChunkReq);
end
```

**AddressChunker** 会将地址范围按照 PMTU 边界拆分成多个块，每个块对应一个 RDMA 包。

**(2) 获取 MR Table 响应**:
```bsv
let mrTableMaybe <- mrTableQueryCltInst.getResp;
let mrTable = fromMaybe(?, mrTableMaybe);
```

**(3) 生成 Payload 读取请求**:
```bsv
let payloadGenReq = PayloadGenReq{
    addr:      wqe.laddr,          // 本地虚拟地址
    len:       wqe.len,            // 数据长度
    baseVA:    mrTable.baseVA,     // 基虚拟地址
    pgtOffset: mrTable.pgtOffset   // 页表偏移
};
genReqPipeOutQ.enq(payloadGenReq);
```

**(4) 计算地址对齐偏移**:
```bsv
ByteIdxInDword localAddrOffset = truncate(wqe.laddr);   // 本地地址的 dword 内偏移
ByteIdxInDword remoteAddrOffset = truncate(wqe.raddr);  // 远程地址的 dword 内偏移
DataBusSignedByteShiftOffset localToRemoteAlignShiftOffset =
    zeroExtend(localAddrOffset) - zeroExtend(remoteAddrOffset);
payloadStreamShifterOffsetPipeInConverter.enq(localToRemoteAlignShiftOffset);
```

**为什么需要地址对齐**?
- RDMA 要求有效载荷按照**远程地址对齐**
- 本地地址和远程地址可能有不同的 dword 内偏移
- StreamShifter 会根据偏移量调整数据流

**示例**:
```
本地地址: 0x1002  (偏移 2)
远程地址: 0x2001  (偏移 1)
偏移量: 2 - 1 = 1 字节右移
```

### Stage 3: genPacketHeaderStep1 (`PacketGenAndParse.bsv:582-676`)

**功能**:
1. 获取地址分块响应（每个块对应一个包）
2. 确定包的 isFirst/isLast 标志
3. 计算 PSN 并递增
4. 计算 UDP 载荷长度

**关键逻辑**:

**(1) 获取地址块信息**:
```bsv
if (hasPayload) begin
    packetInfo = wqeToPacketChunker.responsePipeOut.first;
    wqeToPacketChunker.responsePipeOut.deq;

    // 结合 WQE 和块的标志
    isFirstPacket = isFirstPacket && packetInfo.isFirst;
    isLastPacket = isLastPacket && packetInfo.isLast;

    remoteAddr = packetInfo.startAddr;  // 包的远程地址
    dlen = isFirstPacket ? wqe.totalLen : packetInfo.len;  // 数据长度
}
```

**AddressChunkResp** 结构:
```bsv
typedef struct {
    ADDR startAddr;  // 块的起始地址
    Length len;      // 块的长度
    Bool isFirst;    // 是否是第一个块
    Bool isLast;     // 是否是最后一个块
} AddressChunkResp deriving(Bits, FShow);
```

**(2) PSN 管理**:
```bsv
if (packetInfo.isFirst) begin
    psn = wqe.psn;          // 第一个包使用 WQE 的 PSN
    psnReg <= psn + 1;      // 递增 PSN
end
else begin
    psn = psnReg;           // 后续包使用递增的 PSN
    psnReg <= psn + 1;
end
```

**(3) 计算 UDP 载荷长度**:
```bsv
UdpLength udpPayloadLen = fromInteger(valueOf(RDMA_FIXED_HEADER_BYTE_NUM)); // 基础: 12 字节 BTH

if (hasPayload) begin
    ByteIdxInDword paddingByteNumForRemoteAddressAlign = truncate(remoteAddr);
    udpPayloadLen = udpPayloadLen
                  + truncate(packetInfo.len)                    // 有效载荷长度
                  + zeroExtend(paddingByteNumForRemoteAddressAlign);  // 对齐填充
end
```

**UDP 载荷长度组成**:
- BTH: 12 字节
- 扩展头 (RETH/ImmDt 等): 0-16 字节
- 填充字节: 0-3 字节（用于远程地址对齐）
- 有效载荷: 0-PMTU 字节

### Stage 4: genPacketHeaderStep2 (`PacketGenAndParse.bsv:678-751`)

**功能**:
1. 生成 RDMA BTH (Base Transport Header)
2. 生成 RDMA 扩展头 (RETH/ImmDt 等)
3. 生成 MAC/IP/UDP 元数据
4. 发起数据流拆分请求

**关键逻辑**:

**(1) 生成 BTH**:
```bsv
let bthMaybe <- genRdmaBTH(
    wqe,            // 工作队列元素
    isFirstPacket,  // 是否是第一个包
    isLastPacket,   // 是否是最后一个包
    solicited,      // 是否需要对方发送通知
    psn,            // 包序列号
    padCnt,         // 填充字节数
    ackReq,         // 是否需要 ACK
    remoteAddr,     // 远程地址
    dlen            // 数据长度
);
```

**BTH 生成** (`PacketGenAndParse.bsv:78-139`):
- 根据 WQE.opcode 和 isFirst/isLast 确定 RDMA OpCode:
  ```
  SEND + First + Last  → SEND_ONLY
  SEND + First + !Last → SEND_FIRST
  SEND + !First + !Last → SEND_MIDDLE
  SEND + !First + Last → SEND_LAST
  ```
- 设置 BTH 字段

**(2) 生成扩展头**:
```bsv
let extendHeaderBufferMaybe <- genRdmaExtendHeader(
    wqe,            // 工作队列元素
    isFirstPacket,  // 是否是第一个包
    isLastPacket,   // 是否是最后一个包
    remoteAddr,     // 远程地址
    dlen            // 数据长度
);
```

**扩展头类型**:
- **RETH** (16 字节): RDMA READ/WRITE 的第一个包
  ```bsv
  typedef struct {
      ADDR  va;    // 远程虚拟地址
      RKEY  rkey;  // 远程密钥
      Length dlen; // DMA 长度
  } RETH deriving(Bits, FShow);
  ```
- **ImmDt** (4 字节): SEND_WITH_IMM / WRITE_WITH_IMM
- **IEth** (4 字节): SEND_WITH_INV
- **AtomicEth** (28 字节): 原子操作

**(3) 组装 RDMA 包元数据**:
```bsv
let rdmaPacketMeta = RdmaSendPacketMeta {
    header: RdmaBthAndExtendHeader{
        bth: bth,
        rdmaExtendHeaderBuf: extendHeaderBuffer
    },
    hasPayload: hasPayload
};
rdmaPacketMetaPipeOutQueue.enq(rdmaPacketMeta);
```

**(4) 生成 MAC/IP/UDP 元数据**:
```bsv
let macIpUdpMeta = ThinMacIpUdpMetaDataForSend{
    dstMacAddr:    wqe.macAddr,                 // 目标 MAC
    ipDscp:        0,                           // DSCP
    ipEcn:         wqe.enableEcn ? pack(IpHeaderEcnFlagEnabled) : pack(IpHeaderEcnFlagNotEnabled),
    dstIpAddr:     wqe.dqpIP,                   // 目标 IP
    srcPort:       truncate(wqe.sqpn),          // 源端口 (QPN)
    dstPort:       fromInteger(valueOf(UDP_PORT_RDMA)),  // RDMA 端口 4791
    udpPayloadLen: udpPayloadLen,               // UDP 载荷长度
    ethType:       fromInteger(valueOf(ETH_TYPE_IP))     // IPv4
};
macIpUdpMetaPipeOutQueue.enq(macIpUdpMeta);
```

**(5) 发起数据流拆分**:
```bsv
if (hasPayload) begin
    // 计算需要拆分的 AlignBlock 数量
    AlignBlockCntInPmtu alignBlockCntForStreamSplit = truncate(
        (truncatedStreamSplitEndAddrForAlignCalc >> valueOf(LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE)) -
        (truncatedStreamSplitStartAddr >> valueOf(LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE))
    ) + 1;
    payloadSplitorStreamAlignBlockCountPipeInConverter.enq(alignBlockCntForStreamSplit);
end
```

**AlignBlock**: 数据流的基本拆分单位，通常为 4 字节（LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE = 2）

### Stage 5: forwardSplitStream (`PacketGenAndParse.bsv:753-766`)

**功能**: 转发拆分后的数据流

**逻辑**:
```bsv
rule forwardSplitStream;
    let ds = payloadSplitor.dataPipeOut.first;
    payloadSplitor.dataPipeOut.deq;
    perPacketPayloadDataStreamQ.enq(ds);
endrule
```

**DtldStreamSplitor** 的作用:
- 输入: 一个长数据流（整个 WQE 的数据）
- 输出: 多个短数据流（每个流对应一个 RDMA 包的载荷）
- 拆分依据: Stage 4 提供的 AlignBlock 数量

## 5. 内部组件详解

### 5.1 AddressChunker

**位置**: `/home/peng/projects/rdma_all/open-rdma-rtl/src/AddressChunker.bsv:37`

**作用**: 将地址范围按照 PMTU 边界拆分成多个块

**接口**:
```bsv
interface AddressChunker#(type tAddr, type tLen, type tChunkAlignLog);
    interface PipeInB0#(AddressChunkReq#(tAddr, tLen, tChunkAlignLog)) requestPipeIn;
    interface PipeOut#(AddressChunkResp#(tAddr, tLen)) responsePipeOut;
endinterface
```

**示例**:
```
输入: startAddr = 0x1000, len = 8192, chunk = 12 (4096 字节)
输出:
  块 1: startAddr = 0x1000, len = 4096, isFirst=True,  isLast=False
  块 2: startAddr = 0x2000, len = 4096, isFirst=False, isLast=True
```

**详细原理**: 参见 [PacketGen分包机制详解.md](./PacketGen分包机制详解.md) 第 2 节

### 5.2 StreamShifter

**位置**: `/home/peng/projects/rdma_all/open-rdma-rtl/src/PacketGenAndParse.bsv:456`

**作用**: 调整数据流的对齐方式，从本地地址对齐转换为远程地址对齐

**接口**:
```bsv
interface StreamShifterG#(type tData);
    interface PipeInB0#(DataBusSignedByteShiftOffset) offsetPipeIn;  // 偏移量输入
    interface PipeIn#(DataStream) streamPipeIn;                      // 数据流输入
    interface PipeOut#(DataStream) streamPipeOut;                    // 数据流输出
endinterface
```

**工作原理**:
- 根据 `localAddrOffset - remoteAddrOffset` 调整数据流
- 使用移位寄存器缓存数据
- 支持左移和右移

**示例**:
```
偏移量: +1 字节 (右移)
输入:  [D3 D2 D1 D0] [D7 D6 D5 D4] [-- -- -- D8]
输出:  [D2 D1 D0 --] [D6 D5 D4 D3] [-- -- D8 D7]
```

### 5.3 DtldStreamSplitor

**位置**: `/home/peng/projects/rdma_all/open-rdma-rtl/src/PacketGenAndParse.bsv:459`

**作用**: 将长数据流拆分成多个按 PMTU 边界的短数据流

**接口**:
```bsv
interface DtldStreamSplitor#(type tData, type tAlignBlockCnt, numeric type szAlignBlockSizeLog);
    interface PipeInB0#(tAlignBlockCnt) streamAlignBlockCountPipeIn;  // 块数输入
    interface PipeIn#(DataStream) dataPipeIn;                         // 数据流输入
    interface PipeOut#(DataStream) dataPipeOut;                       // 数据流输出
endinterface
```

**工作原理**:
- 接收 AlignBlock 数量（每个数量对应一个子流）
- 按照指定的 AlignBlock 数量拆分数据流
- 为每个子流设置正确的 isFirst/isLast 标志

**详细原理**: 参见 [PacketGen分包机制详解.md](./PacketGen分包机制详解.md) 第 4 节

## 6. 数据流处理示例

假设一个 RDMA WRITE 请求:
- 本地地址: `laddr = 0x1002` (偏移 2)
- 远程地址: `raddr = 0x2001` (偏移 1)
- 数据长度: `len = 8192`
- PMTU: `IBV_MTU_4096` (4096 字节)

### 6.1 数据流处理流程

```
                                Stage 1          Stage 2          Stage 3          Stage 4          Stage 5
                             queryMrTable    chunkAddr      genHeader1       genHeader2       forwardStream
                                  ↓              ↓              ↓              ↓              ↓
输入 WQE                        查询 MR         拆分地址        确定包边界      生成协议头        转发数据流
len=8192                         ↓              ↓              ↓              ↓              ↓
pmtu=4096                      MR Entry        块 1:          包 1:          BTH+RETH         流 1:
laddr=0x1002                    baseVA         0x2001         isFirst=T      OpCode=FIRST     4095 字节
raddr=0x2001                    pgtOffset      len=4095       isLast=F       PSN=100          isFirst=T
                                  ↓            isFirst=T      dlen=8192      raddr=0x2001     isLast=F
                                发起 DMA        isLast=F       PSN=100        rkey=...            ↓
                                读取请求          ↓              ↓              ↓              输出包 1
                                  ↓            块 2:          包 2:          BTH
                                读取数据        0x2FFF+1       isFirst=F      OpCode=LAST      流 2:
                                  ↓            len=4097       isLast=T       PSN=101          4097 字节
                                  ↓            isFirst=F      dlen=4097         ↓            isFirst=F
                                  ↓            isLast=T       PSN=101        输出协议头       isLast=T
                                  ↓              ↓              ↓                                ↓
                               StreamShifter  计算 PSN       计算 UDP 长度                    输出包 2
                               (对齐转换)    递增 PSN
                                offset=+1
                                  ↓
                              DtldStreamSplitor
                              (数据流拆分)
                              块数=[1019, 1025]
                                  ↓
                              输出两个数据流
```

### 6.2 第一个包 (RDMA_WRITE_FIRST)

**协议头**:
```
MAC/IP/UDP 元数据:
  dstMacAddr: 从 WQE.macAddr
  dstIpAddr:  从 WQE.dqpIP
  srcPort:    从 WQE.sqpn
  dstPort:    4791 (RDMA)
  udpPayloadLen: 12 (BTH) + 16 (RETH) + 1 (填充) + 4095 (数据) = 4124

RDMA 包元数据:
  BTH:
    opCode:   RDMA_WRITE_FIRST
    psn:      100 (从 WQE.psn)
    destQPN:  从 WQE.dqpn
    padCnt:   1 (远程地址偏移 = 1)
  RETH:
    va:       0x2001 (远程地址)
    rkey:     从 WQE.rkey
    dlen:     8192 (总长度)

有效载荷:
  长度: 4095 字节 (4096 - 1 填充)
  对齐: 按远程地址 0x2001 对齐
  填充: 1 字节在数据前
```

**为什么是 4095 字节**?
- PMTU = 4096 字节
- 远程地址偏移 = 1 字节
- 需要填充 1 字节使数据按远程地址对齐
- 实际数据 = 4096 - 1 = 4095 字节

### 6.3 第二个包 (RDMA_WRITE_LAST)

**协议头**:
```
MAC/IP/UDP 元数据:
  udpPayloadLen: 12 (BTH) + 0 (无扩展头) + 0 (无填充) + 4097 (数据) = 4109

RDMA 包元数据:
  BTH:
    opCode:   RDMA_WRITE_LAST
    psn:      101 (递增)
    destQPN:  从 WQE.dqpn
    padCnt:   0
  (无扩展头)

有效载荷:
  长度: 4097 字节 (8192 - 4095)
  对齐: 按远程地址 0x2FFF+1 = 0x3000 对齐 (无需填充)
```

## 7. 三路输出的同步机制

PacketGen 的三路输出**必须严格同步**:

```bsv
// Stage 4 同时输出三路数据
rdmaPacketMetaPipeOutQueue.enq(rdmaPacketMeta);      // 输出 1
macIpUdpMetaPipeOutQueue.enq(macIpUdpMeta);          // 输出 2
// 输出 3 在 Stage 5
perPacketPayloadDataStreamQ.enq(ds);
```

**同步保证**:
1. **流水线顺序**: 所有请求按照相同的流水线顺序处理
2. **FIFO 队列**: 使用 FIFOF 保证先进先出
3. **一对一映射**: 每个 RDMA 包对应唯一的三元组 (macIpUdpMeta, rdmaPacketMeta, payloadStream)

**下游模块（EthernetPacketGenerator）的处理**:
```bsv
rule genFirstBeat;
    let macIpUdpMeta = macIpUdpMetaPipeIn.first;
    macIpUdpMetaPipeIn.deq;

    let rdmaPacketMeta = rdmaPacketMetaPipeIn.first;
    rdmaPacketMetaPipeIn.deq;

    // 组装以太网包头 (32 字节)
    // 从 macIpUdpMeta 生成 MAC/IP/UDP 头
    // 从 rdmaPacketMeta 生成 BTH 和扩展头
    ...
endrule

rule genPayload if (rdmaPacketMeta.hasPayload);
    let ds = rdmaPayloadPipeIn.first;
    rdmaPayloadPipeIn.deq;
    // 输出载荷数据
    ...
endrule
```

## 8. 关键设计要点

### 8.1 地址对齐处理

**RDMA 规范要求**: 有效载荷必须按照**远程地址**对齐

**实现方式**:
1. StreamShifter 根据地址偏移调整数据流
2. 计算填充字节数: `padCnt = remoteAddr & 0x3`
3. EthernetPacketGenerator 在数据前插入填充字节

### 8.2 PSN 管理

**PSN (Packet Sequence Number)** 是 RDMA 可靠传输的核心:

```bsv
Reg#(PSN) psnReg <- mkRegU;  // PSN 寄存器

// 第一个包使用 WQE 的 PSN
if (packetInfo.isFirst) begin
    psn = wqe.psn;
    psnReg <= psn + 1;
end
// 后续包递增 PSN
else begin
    psn = psnReg;
    psnReg <= psn + 1;
end
```

**PSN 特点**:
- 24 位计数器，循环使用
- 每个 RDMA 包递增 1
- 接收方用于检测丢包和乱序

### 8.3 OpCode 生成

根据 WQE.opcode 和 isFirst/isLast 组合生成 RDMA OpCode:

```bsv
function Maybe#(RdmaOpCode) genRdmaOpCode(WorkReqOpCode wrOpCode, Bool isFirst, Bool isLast);
    return case ({pack(isFirst), pack(isLast)})
        'b11:   // ONLY
                case (wrOpCode)
                    IBV_WR_RDMA_WRITE:          tagged Valid RDMA_WRITE_ONLY;
                    IBV_WR_SEND:                tagged Valid SEND_ONLY;
                    ...
                endcase
        'b10:   // FIRST
                case (wrOpCode)
                    IBV_WR_RDMA_WRITE:          tagged Valid RDMA_WRITE_FIRST;
                    IBV_WR_SEND:                tagged Valid SEND_FIRST;
                    ...
                endcase
        'b00:   // MIDDLE
                case (wrOpCode)
                    IBV_WR_RDMA_WRITE:          tagged Valid RDMA_WRITE_MIDDLE;
                    IBV_WR_SEND:                tagged Valid SEND_MIDDLE;
                    ...
                endcase
        'b01:   // LAST
                case (wrOpCode)
                    IBV_WR_RDMA_WRITE:          tagged Valid RDMA_WRITE_LAST;
                    IBV_WR_SEND:                tagged Valid SEND_LAST;
                    ...
                endcase
    endcase;
endfunction
```

### 8.4 流水线深度配置

```bsv
// 流水线队列深度
FIFOF#(SendChunkByRemoteAddrReqAndPayloadGenReqPipelineEntry)
    sendChunkByRemoteAddrReqAndPayloadGenReqPipelineQ <- mkSizedFIFOF(5);

FIFOF#(GenPacketHeaderStep1PipelineEntry)
    genPacketHeaderStep1PipelineQ <- mkSizedFIFOF(4);

FIFOF#(GenPacketHeaderStep2PipelineEntry)
    genPacketHeaderStep2PipelineQ <- mkLFIFOF;  // 深度 2

// 输出队列深度（重要！）
FIFOF#(ThinMacIpUdpMetaDataForSend)
    macIpUdpMetaPipeOutQueue <- mkSizedFIFOF(256);  // 足够容纳 PCIe 读延迟

FIFOF#(RdmaSendPacketMeta)
    rdmaPacketMetaPipeOutQueue <- mkSizedFIFOF(256);

FIFOF#(DataStream)
    perPacketPayloadDataStreamQ <- mkSizedFIFOF(8);  // 匹配以太网包生成器延迟
```

**设计考虑**:
- 输出队列深度必须足够大，以容纳 PCIe DMA 读延迟 (256 个包)
- 数据流队列深度匹配以太网包生成器的头部处理延迟 (8 beats)
- 保证流水线持续满载，达到最大吞吐量

## 9. 性能分析

### 9.1 吞吐量

理论最大吞吐量:
- 数据总线宽度: 256 bits = 32 bytes
- 时钟频率: 250 MHz
- **理论带宽**: 32 × 250 = 8 GB/s = 64 Gbps

实际吞吐量受限于:
1. PCIe DMA 读取延迟
2. MR Table 查询延迟
3. 流水线效率

### 9.2 延迟

从 WQE 输入到第一个包输出:
- Stage 1 (queryMrTable): 1 cycle (发起查询)
- MR Table 查询延迟: ~10 cycles
- Stage 2 (sendChunkByRemoteAddrReq): 1 cycle
- Stage 3 (genPacketHeaderStep1): 1 cycle
- Stage 4 (genPacketHeaderStep2): 1 cycle
- Stage 5 (forwardSplitStream): 1 cycle
- **总延迟**: ~15 cycles = 60 ns @ 250 MHz

### 9.3 资源占用

主要资源:
- **BRAM**: MR Table 查询缓存
- **寄存器**: 流水线寄存器、PSN 寄存器
- **FIFO**: 各级流水线队列
- **逻辑资源**: AddressChunker、StreamShifter、DtldStreamSplitor

## 10. 与其他模块的交互

```
                    ┌─────────────────┐
                    │  WorkQueueDesc  │
                    │     Parser      │
                    └────────┬────────┘
                             │ WorkQueueElem
                             ↓
                    ┌─────────────────┐
                    │   PacketGen     │ ← 本模块
                    │   (5 stages)    │
                    └─┬───┬─────────┬─┘
                      │   │         │
         ┌────────────┘   │         └──────────────┐
         │                │                        │
         │ macIpUdpMeta   │ rdmaPacketMeta        │ rdmaPayload
         ↓                ↓                        ↓
    ┌─────────────────────────────────────────────────┐
    │        PacketGenReqArbiter (仲裁器)             │
    └────────────────────┬────────────────────────────┘
                         │
                         ↓
    ┌─────────────────────────────────────────────────┐
    │        EthernetPacketGenerator                  │
    │        (组装以太网帧)                           │
    └────────────────────┬────────────────────────────┘
                         │ IoChannelEthDataStream
                         ↓
                   以太网接口
```

**模块间数据流**:

1. **WorkQueueDescParser → PacketGen**:
   - 数据: WorkQueueElem
   - 方向: 单向
   - 协议: PipeInB0 (无反压)

2. **PacketGen → MR Table**:
   - 数据: MrTableQueryReq / MemRegionTableEntry
   - 方向: 双向
   - 协议: ClientP (请求/响应)

3. **PacketGen → PayloadGenAndCon**:
   - 数据: PayloadGenReq / DataStream
   - 方向: 双向
   - 协议: PipeOut/PipeIn

4. **PacketGen → PacketGenReqArbiter**:
   - 数据: 三路并行流
   - 方向: 单向
   - 协议: PipeOut

5. **PacketGenReqArbiter → EthernetPacketGenerator**:
   - 数据: 三路并行流（仲裁后）
   - 方向: 单向
   - 协议: PipeOut/PipeIn

**仲裁器的作用**:
- 合并多个源的包生成请求:
  - 源 0: 正常的 SQ 发送请求
  - 源 1: CNP (拥塞通知包)
  - 源 2: Auto ACK

## 11. 总结

PacketGen 模块是 RDMA 发送路径的核心，其主要作用包括:

1. **协议头生成**: 生成 MAC/IP/UDP 和 RDMA 协议头
2. **数据包分片**: 根据 PMTU 将大消息拆分成多个小包
3. **地址对齐**: 调整数据流以满足远程地址对齐要求
4. **PSN 管理**: 为每个包分配递增的 PSN
5. **OpCode 生成**: 根据包的位置生成正确的 RDMA OpCode
6. **数据流处理**: 协调 DMA 读取和数据流拆分

**关键特性**:
- **5 级流水线**: 实现高吞吐量
- **三路并行输出**: 协议元数据和有效载荷分离
- **严格同步**: 保证三路输出的一致性
- **模块化设计**: AddressChunker、StreamShifter、DtldStreamSplitor 可复用

**相关文档**:
- [PacketGen分包机制详解.md](./PacketGen分包机制详解.md) - 详细解释分包逻辑
- [EthernetPacketGenerator详解.md](./EthernetPacketGenerator详解.md) - 下游模块介绍
