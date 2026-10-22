# OpenRDMA-Offline：TX RTL → ns-3 → RX RTL 离线互联

本树把 TX RTL（OpenRDMA-flow 的 M0–M3 发送链）、ns-3 网络与 RX RTL（mvp/rx 的
`mkRxTop`）串成一条离线端到端流程：TX 产生以太网帧，ns-3 在四路径网络上传输，
RX 收包并落显存。三个阶段各自运行在自己的仿真器中，之间以 CSV/JSON 文件交接，
不共享时钟，也不共享进程。

本目录提供配套文章的离线仿真复现代码与实验手册，从基础网络实验讲起，包含环境配置、运行步骤、结果解读，以及三阶段离线仿真的复现方法。

可以按兴趣选择入口：

- **复现 OpenRDMA 离线仿真**：运行 TX RTL → ns-3 → RX RTL 全流程，核对报文、乱序传输和接收端数据放置，见下方“快速开始”。
- **只学习 ns-3**：无需 RDMA 背景，也无需安装 RTL 仿真工具。先从 [Ns3-lab](Ns3-lab/README.md) 的 Lab 1–4 开始，观察链路时延、排队、丢包和多路径乱序；感兴趣时再运行 Lab 5 的 RoCEv2 帧回放。

```
  ┌──────────────┐   tx-trace.csv    ┌──────────────┐   rx-trace.csv   ┌──────────────┐
  │ TX RTL  (bsc │   tx-frames.csv   │ ns-3 四路径  │   ns3-offline    │ RX RTL  (bsc │
  │ + Verilator) │──►tx-messages.csv─►│ 网络 + 校验  │──►.json─────────►│ + Verilator) │
  │ mkTestM0Chain│   tx-connection   │ offline-txrx │                  │ mkTestRxTop  │
  └──────────────┘   .csv            └──────────────┘                  └──────────────┘
      make tx                            make ns3                           make rx
```

## 快速开始

以下命令在 Linux 环境下执行。只跑 ns-3 实验时，在本目录运行：

```bash
make -C Ns3-lab build    # 首次下载并编译 ns-3.48，需要网络
make -C Ns3-lab lab1     # 从两节点 UDP 实验开始
make -C Ns3-lab test     # Lab 1–5 独立回归，不依赖 TX/RX RTL
```

