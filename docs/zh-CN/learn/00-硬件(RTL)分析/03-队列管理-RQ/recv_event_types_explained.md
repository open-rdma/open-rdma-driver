# RecvEventOp 接收操作类型详解

## 完整定义

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RecvEventOp {
    Write,                    // 收到 RDMA WRITE
    WriteWithImm { imm: u32 },// 收到 RDMA WRITE WITH IMMEDIATE
    Recv,                     // 收到 SEND
    RecvWithImm { imm: u32 }, // 收到 SEND WITH IMMEDIATE
    ReadResp,                 // 收到 READ RESPONSE（作为 Read 发起方）
    RecvRead,                 // 收到 READ REQUEST（作为 Read 响应方）
}
```

位置：`completion.rs:413-421`

## 核心概念

### RecvEvent 表示什么？

`RecvEvent` 在**接收端**生成，代表 NIC 收到某种类型的 RDMA 包的元数据：

```rust
pub(crate) struct RecvEvent {
    pub(crate) qpn: u32,        // 哪个 QP 收到的
    pub(crate) op: RecvEventOp, // 收到的操作类型
    pub(crate) meta: MessageMeta, // 消息元数据（MSN、PSN）
    pub(crate) ack_req: bool,   // 是否需要发送 ACK
}
```

**关键点**：
- RecvEvent 是 NIC 硬件或驱动在收到包时生成的
- 不是所有接收操作都会生成用户可见的完成事件（Completion）
- 有些操作需要匹配用户预先 post 的 Recv WR

## 三种操作详解

### 1. RecvEventOp::Recv - 收到 SEND 操作

#### RDMA SEND 操作概述

```
发送端（Sender）              接收端（Receiver）
     |                              |
post_send(SEND)                post_recv()
  wr_id=100                      wr_id=200
     |                              |
     |------ SEND packet -------->  |
     |                              |
     |                        NIC 收到包
     |                              |
     |                     生成 RecvEvent(Recv)
     |                              |
     |                     匹配 post_recv WR
     |                              |
     |<------- ACK --------------|  |
     |                              |
生成 Send Completion         生成 Recv Completion
  wr_id=100                      wr_id=200
```

#### 关键特性

- **双向完成**：发送端和接收端都会生成完成事件
- **需要 Recv WR**：接收端必须预先 post_recv，否则会出错
- **数据传输**：将数据从发送端拷贝到接收端的 Recv WR 指定的缓冲区

#### 代码中的处理

```rust
fn poll_recv_completion(&mut self) -> Option<(RecvEvent, Option<Completion>)> {
    let event = self.merge.pop_recv()?;
    let completion = match event.op {
        RecvEventOp::Recv => {
            // 消耗一个预先 post 的 Recv WR
            let x = self.post_recv_queue.pop_front().expect("no posted recv wr");
            Some(Completion::Recv {
                wr_id: x.wr_id,  // 使用 Recv WR 的 wr_id，不是包里的
                imm: None,
            })
        }
        // ...
    };
    Some((event, completion))
}
```

**处理流程**：
1. 从 `merge.recv` 队列弹出 `RecvEvent(Recv)`
2. 从 `post_recv_queue` 弹出一个预先 post 的 Recv WR
3. 生成 `Completion::Recv`，使用 Recv WR 的 `wr_id`

#### 使用场景

```c
// 发送端
struct ibv_sge sge = {
    .addr = (uintptr_t)send_buffer,
    .length = 1024,
    .lkey = send_mr->lkey,
};
struct ibv_send_wr wr = {
    .wr_id = 100,
    .sg_list = &sge,
    .num_sge = 1,
    .opcode = IBV_WR_SEND,  // SEND 操作
};
ibv_post_send(qp, &wr, &bad_wr);

// 接收端
struct ibv_sge recv_sge = {
    .addr = (uintptr_t)recv_buffer,
    .length = 1024,
    .lkey = recv_mr->lkey,
};
struct ibv_recv_wr recv_wr = {
    .wr_id = 200,
    .sg_list = &recv_sge,
    .num_sge = 1,
};
ibv_post_recv(qp, &recv_wr, &bad_recv_wr);
```

**结果**：
- `send_buffer` 的数据被拷贝到 `recv_buffer`
- 发送端完成事件：`wr_id=100, opcode=IBV_WC_SEND`
- 接收端完成事件：`wr_id=200, opcode=IBV_WC_RECV`

### 2. RecvEventOp::RecvWithImm - 收到 SEND WITH IMMEDIATE

#### 与普通 SEND 的区别

```
SEND                          SEND WITH IMMEDIATE
  |                                  |
