# OpenRDMA-flow RX：四路可靠接收功能 MVP

本目录实现与 `OpenRDMA-flow` 当前 TX 私有 wire format 配套的 RX 数据通路。它面向可综合 RTL 和协议功能验证，包含四个 FTile 风格入口、接收位图、四路 packet-level DMA、全局有序退休和 Ordered Flag barrier。

> 本实现是功能 MVP，不等于 400G 生产实现。尚未覆盖 ICRC/FCS、标准 RoCE 互操作、真实 PCIe posted-write drain、GPU cache 可见性、散页 GPU MR、Quartus P&R 或板级 FTile 联调。

## 数据通路

```text
RxWireBeat[4]
  -> Packet Buffer（16 slot/lane，beat-addressed storage）
  -> Packet Parser（4 decoder，Ethernet/IPv4/UDP/BTH/RETH）
  -> RQ Bitmap Manager（4 QP × 16 PSN，atomic receive commit）
  -> DMA Placement（4 packet DMA lane）
  -> per-lane completion FIFO
  -> single Ordered Retire（globalSeq）
  -> DMA watermark / CQE / Ordered Flag Queue
```

公共类型在 `src/RxTypes.bsv`，顶层为 `src/RxTop.bsv:mkRxTop`。所有 stream 使用 `upstream/CommonTypes.bsv` 的 guarded `PipeIn/PipeOut`。

每次 reset后、调用 `start` 前，控制面必须依次完成 `configureEpoch`、`configureNetwork`和至少一个 valid `configureQp`。packet DMA和 Flag DMA request都会携带配置的 16-bit epoch，response必须原样回显；因此 reset前滞留的 completion不能冒充 reset后的同 `globalSeq/lane` 请求。控制面负责持久化并递增 epoch；单个 RUN生命周期内不得复用。

该 epoch不是 network wire字段，不能识别链路中滞留的旧 frame。重新使用同一 QPN/PSN/MR前，控制面还必须 quiesce并drain FTile/network ingress，或轮换 QPN、rkey和 initial PSN；只修改 DMA epoch而直接复用全部网络身份是不合法的 reset流程。

## 五个模块

| 目录 | 模块 | 已实现语义 |
|---|---|---|
| `m0_packet_buffer` | `mkRxPortBuffer` ×4 | 四 lane 独立 beat FIFO；16 slot/lane；slot generation；SOF/EOF、prefix BE、最大 4138B 检查；malformed 后重同步；按 tag/word 读取 payload；显式 release |
| `m1_packet_parser` | `mkRxFrameDecoder` ×4 | 校验并解析当前 TX 的 Ethernet/IPv4/UDP/BTH/RETH；支持最小 Ethernet frame的 zero padding；WRITE FIRST/MIDDLE/LAST/ONLY；`0x11+AETH`；IPv4 ECN，CE 生成 `RxEcnEvent`；非法帧释放 slot |
| `m2_rq_bitmap` | `mkRxRqBitmapManager` | 4 QP × 16-entry receive bitmap；duplicate、窗口边界、PSN wrap；FIRST 前暂存 continuation并带有扫描 timeout/NAK回收；完整 QP/MR/segment admission 后 atomic commit；立即 ACK/SACK/NAK；无空洞 `globalSeq` |
| `m3_dma_placement` | `mkRxDmaPlacement` | 4 路 packet-level descriptor/payload；从 Packet Buffer 对齐读取；每 lane 一个 packet outstanding；completion identity 校验；per-lane completion FIFO；严格按 `globalSeq` single Ordered Retire |
| `m4_ordered_queue` | `mkRxOrderedQueueManager` | 按 retire 结果推进每 QP `dmaAckPsn`；完整消息 CQE；每 QP 4-entry Ordered Flag FIFO；MR/rkey/alignment 检查；barrier 后 Flag DMA；absolute ordered credit；fatal fail-stop |

## receive 与 DMA 双水位

可靠性进度被明确拆成两层：

- `RxFeedback.ackPsn`：最高连续、已完成 admission 和 receive bitmap commit 的 PSN。它在 packet 接收后推进，**不等待 DMA**。
- `dmaAckPsn(qp)`：最高连续、其 receive ACK 推进记录已经通过 `globalSeq` Ordered Retire 的 PSN。它用于 Ordered Flag barrier。

