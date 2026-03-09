# PacketGenAndParse.bsv 文件详细分析

## 文件概述

`PacketGenAndParse.bsv` 是 RDMA 硬件实现中负责**数据包生成和解析**的核心模块。它处理从工作队列请求到完整网络数据包的转换，以及接收数据包的解析工作。

## 核心概念

### Packet（数据包）vs Payload（有效负载）

#### Packet（数据包）
- **定义**：完整的网络传输单元，包含所有协议层的头部信息
- **组成结构**：
  ```
  [以太网头部][IP头部][UDP头部][RDMA头部(BTH+扩展头部)][Payload数据][CRC]
  ```
- **作用**：在网络中实际传输的完整单元，包含路由、传输控制等所有必要信息
- **大小限制**：受 PMTU (Path MTU) 限制，通常为 256、512、1024、2048、4096 字节等

#### Payload（有效负载）
- **定义**：数据包中实际的用户数据部分，不包含任何协议头部
- **内容**：RDMA 操作要传输的实际数据（如 RDMA Write 的数据、Send 的消息内容）
- **来源**：从用户内存中读取（通过 DMA）
- **特点**：只关心数据内容本身，不关心如何传输

**类比理解**：
- Packet 就像一个快递包裹（包括包装盒、地址标签、运单等）
- Payload 就像包裹里面的实际物品

---

## 主要模块

### 1. PacketGen（数据包生成器）

#### 功能概述
将 RDMA 工作队列元素（WorkQueueElem）转换为可以在网络上发送的完整数据包。

#### 核心流程

```
WorkQueueElem → MR表查询 → Payload生成 → 分包 → 头部生成 → 完整数据包
```

#### 详细处理流程

##### 步骤1：查询内存区域表（queryMrTable）
```bsv
// 位置：PacketGenAndParse.bsv:493-521
rule queryMrTable;
    let wqe = wqePipeInQ.first;
    Bool hasPayload = workReqNeedPayloadGen(wqe.opcode) && !isZeroR(wqe.len);

    if (hasPayload) {
        // 查询内存区域表，获取页表信息
        let mrTableQueryReq = MrTableQueryReq{
            idx: lkey2IndexMR(wqe.lkey)
        };
        mrTableQueryCltInst.putReq(mrTableQueryReq);
    }
endrule
```

**作用**：
- 检查操作是否需要 payload（如 RDMA_WRITE、SEND 需要，RDMA_READ_REQ 不需要）
- 查询本地密钥（lkey）对应的内存区域信息，用于后续的地址转换

##### 步骤2：发送分块请求和 Payload 生成请求
```bsv
// 位置：PacketGenAndParse.bsv:524-580
rule sendChunkByRemoteAddrReqAndPayloadGenReq;
    if (hasPayload) {
        // 按照 PMTU 大小分块远程地址
        let remoteAddrChunkReq = AddressChunkReq{
            startAddr: wqe.raddr,
            len: wqe.len,
            chunk: getPmtuSizeByPmtuEnum(wqe.pmtu)
        };

        // 发送 Payload 生成请求（从本地内存读取数据）
        let payloadGenReq = PayloadGenReq{
            addr: wqe.laddr,
            len: wqe.len,
            baseVA: mrTable.baseVA,
            pgtOffset: mrTable.pgtOffset
        };

        // 计算本地地址到远程地址的对齐偏移
        ByteIdxInDword localToRemoteAlignShiftOffset =
            zeroExtend(localAddrOffset) - zeroExtend(remoteAddrOffset);
    }
endrule
```

**作用**：
- **分块远程地址**：将整个传输按 PMTU 大小分成多个数据包
- **Payload 生成**：向 PayloadGen 模块请求从本地内存读取数据
- **地址对齐**：计算本地和远程地址的对齐差异，用于数据对齐