数据传输                          数据传输 + 立即数
  |                                  |
Recv Completion                 Recv Completion
  imm: None                        imm: Some(0x12345678)
```

**立即数（Immediate Data）**：
- 4 字节的元数据，随包传输
- 不占用数据缓冲区，直接在完成事件中返回
- 常用于传递小型控制信息（如消息类型、长度等）

#### 代码中的处理

```rust
RecvEventOp::RecvWithImm { imm } => {
    let x = self.post_recv_queue.pop_front().expect("no posted recv wr");
    Some(Completion::Recv {
        wr_id: x.wr_id,
        imm: Some(imm),  // 立即数包含在完成事件中
    })
}
```

**与 Recv 的唯一区别**：完成事件中的 `imm` 字段有值

#### 使用场景示例

```c
// 发送端
struct ibv_send_wr wr = {
    .wr_id = 100,
    .opcode = IBV_WR_SEND_WITH_IMM,
    .imm_data = htonl(0x12345678),  // 立即数
    // ...
};

// 接收端
ibv_post_recv(qp, &recv_wr, &bad_wr);

// 收到完成事件
struct ibv_wc wc;
ibv_poll_cq(cq, 1, &wc);
// wc.imm_data == 0x12345678
// wc.opcode == IBV_WC_RECV
```

**典型应用**：
- 传递消息长度（避免接收端猜测）
- 传递消息类型（多路复用）
- 传递序列号或事务 ID

### 3. RecvEventOp::RecvRead - 收到 READ REQUEST

#### RDMA READ 的双向视角

```
发起端（Initiator）             响应端（Responder）
     |                              |
post_send(READ)                   (无需 post)
  读取远端内存                        |
     |                              |
     |------ READ REQUEST ------->  |
     |                              |
     |                        NIC 收到请求
     |                              |
     |                     生成 RecvEvent(RecvRead)
     |                              |
     |                        从本地内存读数据
     |                              |
     |<----- READ RESPONSE -------|  |
     |                              |
NIC 收到响应                         |
     |                              |
生成 RecvEvent(ReadResp)            |
     |                              |
生成 Read Completion              (无 Completion)
  wr_id=100
```

#### 关键特性

- **单向完成**：只有发起端生成完成事件，响应端不生成
- **不需要 Recv WR**：响应端无需预先 post_recv
- **单边操作**：响应端的 CPU 不参与（NIC 直接从内存读取）

#### 代码中的处理

```rust
RecvEventOp::RecvRead | RecvEventOp::Write => None,
```

**关键点**：
- `RecvRead` **不生成用户可见的完成事件**
- 返回 `None` 表示这个接收事件不产生 Completion
- 响应端的 NIC 自动处理 Read 请求，无需应用层干预

#### 为什么需要 RecvEvent(RecvRead)？

虽然不生成用户完成事件，但 RecvEvent 仍然有用：

1. **统计和监控**：
   - 跟踪 QP 的接收活动
   - 统计 Read 请求次数

2. **ACK 管理**：
   ```rust
   if event.ack_req {
       self.ack_resp_tx.send(AckResponse::Ack {
           qpn,
           msn: event.meta().msn,
           last_psn: event.meta().end_psn,
       });
   }
   ```
   - 即使不生成完成事件，仍需发送 ACK

3. **顺序跟踪**：
   - 更新 PSN 和 MSN
   - 确保后续操作的顺序正确

#### 使用场景示例

```c
// 发起端
struct ibv_sge sge = {
    .addr = (uintptr_t)local_buffer,
    .length = 1024,
    .lkey = local_mr->lkey,
};
struct ibv_send_wr wr = {
    .wr_id = 100,
    .opcode = IBV_WR_RDMA_READ,
    .sg_list = &sge,
    .num_sge = 1,
    .wr.rdma = {
        .remote_addr = remote_addr,  // 远端地址
        .rkey = remote_rkey,          // 远端密钥
    },
};
ibv_post_send(qp, &wr, &bad_wr);

