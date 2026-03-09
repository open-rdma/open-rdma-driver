# EthernetPacketGenerator 以太网包生成器详解

## 概述

EthernetPacketGenerator 是 RDMA 数据包发送流程中的**最后一个阶段**，它将 PacketGen 生成的 RDMA 元数据和 payload 组装成完整的以太网帧。

## 在整体流程中的位置

```
WQE → PacketGen (分包) → PacketGenReqArbiter (仲裁) → EthernetPacketGenerator (组装) → 以太网帧输出
```

### 输入接口

从 PacketGenReqArbiter 接收三个流：

1. **macIpUdpMetaPipeIn**: MAC/IP/UDP 元数据
   ```bsv
   typedef struct {
       EthMacAddr dstMacAddr;     // 目标 MAC 地址
       IpAddr dstIpAddr;          // 目标 IP 地址
       UdpPort srcPort;           // 源端口 (QPN)
       UdpPort dstPort;           // 目标端口 (4791 RDMA)
       UdpLength udpPayloadLen;   // UDP payload 长度
       ...
   } ThinMacIpUdpMetaDataForSend;
   ```

2. **rdmaPacketMetaPipeIn**: RDMA 包元数据
   ```bsv
   typedef struct {
       RdmaBthAndExtendHeader header;  // BTH + 扩展头 (RETH, ImmDt 等)
       Bool hasPayload;                // 是否有 payload
   } RdmaSendPacketMeta;
   ```

3. **rdmaPayloadPipeIn**: RDMA payload 数据流
   ```bsv
   DataStream: { data, byteNum, startByteIdx, isFirst, isLast }
   ```

### 输出接口

**ethernetPacketPipeOut**: 完整的以太网帧数据流
```bsv
IoChannelEthDataStream: { data, byteNum, startByteIdx, isFirst, isLast }
```

## 状态机设计

EthernetPacketGenerator 使用 4 个状态生成一个完整的以太网帧：

```bsv
typedef enum {
    EthernetPacketGeneratorStateGenFirstBeat  = 0,  // 生成第 1 beat
    EthernetPacketGeneratorStateGenSecondBeat = 1,  // 生成第 2 beat
    EthernetPacketGeneratorStateGenThirdBeat  = 2,  // 生成第 3 beat
    EthernetPacketGeneratorStateGenMoreBeat   = 3   // 生成剩余 beats
} EthernetPacketGeneratorState;
```

## 详细流程

### 阶段 0: prepareIpHeader (799-833行)

**触发条件**: 当 `macIpUdpMetaPipeIn` 有数据时

**功能**: 准备 IP 头部并启动校验和计算

```bsv
rule prepareIpHeader;
    let macIpUdpMeta = macIpUdpMetaPipeInQ.first;
    macIpUdpMetaPipeInQ.deq;

    // 1. 生成 UDP/IP 头部
    let udpIpHeader = genUdpIpHeader(macIpUdpMeta, localNetSettings, defaultIpId);

    // 2. 生成以太网头部
    let ethHeader = EthHeader {
        dstMacAddr: macIpUdpMeta.dstMacAddr,
        srcMacAddr: localNetSettings.macAddr,
        ethType: macIpUdpMeta.ethType
    };

    // 3. 计算以太网帧总长度
    RdmaEthernetFrameByteLen ethFrameLen =
        macIpUdpMeta.udpPayloadLen + MAC_IP_UDP_TOTAL_HDR_BYTE_WIDTH;

    // 4. 发送到 IP 校验和计算模块
    ipHeaderForChecksumCalcQ.enq(udpIpHeader.ipHeader);

    // 5. 传递到下一阶段
    ipHeaderChecksumCalcPipelineQ.enq(...);
endrule
```

**关键点**:
- IP 校验和计算是流水线化的，在后续阶段会得到结果
- `ethFrameLen` 用于后续判断何时结束

### 阶段 1: genFirstBeat (835-871行)

**触发条件**: `statusReg == EthernetPacketGeneratorStateGenFirstBeat`

**功能**: 生成以太网帧的第 1 个 beat，包含 MAC/IP/UDP 头部

