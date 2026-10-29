# OpenRDMA-flow

M0–M3 RDMA TX 数据通路的分模块自持树。四个模块文件夹（m0–m3）**互不依赖**，各自携带 RTL、
tb、cocotb 单测与 Makefile，拷走即用；只有 `integration/` 依赖模块文件夹（它本来就是"集成"）。

```
OpenRDMA-flow/
├── m0/   TxMvp        GDRP submission → slot/MR/QP 表 → PGT 翻译 → physical DMA chunk 请求流
├── m1/   DMA          DmaEngine（乱序 completion → 有序 payload）+ DmaPayloadCompactor（→ M2 拍契约）
├── m2/   PacketAssembly  message → PTU 分片 packet（descriptor 元数据 + payload，不含 wire header）
├── m3/   MrcTxScheduler 四通道 TX 调度 + EV 路径管理 + Ethernet/IP/UDP/BTH 封装 → 4×EthPacket
└── integration/       M0MessageAdapter + DmaPhaseAIntegration（Synchronizer/DoneChecker/CommitTracker
                       内联）+ 端到端 tb/cocotb；BSV 类型经 -p 从 ../m{0..3}/src 解析，不持有类型副本
```

## 运行测试

cocotb 测试必须使用安装了 Cocotb 2 的解释器；可先用
`python3 -c 'import cocotb; print(cocotb.__version__)'` 检查，再通过
`make test PYTHON=/path/to/python3` 显式指定。默认 `SIM=verilator`，也可使用
`SIM=icarus`（negative 套件只支持 Icarus）。

| 套件 | 命令 | 内容 |
|---|---|---|
| m0 单测 | `make -C m0 test` / `make -C m0 test-long` | tx 10 例；long = 4096 submission 随机 |
| m1 DMA | `make -C m1 test` / `test-long` / `test-negative` | dma 13 例 + long + 16 个 expected-fatal |
| m1 Quartus RTL | `make -C m1 check-quartus-rtl` / `quartus-synth` | 导出 production QIP/filelist，检查层次闭包与 Quartus 综合 |
| m1 compactor | `make -C m1 test TARGET=compactor` | 2 例 |
| m2 单测 | `make -C m2 test` / `make -C m2 test-long` | packetizer 9 例；long = 固定种子随机（长度/PMTU/profile/节流/PSN wrap） |
| m3 单测 | `make -C m3 test` / `test-long` / `yosys` | scheduler 9 例 + MRC profile 10 例；long = 双 profile 随机 + 路径状态扰动；含 standalone-verilog + yosys |
| integration | `make -C integration test-integration` / `test-long` | DmaPhaseA 端到端 5 例 + long |
| m0chain | `make -C integration test-m0chain` / `test-m0chain-long` | 真·m0→m3（GDRP → frame），6 例 + long |
| negative | `make -C integration test-negative`（SIM=icarus） | 4 个 expected-fatal |

`make -C integration compile/verilog` 会先跑 `preflight`。

## 两条 wire profile

M3 支持两条互不相同的报文路径，靠 `MessageContext.wireProfile` / `PacketDescriptor.wireProfile`
选择，M2 只负责分片和每包 placement 元数据，真正序列化头部的只有 M3：

| profile | 内容 | 状态 |
|---|---|---|
| `WIRE_LEGACY_PRIVATE` | 私有 Ethernet/IPv4/UDP/BTH，仅 First/Only 带私有序 RETH，固定 UDP source | 默认，`M0→M3` 链路与全部既有测试走这条 |
| `WIRE_MRC_IPV4_ECMP_NO_ICRC` | MRC normal Write `C6/C7/C8/CA`，每包带 METH 和标准序 `VA｜r_key｜dmalen` RETH，UDP source port 承载 ECMP entropy，Last/Only 设置 BTH.AR，含 0–3 字节 RDMA pad | 实验路径，尚未与任何标准实现互操作 |

M3 侧的 EV 控制面（`EvPathManager`）随 MRC profile 一起提供：4 个 profile × 8 个 EV entry、
`GOOD/SKIP/DENIED/ASSUMED_BAD` 状态、ECN/SKIP/ALWAYS_SKIP/timeout/probe 归一化反馈、
local/peer port 位图和 sideband `ProbeRequest`。traffic class → EV profile 的映射由
`MrcSourceProfileConfig` 配置，descriptor 自带的 `wireProfile/evProfileId` 必须与之匹配；M3 还会检查
profile、opcode/packet position、PMTU 分片和 message range（均用 assertion fail-stop）。

