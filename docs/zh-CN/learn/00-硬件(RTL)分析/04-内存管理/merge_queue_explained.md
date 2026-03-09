# MergeQueue 详解

## 定义

```rust
#[derive(Default)]
struct MergeQueue {
    send: VecDeque<SendEvent>,           // 发送事件队列
    recv: VecDeque<RecvEvent>,           // 接收事件队列
    recv_read_resp: VecDeque<RecvEvent>, // Read 响应专用队列
}
```

位置：`completion.rs:169-174`

## 核心作用

MergeQueue 是 RDMA 完成处理流水线中的**中间缓冲层**，主要负责：

1. **暂存已确认的事件**：存放已经通过 PSN 确认但尚未生成完成事件的操作
2. **配对 Read 请求和响应**：确保 RDMA Read 操作的请求和响应正确匹配
3. **分离不同类型的接收事件**：将 Read 响应与普通接收操作隔离

## 在完成流水线中的位置

```
完成处理流水线：
┌──────────────────┐
│ 1. MessageTracker│  ← 按 MSN 排序，按 PSN 确认
│  - send tracker  │
│  - recv tracker  │
└────────┬─────────┘
         │ ack_send()/ack_recv()
         │ 将已确认的事件移出
         ↓
┌──────────────────┐
│ 2. MergeQueue    │  ← 配对操作（特别是 Read）
│  - send          │
│  - recv          │
│  - recv_read_resp│
└────────┬─────────┘
         │ pop_send()/pop_recv()
         │ 生成完成事件
         ↓
┌──────────────────┐
│ 3. Completion    │  ← 用户可见的完成事件
│    Queue (CQ)    │
└──────────────────┘
```

## 为什么需要 MergeQueue？

### 问题背景

RDMA 操作有不同的完成语义：

| 操作类型 | 发送端完成条件 | 接收端完成条件 |
|---------|--------------|--------------|
| WRITE | ACK 确认即可 | 数据写入内存 |
| SEND | ACK 确认即可 | 消耗 Recv WR |
| **READ** | **需要收到响应数据** | 发送响应数据 |

**Read 操作的特殊性**：
- 其他操作：单向确认（ACK）就能完成
- Read 操作：需要双向交互（请求 + 响应）才能完成

### 如果没有 MergeQueue 会怎样？

假设直接从 MessageTracker 生成完成事件：

```rust
// 伪代码：没有 MergeQueue 的情况
fn poll_send_completion() -> Option<Completion> {
    let event = self.send.pop()?;  // 直接从 tracker 弹出
    match event.op {
        SendEventOp::ReadSignaled => {
            // 问题：响应在哪里？如何匹配？
            // MessageTracker 只管理一个维度（send 或 recv）
            // 无法跨越 send/recv tracker 进行配对
            ???
        }
    }
}
```

**问题**：
- MessageTracker 各自独立，无法跨 tracker 配对
- 需要一个中间层来协调 send 和 recv 的事件

## MergeQueue 的实现细节

### 1. 推送接收事件：分流处理

```rust
fn push_recv(&mut self, event: RecvEvent) {
    match event.op {
        RecvEventOp::ReadResp => {
            // Read 响应走专用通道
            self.recv_read_resp.push_back(event);
        }
        RecvEventOp::Write
        | RecvEventOp::WriteWithImm { .. }
        | RecvEventOp::Recv
        | RecvEventOp::RecvWithImm { .. }
        | RecvEventOp::RecvRead => {
            // 其他接收事件走普通通道
            self.recv.push_back(event);
        }
    }
}
```

**为什么分流？**
- Read 响应需要与 send 队列中的 Read 请求配对
- 其他接收事件（Write、Recv）与 send 队列无关，独立处理
- 分离避免了队列头部阻塞问题

### 2. 弹出发送事件：配对逻辑

```rust
fn pop_send(&mut self) -> Option<SendEvent> {
    let event = self.send.front()?;
    match event.op {
        SendEventOp::WriteSignaled | SendEventOp::SendSignaled => {
            // Write/Send：直接弹出
            self.send.pop_front()
        }
        SendEventOp::ReadSignaled => {
            // Read：必须等待响应
            self.recv_read_resp
                .pop_front()                    // 1. 尝试弹出响应
                .and_then(|_e| self.send.pop_front())  // 2. 成功则弹出请求
        }
    }
}
```

**配对机制**：
- **FIFO 隐式配对**：第一个 Read 请求配对第一个 Read 响应
- **原子操作**：要么同时弹出请求和响应，要么都不弹出
- **阻塞行为**：如果响应未到，Read 请求保持在队列头部，阻塞后续操作

### 3. 弹出接收事件：简单弹出

```rust
fn pop_recv(&mut self) -> Option<RecvEvent> {
    self.recv.pop_front()
}
```

