# Mock vs Sim 模式下 RCCL 行为差异分析

## 执行摘要

本文档分析了 `mock.rs` (MockDeviceCtx) 和 `ctx.rs` (HwDeviceCtx) 两种实现作为 RCCL 后端时的行为差异。

**关键发现**:
- **Mock 模式**: 测试成功完成，AllReduce 正确执行 (result = 3.0 ✓)
- **Sim 模式**: 测试卡住，最终超时失败，出现连接关闭错误

## 1. 架构对比

### 1.1 Mock 模式 (mock.rs)

**特点**:
- 纯软件模拟，无硬件交互
- 基于 TCP socket 进行 QP 间通信
- 每个 QP 拥有独立线程处理消息
- 同步内存访问和数据传输

**实现方式**:
```
QP1 Thread ←─TCP─→ QP2 Thread
    ↓                   ↓
VecDeque<Completion>  VecDeque<Completion>
```

### 1.2 Sim 模式 (ctx.rs)

**特点**:
- 与硬件模拟器通信
- 通过 CSR (Control and Status Register) 进行硬件配置
- 使用共享 worker 线程池处理任务
- 异步消息传递和完成通知

**实现方式**:
```
RCCL → VerbsOps → CommandConfigurator → CSR (UDP:7701/7702) → RTL Simulator
                     ↓
             Worker Threads (RdmaWriteWorker, CompletionWorker, MetaWorker)
                     ↓
         CompletionQueueTable (shared across QPs)
```

## 2. VerbsOps 接口实现差异

### 2.1 Memory Registration (reg_mr)

| 方面 | Mock 模式 | Sim 模式 |
|------|----------|----------|
| 物理地址解析 | `PhysAddrResolverLinuxX86` 简单解析 | 完整的 MTT (Memory Translation Table) 管理 |
| 页表更新 | 仅记录到内存表 `MrTable` | 通过 DMA buffer 更新硬件 PGT |
| MR Key 生成 | 简单递增 `self.mr_key += 1` | `mtt.register()` 分配 |
| 硬件通信 | ❌ 无 | ✅ `CommandConfigurator::update_mtt()` |
| 地址验证 | 基本验证 | 检查 hugepage + 地址范围 |

**代码示例** (mock.rs:170-197):
```rust
self.mr_table.reg(Mr::new(addr, length));
self.mr_key += 1;
Ok(self.mr_key)
```

**代码示例** (ctx.rs:293-370):
```rust
let (mr_key, pgt_entry) = self.mtt.register(num_pages, virt_addr, length, &umem_handler)?;
self.cmd_controller.update_mtt(mtt_update);
// 分块更新页表，每次最多 16 entries
for PgtEntry { index, count } in chunks(pgt_entry) {
    self.cmd_controller.update_pgt(pgt_update);
}
```

### 2.2 Queue Pair Creation (create_qp)

| 方面 | Mock 模式 | Sim 模式 |
|------|----------|----------|
| 通信机制 | TCP listener (端口 10000+QPN) | 硬件命令队列 |
| 线程模型 | 每 QP 一个专用线程 | 共享 worker 线程池 |
| 消息处理 | 线程内循环 + `flume::channel` | 任务队列 + worker 轮询 |
| 连接建立 | `TcpListener::bind()` | `UpdateQp` 硬件命令 |

**Mock 线程创建** (mock.rs:230-382):
```rust
let handle = thread::spawn(move || loop {
    for task in rx.try_iter() {
        // 处理 LocalTask::PostRecv
    }
    if abort_signal.load(Ordering::Relaxed) { break; }
    let Some(msg) = conn_c.recv::<QpTransportMessage>() else { continue; };
    // 处理 WriteReq, ReadReq, SendReq 等
});
```

**Sim 命令配置** (ctx.rs:377-403):
```rust
let qpn = self.qp_manager.create_qp().ok_or(...)?;
let entry = UpdateQp {
    qpn,
    qp_type: attr.qp_type(),
    ip_addr: 0,
    peer_mac_addr: 0,
    // ...
};
self.cmd_controller.update_qp(entry); // → 硬件
```

### 2.3 Queue Pair Update (update_qp)

**Mock 模式连接流程** (mock.rs:412-446):
1. 设置目标 IP 和 QPN
2. 创建 TCP 连接到 `dqp_ip:get_port(dqpn)`
3. 默认 fallback 到 localhost

```rust
ctx.conn().connect(dqpn, dqp_ip); // 建立 TCP 连接
```

