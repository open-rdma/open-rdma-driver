# Ns3-lab

一组从基础网络到 OpenRDMA/Verilator 联合仿真的 ns-3 实验。工程固定使用 **ns-3.48**，每个 Lab 都有明确的
通过条件，不把“仿真跑完”当作验证成功。

## 目录

```text
Ns3-lab/
├── Lab1-PointToPoint/       两节点 UDP、传播与序列化时延
├── Lab2-BottleneckQueue/    瓶颈链路、DropTail、FlowMonitor
├── Lab3-ControlledLoss/     RateErrorModel、seed/run、可复现丢包
├── Lab4-FourPathReorder/    四路径分流与跨路径乱序
├── Lab5-RoceFrameReplay/    M3 风格 RoCEv2 帧解析、PSN wrap、重组
├── Lab6-OpenRdmaTrace/      Verilator M0→M3 单向 trace 离线回放
├── Lab7-OnlineSocketCosim/  Verilator 与 ns-3 在线 Socket 联合仿真
├── common/                  共享 ns-3 工具、RoCE 解析器和 Socket 协议
├── results/                 JSON、CSV 和 PCAP 输出
├── ns-3/                    固定在 ns-3.48 的上游源码
└── Makefile                 统一构建和回归入口
```

## 环境

当前环境使用：

- GCC 11.4；
- Python 3.13；
- CMake 3.31.10（安装在 `~/.local/bin`）；
- ns-3.48。

Ubuntu 22.04 自带的 CMake 3.22 低于 ns-3.48 要求。如果另一台机器没有新版本 CMake，可以执行：

```bash
python3 -m pip install --user cmake==3.31.10
```

工程中的 `ns-3/` 已经下载好。如果目录不存在，`make bootstrap` 会从官方 GitLab 克隆固定 tag。

## 快速开始

```bash
cd OpenRDMA-Offline/Ns3-lab
make build       # 首次配置并编译 ns-3 和七个 Lab
make lab1
make lab2
make lab3
make lab4
make lab5
make lab6        # 需要上一级目录的离线流水线、BSC、Verilator、cocotb 2
make lab7        # 需要 lab7 cocotb suite，本树中已随离线流水线一并移除
```

Labs 1–5 的独立回归：

```bash
make test
```

OpenRDMA 联合回归以及离线/在线等价性检查：

```bash
make test-cosim
```

Lab 3 的重复性检查会执行相同 seed/run 两次，再执行一个不同 run：

```bash
make lab3-repro
```

运行某个 Lab 并覆盖参数：

```bash
make run LAB=lab1 ARGS='--packetCount=500 --dataRate=25Gbps --delay=2us'
```

查看程序参数：

```bash
cd ns-3
./ns3 run 'lab4 --help'
```

## 七个实验

| Lab | 核心问题 | 自动检查 |
|---|---|---|
| 1 | 单链路如何传输 UDP 包 | 无丢包/重复/乱序，payload 正确，时延不低于物理下界 |
| 2 | 过载为什么形成队列和丢包 | queue drop 非零，包数守恒，goodput 不超过瓶颈速率 |
| 3 | 如何制造可复现的随机丢包 | 相同 seed/run 结果一致，不同 run 结果变化 |
| 4 | 四条不同延迟的路径怎样产生乱序 | 每路内部保序，全局发生乱序，记录最大乱序深度 |
| 5 | OpenRDMA M3 风格帧经过网络后能否重组 | 手工 fixture 的协议字段、wrap 和 payload 正确 |
| 6 | 真实 Verilator 输出能否通过离线 trace 进入 ns-3 | 模型 oracle、逐帧字节、四通道、PSN 和消息重组正确 |
| 7 | Verilator 与 ns-3 能否在线同步运行 | 二进制协议、虚拟时间屏障、到达回报和 Lab 6 等价性正确 |

每个目录里的 README 会解释拓扑、运行命令和观察重点。

## 输出

所有生成结果放在 `results/`：

- `lab*.json`：机器可读的验收结果；
- `lab2-queue.csv`：队列深度时间序列；
- `lab1/lab4/lab5/lab6-*.pcap`：链路抓包。

PCAP 可以用以下命令查看：

```bash
tcpdump -nn -tt -r results/lab1-0-0.pcap
```

## 与 OpenRDMA 的关系

Lab 4 使用四个 channel 和不同路径时延，对应 M3 四路输出的网络侧行为。Lab 5 使用手工
生成、与 M3 布局兼容的 Ethernet + IPv4 + UDP/4791 + BTH/RETH fixture。

Lab 6 的 trace 来自上一级目录 `OpenRDMA-Offline/` 的离线流水线：`make lab6-trace` 调用
`make -C .. tx`，由 M0→M3 Verilator 仿真在 M3 EOF dequeue 边界导出 trace 与独立 oracle，
再由 ns-3 离线回放。Lab 7 使用同一个 workload 和时间边界，通过 Unix Domain Socket 在线
发送帧，并把 ns-3 的到达/丢包事件返回 cocotb。

RTL 源码的位置可以覆盖：

```bash
make lab6 OPENRDMA_DIR=/path/to/tx \
  COCOTB_PYTHON=/path/to/cocotb2/bin/python3
```

Lab 7 的 RTL 侧是 `--suite lab7` 这个 cocotb suite，它属于在线联合仿真，不在离线流水线中。
本树保留了 socket 协议与 ns-3 服务端作为该方案的参考实现，运行它需要另行提供该 suite。

Lab 7 当前是 TX + 网络 telemetry，不把返回事件送入 RX RTL；`TxCommit` 也不是网络 ACK。
当前 M3 不生成 RoCE ICRC 或 Ethernet FCS，因此 Labs 5–7 都显式记录为未建模。
