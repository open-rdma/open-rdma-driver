# EthernetPacketGenerator 模块详解 (512位数据总线版本)

## 概述

`mkEthernetPacketGenerator` 是 RDMA 数据包发送流程中的**最后一个阶段**，它将 RDMA 元数据（MAC/IP/UDP/BTH 头部）和 payload 数据组装成完整的以太网帧，并输出到以太网接口。

位置：`open-rdma-rtl/src/EthernetFrameIO512.bsv:709-1071`

## 在整体流程中的位置

```
WQE → PacketGen (分包) → PacketGenReqArbiter (仲裁)
      → EthernetPacketGenerator (组装) → 以太网帧输出 → MAC 层
```

---

## 模块接口定义

### 接口类型（638-650 行）

```bsv
interface EthernetPacketGenerator;
    interface PipeInB0#(ThinMacIpUdpMetaDataForSend) macIpUdpMetaPipeIn;
    interface PipeInB0#(RdmaSendPacketMeta) rdmaPacketMetaPipeIn;
    interface PipeInB0#(DataStream) rdmaPayloadPipeIn;
    interface PipeOut#(IoChannelEthDataStream) ethernetPacketPipeOut;
    method Action setLocalNetworkSettings(LocalNetworkSettings networkSettings);
endinterface
```

### 三个输入接口

1. **macIpUdpMetaPipeIn**: 接收 MAC/IP/UDP 元数据
   ```bsv
   typedef struct {
       EthMacAddr dstMacAddr;     // 目标 MAC 地址
       IpAddr dstIpAddr;          // 目标 IP 地址
       UdpPort srcPort;           // 源端口 (通常是 QPN)
       UdpPort dstPort;           // 目标端口 (4791 RDMA over UDP)
       UdpLength udpPayloadLen;   // UDP payload 长度
       EthType ethType;           // 以太网类型 (0x0800 for IPv4)
   } ThinMacIpUdpMetaDataForSend;
   ```

2. **rdmaPacketMetaPipeIn**: 接收 RDMA 包元数据
   ```bsv
   typedef struct {
       RdmaBthAndExtendHeader header;  // BTH + 扩展头 (RETH, AETH, ImmDt 等)
       Bool hasPayload;                // 是否有 payload 数据
   } RdmaSendPacketMeta;
   ```

3. **rdmaPayloadPipeIn**: 接收 RDMA payload 数据流
   ```bsv
   typedef struct {
       DATA data;                 // 512-bit 数据
       BusByteCnt byteNum;        // 有效字节数
       BusByteIdx startByteIdx;   // 起始字节索引
       Bool isFirst;              // 是否是第一个 beat
       Bool isLast;               // 是否是最后一个 beat
   } DataStream;
   ```

### 一个输出接口

**ethernetPacketPipeOut**: 输出完整的以太网帧数据流
```bsv
typedef struct {
    DATA data;                 // 512-bit 数据
    BusByteCnt byteNum;        // 有效字节数
    BusByteIdx startByteIdx;   // 起始字节索引 (始终为 0)
    Bool isFirst;              // 是否是第一个 beat
    Bool isLast;               // 是否是最后一个 beat
} IoChannelEthDataStream;
```

---

## 模块内部结构（709-738 行）

### 模块声明

```bsv
(*synthesize*)
module mkEthernetPacketGenerator(EthernetPacketGenerator);
```

`(*synthesize*)` 属性表示这个模块会被独立综合成一个硬件实体。

### 关键内部组件

```bsv
// 1. 输入队列适配器
PipeInAdapterB0#(ThinMacIpUdpMetaDataForSend) macIpUdpMetaPipeInQ <- mkPipeInAdapterB0;
PipeInAdapterB0#(RdmaSendPacketMeta) rdmaPacketMetaPipeInQ <- mkPipeInAdapterB0;
PipeInAdapterB0#(DataStream) rdmaPayloadPipeInQ <- mkPipeInAdapterB0;

// 2. 输出队列（带断言，用于调试）
FIFOF#(IoChannelEthDataStream) ethernetPacketPipeOutQ <-
    mkFIFOFWithFullAssert(DebugConf{
        name: "mkEthernetPacketGenerator ethernetPacketPipeOutQ",
        enableDebug: True
    });

// 3. IP 校验和计算相关
FIFOF#(IpHeader) ipHeaderForChecksumCalcQ <- mkFIFOF;
FIFOF#(IpHeaderChecksumCalcPipelineEntry) ipHeaderChecksumCalcPipelineQ <- mkSizedFIFOF(3);

// 4. 流水线寄存器
Reg#(PacketGeneratorFirstBeatToSecondBeatPipelineEntry) firstBeatToSecondBeatPipelineReg <- mkRegU;
Reg#(PacketGeneratorSecondBeatToMoreBeatPipelineEntry) secondBeatToMoreBeatPipelineReg <- mkRegU;

// 5. 网络配置
Reg#(Maybe#(LocalNetworkSettings)) networkSettingsReg <- mkReg(tagged Invalid);

// 6. 以太网帧剩余字节计数器
Reg#(RdmaEthernetFrameByteLen) ethernetFrameLeftByteCounterReg <- mkRegU;

// 7. 状态机
Reg#(EthernetPacketGeneratorState) statusReg <- mkReg(EthernetPacketGeneratorStateGenFirstBeat);

// 8. 前一个 beat 的数据寄存器
Reg#(DataStream) prevBeatReg <- mkRegU;

// 9. IP 校验和计算流水线模块
let ipHdrCheckSumStreamPipeOut <- mkIpHdrCheckSumStream(toPipeOut(ipHeaderForChecksumCalcQ));

// 10. 流水线检查器
let fpChecker <- mkStreamFullyPipelineChecker(
    DebugConf{name: "mkEthernetPacketGenerator", enableDebug: True}
);
```