**Sim 模式连接流程** (ctx.rs:405-494):
1. 更新 QP 属性表 (IP, MAC, MTU, access flags)
2. 发送 `UpdateQp` 命令到硬件
3. **在 RTS 状态时**:
   - 创建 `post_recv_channel` (TCP-based)
   - 刷新 pending RecvWr 队列
   - 启动 `RecvWorker` 线程

```rust
if qp.dqpn != 0 && qp.dqp_ip != 0 {
    let (tx, rx) = post_recv_channel::<TcpChannel>(...)?;
    self.post_recv_tx_table.insert(qpn, tx);
    RecvWorker::new(rx, wr_queue).spawn();
}
```

### 2.4 Post Receive (post_recv)

| 方面 | Mock 模式 | Sim 模式 |
|------|----------|----------|
| 状态管理 | ❌ 无状态区分 | ✅ INIT vs RTR/RTS 状态 |
| Pending 队列 | 内置于 QP 线程 | `pending_post_recv_queue` |
| 事件注册 | ❌ 无 | ✅ `CompletionTask::Register` |
| 通道类型 | `flume::channel` | TCP channel + worker |

**Mock** (mock.rs:559-583):
```rust
ctx.local_task_tx.send(LocalTask::PostRecv(PostRecvReq { wr }))
// QP 线程立即处理或缓存 (pending_write_with_imm)
```

**Sim** (ctx.rs:548-577):
```rust
// 注册 PostRecvEvent 到 CompletionWorker
self.completion_tx.send(CompletionTask::Register {
    qpn,
    event: Event::PostRecv(PostRecvEvent::new(qpn, wr.wr_id))
});

if let Some(tx) = self.post_recv_tx_table.get_qp_mut(qpn) {
    // RTR/RTS: 直接发送
    tx.send(wr)?;
} else {
    // INIT: 缓存到 pending 队列
    self.pending_post_recv_queue.get_mut(qpn).push_back(wr);
}
```

### 2.5 Post Send (post_send)

**Mock 模式** (mock.rs:498-557):
- 同步读取本地内存: `read_local_addr(laddr, len)`
- 序列化为 `QpTransportMessage`
- 通过 TCP 直接发送到对端 QP

**Sim 模式** (ctx.rs:520-531):
- Send 操作转换为 RDMA Write (需要对端 RecvWr)
- 提交到 `rdma_write_tx` 任务队列
- `RdmaWriteWorker` 异步处理

```rust
// Sim: Send → RDMA Write 转换
fn send(&self, qpn: u32, mut wr: SendWrBase) -> Result<()> {
    let recv_wr = self.recv_wr_queue_table.pop(qpn)?;
    let wr = SendWrRdma::new_from_base(wr, recv_wr.addr, recv_wr.lkey);
    self.rdma_write(qpn, wr);
}
```

### 2.6 Completion Queue Polling (poll_cq)

**Mock** (mock.rs:482-496):
```rust
cq.pop() // 从内存 VecDeque 直接 pop
```

**Sim** (ctx.rs:533-546):
```rust
cq.pop_front() // 从共享 CompletionQueueTable
// Completion 由多个 worker 协作生成:
// - MetaWorker: 从硬件读取元数据
// - CompletionWorker: 处理事件并生成 completion
```

## 3. RCCL 行为差异分析

### 3.1 Mock 模式执行流程 (成功)

**日志关键点** (rccl-1.log mock):
```
Line 106: device binding to 0.0.0.0:10481
Line 215: dest ip not provided for qp 1512, defaulting to loopback 127.0.0.1
Line 219: connect to dqpn: 420, ip: 127.0.0.1
Line 364: recv msg from connection: WriteReq(...)
Line 366: post send wr: RdmaWriteWithImm
Line 381: recv msg from connection: WriteWithImmReq(...)
Line 384: new completion: RecvRdmaWithImm { wr_id: 0, imm: 32 }
Line 386: ✓ Test PASSED: result[0] = 3.0 (expected 3.0)
```

**时序**:
1. QP 创建 → TCP listener 启动
2. Update QP → TCP connect 建立
3. RDMA Write → TCP send/recv
4. Completion → VecDeque push/pop
5. **测试成功完成** ✓

### 3.2 Sim 模式执行流程 (卡住)

