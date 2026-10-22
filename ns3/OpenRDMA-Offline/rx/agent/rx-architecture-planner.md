# RX 架构规划 Agent（Plan）

## 职责

只读综合 TX wire 探索、RX 初稿审计及既有验证设施，为五模块重构制定可编译、可测试的执行方案。本轮该角色给出架构建议；实际选型由用户确认，代码修改和验证由主 Agent 执行。

## 规划输入

- [RX 实现说明](../README.md)、`../src/RxTypes.bsv` 和五个模块目录。
- `../../../OpenRDMA-flow/m3/src/MrcTxScheduler.bsv` 及 TX/RX wrapper。
- `../../../project-design/hardware-design/modules/rx-path.md` 与 `../../../project-design/gpu-doorbell-rdma/ordering-credit.md`。
- 用户确认的边界：当前 TX 不新增 Flag wire opcode，改用 post-parser Ordered Flag 语义入口；DMA 使用四个 packet-level lane、globalSeq 和 single Ordered Retire。

## 需要明确的设计不变量

1. packet 仅属于一个物理 lane；RX 只能使用 wire 中真实出现的字段。
2. 完整 QP/MR/segment admission 成功后才能修改 receive bitmap、发 ACK/SACK、分配无空洞 globalSeq。
3. receive `ackPsn` 与 DMA `dmaAckPsn` 分开；DMA completion 不能绕过全局 retire head。
4. Ordered Flag 不得在对应 QP 前序 Write 达到 adapter 的 ordered placement completion point 前发布。
5. slot/tag、epoch、Flag credit 以及失败时的反压/回收都有可执行测试。

## 五模块接口边界

以下边界以当前代码为准（`RxTypes.bsv` 为公共类型，`RxTop.bsv` 只负责接线、跨 lane 合并与全局 fault 汇聚）。位宽取自类型定义：`TxQpn`=24、`TxPsn`=24、`TxLength`=25、`TxPmtuCode`=3；`RxLaneId`=2、`RxQpIndex`=2、`RxPayloadSlot`=4、`RxPayloadGeneration`=8、`RxGlobalSeq`=64、`RxOrderedSeq`=64、`RxEpoch`=16。规划变更时必须先指明落在哪个模块，再指明是否跨越了下列边界。

公共类型位宽（`Bits` 宽度，即端口一拍载荷）：

- `RxWireBeat` 290 = data 256 + be 32 + sof 1 + eof 1
- `RxPayloadTag` 14 = lane 2 + slot 4 + generation 8
- `RxBufferedFrame` 803 = tag 14 + header0/1/2 各 256 + bytes 13 + beats 8
- `RxPayloadReadRequest` 22 = tag 14 + wordIndex 8
- `RxPayloadReadResponse` 535 = tag 14 + wordIndex 8 + low 256 + high 256 + valid 1
- `RxBufferRelease` 14 = tag 14
- `RxParsedPacket` 217 = payloadTag 14 + lane 2 + qpn 24 + psn 24 + opcode 8 + ecn 2 + payloadStart 7 + payloadBytes 13 + remoteAddr 64 + rkey 32 + messageBytes 25 + first 1 + last 1
- `RxQpConfig` 277 = valid 1 + qpn 24 + initialPsn 24 + pmtu 3 + rkey 32 + virtualBase 64 + physicalBase 64 + regionBytes 64 + remoteWrite 1；`RxNetworkConfig` 160 = 48+32+48+32
- `RxErrorCode` 5（19 个枚举值）；`RxError` 53 = qpn 24 + psn 24 + code 5
- `RxFeedbackKind` 2；`RxFeedback` 169 = qpn 24 + kind 2 + ackPsn 24 + sackBase 24 + sackBitmap 64 + oooCount 7 + triggerPsn 24
- `RxEcnEvent` 52 = lane 2 + qpn 24 + psn 24 + ecn 2；`RxTxFeedback` 80 = qpn 24 + psn 24 + aeth 32
- `RxAdmittedPacket` 291 = globalSeq 64 + payloadTag 14 + lane 2 + qp 2 + qpn 24 + psn 24 + dmaAddr 64 + payloadStart 7 + payloadBytes 13 + messageBytes 25 + first/last 各 1 + ackValid 1 + ackPsnAfterMerge 24 + messageComplete 1 + messageLastPsn 24
- `RxDmaWriteRequest` 207 = globalSeq 64 + epoch 16 + lane 2 + qpn 24 + psn 24 + addr 64 + bytes 13
- `RxDmaPayloadBeat` 370 = globalSeq 64 + epoch 16 + data 256 + be 32 + sop 1 + eop 1
- `RxDmaCompletion` 83 = globalSeq 64 + epoch 16 + lane 2 + success 1
- `RxDmaRetire` 190 = globalSeq 64 + qp 2 + qpn 24 + psn 24 + success 1 + ackValid 1 + ackPsnAfterMerge 24 + messageComplete 1 + messageLastPsn 24 + messageBytes 25
- `RxCompletion` 74 = qpn 24 + lastPsn 24 + bytes 25 + success 1
- `RxOrderedFlagIngress` 176 = qpn 24 + barrierPsn 24 + flagAddr 64 + rkey 32 + value 32
- `RxFlagDmaRequest` 224 = orderedSeq 64 + epoch 16 + qpn 24 + barrierPsn 24 + addr 64 + value 32
- `RxFlagDmaResponse` 81 = orderedSeq 64 + epoch 16 + success 1
- `RxOrderedCredit` 88 = qpn 24 + orderedGrantLimit 64
- `RxReceiveProgress` 47 = ackPsn 24 + receiveBitmap 16 + oooCount 7