##### 步骤3：生成数据包头部（第一步）
```bsv
// 位置：PacketGenAndParse.bsv:582-676
rule genPacketHeaderStep1;
    Bool isFirstPacket = wqe.isFirst;
    Bool isLastPacket  = wqe.isLast;

    if (hasPayload) {
        // 获取当前数据包的信息（由地址分块器提供）
        packetInfo = wqeToPacketChunker.responsePipeOut.first;

        isFirstPacket = isFirstPacket && packetInfo.isFirst;
        isLastPacket = isLastPacket && packetInfo.isLast;

        // 为第一个数据包设置起始 PSN
        if (packetInfo.isFirst) {
            psn = wqe.psn;
            psnReg <= psn + 1;
        }

        remoteAddr = packetInfo.startAddr;
        dlen = isFirstPacket ? wqe.totalLen : packetInfo.len;
    }
endrule
```

**作用**：
- 确定数据包的类型（First/Middle/Last/Only）
- 分配 PSN（Packet Sequence Number）数据包序列号
- 计算 UDP 负载长度

##### 步骤4：生成数据包头部（第二步）
```bsv
// 位置：PacketGenAndParse.bsv:678-751
rule genPacketHeaderStep2;
    // 生成 BTH（Base Transport Header）
    let bthMaybe <- genRdmaBTH(wqe, isFirstPacket, isLastPacket,
                                solicited, psn, padCnt, ackReq,
                                remoteAddr, dlen);

    // 生成扩展头部（RETH, DETH, AtomicEth 等）
    let extendHeaderBufferMaybe <- genRdmaExtendHeader(wqe, isFirstPacket,
                                                        isLastPacket,
                                                        remoteAddr, dlen);

    // 组装 RDMA 数据包元数据
    let rdmaPacketMeta = RdmaSendPacketMeta {
        header: RdmaBthAndExtendHeader{
            bth: bth,
            rdmaExtendHeaderBuf: extendHeaderBuffer
        },
        hasPayload: hasPayload
    };

    // 生成 MAC/IP/UDP 元数据
    let macIpUdpMeta = ThinMacIpUdpMetaDataForSend{
        dstMacAddr: wqe.macAddr,
        dstIpAddr: wqe.dqpIP,
        srcPort: truncate(wqe.sqpn),
        dstPort: UDP_PORT_RDMA,
        udpPayloadLen: udpPayloadLen
    };
endrule
```

**作用**：
- **生成 BTH**：包含操作码、PSN、目标 QPN 等核心信息
- **生成扩展头部**：根据操作类型生成不同的扩展头部
  - RDMA_WRITE: RETH（包含远程地址、rkey、长度）
  - SEND: 可能包含 ImmDt（立即数）或 IETH（无效化的 rkey）
  - ATOMIC: AtomicEth（包含原子操作的比较值和交换值）
- **生成网络层元数据**：MAC 地址、IP 地址、UDP 端口等

#### 关键数据结构

##### WorkQueueElem（工作队列元素）
```bsv
typedef struct {
    WorkReqOpCode opcode;     // 操作码：RDMA_WRITE, SEND, READ 等
    ADDR   laddr;             // 本地地址（源地址）
    LKEY   lkey;              // 本地密钥（用于内存保护）
    ADDR   raddr;             // 远程地址（目标地址）
    RKEY   rkey;              // 远程密钥
    Length len;               // 传输长度
    PSN    psn;               // 起始数据包序列号
    PMTU   pmtu;              // 路径最大传输单元
    QPN    dqpn;              // 目标 QP 号
    // ... 其他字段
} WorkQueueElem;
```

##### 支持的 RDMA 操作码映射

```bsv
function Maybe#(RdmaOpCode) genRdmaOpCode(WorkReqOpCode wrOpCode, Bool isFirst, Bool isLast);
    // 示例：RDMA_WRITE 的不同变体
    // First + Last (Only) → RDMA_WRITE_ONLY
    // First + !Last       → RDMA_WRITE_FIRST
    // !First + !Last      → RDMA_WRITE_MIDDLE
    // !First + Last       → RDMA_WRITE_LAST
endfunction
```

---

### 2. PacketParse（数据包解析器）

#### 功能概述
接收网络数据包，解析出 RDMA 头部信息和 payload 数据。

#### 组成模块