`RxFeedback.sackBase = ackPsn + 1`，`sackBitmap[i]` 表示 `sackBase+i` 已经 receive-committed；它不表示 DMA 状态。duplicate 和 reject 不分配 `globalSeq`，不产生 DMA。

当前 TX 只有 FIRST/ONLY 携带 RETH。MIDDLE/LAST 在 FIRST 到达前可以占用 pending slot，但不会 ACK、不会分配 `globalSeq`、不会 DMA。FIRST 建立并验证消息上下文后，pending packet 才进入 receive commit；若 FIRST长期缺失，按 QP/slot轮询的 age达到 15次扫描后产生 NAK和 `RX_PENDING_TIMEOUT`并释放 payload tag。相同 PSN的后到 FIRST会替换尚未 admission的 pending条目，避免坏/过时副本毒化 ring slot。

## Ordered Flag 入口

当前 TX 尚无冻结的 Ordered Flag wire opcode，因此 RX 不占用临时 BTH opcode。顶层提供 post-parser 语义入口：

```text
RxOrderedFlagIngress {
    qpn, barrierPsn, flagAddr, rkey, value
}
```

该入口的 producer 必须保证事件已完成 authentication 和 duplicate suppression，并按 QP协议顺序提交非递减 barrier；逆序 barrier会触发 fail-stop，避免重排后旧 Flag覆盖新值。24-bit PSN serial比较要求同一活动 epoch内任意待比较水位跨度小于 `2^23`。Ordered Queue 仍会二次检查 QPN、4B alignment、rkey、remote-write 和 MR range。

只有 `dmaAckPsn[qpn]` 在 24-bit 模空间达到 `barrierPsn` 后，队首才可输出 `RxFlagDmaRequest`。外部 adapter 实际接受（`deq`）请求时返回 absolute `RxOrderedCredit`。每 QP的 4-entry queue对应初始 grant limit 4，因此首个释放反馈为 5；发送端对 absolute watermark取最大值，不做增量累加。将来 wire format 固定后，只需在 Parser 前增加 decoder，Ordered Queue 无需改变。

## DMA adapter 契约

RX 对每个唯一 packet 输出：

- 一个携带 `globalSeq/lane/epoch` 的 `RxDmaWriteRequest` descriptor；
- 若干携带同 `globalSeq/epoch` 的连续 `RxDmaPayloadBeat`（256 bit，尾拍 prefix BE）；
- 输入一个带原 `globalSeq/lane/epoch` 的 `RxDmaCompletion`。

adapter 可以在内部拆 PCIe transaction，但 `success` 必须表示该 packet 的所有 payload bytes 已达到 Ordered Flag 判断所需的 ordered placement completion point。PCIe Memory Write 是 posted write，不能把这里的 response 当成普通 CplD，也不能在 descriptor 入队时提前成功。

跨 lane completion 可以乱序；每 lane completion FIFO 保存完成记录，single Ordered Retire 只退休 `globalSeq == retireHead` 的队首。completion mismatch 或失败会进入 sticky fail-stop，不推进 retire 或发布 Flag。

MR MVP 使用连续映射：

```text
physicalBase + (remoteAddr - virtualBase)
```

## 与 OpenRDMA-flow TX 的兼容面

`upstream/` 是 RX 固定使用的 **legacy TX wire 基线**（快照日期与各文件 sha256 记录在 `upstream/BASELINE.sha256`）。根 Makefile 的 `preflight`：

1. 用 `sha256sum -c` 校验 `upstream/` 四个文件与基线一致 —— 快照被意外改动会直接报错；
2. 与 `OpenRDMA-flow/m0|m2|m3` 权威文件比较，若上游 TX 已演进只打印 `WARNING`，不阻止构建。

也就是说，**RX 的全部验证结果只描述 `BASELINE.sha256` 记录的那一版 wire 契约**。`OpenRDMA-flow` 后续新增的 wire profile（例如 `OP_MRC_WRITE_*`、`TxMsn`）尚未被 RX parser 支持；要覆盖它们需要更新快照并扩展 Parser，属于一次新的实现任务，而不是修改 `BASELINE.sha256` 就能完成的事。

RX 只接收 `EthPacket.data/be/sof/eof`，不会读取 TX 的 monitoring sideband。当前字节布局为：

