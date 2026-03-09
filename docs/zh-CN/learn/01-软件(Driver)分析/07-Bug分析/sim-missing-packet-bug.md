# Sim 模式第二个 Write 包丢失 Bug 分析

## 问题描述

在 Sim 模式下运行 RCCL 测试时，发送端发送了 **2 个 RDMA Write 包**，但接收端只收到 **1 个 Meta 上报**，导致第二个包的数据丢失，RCCL 测试卡住。

## 详细证据

### 发送端 (QPN 1325)

**第一个 Write 包**:
```
Line 896: post_send called, qpn is 1325
  wr: SendWrRdma {
    laddr: 7fde02051040,
    length: 64,
    send_flags: 2 (IBV_SEND_SIGNALED),
    opcode: RdmaWrite
  }

Line 897: RdmaWriteWorker: write called with sqpn=1325, dqpn=433
Line 900: RdmaWriteWorker: handle write done
Line 902: CSR write (addr: 8, value: 2)  ← 通知硬件
```

**第二个 Write 包**:
```
Line 906: post_send called, qpn is 1325
  wr: SendWrRdma {
    laddr: 7fde02051240,  ← 注意地址不同 (+0x200)
    length: 64,
    send_flags: 0 (UNSIGNALED),
    opcode: RdmaWrite
  }

Line 907: RdmaWriteWorker: write called with sqpn=1325, dqpn=433
Line 908: RdmaWriteWorker: handle write done
Line 909: CSR write (addr: 8, value: 4)  ← 通知硬件
```

### 接收端 (QPN 384)

**只收到一个 Meta**:
```
Line 912: meta report queue got new desc:
  HeaderWrite(HeaderWriteMeta {
    msn: 1,
    psn: Psn(1),
    dqpn: 384,
    total_len: 64,
    raddr: 140591493091904,  ← 转换为 0x7fde02051040 (第一个包的地址!)
    imm: 0,
    header_type: Write
  })

Line 915: send event to completion_tx queue:
  event=Recv(RecvEvent { qpn: 384, op: Write, ... })

Line 916: CompletionWorker got task: Register { ... }
```

**第二个 Write 包 (raddr: 0x7fde02051240) 的 Meta 完全丢失！**

### 时间线分析

```
22:15:26.624 - 发送第一个 Write
22:15:26.625 - 发送第二个 Write
22:15:26.626 - RdmaWriteWorker 处理完毕
22:15:27.866 - 硬件上报第一个 Meta (延迟 ~1.2 秒)
             ↓
            ??? 第二个 Meta 在哪里？
             ↓
22:16:15.383 - 只收到 AckLocalHw (没有第二个包的 Meta)
22:17:01.642 - 超时失败
```

## 问题定位

### 可能的原因

#### 1. 硬件模拟器 Bug (最可能)

**CSR write value 的含义**:
- Line 902: `CSR write (addr: 8, value: 2)` - 第一个包
- Line 909: `CSR write (addr: 8, value: 4)` - 第二个包

value 从 2 变成 4，说明**两个 descriptor 都已经写入硬件**。

**问题**:
- 硬件可能错误地认为这是**同一个消息的两个部分**
- 或者硬件只处理了第一个 descriptor，忽略了第二个
- 或者 Meta 上报队列溢出，第二个 Meta 被丢弃

#### 2. Descriptor 生成问题

**检查点**:
```rust
// workers/send/ 相关代码
// 需要确认每个 write 是否生成了独立的 descriptor
```

可能的问题：
- 两个 Write 被错误地合并成一个 descriptor
- MSN (Message Sequence Number) 分配有误
- PSN (Packet Sequence Number) 冲突

#### 3. Meta 上报队列问题

`MetaReportQueueHandler` 可能的问题：
- 队列大小限制
- Ring buffer 覆盖
- 读取逻辑错误

## 对比 Mock 模式

Mock 模式下，两个 Write 正确处理：

```
Line 370: post send wr: RdmaWrite (laddr: 79705cc51040)
Line 373: post send wr: RdmaWrite (laddr: 79705cc51240)
Line 367: recv WriteReq (第一个包到达)
Line 374: recv WriteResp (第一个包 ACK)
(第二个包虽然 unsignaled，仍然正确传输)
```

**关键差异**:
- Mock: 通过 TCP 直接传输，**每个 Write 独立处理**
- Sim: 通过硬件队列，**依赖硬件上报 Meta**

## 调试建议

### 1. 增加 SendWorker 日志

```rust
// workers/send/mod.rs
info!("SendWorker generated descriptor for QP {}, MSN: {}, PSN: {}, addr: 0x{:x}",
      qpn, msn, psn, laddr);
```

