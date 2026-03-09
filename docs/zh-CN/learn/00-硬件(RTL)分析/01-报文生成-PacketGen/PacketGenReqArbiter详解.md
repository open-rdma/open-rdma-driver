# PacketGenReqArbiter 包生成请求仲裁器详解

## 概述

`mkPacketGenReqArbiter` 是一个**三路仲裁器**，用于合并来自三个不同来源的 RDMA 包生成请求，并按优先级将它们转发到 `EthernetPacketGenerator` 进行以太网帧封装。

位置：`open-rdma-rtl/src/PacketGenAndParse.bsv:824-924`

---

## 为什么是 NUMERIC_TYPE_THREE (3个通道)?

### NUMERIC_TYPE_THREE 的定义

```bsv
// BasicDataTypes.bsv:14
typedef 3 NUMERIC_TYPE_THREE;
```

### 三个通道分别对应

在 `Top100G.bsv:888-898` 中可以看到这 3 个通道的连接：

| 通道索引 | 来源模块 | 功能描述 | 优先级 |
|---------|---------|---------|--------|
| **Channel 0** | `sq` (Send Queue) | 正常的 RDMA 发送请求<br>- SEND, WRITE, READ 等操作 | 最低 |
| **Channel 1** | `cnpPacketGenerator` | 拥塞通知包 (CNP)<br>- RoCEv2 拥塞控制 | 中等 |
| **Channel 2** | `autoAckGenerator` | 自动 ACK 响应包<br>- RC 连接的 ACK/NACK | 最高 |

**连接代码**:
```bsv
// Channel 0: SQ 正常发送
mkConnection(sq.macIpUdpMetaPipeOut, packetGenReqArbiter.macIpUdpMetaPipeInVec[0]);
mkConnection(sq.rdmaPacketMetaPipeOut, packetGenReqArbiter.rdmaPacketMetaPipeInVec[0]);
mkConnection(sq.rdmaPayloadPipeOut, packetGenReqArbiter.rdmaPayloadPipeInVec[0]);

// Channel 1: CNP 拥塞通知
mkConnection(cnpPacketGenerator.macIpUdpMetaPipeOut, packetGenReqArbiter.macIpUdpMetaPipeInVec[1]);
mkConnection(cnpPacketGenerator.rdmaPacketMetaPipeOut, packetGenReqArbiter.rdmaPacketMetaPipeInVec[1]);
mkConnection(cnpPacketGenerator.rdmaPayloadPipeOut, packetGenReqArbiter.rdmaPayloadPipeInVec[1]);

// Channel 2: Auto ACK
mkConnection(autoAckGenerator.macIpUdpMetaPipeOut, packetGenReqArbiter.macIpUdpMetaPipeInVec[2]);
mkConnection(autoAckGenerator.rdmaPacketMetaPipeOut, packetGenReqArbiter.rdmaPacketMetaPipeInVec[2]);
mkConnection(autoAckGenerator.rdmaPayloadPipeOut, packetGenReqArbiter.rdmaPayloadPipeInVec[2]);
```

---

## 为什么需要仲裁器?

### 问题背景

在 RDMA 硬件实现中，可能同时存在多种包生成需求：

1. **正常数据传输** (SQ): 用户发起的 SEND/WRITE/READ 请求
2. **拥塞控制** (CNP): 当检测到网络拥塞时，需要立即发送 CNP 通知对端降速
3. **可靠传输保证** (ACK): RC 连接需要及时回复 ACK，确认接收到的数据

### 挑战

这三种来源可能**同时**产生包生成请求，但 `EthernetPacketGenerator` 只有**一个输入端口**，无法同时处理多个请求。

### 解决方案

使用仲裁器（Arbiter）进行**时分复用**：
- 按优先级选择一个通道
- 将其完整的包（元数据 + payload）转发到 `EthernetPacketGenerator`
- 继续处理下一个请求

---

## 模块接口定义（813-821 行）