---

## 状态机设计（652-657 行）

```bsv
typedef enum {
    EthernetPacketGeneratorStateGenFirstBeat  = 0,  // 生成第 1 个 beat
    EthernetPacketGeneratorStateGenSecondBeat = 1,  // 生成第 2 个 beat
    EthernetPacketGeneratorStateGenMoreBeat   = 2,  // 生成后续 beats
    EthernetPacketGeneratorStateGenExtraLastBeat = 3 // 生成额外的最后一个 beat
} EthernetPacketGeneratorState deriving(Bits, FShow, Eq);
```

### 状态转换图

```
          ┌─────────────────────────────────────┐
          │  GenFirstBeat (初始状态)            │
          └──────────────┬──────────────────────┘
                         │ 生成 beat 0
                         │ (MAC/IP/UDP 头 + BTH 部分)
                         ↓
          ┌─────────────────────────────────────┐
          │  GenSecondBeat                       │
          └──────────────┬──────────────────────┘
                         │
                         ├─→ hasPayload=False && isLast ──→ GenFirstBeat (下一个包)
                         │
                         ├─→ hasPayload=True && isLast ──→ GenFirstBeat (下一个包)
                         │
                         ├─→ hasPayload=True && !isLast && payload.isLast ──→ GenExtraLastBeat
                         │
                         └─→ hasPayload=True && !isLast && !payload.isLast ──→ GenMoreBeat
                                                 ↓
                         ┌─────────────────────────────────────┐
                         │  GenMoreBeat                         │
                         └──────────────┬──────────────────────┘
                                        │
                                        ├─→ isLast ──→ GenFirstBeat (下一个包)
                                        │
                                        ├─→ !isLast && payload.isLast ──→ GenExtraLastBeat
                                        │
                                        └─→ !isLast && !payload.isLast ──→ GenMoreBeat (循环)
                                                        ↓
                         ┌─────────────────────────────────────┐
                         │  GenExtraLastBeat                    │
                         └──────────────┬──────────────────────┘
                                        │
                                        └─→ 总是 ──→ GenFirstBeat (下一个包)
```

---

## 数据总线宽度说明

本模块使用 **512-bit (64 bytes)** 数据总线：
- `DATA_BUS_BYTE_WIDTH = 64`
- `DATA_BUS_HALF_BEAT_BYTE_WIDTH = 32`

这与文件名 `EthernetFrameIO512.bsv` 相符。

---

## 详细处理流程

### 阶段 0: prepareIpHeader (740-774 行)

**触发条件**: `macIpUdpMetaPipeInQ` 有数据

**功能**: 准备 IP 头部，并启动 IP 校验和的流水线计算

```bsv
rule prepareIpHeader;
    let curFpDebugTime <- getSimulationTime;

    // 1. 获取 MAC/IP/UDP 元数据
    let macIpUdpMeta = macIpUdpMetaPipeInQ.first;
    macIpUdpMetaPipeInQ.deq;

    // 2. 获取本地网络配置
    LocalNetworkSettings localNetSettings = fromMaybe(?, networkSettingsReg);

    // 3. 生成 UDP/IP 头部
    let udpIpHeader = genUdpIpHeader(macIpUdpMeta, localNetSettings, defaultIpId);

    // 4. 生成以太网头部
    let ethHeader = EthHeader {
        dstMacAddr: macIpUdpMeta.dstMacAddr,
        srcMacAddr: localNetSettings.macAddr,
        ethType: macIpUdpMeta.ethType  // 通常是 0x0800 (IPv4)
    };

    // 5. 计算以太网帧总长度
    // = UDP payload + (MAC + IP + UDP 头部总长度)
    RdmaEthernetFrameByteLen ethFrameLen =
        truncate(macIpUdpMeta.udpPayloadLen) +
        fromInteger(valueOf(MAC_IP_UDP_TOTAL_HDR_BYTE_WIDTH));

    // 6. 将 IP 头部发送到校验和计算模块
    ipHeaderForChecksumCalcQ.enq(udpIpHeader.ipHeader);

    // 7. 将完整的头部信息传递到流水线队列
    let outPipelineEntry = IpHeaderChecksumCalcPipelineEntry{
        macIpUdpHeader: MacIpUdpHeader{
            ethHeader: ethHeader,
            ipHeader: udpIpHeader.ipHeader,
            udpHeader: udpIpHeader.udpHeader
        },
        totalEthernetFrameLen: ethFrameLen
    };
    ipHeaderChecksumCalcPipelineQ.enq(outPipelineEntry);

    $display("time=%0t: mkEthernetPacketGenerator prepareIpHeader, "
             "udpPayloadLen=%0d, totalEthFrameLen=%0d",
             $time, macIpUdpMeta.udpPayloadLen, ethFrameLen);
endrule
```