```bsv
rule genFirstBeat if (statusReg == EthernetPacketGeneratorStateGenFirstBeat);
    // 1. 获取 IP 校验和计算结果
    let checksum = ipHdrCheckSumStreamPipeOut.first;
    ipHdrCheckSumStreamPipeOut.deq;

    let pipelineEntry = ipHeaderChecksumCalcPipelineQ.first;
    ipHeaderChecksumCalcPipelineQ.deq;

    // 2. 填充 IP 校验和
    pipelineEntry.macIpUdpHeader.ipHeader.ipChecksum = checksum;

    // 3. 初始化剩余字节计数器
    ethernetFrameLeftByteCounterReg <=
        pipelineEntry.totalEthernetFrameLen - DATA_BUS_BYTE_WIDTH;

    // 4. 生成第 1 个输出 beat
    IoChannelEthDataStream outBeat = IoChannelEthDataStream{
        data: swapEndianByte(truncateLSB(pack(pipelineEntry.macIpUdpHeader))),
        byteNum: DATA_BUS_BYTE_WIDTH,  // 32 bytes
        startByteIdx: 0,
        isFirst: True,
        isLast: False
    };

    ethernetPacketPipeOutQ.enq(outBeat);

    // 5. 状态转移到第 2 beat
    statusReg <= EthernetPacketGeneratorStateGenSecondBeat;
    metricsFirstBeatCntReg <= metricsFirstBeatCntReg + 1;
endrule
```

**第 1 beat 内容结构** (256-bit 数据总线):

```
Byte 0-13:   以太网头 (14 bytes)
  - dstMacAddr (6 bytes)
  - srcMacAddr (6 bytes)
  - ethType (2 bytes)

Byte 14-33:  IP 头 (20 bytes)
  - version, ihl, dscp, ecn
  - totalLen
  - identification
  - flags, fragmentOffset
  - ttl, protocol
  - ipChecksum ← 在这个阶段填充
  - srcIpAddr
  - dstIpAddr

Byte 34-41:  UDP 头 (8 bytes，部分）
  - srcPort
  - dstPort
  - udpLength
  - udpChecksum
```

**关键操作**:
- `swapEndianByte()`: 转换字节序（网络字节序 vs 主机字节序）
- `truncateLSB()`: 取低位部分（因为头部可能不对齐）

### 阶段 2: genSecondBeat (875-933行)

**触发条件**: `statusReg == EthernetPacketGeneratorStateGenSecondBeat`

**功能**: 生成第 2 个 beat，包含剩余 UDP 头 + BTH + 部分扩展头

```bsv
rule genSecondBeat if (statusReg == EthernetPacketGeneratorStateGenSecondBeat);
    // 1. 读取 RDMA 包元数据
    let rdmaMeta = rdmaPacketMetaPipeInQ.first;
    rdmaPacketMetaPipeInQ.deq;

    // 2. 更新剩余字节计数
    ethernetFrameLeftByteCounterReg <=
        ethernetFrameLeftByteCounterReg - DATA_BUS_BYTE_WIDTH;
    let isLast =
        ethernetFrameLeftByteCounterReg <= DATA_BUS_BYTE_WIDTH;

    // 3. 组合 MAC/IP/UDP + BTH + 扩展头
    let macIpUdpHeader = firstBeatToSecondBeatPipelineReg.macIpUdpHeader;
    let macIpUdpBthEth = {pack(macIpUdpHeader), pack(rdmaMeta.header)};

    // 4. 取第 2 个 beat 的数据（跳过第 1 beat 已输出的部分）
    NocData data = truncateLSB(macIpUdpBthEth << DATA_BUS_WIDTH);

    // 5. 生成输出 beat
    let outBeat = genEthernetPacket(
        swapEndianByte(data),
        DATA_BUS_BYTE_WIDTH,
        0,
        False,   // isFirst
        isLast
    );

    ethernetPacketPipeOutQ.enq(outBeat);

    // 6. 状态转移
    if (isLast) {
        statusReg <= EthernetPacketGeneratorStateGenFirstBeat;  // 下一个包
    } else {
        if (rdmaMeta.hasPayload) {
            statusReg <= EthernetPacketGeneratorStateGenThirdBeat;
        } else {
            statusReg <= EthernetPacketGeneratorStateGenFirstBeat;
        }
    }
endrule
```

**第 2 beat 内容示例** (对于 SEND 操作):

```
Byte 0-11:   BTH (12 bytes)
  - opcode, flags
  - padCnt, tver
  - dqpn
  - ackReq, psn

Byte 12-27:  RETH (16 bytes) - 对于 RC/UC SEND
  - virtual address (8 bytes)
  - rkey (4 bytes)
  - dlen (4 bytes)

Byte 28-31:  其他扩展头或 payload 开始
```

**关键点**:
- `isLast` 判断：如果剩余字节少于一个 beat，则这是最后一个 beat
- 有些小包（如 ACK）可能在第 2 beat 就结束

### 阶段 3: genThirdBeat (936-974行)

**触发条件**: `statusReg == EthernetPacketGeneratorStateGenThirdBeat`

**功能**: 生成第 3 个 beat，包含剩余的扩展头部分