```bsv
interface PacketGenReqArbiter;
    // 输入: 3 个通道，每个通道包含 3 个流
    interface Vector#(NUMERIC_TYPE_THREE, PipeInB0#(ThinMacIpUdpMetaDataForSend)) macIpUdpMetaPipeInVec;
    interface Vector#(NUMERIC_TYPE_THREE, PipeInB0#(RdmaSendPacketMeta)) rdmaPacketMetaPipeInVec;
    interface Vector#(NUMERIC_TYPE_THREE, PipeInB0#(DataStream)) rdmaPayloadPipeInVec;

    // 输出: 合并后的单一流
    interface PipeOut#(ThinMacIpUdpMetaDataForSend) macIpUdpMetaPipeOut;
    interface PipeOut#(RdmaSendPacketMeta) rdmaPacketMetaPipeOut;
    interface PipeOut#(DataStream) rdmaPayloadPipeOut;
endinterface
```

### 接口说明

**输入**: 3 个通道 × 3 个流 = 9 个输入接口
- `macIpUdpMetaPipeInVec[0~2]`: MAC/IP/UDP 元数据
- `rdmaPacketMetaPipeInVec[0~2]`: RDMA 包元数据 (BTH + 扩展头)
- `rdmaPayloadPipeInVec[0~2]`: RDMA payload 数据流

**输出**: 3 个合并后的单一流
- `macIpUdpMetaPipeOut`: 合并后的 MAC/IP/UDP 元数据
- `rdmaPacketMetaPipeOut`: 合并后的 RDMA 包元数据
- `rdmaPayloadPipeOut`: 合并后的 payload 数据流

---

## 模块内部结构（824-850 行）

### 内部组件

```bsv
module mkPacketGenReqArbiter(PacketGenReqArbiter);
    // 1. 接口实例（用于暴露给外部）
    Vector#(NUMERIC_TYPE_THREE, PipeInB0#(ThinMacIpUdpMetaDataForSend)) macIpUdpMetaPipeInVecInst = newVector;
    Vector#(NUMERIC_TYPE_THREE, PipeInB0#(RdmaSendPacketMeta)) rdmaPacketMetaPipeInVecInst = newVector;
    Vector#(NUMERIC_TYPE_THREE, PipeInB0#(DataStream)) rdmaPayloadPipeInVecInst = newVector;

    // 2. 输入队列（每个通道 3 个队列）
    Vector#(NUMERIC_TYPE_THREE, PipeInAdapterB0#(ThinMacIpUdpMetaDataForSend)) macIpUdpMetaPipeInQueueVec
        <- replicateM(mkPipeInAdapterB0);
    Vector#(NUMERIC_TYPE_THREE, PipeInAdapterB0#(RdmaSendPacketMeta)) rdmaPacketMetaPipeInQueueVec
        <- replicateM(mkPipeInAdapterB0);
    Vector#(NUMERIC_TYPE_THREE, PipeInAdapterB0#(DataStream)) rdmaPayloadPipeInQueueVec
        <- replicateM(mkPipeInAdapterB0);

    // 3. 输出队列（合并后的单一队列）
    FIFOF#(ThinMacIpUdpMetaDataForSend) macIpUdpMetaPipeOutQueue <- mkFIFOF;
    FIFOF#(RdmaSendPacketMeta) rdmaPacketMetaPipeOutQueue <- mkFIFOF;
    FIFOF#(DataStream) rdmaPayloadPipeOutQueue <- mkFIFOF;

    // 4. 待转发队列（记录哪个通道的 payload 正在转发）
    FIFOF#(Bit#(TLog#(NUMERIC_TYPE_THREE))) pendingForwardQueue <- mkFIFOF;
    // TLog#(3) = 2, 所以是 Bit#(2), 可以表示 0, 1, 2

    // 5. 仲裁器（3 路）
    Arbiter_IFC#(NUMERIC_TYPE_THREE) arbiter <- mkArbiter(False);
    // False 表示 round-robin 仲裁策略

    // 6. 连接接口到队列（846-850 行）
    for (Integer channelIdx = 0; channelIdx < valueOf(NUMERIC_TYPE_THREE); channelIdx = channelIdx + 1) begin
        macIpUdpMetaPipeInVecInst[channelIdx] = macIpUdpMetaPipeInQueueVec[channelIdx].pipeInIfc;
        rdmaPacketMetaPipeInVecInst[channelIdx] = rdmaPacketMetaPipeInQueueVec[channelIdx].pipeInIfc;
        rdmaPayloadPipeInVecInst[channelIdx] = rdmaPayloadPipeInQueueVec[channelIdx].pipeInIfc;
    end
```