**关键点**:
1. **IP 校验和计算**: 发送到独立的流水线模块 (`mkIpHdrCheckSumStream`)，不阻塞主流程
2. **以太网帧总长度**: 包括所有头部 + UDP payload（包含 BTH + 扩展头 + RDMA payload）
3. **流水线队列深度**: `ipHeaderChecksumCalcPipelineQ` 深度为 3，允许多个包同时处理

---

### 阶段 1: genFirstBeat (788-850 行)

**触发条件**: `statusReg == EthernetPacketGeneratorStateGenFirstBeat`

**功能**: 生成以太网帧的第 1 个 beat，包含 MAC/IP/UDP 头部 + BTH 头部

```bsv
rule genFirstBeat if (statusReg == EthernetPacketGeneratorStateGenFirstBeat);
    let curFpDebugTime <- getSimulationTime;

    // 1. 获取 IP 校验和计算结果（来自流水线）
    let checksum = ipHdrCheckSumStreamPipeOut.first;
    ipHdrCheckSumStreamPipeOut.deq;

    // 2. 获取 RDMA 包元数据
    let rdmaMeta = rdmaPacketMetaPipeInQ.first;
    rdmaPacketMetaPipeInQ.deq;

    // 3. 获取流水线传递的头部信息
    let pipelineEntry = ipHeaderChecksumCalcPipelineQ.first;
    ipHeaderChecksumCalcPipelineQ.deq;

    // 4. 填充 IP 校验和
    pipelineEntry.macIpUdpHeader.ipHeader.ipChecksum = checksum;
    let macIpUdpHeader = pipelineEntry.macIpUdpHeader;

    // 5. 组合所有头部数据
    // macIpUdpHeader (42 bytes) + BTH + 扩展头 (可能 12~28 bytes) = 96 bytes
    // 这样设计是为了确保正好是 3 个半 beat (3 * 32 = 96 bytes)
    Bit#(TMul#(3, DATA_BUS_HALF_BEAT_BIT_WIDTH)) macIpUdpBthEth =
        {pack(macIpUdpHeader), pack(rdmaMeta.header)};

    // 6. 取第一个 64 bytes (一个 beat)
    DATA data = truncateLSB(macIpUdpBthEth);

    // 7. 判断是否是最后一个 beat
    let isLast = pipelineEntry.totalEthernetFrameLen <= fromInteger(valueOf(DATA_BUS_BYTE_WIDTH));

    // 8. 生成输出 beat
    let outBeat = IoChannelEthDataStream{
        data: swapEndianByte(data),  // 字节序转换
        byteNum: fromInteger(valueOf(DATA_BUS_BYTE_WIDTH)),  // 64 bytes
        startByteIdx: 0,
        isFirst: True,
        isLast: isLast
    };

    // 9. 断言：发送包必须大于 1 个 beat（在 512 位总线上）
    immAssert(!isLast,
              "send packet must be more than 1 beat in 512 bits wide bus",
              ...);

    // 10. 输出到队列
    ethernetPacketPipeOutQ.enq(outBeat);

    // 11. 保存剩余的 32 bytes 到 prevBeatReg
    DataBeatHalf prevBeatHalfData = truncate(macIpUdpBthEth);
    prevBeatReg <= DataStream {
        data: zeroExtendLSB(prevBeatHalfData),
        // ... 其他字段不关心
    };

    // 12. 准备传递到下一阶段的信息
    let outPipelineEntry = PacketGeneratorFirstBeatToSecondBeatPipelineEntry {
        hasPayload: rdmaMeta.hasPayload,
        fpDebugTime: curFpDebugTime
    };
    firstBeatToSecondBeatPipelineReg <= outPipelineEntry;

    // 13. 更新剩余字节计数器
    ethernetFrameLeftByteCounterReg <=
        pipelineEntry.totalEthernetFrameLen - fromInteger(valueOf(DATA_BUS_BYTE_WIDTH));

    // 14. 状态转移
    statusReg <= EthernetPacketGeneratorStateGenSecondBeat;

    $display("time=%0t: genFirstBeat, totalFrameLen=%0d",
             $time, pipelineEntry.totalEthernetFrameLen);
endrule
```

**第 1 个 beat 的数据布局** (512-bit = 64 bytes):

