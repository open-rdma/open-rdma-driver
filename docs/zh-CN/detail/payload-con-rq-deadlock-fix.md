# PayloadCon 与 RQ 死锁问题修复

## 问题概述

在 RQ (Receive Queue) 满载情况下，PayloadCon 模块与 RQ meta report ringbuf 之间会发生死锁，导致整个数据路径完全阻塞。

## 问题现象

- RQ meta report ringbuf 的 `hw_head` 停止更新，卡在某个固定值（如 `0x08`）
- `inFlightWriteReqHeaadUpdateQ` 持续报告 Full 状态
- DMA 写操作无法完成，数据流停滞

## 根本原因分析

### 死锁形成机制

当 RQ 被填满时，反压沿着数据路径向上传播。在这种情况下，`StreamArbiterSlave`（DMA 通道复用器）可能会出现以下调度顺序：

1. **请求接收顺序**：DMA 通道复用器先接收到 PayloadCon 的写请求，后接收到 RQ ringbuf 的写请求

2. **顺序保证约束**：为了保证 DMA 写操作的顺序性，必须先转发 PayloadCon 的写请求，再处理 RQ Ringbuf 的请求

3. **阻塞传播**：
   - RQ Ringbuf 的请求无法被及时处理（因为所有上游 FIFO 都已阻塞）
   - PayloadCon 需要完成其写操作才能释放 DMA 通道

4. **关键耦合点**：PayloadCon 的设计中，转发最后一个 DMA data beat 到 DMA 通道 与 向 `conRespPipeOut` 发送响应给 RQ 是**在同一个 rule 中绑定执行**的

5. **死锁闭环**：
   ```
   PayloadCon.forwardConsumedFinishedSignal
       → 需要向 conRespPipeOutQ.enq(True)
       → 但 conRespPipeOutQ 是普通 FIFOF，如果 RQ 不消费则会满
       → RQ 已经被阻塞，无法消费
       → 同时这个 rule 也需要 dsSpliterDataPipeInConverter.enq(ds) 来转发数据
       → 但 enq 到 conRespPipeOutQ 阻塞导致整个 rule 无法执行
       → DMA 数据无法转发
       → DMA 通道被 PayloadCon 占用
       → RQ Ringbuf 的 DMA 请求无法被处理
       → RQ 更加无法消费 conRespPipeOut
       → 死锁！
   ```

### 数据流图示

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          StreamArbiterSlave                              │
│                        (DMA Channel Muxer)                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   ┌──────────────┐                    ┌──────────────────┐              │
│   │  PayloadCon  │ ──── DMA Write ──→ │                  │              │
│   │              │      Request       │   Arbitrated     │ ──→ PCIe    │
│   └──────────────┘                    │   DMA Channel    │              │
│          ↑                            │                  │              │
│          │ conRespPipeOut (BLOCKED!)  └──────────────────┘              │
│          │                                    ↑                          │
│   ┌──────┴───────┐                            │                          │
│   │      RQ      │ ──── DMA Write ──────────→ │ (等待中)                 │
│   │   (已满)     │      Request               │                          │
│   └──────────────┘                                                       │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## 修复方案

### 核心思路

将 `conRespPipeOutQ` 从普通 FIFOF 改为 **ReservedFIFOF**，实现"先预留槽位，后实际入队"的模式，打破阻塞耦合。

### 实现细节

#### 1. 新增 ReservedFIFOF 抽象

```bsv
interface ReservedFIFOF#(type td, numeric type capacity);
    method Action reserve();    // 预留槽位（有条件阻塞）
    method Bool notFull;
    method Bool notEmpty;
    method Action enq(td x);    // 实际入队（无条件，使用已预留的槽位）
    method td first;
    method Action deq;
endinterface
```

**关键设计**：
- `reserve()` 方法：在 `count < capacity - 1` 时才能执行，具有阻塞语义
- `enq()` 方法：无条件执行（只做断言检查），使用之前预留的槽位
- 使用独立的 `count` 寄存器跟踪预留数量，而非依赖 FIFO 本身的 `notFull`

#### 2. 修改 PayloadCon 模块

**变更 1**：将 `conRespPipeOutQ` 类型从 `FIFOF#(Bool)` 改为 `ReservedFIFOF#(Bool, NUMERIC_TYPE_TWO)`

```bsv
// 修改前
FIFOF#(Bool) conRespPipeOutQ <- mkFIFOF;

// 修改后
ReservedFIFOF#(Bool, NUMERIC_TYPE_TWO) conRespPipeOutQ <- mkReservedFIFOF;
```

**变更 2**：在发起 DMA 写请求时预留响应槽位

```bsv
rule getBeatChunkMetaCalculateRespAndIssueAxiWrite;
    // ... 原有逻辑 ...
    dmaWriteReqAddrPipeOutQ.enq(writeReq);
    conRespPipeOutQ.reserve();  // 新增：预留响应槽位
    // ...
endrule
```

**变更 3**：响应入队改为无阻塞操作

```bsv
rule forwardConsumedFinishedSignal;
    let ds = payloadConStreamPipeInQ.first;
    payloadConStreamPipeInQ.deq;
    dsSpliterDataPipeInConverter.enq(ds);  // 数据转发

    if (ds.isLast) begin
        conRespPipeOutQ.enq(True);  // 无阻塞入队（槽位已预留）
    end
endrule
```

### 修复效果

| 阶段 | 修复前行为 | 修复后行为 |
|------|-----------|-----------|
| 接受新写请求 | 无条件接受 | 检查 `conRespPipeOutQ.reserve()` 是否可执行 |
| 转发最后 beat | 可能因 `conRespPipeOutQ.enq` 阻塞 | 无条件成功（槽位已预留） |
| 死锁风险 | 存在 | 消除 |

### 设计原理

通过将阻塞点从"数据转发时"前移到"请求接受时"：

1. **入口控制**：如果 `conRespPipeOutQ` 没有空余槽位，PayloadCon 不会接受新的写请求
2. **出口保证**：一旦写请求被接受，后续的响应入队必然成功
3. **解耦关键路径**：数据转发（`dsSpliterDataPipeInConverter.enq`）不再与响应入队（`conRespPipeOutQ.enq`）存在阻塞耦合

## 涉及文件

- `open-rdma-rtl/src/PayloadGenAndCon.bsv`
  - 新增 `ReservedFIFOF` interface 和 `mkReservedFIFOF` module
  - 新增 `f_ReservedFIFOF_to_PipeOut` 转换函数
  - 修改 `mkPayloadCon` 模块使用 `ReservedFIFOF`

## 测试验证

修复后需验证以下场景：
1. RQ 满载时的数据流是否正常
2. 多通道并发 DMA 写操作是否正常
3. PayloadCon 与 RQ ringbuf 同时发起请求时的行为

## 相关背景

此问题在 loopback 测试中被发现，表现为 RQ meta report descriptor 写入停滞。通过添加 FIFO full 状态监控和数据流追踪，定位到死锁发生在 `StreamArbiterSlave` 的仲裁逻辑与 PayloadCon 响应队列之间的交互。