```bsv
module mkPacketParse(PacketParse);
    // 1. 输入分类器：区分 RDMA 数据包和其他数据包
    InputPacketClassifier inputPacketClassifier <- mkInputPacketClassifier;

    // 2. RDMA 元数据和 Payload 提取器
    RdmaMetaAndPayloadExtractor rdmaHeaderExtractor <- mkRdmaMetaAndPayloadExtractor;

    // 连接两个模块
    mkConnection(inputPacketClassifier.rdmaRawPacketPipeOut,
                 rdmaHeaderExtractor.ethPipeIn);
endmodule
```

#### 处理流程

```
以太网帧输入 → 分类器 → RDMA数据包?
                         ├─ 是 → 头部提取器 → RDMA元数据 + Payload
                         └─ 否 → 其他数据包输出
```

---

### 3. PacketGenReqArbiter（数据包生成请求仲裁器）

#### 功能
当有多个来源（如多个 QP）同时请求发送数据包时，进行仲裁选择。

#### 工作原理

```bsv
module mkPacketGenReqArbiter(PacketGenReqArbiter);
    // 支持 3 个输入通道
    Vector#(3, PipeInB0#(ThinMacIpUdpMetaDataForSend)) macIpUdpMetaPipeInVec;
    Vector#(3, PipeInB0#(RdmaSendPacketMeta)) rdmaPacketMetaPipeInVec;
    Vector#(3, PipeInB0#(DataStream)) rdmaPayloadPipeInVec;

    // 仲裁器：轮询或优先级选择
    Arbiter_IFC#(3) arbiter <- mkArbiter(False);

    // 选择一个通道的请求转发到输出
    rule recvArbitResp;
        for (Integer channelIdx = 0; channelIdx < 3; channelIdx++) {
            if (arbiter.clients[channelIdx].grant) {
                // 转发该通道的元数据和 payload
            }
        }
    endrule
endmodule
```

---

## 关键函数详解

### 1. genRdmaOpCode - 生成 RDMA 操作码

```bsv
function Maybe#(RdmaOpCode) genRdmaOpCode(
    WorkReqOpCode wrOpCode, Bool isFirst, Bool isLast
);
```

**作用**：根据工作请求操作码和数据包位置，确定实际的 RDMA 传输操作码。

**示例映射**：
- `IBV_WR_RDMA_WRITE` + First&Last → `RDMA_WRITE_ONLY`
- `IBV_WR_RDMA_WRITE` + First → `RDMA_WRITE_FIRST`
- `IBV_WR_RDMA_WRITE` + Middle → `RDMA_WRITE_MIDDLE`
- `IBV_WR_RDMA_WRITE` + Last → `RDMA_WRITE_LAST`
- `IBV_WR_RDMA_READ` + First&Last → `RDMA_READ_REQUEST`

### 2. genRdmaExtendHeader - 生成扩展头部

```bsv
function ActionValue#(Maybe#(RdmaExtendHeaderBuffer)) genRdmaExtendHeader(
    WorkQueueElem wqe, Bool isFirst, Bool isLast,
    ADDR remoteAddr, Length dlen
);
```

**作用**：根据操作类型和 QP 类型，生成相应的扩展头部。

**不同操作的扩展头部**：

| 操作类型 | QP 类型 | 扩展头部组成 |
|---------|---------|-------------|
| RDMA_WRITE | RC/UC | RETH（远程地址+rkey+长度） |
| RDMA_WRITE | XRC | XRCETH + RETH |
| RDMA_WRITE_WITH_IMM | RC/UC | RETH + ImmDt（仅 Last 包） |
| SEND | UD | DETH（qkey+sqpn） |
| SEND_WITH_IMM | RC/UC | ImmDt（仅 Last 包） |
| SEND_WITH_INV | RC | IETH（rkey to invalidate，仅 Last 包） |
| RDMA_READ | RC | RETH + RRETH（本地地址+lkey） |
| ATOMIC | RC | AtomicEth（地址+rkey+swap+comp） |
| ACK | RC | AETH（确认信息） |

### 3. genRdmaBTH - 生成基本传输头部