// 响应端
// 无需任何代码！NIC 自动处理

// 发起端收到完成
struct ibv_wc wc;
ibv_poll_cq(cq, 1, &wc);
// wc.wr_id == 100
// wc.opcode == IBV_WC_RDMA_READ
// local_buffer 已填充远端数据
```

## 三种操作的对比

| 操作 | 需要 Recv WR | 接收端完成 | 数据方向 | CPU 参与 | 典型用途 |
|-----|------------|-----------|---------|---------|---------|
| **Recv** | ✓ 是 | ✓ 生成 | Send → Recv | 应用层处理 | 消息传递、RPC |
| **RecvWithImm** | ✓ 是 | ✓ 生成 | Send → Recv + imm | 应用层处理 | 带元数据的消息 |
| **RecvRead** | ✗ 否 | ✗ 不生成 | Resp → Init | 仅 NIC 处理 | 单边内存读取 |

## 为什么 RecvRead 不需要 Recv WR？

### 对比：SEND vs READ

```
SEND 操作：
发送端决定：发什么
接收端决定：放哪里（通过 Recv WR 指定缓冲区）
→ 需要接收端预先准备（post_recv）

READ 操作：
发起端决定：读什么（remote_addr）、放哪里（local_addr）
响应端无需决定任何事情
→ 不需要接收端参与
```

### 内存注册的作用

```c
// 响应端预先注册内存区域
struct ibv_mr *mr = ibv_reg_mr(pd, buffer, size,
    IBV_ACCESS_REMOTE_READ | IBV_ACCESS_LOCAL_WRITE);

// 将 mr->rkey 告知发起端（带外通信）
// 发起端使用 rkey 进行 Read 操作
```

**安全性**：
- 只有注册为 `REMOTE_READ` 的内存可以被读取
- 未注册的内存无法访问（NIC 会拒绝）
- 不需要每次 post_recv，但需要预先注册

## 完成事件的生成流程

### Recv 和 RecvWithImm

```rust
fn poll_recv_completion(&mut self) -> Option<(RecvEvent, Option<Completion>)> {
    let event = self.merge.pop_recv()?;
    let completion = match event.op {
        RecvEventOp::Recv => {
            // 1. 弹出预先 post 的 Recv WR
            let x = self.post_recv_queue.pop_front().expect("no posted recv wr");
            // 2. 生成完成事件
            Some(Completion::Recv {
                wr_id: x.wr_id,
                imm: None,
            })
        }
        RecvEventOp::RecvWithImm { imm } => {
            let x = self.post_recv_queue.pop_front().expect("no posted recv wr");
            Some(Completion::Recv {
                wr_id: x.wr_id,
                imm: Some(imm),  // 包含立即数
            })
        }
        RecvEventOp::RecvRead | RecvEventOp::Write => None,  // 不生成完成
        // ...
    };
    Some((event, completion))
}
```

### 完成事件的调用路径

```
CompletionWorker::process(AckRecv)
  └─> tracker.ack_recv(base_psn)
      └─> recv.pop() → merge.push_recv()
          └─> poll_recv_completion()
              ├─> Recv/RecvWithImm → 生成 Completion ✓
              └─> RecvRead/Write → 返回 None ✗
                  └─> recv_cq.push_back(completion) [跳过]
```

## Post Recv Queue 的作用

```rust
struct QueuePairMessageTracker {
    // ...
    post_recv_queue: VecDeque<PostRecvEvent>,  // 预先 post 的 Recv WR
}
```

### 为什么需要这个队列？

**问题**：Recv WR 和 RecvEvent 的顺序匹配

```
时间线：
t1: post_recv(wr_id=1)
t2: post_recv(wr_id=2)
t3: post_recv(wr_id=3)
t4: 收到 SEND #1 → RecvEvent
t5: 收到 SEND #2 → RecvEvent
t6: 收到 SEND #3 → RecvEvent