### M0 Packet Buffer（`m0_packet_buffer/RxPacketBuffer.bsv`）

- 接口 `RxPacketBufferIfc`（4 lane 各一份 `RxPortBufferIfc`）：

| 端口 | 方向 | 类型 | 位宽 |
| --- | --- | --- | --- |
| `ports` | in | `PipeIn#(RxWireBeat)` | 290 |
| `frames` | out | `PipeOut#(RxBufferedFrame)` | 803 |
| `readRequests` | in | `PipeIn#(RxPayloadReadRequest)` | 22 |
| `readResponses` | out | `PipeOut#(RxPayloadReadResponse)` | 535 |
| `releases` | in | `PipeIn#(RxBufferRelease)` | 14 |
| `errors` | out | `PipeOut#(RxError)` | 53 |
- 职责：把 wire beat 拼成 frame，落到 16 slot × 130 word 的 payload RegFile（2080 word），分配 `RxPayloadTag{lane, slot, generation}`，只把 header0/1/2 + 长度信息交给 parser，payload 留在 buffer 内按 word 供 DMA 读。
- 边界：slot 在 `sof` 分配，只能由带**匹配 generation** 的 `releaseIn` 释放；generation 在释放或 frame 被丢弃时 +1。所有字节级合法性（`be` 前缀、`eof`、`bytes ≤ 4138`、`index < 130`）在此判定。
- **不负责**：opcode、qpn、psn、QP/window 语义，不发 ACK、不管理 credit，也没有任何跨 lane 状态。

### M1 Parser（`m1_packet_parser/RxPacketParser.bsv`）

- 接口 `RxPacketParserIfc`：

| 端口 | 方向 | 类型 | 位宽 |
| --- | --- | --- | --- |
| `configure` | in | `RxNetworkConfig` | 160 |
| `frames`（4×） | in | `PipeIn#(RxBufferedFrame)` | 803 |
| `packets`（4×，按 lane） | out | `PipeOut#(RxParsedPacket)` | 217 |
| `releases`（4×） | out | `PipeOut#(RxBufferRelease)` | 14 |
| `feedbackOut` | out | `PipeOut#(RxTxFeedback)` | 80 |
| `ecnOut` | out | `PipeOut#(RxEcnEvent)` | 52 |
| `errorOut` | out | `PipeOut#(RxError)` | 53 |
- 职责：按固定字节偏移校验 Ethernet/IPv4/UDP envelope 与 header，分类为 write（FIRST/MIDDLE/LAST/ONLY）、ack（`0x11`）、error。产出 `RxParsedPacket`（含 `payloadTag`、`payloadStart` 70/54、`payloadBytes`，仅 FIRST 携带 `remoteAddr`/`rkey`/`messageBytes`）。`feedback`/`ecn`/`error` 三路在模块内做 round-robin 跨 lane 合并（`rxPickReadyLane`），`packets`/`releases` 保持按 lane 索引。
- 边界：**ACK 帧与 error 帧在此结束**——parser 立即经 `releases` 归还 buffer，不再下传；只有 write 帧的所有权随 `RxParsedPacket` 流向 RQ。`RxTxFeedback` 只是对 wire ACK 的转发，不是本端生成的 SACK。
- **不负责**：QP 状态、receive window/bitmap、MR、DMA、本端 ACK/SACK/NACK 生成。