```
Byte 0-13:    以太网头部 (14 bytes)
  ├─ dstMacAddr (6 bytes)
  ├─ srcMacAddr (6 bytes)
  └─ ethType (2 bytes)

Byte 14-33:   IP 头部 (20 bytes)
  ├─ version, IHL, DSCP, ECN
  ├─ totalLength
  ├─ identification
  ├─ flags, fragmentOffset
  ├─ TTL, protocol
  ├─ ipChecksum ← 在这个阶段填充
  ├─ srcIpAddr (4 bytes)
  └─ dstIpAddr (4 bytes)

Byte 34-41:   UDP 头部 (8 bytes)
  ├─ srcPort (2 bytes)
  ├─ dstPort (2 bytes)
  ├─ udpLength (2 bytes)
  └─ udpChecksum (2 bytes)

Byte 42-53:   BTH (12 bytes)
  ├─ opcode (1 byte)
  ├─ flags (1 byte)
  ├─ padCnt, tver (1 byte)
  ├─ dqpn (3 bytes)
  ├─ ackReq, reserved (1 byte)
  └─ psn (3 bytes)

Byte 54-63:   扩展头部的前 10 bytes
  └─ RETH/AETH/ImmDt 等（根据操作类型）
```

**关键操作**:
1. **swapEndianByte()**: 转换字节序，因为协议头部使用网络字节序（大端）
2. **96 bytes 的巧妙设计**: 所有头部正好 96 bytes = 1.5 个 beat
   - Beat 1: 前 64 bytes
   - prevBeatReg: 剩余 32 bytes，用于与下一个数据拼接
3. **断言检查**: 确保包大小合理

---

### 阶段 2: genSecondBeat (854-950 行)

**触发条件**: `statusReg == EthernetPacketGeneratorStateGenSecondBeat`

**功能**: 生成第 2 个 beat，包含剩余的头部 + payload 开始部分

```bsv
rule genSecondBeat if (statusReg == EthernetPacketGeneratorStateGenSecondBeat);
    let curFpDebugTime <- getSimulationTime;

    // 1. 判断是否是最后一个 beat
    let isLast = ethernetFrameLeftByteCounterReg <= fromInteger(valueOf(DATA_BUS_BYTE_WIDTH));

    let outBeat = ?;
    let hasPayload = firstBeatToSecondBeatPipelineReg.hasPayload;

    // 2. 从 prevBeatReg 获取前一个 beat 的剩余数据（32 bytes）
    DataBeatHalf payloadDataPartFromPreviousBeat = truncateLSB(prevBeatReg.data);

    if (hasPayload) begin
        // === 情况 A: 有 payload ===

        // 3. 获取 payload 的第一个 beat
        let payloadDs = rdmaPayloadPipeInQ.first;
        rdmaPayloadPipeInQ.deq;

        // 4. 取 payload 的前 32 bytes
        DataBeatHalf payloadDataPartConsumedByThisBeat = truncate(payloadDs.data);

        // 5. payload 数据的有效字节数（考虑 startByteIdx）
        // 注意: payload 数据是 Dword 对齐的，所以第一个 beat 的 startIdx 可能不为 0
        let payloadDataByteNum = zeroExtend(payloadDs.startByteIdx) + payloadDs.byteNum;

        if (isLast) begin
            // 情况 A1: 这是最后一个 beat
            immAssert(
                payloadDs.isFirst && payloadDs.isLast &&
                payloadDs.byteNum <= fromInteger(valueOf(DATA_BUS_HALF_BEAT_BYTE_WIDTH)),
                "payload beat is not correct",
                ...
            );
            statusReg <= EthernetPacketGeneratorStateGenFirstBeat;
        end
        else begin
            // 情况 A2: 还有更多 beats
            immAssert(
                payloadDs.isFirst &&
                payloadDs.byteNum > fromInteger(valueOf(DATA_BUS_HALF_BEAT_BYTE_WIDTH)),
                "payload beat is not correct",
                ...
            );

            if (payloadDs.isLast) begin
                // payload 在这个 beat 就结束了，但以太网帧还需要一个 beat
                statusReg <= EthernetPacketGeneratorStateGenExtraLastBeat;
            end
            else begin
                // payload 还有更多 beats
                statusReg <= EthernetPacketGeneratorStateGenMoreBeat;
            end
        end

        prevBeatReg <= payloadDs;

        // 6. 组合输出 beat: 前一个 beat 的剩余 32 bytes + payload 的前 32 bytes
        outBeat = IoChannelEthDataStream{
            data: {payloadDataPartConsumedByThisBeat,
                   swapEndianByte(payloadDataPartFromPreviousBeat)},
            byteNum: fromInteger(valueOf(DATA_BUS_HALF_BEAT_BYTE_WIDTH)) + payloadDataByteNum,
            startByteIdx: 0,
            isFirst: False,
            isLast: isLast
        };
    end
    else begin
        // === 情况 B: 没有 payload ===

        immAssert(isLast,
                  "when no payload, the second beat must be last beat",
                  ...);

        // 只输出前一个 beat 的剩余部分
        outBeat = IoChannelEthDataStream{
            data: {0, swapEndianByte(payloadDataPartFromPreviousBeat)},
            byteNum: fromInteger(valueOf(DATA_BUS_HALF_BEAT_BYTE_WIDTH)),  // 32 bytes
            startByteIdx: 0,
            isFirst: False,
            isLast: isLast
        };
        statusReg <= EthernetPacketGeneratorStateGenFirstBeat;
    end

    // 7. 输出 beat
    ethernetPacketPipeOutQ.enq(outBeat);

    // 8. 更新流水线信息和计数器
    let outPipelineEntry = PacketGeneratorSecondBeatToMoreBeatPipelineEntry{
        fpDebugTime: curFpDebugTime
    };
    secondBeatToMoreBeatPipelineReg <= outPipelineEntry;
    ethernetFrameLeftByteCounterReg <=
        ethernetFrameLeftByteCounterReg - fromInteger(valueOf(DATA_BUS_BYTE_WIDTH));

    dataOutputFullyPipelineCheckTimeReg <= curFpDebugTime;

    // 9. 流水线检查
    let _ <- fpChecker.putStreamBeatInfo(True, outBeat.isLast);

    $display("time=%0t: genSecondBeat, byteNum=%0d, isLast=%0d",
             $time, outBeat.byteNum, outBeat.isLast);
endrule
```