环境依赖见 [Ns3-lab 环境说明](Ns3-lab/README.md#环境)。实验结果写入 `Ns3-lab/results/`。

复现完整离线流水线时，另需 Bluespec 编译器（`bsc`）、Verilator 和 cocotb 2，
并将 `COCOTB_PYTHON` 指向自己的 Python 环境：

```bash
make COCOTB_PYTHON=/path/to/cocotb2/bin/python3
                # 全流程：TX RTL → ns-3 → RX RTL（等价于 make tx && make ns3 && make rx）
make test       # TX 与 RX 各自的单元/集成 cocotb 套件（不经过 ns-3）
make clean
```

首次运行前先准备 ns-3 引擎（浅克隆 ns-3.48 并编译，需要网络，之后全流程离线可用）：

```bash
make -C Ns3-lab build
```

单阶段重跑：把上一阶段的 CSV 留在 `results/` 里即可。

```bash
make tx         # 只重跑 TX 导出
make ns3        # 只重跑网络（ARGS 可覆盖速率/队列，见 ns3/README.md）
make rx         # 只重跑 RX 回放
```

三个入口都通过 `ns3/cocotb/offline_runner.py` 转调，需要装有 Cocotb 2 的解释器。
Makefile 顶部三个变量可覆盖：

| 变量 | 默认 | 说明 |
|---|---|---|
| `COCOTB_PYTHON` | `/home/l/miniconda3/envs/cocotb2/bin/python3` | cocotb 2 解释器 |
| `NS3_DIR` | `Ns3-lab/ns-3` | ns-3 引擎；不在仓库中，由 `make -C Ns3-lab build` 获取并编译 |
| `OFFLINE_RESULTS` | `results/` | 工件目录 |

## 目录

```
OpenRDMA-Offline/
├── tx/                    TX 树（来自 open-rdma-design/OpenRDMA-flow，m0–m3 + integration）
│   └── integration/       M0→M3 端到端，mkTestM0Chain 是离线流程的 TX 顶层
├── rx/                    RX 树（来自 open-rdma-design/mvp/rx）
│   └── src/RxTop.bsv      mkRxTop；离线流程用 tb/TestRxTop.bsv 的 mkTestRxTop
├── ns3/
│   ├── offline-txrx.cc    ns-3 程序：读 TX 工件 → 四路径网络 → 写 RX 工件
│   ├── common/            roce-frame.h / lab-common.h / four-path-roce.h（自 Ns3-lab 复制）
│   └── cocotb/            三个阶段的驱动 + 共享库（见下）
├── Ns3-lab/               实验 1 至 7 的源码、共享工具与 ns-3 引擎
└── results/               每次运行的工件（可删，make clean 也会删）
```

`ns3/cocotb/` 里：

| 文件 | 作用 |
|---|---|
| `offline_lib.py` | 工件格式（CSV 读写、`PSN_MASK`、`psn_distance`）、路径、wire 头部常量 |
| `tx_export.py` | 阶段 1：驱动 TX RTL，导出 `tx-*.csv` |
| `rx_replay.py` | 阶段 3：把 `rx-trace.csv` 灌进 RX RTL 并核对落显存结果 |
| `offline_runner.py` | 三阶段启动器（`make` 调的就是它） |

本树不依赖树外的任何代码，ns-3 引擎是唯一需要先抓取的部件：`make -C Ns3-lab build`
把 ns-3.48 浅克隆到 `Ns3-lab/ns-3` 并配置编译（仓库不含该引擎，`Ns3-lab/.gitignore`
已排除），此后来源只剩本树自身的 TX/RX RTL。`NS3_DIR` 默认指向它；要换用别的
ns-3 checkout，把 `NS3_DIR` 指过去即可。

## 三个阶段

### 阶段 1 — `make tx`：TX RTL 产生包

`tx/integration` 编译 `mkTestM0Chain`（M0→M1→M2→M3 全链），cocotb 跑固定的 6 笔
workload（4 个 QP、四通道、PMTU 混合、PSN 边界 0xFFFFFE 回绕、两处故意重叠的目标地址），
在每帧离开 M3 出口的时刻记录时间戳，产出四个文件：

| 工件 | 列 |
|---|---|
| `tx-trace.csv` | `frame_id, eof_time_ps, channel, frame_hex`：帧及其离开 M3 的时刻 |
| `tx-frames.csv` | `frame_id, message_id, packet_index, packet_count, request_id, qpn, psn, opcode, payload_offset, payload_length, expected_frame_hex, channel` |
| `tx-messages.csv` | `message_id, request_id, qpn, start_psn, remote_addr, remote_key, message_length, expected_payload_hex` |
| `tx-connection.csv` | `src_mac, src_ip, dst_mac, dst_ip`：声明的 L2/L3 身份 |

`tx-frames.csv`、`tx-messages.csv`、`tx-connection.csv` 是预言机：TX 在其中声明
自己发了什么、连接是什么，供后两阶段核对。`frame_hex` 是完整以太网帧
（Ethernet + IPv4 + UDP + BTH[+RETH] + payload），没有 ICRC/FCS。

这一阶段断言 M3 调度器用满四个已使能通道：commit 流里的 `channel` 集合必须是
{0,1,2,3}；channel 同时写进 `tx-trace.csv` 与 `tx-frames.csv`。`tx-connection.csv`
的地址不是从 DUT 输出反推的：帧先与 `expected_frame(..., CFG)` 逐字节比对通过，
因此该文件描述的必然是 wire 上的取值。

这一阶段的工件已与 `Ns3-lab` 的 lab6 基线逐字节比对过：帧内容、帧/消息表完全一致。

### 阶段 2 — `make ns3`：ns-3 传输

`ns3/offline-txrx.cc` 编译后连到已构建的 ns-3 库上运行：

1. 注入前自检（ValidateTrace）：帧字节与预言机一致；QPN/PSN/opcode/payload 长度自洽；
   trace 的 channel 与预言机一致且四通道全在；每帧 MAC/IP 与 `tx-connection.csv` 一致；
2. 按 `eof_time_ps` 把帧注入 `FourPathRoceNetwork`（2 节点、4 条 PointToPoint、
   时延 {200ns, 400ns, 800ns, 2µs}、100 Gbps、DropTailQueue 100 包；channel 即路径、
   即接收 lane）；
3. 送达后校验（ValidateDeliveries）：无损、字节不变、到达不早于注入、每帧恰好一个
   终态事件；同一路径上的帧保持注入的先后（PointToPoint 链路先入先出）；每笔 message
   重组后与预言机逐字节相等；PSN 与 opcode 序列正确，RETH 字段正确；
4. 统计乱序（AnalyzeReorder）：成对逆序数与最大乱序深度；
5. 写 `rx-trace.csv`（`sequence, frame_id, tx_order, channel, arrival_time_ps, frame_hex`，
   只含真正到达的帧）、`ns3-offline.json`，可选 4 个 pcap。

当前配置下的典型结果：11 帧全部送达，0 丢包，13 对逆序，最大深度 8。

`make ns3 ARGS="..."` 可以把参数透传给这个程序（例如 `ARGS="--dataRate=1Gbps"`）。
`--lossless=0` 打开丢包诊断模式：允许 DropTail 拒收，只校验到达的帧，并报告
`messages_complete`。丢包需要同时调慢速率并调小队列才会出现；丢包后的 trace 不能
用于 RX 回放，因为 RX 没有重传逻辑，缺 PSN 的 message 永远不会 retire。
细节见 `ns3/README.md`。

### 阶段 3 — `make rx`：RX RTL 收包

`rx_replay.py` 驱动 `mkTestRxTop`（Packet Buffer → Parser → RQ Bitmap → DMA Placement →
Ordered Queue）：

* 连接上下文从工件推导，不从 TX 源码导入：QPN、initial PSN（对每个 QP 取到所有
  message 起点前向距离最小的那个）、RKEY、虚拟基址都由 `tx-frames/messages.csv` 推出；
  推导时同时断言 trace 自洽（同一 QP 的 RKEY 与基址一致、PSN 流无洞无重复）。L2/L3
  地址从 `tx-connection.csv` 读声明值配置给 RX，到达帧必须与声明一致。
* 帧按 `arrival_time_ps` 的相对时序喂进四条 lane；DMA 完成逐拍回灌；CQE、feedback、
  credit、error 由后台收走。
* 核对项：无 fault、无 error、无多余 flag 请求；每个 `(QPN, PSN)` 恰好写一次，且地址、
  长度、字节与 TX 预言机一致（workload 里的两处地址重叠因此不会漏检）；CQE 多重集
  `(qpn, lastPsn, bytes)` 精确匹配；`retireHead` 等于帧数；四条 lane 都被用到；两个水位
  （`dmaAckPsn0` 与 `progress0.ackPsn`）停在各自流的最后一个 PSN；`oooCount` 为 0。

结果写 `results/rx-replay.json`。