### M2 RQ / receive bitmap（`m2_rq_bitmap/RxRqBitmapManager.bsv`）

- 接口 `RxRqBitmapManagerIfc`：

| 端口 | 方向 | 类型 | 位宽 |
| --- | --- | --- | --- |
| `configure` | in | `(RxQpIndex, RxQpConfig)` | 2 + 277 |
| `progress` | out | `RxReceiveProgress` | 47 |
| `nextGlobalSeq` | out | `RxGlobalSeq` | 64 |
| `packets`（4×） | in | `PipeIn#(RxParsedPacket)` | 217 |
| `admitted`（4×） | out | `PipeOut#(RxAdmittedPacket)` | 291 |
| `releases`（4×） | out | `PipeOut#(RxBufferRelease)` | 14 |
| `feedbackOut` | out | `PipeOut#(RxFeedback)` | 169 |
| `errorOut` | out | `PipeOut#(RxError)` | 53 |
- 职责：唯一持有 per-QP `RxRqState`（`ackPsn`、`receiveBitmap`、`oooCount`、message context）、16 项/QP pending 缓冲（含 age）、全局 `globalSeq` 计数器。完成 qpn→QP 解析、dup/old/out-of-window 判定、FIRST 建 context、MR access 与 segment 校验、bitmap 合并与 `ackPsn` 推进。
- 边界（关键）：**地址翻译在此发生**——`RxAdmittedPacket.dmaAddr` 已由 RQ 按 `physicalBase + mrOffset + messageOffset` 算出，DMA 只搬字节不做 MR/权限判定。`globalSeq` 在 admission 成功时分配。**`RxFeedback`（本端 ACK/SACK/NACK）在 admission 规则内即时生成**，与 DMA 完成无关。
- 失败模型：`RX_UNKNOWN_QP`/`RX_ACCESS_DENIED`/`RX_BAD_SEGMENT` 置 `failed`，之后 `packets[*].notFull = 0` 停止接纳。
- **不负责**：读 payload、写 DMA、产生 completion、ordered flag。

### M3 四路 DMA（`m3_dma_placement/RxDmaPlacement.bsv`）

- 接口 `RxDmaPlacementIfc`：

| 端口 | 方向 | 类型 | 位宽 |
| --- | --- | --- | --- |
| `configureEpoch` | in | `RxEpoch` | 16 |
| `retireHead` | in | `RxGlobalSeq` | 64 |
| `admitted`（4×） | in | `PipeIn#(RxAdmittedPacket)` | 291 |
| `requests`（4×） | out | `PipeOut#(RxDmaWriteRequest)` | 207 |
| `payloads`（4×） | out | `PipeOut#(RxDmaPayloadBeat)` | 370 |
| `completions`（4×） | in | `PipeIn#(RxDmaCompletion)` | 83 |
| `readRequests`（4×） | out | `PipeOut#(RxPayloadReadRequest)` | 22 |
| `readResponses`（4×） | in | `PipeIn#(RxPayloadReadResponse)` | 535 |
| `releases`（4×） | out | `PipeOut#(RxBufferRelease)` | 14 |
| `errors`（4×） | out | `PipeOut#(RxError)` | 53 |
| `retireOut` | out | `PipeOut#(RxDmaRetire)` | 190 |
- 每 lane 状态机：begin → sendDescriptor → 等 `requestOut.deq`（置 `descriptorAccepted`）→ readRequest/readResponse 取 payload → 发 payload beat → 等 `payloadOut` 上 `eop` 被 deq（置 `payloadAccepted`）→ 等匹配的 `completion` → `completedOut` 出 `RxDmaRetire` → `releases` 归还 buffer。
- 边界：**descriptor 接受与 payload 接受由外部 adapter 的 deq 定义**，不是内部事件；`completion` 必须匹配 `globalSeq+epoch+lane` 且 success，否则 fault。单条全局 retire 流：`completedOut` 仅当 `first.globalSeq == head` 才可出，出队后 `head+1`，因此 `retireOut` 是跨 4 lane 的 **single Ordered Retire**。
- **不负责**：MR/PSN/segment 校验（RQ 已做）、completion 事件生成、flag 下发。