### 数据流架构图

```
输入通道 0 (SQ)
  ├─ macIpUdpMetaPipeInQueueVec[0]
  ├─ rdmaPacketMetaPipeInQueueVec[0]
  └─ rdmaPayloadPipeInQueueVec[0]
                                        ↘
输入通道 1 (CNP)                          ┌──────────┐
  ├─ macIpUdpMetaPipeInQueueVec[1]   →  │ Arbiter  │  →  macIpUdpMetaPipeOutQueue
  ├─ rdmaPacketMetaPipeInQueueVec[1]  →  │  3-way   │  →  rdmaPacketMetaPipeOutQueue
  └─ rdmaPayloadPipeInQueueVec[1]     →  │          │  →  rdmaPayloadPipeOutQueue
                                        ↗ └──────────┘
输入通道 2 (ACK)
  ├─ macIpUdpMetaPipeInQueueVec[2]
  ├─ rdmaPacketMetaPipeInQueueVec[2]
  └─ rdmaPayloadPipeInQueueVec[2]
```

---

## 仲裁逻辑详解

### Rule 1: sendArbitReq (852-862 行)

**功能**: 检查每个通道，如果元数据都准备好了，就向仲裁器发送请求

```bsv
rule sendArbitReq;
    for (Integer channelIdx = 0; channelIdx < valueOf(NUMERIC_TYPE_THREE); channelIdx = channelIdx + 1) begin
        // 条件: macIpUdpMeta 和 rdmaPacketMeta 都有数据
        if (macIpUdpMetaPipeInQueueVec[channelIdx].notEmpty &&
            rdmaPacketMetaPipeInQueueVec[channelIdx].notEmpty) begin
            // 向仲裁器发送请求
            arbiter.clients[channelIdx].request;
        end
    end
endrule
```

**关键点**:
1. **不检查 payload**: 因为有些包可能没有 payload (如 ACK, CNP)
2. **元数据完整性**: 必须同时有 `macIpUdpMeta` 和 `rdmaPacketMeta`
3. **并发请求**: 多个通道可以同时发送请求，仲裁器会选择其中一个

---

### Rule 2: recvArbitResp (865-899 行)

**功能**: 接收仲裁器的授权，转发元数据到输出队列

```bsv
rule recvArbitResp;
    Maybe#(ThinMacIpUdpMetaDataForSend) macIpUdpMetaMaybe = tagged Invalid;
    RdmaSendPacketMeta rdmaMeta = ?;
    Bit#(TLog#(NUMERIC_TYPE_THREE)) curChannelIdx = 0;  // 2-bit, 可表示 0~2

    // 遍历所有通道，找到被授权的那个
    for (Integer channelIdx = 0; channelIdx < valueOf(NUMERIC_TYPE_THREE); channelIdx = channelIdx + 1) begin
        if (arbiter.clients[channelIdx].grant) begin
            // 获取元数据
            macIpUdpMetaMaybe = tagged Valid macIpUdpMetaPipeInQueueVec[channelIdx].first;
            rdmaMeta = rdmaPacketMetaPipeInQueueVec[channelIdx].first;

            // 出队
            macIpUdpMetaPipeInQueueVec[channelIdx].deq;
            rdmaPacketMetaPipeInQueueVec[channelIdx].deq;

            // 记录当前通道索引
            curChannelIdx = fromInteger(channelIdx);
        end
    end

    // 如果有被授权的通道
    if (macIpUdpMetaMaybe matches tagged Valid .macIpUdpMeta) begin
        // 转发元数据到输出队列
        macIpUdpMetaPipeOutQueue.enq(macIpUdpMeta);
        rdmaPacketMetaPipeOutQueue.enq(rdmaMeta);

        // 如果有 payload，记录通道索引到待转发队列
        if (rdmaMeta.hasPayload) begin
            pendingForwardQueue.enq(curChannelIdx);
        end
    end
endrule
```

