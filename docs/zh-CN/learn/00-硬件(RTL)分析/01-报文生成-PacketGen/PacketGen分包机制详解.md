# PacketGen 分包机制详解

## 概述

PacketGen 模块负责将 RDMA 工作队列元素 (WQE) 转换为符合 PMTU (Path MTU) 限制的多个网络数据包。本文详细解析其分包流程和关键机制。

## 分包流程概览

```
WQE → 查询MR表 → 地址分块 → 生成包头 → 数据流分割 → [PacketGen输出] → 仲裁器 → 以太网帧组装 → 完整帧输出
```

**注意**: PacketGen 只负责 RDMA 层面的分包，输出的是 RDMA 包元数据和 payload。最终的以太网帧组装由 `EthernetPacketGenerator` 模块完成（详见 [EthernetPacketGenerator详解.md](./EthernetPacketGenerator详解.md)）。

## 关键组件

### 1. AddressChunker (地址分块器)

**位置**: `/home/peng/projects/rdma_all/open-rdma-rtl/src/AddressChunker.bsv`

**功能**: 将一个连续的地址范围按照指定的块大小（PMTU）分割成多个块。

#### 输入结构

```bsv
typedef struct {
    tAddr startAddr;      // 起始地址
    tLen len;             // 总长度
    tChunkAlignLog chunk; // 块大小的对数值 (例如 PMTU)
} AddressChunkReq
```

#### 输出结构

```bsv
typedef struct {
    tAddr startAddr;  // 当前块的起始地址
    tLen len;         // 当前块的长度
    Bool isFirst;     // 是否是第一个块
    Bool isLast;      // 是否是最后一个块
} AddressChunkResp
```

#### 工作原理

1. **预计算阶段** (preCalc 规则, 68-87行):
   ```bsv
   // 计算块大小
   tLen chunkSize = unpack(1 << chunkReq.chunk);

   // 计算需要分成多少个块
   zeroBasedChunkCnt =
       ((startAddr + len - 1) >> chunk) - (startAddr >> chunk);

   // 计算第一个块中无效的字节数（地址对齐）
   nonValidByteCntInFirstChunk = startAddr & (chunkSize - 1);
   ```

2. **第一个块处理** (doFirstBeat 规则, 89-130行):
   - 第一个块的长度 = `chunkSize - nonValidByteCntInFirstChunk`
   - 特殊情况：如果总长度小于一个块，则 `isFirst = True, isLast = True`

3. **后续块处理** (doOtherBeat 规则, 132-163行):
   - 中间块的长度 = `chunkSize`
   - 最后一个块的长度 = 剩余长度

#### 示例

假设从日志中的参数：
- `raddr = 0x0000760bf5600000`
- `len = 0x00002000` (8192 bytes)
- `pmtu = IBV_MTU_4096` (4096 bytes)

分块结果：
```
Chunk 1: startAddr=0x0000760bf5600000, len=4096, isFirst=True,  isLast=False
Chunk 2: startAddr=0x0000760bf5601000, len=4096, isFirst=False, isLast=True
```

### 2. StreamShifter (数据流移位器)

**位置**: `/home/peng/projects/rdma_all/open-rdma-rtl/src/StreamShifterG.bsv`

**功能**: 对齐本地地址和远程地址的数据流。

在 PacketGen 的 `sendChunkByRemoteAddrReqAndPayloadGenReq` 规则中 (560-563行):

```bsv
ByteIdxInDword localAddrOffset = truncate(wqe.laddr);
ByteIdxInDword remoteAddrOffset = truncate(wqe.raddr);
DataBusSignedByteShiftOffset localToRemoteAlignShiftOffset =
    zeroExtend(localAddrOffset) - zeroExtend(remoteAddrOffset);
payloadStreamShifterOffsetPipeInConverter.enq(localToRemoteAlignShiftOffset);
```

**作用**:
- 本地地址：`0x000073bb33000000`（日志中的 laddr）
- 远程地址：`0x0000760bf5600000`（日志中的 raddr）
- 两者的低位偏移可能不同，需要通过移位器对齐

### 3. DtldStreamSplitor (数据流分割器)