接收事件（除了 Read 响应）不需要配对，直接弹出即可。

## 典型使用场景

### 场景 1：RDMA Write 操作

```
时间线：
t1: post_send(WRITE)  → SendEvent 插入 send tracker
t2: 收到 ACK          → ack_send() 调用
t3: poll_cq()         → 生成完成事件

详细流程：
1. t2: ack_send(psn)
   └─> send.pop() 弹出 WriteSignaled 事件
       └─> merge.push_send(WriteSignaled)
           └─> WriteSignaled 进入 merge.send 队列

2. t3: poll_send_completion()
   └─> merge.pop_send()
       └─> 匹配 WriteSignaled
           └─> 直接 pop_front()
               └─> 返回 SendEvent
                   └─> 生成 Completion::RdmaWrite
```

**MergeQueue 的作用**：
- 简单的中转缓冲
- 不需要特殊处理

### 场景 2：RDMA Read 操作

```
时间线：
t1: post_send(READ)     → SendEvent(ReadSignaled) 插入 send tracker
t2: 收到 Read 响应数据  → RecvEvent(ReadResp) 插入 recv tracker
t3: 收到 ACK           → ack_send() 和 ack_recv() 调用
t4: poll_cq()          → 生成完成事件

详细流程：
1. t3: ack_send(psn)
   └─> send.pop() 弹出 ReadSignaled 事件
       └─> merge.push_send(ReadSignaled)
           └─> ReadSignaled 进入 merge.send 队列

   ack_recv(psn)
   └─> recv.pop() 弹出 ReadResp 事件
       └─> merge.push_recv(ReadResp)
           └─> ReadResp 进入 merge.recv_read_resp 队列

2. t4: poll_send_completion()
   └─> merge.pop_send()
       └─> 检查队列头：ReadSignaled
           └─> 查找 recv_read_resp.pop_front()
               ├─> 找到 ReadResp ✓
               │   └─> send.pop_front() 弹出 ReadSignaled
               │       └─> 返回 SendEvent
               │           └─> 生成 Completion::RdmaRead
               └─> 未找到 ✗
                   └─> 返回 None（等待响应）
```

**MergeQueue 的作用**：
- **配对协调**：确保请求和响应同时存在才完成
- **顺序保证**：FIFO 配对确保顺序正确
- **阻塞控制**：响应未到时阻塞完成

### 场景 3：混合操作

```
操作序列：
1. post_send(WRITE1)  → MSN=1
2. post_send(READ1)   → MSN=2
3. post_send(WRITE2)  → MSN=3

到达情况：
- WRITE1 ACK 到达
- READ1 ACK 到达，但响应未到
- WRITE2 ACK 到达

MergeQueue 状态：
merge.send: [WriteSignaled(MSN=1), ReadSignaled(MSN=2), WriteSignaled(MSN=3)]
merge.recv_read_resp: []  // 空！

完成顺序：
1. poll_send_completion()
   → pop_send() 成功弹出 WRITE1 ✓
   → 返回 Completion::RdmaWrite

2. poll_send_completion()
   → pop_send() 检查队列头 READ1
   → recv_read_resp 为空 ✗
   → 返回 None（阻塞）

3. poll_send_completion()
   → 仍然阻塞在 READ1

... 直到 READ1 响应到达 ...

4. [READ1 响应到达]
   → ack_recv() → merge.recv_read_resp.push_back(ReadResp)

5. poll_send_completion()
   → pop_send() 成功配对 READ1 + ReadResp ✓
   → 返回 Completion::RdmaRead

6. poll_send_completion()
   → pop_send() 成功弹出 WRITE2 ✓
   → 返回 Completion::RdmaWrite
```

**关键行为**：
- **队列头部阻塞（Head-of-Line Blocking）**：READ1 未完成会阻塞 WRITE2
- **严格顺序性**：确保完成顺序与发起顺序一致
- **配对隔离**：Read 响应不会干扰其他接收事件

## 设计优势

### 1. 分离关注点

```
MessageTracker  → 负责排序和确认（MSN/PSN）
     ↓
MergeQueue     → 负责配对和完成条件检查
     ↓
Completion     → 用户可见的完成事件
```

每层职责清晰，降低复杂度。

### 2. 支持复杂的完成语义

不同操作类型的完成条件差异很大：
- Write/Send：单向确认
- Read：双向配对
- Recv：需要匹配 PostRecvEvent

MergeQueue 提供了灵活的配对机制。

### 3. 保持顺序性

通过队列头部阻塞，强制完成顺序与发起顺序一致：
```
发起顺序：Op1 → Op2 → Op3
完成顺序：Op1 → Op2 → Op3  ✓ 保证一致
```

### 4. 隔离 Read 响应

```rust
recv: VecDeque<RecvEvent>,           // 普通接收
recv_read_resp: VecDeque<RecvEvent>, // Read 响应
```