```bsv
rule genThirdBeat if (statusReg == EthernetPacketGeneratorStateGenThirdBeat);
    let rdmaMeta = secondBeatToThirdBeatPipelineReg.rdmaMeta;

    ethernetFrameLeftByteCounterReg <=
        ethernetFrameLeftByteCounterReg - DATA_BUS_BYTE_WIDTH;
    let isLast =
        ethernetFrameLeftByteCounterReg <= DATA_BUS_BYTE_WIDTH;

    // 取第 3 个 beat 的数据
    let macIpUdpHeader = secondBeatToThirdBeatPipelineReg.macIpUdpHeader;
    let macIpUdpBthEth = {pack(macIpUdpHeader), pack(rdmaMeta.header)};
    NocData data = truncateLSB(
        macIpUdpBthEth << (BYTE_NUM_OF_TWO_BEATS * BYTE_WIDTH)
    );

    let outBeat = genEthernetPacket(
        swapEndianByte(data),
        DATA_BUS_BYTE_WIDTH,
        0,
        False,
        isLast
    );

    ethernetPacketPipeOutQ.enq(outBeat);

    if (isLast) {
        statusReg <= EthernetPacketGeneratorStateGenFirstBeat;
    } else {
        statusReg <= EthernetPacketGeneratorStateGenMoreBeat;
    }
endrule
```

**第 3 beat 可能包含**:
- 剩余的扩展头字节
- Payload 的开始部分

### 阶段 4: genMoreBeat (978-1036行)

**触发条件**: `statusReg == EthernetPacketGeneratorStateGenMoreBeat`

**功能**: 生成剩余的所有 beats，主要是 payload 数据

```bsv
rule genMoreBeat if (statusReg == EthernetPacketGeneratorStateGenMoreBeat);
    ethernetFrameLeftByteCounterReg <=
        ethernetFrameLeftByteCounterReg - DATA_BUS_BYTE_WIDTH;
    let isLast =
        ethernetFrameLeftByteCounterReg <= DATA_BUS_BYTE_WIDTH;

    // 1. 直接读取 payload 数据
    let payload = rdmaPayloadPipeInQ.first;
    rdmaPayloadPipeInQ.deq;
    NocData data = payload.data;

    // 2. Payload 不需要字节序转换（已经是正确的顺序）
    let outBeat = genEthernetPacket(
        data,                    // 注意：没有 swapEndianByte
        payload.byteNum,
        payload.startByteIdx,
        False,
        isLast
    );

    ethernetPacketPipeOutQ.enq(outBeat);

    if (isLast) {
        // 验证 payload 流也认为这是最后一个 beat
        immAssert(
            payload.isLast,
            "payload should be last when isLast is true",
            ...
        );

        statusReg <= EthernetPacketGeneratorStateGenFirstBeat;  // 下一个包
    }
endrule
```

**关键点**:
- Payload 数据**不需要**字节序转换，因为它来自 DMA，已经是正确的格式
- 使用两种方法验证最后一个 beat：
  1. `ethernetFrameLeftByteCounterReg` 计数
  2. `payload.isLast` 标志

## 字节序处理

### 为什么需要 `swapEndianByte()`？

RDMA 协议头部字段使用**网络字节序（大端）**，而硬件内部可能使用不同的字节序。

```bsv
function NocData swapEndianByte(NocData data);
    // 交换每个字节的顺序
    // 例如：0x0123456789ABCDEF → 0xEFCDAB8967452301
endfunction
```

**应用规则**:
- **协议头部** (MAC/IP/UDP/BTH/RETH 等): **需要** `swapEndianByte()`
- **Payload 数据**: **不需要** `swapEndianByte()`

## 以太网帧结构示例

假设一个 SEND FIRST 包，payload 8192 bytes，PMTU 4096:

### Beat 布局

```
Beat 0 (First beat):
  ┌────────────────────────────────────────┐
  │ Ethernet Header (14 bytes)             │
  │ IP Header (20 bytes)                   │
  │ UDP Header (前 6 bytes，部分)          │
  └────────────────────────────────────────┘
  Total: 32 bytes, isFirst=True

Beat 1 (Second beat):
  ┌────────────────────────────────────────┐
  │ UDP Header (后 2 bytes，剩余)          │
  │ BTH (12 bytes)                         │
  │ RETH (16 bytes)                        │
  │ (padding 2 bytes)                      │
  └────────────────────────────────────────┘
  Total: 32 bytes

Beat 2 (Third beat):
  ┌────────────────────────────────────────┐
  │ Payload 开始 (32 bytes)                │
  └────────────────────────────────────────┘

Beat 3-129 (More beats):
  ┌────────────────────────────────────────┐
  │ Payload 数据 (每个 32 bytes)           │
  └────────────────────────────────────────┘
  ...

Beat 130 (Last beat):
  ┌────────────────────────────────────────┐
  │ Payload 最后部分                       │
  │ (可能不满 32 bytes)                    │
  └────────────────────────────────────────┘
  Total: 取决于 byteNum，isLast=True
```