**第 2 个 beat 的数据布局示例** (有 payload):

```
低 32 bytes: 扩展头的剩余部分（来自 prevBeatReg）
  └─ RETH 的剩余部分（虚拟地址、R_Key、DMA 长度等）

高 32 bytes: Payload 数据的开始部分
  └─ RDMA payload 的前 32 bytes
```

**关键逻辑**:
1. **两种情况分支**:
   - 有 payload: 拼接头部剩余 + payload 开始
   - 无 payload: 只输出头部剩余（例如 ACK 包、SEND 0 字节等）

2. **payload 对齐处理**:
   - payload 是 Dword 对齐的，`startByteIdx` 可能不为 0
   - 实际有效字节数 = `startByteIdx + byteNum`

3. **状态转移逻辑**:
   - 如果是最后一个 beat → `GenFirstBeat`
   - 如果 payload 在这个 beat 结束 → `GenExtraLastBeat`
   - 否则 → `GenMoreBeat`

---

### 阶段 3: genMoreBeat (952-1009 行)

**触发条件**: `statusReg == EthernetPacketGeneratorStateGenMoreBeat`

**功能**: 生成后续的 beats，主要是 payload 数据

```bsv
rule genMoreBeat if (statusReg == EthernetPacketGeneratorStateGenMoreBeat);
    let curFpDebugTime <- getSimulationTime;

    // 1. 判断是否是最后一个 beat
    let isLast = ethernetFrameLeftByteCounterReg <= fromInteger(valueOf(DATA_BUS_BYTE_WIDTH));

    // 2. 获取 payload 数据
    let payloadDs = rdmaPayloadPipeInQ.first;
    rdmaPayloadPipeInQ.deq;

    // 3. 拼接数据
    DataBeatHalf payloadDataPartConsumedByThisBeat = truncate(payloadDs.data);
    DataBeatHalf payloadDataPartFromPreviousBeat = truncateLSB(prevBeatReg.data);

    if (isLast) begin
        // 最后一个 beat
        immAssert(
            !payloadDs.isFirst && payloadDs.isLast &&
            payloadDs.byteNum <= fromInteger(valueOf(DATA_BUS_HALF_BEAT_BYTE_WIDTH)),
            "payload beat is not correct",
            ...
        );
        statusReg <= EthernetPacketGeneratorStateGenFirstBeat;
    end
    else begin
        if (payloadDs.isLast) begin
            // payload 结束了，但还需要一个额外的 beat
            statusReg <= EthernetPacketGeneratorStateGenExtraLastBeat;
        end
        else begin
            statusReg <= EthernetPacketGeneratorStateGenMoreBeat;  // 继续
        end
    end

    // 4. 计算有效字节数
    let byteNum = isLast ? truncate(ethernetFrameLeftByteCounterReg)
                         : fromInteger(valueOf(DATA_BUS_BYTE_WIDTH));

    // 5. 生成输出 beat
    let outBeat = IoChannelEthDataStream{
        data: {payloadDataPartConsumedByThisBeat, payloadDataPartFromPreviousBeat},
        byteNum: byteNum,
        startByteIdx: 0,
        isFirst: False,
        isLast: isLast
    };

    ethernetPacketPipeOutQ.enq(outBeat);
    prevBeatReg <= payloadDs;
    ethernetFrameLeftByteCounterReg <=
        ethernetFrameLeftByteCounterReg - fromInteger(valueOf(DATA_BUS_BYTE_WIDTH));

    dataOutputFullyPipelineCheckTimeReg <= curFpDebugTime;

    // 6. 流水线检查（从第二个 payload beat 开始）
    if (!payloadDs.isFirst) begin
        checkFullyPipeline(dataOutputFullyPipelineCheckTimeReg, 1, 2000,
                          DebugConf{name: "mkEthernetPacketGenerator genMoreBeat",
                                    enableDebug: True});
    end
    let _ <- fpChecker.putStreamBeatInfo(outBeat.isFirst, outBeat.isLast);

    $display("time=%0t: genMoreBeat, byteNum=%0d, isLast=%0d",
             $time, byteNum, isLast);
endrule
```