**日志关键点** (rccl-1.log sim):
```
Line 64: create sim ptr is:0x1a94f20
Line 82: new tcp client with pid 3266282, addr is: 127.0.0.1:7003
Line 86: connect to: 127.0.0.1:7701
Line 90-120: send msg write: CsrAccessRpcMessage { is_write: true, ...}
Line 647: start RTS!!!!!!
Line 650: TcpChannelRx bind port 64164
Line 866: Meta Handler got meta = HeaderWriteMeta { ... }
Line 870: send event to completion_tx queue
Line 876: got new desc: AckLocalHw(AckMetaLocalHw { qpn: 384, ...})
Line 879: Failed to get request: Connection closed  ❌
```

**问题点**:
1. ✅ 初始化成功 (CSR 连接到 127.0.0.1:7701)
2. ✅ QP 创建成功
3. ✅ RTS 转换成功
4. ✅ RDMA Write 发送成功
5. ✅ 硬件返回 Meta
6. **❌ 卡在等待 Completion**
7. **❌ Memory proxy 连接超时关闭**

## 4. 根本原因分析

### 4.1 差异点总结

| 组件 | Mock 模式 | Sim 模式 | 影响 |
|------|----------|----------|------|
| **通信路径** | 进程内 TCP | UDP→硬件模拟器→TCP | 延迟 + 复杂度 |
| **状态管理** | 无状态 | 复杂状态机 (INIT→RTR→RTS) | 时序依赖 |
| **事件处理** | 同步 | 异步 worker 协作 | 时序不确定 |
| **Completion 生成** | 直接 push | Meta→Event→Completion 多阶段 | 可能丢失 |
| **RecvWr 匹配** | 队列 pop (FIFO) | CompletionWorker 管理 | 匹配逻辑差异 |

### 4.2 可能的 Bug 点

#### 问题 1: RecvWr 与 WRITE_WITH_IMM 不匹配

**Mock 实现** (mock.rs:284-307):
```rust
QpTransportMessage::WriteWithImmReq(req) => {
    if let Some(recv_req) = recv_reqs.pop_front() {
        // 立即处理
        write_local_addr(&mr_table, req.raddr, &req.data);
        let completion = Completion::RecvRdmaWithImm { wr_id: recv_req.wr.wr_id, imm: req.imm };
        recv_cq.push(completion);
    } else {
        // 缓存消息
        pending_write_with_imm.push_back(req);
    }
}
```

**Sim 实现**:
- WRITE_WITH_IMM 通过 `MetaWorker` 处理
- `CompletionWorker` 需要匹配 `PostRecvEvent`
- **可能存在竞态**: RecvWr 未注册时收到 Meta

#### 问题 2: Completion 事件丢失

**观察**: Sim 日志中看到:
- `send event to completion_tx queue: Recv(RecvEvent {...})`
- 但没有后续的 `poll_cq returned` 日志

**可能原因**:
1. `CompletionWorker` 未正确处理 `RecvEvent`
2. `MetaWorker` 与 `CompletionWorker` 时序冲突
3. QP 状态检查导致 completion 被丢弃

参见 `ctx.rs:548` post_recv 实现:
```rust
// 注册事件
self.completion_tx.send(CompletionTask::Register { qpn, event });

// 问题: 如果 RecvWorker 还未启动 (RTS 之前), 事件可能丢失
```

#### 问题 3: Memory Proxy 超时

**错误日志**:
```
[2025-12-29T22:17:01.642862196Z ERROR] Failed to get request: Connection closed
```

**位置**: `memory_proxy_simple.rs`

**可能原因**:
- RCCL 等待 completion 超时 (默认 120 秒)
- Memory proxy 连接被硬件模拟器关闭
- DMA buffer 访问超时

## 5. 关键差异对 RCCL 的影响

### 5.1 初始化阶段

**Mock**:
- 快速初始化 (~1ms)
- 无硬件依赖

**Sim**:
- 需要连接模拟器 (~1s CSR 握手)
- 需要配置硬件寄存器 (每次 CSR 写入 ~0.5s)

### 5.2 数据传输阶段

**Mock**:
- 直接内存拷贝
- 同步完成
- 延迟 <1ms

**Sim**:
- CSR 命令 → 硬件处理 → Meta 上报 → Worker 处理
- 异步完成
- 延迟 ~1-47s (从日志时间戳推算)

**日志证据**:
```
[22:15:26.624] post_send called (RDMA Write 发送)
[22:15:27.866] Meta Handler got meta (硬件处理完成)
[22:16:15.383] AckLocalHw (ACK 确认)
```
→ 总延迟 ~49 秒

### 5.3 Completion 通知

**Mock**:
- 发送端: 收到 `WriteResp` → 立即 push completion
- 接收端: `WriteWithImmReq` → 匹配 RecvWr → push completion