**位置**: `/home/peng/projects/rdma_all/open-rdma-rtl/src/DtldStream.bsv:868-1100`

**功能**: 将一个长的数据流按照对齐块计数分割成多个子流。

#### 核心概念

- **AlignBlockCntInPmtu**: 一个数据包中包含的对齐块数量
- **AlignBlock**: 对齐单位，通常是 `2^LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE` 字节

#### 计算对齐块数量 (genPacketHeaderStep2 规则, 719-723行)

```bsv
AlignBlockCntInPmtu alignBlockCntForStreamSplit = truncate(
    (truncatedStreamSplitEndAddrForAlignCalc >> LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE) -
    (truncatedStreamSplitStartAddr >> LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE)
) + 1;
```

这个值告诉 DtldStreamSplitor 当前数据包应该包含多少个对齐块的数据。

#### 工作原理

1. **状态机**:
   - `DtldStreamSplitorStateOutput`: 正常输出状态
   - `DtldStreamSplitorStateOutputLastBeat`: 输出最后一个 beat
   - `DtldStreamSplitorStateOutputLastStream`: 输出最后一个子流

2. **输出逻辑** (outputState 规则, 913-1039行):
   - 从输入流中读取数据
   - 根据 `alignBlockCntForStreamSplit` 决定输出多少数据
   - 使用位移和掩码操作重组数据
   - 设置输出流的 `isFirst` 和 `isLast` 标志

## PacketGen 分包流水线

### 阶段 1: queryMrTable (493-521行)

```bsv
rule queryMrTable;
    let wqe = wqePipeInQ.first;
    Bool hasPayload = workReqNeedPayloadGen(wqe.opcode) && !isZeroR(wqe.len);

    if (hasPayload) {
        // 查询内存区域表，获取页表信息
        mrTableQueryCltInst.putReq(MrTableQueryReq{
            idx: lkey2IndexMR(wqe.lkey)
        });
    }

    sendChunkByRemoteAddrReqAndPayloadGenReqPipelineQ.enq(...);
endrule
```

**关键点**:
- 判断是否需要 payload (`IBV_WR_SEND`, `IBV_WR_RDMA_WRITE` 等操作需要)
- 查询 MR 表获取虚拟地址到物理地址的映射信息

### 阶段 2: sendChunkByRemoteAddrReqAndPayloadGenReq (524-580行)

```bsv
rule sendChunkByRemoteAddrReqAndPayloadGenReq;
    if (hasPayload) {
        // 1. 向 AddressChunker 发送分块请求
        let remoteAddrChunkReq = AddressChunkReq{
            startAddr: wqe.raddr,
            len: wqe.len,
            chunk: getPmtuSizeByPmtuEnum(wqe.pmtu)  // 关键：按 PMTU 大小分块
        };
        wqeToPacketChunkerRequestPipeInAdapter.enq(remoteAddrChunkReq);

        // 2. 生成 payload 请求
        let payloadGenReq = PayloadGenReq{
            addr: wqe.laddr,
            len: wqe.len,
            baseVA: mrTable.baseVA,
            pgtOffset: mrTable.pgtOffset
        };
        genReqPipeOutQ.enq(payloadGenReq);

        // 3. 计算并设置数据流移位偏移
        DataBusSignedByteShiftOffset localToRemoteAlignShiftOffset =
            zeroExtend(localAddrOffset) - zeroExtend(remoteAddrOffset);
        payloadStreamShifterOffsetPipeInConverter.enq(localToRemoteAlignShiftOffset);
    }
endrule
```

**关键点**:
- **分块请求**: 这是分包的核心步骤，将整个传输分成 PMTU 大小的块
- **Payload 生成**: 从本地内存读取数据
- **地址对齐**: 计算本地和远程地址的偏移差

### 阶段 3: genPacketHeaderStep1 (582-676行)