| Ethernet 起始偏移 | 内容 |
|---|---|
| 0 / 6 / 12 | destination MAC / source MAC / EtherType |
| 14 / 34 | 20-byte IPv4 / 8-byte UDP |
| 42 / 46 / 50 | opcode / destination QPN / PSN |
| 54 / 58 / 66 | FIRST/ONLY 的 rkey / remote address / message length |
| 70 或 54 | FIRST/ONLY 或 MIDDLE/LAST payload |

opcode 为 `0x06/0x07/0x08/0x09`。FIRST payload 上限为 `PMTU-28`，continuation 为 `PMTU-12`。当前 frame 不含 ICRC、RDMA padding 或 FCS。

## 构建和测试

推荐使用项目的 cocotb 环境：

```bash
PYTHON=/home/mmhh/miniconda3/envs/cocotb2/bin/python

# 每个模块有独立 Makefile
SIM=icarus   make -C m0_packet_buffer test PYTHON=$PYTHON
SIM=verilator make -C m1_packet_parser test PYTHON=$PYTHON
SIM=icarus   make -C m2_rq_bitmap test-long PYTHON=$PYTHON
SIM=verilator make -C m3_dma_placement test PYTHON=$PYTHON
SIM=icarus   make -C m4_ordered_queue test PYTHON=$PYTHON

# 根目录矩阵
SIM=icarus    make test-all PYTHON=$PYTHON
SIM=verilator make test-all PYTHON=$PYTHON
SIM=icarus    make test-long-all PYTHON=$PYTHON
make compile-all
make verilog-all
make production-verilog
```

每个模块 Makefile 均支持 `compile`、`verilog`、`test`、`test-long`、`clean`。构建产物位于 `build/<target>/`。

测试采用透明 BSV wrapper、Cocotb 2 Pipe/Method BFM、严格 struct packer、独立 Python wire builder 和逐事件/逐 byte scoreboard。覆盖：

- Packet Buffer 四 lane 并发、4138B、BE/SOF/EOF、slot exhaustion/reuse/generation；
- Parser 四 opcode、checksum/length、ECN CE、AETH、错误 release；
- RQ FIRST 迟到、SACK、duplicate、delta 15/16、PSN wrap、MR fail-stop、固定种子乱序长稳；
- DMA 四 lane并行、最大 payload、reverse completion、global HOL、identity/epoch mismatch fail-stop；
- Ordered Flag barrier、PSN wrap、跨 QP RR、初始/absolute credit、固定 4-entry容量、response epoch、queue overflow；
- 完整 RX 双水位与 Flag；
- 真实 `mkPacketAssemblyEngine + mkFourChannelTxScheduler` 生成 frame，只取 wire fields，重排后送入 RX，并逐 byte校验 GPU memory model。

实际验证结果见 `VALIDATION.md`。

## 已知实现限制

- Packet Buffer 是 beat-addressed `RegFile` 功能存储；量产前应替换为明确的 banked BRAM/URAM primitive并做资源/Fmax评估。
- central RQ state owner 每次只提交一个 candidate；pending resolver和 ingress采用保守分阶段调度，未优化到四 lane line-rate。
- 每个 DMA lane当前只有一个 packet outstanding；真实 adapter可扩展 tag/credit深度，但必须维持 lane identity和 completion契约。
- ACK/SACK/NAK 是 NIC 内部事件，尚未编码为网络控制包；`RxTxFeedback` 只解码当前 `0x11 + AETH`。
- Ordered Queue为固定 cursor的 per-QP round-robin；空/blocked QP最多增加 3 cycle选择延迟，功能正确但未做单周期 work-conserving priority encoder优化。
- sticky fault 需通过 reset建立新 epoch；当前没有在线 stop/drain/reconfigure。控制面必须保证 epoch不与仍可能返回的旧 request复用。

## 离线流程中的角色

本树是 `OpenRDMA-Offline` 的 RX 一半：`make rx`（顶层 Makefile）编译 `tb/TestRxTop.bsv` 的
`mkTestRxTop`，跑 `../ns3/cocotb/rx_replay.py`，把 ns-3 写出的 `results/rx-trace.csv`
按到达时序回放进真实的 RX 数据通路，核对逐包落显存、CQE 与双水位。