```bsv
function ActionValue#(Maybe#(BTH)) genRdmaBTH(
    WorkQueueElem wqe, Bool isFirst, Bool isLast,
    Bool solicited, PSN psn, PAD padCnt, Bool ackReq,
    ADDR remoteAddr, Length dlen
);
```

**BTH 字段**：
- `opcode`：RDMA 操作码
- `psn`：数据包序列号
- `dqpn`：目标 QP 号
- `ackReq`：是否请求确认
- `solicited`：是否请求事件通知
- `padCnt`：填充字节数

---

## 数据流图

### 发送路径（PacketGen）

```
┌─────────────────┐
│ WorkQueueElem   │ (用户发送请求)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  查询 MR 表      │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────┐
│  地址分块（按 PMTU）                 │
│  + Payload 生成请求（DMA 读内存）    │
└────────┬────────────────────────────┘
         │
         ├──► Payload 数据流 ────┐
         │                       │
         ▼                       ▼
┌─────────────────┐      ┌──────────────┐
│  生成 BTH       │      │ Payload      │
│  + 扩展头部      │      │ 对齐和分割    │
└────────┬────────┘      └──────┬───────┘
         │                       │
         ▼                       │
┌─────────────────┐              │
│ MAC/IP/UDP      │              │
│ 元数据          │              │
└────────┬────────┘              │
         │                       │
         └───────┬───────────────┘
                 │
                 ▼
         ┌──────────────┐
         │ 完整网络数据包│
         └──────────────┘
```

### 接收路径（PacketParse）

```
┌──────────────┐
│ 以太网帧输入  │
└──────┬───────┘
       │
       ▼
┌──────────────────┐
│ 数据包分类器      │
│ (检查目标 IP/端口)│
└──────┬───────────┘
       │
       ├─ RDMA? ──┐
       │          │
       ▼          ▼
┌──────────┐  ┌─────────────────┐
│其他数据包 │  │ RDMA 头部提取器  │
└──────────┘  └────────┬─────────┘
                       │
                       ├──► RDMA 元数据
                       │
                       ├──► Payload 数据
                       │
                       └──► 尾部元数据
```

---

## 性能优化特性

### 1. 流水线设计
代码中多处使用了 Pipeline FIFO：
```bsv
FIFOF#(GenPacketHeaderStep1PipelineEntry) genPacketHeaderStep1PipelineQ <- mkSizedFIFOF(4);
FIFOF#(GenPacketHeaderStep2PipelineEntry) genPacketHeaderStep2PipelineQ <- mkLFIFOF;
```

**作用**：允许不同阶段的处理并行进行，提高吞吐量。

### 2. 完全流水线检查
```bsv
checkFullyPipeline(wqe.fpDebugTime, 11, 2000,
                   DebugConf{name: "mkPacketGen sendChunkByRemoteAddrReqAndPayloadGenReq",
                   enableDebug: True});
```

**作用**：确保流水线没有阻塞，维持高性能。

### 3. 数据对齐处理
```bsv
ByteIdxInDword localToRemoteAlignShiftOffset =
    zeroExtend(localAddrOffset) - zeroExtend(remoteAddrOffset);
payloadStreamShifter.offsetPipeIn.enq(localToRemoteAlignShiftOffset);
```

**作用**：自动处理本地和远程地址的对齐差异，无需 CPU 干预。

---

## 总结

### PacketGenAndParse.bsv 的核心作用

1. **PacketGen**：
   - 将高层的 RDMA 操作请求转换为网络数据包
   - 负责数据包的分片、头部生成、地址对齐
   - 协调 Payload 数据的读取和组装

2. **PacketParse**：
   - 解析接收到的网络数据包
   - 提取 RDMA 控制信息和用户数据
   - 分类和路由不同类型的数据包

### Packet vs Payload 关系

```
Packet = [协议头部] + [Payload] + [校验和]
         ↑                ↑
    PacketGenAndParse   PayloadGenAndCon
    负责生成/解析      负责读取/写入
```

- **Packet 层面**：关注网络传输的完整性、路由、可靠性
- **Payload 层面**：关注用户数据的存储、传输、内存管理

这两个模块协同工作，实现了 RDMA 的零拷贝、内核旁路的高性能数据传输。