**关键操作**:
1. **查找授权通道**: 遍历检查 `arbiter.clients[i].grant`
2. **原子性出队**: 同时出队 `macIpUdpMeta` 和 `rdmaPacketMeta`
3. **条件记录**: 只有 `hasPayload=True` 时才记录通道索引
4. **状态传递**: 通过 `pendingForwardQueue` 告诉 `forwardMoreBeat` 从哪个通道读 payload

---

### Rule 3: forwardMoreBeat (901-915 行)

**功能**: 转发选中通道的 payload 数据流

```bsv
rule forwardMoreBeat;
    // 从待转发队列获取通道索引
    let curChannelIdx = pendingForwardQueue.first;

    // 从对应通道读取 payload beat
    let ds = rdmaPayloadPipeInQueueVec[curChannelIdx].first;
    rdmaPayloadPipeInQueueVec[curChannelIdx].deq;

    // 转发到输出队列
    rdmaPayloadPipeOutQueue.enq(ds);

    // 如果是最后一个 beat，完成转发
    if (ds.isLast) begin
        pendingForwardQueue.deq;  // 从待转发队列中移除
    end

    $display("time=%0t: mkPacketGenReqArbiter forwardMoreBeat, ds=%p",
             $time, ds);
endrule
```

**关键点**:
1. **流式转发**: 逐个 beat 转发，直到 `isLast=True`
2. **通道锁定**: 在转发完整个 payload 之前，一直从同一个通道读取
3. **完成标志**: `isLast` 表示 payload 结束，可以处理下一个包

---

## 仲裁策略分析

### Round-Robin 仲裁

```bsv
Arbiter_IFC#(NUMERIC_TYPE_THREE) arbiter <- mkArbiter(False);
// False 表示 round-robin (循环) 模式
```

**工作原理**:
1. 维护一个内部指针，指向上次授权的通道
2. 从下一个通道开始检查请求
3. 找到第一个有请求的通道，授权给它
4. 更新指针，下次从这个通道的下一个开始

**公平性**:
- 长期来看，每个通道获得的机会相等
- 避免某个通道被饿死

### 实际优先级行为

虽然是 round-robin，但由于不同模块的请求频率不同，实际表现出**隐式优先级**：

| 通道 | 请求频率 | 实际优先级 |
|-----|---------|----------|
| Channel 2 (ACK) | 低频，但响应及时 | 实际最高（低延迟）|
| Channel 1 (CNP) | 中频，拥塞时触发 | 中等 |
| Channel 0 (SQ) | 高频，持续发送 | 最低（但总吞吐量最大）|

**为什么 ACK 实际优先级最高？**
- ACK 包小且无 payload，处理快
- 发送频率低，不会长时间占用仲裁器
- round-robin 保证它能快速获得授权

---

## 完整数据流示例

### 场景: 三个通道同时有请求

**初始状态**:
```
Channel 0 (SQ):  有一个 SEND 包，payload 8192 bytes
Channel 1 (CNP): 有一个 CNP 包，无 payload
Channel 2 (ACK): 有一个 ACK 包，无 payload
```

**时序**:

```
时刻 t=0:
  - sendArbitReq: 三个通道都发送仲裁请求
  - Arbiter: 假设选择 Channel 2 (ACK)

时刻 t=1:
  - recvArbitResp:
    * 从 Channel 2 出队 macIpUdpMeta, rdmaPacketMeta
    * 转发到输出队列
    * rdmaMeta.hasPayload = False, 不入队 pendingForwardQueue
    * ACK 包处理完毕

时刻 t=2:
  - sendArbitReq: Channel 0, 1 继续请求
  - Arbiter: 选择 Channel 1 (CNP)

时刻 t=3:
  - recvArbitResp:
    * 从 Channel 1 出队元数据
    * 转发到输出队列
    * CNP 包处理完毕

时刻 t=4:
  - sendArbitReq: 只有 Channel 0 请求
  - Arbiter: 选择 Channel 0 (SQ)

时刻 t=5:
  - recvArbitResp:
    * 从 Channel 0 出队元数据
    * 转发到输出队列
    * rdmaMeta.hasPayload = True
    * pendingForwardQueue.enq(0)  // 记录通道 0

时刻 t=6 ~ t=133:
  - forwardMoreBeat:
    * 从 Channel 0 持续读取 payload beats (8192 / 64 = 128 beats)
    * 转发到 rdmaPayloadPipeOutQueue
    * 直到 ds.isLast = True

时刻 t=134:
  - forwardMoreBeat 检测到 isLast
  - pendingForwardQueue.deq
  - SEND 包处理完毕
```

---

## 设计考量

### 1. 为什么元数据和 Payload 分开处理?

**原因**:
- 元数据总是存在，payload 可选
- 元数据很小（几十字节），可以快速仲裁
- Payload 可能很大（几 KB ~ 几 MB），需要流式转发

**好处**:
- 仲裁器只需关注元数据，快速做决策
- Payload 转发不参与仲裁，减少复杂度

### 2. 为什么使用 pendingForwardQueue?

**问题**: `recvArbitResp` 和 `forwardMoreBeat` 是两个独立的 rule，如何传递通道信息？

**解决方案**: 使用队列传递通道索引
```bsv
FIFOF#(Bit#(TLog#(NUMERIC_TYPE_THREE))) pendingForwardQueue;
// Bit#(2) 可以表示 0, 1, 2
```

**优势**:
- 解耦了元数据仲裁和 payload 转发
- 支持流水线处理：可以在转发当前包的 payload 时，同时仲裁下一个包的元数据

### 3. 为什么是 3 个通道而不是更多?

**当前需求**:
1. 正常数据传输 (SQ)
2. 拥塞控制 (CNP)
3. 可靠传输 (ACK)

**未来扩展**:
- 如果需要支持更多优先级，可以修改为 `NUMERIC_TYPE_FOUR`, `NUMERIC_TYPE_FIVE` 等
- 例如：增加一个高优先级控制通道用于紧急事件

**硬件成本**:
- 每增加一个通道：需要额外的 3 个队列 + 仲裁逻辑
- 3 个通道是功能需求和硬件成本的平衡点

### 4. Round-Robin vs Fixed Priority?

**Round-Robin (当前选择)**:
- ✅ 公平性好，避免饿死
- ✅ 简单，易于实现
- ❌ 无法保证严格的实时性

**Fixed Priority (备选)**:
- ✅ 可以保证高优先级包的延迟
- ❌ 低优先级可能被饿死
- ❌ 需要额外的机制防止饿死

**为什么选择 Round-Robin?**
- RDMA 场景下，ACK 和 CNP 包都很小且频率低
- Round-Robin 已经能保证足够低的延迟
- 避免 SQ 被完全阻塞（影响吞吐量）

---

## 性能分析

### 吞吐量

**理论最大吞吐量**: 受限于输出端 (EthernetPacketGenerator)
- 假设 512-bit 总线，100 Gbps 链路
- 最大吞吐量: 100 Gbps

**仲裁开销**:
- 元数据仲裁: 1 周期
- Payload 转发: 0 额外开销（直接流水线转发）

**总开销**: < 1% (因为元数据仅占 1 个周期，而 payload 可能几百周期)

### 延迟

**ACK 包延迟** (最关心的指标):
```
最好情况: 1 周期 (仲裁) + 0 周期 (无 payload)
最坏情况: 等待当前包的 payload 转发完成
  = 1 (仲裁) + 最大包大小 / 数据率
  = 1 + (4096 bytes / 64 bytes/cycle)
  = 1 + 64 cycles
  ≈ 65 cycles @ 250 MHz = 260 ns
```