**数据流示例**:

```
Payload 流:     [Beat 0: 64B] [Beat 1: 64B] [Beat 2: 64B] [Beat 3: 32B isLast]
                      ↓             ↓             ↓             ↓
prevBeatReg:    [---]  [Beat 0]    [Beat 1]    [Beat 2]    [Beat 3]
                      ↓             ↓             ↓             ↓
输出流:         [头部] [前32+B0前32][B0后32+B1前32][B1后32+B2前32]
```

**关键点**:
1. **数据拼接**: 每个输出 beat 由两部分组成
   - 前一个 payload beat 的高 32 bytes
   - 当前 payload beat 的低 32 bytes

2. **流水线检查**: 确保 payload beats 之间没有停顿（完全流水线）

3. **状态转移**:
   - 如果是最后一个 beat → `GenFirstBeat`
   - 如果 payload 结束但还有剩余空间 → `GenExtraLastBeat`
   - 否则继续 `GenMoreBeat`

---

### 阶段 4: genExtraLastBeat (1011-1061 行)

**触发条件**: `statusReg == EthernetPacketGeneratorStateGenExtraLastBeat`

**功能**: 生成额外的最后一个 beat，用于输出 prevBeatReg 中剩余的数据

```bsv
rule genExtraLastBeat if (statusReg == EthernetPacketGeneratorStateGenExtraLastBeat);
    let curFpDebugTime <- getSimulationTime;

    // 1. 确认这是最后一个 beat
    let isLast = ethernetFrameLeftByteCounterReg <= fromInteger(valueOf(DATA_BUS_BYTE_WIDTH));
    immAssert(isLast, "must be last beat here", ...);

    // 2. 获取前一个 beat 的剩余数据
    DataBeatHalf payloadDataPartFromPreviousBeat = truncateLSB(prevBeatReg.data);

    // 3. 计算有效字节数
    BusByteIdx mod = truncate(ethernetFrameLeftByteCounterReg);

    // 从 prevBeatReg 的 byteNum 计算
    BusByteCnt byteNum = prevBeatReg.byteNum -
                         fromInteger(valueOf(DATA_BUS_HALF_BEAT_BYTE_WIDTH));

    // 4. 生成输出 beat
    DATA data = zeroExtend(payloadDataPartFromPreviousBeat);
    let outBeat = IoChannelEthDataStream{
        data: data,
        byteNum: byteNum,
        startByteIdx: 0,
        isFirst: False,
        isLast: True
    };
    ethernetPacketPipeOutQ.enq(outBeat);

    // 5. 交叉验证字节数计算
    BusByteCnt byteNumCalcFromByteCounter =
        mod == 0 ? fromInteger(valueOf(DATA_BUS_BYTE_WIDTH)) : zeroExtend(pack(mod));
    immAssert(
        byteNumCalcFromByteCounter == byteNum,
        "last beat of payload length calculated by two different ways have different result.",
        ...
    );

    // 6. 状态回到初始状态
    statusReg <= EthernetPacketGeneratorStateGenFirstBeat;

    // 7. 流水线检查
    checkFullyPipeline(dataOutputFullyPipelineCheckTimeReg, 1, 2000,
                      DebugConf{name: "mkEthernetPacketGenerator genExtraLastBeat",
                                enableDebug: True});
    let _ <- fpChecker.putStreamBeatInfo(outBeat.isFirst, outBeat.isLast);

    $display("time=%0t: genExtraLastBeat, byteNum=%0d", $time, byteNum);
endrule
```

**何时需要 ExtraLastBeat?**

当 payload 的最后一个 beat 占用的字节数超过 32 bytes，但不足 64 bytes 时：

```
例子: Payload 最后一个 beat 有 48 bytes

genSecondBeat/genMoreBeat:
  输出: [前一个 beat 的高 32B] + [最后 beat 的低 32B]
  还剩: 最后 beat 的高 16B 未输出

genExtraLastBeat:
  输出: [最后 beat 的高 16B]
```

**关键验证**:
- 使用两种方法计算有效字节数，确保一致性
  1. 从 `prevBeatReg.byteNum` 计算
  2. 从 `ethernetFrameLeftByteCounterReg` 计算

---

## 字节序处理

### swapEndianByte() 函数

RDMA 协议头部使用**网络字节序（大端）**，需要转换：

```bsv
function DATA swapEndianByte(DATA data);
    // 交换每个字节的顺序
    // 输入:  0x0123456789ABCDEF...
    // 输出:  0xEFCDAB8967452301...
endfunction
```

### 应用规则

| 数据类型 | 是否需要 swapEndianByte | 原因 |
|---------|----------------------|------|
| MAC/IP/UDP 头部 | ✅ 需要 | 协议头部字段使用网络字节序 |
| BTH/RETH/AETH 等 | ✅ 需要 | RDMA 协议头部使用网络字节序 |
| Payload 数据 | ❌ 不需要 | 来自 DMA 的原始数据，保持原样 |