**为什么分离？**
- Read 响应参与 send 侧的完成逻辑
- 普通接收事件（Write、Recv）参与 recv 侧的完成逻辑
- 两者混在一起会导致逻辑混乱

## 潜在问题

### 队列头部阻塞（Head-of-Line Blocking）

```
场景：
Op1 (READ) → 响应延迟
Op2 (WRITE) → 已完成
Op3 (WRITE) → 已完成

merge.send 状态: [READ, WRITE, WRITE]

结果：
- Op2 和 Op3 已经可以完成
- 但被 Op1 阻塞在队列中
- 用户无法获得 Op2/Op3 的完成事件
```

**这是设计上的权衡**：
- ✓ 优点：严格保证顺序性
- ✗ 缺点：性能损失（阻塞后续操作）

**RDMA 规范要求**：
- 同一 QP 内的操作必须按顺序完成
- 这是协议规定，不是实现缺陷

**缓解方案**（在应用层）：
- 使用多个 QP 并行发送操作
- 避免混合不同延迟特性的操作

## 与 MessageTracker 的配合

```rust
impl QueuePairMessageTracker {
    fn ack_send(&mut self, psn: Psn) {
        // 1. MessageTracker 确认并弹出事件
        self.send.ack(psn);
        while let Some(event) = self.send.pop() {
            // 2. 移动到 MergeQueue
            self.merge.push_send(event);
        }
    }

    fn poll_send_completion(&mut self) -> Option<(SendEvent, Completion)> {
        // 3. 从 MergeQueue 弹出并生成完成事件
        let event = self.merge.pop_send()?;
        let completion = match event.op {
            SendEventOp::ReadSignaled => Completion::RdmaRead { wr_id: event.wr_id },
            // ...
        };
        Some((event, completion))
    }
}
```

**协作流程**：
1. MessageTracker 负责事件的存储、排序、确认
2. 确认后的事件移到 MergeQueue
3. MergeQueue 负责配对和生成完成事件

## 数据流图

```
发送侧：
post_send(WRITE)           post_send(READ)
     ↓                          ↓
SendEvent(WriteSignaled)   SendEvent(ReadSignaled)
     ↓                          ↓
MessageTracker<SendEvent>  MessageTracker<SendEvent>
  [按 MSN 排序]              [按 MSN 排序]
     ↓                          ↓
  收到 ACK                   收到 ACK
     ↓                          ↓
  send.pop()                 send.pop()
     ↓                          ↓
MergeQueue.push_send()     MergeQueue.push_send()
     ↓                          ↓
merge.send 队列            merge.send 队列
     ↓                          ↓
merge.pop_send()           merge.pop_send()
  直接弹出 ✓                  等待响应...
     ↓                               ↓
Completion::RdmaWrite              阻塞
                                     ↓
接收侧：                         收到 Read 响应
NIC 收到 Read 响应                    ↓
     ↓                          RecvEvent(ReadResp)
RecvEvent(ReadResp)                  ↓
     ↓                    MessageTracker<RecvEvent>
MessageTracker<RecvEvent>            ↓
     ↓                          收到 ACK
  收到 ACK                           ↓
     ↓                          recv.pop()
  recv.pop()                         ↓
     ↓                    MergeQueue.push_recv()
MergeQueue.push_recv()               ↓
     ↓                    merge.recv_read_resp 队列
merge.recv_read_resp 队列            ↓
     ↓                          merge.pop_send()
merge.pop_send()                     ↓
  配对成功 ✓ ←───────────────────── 弹出请求+响应
     ↓
Completion::RdmaRead
```

## 总结

### MergeQueue 的核心价值

1. **配对协调器**：为 Read 操作提供请求-响应配对机制
2. **完成条件仲裁器**：根据不同操作类型判断是否满足完成条件
3. **顺序执行器**：通过队列头部阻塞强制顺序完成
4. **隔离器**：分离 Read 响应和普通接收事件

### 关键特性

| 特性 | 实现方式 | 目的 |
|-----|---------|------|
| **双队列接收** | `recv` + `recv_read_resp` | 隔离不同类型的接收事件 |
| **FIFO 配对** | `pop_front()` 顺序匹配 | 确保正确的请求-响应配对 |
| **原子弹出** | `and_then` 组合 | Read 操作要么完全完成要么不完成 |
| **队列头阻塞** | 队列头不满足条件时返回 None | 强制顺序性 |

### 设计哲学

MergeQueue 体现了 RDMA 完成处理的核心挑战：
- **异步性**：请求和响应在不同时间到达
- **顺序性**：必须按发起顺序完成
- **完整性**：Read 操作需要完整的请求-响应对

通过引入中间缓冲层，MergeQueue 优雅地解决了这些挑战。
