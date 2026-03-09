# RDMA Bitmap 追踪机制深度分析

本文档详细分析了 open-rdma 驱动中的 bitmap 追踪机制，包括软件驱动和硬件实现。

## 目录

1. [概述](#概述)
2. [Bitmap 追踪方向](#bitmap-追踪方向)
3. [硬件架构](#硬件架构)
4. [QP 与硬件通道映射](#qp-与硬件通道映射)
5. [并发处理机制](#并发处理机制)
6. [数据流详解](#数据流详解)

---

## 概述

RDMA 可靠传输需要追踪哪些数据包已经成功发送/接收，以支持重传和确认机制。Open-RDMA 使用 128 位 bitmap 来高效追踪 PSN（Packet Sequence Number）的接收状态。

### 关键发现

- **双向追踪**：bitmap 同时追踪发送和接收方向
- **硬件生成**：bitmap 在硬件中动态生成并随 ACK/NAK 描述符传递
- **基于 QP 索引**：每个 QP 在硬件 BRAM 中有独立的 bitmap 条目
- **并发安全**：通过仲裁器和互斥锁机制保证多通道并发访问的正确性

---

## Bitmap 追踪方向

### 1. 接收方向（本地硬件 bitmap）

**位置**：`workers/meta_report/worker.rs:115-122`

```rust
fn handle_ack_local_hw(&mut self, meta: AckMetaLocalHw) -> Option<()> {
    // 使用 recv_table (LocalAckTracker)
    let tracker = self.recv_table.get_qp_mut(meta.qpn)?;

    // 使用本地硬件生成的 bitmap 更新追踪器
    if let Some(psn) = tracker.ack_bitmap(meta.psn_now, meta.now_bitmap) {
        self.receiver_updates(meta.qpn, psn);
    }
    Some(())
}
```

**用途**：
- 追踪**本地接收到的包**的 PSN
- 本地硬件生成 ACK/NAK 时，bitmap 描述本地接收状态
- 用于告诉对端哪些包已成功接收

**数据结构**：
```rust
pub(super) recv_table: QpTable<LocalAckTracker>,  // 每个 QP 的接收追踪器
```

### 2. 发送方向（对端硬件 bitmap）

**位置**：`workers/meta_report/worker.rs:146-168`

```rust
fn handle_nak_remote_hw(&mut self, meta: NakMetaRemoteHw) -> Option<()> {
    // 使用 send_table (RemoteAckTracker)
    let tracker = self.send_table.get_qp_mut(meta.qpn)?;

    // 使用对端硬件发来的 bitmap 更新追踪器
    if let Some(psn) = tracker.nak_bitmap(
        meta.msn,
        meta.psn_pre,
        meta.pre_bitmap,    // 对端的 bitmap
        meta.psn_now,
        meta.now_bitmap,    // 对端的 bitmap
    ) {
        self.sender_updates(meta.qpn, psn);
    }

    // 触发重传
    self.packet_retransmit_tx.send(PacketRetransmitTask::RetransmitRange {
        qpn: meta.qpn,
        psn_low: meta.psn_pre,
        psn_high: meta.psn_now + 128,
    });

    Some(())
}
```

**用途**：
- 追踪**本地发送的包**在对端的接收状态
- 对端硬件生成 NAK 时，bitmap 描述对端的接收缺口
- 用于确定哪些包需要重传

**数据结构**：
```rust
pub(super) send_table: QpTable<RemoteAckTracker>,  // 每个 QP 的发送追踪器
```

### 3. ACK/NAK 类型对比

| ACK/NAK 类型 | 生成位置 | 追踪表 | Bitmap 含义 | 是否有 Bitmap |
|-------------|---------|--------|------------|--------------|
| `AckLocalHw` | 本地硬件 | `recv_table` | 本地接收到的 PSN | ✅ 有 (`now_bitmap`) |
| `NakLocalHw` | 本地硬件 | `recv_table` | 本地接收到的 PSN | ✅ 有 (`pre_bitmap`, `now_bitmap`) |
| `NakRemoteHw` | 对端硬件 | `send_table` | 对端接收本地发送包的状态 | ✅ 有 (`pre_bitmap`, `now_bitmap`) |
| `AckRemoteDriver` | 对端驱动 | `send_table` | N/A | ❌ 无 bitmap |
| `NakRemoteDriver` | 对端驱动 | `send_table` | N/A | ❌ 无 bitmap |

---

## 硬件架构

### 1. 四个 Meta Report 队列

**位置**：`workers/meta_report/types.rs:33-38`

```rust
pub(crate) struct MetaReportQueueHandler<Dev: DeviceAdaptor> {
    /// All four meta report queues
    inner: Vec<MetaReportQueueCtx<Dev>>,
    /// Current position, used for round robin polling
    pos: usize,
}
```

硬件有 **4 个 meta report 队列**，驱动通过 **round-robin** 方式轮询这些队列。

### 2. ACK 描述符结构

**位置**：`descriptors/meta_report.rs:428-468`

```rust
struct MetaReportQueueAckDescChunk0 {
    pub now_bitmap_low: u64,   // bitmap 低 64 位
}

struct MetaReportQueueAckDescChunk1 {
    pub now_bitmap_high: u64,  // bitmap 高 64 位
}

struct MetaReportQueueAckDescChunk2 {
    pub msn: u16,
    pub qpn: u24,              // ← 每个描述符都包含 QPN！
    pub psn_now: u24,
}
```

**关键点**：
- 每个 ACK/NAK 描述符都包含 **QPN 字段**
- Bitmap 数据（128 位）嵌入在描述符中
- 硬件不需要为每个 QP 维护独立队列

### 3. 硬件 Bitmap 存储

**位置**：`open-rdma-rtl/src/PsnContinousChecker.bsv:100-102`

```bsv
Vector#(NUMERIC_TYPE_TWO, AutoInferBramQueuedOutput#(tRowAddr, BitmapWindowStorageEntry#(tData, tBoundary))) storage = newVector;
storage[0] <- mkAutoInferBramQueuedOutput(False, "", "mkBitmapWindowStorage 0");
storage[1] <- mkAutoInferBramQueuedOutput(False, "", "mkBitmapWindowStorage 1");
```

**存储结构**：
- 使用 **2 个 BRAM**（块存储器）
- 索引是 **`IndexQP`**（从 QPN 中提取）
- 每个 QP 有独立的 bitmap 条目

**条目结构**：
```bsv
typedef struct {
    tData         data;          // Bitmap 数据（128位）
    tBoundary     leftBound;     // 窗口左边界（PSN）
    KeyQP         qpnKeyPart;    // QPN 的 key 部分（用于验证）
} BitmapWindowStorageEntry
```

### 4. Bitmap 更新流程（硬件）

```
┌─────────────────────────────────────┐
│ Stage 1: 接收请求                    │
│ - 提取 IndexQP(QPN)                 │
│ - 读取 BRAM[IndexQP]                │
│ - 计算 PSN 的 one-hot bitmap        │
└────────┬────────────────────────────┘
         ↓
┌─────────────────────────────────────┐
│ Stage 2: 合并 bitmap                │
│ - oldBitmap = BRAM[IndexQP].data    │
│ - newBitmap = oldBitmap | (1<<PSN)  │ ← OR 操作合并
│ - 检测窗口滑动和丢包                │
└────────┬────────────────────────────┘
         ↓
┌─────────────────────────────────────┐
│ Stage 3: 写回 BRAM                  │
│ - BRAM[IndexQP] = newEntry          │
│ - 生成 ACK/NAK 描述符               │
└─────────────────────────────────────┘
```

**位置**：`open-rdma-rtl/src/PsnContinousChecker.bsv:142-293`

**关键逻辑**（第259行）：
```bsv
newEntry.data = newEntry.data | newestAlreadyExistEntry.data;
```

---

## QP 与硬件通道映射

### 1. 硬件通道配置

**位置**：`open-rdma-rtl/src/Settings.bsv:28`

```bsv
typedef 4 HARDWARE_QP_CHANNEL_CNT;
```

硬件有 **4 个 QP 通道**，每个通道包含独立的：
- RQ（接收队列）
- SQ（发送队列）
- Meta Report 描述符输出

### 2. 包分发策略

**位置**：`open-rdma-rtl/src/vendor/altera/FTileMacAdaptor.bsv:684-747`

```bsv
// 基于负载均衡的分发
if (isCurrentPacketNotEnd) begin
    // 包未结束，继续使用当前通道
    outputEntryVec[0].targetChannelIdx = currentOutputChannelIdx;
end
else begin
    // 新包开始，选择最空闲的通道
    currentOutputChannelIdx = dispatchOrderWithoutCurrentChannel[0];
    outputEntryVec[0].targetChannelIdx = currentOutputChannelIdx;
end
```

**分发规则**：
1. **基于包（packet）级别**，不是基于 QPN
2. **负载均衡**：根据各通道缓冲区使用率动态选择
3. **包级别保证**：同一个包的所有片段发送到同一通道
4. **同一 QP 的不同包可能在不同通道处理**

### 3. 架构图

```
网络包接收
    ↓
┌──────────────────────────────────────┐
│ 负载均衡分发器（基于缓冲区使用率）     │
└────────┬─────────────────────────────┘
         ↓
┌────────────────────────────────────┐
│ RQ Channel 0 │ RQ Channel 1        │
│ RQ Channel 2 │ RQ Channel 3        │  ← 4 个接收通道
└────────┬───────────────────────────┘
         ↓ (各自独立处理包)
┌────────────────────────────────────┐
│ 生成 AutoAckGeneratorReq           │
│ {QPN, PSN, QPC}                    │
└────────┬───────────────────────────┘
         ↓
┌────────────────────────────────────┐
│ Round-Robin 仲裁器                 │  ← 串行化请求
└────────┬───────────────────────────┘
         ↓ (一次只处理一个请求)
┌────────────────────────────────────┐
│ AutoAckGenerator (单例)            │
│ └─ BitmapWindowStorage             │
│    ├─ BRAM[0..MAX_QP]              │
│    └─ bitmapUpdateBusyReg          │  ← 互斥锁
└────────┬───────────────────────────┘
         ↓
┌────────────────────────────────────┐
│ Meta Report 队列 (4个)             │
└────────────────────────────────────┘
         ↓
┌────────────────────────────────────┐
│ 驱动 (Round-Robin 轮询)            │
└────────────────────────────────────┘
```

---

## 并发处理机制

### 问题场景

```
时刻 T1: 包1 (QPN=12345, PSN=100) → RQ Channel 0
时刻 T2: 包2 (QPN=12345, PSN=101) → RQ Channel 2

两个通道同时要更新 QPN=12345 的 bitmap！
```

### 解决方案：三层保护

#### 第一层：仲裁器串行化

**位置**：`open-rdma-rtl/src/Top.bsv:819, 843`

```bsv
// 创建仲裁器
SimpleRoundRobinPipeArbiter#(HARDWARE_QP_CHANNEL_CNT, AutoAckGeneratorReq)
    autoAckGeneratorReqArbiter <- mkSimpleRoundRobinPipeArbiter(...);

// 连接：4个通道 → 仲裁器 → 1个 AutoAckGenerator
mkConnection(autoAckGeneratorReqArbiter.pipeOut, autoAckGenerator.reqPipeIn);
```

**作用**：
- 将 4 个 RQ 通道的请求串行化
- 确保同一时间只有一个请求到达 `AutoAckGenerator`
- Round-Robin 策略保证公平性

#### 第二层：BRAM 访问互斥

**位置**：`open-rdma-rtl/src/PsnContinousChecker.bsv:121, 142, 281`

```bsv
Reg#(Bool) bitmapUpdateBusyReg <- mkReg(False);

// 规则条件：只有在不忙时才接受新请求
rule sendBramQueryReqAndGenOneHotBitmap if (bramInitedReg && !bitmapUpdateBusyReg);
    if (reqPipeInQueue.notEmpty) begin
        bitmapUpdateBusyReg <= True;  // ← 设置忙标志
        // ... 开始处理请求 ...
    end
endrule

// 写回完成后清除忙标志
rule doBramWriteBack if (bramInitedReg && bitmapUpdateBusyReg);
    storage[0].write(writeBackReq.rowAddr, writeBackReq.newEntry);
    storage[1].write(writeBackReq.rowAddr, writeBackReq.newEntry);
    bitmapUpdateBusyReg <= False;  // ← 清除忙标志
endrule
```

**作用**：
- `bitmapUpdateBusyReg` 作为互斥锁
- 确保读-修改-写操作的原子性
- 防止流水线被新请求打断

#### 第三层：流水线化处理

```
┌──────────────────────────────┐
│ Pipeline Stage 1             │
│ - 提取 QP 索引               │
│ - 读取 BRAM[IndexQP]         │
│ - 生成 one-hot bitmap        │
└────────┬─────────────────────┘
         ↓
┌──────────────────────────────┐
│ Pipeline Stage 2             │
│ - 计算窗口边界差异           │
│ - 合并 bitmap: old | new     │
│ - 检测窗口滑动和丢包         │
└────────┬─────────────────────┘
         ↓
┌──────────────────────────────┐
│ Pipeline Stage 3             │
│ - 写回 BRAM                  │
│ - 生成 ACK/NAK 响应          │
└──────────────────────────────┘
```

**作用**：
- 三级流水线确保操作顺序
- `bitmapUpdateBusyReg` 确保流水线完整执行
- 提高吞吐量的同时保证正确性

### 完整并发处理时序

```
时间轴：

T0: Channel 0: 收到包 (QPN=12345, PSN=100)
    └─ 生成 Req0 → 仲裁器队列

T1: Channel 2: 收到包 (QPN=12345, PSN=101)
    └─ 生成 Req1 → 仲裁器队列

T2: 仲裁器选择 Req0
    └─ Req0 → AutoAckGenerator
       ├─ busyReg = True
       ├─ 读取 BRAM[IndexQP(12345)]
       ├─ 合并: bitmap = old | (1<<100)
       └─ 写回 BRAM[IndexQP(12345)]

T3: Req0 完成
    └─ busyReg = False

T4: 仲裁器选择 Req1
    └─ Req1 → AutoAckGenerator
       ├─ busyReg = True
       ├─ 读取 BRAM[IndexQP(12345)]  ← 读取已更新的值
       ├─ 合并: bitmap = updated | (1<<101)
       └─ 写回 BRAM[IndexQP(12345)]

T5: Req1 完成
    └─ busyReg = False

结果：bitmap 正确包含 PSN 100 和 101
```

---

## 数据流详解

### 1. 接收方向数据流

```
┌──────────────────────────────────────┐
│ 网络包到达                            │
│ (QPN=12345, PSN=150, payload)        │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ 硬件分发（负载均衡）                  │
│ → 选择 RQ Channel 2                  │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ RQ Channel 2 处理                     │
│ 1. 解析包头                           │
│ 2. 检查 QPC                           │
│ 3. 写入 payload 到内存                │
│ 4. 生成 AutoAckGeneratorReq           │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ Round-Robin 仲裁器                    │
│ 等待获取访问权限...                   │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ AutoAckGenerator                      │
│ ├─ IndexQP = getIndexQP(12345) = 123 │
│ ├─ 读取 BRAM[123]                     │
│ │  └─ oldBitmap, leftBound           │
│ ├─ 计算 newBitmap                     │
│ │  └─ (1 << (150 % 128))             │
│ ├─ 合并: merged = old | new           │
│ └─ 写回 BRAM[123]                     │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ 检测是否需要发送 ACK/NAK              │
│ - 窗口滑动？                          │
│ - 丢包检测？                          │
└────────┬─────────────────────────────┘
         ↓ (如需发送 ACK)
┌──────────────────────────────────────┐
│ 生成 ACK 描述符                       │
│ {                                     │
│   qpn: 12345,                         │
│   psn_now: 150,                       │
│   now_bitmap: merged,                 │
│   is_send_by_local_hw: true           │
│ }                                     │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ Meta Report 队列（4个之一）           │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ 驱动轮询队列                          │
│ MetaReportQueueHandler::try_recv_meta │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ 驱动处理 ACK                          │
│ MetaHandler::handle_ack_local_hw      │
│ ├─ recv_table[12345].ack_bitmap()    │
│ └─ receiver_updates()                 │
│    ├─ CompletionTask::AckRecv        │
│    └─ PacketRetransmitTask::Ack      │
└──────────────────────────────────────┘
```

### 2. 发送方向数据流（重传触发）

```
┌──────────────────────────────────────┐
│ 对端发送 NAK                          │
│ (QPN=12345, psn_now=200,              │
│  now_bitmap=0xFF...FE, ← PSN 200 丢失 │
│  psn_pre=150, pre_bitmap=...)         │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ 硬件接收 NAK 包                       │
│ 解析为 NakMetaRemoteHw                │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ Meta Report 队列                      │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ 驱动处理 NAK                          │
│ MetaHandler::handle_nak_remote_hw     │
│ ├─ send_table[12345].nak_bitmap()    │
│ │  └─ 更新发送追踪器                 │
│ ├─ sender_updates()                   │
│ │  ├─ CompletionTask::AckSend        │
│ │  └─ PacketRetransmitTask::Ack      │
│ └─ 触发重传                           │
│    └─ PacketRetransmitTask::          │
│       RetransmitRange {               │
│         qpn: 12345,                   │
│         psn_low: 150,                 │
│         psn_high: 328                 │
│       }                               │
└────────┬─────────────────────────────┘
         ↓
┌──────────────────────────────────────┐
│ PacketRetransmitWorker                │
│ 1. 从 IbvSendQueue[12345] 查找包      │
│ 2. PSN 150-328 范围内的包             │
│ 3. 根据 bitmap 确定具体丢失的包       │
│ 4. 设置 is_retry 标志                 │
│ 5. 重新发送                           │
└──────────────────────────────────────┘
```

---

## 关键代码位置索引

### 软件驱动

| 功能 | 文件 | 行号 |
|-----|------|-----|
| LocalAckTracker 定义 | `src/rdma_utils/psn_tracker.rs` | 9-47 |
| RemoteAckTracker 定义 | `src/rdma_utils/psn_tracker.rs` | 50-80 |
| PsnTracker 核心逻辑 | `src/rdma_utils/psn_tracker.rs` | 83-197 |
| MetaHandler 定义 | `src/workers/meta_report/worker.rs` | 67-94 |
| handle_ack_local_hw | `src/workers/meta_report/worker.rs` | 115-122 |
| handle_nak_remote_hw | `src/workers/meta_report/worker.rs` | 146-168 |
| ACK 描述符结构 | `src/descriptors/meta_report.rs` | 428-547 |
| Meta Report 队列处理 | `src/workers/meta_report/types.rs` | 52-144 |

### 硬件实现

| 功能 | 文件 | 行号 |
|-----|------|-----|
| BitmapWindowStorage 定义 | `src/PsnContinousChecker.bsv` | 52-61 |
| BRAM 存储初始化 | `src/PsnContinousChecker.bsv` | 100-102 |
| Bitmap 更新流水线 Stage 1 | `src/PsnContinousChecker.bsv` | 142-186 |
| Bitmap 更新流水线 Stage 2 | `src/PsnContinousChecker.bsv` | 189-278 |
| Bitmap 更新流水线 Stage 3 | `src/PsnContinousChecker.bsv` | 281-293 |
| AutoAckGenerator 定义 | `src/AutoAckGenerator.bsv` | 60-71 |
| 硬件通道配置 | `src/Settings.bsv` | 28 |
| 包分发逻辑 | `src/vendor/altera/FTileMacAdaptor.bsv` | 684-747 |
| 仲裁器实例化 | `src/Top.bsv` | 819, 843 |
| RQ 通道实例化 | `src/Top.bsv` | 825, 848 |

---

## 潜在问题分析

### 1. receiver_updates 中的 PacketRetransmitTask

**位置**：`src/workers/meta_report/worker.rs:209-211`

```rust
pub(crate) fn receiver_updates(&self, qpn: u32, base_psn: Psn) {
    self.completion_tx.send(CompletionTask::AckRecv { qpn, base_psn });

    // 这对吗？为什么recv也要负责tx重传？
    self.packet_retransmit_tx.send(PacketRetransmitTask::Ack { qpn, psn: base_psn });
}
```

**问题**：
- `receiver_updates` 处理接收方向的确认
- 但调用了 `PacketRetransmitTask::Ack`，这会清理**发送队列**
- **接收方向的 PSN** 与 **发送方向的 PSN** 是独立的
- 可能导致 PSN 空间混淆

**可能的解释**：
1. 设计错误：应该删除这行代码
2. 特殊场景：某些情况下发送和接收共用 PSN 空间（不符合 RDMA 标准）
3. 未完成的重构：代码注释已经质疑这个设计

**建议**：需要进一步验证这个行为是否正确。

---

## 总结

### 核心机制

1. **双向追踪**：
   - 接收方向：`recv_table` (LocalAckTracker) + 本地硬件 bitmap
   - 发送方向：`send_table` (RemoteAckTracker) + 对端硬件 bitmap

2. **硬件高效性**：
   - 每个 QP 在 BRAM 中有独立条目，索引是 `IndexQP`
   - Bitmap 随 ACK/NAK 描述符传递，无需为每个 QP 分配队列
   - 128 位 bitmap 可追踪 128 个连续 PSN

3. **并发安全**：
   - 仲裁器串行化多通道请求
   - `bitmapUpdateBusyReg` 互斥锁保证原子操作
   - 三级流水线确保操作顺序

4. **灵活分发**：
   - 基于负载均衡，不固定 QP 到通道的映射
   - 同一个包的片段保证在同一通道处理
   - 单例 `BitmapWindowStorage` + 仲裁器解决并发问题

### 设计优势

- **可扩展性**：支持大量 QP（2^24 个），不受队列数量限制
- **高吞吐**：4 个并行通道 + 流水线化处理
- **低延迟**：硬件直接生成 bitmap，无需软件计算
- **可靠性**：多层互斥机制保证并发正确性

---

*文档生成时间：2025-12-29*
*分析基于：open-rdma-driver (rust-driver) 和 open-rdma-rtl*