匹配：
RecvEvent #1 → Recv WR wr_id=1 ✓
RecvEvent #2 → Recv WR wr_id=2 ✓
RecvEvent #3 → Recv WR wr_id=3 ✓
```

**FIFO 匹配规则**：
- 第一个 RecvEvent 匹配第一个 Recv WR
- 第二个 RecvEvent 匹配第二个 Recv WR
- 依此类推

### 代码实现

```rust
impl QueuePairMessageTracker {
    fn append(&mut self, event: Event) {
        match event {
            Event::PostRecv(x) => {
                // 用户调用 post_recv 时，加入队列
                self.post_recv_queue.push_back(x);
            }
            // ...
        }
    }

    fn poll_recv_completion(&mut self) -> Option<(RecvEvent, Option<Completion>)> {
        // 收到 RecvEvent 时，从队列头部弹出匹配
        let x = self.post_recv_queue.pop_front().expect("no posted recv wr");
        // ...
    }
}
```

## 典型错误场景

### 错误 1：未 post_recv 就收到 SEND

```rust
// 接收端未调用 post_recv
// 发送端发送 SEND

// 结果：
self.post_recv_queue.pop_front().expect("no posted recv wr");
// ↑ Panic! 队列为空
```

**正确做法**：
```c
// 接收端必须预先 post_recv
ibv_post_recv(qp, &recv_wr, &bad_wr);
```

### 错误 2：post_recv 不足

```rust
时间线：
t1: post_recv(wr_id=1)  // 只 post 了 1 个
t2: 收到 SEND #1 → 匹配成功 ✓
t3: 收到 SEND #2 → Panic! 没有更多 Recv WR
```

**正确做法**：
```c
// 保持足够的 Recv WR 在队列中
for (int i = 0; i < NUM_RECVS; i++) {
    ibv_post_recv(qp, &recv_wrs[i], &bad_wr);
}
```

### 错误 3：对 READ 操作 post_recv

```c
// 错误：READ 不需要 Recv WR
ibv_post_recv(qp, &recv_wr, &bad_wr);  // 浪费

// 远端发起 READ
// 结果：Recv WR 未被消耗，一直在队列中
```

## 数据流图

### SEND 操作（Recv）

```
发送端                    接收端
  |                         |
post_send(SEND)         post_recv()
  ↓                         ↓
SendEvent              PostRecvEvent
  ↓                         ↓
MessageTracker         post_recv_queue
  ↓                         ↓
ACK 确认                   收到包
  ↓                         ↓
merge.send             RecvEvent(Recv)
  ↓                         ↓
Completion::Send      MessageTracker
                            ↓
                       ACK 确认
                            ↓
                      merge.recv
                            ↓
                   poll_recv_completion()
                            ↓
                   pop post_recv_queue
                            ↓
                   Completion::Recv
```

### READ 操作（RecvRead）

```
发起端                    响应端
  |                         |
post_send(READ)         (无需 post)
  ↓                         |
SendEvent                  收到请求
  ↓                         ↓
等待响应              RecvEvent(RecvRead)
  ↓                         ↓
收到响应              不生成 Completion
  ↓                         ↓
RecvEvent(ReadResp)     发送 ACK（可选）
  ↓
配对成功
  ↓
Completion::RdmaRead
```

## 总结

### 三种操作的本质

1. **Recv**：消息接收操作
   - 需要接收端预先准备（post_recv）
   - 双向交互（发送+接收都有完成事件）
   - 应用层参与

2. **RecvWithImm**：带元数据的消息接收
   - 与 Recv 相同，但额外传递 4 字节立即数
   - 立即数不占缓冲区，在完成事件中返回

3. **RecvRead**：READ 请求的接收
   - 单边操作，响应端无需准备
   - 只有 NIC 参与，CPU 无需干预
   - 不生成接收端完成事件

### 设计理念

- **SEND 类操作**（Recv、RecvWithImm）：推送模型，接收端决定数据放哪里
- **READ 类操作**（RecvRead）：拉取模型，发起端决定读什么、放哪里

### 为什么有 RecvRead 但看起来没用？

虽然不生成用户完成事件，但 RecvRead 在驱动内部仍然重要：
- ACK 处理
- 顺序跟踪（PSN/MSN）
- 统计和调试

这体现了事件系统的完整性：所有接收到的包都生成 RecvEvent，即使有些不产生用户可见的完成事件。