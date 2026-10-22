# RX 验证记录

日期：2026-09-13。

## 工具环境

- B-Lang BSC：2025.01.1（build `65e3a87a`）
- Icarus Verilog：12.0 stable
- Verilator：5.048
- Python：3.13.13（`/home/mmhh/miniconda3/envs/cocotb2/bin/python`）
- cocotb：2.1.0.dev0+ra1888346
- TX 源：本目录 `upstream/` 是 RX 固定的 legacy TX wire 基线，哈希记录在 `upstream/BASELINE.sha256`；本记录的全部结果只描述该版契约。`OpenRDMA-flow` 上游此后的演进（新增 `OP_MRC_WRITE_*` 等 profile）尚未被 RX parser 覆盖，详见 README 的兼容面一节。

## 最终执行结果

### Icarus short matrix

执行：

```bash
SIM=icarus PYTHON=/home/mmhh/miniconda3/envs/cocotb2/bin/python make test-all
```

| target | 通过 | 失败 |
|---|---:|---:|
| `m0_packet_buffer` | 6 | 0 |
| `m1_packet_parser` | 3 | 0 |
| `m2_rq_bitmap` | 8 | 0 |
| `m3_dma_placement` | 5 | 0 |
| `m4_ordered_queue` | 9 | 0 |
| `rx_top` | 5 | 0 |
| `tx_rx_loopback` | 1 | 0 |
| **合计** | **37** | **0** |

### Verilator short matrix

执行：

```bash
SIM=verilator PYTHON=/home/mmhh/miniconda3/envs/cocotb2/bin/python make test-all
```

同样为 **37/37 通过，0 失败**。

### 固定种子长稳

执行：

```bash
SIM=icarus PYTHON=/home/mmhh/miniconda3/envs/cocotb2/bin/python \
    make -C m2_rq_bitmap test-long
```

结果：**9/9 通过**。额外长稳场景使用固定 seed `0x52444d41`，连续执行 32 条三包消息（96 个唯一 packet），随机化 FIRST/MIDDLE/LAST 到达顺序和物理 lane；检查 receive ACK、pending resolution 和 `globalSeq=0..95` 无空洞。

完整 `test-long-all` 也已执行，共 **38/38 通过**；其中非 m2 target 复用 short directed suite。

### 生产顶层 Verilog

执行：

```bash
make production-verilog
```

结果：成功生成 `build/production/verilog/mkRxTop.v`（20,573 行），不存在 BSC 编译或 elaboration 错误。

## 功能证据

### Packet Buffer

- 四个 lane 同时输入 1B、33B、95B 和 4138B frame，全部逐 byte readback一致。
- 非连续 BE 被拒绝并报告 `RX_BAD_FRAME`。
- malformed frame 后可立即接收 298B WRITE FIRST frame。
- 16 个 slot 全部被占用时该 lane不产生第 17 个 descriptor；其他 lane继续工作。
- release 后相同 slot 的 generation 增加，陈旧 tag不能读取新内容；`wordIndex=255` 越界 read 返回 invalid/zero，并报告 `RX_BUFFER_BAD_TAG`。
- 满 slot 的 lane 存在等待 ingress 时，payload read 仍可完成，释放后可以继续组帧。

### Packet Parser

- 独立 Python builder构造 WRITE FIRST/MIDDLE/LAST/ONLY，QPN、PSN、rkey、remote address、message length和 payload offset全部匹配。
- IPv4 ECN `CE=3` 同时生成 parsed packet与 `RxEcnEvent`，数据不被 ECN旁路丢弃。
- `ECT(0)` 不误报 CE。
- 当前 `0x11 + 4-byte AETH`（包括补零到 60B 的 Ethernet 最小帧）被分流到 `RxTxFeedback`，padding 不计入有效载荷。
- checksum/header错误以及超过 TX 16 MiB 上限的 message length产生 `RX_BAD_HEADER`并释放 buffer slot。

### RQ Bitmap Manager

- LAST、MIDDLE先于 FIRST时不 ACK、不 DMA；FIRST建立并验证上下文后，三个 packet全部 receive-commit。
- 24-bit PSN `fffffe -> ffffff -> 000000` 后 `ackPsn=000000`、bitmap归零。
- receive feedback 在 DMA completion为零时已经产生，证明 receive ACK与 DMA watermark解耦。
- 窗口 delta=15 接收并产生 SACK bit 15；delta=16产生 NAK和 `RX_OUT_OF_WINDOW`。
- duplicate不消耗 `globalSeq`、不产生第二次 DMA。
- wrong rkey及整消息 physical mapping overflow都在 bitmap commit前进入 fail-stop：`ackPsn`、bitmap和 `globalSeq`保持不变。
- 同一 PSN 的无上下文 continuation或未 admission 的 FIRST不会永久毒化 ring slot；后到 FIRST替换旧条目并释放旧 payload tag。
- FIRST 持续缺失时，pending age 到达 15 次扫描产生 NAK和 `RX_PENDING_TIMEOUT`，回收 slot后相同 PSN可重传。