```bsv
rule genPacketHeaderStep1;
    Bool isFirstPacket = wqe.isFirst;
    Bool isLastPacket  = wqe.isLast;

    if (hasPayload) {
        // 从 AddressChunker 获取分块信息
        packetInfo = wqeToPacketChunker.responsePipeOut.first;
        wqeToPacketChunker.responsePipeOut.deq;

        // 更新 isFirst/isLast 标志
        isFirstPacket = isFirstPacket && packetInfo.isFirst;
        isLastPacket = isLastPacket && packetInfo.isLast;

        // PSN（包序列号）管理
        if (packetInfo.isFirst) {
            psn = wqe.psn;
            psnReg <= psn + 1;
        } else {
            psnReg <= psn + 1;
        }

        remoteAddr = packetInfo.startAddr;
        dlen = isFirstPacket ? wqe.totalLen : packetInfo.len;
    }

    // 只有当处理完所有块后才出队
    if ((hasPayload && packetInfo.isLast) || !hasPayload) {
        genPacketHeaderStep1PipelineQ.deq;
    }
endrule
```

**关键点**:
- **PSN 递增**: 每个数据包都有唯一的 PSN，从 WQE 的 PSN 开始递增
- **循环处理**: 一个 WQE 会在这个规则中循环多次，每次处理一个数据包
- **dlen 设置**:
  - 第一个包：`dlen = totalLen`（RETH 需要总长度）
  - 后续包：`dlen = packetInfo.len`

### 阶段 4: genPacketHeaderStep2 (678-751行)

```bsv
rule genPacketHeaderStep2;
    // 1. 生成 RDMA BTH 头
    let bthMaybe <- genRdmaBTH(
        wqe, isFirstPacket, isLastPacket,
        solicited, psn, padCnt, ackReq,
        remoteAddr, dlen
    );

    // 2. 生成 RDMA 扩展头 (RETH, ImmDt 等)
    let extendHeaderBufferMaybe <- genRdmaExtendHeader(
        wqe, isFirstPacket, isLastPacket,
        remoteAddr, dlen
    );

    // 3. 生成 MAC/IP/UDP 元数据
    let macIpUdpMeta = ThinMacIpUdpMetaDataForSend{
        dstMacAddr: wqe.macAddr,
        dstIpAddr: wqe.dqpIP,
        srcPort: truncate(wqe.sqpn),
        dstPort: UDP_PORT_RDMA,
        udpPayloadLen: udpPayloadLen,
        ...
    };

    // 4. 计算数据流分割所需的对齐块数量
    if (hasPayload) {
        AlignBlockCntInPmtu alignBlockCntForStreamSplit = truncate(
            (truncatedStreamSplitEndAddrForAlignCalc >> LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE) -
            (truncatedStreamSplitStartAddr >> LOG_OF_DATA_STREAM_ALIGN_BLOCK_SIZE)
        ) + 1;
        payloadSplitorStreamAlignBlockCountPipeInConverter.enq(alignBlockCntForStreamSplit);
    }
endrule
```

**关键点**:
- **OpCode 映射**: 根据 `isFirst` 和 `isLast` 生成正确的 RDMA OpCode
  - `isFirst=True, isLast=True`: `SEND_ONLY`
  - `isFirst=True, isLast=False`: `SEND_FIRST`
  - `isFirst=False, isLast=False`: `SEND_MIDDLE`
  - `isFirst=False, isLast=True`: `SEND_LAST`

### 阶段 5: forwardSplitStream (753-766行)

```bsv
rule forwardSplitStream;
    // DtldStreamSplitor 根据每个包的对齐块数量分割数据流
    let ds = payloadSplitor.dataPipeOut.first;
    payloadSplitor.dataPipeOut.deq;
    perPacketPayloadDataStreamQ.enq(ds);
endrule
```

**关键点**:
- `payloadSplitor` 自动根据之前 enqueue 的 `alignBlockCntForStreamSplit` 分割数据流
- 输出的每个子流对应一个数据包的 payload

## OpCode 生成逻辑

在 `genRdmaOpCode` 函数 (72-117行) 中，根据以下规则生成 RDMA OpCode：

```bsv
{isFirst, isLast}:
'b00 (MIDDLE):   IBV_WR_SEND → SEND_MIDDLE
'b01 (LAST):     IBV_WR_SEND → SEND_LAST
'b10 (FIRST):    IBV_WR_SEND → SEND_FIRST
'b11 (ONLY):     IBV_WR_SEND → SEND_ONLY
```

## 扩展头生成