### 2. 检查硬件模拟器日志

```bash
# 查看模拟器是否收到两个 descriptor
grep "descriptor\|write request" simulator.log

# 检查 Meta 上报
grep "meta report\|HeaderWrite" simulator.log
```

### 3. 验证 MetaReportQueue

```rust
// workers/meta_report/types.rs
pub fn try_recv_meta(&mut self) -> Option<ReportMeta> {
    let meta = ...; // 读取 Meta
    debug!("MetaReportQueue: read meta at index {}: {:?}", index, meta);
    Some(meta)
}
```

### 4. 检查 MSN/PSN 分配

```bash
# 查看日志中的 MSN 和 PSN
grep "msn:\|psn:" rccl-1.log
```

**预期**:
- 第一个 Write: msn: 0, psn: 0
- 第二个 Write: msn: 1, psn: 1

**实际**:
- CompletionWorker Register: `msn: 0, psn: Psn(1)`
- HeaderWrite Meta: `msn: 1, psn: Psn(1)`

→ **MSN 不匹配！这可能是线索**

## 最小复现测试

```rust
#[test]
fn test_two_consecutive_writes() {
    let mut dev = HwDeviceCtx::new();

    // Setup QP
    let cq = dev.create_cq()?;
    let qp = dev.create_qp(IbvQpInitAttr { recv_cq: Some(cq), ... })?;
    dev.update_qp(qp, IbvQpAttr { dest_qp_num: Some(peer_qp), ... })?;

    // Register MR
    let buf = vec![0u8; 256];
    let mr = dev.reg_mr(buf.as_ptr() as u64, 256, pd, ACCESS_FLAGS)?;

    // 发送两个连续的 Write
    dev.post_send(qp, SendWr::Rdma(SendWrRdma {
        base: SendWrBase {
            laddr: VirtAddr::new(buf.as_ptr() as u64),
            length: 64,
            send_flags: 2, // SIGNALED
            opcode: RdmaWrite,
            ...
        },
        raddr: RemoteAddr::new(remote_buf),
        rkey: remote_mr,
    }))?;

    dev.post_send(qp, SendWr::Rdma(SendWrRdma {
        base: SendWrBase {
            laddr: VirtAddr::new(buf.as_ptr() as u64 + 64),
            length: 64,
            send_flags: 0, // UNSIGNALED
            opcode: RdmaWrite,
            ...
        },
        raddr: RemoteAddr::new(remote_buf + 64),
        rkey: remote_mr,
    }))?;

    // 等待硬件处理
    thread::sleep(Duration::from_secs(5));

    // 检查接收端是否收到两个 Meta
    // 预期：两个 HeaderWrite Meta
    // 实际：只有一个！
}
```

## 临时 Workaround

在找到根本原因之前，可以尝试：

### 1. 强制 SIGNALED

修改 RCCL 或驱动，**所有 Write 都设置 IBV_SEND_SIGNALED**：
```rust
// ctx.rs post_send
let mut wr = wr;
wr.base.send_flags |= ibverbs_sys::ibv_send_flags::IBV_SEND_SIGNALED.0;
```

**缺点**: 性能下降

### 2. 批处理间延迟

在两个 Write 之间增加延迟：
```rust
// workers/rdma.rs
fn handle_write(&mut self, task: RdmaWriteTask) {
    // ... 发送 descriptor
    thread::sleep(Duration::from_millis(10)); // 给硬件时间处理
}
```

**缺点**: 大幅降低吞吐量

### 3. 限制发送队列深度

```rust
// 发送端限制
if pending_sends > 1 {
    wait_for_completion();
}
```

## 下一步行动

1. **紧急**: 检查硬件模拟器代码
   - Meta 上报逻辑
   - Descriptor 读取逻辑
   - MSN/PSN 处理

2. **重要**: 增强日志
   - SendWorker descriptor 生成
   - MetaReportQueue 读取
   - 硬件侧 Meta 写入

3. **验证**: 运行最小复现测试
   - 隔离问题到具体模块

4. **修复**: 根据证据修复 bug
   - 可能在硬件模拟器
   - 可能在 descriptor 生成
   - 可能在 Meta 队列管理

## 总结

**问题**: Sim 模式下第二个 RDMA Write 包的 Meta 丢失

**影响**: RCCL AllReduce 卡住，测试失败

**根本原因**: 待确认，最可能是硬件模拟器未正确处理多个 descriptor

**证据确凿度**: ★★★★★ (日志明确显示两个发送，一个上报)

**修复优先级**: P0 (阻塞 RCCL 测试)

---

*文档生成时间: 2025-12-30*
*发现者: 用户观察 sim 日志*
*分析者: Claude Code*