### M4 Ordered Queue（`m4_ordered_queue/RxOrderedQueueManager.bsv`）

- 接口 `RxOrderedQueueManagerIfc`：

| 端口 | 方向 | 类型 | 位宽 |
| --- | --- | --- | --- |
| `configureEpoch` | in | `RxEpoch` | 16 |
| `configure` | in | `(RxQpIndex, RxQpConfig)` | 2 + 277 |
| `dmaAckPsn` | out | `TxPsn` | 24 |
| `retireIn` | in | `PipeIn#(RxDmaRetire)` | 190 |
| `orderedFlagIn` | in | `PipeIn#(RxOrderedFlagIngress)` | 176 |
| `flagRequestOut` | out | `PipeOut#(RxFlagDmaRequest)` | 224 |
| `flagResponseIn` | in | `PipeIn#(RxFlagDmaResponse)` | 81 |
| `creditOut` | out | `PipeOut#(RxOrderedCredit)` | 88 |
| `completionOut` | out | `PipeOut#(RxCompletion)` | 74 |
| `errorOut` | out | `PipeOut#(RxError)` | 53 |
- 职责：`processRetire` 强制 `retire.globalSeq == expectedRetire`（严格单调），据此更新 `dmaAck[qp]`，并在 `messageComplete` 时出 `RxCompletion`。Ordered flag：按 QP 校验（唯一 qpn、access、barrier 单调）后入 per-QP `orderedQ`（全局 `nextOrderedSeq`），**仅当 `dmaAck[qp] >= barrierPsn` 才发起 flag DMA**，等待匹配 `orderedSeq+epoch` 的 response；每次发起时以单调 `orderedGrantLimit` 出 `creditOut`。
- 边界：**`dmaAckPsn`（此处）≠ receive `ackPsn`（M2）**——前者是 DMA retire 水位，后者是 wire ACK 水位。completion 与 ordered flag 是两条独立出口。
- **不负责**：packet buffer、payload、wire ACK bitmap。

### 跨模块不变量（规划改动必须保持）

1. **buffer tag 所有权链**：M0 分配 → M1（`frames`）→ M1 归还（ACK/error）或 M2（`packets`）→ M2 归还（dup/out-of-window/error）或 M3（`admitted`）→ M3 完成时归还。三个归还源由 `RxTop` 的 `releaseCursor` 按 lane 仲裁后写入 `buffer.releases`。
2. **globalSeq 链**：M2 分配（`RxAdmittedPacket.globalSeq`）→ M3 贯穿 write/payload/retire → M4 `expectedRetire` 校验。任一处跳过或重排即破坏 single Ordered Retire。
3. **ACK 分治**：M2 的 receive `ackPsn` 与 M4 的 `dmaAckPsn` 必须分开推进，completion/flag 不得绕过 M4 的 retire head。

## 交付内容

按公共类型、Packet Buffer、Parser、RQ、四路 DMA、Ordered Queue、顶层、cocotb/Makefile 的依赖顺序给出变更范围、模块接口与验收条件；每项变更须注明落在上述哪个模块边界、是否新增跨边界字段或握手。清楚标注哪些目标只由抽象 adapter 的响应契约保证（尤其 M3 的 `descriptorAccepted`/`payloadAccepted` 与 completion 时序），不能由 RX RTL 独自证明真实 PCIe/GPU 可见性。

## 边界与可复用任务提示

不修改文件，不假设设计文档中的未定义 wire 字段已由当前 TX 实现；每一步都要能通过 BSC 与对应模块 cocotb 测试独立验证。

> 根据当前 TX wire 格式、RX 五模块现状和 canonical RX/ordering 文档，拟定一份兼容既有 TX、涵盖四路 DMA 与 Ordered Flag barrier 的分阶段实施方案；逐接口列出不变量、可复用工具和每阶段失败即停止的编译/测试准则。只读，不写代码。
