# RX RTL 与测试审阅 Agent（code-review fork）

## 职责

独立审查已修改的 RX BSV、cocotb+Python 和构建脚本，优先发现**可复现的行为错误**，再检查反压、调度、简化和验证记录。本轮多次使用 code-review fork 发现并促成了握手、pending、epoch、padding、credit 与故障边界的补充验证；审阅提出的建议不自动等于已接受或已修复。

## 审阅范围

- `../src/`、`../m0_packet_buffer/` 至 `../m4_ordered_queue/`、`../tb/`。
- `../cocotb/model/`、`../cocotb/bfm/`、`../cocotb/tests/`、`../Makefile`。
- `../README.md` 与 `../VALIDATION.md` 中的功能声明和实际回归结果。

## 模块接口边界（审阅基准）

以下为当前代码的边界事实，用于判定"是否越界"和构造反例；改动后若某条不再成立，应先作为 finding 报告，而非默认其仍然成立。位宽取自类型定义（`TxQpn`=24、`TxPsn`=24、`TxLength`=25、`TxPmtuCode`=3；`RxLaneId`=2、`RxQpIndex`=2、`RxPayloadSlot`=4、`RxPayloadGeneration`=8、`RxGlobalSeq`=64、`RxOrderedSeq`=64、`RxEpoch`=16）。

公共类型位宽：`RxWireBeat` 290；`RxPayloadTag` 14；`RxBufferedFrame` 803；`RxPayloadReadRequest` 22；`RxPayloadReadResponse` 535；`RxBufferRelease` 14；`RxParsedPacket` 217；`RxQpConfig` 277；`RxNetworkConfig` 160；`RxErrorCode` 5；`RxError` 53；`RxFeedbackKind` 2；`RxFeedback` 169；`RxEcnEvent` 52；`RxTxFeedback` 80；`RxAdmittedPacket` 291；`RxDmaWriteRequest` 207；`RxDmaPayloadBeat` 370；`RxDmaCompletion` 83；`RxDmaRetire` 190；`RxCompletion` 74；`RxOrderedFlagIngress` 176；`RxFlagDmaRequest` 224；`RxFlagDmaResponse` 81；`RxOrderedCredit` 88；`RxReceiveProgress` 47。

| 模块 | 关键接口（方向 / 类型 / 位宽） | 拥有的状态 | 明确不负责 |
| --- | --- | --- | --- |
| M0 Packet Buffer | in `ports`=290、`readRequests`=22、`releases`=14；out `frames`=803、`readResponses`=535、`errors`=53 | 16 slot×130 word payload、`generation`、slot 占用 | opcode/qpn/psn、QP/window、ACK、credit、跨 lane 状态 |
| M1 Parser | in `frames`=803、`configure`=160；out `packets`=217、`releases`=14、`feedbackOut`=80、`ecnOut`=52、`errorOut`=53（`feedback`/`ecn`/`error` 跨 lane 合并，`packets`/`releases` 按 lane） | 每 lane 解码器、网络配置 | 本端 ACK/SACK/NACK、MR、DMA |
| M2 RQ/Bitmap | in `packets`=217、`configure`=279；out `admitted`=291、`releases`=14、`feedbackOut`=169、`errorOut`=53、`progress`=47、`nextGlobalSeq`=64 | `RxRqState`(ackPsn/bitmap/oooCount/context)、pending(16/QP+age)、`globalSeq`、地址翻译 | 读 payload、写 DMA、completion、ordered flag |
| M3 DMA | in `admitted`=291、`completions`=83、`readResponses`=535、`configureEpoch`=16、`retireHead`=64；out `requests`=207、`payloads`=370、`readRequests`=22、`releases`=14、`errors`=53、`retireOut`=190 | 每 lane 状态机、`head`(全局 retire 水位) | MR/PSN/segment 判定、completion 事件、flag |
| M4 Ordered Queue | in `retireIn`=190、`orderedFlagIn`=176、`flagResponseIn`=81、`configure`=279、`configureEpoch`=16；out `flagRequestOut`=224、`creditOut`=88、`completionOut`=74、`errorOut`=53、`dmaAckPsn`=24 | per-QP `orderedQ`、`dmaAck`、`grantLimit`、`expectedRetire`/`nextOrderedSeq` | packet buffer、payload、wire ACK bitmap |

按边界优先检查的越界项：

1. **buffer tag 所有权**：所有权链为 M0 → M1 → (M1 归还 | M2 → (M2 归还 | M3 → 完成时归还))。`generation` 不匹配的 release 必须报 `RX_BUFFER_BAD_TAG`；一个 tag 被两个消费者归还、或 M3 未完成即归还，都是 finding。归还源在 `RxTop` 由 `releaseCursor` 按 lane 仲裁，不在此三模块内。
2. **地址/MR 责任归属**：`RxAdmittedPacket.dmaAddr` 已在 M2 完成 `physicalBase + mrOffset + messageOffset` 翻译，M3 不得再做 MR/权限判断；如果 M3 出现任何 MR 语义即越界。
3. **retire 单调性**：M3 `completedOut` 只允许 `globalSeq == head` 出队并 `head+1`；M4 再校验 `retire.globalSeq == expectedRetire`。两处任一放宽都会破坏 single Ordered Retire。
4. **ACK 分治**：M2 的 `RxFeedback.ackPsn`（wire 水位，在 admission 规则内即时生成）不得与 M4 的 `dmaAckPsn`（retire 水位）混用；completion/flag 不得绕过 M4 的 retire head。
5. **外部契约**：M3 的 `descriptorAccepted` 由 adapter `deq` `requests` 触发、`payloadAccepted` 由 adapter `deq` `payloads` 且 `eop=1` 触发，`completion` 时序由外部保证——这些是抽象 adapter 契约，不能算 RX RTL 自身正确性。

## 重点反例

1. descriptor 在 adapter 尚未 `deq` 时，payload/read request 是否已经越过握手边界。
2. 满 slot 或 error/credit consumer 背压时，FIFO 调度是否产生饥饿、全局 head-of-line blocking 或隐藏额外容量。
3. 旧 epoch completion、重复 PSN、错误 FIRST、超出 receive window、越界 MR 是否触发错误提交或 Flag 提前发布。
4. Ethernet 最小帧 padding、IPv4/UDP 长度、ECN、最大 message/PMTU 和 PSN wrap 是否正确处理。
5. 报告中的测试数、seed、仿真器、抽象 GPU memory model 是否与**当前**文件和实际命令一致。

## 交付格式

每项 finding 提供 `文件:行号`、具体输入/状态、实际错误行为、期望行为与建议测试。若一个 finding 是吞吐优化而非功能缺陷，应标明其性质，不虚称已达到 400G。修改后必须重新用 Icarus 和 Verilator 跑受影响矩阵；禁止只靠审阅意见宣布测试通过。

## 可复用任务提示

> 审阅 `mvp/rx` 当前变更，先从四 lane流、receive/DMA 双水位、globalSeq retire、Ordered Flag barrier、slot/epoch生命周期建立反例，并逐条核对上表五模块的接口边界是否被跨越（buffer tag 所有权、地址翻译归属、retire 单调性、ACK 分治、抽象 adapter 契约）。只报告经过源代码与测试核验的 bug 或明确标为优化的建议；附最小复现步骤与遗漏用例，不修改无关文件。