**CNP 包延迟**: 类似 ACK，因为也无 payload

**SQ 包延迟**: 不太关键，因为是批量发送

---

## 与其他模块的连接

### 上游模块

```bsv
SQ (Send Queue)                  → Channel 0
CNP Packet Generator             → Channel 1
Auto ACK Generator               → Channel 2
```

### 下游模块

```bsv
PacketGenReqArbiter 输出 → EthernetPacketGenerator → MAC 层
```

### 完整路径

```
┌─────────────┐
│     SQ      │ → macIpUdpMetaPipeOut
│ (Send Queue)│ → rdmaPacketMetaPipeOut
└─────────────┘ → rdmaPayloadPipeOut
                        ↓ Channel 0
┌─────────────┐         ↓
│     CNP     │ ────→ ┌────────────────────┐
│  Generator  │ Ch 1  │ PacketGenReqArbiter│
└─────────────┘   →   │   (3-way Arbiter)  │ → macIpUdpMetaPipeOut
                  ↓   └────────────────────┘ → rdmaPacketMetaPipeOut
┌─────────────┐   ↓                          → rdmaPayloadPipeOut
│  Auto ACK   │───→                                   ↓
│  Generator  │ Ch 2                        ┌────────────────────┐
└─────────────┘                             │ EthernetPacketGen  │
                                            │  (Encapsulation)   │
                                            └────────────────────┘
                                                      ↓
                                            完整的以太网帧
                                                      ↓
                                                  MAC 层
```

---

## 调试与验证

### 调试输出

`forwardMoreBeat` rule 有详细的调试输出：

```bsv
$display(
    "time=%0t:", $time, toGreen(" mkPacketGenReqArbiter forwardMoreBeat"),
    toBlue(", ds="), fshow(ds)
);
```

可以通过这个输出观察：
- 哪个通道的 payload 正在转发
- 每个 beat 的数据内容
- 是否正确检测到 `isLast`

### 常见问题

**问题 1**: Payload 转发卡住

**原因**: `pendingForwardQueue` 有数据，但对应通道的 `rdmaPayloadPipeInQueueVec` 为空

**检查**:
```bsv
// 确保 recvArbitResp 中的逻辑正确
if (rdmaMeta.hasPayload) begin
    pendingForwardQueue.enq(curChannelIdx);
end
```

**问题 2**: 某个通道被饿死

**原因**: Round-robin 仲裁器实现有 bug，或者其他通道请求过于频繁

**检查**: 观察 `sendArbitReq` 和 `recvArbitResp` 的调试输出，统计每个通道的授权次数

---

## 总结

### 核心功能

`mkPacketGenReqArbiter` 是一个**三路包生成请求仲裁器**，它：

1. **合并三个来源**: SQ、CNP、ACK
2. **公平仲裁**: Round-robin 策略
3. **流式转发**: 元数据原子性，payload 流水线化
4. **通道隔离**: 每个通道独立排队，互不干扰

### 为什么是 NUMERIC_TYPE_THREE?

因为 RDMA 硬件需要处理**三种不同优先级和类型**的包：
1. **数据包** (SQ): 用户发起的 RDMA 操作
2. **拥塞包** (CNP): 网络拥塞控制
3. **控制包** (ACK): 可靠传输保证

每种包都需要一个独立的通道，因此是 **3 个通道**。

### 设计亮点

1. **元数据 + Payload 分离**: 快速仲裁 + 流式转发
2. **pendingForwardQueue**: 优雅地解耦仲裁和转发逻辑
3. **Round-Robin**: 公平性和实时性的平衡
4. **可扩展性**: 通过修改 `NUMERIC_TYPE_THREE` 轻松增加通道数

这个模块是 RDMA 发送路径中的**关键调度点**，确保不同优先级的包都能及时、公平地发送到网络。