### 总帧大小计算

```
Frame Size = MAC/IP/UDP Header + BTH + RETH + Payload + CRC
          = 42 + 12 + 16 + 4096 + 4
          = 4170 bytes
```

## 完整流程时序图

```
Time    Module              Action
────────────────────────────────────────────────────────────────
t=0     PacketGen           输出 macIpUdpMeta (WQE 1, Pkt 1)
        ↓
t=1     prepareIpHeader     生成 IP 头，启动校验和计算
        ↓
t=4     genFirstBeat        输出 Beat 0 (MAC/IP/UDP)
        ↓
t=5     PacketGen           输出 rdmaPacketMeta (WQE 1, Pkt 1)
        ↓
t=5     genSecondBeat       输出 Beat 1 (BTH/RETH)
        ↓
t=6     genThirdBeat        输出 Beat 2 (扩展头/Payload 开始)
        ↓
t=7     PacketGen           输出 payload beat 0
        ↓
t=7     genMoreBeat         输出 Beat 3 (Payload)
        ↓
t=8     PacketGen           输出 payload beat 1
        ↓
t=8     genMoreBeat         输出 Beat 4 (Payload)
        ...
        ↓
t=135   PacketGen           输出 payload last beat
        ↓
t=135   genMoreBeat         输出 Last Beat, isLast=True
                            状态回到 GenFirstBeat
        ↓
t=136   PacketGen           输出 macIpUdpMeta (WQE 1, Pkt 2)
        ↓
        ...重复上述流程处理第 2 个包
```

## 性能优化

### 1. 流水线深度

```bsv
FIFOF#(IpHeaderChecksumCalcPipelineEntry) ipHeaderChecksumCalcPipelineQ <- mkSizedFIFOF(3);
```

深度为 3，可以容纳：
- 1 个正在计算校验和的包
- 1 个等待输出的包
- 1 个新到达的包

### 2. 校验和计算流水线化

IP 校验和计算在独立的流水线中进行 (`mkIpHdrCheckSumStream`)，不阻塞主流水线。

### 3. 状态寄存器传递

使用寄存器而非队列传递状态之间的数据，减少资源占用：

```bsv
Reg#(PacketGeneratorFirstBeatToSecondBeatPipelineEntry) firstBeatToSecondBeatPipelineReg;
Reg#(PacketGeneratorSecondBeatToThirdBeatPipelineEntry) secondBeatToThirdBeatPipelineReg;
```

### 4. 性能监控

模块提供 4 个计数器监控各阶段吞吐量：

```bsv
metricsFirstBeatCntReg   // 第 1 beat 计数
metricsSecondBeatCntReg  // 第 2 beat 计数
metricsThirdBeatCntReg   // 第 3 beat 计数
metricsMoreBeatCntReg    // 后续 beats 计数
```

可通过 CSR 接口读取这些性能指标。

## 关键设计考虑

### 1. 固定头部长度

假设：
- MAC 头：14 bytes
- IP 头：20 bytes（不支持 IP 选项）
- UDP 头：8 bytes
- 总计：42 bytes

### 2. 数据总线宽度

256-bit (32 bytes) 数据总线意味着：
- 第 1 beat 可以容纳 MAC + IP 头 + 部分 UDP 头
- 第 2 beat 包含剩余 UDP + BTH + 扩展头

### 3. 对齐处理

`genEthernetPacket` 函数强制 `startByteIdx = 0`：

```bsv
function IoChannelEthDataStream genEthernetPacket(...) {
    let outBeat = IoChannelEthDataStream{
        data: data,
        byteNum: byteNum + zeroExtend(startByteIdx),  // 补偿
        startByteIdx: 0,                               // 强制为 0
        isFirst: isFirst,
        isLast: isLast
    };
    return outBeat;
}
```

原因：以太网包是**纯流式**的，不支持中间对齐。

### 4. 完全流水线检查

在关键路径上使用 `checkFullyPipeline` 确保流水线不会停滞：

```bsv
checkFullyPipeline(
    secondBeatToThirdBeatPipelineReg.fpDebugTime,
    1,
    2000,
    DebugConf{name: "mkEthernetPacketGenerator genThirdBeat", enableDebug: True}
);
```

## 总结

EthernetPacketGenerator 的核心职责：

1. **组装协议栈**: 将 RDMA 包头嵌入 UDP/IP/Ethernet 封装中
2. **字节序转换**: 对协议头部进行必要的字节序转换
3. **流式输出**: 将数据转换为固定格式的流式输出
4. **完全流水线**: 与前级（PacketGen）和后级（以太网 MAC）保持完全流水线

这个模块标志着 RDMA 软件栈在硬件层面的**最后一步**，输出的数据可以直接送到以太网 MAC 层发送。