在 `genRdmaExtendHeader` 函数 (253-387行) 中：

### SEND 操作的扩展头

- **RC/UC QP**:
  - 所有包都包含 RETH
  - 最后一个包如果有 immediate，还包含 ImmDt

- **UD QP**:
  - 包含 DETH
  - 如果有 immediate，还包含 ImmDt

### RDMA_WRITE 操作的扩展头

- 所有包都包含 RETH（第一个包中的 RETH 包含完整的 `raddr`, `rkey`, `totalLen`）
- 最后一个包如果是 `RDMA_WRITE_WITH_IMM`，还包含 ImmDt

## 实际分包示例

基于日志中的 WQE：

```
opcode: IBV_WR_SEND
qpType: IBV_QPT_RC
psn: 0x000000
pmtu: IBV_MTU_4096
laddr: 0x000073bb33000000
lkey: 0x001fffae
raddr: 0x0000760bf5600000
rkey: 0x001fffb2
len: 0x00002000 (8192 bytes)
totalLen: 0x00002000
```

### 分包结果

**Packet 1**:
- OpCode: `SEND_FIRST`
- PSN: `0x000000`
- RETH: `va=0x0000760bf5600000, rkey=0x001fffb2, dlen=0x00002000`
- Payload: 4096 bytes (从 laddr 0x000073bb33000000 开始)

**Packet 2**:
- OpCode: `SEND_LAST` (因为 WQE 的 flags 包含 `IBV_SEND_SIGNALED`)
- PSN: `0x000001`
- RETH: `va=0x0000760bf5601000, rkey=0x001fffb2, dlen=4096`
- Payload: 4096 bytes (从 laddr 0x000073bb33001000 开始)

## 关键设计要点

### 1. 流水线设计

PacketGen 使用多级流水线，每个阶段都有队列缓冲：
- `sendChunkByRemoteAddrReqAndPayloadGenReqPipelineQ` (深度 5)
- `genPacketHeaderStep1PipelineQ` (深度 4)
- `genPacketHeaderStep2PipelineQ` (LFIFO)
- `perPacketPayloadDataStreamQ` (深度 8)

### 2. 完全流水线检查

代码中多处使用 `checkFullyPipeline` 来确保流水线不会停滞：

```bsv
checkFullyPipeline(wqe.fpDebugTime, 11, 2000,
    DebugConf{name: "mkPacketGen sendChunkByRemoteAddrReqAndPayloadGenReq",
              enableDebug: True});
```

### 3. 地址对齐

- RDMA 要求 payload 按照远程地址对齐
- 使用 `StreamShifter` 处理本地和远程地址的偏移差
- `padCnt` 在这个设计中总是 0，因为 payload 已经对齐

### 4. 一对多处理

一个 WQE 可能生成多个数据包：
- `genPacketHeaderStep1` 规则中，只有当 `packetInfo.isLast` 为真时才 dequeue WQE
- 在此之前，WQE 会一直保留在队列头部，循环处理每个分块

## 性能优化

### 1. 预计算

AddressChunker 的 `preCalc` 规则预先计算分块信息，减少关键路径延迟。

### 2. 队列深度

关键路径上的队列深度经过精心设计：
- `macIpUdpMetaPipeOutQueue`: 256（容纳 PCIe 读延迟）
- `rdmaPacketMetaPipeOutQueue`: 256（容纳 PCIe 读延迟）

### 3. 并行处理

多个 WQE 可以同时在不同流水线阶段处理，提高吞吐量。

## 总结

PacketGen 的分包机制是一个高度流水线化的设计：

1. **AddressChunker** 按 PMTU 分割地址范围
2. **StreamShifter** 对齐本地和远程地址
3. **多级流水线** 生成每个包的头部信息
4. **DtldStreamSplitor** 将数据流分割成对应的子流

整个设计确保了：
- 正确的包序列号（PSN）递增
- 正确的 OpCode（FIRST/MIDDLE/LAST/ONLY）
- 正确的扩展头（RETH, ImmDt 等）
- 符合 PMTU 限制的包大小

这个机制支持所有 RDMA 操作类型，并能处理各种 QP 类型（RC, UC, UD, XRC）。