**Sim**:
- 发送端: `SendEvent` → CompletionWorker 处理 → push
- 接收端: `RecvEvent` → **需要 PostRecvEvent 已注册** → push
  - **问题**: 如果 PostRecvEvent 晚于 RecvEvent, completion 可能丢失

## 6. 建议的修复方向

### 6.1 短期修复

1. **增强 CompletionWorker 日志**:
   ```rust
   // ctx.rs CompletionWorker
   debug!("CompletionWorker processing event: {:?}", event);
   debug!("Current registered events for QP {}: {:?}", qpn, ...);
   ```

2. **检查 RecvWr pending 队列刷新时机**:
   ```rust
   // ctx.rs:466-480
   // 确保在 RecvWorker 启动前刷新所有 pending RecvWr
   ```

3. **添加 Completion 生成跟踪**:
   ```rust
   // workers/completion.rs
   info!("Generated completion for QP {}: {:?}", qpn, completion);
   ```

### 6.2 长期优化

1. **统一 RecvWr 处理逻辑**:
   - Mock 和 Sim 使用相同的 pending 队列机制
   - 避免时序依赖

2. **改进 MetaWorker ↔ CompletionWorker 协作**:
   - 增加事件缓冲区
   - 处理乱序到达的 Meta 和 PostRecvEvent

3. **增加硬件模拟器健康检查**:
   - 定期 ping CSR 连接
   - Memory proxy 连接保活

## 7. 调试建议

### 7.1 验证步骤

1. **在 Sim 模式下添加详细日志**:
   ```bash
   RUST_LOG=blue_rdma_driver::workers::completion=trace,\
   blue_rdma_driver::workers::meta_report=trace \
   ./tests/rccl_test/build/normal_test_nompi 0
   ```

2. **对比 poll_cq 调用**:
   ```rust
   // 检查 Sim 模式下 poll_cq 返回空的原因
   if completions.is_empty() {
       debug!("poll_cq returned empty for CQ {}, table state: {:?}",
              handle, self.cq_table.get_cq(handle));
   }
   ```

3. **检查 CompletionWorker 队列状态**:
   - 是否有积压的 CompletionTask
   - PostRecvEvent 是否正确注册

### 7.2 最小复现

创建简单测试 case:
```rust
// 模拟 RCCL 的 AllReduce 流程
#[test]
fn test_write_with_imm_completion() {
    let mut dev = HwDeviceCtx::new();

    // 1. 创建 QP 和 CQ
    let cq = dev.create_cq()?;
    let qp = dev.create_qp(IbvQpInitAttr { recv_cq: Some(cq), ... })?;

    // 2. Update QP 到 RTS
    dev.update_qp(qp, IbvQpAttr { dest_qp_num: Some(peer_qp), ... })?;

    // 3. Post RecvWr
    dev.post_recv(qp, RecvWr { wr_id: 0, addr: VirtAddr(0), length: 0, ... })?;

    // 4. Post Send (WRITE_WITH_IMM)
    dev.post_send(qp, SendWr::Rdma(SendWrRdma {
        opcode: RdmaWriteWithImm,
        imm: 32,
        ...
    }))?;

    // 5. Poll CQ
    thread::sleep(Duration::from_secs(5)); // 等待硬件处理
    let completions = dev.poll_cq(cq, 10);
    assert_eq!(completions.len(), 1); // ← 这里可能失败
}
```

## 8. 总结

### Mock vs Sim 的本质差异

| 维度 | Mock | Sim |
|------|------|-----|
| **同步性** | 同步 | 异步 |
| **复杂度** | 简单 (单线程逻辑) | 复杂 (多 worker 协作) |
| **确定性** | 高 (顺序执行) | 低 (时序依赖) |
| **调试难度** | 低 | 高 |
| **RCCL 兼容性** | ✅ 完美 | ❌ 存在问题 |

### 核心问题

**Sim 模式下 RCCL AllReduce 卡住的根本原因**:
- WRITE_WITH_IMM 的 completion 未正确生成
- 可能由于 PostRecvEvent 与 RecvEvent 时序不匹配
- CompletionWorker 未正确处理或丢弃了 completion 事件

**验证方法**:
1. 检查 CompletionWorker 是否收到 RecvEvent
2. 检查 PostRecvEvent 是否在 RecvEvent 之前注册
3. 检查 poll_cq 是否从 CQ 中正确读取 completion

---

*文档生成时间: 2025-12-30*
*分析基于: open-rdma-driver commit ca025d5*