在代码中的体现：
- `genFirstBeat`: 对头部数据调用 `swapEndianByte()`
- `genSecondBeat`: 对头部剩余部分调用 `swapEndianByte()`
- `genMoreBeat`: **不**对 payload 数据调用 `swapEndianByte()`

---

## 以太网帧结构完整示例

假设一个 **SEND FIRST** 包，payload 4000 bytes，PMTU 4096:

### 帧结构

```
┌────────────────────────────────────────────────────────┐
│ Ethernet Header (14 bytes)                             │
│  - Dst MAC: 6B                                         │
│  - Src MAC: 6B                                         │
│  - EthType: 2B (0x0800)                                │
├────────────────────────────────────────────────────────┤
│ IP Header (20 bytes)                                   │
│  - Version, IHL, DSCP, ECN: 2B                         │
│  - Total Length: 2B (4000 + 12 + 16 + 8 + 20 = 4056)   │
│  - Identification: 2B                                  │
│  - Flags, Fragment Offset: 2B                          │
│  - TTL: 1B                                             │
│  - Protocol: 1B (17 = UDP)                             │
│  - Header Checksum: 2B ← 由 IP 校验和流水线计算        │
│  - Src IP: 4B                                          │
│  - Dst IP: 4B                                          │
├────────────────────────────────────────────────────────┤
│ UDP Header (8 bytes)                                   │
│  - Src Port: 2B                                        │
│  - Dst Port: 2B (4791)                                 │
│  - Length: 2B (4000 + 12 + 16 + 8 = 4036)              │
│  - Checksum: 2B                                        │
├────────────────────────────────────────────────────────┤
│ BTH (12 bytes)                                         │
│  - OpCode: 1B (RC_SEND_FIRST)                          │
│  - Flags: 1B                                           │
│  - Partition Key, Pad Count, Transport Header Version: 2B│
│  - Destination QP: 3B                                  │
│  - Ack Request, Reserved: 1B                           │
│  - Packet Sequence Number: 3B                          │
├────────────────────────────────────────────────────────┤
│ RETH (16 bytes) - 仅对于 SEND/WRITE FIRST/ONLY         │
│  - Virtual Address: 8B                                 │
│  - R_Key: 4B                                           │
│  - DMA Length: 4B                                      │
├────────────────────────────────────────────────────────┤
│ Payload (4000 bytes)                                   │
│  - 实际的 RDMA 数据                                    │
└────────────────────────────────────────────────────────┘

总长度: 14 + 20 + 8 + 12 + 16 + 4000 = 4070 bytes
```

### Beat 布局 (512-bit 数据总线, 64 bytes/beat)

```
Beat 0 (genFirstBeat):
  ┌────────────────────────────────────────────────────────┐
  │ Eth(14B) + IP(20B) + UDP(8B) + BTH(12B) + RETH(10B)    │
  └────────────────────────────────────────────────────────┘
  64 bytes, isFirst=True, isLast=False

Beat 1 (genSecondBeat):
  ┌────────────────────────────────────────────────────────┐
  │ RETH 剩余(6B) + Payload(0~58B)                         │
  └────────────────────────────────────────────────────────┘
  64 bytes, isFirst=False

Beat 2-62 (genMoreBeat):
  ┌────────────────────────────────────────────────────────┐
  │ Payload 中间部分 (每个 64B)                             │
  └────────────────────────────────────────────────────────┘
  64 bytes, isFirst=False

Beat 63 (genMoreBeat 或 genExtraLastBeat):
  ┌────────────────────────────────────────────────────────┐
  │ Payload 最后部分 (不满 64B)                            │
  └────────────────────────────────────────────────────────┘
  < 64 bytes, isFirst=False, isLast=True
```

总 beats: 约 64 个 (4070 / 64 ≈ 63.6)

---

## 性能优化机制

### 1. IP 校验和流水线化

IP 校验和计算在独立模块中进行，不阻塞主流程：

```bsv
let ipHdrCheckSumStreamPipeOut <- mkIpHdrCheckSumStream(
    toPipeOut(ipHeaderForChecksumCalcQ)
);
```

**流水线深度**: 3 级
- 允许 3 个包同时处理（1 个计算中，1 个等待，1 个新到达）

### 2. 完全流水线检查

使用 `checkFullyPipeline()` 确保关键路径不会停顿：

```bsv
checkFullyPipeline(
    dataOutputFullyPipelineCheckTimeReg,  // 上一个事件的时间戳
    1,                                     // 期望的周期数
    2000,                                  // 超时阈值
    DebugConf{name: "mkEthernetPacketGenerator genMoreBeat", enableDebug: True}
);
```

**检查点**:
- `genMoreBeat`: 检查 payload beats 之间的间隔
- `genExtraLastBeat`: 检查最后一个 beat 的延迟

### 3. 流水线寄存器传递

使用寄存器而非队列传递状态间的少量数据，减少资源：