### 四路 DMA 与 Ordered Retire

- 四 lane 同时接收 packet-level descriptor和 payload stream。adapter 长时间不接受 descriptor 时，request 保持稳定且该 lane不发出 payload read/request；descriptor 实际握手后才发出 payload，尾拍也必须被接受后才处理 completion。
- 覆盖 54B/70B header alignment、1B、33B、228B、244B和 4084B payload。
- completion以 seq `3,2,1` 返回时 `retireHead`保持 0且无 retire输出；seq 0返回后输出严格为 `0,1,2,3`。
- completion identity检查覆盖 `globalSeq/lane/epoch`；mismatch产生 `RX_DMA_BAD_COMPLETION`并进入 fail-stop，`retireHead`不推进。
- reset后由控制面配置新 epoch；旧 epoch、相同 `globalSeq/lane` 的 completion不能冒充新请求。
- 所有 payload逐 byte比较；scoreboard不去重 DUT输出。

### Ordered Queue

- receive ACK到达 barrier而 DMA watermark未到时，Flag request保持不可见。
- `dmaAckPsn`达到 `barrierPsn` 后才输出一次 4B Flag DMA。
- ordered credit只在外部实际接受 Flag request后出现；4-entry queue的初始 absolute grant为 4，首个释放反馈为 5。
- 同 QP barrier必须按协议序单调提交，逆序 barrier触发 fail-stop；合法的延迟、已满足 barrier仍会立即发布。
- Flag DMA response同时校验 `orderedSeq/epoch`；错误事件保留原始 QPN和 barrier PSN。
- 同 QP FIFO不越序，不同 QP由 round-robin选择；PSN wrap barrier通过。
- 4-entry queue的第 5个 blocked Flag触发 `RX_ORDERED_QUEUE_FULL` fail-stop。
- 错误 `globalSeq` retire触发 `RX_ORDERED_BAD_RETIRE`。

### 完整 RX 与真实 TX 回环

`rx_top` 场景证明：

1. 四物理入口支持跨 lane乱序；
2. DMA stall期间 `ackPsn=000000`，而 `dmaAckPsn=fffffd`、`retireHead=0`；
3. 跨 lane completion倒序到达不能越过 global head；
4. Ordered Flag在 barrier前不可见，barrier满足后仅写一次；
5. malformed FTile beat报告错误后，后续合法 frame可正常 DMA；
6. RQ bad-rkey sticky fault会在顶层阻断新的 Ordered Flag ingress和 Flag publication；
7. `start` 需要 epoch、network、至少一个 valid QP，重复 QPN 配置必须修正后才能启动。

`tx_rx_loopback` 实例化真实：

```text
mkPacketAssemblyEngine -> mkFourChannelTxScheduler
```

TX 为 500B消息生成三个 frame，实测长度为 298B、298B、82B，对应 PSN `fffffe/ffffff/000000`。测试丢弃所有 monitoring sideband，只把 `data/be/sof/eof`按逆序注入 RX；GPU memory model最终逐 byte等于原始 500B payload，DMA物理地址为 `0x9000`、`0x90e4`、`0x91d8`，最后仅生成一个 500B CQE。

## 调度警告与限制

BSC 报告若干 FIFO enqueue/dequeue和共享错误/状态寄存器的 urgency warning；没有 rule-never-fires、类型、elaboration或 Verilog generation错误。directed和随机回归在 Icarus/Verilator上的结果一致。

尚未验证：

- Quartus综合/P&R、Fmax、资源和功耗；
- 真实 FTile、PCIe、GPU memory与cache可见性；
- PCIe posted-write drain/fence adapter；
- ICRC、RDMA padding、FCS和标准 RoCE/MRC互操作；
- ACK/SACK/NAK网络编码与 CPU retransmission manager；
- 散页 GPU MR、IOMMU/ATS；
- 形式验证、控制面 epoch 持久化机制和长期 64-bit sequence wrap。

因此，当前结论是：五个 RX 子模块和它们的端到端协议语义已达到可综合、双 simulator验证的功能 MVP；不能据此宣称已达到 400G时序或完成真实 GPU板级交付。