**本轮明确未实现**（不要据此声称互操作或合规）：RoCE **ICRC**、IPv6 Structured EV、SRv6、
WriteImm（`C9/CB` + ImmDt + RQMSN）、TSETH、on-wire 的 Reliability SACK/NACK/Probe/Port Status
报文、以及 RX 侧对新实验 wire 的解析。OCP 文档自身的 UDP dport 4791/4971 冲突未在本仓库裁决，
实验路径固定使用 4791。

## 两个完成点

| 信号 | 含义 |
|---|---|
| `TxCommit` | packet 已完整写入 M3 的 per-channel buffer（buffer admission），**不是**上线、不是 ACK |
| `TxEgressEvent` | 该 frame 的 EOF word 已从 channel buffer 取走（真实 EOF 握手），携带 EV profile/index/generation/entropy |

integration 的 request-ID 生命周期仍以 `TxCommit` 回收；`TxEgressEvent` 是纯可观测流，四个
通道的 BFM 与 integration TB 在后台持续排空它（它带反压）。它也不等于 MAC 上线时刻或 RTT 起点。

## 共享类型包（每个模块文件夹自持一份字节一致的副本）

BSC 按包名查文件，同名包不能在同一编译单元里出现两个不同定义。四个模块文件夹按依赖闭包
各自携带一份完整类型包副本，互不 import 对方：

| 类型包 | 持有者 | 说明 |
|---|---|---|
| `CommonTypes.bsv` | m0 m1 m2 m3（4 份） | PipeIn/PipeOut/ServerP/ClientP + mkConnection；四副本字节一致 |
| `TxTypes.bsv`（phasea 版） | m1 m2 m3（3 份） | MessageContext/PacketDescriptor/MessagePayloadBeat/EthPacket/…；`TxPsnCount` 为 18-bit |
| `TxMvpTypes.bsv` | m0（1 份） | M0 自身标量类型 + TxDmaReadRequest |
| `DmaTypes.bsv` | m1（1 份） | M1 自身类型 |

`integration/Makefile` 的 `preflight`（`cmp`）门保证 CommonTypes×4、TxTypes×3、m2/m3 Python
mirrors，以及 `model/common.py`/`lib/cocotb_bsv/*.py`×5 在任何编译前字节一致——副本一旦漂移，
integration 的 -p 包选择或 cocotb packing/driver 行为就会变成顺序相关、静默错误。

同步规则：只允许整树一致的修改（例如给 `CommonTypes.bsv` 加类型时四个副本要一起改）。
模块内只有一份的类型（DmaTypes/TxMvpTypes）没有门，改起来最自由。

## python 镜像与自持

每个文件夹 `cocotb/` 自持其测试所需的全部 python（bfm/model 镜像 + `lib/cocotb_bsv`）。
两个生态（mvp 的 dma/tx 镜像与 phasea 的 m2/m3 镜像）的共享基础件差异是纯追加的：
`model/common.py` 与 `lib/cocotb_bsv` 用的是 phasea 超集版本（多 Bit9/Bit48、
ActionValueMethod），五个文件夹内容一致，能同时服务两套套件。`legacy_frame.py` 已无引用者，
未复制。

## 来源

由 `~/Desktop/Design/open-rdma-design`（paper-ji + implementation-taping 合并工作区）整理而来；
原仓库原样保留。模块文件 = 上游源码 + 本次联调新增/修复（无未提交改动丢失）；
integration 的 m0chain 套件等为本轮新增。本树不依赖原仓库路径，单独拷贝后可在任何位置运行
（integration 需要与 m0..m3 同级目录）。

## 离线流程中的角色

本树是 `OpenRDMA-Offline` 的 TX 一半：`make tx`（顶层 Makefile）编译 `integration` 的
`mkTestM0Chain`，跑 `../../ns3/cocotb/tx_export.py`，把 6 笔 workload 产生的 11 帧导出成
`results/tx-trace.csv` / `tx-frames.csv` / `tx-messages.csv`，交给 ns-3 阶段。
`make test-tx` 则是不经过网络的常规 cocotb 套件（等价于本目录的 `make test-all`）。

与上游的唯一差异：`integration/Makefile` 的 preflight 把 5 处 BSV `cmp -s` 换成了
`cocotb/bsv_identical.py`（剥注释后比较）。原因见顶层 `../README.md`。