```bsv
Reg#(PacketGeneratorFirstBeatToSecondBeatPipelineEntry) firstBeatToSecondBeatPipelineReg;
Reg#(PacketGeneratorSecondBeatToMoreBeatPipelineEntry) secondBeatToMoreBeatPipelineReg;
```

### 4. 流完整性检查

使用 `mkStreamFullyPipelineChecker` 验证输出流的完整性：

```bsv
let fpChecker <- mkStreamFullyPipelineChecker(...);
let _ <- fpChecker.putStreamBeatInfo(outBeat.isFirst, outBeat.isLast);
```

确保：
- 每个包都有 `isFirst` 和 `isLast`
- beats 连续输出，无遗漏

---

## 关键设计考虑

### 1. 固定头部长度假设

```bsv
MAC_IP_UDP_TOTAL_HDR_BYTE_WIDTH = 42
- Ethernet: 14 bytes
- IP: 20 bytes (不支持 IP 选项)
- UDP: 8 bytes
```

**限制**: 不支持带 IP 选项的包

### 2. 数据对齐

- **输入 payload**: 可能 Dword 对齐（`startByteIdx != 0`）
- **输出以太网帧**: 强制字节对齐（`startByteIdx = 0`）

处理方式：
```bsv
byteNum: fromInteger(valueOf(DATA_BUS_HALF_BEAT_BYTE_WIDTH)) + payloadDataByteNum
startByteIdx: 0  // 强制为 0
```

### 3. 最小包大小

```bsv
immAssert(!isLast,
          "send packet must be more than 1 beat in 512 bits wide bus",
          ...);
```

在 512 位数据总线上，发送包**必须大于 64 bytes**。

**原因**: 头部本身就占用约 54-70 bytes（取决于扩展头）

### 4. 双重验证机制

使用两种方法验证包结束：
1. **计数器方法**: `ethernetFrameLeftByteCounterReg <= DATA_BUS_BYTE_WIDTH`
2. **标志方法**: `payloadDs.isLast`

在 `genMoreBeat` 和 `genExtraLastBeat` 中都有断言验证两者一致。

---

## 模块接口实现（1063-1071 行）

```bsv
method Action setLocalNetworkSettings(LocalNetworkSettings networkSettings);
    networkSettingsReg <= tagged Valid networkSettings;
endmethod

interface macIpUdpMetaPipeIn    = toPipeInB0(macIpUdpMetaPipeInQ);
interface rdmaPacketMetaPipeIn  = toPipeInB0(rdmaPacketMetaPipeInQ);
interface rdmaPayloadPipeIn     = toPipeInB0(rdmaPayloadPipeInQ);
interface ethernetPacketPipeOut = toPipeOut(ethernetPacketPipeOutQ);
```

**配置方法**:
- `setLocalNetworkSettings()`: 设置本地 MAC 地址、IP 地址等网络配置

**接口转换**:
- `toPipeInB0()`: 将内部 FIFOF 转换为 PipeInB0 接口
- `toPipeOut()`: 将内部 FIFOF 转换为 PipeOut 接口

---

## 调试与验证

### 1. 调试输出

每个状态都有详细的 `$display` 输出：

```bsv
$display(
    "time=%0t:", $time, toGreen(" mkEthernetPacketGenerator genFirstBeat"),
    toBlue(", outBeat="), fshow(outBeat),
    toBlue(", totalEthernetFrameLen="), fshow(pipelineEntry.totalEthernetFrameLen),
    ...
);
```

### 2. 断言检查

关键逻辑点都有断言：
- 包大小合理性
- payload beat 标志正确性
- 字节数计算一致性

### 3. 流水线监控

- `checkFullyPipeline()`: 监控时序
- `fpChecker`: 验证流完整性
- `ethernetPacketPipeOutQ`: 带 full 断言，防止队列溢出

---

## 总结

`mkEthernetPacketGenerator` 模块的核心职责：

1. **协议栈封装**: 将 RDMA 包（BTH + 扩展头 + payload）封装到 UDP/IP/Ethernet 帧中
2. **字节序转换**: 对协议头部进行网络字节序转换
3. **数据拼接**: 巧妙地处理 96 bytes 头部在 512-bit 总线上的跨 beat 拼接
4. **流式输出**: 生成完全流水线化的输出流，适配 MAC 层接口
5. **性能保证**: 通过流水线设计和完整性检查，确保高性能和正确性

### 关键创新点

1. **96 bytes 头部设计**: 正好 1.5 个 beat，巧妙利用 `prevBeatReg` 处理跨 beat 数据
2. **四状态机**: 清晰地分离了头部、payload 开始、payload 中间、payload 结尾的处理
3. **双重验证**: 使用计数器和标志两种方法验证包结束，提高可靠性
4. **流水线化校验和计算**: 不阻塞主流程，提高吞吐量

这个模块是 RDMA 硬件实现中连接协议逻辑和物理层的关键桥梁，标志着软件可见的 RDMA 操作转换为硬件可发送的以太网帧的最后一步。
