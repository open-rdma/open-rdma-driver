# ns-3 实验手册

本手册对应同一棵树中的七个实验：

- 根目录 —— 两个 RTL 联合实验，把 Verilator 仿真的真实收发 RTL 通过一条三阶段离线流水线接到 ns-3 网络上；
- `Ns3-lab/` 子目录 —— 五个纯 ns-3 实验，从两节点之间的一条点对点链路开始，逐步引入队列、可控丢包、多路径乱序与 RoCEv2 帧格式。

每个实验都可以用一条命令运行，结果写入 `results/*.json`，通过条件由断言检查。全部满足时打印 `PASS` 并正常退出；否则断言抛出异常，进程终止。失败时没有 `FAIL` 字样，判断结果请依据退出码。

阅读本手册需要：Linux 命令行基础，C++ 基础，了解 IP、UDP 的基本格式，以及队列溢出会导致丢包这一事实。ns-3 与 Bluespec 都不需要预先学过，第 3 章会讲 ns-3 的基本模型，第 9 章会讲 RTL 侧的模块划分。

---

## 1　实验总览

### 1.1　实验列表

| 实验 | 目录 | 回答的问题 | 通过条件 |
|---|---|---|---|
| 1 | `Ns3-lab/Lab1-PointToPoint` | 一条链路传输 UDP 包，时延由什么决定 | 100 个包全部到达、载荷无误、无重复无乱序、平均时延不低于物理下界 |
| 2 | `Ns3-lab/Lab2-BottleneckQueue` | 输入速率超过链路速率时发生什么 | 队列丢包非零、收包数加丢包数等于发送数、队列深度不超配置、goodput 不超瓶颈 |
| 3 | `Ns3-lab/Lab3-ControlledLoss` | 如何制造可复现的随机丢包 | 相同 seed/run 的两次运行结果逐字节相同 |
| 4 | `Ns3-lab/Lab4-FourPathReorder` | 四条时延不同的路径如何造成乱序 | 每条路径内部保序、全局确实乱序，并记录最大乱序深度 |
| 5 | `Ns3-lab/Lab5-RoceFrameReplay` | RoCEv2 帧经过网络后能否被正确重组 | 协议字段、PSN 回绕、载荷逐字节正确 |
| 6 | `OpenRDMA-Offline`（阶段 1 与 2） | 真实 RTL 产生的帧能否接进网络 | 与帧 oracle 逐字节一致、四通道分配正确、消息重组正确、网络确实乱了序 |
| 7 | `OpenRDMA-Offline`（阶段 3） | 网络送来的乱序输出能否被真实 RX RTL 正确接收 | 每个 `(QPN, PSN)` 恰好写一次且字节正确、每笔消息一个 CQE、全部退休、无 fault |

### 1.2　学习路径

Lab 1 至 Lab 5 是纯 ns-3 实验，只需要编译器、CMake 与 `Ns3-lab/` 这一个子目录。环境配置完成后半小时内可以拿到第一个通过结果。它们由浅入深：Lab 1 讲时延的构成，Lab 2 讲队列，Lab 3 讲随机性与可复现性，Lab 4 讲多路径乱序，Lab 5 讲 RoCEv2 帧格式。Lab 4 与 Lab 5 是 Lab 6 与 Lab 7 的直接铺垫——前者给出了那四条路径，后者给出了帧的格式。

Lab 6 与 Lab 7 在 `OpenRDMA-Offline` 目录下，另需 Bluespec 编译器、Verilator 与 cocotb 2。这两个实验共用一条三阶段流水线：阶段 1 跑 TX RTL 并把帧导出成 CSV，阶段 2 把帧接进 ns-3 网络，阶段 3 把网络的到达记录回灌给 RX RTL。Lab 6 覆盖阶段 1 与阶段 2，Lab 7 覆盖阶段 3。

只想学习 ns-3 的读者可以在 Lab 5 结束后停止。三个阶段的划分方式与工件格式本身也值得一读，第 9 章会解释为什么这样切分。

### 1.3　目录的分工

实验代码全部集中在本手册所在的 `OpenRDMA-Offline/` 树内。五个纯 ns-3 实验和它们依赖的 ns-3 引擎放在子目录 `Ns3-lab/` 下，其余部分在根目录：

| | `OpenRDMA-Offline/Ns3-lab/` | `OpenRDMA-Offline/` 根目录 |
|---|---|---|
| 内容 | 七个 `Lab*/main.cc` 与 `common/` 共享头文件 | TX RTL、RX RTL、ns-3 程序与三阶段驱动 |
| 语言 | C++ | Bluespec、C++、Python |
| 工具链 | g++、CMake、ns-3 | g++、Bluespec、Verilator、cocotb 2、ns-3 |
| 命令前缀 | `make labN`（在 `Ns3-lab/` 下执行） | `make tx` / `make ns3` / `make rx` |

根目录使用 ns-3 引擎，但**不复制**它：ns-3 是一棵已经配置并编译好的多 GB 源码树，复制一份既慢又没有意义。默认情况下它直接使用 `Ns3-lab/ns-3`。换一台机器时，把 `NS3_DIR` 指向任意一棵已经 `./ns3 configure && ./ns3 build` 过的 ns-3 即可。

除 ns-3 引擎外，这棵树不依赖本树之外的任何代码：它自带的 TX 与 RX RTL 就是从零跑通这条流水线所需要的全部源码。

---

## 2　环境与构建

### 2.1　环境要求

前三个实验只需要前三项；Lab 4 起的实验全部依赖已有的 ns-3 构建，Lab 6 与 Lab 7 还需要最后三项 RTL 工具。

| 项目 | 要求 | 用途 | 安装说明 |
|---|---|---|---|
| 操作系统 | Linux | 全部 | 在 Ubuntu 22.04 上验证 |
| 编译器 | g++ 11 或更新 | 全部 | |
| Python | Python 3 | 生成 Lab 5 的测试数据 | |
| 构建工具 | CMake **3.25 或更新** | 仅 Lab 1 至 Lab 5 | 版本要求来自 ns-3.48 自身，见下 |
| BSC | 2023.01 或更新 | 仅 Lab 6 与 Lab 7 | apt 中没有，从 B-Lang 的 GitHub Releases 下载 tarball 解压，把 `bin` 加入 PATH |
| Verilator | 5.036 或更新 | 仅 Lab 6 与 Lab 7 | Ubuntu 22.04 软件源为 4.038，不满足；需源码编译（30 至 60 分钟）或用 conda-forge |
| cocotb | 2 | 仅 Lab 6 与 Lab 7 | 需要指定运行它的 Python 解释器，通过 `COCOTB_PYTHON=` 传入 |
| 其他 | git、make | 全部 | |
| 磁盘 | 约 2.5 GB | 全部 | ns-3 源码与构建产物约 1.5 GB，两个 RTL 树与构建产物约 1 GB |
| CPU | 8 核以上 | 全部 | 仅影响编译速度，不影响结果 |

RTL 侧不经过 CMake，编译由 Bluespec 与 Verilator 各自的驱动完成。因此 Lab 6 与 Lab 7 在 CMake 版本不够的机器上仍然可以运行，只要 ns-3 那一侧已经编译好。

CMake 是最容易卡住的一项。ns-3.48 的 `CMakeLists.txt` 第 4 行是 `cmake_minimum_required(VERSION 3.25..3.25)`，而 Ubuntu 22.04 软件源中的版本是 3.22，不满足要求。用 pip 安装：

```bash
python3 -m pip install --user cmake==3.31.10
```

安装后位于 `~/.local/bin/cmake`。若系统 Python 没有 pip，先执行 `sudo apt install python3-pip`。

Makefile 只使用这个路径：`Makefile:7` 定义 `CMAKE_BIN := $(HOME)/.local/bin/cmake`，`Makefile:29` 用 `test -x` 检查它。系统中其他位置安装的新版 CMake 不会被使用。需要更换时修改该行，或建立软链接。

### 2.2　构建 ns-3 侧

全树只克隆一次：

```bash
git clone <仓库地址> OpenRDMA-Offline
cd OpenRDMA-Offline/Ns3-lab
make build
```

`make build` 依次执行四步：

1. 检查 g++、python3、CMake 是否存在（`bootstrap`，`Makefile:26`）
2. 若 `ns-3/` 目录不存在，从官方 GitLab 浅克隆 `ns-3.48` 标签（`Makefile:36`）
3. 把七个 `Lab*/main.cc` 软链接为 `ns-3/scratch/lab1.cc` 至 `lab7.cc`，`common/` 目录也一并链接（`links`）
4. 配置并编译（`configure` 与 `build`），启用模块为 `core;network;internet;point-to-point;applications;flow-monitor`

第 4 步耗时最长：515 个编译单元，8 核约六分钟。`./ns3 build` 的默认并行度是 CPU 核数减一，由 ns-3 自带的 `ns3` 脚本决定。首次编译较慢属于正常现象。

第 3 步使用软链接而非复制，因此修改 `Lab1-PointToPoint/main.cc` 后 ns-3 侧立即生效，不需要重新链接。

### 2.3　构建 RTL 侧

回到树的根目录，同一份克隆：

```bash
cd ..
make tx
```

`make tx` 会编译 `mkTestM0Chain`（BSC 转 Verilog，Verilator 再编成可执行模型），然后跑一次固定的负载并导出工件，第一次约需两到三分钟。构建产物在 `tx/integration/build/` 下，`make clean` 可以删掉。

三个阶段的命令分别是：

```bash
make tx         # 阶段 1：TX RTL 导出 results/tx-*.csv
make ns3        # 阶段 2：读上面的 CSV，跑网络，写 results/rx-trace.csv 与 ns3-offline.json
make rx         # 阶段 3：把 results/rx-trace.csv 回灌 RX RTL，写 results/rx-replay.json
make            # 全流程，等价于 make tx && make ns3 && make rx
```

`make ns3` 不需要 cmake：`ns3/cocotb/offline_runner.py` 从 `NS3_DIR/build/lib` 里发现 `libns3.<版本>-<profile>` 前缀，把该前缀下的共享库（排除 `-test-`）与 `build/include` 的头文件路径一起链接进来，因此换一个 ns-3 版本不用改任何代码。

Makefile 顶部四个变量可以覆盖：

| 变量 | 默认 | 说明 |
|---|---|---|
| `COCOTB_PYTHON` | `/home/l/miniconda3/envs/cocotb2/bin/python3` | 装了 cocotb 2 的 Python 解释器 |
| `NS3_DIR` | `Ns3-lab/ns-3` | 已编译好的 ns-3 树 |
| `OFFLINE_RESULTS` | `./results` | 工件目录 |
| `ARGS` | 空 | 转给 ns-3 程序的额外参数 |

第一个默认值写死在一台具体机器上，换机器时必须显式传入。只在 `OpenRDMA-Offline` 单独一棵树里跑全流程，命令形如：

```bash
make COCOTB_PYTHON=$(which python3) NS3_DIR=/path/to/ns-3
```

`COCOTB_PYTHON` 必须装了 cocotb 2，三个阶段都由它启动。另外两个变量 `BSC`（Bluespec 编译器路径）与 `SIM`（仿真器，默认 `verilator`）同样可以用 `make 变量=值` 的形式覆盖；`BSC` 找不到时的现象见第 11 章。

`ARGS` 只对阶段 2 有效，用来覆盖链路参数：

```bash
make ns3 ARGS="--dataRate=25Gbps --queueSize=4p"
```

单独跑 TX 与 RX 的 cocotb 套件（不经过 ns-3）：

```bash
make test-tx    # TX 的 m0 至 m3 与 integration 套件
make test-rx    # RX 的 m0 至 m4、rx_top 与 loopback 套件
make test       # 两者
```

### 2.4　运行第一个实验

```bash
make lab1
```

```
Lab 1 PASS: 100 packets, mean delay 1084 ns
```

结果文件 `results/lab1.json` 内容如下：

```json
{
  "lab": 1,
  "scenario": "point-to-point",
  "data_rate": "100Gbps",
  "propagation_delay": "1us",
  "tx_packets": 100,
  "rx_packets": 100,
  "payload_errors": 0,
  "mean_delay_ns": 1084.000,
  "serialization_lower_bound_ns": 81,
  "status": "PASS"
}
```

后续章节引用的 JSON 均为节选，省略了 `lab`、`scenario` 这类固定字段和过长的数组，完整内容以 `results/` 下的文件为准。

### 2.5　结果判定

不要仅凭是否打印 `PASS` 判断结果。ns-3 侧的程序使用一组 `Require` 断言（定义在 `common/lab-common.h`），条件不满足时抛出 `std::runtime_error`，进程异常终止。退出码是可靠判据，但不同调用层次返回的退出码不同：

| 调用方式 | 失败时的退出码 |
|---|---|
| 直接执行二进制 `build/scratch/ns3.48-lab1-debug` | 134（128 + SIGABRT） |
| `./ns3 run ...` | 250，并打印 `Command '...' died with <Signals.SIGABRT: 6>` |
| `make lab1` | 2（make 在配方失败时统一返回 2） |

在脚本或 CI 中不要写死数字，使用 `if ! make lab1; then ...`。

`OpenRDMA-Offline` 的退出码来源与此不同。cocotb 的 `runner.test()` 只在自身运行于 pytest 之下时才把测试结论变成退出码；直接调用时**即使测试失败，模拟器进程也退 0**。`offline_runner.py` 因此在 `runner.test()` 返回之后自行解析 cocotb 写出的 `results-*.xml`，判定失败则中止。它还在每个阶段开始前清掉本阶段的输出工件——否则一次失败的运行会把上一轮留下的 `"status": "PASS"` 保留在 `results/` 里，而 `make ns3` 会继续使用旧的 `tx-*.csv` 通过。

要观察一次失败，可以把发包间隔拉长到超出仿真窗口：

```bash
cd ns-3
./ns3 run --no-build "lab1 --packetCount=100 --intervalNs=200000"
```

100 个包按 200 µs 间隔需要 20 ms 才能发完，而源应用在 5 ms 时被停止（`SetStopTime(MilliSeconds(5))`），实际只发出了 25 个：

```
terminate called after throwing an instance of 'std::runtime_error'
  what():  sender did not emit every packet
```

### 2.6　输出文件

五个纯 ns-3 实验的输出全部位于 `Ns3-lab/results/`，每次运行覆盖上一次的结果；根目录的三阶段流水线写自己的 `results/`，两者互不干扰。除 JSON 外还有两类：

- `lab2-queue.csv`：瓶颈队列的深度时间序列，可直接用于绘图
- `lab1-*.pcap`、`lab4-*.pcap`、`lab5-*.pcap`：链路抓包

`OpenRDMA-Offline` 的输出同样在 `results/` 下，由三个阶段分别写入：

| 文件 | 阶段 | 内容 |
|---|---|---|
| `tx-trace.csv` | 1 | 帧字节与它离开 M3 的时刻、通道 |
| `tx-frames.csv` | 1 | 帧 oracle：逐帧的期望字节与协议字段 |
| `tx-messages.csv` | 1 | 消息 oracle：逐笔消息应有的载荷与目标地址 |
| `tx-connection.csv` | 1 | 连接身份：TX 与 RX 的 MAC / IP |
| `rx-trace.csv` | 2 | 到达记录：到达顺序、帧号、通道、到达时刻、帧字节 |
| `ns3-offline.json` | 2 | 网络统计：帧数、丢包数、逆序对数、乱序深度、逐帧事件表 |
| `ns3-offline-ch*-*.pcap` | 2 | 四条路径上的抓包 |
| `rx-replay.json` | 3 | RX 的接收结果：DMA 写次数、CQE 数、退休条目数、水位 |

阶段之间的接口就是这些文件。删掉 `results/` 只会丢掉上一轮的产物，重跑即可再生成。

抓包文件可以用 tcpdump 查看：

```bash
tcpdump -nn -r results/lab1-0-0.pcap
```

```
19:00:00.000001 IP 10.1.0.1.49153 > 10.1.0.2.9000: UDP, length 1024
19:00:00.000002 IP 10.1.0.1.49153 > 10.1.0.2.9000: UDP, length 1024
```

PCAP 中记录的是仿真时间而非墙上时间。源应用在 1 µs 启动（`SetStartTime(MicroSeconds(1))`），因此第一条记录的时间是 1 µs。显示中的 `19:00:00` 是 tcpdump 在 pcap 不含绝对日期时使用的基准，只有后面的 `.000001` 有意义；加 `-tt` 选项可以只显示 `0.000001`。

### 2.7　目录结构

全树的布局如下。实验 6 与 7 的 RTL 在根目录下，实验 1 至 5 连同它们共用的 ns-3 引擎收在 `Ns3-lab/` 子目录里：

```
OpenRDMA-Offline/
├── tx/                          TX 树
│   ├── m0/ … m3/                M0 至 M3 四个模块与各自的 cocotb 套件
│   └── integration/             M0→M3 端到端，mkTestM0Chain 是流水线的 TX 顶层
├── rx/                          RX 树
│   ├── m0_packet_buffer/ … m4_ordered_queue/
│   ├── src/RxTop.bsv            mkRxTop，流水的 RX 顶层
│   └── VALIDATION.md            RX 的验证记录
├── ns3/
│   ├── offline-txrx.cc          ns-3 程序：读 TX 工件 → 四路径网络 → 写 RX 工件
│   ├── common/                  共享头文件，与 Ns3-lab/common 同源
│   └── cocotb/                  三个阶段的驱动与共享库
├── results/                     每次运行的工件，被 .gitignore 忽略，可删
├── Makefile                     make tx / ns3 / rx / test
├── README.md
├── TUTORIAL.md                  本手册
├── .gitignore                   忽略 results/ 与各处的 build/ 产物
└── Ns3-lab/                     实验 1 至 5，以及七个实验共用的 ns-3 引擎
    ├── common/                  共享头文件
    │   ├── lab-common.h             报文头、UDP 收发器、时延统计、Require
    │   ├── roce-frame.h             RoCEv2 帧解析
    │   ├── four-path-roce.h         四通道网络与二层注入
    │   └── socket-protocol.h        Unix 域套接字二进制协议
    ├── Lab1-PointToPoint/ … Lab7-OnlineSocketCosim/
    │   └── main.cc                  每个实验一个文件，另有各自的 README 与 test-data/
    ├── results/                 所有输出，被 .gitignore 忽略
    ├── ns-3/                    ns-3.48 源码，由 make build 获取，不入库
    ├── Makefile                 make labN
    ├── README.md
    └── TUTORIAL.md              指向本手册的说明
```

`Ns3-lab/Lab6-OpenRdmaTrace/` 与 `Lab7-OnlineSocketCosim/` 是这两个实验早期的一版实现：RTL 与 ns-3 同时运行，通过 Unix 域套接字同步。本手册改用根目录下的三阶段离线流水线，两者取舍见 9.5.5 节。这两个目录仍留在树中，作为在线方案的参考实现；它们的 RTL 侧原本取自另一棵 `OpenRDMA-flow`，那份代码现已并入根目录的 `tx/`，Lab 6 的 `make lab6-trace` 因此改为直接调用上一级的流水线，Lab 7 所需的 `lab7` cocotb 套件则不在树内。

---

## 3　ns-3 基础

### 3.1　离散事件仿真

ns-3 不在固定的时间步长上循环推进，而是维护一个按时间排序的事件队列。每一步取出最早的事件，把仿真时间直接跳到该事件的时刻并执行它，然后再取下一个。时间跳跃式前进，两个事件之间没有消耗任何计算。

由此产生几个结果：

- 仿真时间与墙上时间无关。"仿真 1 秒"可能只花几毫秒的墙上时间，也可能更久，取决于事件的数量。
- 时延是精确的。一个包到达与发出的时刻是两个事件的登记时间，相减即可，不需要数值积分，也不会因为步长而失真。
- 结果可复现。不涉及随机数时，同样的事件顺序每次都一样。

一个包从发送端到接收端，在 ns-3 内部是一串事件：应用发送、设备开始传输、信道传播结束、对端设备收到、向上递交到应用。每个事件都登记在未来某个确定时刻。理解这一点，后面所有关于时延的计算都只是把几个事件时刻相减。

### 3.2　节点、设备与信道

| 概念 | 对应类 | 作用 |
|---|---|---|
| 节点 | `Node` | 容器，容纳协议栈与应用，本身不做别的事 |
| 应用 | `Application` | 产生或消费数据的一端 |
| 网络设备 | `NetDevice` | 协议栈与信道之间的接口，相当于网卡 |
| 信道 | `Channel` | 传输介质，决定传播时延 |
| 队列 | `Queue` | 挂在设备之前，包在这里排队等待发送 |

一个包从应用到线缆，依次经过：应用 → Socket → UDP → IPv4 → 流量控制层（队列）→ 网络设备 → 信道。每经过一层就加上该层的头。接收端反向剥离，最后交给对端应用。

队列是理解拥塞的关键：设备同一时刻只能发送一个包，后来的包若发现设备正忙，就在队列里等待。队列满了，新到的包被直接丢弃，这就是队列丢包。

### 3.3　公共代码

七个实验共用 `common/lab-common.h`，建议先通读该文件。它包含四个组件。

**`LabHeader`**：每个 UDP 载荷的前 24 字节，含 magic `"NS3L"`、messageId、sequence、channel、txTimeNs。接收端依据它判断丢包、乱序与重复。txTimeNs 是发送时刻，接收端用它减去当前时间得到时延；两端位于同一仿真器内，时间戳精确，不涉及时钟同步。

**`UdpSequenceSource`**：ns-3 的 `Application` 子类，按固定间隔发包，通道分配规则为 `channel = sequence % 目的地址数`，多个目的地址对应多条路径。

**`UdpCollector`**：统计载荷错误、重复包、乱序包与最大乱序深度，同时计算平均时延和吞吐。

**`Require(bool, msg)`**：条件为假时抛出异常。整个仓库的通过条件都由它书写。

载荷是确定性的，第 i 个字节为 `(sequence * 17 + i * 31) & 0xff`。不含随机数，同一序号重发的内容完全相同，接收端才能逐字节校验。

---

## 4　Lab 1　点对点链路的时延

### 4.1　目的

观察一个 UDP 包从发送到接收所经历的时间由哪些部分构成，并确认仿真结果与手工计算一致。

### 4.2　拓扑与参数

两个节点，一条点对点链路。

```
Sender ---------------------------------- Receiver
   10.1.0.1          100 Gbps              10.1.0.2
                    1 us (each way)
```

| 参数 | 默认值 |
|---|---|
| 链路速率 | 100 Gbps |
| 单向传播时延 | 1 µs |
| UDP 载荷 | 1024 字节 |
| 发包数量 | 100 |
| 发包间隔 | 1 µs |

一个节点运行 `UdpSequenceSource`，另一个运行 `UdpCollector`。包从发送端单向流向接收端，接收端不回复。

### 4.3　运行

```bash
make lab1
```

```
Lab 1 PASS: 100 packets, mean delay 1084 ns
```

### 4.4　结果

`results/lab1.json` 中的关键字段：

| 字段 | 值 |
|---|---|
| `tx_packets` | 100 |
| `rx_packets` | 100 |
| `payload_errors` | 0 |
| `mean_delay_ns` | 1084.000 |
| `serialization_lower_bound_ns` | 81 |

### 4.5　分析

#### 4.5.1　时延由两部分组成

一个包从发送端到接收端，路上花的时间由两部分构成：

**传播时延**：信号在介质上跑完这段距离所需的时间。由链路长度决定，本实验设为 1 µs，与包的大小无关。

**序列化时延**（也称发送时延）：把一个包的全部比特推上线缆所需的时间，等于帧长除以链路速率。

一个 1024 字节的载荷从 UDP 向下走，每层都会加上自己的头：

| 层 | 字节 |
|---|---|
| UDP 载荷（含 24 字节 lab 头） | 1024 |
| UDP 头 | 8 |
| IPv4 头 | 20 |
| PPP 头（点对点设备的链路层头） | 2 |
| 线上帧长 | 1054 |

序列化时延为 1054 × 8 / 100×10⁹ = 84.32 ns。加上 1000 ns 的传播时延，合计 1084.32 ns。ns-3 默认时间分辨率为纳秒，取整后得到 1084，这就是 `mean_delay_ns` 的来源。

即：**平均时延 = 传播时延 + 序列化时延**。

这个划分适用于后面每一个实验。时延发生变化时，先判断是哪一项变了：改链路速率或包长影响序列化，改链路长度或传播时延参数影响传播。

还有第三项在本实验中不出现，但真实网络里往往占主导——**排队时延**，即包在队列里等待设备空闲的时间。本实验的链路不会拥塞，队列始终为空，那属于 Lab 2 的内容。

上面的帧长可以在抓包文件里核对：`results/lab1-0-0.pcap` 中每条记录的长度就是 1054，减去 2 字节 PPP 头即为 IP 总长。

> 点对点链路不做 ARP 地址解析，因此第一个包不会像官方教程中总线/CSMA 拓扑那样慢一些。原因见附录 B 第 1 条。

#### 4.5.2　MTU 与分片

每个链路和设备都有一个最大传输单元（MTU），点对点设备的默认值是 1500 字节。超过 MTU 的 IP 包会被切分成若干片分别发送，在接收端再拼回去。分片发生在 IP 层，对上层透明。

把载荷改为 2024 字节：

```bash
./ns3 run --no-build "lab1 --packetCount=10 --packetSize=2024 --output=/tmp/a.json --pcapPrefix=/tmp/a"
```

```
Lab 1 PASS: 10 packets, mean delay 1166 ns
```

按 4.5.1 的公式，帧长为 2054 字节，序列化 164.32 ns，时延应为 1164。实测 1166，多出约 2 ns，原因正是分片：切成两片之后，第二片必须自己再带一个 20 字节的 IP 头。

在抓包文件中可以直接看到：

```bash
tcpdump -nn -r /tmp/a-0-0.pcap
```

```
19:00:00.000001 IP 10.1.0.1.49153 > 10.1.0.2.9000: UDP, length 2024
19:00:00.000001 IP 10.1.0.1 > 10.1.0.2: ip-proto-17
```

第二条记录是非首片，没有 UDP 头可供解析，因此 tcpdump 显示为 `ip-proto-17` 而非 `UDP`。两条记录的 IP 层信息如下：

| 记录 | 帧长 | IP 总长 | MF（还有后续分片） | 分片偏移 |
|---|---|---|---|---|
| 第一片 | 1502 | 1500 | 1 | 0 |
| 第二片 | 574 | 572 | 0 | 185（185 × 8 = 1480） |

分片有代价：额外的 IP 头、接收端的重组开销，以及任何一片丢失都会导致整个数据报被丢弃。这也是路径 MTU 发现机制存在的原因。

### 4.6　练习

**练习 1.1　修改链路速率**

```bash
./ns3 run --no-build "lab1 --dataRate=25Gbps --output=/tmp/r25.json"
```

平均时延由 1084 变为 1337。传播时延不变，多出的 253 ns 全部来自序列化：1054 × 8 / 25×10⁹ = 337.28，原 100 Gbps 下为 84.32，相差 252.96。`serialization_lower_bound_ns` 也按新速率重算，为 327。

> **Hint:** 修改速率只影响序列化，不影响传播。这个结论在后续每个实验中都要用到。

**练习 1.2　找到 UDP 载荷的上限**

```bash
./ns3 run --no-build "lab1 --packetCount=1 --packetSize=65507 --output=/tmp/big.json"
./ns3 run --no-build "lab1 --packetCount=1 --packetSize=65508 --output=/tmp/big2.json"
```

第一条通过，时延 6313 ns；第二条失败：

```
what():  UDP send failed
```

65507 是 UDP 载荷的上限，等于 IPv4 总长上限 65535 减去 IP 头 20 字节和 UDP 头 8 字节。超过它，`UdpSequenceSource` 的发送调用直接失败。

> **Hint:** 65507 字节的数据报在 1500 字节 MTU 下被切成 45 片。传播时延只占 1000 ns，其余 5313 ns 全部是序列化开销。这就是小包不利于吞吐的原因：包越小，协议头占比越高，同样的链路速率能承载的有效数据越少。

---

## 5　Lab 2　瓶颈链路与队列

### 5.1　目的

观察输入速率超过链路速率时缓冲区的行为，并确认队列丢包与链路带宽之间的关系。

### 5.2　拓扑与参数

三个节点，两条链路。入口链路快、瓶颈链路慢，源端的发送速率高于瓶颈速率。

```
Sender ----------- Router ----------- Receiver
        100 Gbps            25 Gbps
         100 ns              1 us
                       [queue: 32 packets]
```

| 参数 | 默认值 |
|---|---|
| 源端发送速率 | 40 Gbps |
| 发包数量 | 2000 |
| UDP 载荷 | 1200 字节 |
| 瓶颈队列容量 | 32 个包 |

### 5.3　运行

```bash
make lab2
```

```
Lab 2 PASS: received=1251 dropped=749 max_queue=32
```

### 5.4　结果

`results/lab2.json` 中的关键字段：

| 字段 | 值 |
|---|---|
| `tx_packets` | 2000 |
| `rx_packets` | 1251 |
| `queue_disc_drops` | 749 |
| `flow_monitor_lost_packets` | 749 |
| `max_queue_packets` | 32 |
| `goodput_gbps` | 24.385 |

### 5.5　分析

#### 5.5.1　丢包的守恒关系

1251 + 749 = 2000。收包数加丢包数等于发送数，不多不少。这条守恒是断言的一部分，用来保证丢包不是被其他环节悄悄消耗掉的（路由错误、载荷校验失败等）。

`max_queue_packets` 恰好等于配置的 32。队列满了才丢包，因此丢包数量由队列容量和持续过载时间共同决定，而不是由某个"丢包率参数"决定。这是队列型丢包与 Lab 3 随机丢包的根本区别。

#### 5.5.2　goodput 的上限

`goodput_gbps` 为 24.385，紧贴 25 Gbps 的瓶颈但略低。原因在于链路上传输的不只是有效载荷：每个帧都带 30 字节协议开销（8 UDP + 20 IPv4 + 2 PPP），有效载荷只有 1200 字节。因此 25 Gbps 的链路最多提供

```
25 × 1200 / 1230 = 24.390 Gbps
```

实测 24.385，比这个上限低 0.02%，差异来自纳秒取整和吞吐统计的时间跨度，可以忽略。断言为 `goodput <= bottleneck * 1.01`，留有 1% 余量。

**链路带宽**是物理上限，**吞吐**是应用实际拿到多少有效数据，两者之间的差距是协议开销。包越小差距越大，载荷为 64 字节时开销占比从 2.4% 升至 32%。这是小包在大带宽链路上跑不快的原因。

#### 5.5.3　两层队列

ns-3 中每个网络设备有两层缓冲：

1. **设备队列**（`DropTailQueue`），由 `PointToPointHelper::SetQueue` 设置
2. **流量控制层**（queue disc），由 `TrafficControlHelper` 安装

问题在于，ns-3.48 会在 `Ipv4AddressHelper::Assign()` 时自动安装一个 `FqCoDelQueueDisc`（`src/internet/helper/ipv4-address-helper.cc:171-172` → `TrafficControlHelper::Default()`，位于 `src/traffic-control/helper/traffic-control-helper.cc:97`；单队列设备走第 104 行的 `SetRootQueueDisc("ns3::FqCoDelQueueDisc")`）。FqCoDel 是主动队列管理算法，有自己的排队与丢弃逻辑，会完全覆盖掉想要观察的 DropTail 行为。

因此 Lab 2 在配置队列之前先执行两步（`Lab2-BottleneckQueue/main.cc:105-112`）：

```cpp
trafficControl.Uninstall(slowDevices.Get(0));
trafficControl.SetRootQueueDisc("ns3::FifoQueueDisc", "MaxSize",
                                StringValue(std::to_string(queuePackets) + "p"));
```

先卸载自动安装的 queue disc，再安装最朴素的 FIFO。顺序颠倒则不起作用。自己编写 ns-3 程序时，如果队列参数怎么调都没有反应，应先检查是否被自动安装的 queue disc 接管。

#### 5.5.4　队列容量的影响

把队列容量改为 4 个包：

```bash
./ns3 run --no-build "lab2 --queuePackets=4 --output=/tmp/q4.json --queueTrace=/tmp/q4.csv"
```

丢包由 749 增至 777，`max_queue` 变为 4，而 goodput 仍为 **24.385**，没有变化。队列容量只决定丢多少，瓶颈速率才决定能通过多少。

#### 5.5.5　队列深度曲线

`results/lab2-queue.csv` 记录队列深度的每次变化，表头为 `time_ns,packets_in_queue`，约 2500 行。它记录变化而非定时采样，因此时间戳不连续。可以从中观察队列如何填满，以及之后"满了就丢、丢后继续填"的稳态。

### 5.6　练习

**练习 2.1　降低输入速率**

```bash
./ns3 run --no-build "lab2 --offeredRate=20Gbps --output=/tmp/o20.json"
```

实验失败：

```
what():  the configured overload did not fill the queue
```

这不是故障，而是没有出现预期的现象。`Lab2-BottleneckQueue/main.cc:153` 有一条断言：

```cpp
ns3lab::Require(dropped > 0, "the configured overload did not fill the queue");
```

20 Gbps 低于 25 Gbps 的瓶颈速率，队列永远填不满，丢包为 0，断言因此拦截。

本仓库的每个实验都检查两件事：**该成立的不变量成立**，以及**该发生的现象确实发生**。否则一个什么都不做的程序也能通过测试——"没有丢包"与"根本没有流量"在统计上无法区分。

**练习 2.2　观察队列深度曲线**

```bash
head -20 results/lab2-queue.csv
```

> **Hint:** 结合 5.5.4 节的结论想一想：把队列容量从 32 改成 4，这条曲线会怎么变，而 `goodput_gbps` 会不会变。

---

## 6　Lab 3　可控丢包与可复现性

### 6.1　目的

用 `RateErrorModel` 按固定概率丢包，并使实验可复现。

### 6.2　拓扑与参数

两个节点，一条链路，链路上挂一个按概率丢弃数据包的错误模型。

```
Sender ---------x-------------------x----- Receiver
                 error model: 8% loss
            100 Gbps, 1 us (each way)
```

| 参数 | 默认值 |
|---|---|
| 发包数量 | 1000 |
| 丢包率 | 8% |
| seed | 20260914 |
| run | 1 |

### 6.3　运行

```bash
make lab3
```

```
Lab 3 PASS: seed=20260914 run=1 lost=78 signature=0xcc4903a5c927a61
```

### 6.4　结果

`results/lab3.json` 中列出了具体的丢包序号（`missing_sequences`）：

```
7,37,40,41,68,91,97,104,132,135,145,167,170,193,195,200,234,242,248,269,289,
304,309,322,330,336,351,364,369,374,396,398,399,415,428,450,467,507,508,519,
528,534,540,566,592,593,600,602,606,618,635,636,641,642,646,662,664,733,738,
741,747,787,792,797,807,808,815,838,842,851,874,893,903,917,920,926,967,969
```

1000 个包中丢失 78 个，设定值为 8%，期望 80 个，实测接近期望。

8% 的丢包率**不等于**"每 100 个包丢 8 个"。每个包独立地以 8% 的概率被丢弃，实际丢弃数量会在期望值附近上下波动，1000 个包的标准差约为 8.6，落在 66 至 94 之间都属于正常范围。要确认丢包率参数是否准确，需要跑几万个包，或者直接检查随机数发生器的输出。

`missing_sequences` 中包含 40、41 以及 398、399、507、508 这样的连续对。独立随机丢包本来就会出现相邻的丢包，这并不说明丢包之间有相关性。

### 6.5　分析

#### 6.5.1　seed 与 run 的设置顺序

`Lab3-ControlledLoss/main.cc` 第 56 至 61 行：

```cpp
auto errorModel = CreateObject<RateErrorModel>();          // 56
errorModel->SetAttribute("ErrorUnit", EnumValue(RateErrorModel::ERROR_UNIT_PACKET));
errorModel->SetAttribute("ErrorRate", DoubleValue(lossRate));
errorModel->AssignStreams(7);                              // 59
RngSeedManager::SetSeed(seed);                             // 60
RngSeedManager::SetRun(run);                               // 61
```

顺序颠倒了。ns-3 的随机数不是每个对象一条独立的流，而是一个全局流集合：谁先 `CreateObject` 一个随机变量，谁就先取走一段。`RateErrorModel` 在第 56 行创建时已经取得随机流，第 60、61 行再设置 seed 和 run 对它不再有影响。

用三个不同的 seed 各运行一次即可验证：

```bash
cd ns-3
./ns3 run --no-build "lab3 --seed=1 --output=/tmp/s1.json"
./ns3 run --no-build "lab3 --seed=999 --output=/tmp/s999.json"
./ns3 run --no-build "lab3 --seed=20260914 --output=/tmp/s0.json"
```

三个文件的 `loss_signature` 全部为 `0xcc4903a5c927a61`，没有任何变化。把 `--run` 由 1 改为 2 同样如此。

因此 `--seed` 与 `--run` 在当前实现下不改变结果。实验仍然可复现，但可复现性来自 ns-3 默认 seed 固定，而非这两个开关。

正确做法是把 `CreateObject<RateErrorModel>()` 及其属性设置移到 `SetSeed`/`SetRun` 之后。修改后重新执行上面三条命令，签名应各不相同。

这类问题的特点是：程序不崩溃，结果也不异常，只是参数悄悄失效，只有对比运行结果才能发现。自行编写仿真时，`SetSeed`/`SetRun` 必须放在任何随机对象创建之前。

#### 6.5.2　可复现性检验

```bash
make lab3-repro
```

该目标执行三步：

1. 用相同 seed/run 运行两次，用 `cmp` 要求两个 JSON 逐字节相同（`Makefile:73-74`）
2. 用不同 run 再运行一次，要求结果不同（`Makefile:76-78`）
3. 打印 `Lab 3 reproducibility PASS`

第 1 步是实打实的：`lab3-a.json` 与 `lab3-b.json` 确实逐字节相同，包括 `missing_sequences` 中那 78 个数字的顺序。

第 2 步的意图正确，但目前不生效。`cmp -s` 比较的是整个文件，而 JSON 中有一行 `"run": 1` / `"run": 2`，仅这一行不同就足以让 `cmp` 判定两个文件不同：

```
$ diff results/lab3-a.json results/lab3-c.json
5c5
<   "run": 1,
---
>   "run": 2,
```

丢包的位置、数量与签名完全相同。

要让第 2 步真正生效，需先修正 seed/run 的设置顺序；或者在比对时排除 `run` 字段——那样该目标现在就会失败，正好暴露问题。

#### 6.5.3　丢包序列的指纹

`0xcc4903a5c927a61` 是把丢包序列哈希成一个指纹，使用 FNV 风格的滚动哈希，实现在 `LossSignature` 中。修改代码后对比该值即可知道丢包模式是否改变，比核对 78 个数字方便。它用 `std::hex` 直接输出，不补前导零，因此位数可能是 15 位或 16 位。

该实现使用的偏移基数是 `1469598103934665603`，而标准 FNV-1a 64 位使用 `14695981039346656037`，相差一位。作为指纹工作正常，但它不是标准 FNV，不应与其他 FNV 实现的结果对比。

#### 6.5.4　丢包率的两个维度

丢包率只回答"丢多少"，不回答"怎么丢"。同样 8% 的丢包率，可以是均匀散布的孤立丢包，也可以是成片连续丢包，两者的网络含义完全不同。区分它们要看连续丢包的段长分布。

把丢包率改为 50% 后统计段长（方法见练习 3.3），结果如下：

| 丢包率 | 连续段数 | 最长 | 平均段长 |
|---|---|---|---|
| 8% | 71 | 2 | 1.10 |
| 50% | 247 | 13 | 2.02 |

平均段长由 1.10 升至 2.02。这是独立随机丢包的必然结果：丢包率越高，两个丢包相邻的概率越大。

真实网络中的拥塞丢包往往是连续的——队列满时会连续丢弃一批包。对比段长分布就能看出一个丢包模型是否接近真实网络。

### 6.6　练习

**练习 3.1　改为 0% 丢包**

```bash
./ns3 run --no-build "lab3 --lossRate=0.0 --output=/tmp/l3-0.json"
```

`Lab3-ControlledLoss/main.cc:95` 有一条专门的分支：丢包率为 0 时 `missing_sequences` 必须为空。这防止出现"参数忘了传、结果看起来正常"的情况。输出的 `lost=0` 是断言实际检查过的结论，不是未检查的默认值。

**练习 3.2　修正 seed/run 的设置顺序**

把 `CreateObject<RateErrorModel>()` 及其属性设置移到 `SetSeed`/`SetRun` 之后，重新编译并运行 6.5.1 节中的三条命令，确认三个 seed 得到三个不同的签名。

> **Hint:** 修改之后，`make lab3-repro` 的第 2 步也会真正生效。

**练习 3.3　统计连续丢包的段长**

```bash
./ns3 run --no-build "lab3 --lossRate=0.5 --output=/tmp/l3-50.json"
```

```python
import json

missing = json.load(open('/tmp/l3-50.json'))['missing_sequences']
runs, current = [], 1
for previous, nxt in zip(missing, missing[1:]):
    if nxt == previous + 1:
        current += 1
    else:
        runs.append(current)
        current = 1
runs.append(current)

print('连续段数', len(runs), '最长', max(runs),
      '平均段长', round(sum(runs) / len(runs), 2))
```

> **Hint:** 结果与 6.5.4 节表格中的 50% 一行一致。把 `--lossRate` 换成其他值，观察平均段长如何变化。

---

## 7　Lab 4　四路径与乱序

### 7.1　目的

观察多条时延不同的并行路径如何导致包到达顺序与发送顺序不一致，并量化乱序的程度。

### 7.2　拓扑与参数

两个节点之间四条并行的点对点链路，速率均为 100 Gbps，传播时延各不相同。

```
                   200 ns
        +-------------------------------+
        |            400 ns             |
Sender  +-------------------------------+  Receiver
        |            800 ns             |
        +-------------------------------+
        |             2 us              |
        +-------------------------------+
        sequence % 4 selects the path
```

| 参数 | 默认值 |
|---|---|
| 四条路径的传播时延 | 200 ns、400 ns、800 ns、2 µs |
| 发包数量 | 80 |
| UDP 载荷 | 256 字节 |
| 发包间隔 | 100 ns |
| 通道分配 | `sequence % 4` |

### 7.3　运行

```bash
make lab4
```

```
Lab 4 PASS: reordered=38 max_depth=17
```

### 7.4　结果

`results/lab4.json` 中的关键字段：

| 字段 | 值 |
|---|---|
| `tx_packets` | 80 |
| `rx_packets` | 80 |
| `reordered_packets` | 38 |
| `max_reorder_depth` | 17 |
| `channel_counts` | `[20,20,20,20]` |

80 个包全部到达，没有丢包。链路上没有过载，本实验只考察时延差异。

`arrival_order` 的前 16 项为：

```
0, 1, 4, 5, 2, 8, 9, 6, 12, 13, 10, 16, 17, 14, 20, 3
```

### 7.5　分析

#### 7.5.1　到达顺序的成因

以包 0 的发送时刻为 0 计算。线路上帧长为 256 + 8 + 20 + 2 = 286 字节，100 Gbps 下序列化时延为 22.88 ns，四个包相同，不影响先后顺序。

| 包 | 发出时刻 | 序列化 | 路径时延 | 到达时刻 |
|---|---|---|---|---|
| 2 | 200 ns | 22.88 ns | 800 ns | 1022.88 ns |
| 4 | 400 ns | 22.88 ns | 200 ns | 622.88 ns |
| 3 | 300 ns | 22.88 ns | 2 µs | 2322.88 ns |
| 20 | 2000 ns | 22.88 ns | 200 ns | 2222.88 ns |

包 4 比包 2 晚发 200 ns，但路径快 600 ns，净提前 400 ns，插到了包 2 前面。包 3 要到 2322.88 ns 才到达，而比它晚发的包 20 在 2222.88 ns 就已经收到。

到达序列因此呈现"每四个一组、组内快路径的包先到"的结构，包 3 被推迟到最后——它就是使 `max_reorder_depth` 达到 17 的那个包。

#### 7.5.2　路径内部保序

`Lab4-FourPathReorder/main.cc:79-80` 有一条断言：

```cpp
ns3lab::Require(static_cast<int64_t>(record.sequence) > previousSequence[record.channel],
                "a path reordered its own packet stream");
```

它按通道记录上一次收到的序号，要求递增。同一条链路的传播时延固定，只要路径内部先进先出，先发的必然先到。

这条断言是**不变量**，不是本实验研究的现象，在本实验中也很难被触发：FIFO 队列拥塞时只会丢包，不会让后发的包先出去，而丢包会被 `missing.empty()` 捕获。要让它失败，需要给某条路径安装会重排的队列（例如多队列调度器让不同流互相超车），那属于另一个实验。

另一条断言：

```cpp
ns3lab::Require(record.sequence % 4 == record.channel, "packet arrived on the wrong path");
```

序号决定包应走的路径，收到时必须一致。这条断言失败说明分流逻辑或通道编号错位。

#### 7.5.3　乱序深度的定义

`max_reorder_depth` 的定义在 `common/lab-common.h:262-267`：

```cpp
if (m_haveHighest && sequence < m_highestSequence)
{
    ++m_reorderedPackets;
    m_maxReorderDepth = std::max(m_maxReorderDepth, m_highestSequence - sequence);
}
m_highestSequence = m_haveHighest ? std::max(m_highestSequence, sequence) : sequence;
```

一个包到达时，如果其序号小于已见过的最大序号，就算作乱序，深度为两者之差，取整个过程的最大值。

它表示的不是"缓冲区中积压了几个包"，而是"这个包落后于领先边沿多远"。这两个定义在真实实现中都存在，阅读代码时不要混淆。

回到数据：包 3 到达之前，已经收到 0、1、4、5、2、8、9、6、12、13、10、16、17、14、20，最大序号为 20。20 − 3 = **17**。

一个走最慢路径的包在路上被领先了 17 个包。接收端若只有一个重排窗口，17 就是它至少需要容纳的深度。

#### 7.5.4　发送间隔的影响

```bash
./ns3 run --no-build "lab4 --intervalNs=2000"
```

```
what():  path delay spread produced no global reorder
```

乱序消失。四条路径时延的最大差值为 2 µs − 200 ns = 1800 ns。只要发送间隔大于 1800 ns，任何包在被下一个追上之前就已到达，全局乱序不再发生。

乱序是"时延差异"与"发送速率"共同作用的结果，仅有前者不够。默认间隔 100 ns（`Lab4-FourPathReorder/main.cc:16`）远小于 1800 ns，因此乱序明显。

与 Lab 2 一样，这里也有要求"现象必须发生"的断言（`Lab4-FourPathReorder/main.cc:88`）。把参数调到不乱序，实验不会假装通过。

### 7.6　练习

**练习 4.1　使四条路径时延相同**

修改 `Lab4-FourPathReorder/main.cc:36`，四项都填 `"200ns"`，重新执行 `./ns3 build` 后再运行。

```
what():  path delay spread produced no global reorder
```

乱序消失，`main.cc:88` 那条"现象必须发生"的断言拦截了实验。这次失败本身就是结论：乱序来自时延差异，而不是来自并行本身。

**练习 4.2　缩短最慢路径**

把 2 µs 改为 300 ns，时延顺序变为 200 / 300 / 400 / 800 ns。实验仍然通过，但会以另一种模式乱序，观察 `arrival_order` 的结构变化。

> **Hint:** 最大时延差由 1800 ns 缩小到 600 ns。按 7.5.4 节的规律推算，`max_depth` 会明显变小。

---

## 8　Lab 5　RoCEv2 帧解析与重组

### 8.1　目的

把一个真实格式的 RoCEv2 帧（Ethernet / IPv4 / UDP 4791 / BTH / RETH / 载荷）送入网络，验证解析、按 PSN 排序与消息重组的正确性。

前四个实验使用自定义的 24 字节报文头（`LabHeader`），本实验改用真实协议格式。这是一个新的起点：从本章开始进入 RDMA 相关内容。

本实验不需要 RTL 工具链，也不需要 OpenRDMA 源码。帧是固定的十六进制数据，由 `Lab5-RoceFrameReplay/generate_trace.py` 生成后写入 `test-data/m3-trace.csv`，整个实验自足。

### 8.2　拓扑与参数

网络与 Lab 4 相同，只是把注入的内容换成了 RoCEv2 帧，并且只发送 4 个。

```
                 200 ns
        +-----------------------------+
        |           400 ns            |
4 RoCE  +-----------------------------+  按 PSN 排序
frames  |           800 ns            |  → 重组为 2 条消息
        +-----------------------------+
        |            2 us             |
        +-----------------------------+
```

这 4 个帧的构造如下：

| 发送时刻 | 通道 | QPN | PSN | 操作码 | 内容 |
|---|---|---|---|---|---|
| 0 | 3（最慢） | 2 | 0xFFFFFE | 0x06 FIRST | 700 字节消息的前 228 字节 |
| 100 ns | 0（最快） | 2 | 0xFFFFFF | 0x07 MIDDLE | 中间 244 字节 |
| 200 ns | 1 | 2 | 0x000000 | 0x08 LAST | 最后 228 字节 |
| 300 ns | 2 | 1 | 0x000100 | 0x09 ONLY | 另一条消息，64 字节 |

FIRST 被刻意安排在**最慢**的路径上，并且最早发出。后三段在更快的路径上、更晚发出，会全部超过它。重组后必须得到 FIRST、MIDDLE、LAST 的顺序，拼出的 700 字节要与预期载荷逐字节一致。

### 8.3　运行

```bash
make lab5
```

```
Lab 5 PASS: frames=4 messages=2
```

### 8.4　结果

`results/lab5.json` 中的关键字段：

| 字段 | 值 |
|---|---|
| `tx_frames` | 4 |
| `rx_frames` | 4 |
| `reassembled_messages` | 2 |
| `icrc_modeled` | false |

`arrival` 数组按到达先后排列：

| 顺序 | PSN | 操作码 | 说明 |
|---|---|---|---|
| 1 | 0xFFFFFF（16777215） | 0x07 | MIDDLE |
| 2 | 0x000000（0） | 0x08 | LAST |
| 3 | 0x000100（256） | 0x09 | ONLY，另一条消息 |
| 4 | 0xFFFFFE（16777214） | 0x06 | FIRST，最后到达 |

到达顺序是 MIDDLE、LAST、ONLY、FIRST，与 PSN 顺序完全相反。

### 8.5　分析

#### 8.5.1　帧格式

`common/roce-frame.h` 中的 `ParseRoceFrame()` 按以下偏移解析：

| 偏移 | 长度 | 字段 |
|---|---|---|
| 0 | 6 | 目的 MAC `02:00:00:00:00:02` |
| 6 | 6 | 源 MAC `02:00:00:00:00:01` |
| 12 | 2 | EtherType `0x0800` |
| 14 | 20 | IPv4 头（版本/IHL 为 `0x45`，协议号为 17） |
| 34 | 8 | UDP 头（目的端口位于偏移 36，值为 4791） |
| 42 | 12 | BTH：操作码位于偏移 42，QPN 位于偏移 46（24 位），PSN 位于偏移 50（24 位） |
| 54 | 16 | RETH，仅 FIRST 与 ONLY 具有 |
| 70 或 54 | — | 载荷起点 |

RETH 包含三个字段：rkey 位于偏移 54，远程地址位于偏移 58（8 字节），消息长度位于偏移 66。

**RETH 的字段顺序与标准 RoCE 相反。** 标准为远程地址在前、rkey 在后，此处实现相反。`common/roce-frame.h:139` 的注释说明了原因：顺序跟随当前 OpenRDMA M3 的实现。解析由其他 RoCE 实现生成的帧时，不应假定符合标准布局。

由于 MIDDLE 与 LAST 不带 RETH，四个帧的长度并不整齐：

| 帧 | 操作码 | 载荷 | 总长 |
|---|---|---|---|
| FIRST | 0x06 | 228 | 14+20+8+12+16+228 = 298 |
| MIDDLE | 0x07 | 244 | 14+20+8+12+244 = 298 |
| LAST | 0x08 | 228 | 14+20+8+12+228 = 282 |
| ONLY | 0x09 | 64 | 14+20+8+12+16+64 = 134 |

因此解析器要根据操作码决定载荷偏移（`common/roce-frame.h:133`）：

```cpp
const bool firstLayout = packet.opcode == 0x06 || packet.opcode == 0x09;
packet.payloadOffset = firstLayout ? 70 : 54;
```

RoCEv2 使用 UDP 4791 端口是一个关键设计，它使 RDMA 流量能像普通 UDP 一样被路由，这是 RoCEv2 相比 RoCEv1（二层）最重要的变化。

#### 8.5.2　PSN 回绕

PSN 是 24 位字段，掩码为 `0xFFFFFF`。四个帧中属于同一消息的三个 PSN 为：

```
0xFFFFFE → 0xFFFFFF → 0x000000
```

跨过了回绕点。因此不能直接比较数值大小——`0x000000` 在数值上小于 `0xFFFFFE`，但它是**后**发的。

正确做法是比较模距离：

```cpp
(psn_b - psn_a) & 0xFFFFFF
```

从 0xFFFFFE 到 0xFFFFFF 的距离为 1，从 0xFFFFFE 到 0x000000 的距离为 2。距离单调递增，排序即可正确。TCP 序列号使用同样的方法。

#### 8.5.3　未建模的部分

`"icrc_modeled": false` 是明确写出的字段。

真实的 RoCEv2 帧结尾有 4 字节 ICRC（不变式 CRC），以太网层还有 4 字节 FCS，两者都没有计算。原因是当前 M3 的 RTL 输出不产生它们，仿真器也不校验。

这是声明而非疏漏：从事安全或错误检测相关工作时，需要知道模型缺少什么。若要补上，帧长度需要相应调整，`ParseRoceFrame()` 也要修改。

### 8.6　练习

以下三个练习都修改 `Lab5-RoceFrameReplay/generate_trace.py`，修改后重新生成测试数据：

```bash
cd Lab5-RoceFrameReplay && python3 generate_trace.py && cd ../ns-3 && ./ns3 run --no-build lab5
```

**练习 5.1　取消 PSN 回绕**

把第 67、69、71 行的 `psn` 改为 0x000010、0x000011、0x000012，重新生成后运行，结果仍为 `Lab 5 PASS: frames=4 messages=2`。模距离排序对连续值和不回绕的值给出相同的顺序，回绕本身没有被特殊处理——处理回绕靠的是排序规则，而不是补丁。

那么，真实硬件为什么使用会回绕的 24 位计数器，而不是一个足够大的整数？

**练习 5.2　截短 FIRST 的载荷**

把第 67 行的 `long_message[:228]` 改为 `long_message[:100]`，`message_length=700` 保持不变。三段载荷变为 100 + 244 + 228 = 572，与 RETH 中声明的 700 不一致。

> **Hint:** 哪一步先发现不一致？是重组阶段，还是更早的帧 oracle 逐字节比对阶段？

**练习 5.3　修改 FIRST 的操作码**

把 FIRST 的操作码由 0x06 改为 0x09（ONLY），表示这一帧自身就是一条完整消息。但后面仍跟着两段续帧，且当前载荷长度也不等于 RETH 中声明的消息长度。观察断言在哪一条上触发。

---

## 9　Lab 6　TX RTL 接入 ns-3

### 9.1　目的

把 Verilator 仿真的真实输出——M0 至 M3 流水线产生的 RoCEv2 帧——接入 ns-3 网络，验证这些帧经过四路径网络后仍能被正确识别与重组。

与 Lab 5 的区别在帧的来源。Lab 5 的帧由 Python 脚本按格式拼装；Lab 6 的帧由 RTL 在仿真中真实产生，分片规则、PSN 递增与每一个头部字段都由硬件逻辑决定。

### 9.2　拓扑与参数

本实验与下一章共用一条三阶段的离线流水线。三个阶段各自跑在自己的仿真器进程里，中间只用 CSV 与 JSON 文件交接，不共享时钟、不共享进程：

```
  +----------------+   tx-trace.csv     +----------------+   rx-trace.csv   +----------------+
  | TX RTL         |   tx-frames.csv    | ns-3           |   ns3-offline    | RX RTL         |
  | bsc+Verilator  |-->tx-messages.csv->| 四路径网络     |--> .json -------->| bsc+Verilator  |
  | mkTestM0Chain  |                    | 与校验         |                  | mkTestRxTop    |
  +----------------+                    +----------------+                  +----------------+
       make tx                             make ns3                            make rx
```

本章覆盖前两个阶段，下一章覆盖第三个阶段。“离线”指两个仿真器不同时运行：TX 一次性跑完并留下记录，ns-3 再读这份记录跑一遍。

**TX 侧**是 M0 到 M3 四个模块串联，`tx/integration/tb/TestM0Chain.bsv` 把它们接在一起：

| 模块 | 源码 | 职责 |
|---|---|---|
| M0 | `tx/m0/src/TxMvp.bsv` | 解析提交描述符，管理 QP 上下文与内存区域表 |
| M1 | `tx/m1/src/DmaEngine.bsv` | 从显存读载荷，每拍送出 128 字节 |
| M2 | `tx/m2/src/PacketAssembly.bsv` | 按 PMTU 分片，逐包生成描述符与 PSN |
| M3 | `tx/m3/src/MrcTxScheduler.bsv` | 四通道调度，封装 Ethernet / IPv4 / UDP / BTH / RETH |

**阶段 1 的工作负载**固定写死，不含随机数，每次运行完全相同：4 个 QP、6 笔消息、11 个帧。

| 消息 | QPN | 帧数 | 起始 PSN | 末 PSN | 长度 | 操作码 |
|---|---|---|---|---|---|---|
| 0 | 0 | 3 | 16777214 | 0 | 700 | FIRST / MIDDLE / LAST |
| 1 | 1 | 3 | 1192960 | 1192962 | 985 | FIRST / MIDDLE / LAST |
| 2 | 2 | 1 | 32 | 32 | 128 | ONLY |
| 3 | 0 | 1 | 1 | 1 | 64 | ONLY |
| 4 | 3 | 2 | 8388608 | 8388609 | 5000 | FIRST / LAST |
| 5 | 1 | 1 | 1192963 | 1192963 | 484 | ONLY |

这份负载刻意挑了边界情况：消息 0 的 PSN 在中间回绕（16777214 → 16777215 → 0），消息 4 有 5000 字节需要分片，消息 3 只有 64 字节、一个包就发完。QPN 0 上放了两笔消息，一笔从回绕点之前开始，一笔从 PSN 1 开始，把这个 QP 的 PSN 空间两头都用上了。

**ns-3 侧**的网络与 Lab 4 相同：100 Gbps，四条路径的传播时延为 200 ns / 400 ns / 800 ns / 2 µs，每条路径一个 100 包的 DropTail 队列。M3 的四个出口通道、四条网络路径与四个接收 lane 一一对应。

### 9.3　运行

```bash
cd OpenRDMA-Offline
make tx
make ns3
```

`make tx` 编译并运行 `mkTestM0Chain`，写出四份文件：

| 文件 | 列 | 性质 |
|---|---|---|
| `tx-trace.csv` | `frame_id, eof_time_ps, channel, frame_hex` | 记录 |
| `tx-frames.csv` | `frame_id, message_id, packet_index, packet_count, request_id, qpn, psn, opcode, payload_offset, payload_length, expected_frame_hex, channel` | 帧预言机 |
| `tx-messages.csv` | `message_id, request_id, qpn, start_psn, remote_addr, remote_key, message_length, expected_payload_hex` | 消息预言机 |
| `tx-connection.csv` | `src_mac, src_ip, dst_mac, dst_ip` | 连接身份 |

`eof_time_ps` 是帧的最后一个字被从 M3 通道缓冲里取走时的仿真时刻。取这个边界而不是帧的第一个字，是因为它对应存储转发：此刻这个帧已经完整地站在出口上。它是仿真时间戳，所以阶段 2 复现的是帧与帧之间的**相对间隔**，不是绝对时间。

除 `tx-trace.csv` 外的三份是**预言机（oracle）**，由 cocotb 侧的 Python 参考模型独立算出，**不是 ns-3 生成的**。这一点很重要：如果 oracle 由 ns-3 生成，验证就成了自我证明。

`tx-trace.csv` 里的 `frame_hex` 是 RTL 实际吐出的字节，`tx-frames.csv` 与 `tx-messages.csv` 是"它本该吐出什么"，两者在 `tx_export.py` 里当场比对——对不上，阶段 1 直接失败，不会留下自相矛盾的工件。`tx-connection.csv` 记录的是配置进 DUT 的 L2/L3 身份，后一阶段拿它当期望值去校验收到的帧。

`tx-frames.csv` 里也有一列 `channel`，阶段 2 会断言它等于 `tx-trace.csv` 里的同名列（`ns3/offline-txrx.cc:272`）。这个断言要读得准：该列在 `tx_export.py:342` 处直接取自 trace 行（`channel=row.channel`），并非由参考模型另行算出，因此它不会失败于"M3 调度错了"——它防的是**两份工件对不上**，比如一次失败运行留下的旧 `tx-frames.csv` 配上了新写的 `tx-trace.csv`。真正检验调度器的是阶段 1 自己那条 `channels_used == {0,1,2,3}`（`tx_export.py:303`）：四个出口通道必须都被用到，否则后面所有按 lane 统计的结论都是空的。

`make ns3` 编译 `ns3/offline-txrx.cc` 并运行，它读上面四份文件，依次做三件事：

1. **注入前校验**：每帧的字节与 `tx-frames.csv` 里的期望值一致，QPN、PSN、操作码、载荷长度彼此自洽；
2. 按 `eof_time_ps` 把帧注入四路径网络；
3. **送达后校验**：无丢包、字节未被改动、按消息重组后的载荷与 oracle 逐字节相等、每个 QP 的 PSN 与操作码序列正确、RETH 里的地址与 rkey 正确。

随后统计成对逆序数与最大乱序深度，写出 `results/rx-trace.csv` 与 `results/ns3-offline.json`。

### 9.4　结果

```
Offline ns-3 PASS: tx_frames=11 rx_frames=11 messages=6 complete=6 reorder_inversions=13 dropped=0
```

`complete=6` 指 6 笔消息全部重组完整：每个帧都在、顺序能还原、载荷长度与 oracle 对得上。它和 `messages=6` 不同，后者只数出了几笔消息。

`results/ns3-offline.json` 的关键字段：

| 字段 | 值 | 含义 |
|---|---|---|
| `timestamp_boundary` | `m3_eof_dequeue_ps` | 时间戳取自 M3 的 EOF dequeue 边界 |
| `tx_frames` / `accepted_frames` | 11 / 11 | 发出、并被设备接受入队的帧数 |
| `delivered_frames` / `dropped_frames` | 11 / 0 | 送达与丢弃 |
| `rx_frames` / `rx_events` | 11 / 11 | 到达的帧数与事件数 |
| `messages` / `messages_complete` | 6 / 6 | 识别出的消息数与重组完整的消息数 |
| `lossless` | true | 本次运行按无损前提判定（丢一帧即报错） |
| `reorder_inversions` | 13 | 成对逆序数 |
| `reorder_max_depth` | 8 | 最大乱序深度 |
| `icrc_modeled` / `fcs_modeled` | false / false | ICRC 与 Ethernet FCS 不建模 |

`accepted_frames` 与 `delivered_frames` 分开记是有意的：`Send()` 返回 true 只表示帧进了设备的发送队列，`delivered_frames` 才是真正走完链路到达对端的。两者相等，说明队列一个都没丢。

11 个帧全部送达、0 丢包，但到达顺序与发送顺序不同：13 对逆序，最大乱序深度 8。阶段 3 因此收到的是一份真正乱序的输入，而非有序输入的退化情形。

`lossless` 默认为真，含义是"丢一个帧就报错"。加 `--lossless=0` 可以放宽，此时允许多个帧被丢弃、只检查剩下的，通过行也会变成 `Offline ns-3 PASS (lossy mode)`。需要留意的是，这条放宽路径在本实验里同样走不到——9.5.6 节会说明这份负载根本撑不满队列，所以在 `dropped=0` 的前提下两种模式的结论完全一样。另外，阶段 3 无论如何都要求一份无损的 `rx-trace.csv`：RX 侧没有重传，缺帧就是缺帧。

### 9.5　分析

#### 9.5.1　二层注入

本实验的数据通路与前五个实验完全不同。

Lab 1 至 Lab 5 中，数据走正常路径：应用调用 `SendTo()`，UDP 加头，IP 加头，然后交给设备。本实验不走这条路，帧在二层直接注入（`ns3/common/four-path-roce.h`）：

```cpp
auto packet = ns3::Create<ns3::Packet>(transmission.frame.data(), transmission.frame.size());
packet->AddPacketTag(FrameIdTag(transmission.frameId));
const bool accepted = m_paths[transmission.channel].Get(0)->Send(
    packet, m_paths[transmission.channel].Get(1)->GetAddress(), kIpv4EtherType);
```

来自 RTL 的整块字节（连同它自己的 Ethernet 头、IP 头、UDP 头）被当作不透明载荷，直接交给 `NetDevice::Send()`。接收端用 `RegisterProtocolHandler` 按以太网类型 0x0800 接收。

帧的字节是外来的，ns-3 不认识它，因此依靠 `FrameIdTag` 这个 ns-3 `Tag` 携带身份：帧在网络中传递时字节可能被复制，但 Tag 跟随数据包，返回时可以对应上。

`PointToPointNetDevice::Send()` 不检查 MTU（源码中只有 `SetMtu`/`GetMtu` 两个函数，`Send` 中没有调用），因此那些超过 1500 字节的帧——例如 id 为 8 的 4138 字节帧——可以正常通过。换成以太网设备则会被丢弃。

#### 9.5.2　逐帧时延

四路径配置为 100 Gbps，传播时延 200 ns / 400 ns / 800 ns / 2 µs。线缆上的帧长是帧字节数加 2（PPP 头）。

帧 0 走通道 0，此时通道空闲：

```
注入时刻   338000 ps
序列化     (298 + 2) × 8 / 100e9 = 24000 ps
传播       200 ns = 200000 ps
到达       338000 + 24000 + 200000 = 562000 ps
```

`rx-trace.csv` 中帧 0 的 `arrival_time_ps` 是 562000，吻合。

下面把 11 个帧全部算一遍。`上线` 一列是帧真正开始占用链路的时间：帧进了设备的发送队列，不等于立刻上线。

| 帧 | 注入 ps | 通道 | 字节 | 序列化 ps | 上线 ps | 传播 ps | 到达 ps |
|---|---|---|---|---|---|---|---|
| 0 | 338000 | 0 | 298 | 24000 | 338000 | 200000 | 562000 |
| 1 | 350000 | 1 | 298 | 24000 | 350000 | 400000 | 774000 |
| 2 | 358000 | 2 | 282 | 22720 | 358000 | 800000 | 1180720 |
| 5 | 372000 | 1 | 55 | 4560 | **374000** | 400000 | 778560 |
| 6 | 398000 | 2 | 198 | 16000 | 398000 | 800000 | 1214000 |
| 3 | 416000 | 3 | 554 | 44480 | 416000 | 2000000 | 2460480 |
| 4 | 434000 | 0 | 554 | 44480 | 434000 | 200000 | 678480 |
| 7 | 438000 | 3 | 134 | 10880 | **460480** | 2000000 | 2471360 |
| 10 | 756000 | 2 | 554 | 44480 | 756000 | 800000 | 1600480 |
| 9 | 796000 | 1 | 986 | 79040 | 796000 | 400000 | 1275040 |
| 8 | 1206000 | 0 | 4138 | 331200 | 1206000 | 200000 | 1737200 |

表中只有帧 5 与帧 7 的上线时刻晚于注入时刻，其余各行的注入时刻即为上线时刻。

帧 5 在 372000 ps 注入通道 1，但通道 1 被帧 1 占着（帧 1 于 350000 ps 上线，占用 24000 ps，到 374000 ps 才空出来），所以它只能等到 374000 ps 才上线，比自己的注入时刻晚 2000 ps：

```
374000 + 4560 + 400000 = 778560
```

帧 7 更极端。它在 438000 ps 注入通道 3，通道 3 被帧 3 占着（帧 3 于 416000 ps 上线，占用 44480 ps，到 460480 ps 空闲），于是晚了 22480 ps 才上线：

```
460480 + 10880 + 2000000 = 2471360
```

**链路是串行资源**：同一条通道上的帧不会重叠，后一个必须等前一个发完，即使自己的注入时刻已经到了也上不了线。整章的时间行为由一条公式描述：

```
到达 = max(注入, 通道空闲) + 序列化 + 传播
通道空闲 = 同通道上一帧的上线时刻 + 该帧的序列化
```

序列化时延本身也很直观：一个字节 8 比特，100 Gbps 的链路每皮秒能送 0.1 比特，所以每字节 80 ps。帧 8 是 4138 字节，4138 + 2 = 4140 字节，4140 × 80 = 331200 ps，约 331 ns。

这解释了本实验的乱序从哪来。帧 4 比帧 7 早注入 4000 ps，但帧 4 走通道 0（200 ns 快路径），帧 7 走通道 3（2 µs 慢路径），到达时刻相差 1.79 ms，约为帧 4 自身传输时间的 440 倍。两者的注入间隔只有 4000 ps，到达间隔却由路径时延决定。同理，注入最晚的帧 8 在 1737200 ps 到达，比帧 3、帧 7 早约 730 µs。

#### 9.5.3　时间分辨率

`ns3/offline-txrx.cc` 在 `main()` 开头调用：

```cpp
Time::SetResolution(Time::PS);
```

ns-3 默认分辨率为纳秒，而本实验中 10880 ps（10.88 ns）这样的量级到处都是，纳秒分辨率会把小数部分抹掉，逐帧比对无法通过。

代价是仿真可表示的最大时间变短：ns-3 用 64 位整数存储时间，分辨率调细 1000 倍，可表示的时间范围也缩小 1000 倍。本实验只运行几毫秒，够用。

#### 9.5.4　停止时间的计算

仿真必须有明确的结束时刻。ns-3 的事件队列一空，`Simulator::Run()` 就返回；如果停止时刻取早了，最后几个还在链路上走的帧永远不会到达，而失败的样子和丢包一模一样。

`FourPathRoceNetwork::DrainGuardPs()` 就是用来算这个余量的（`ns3/common/four-path-roce.h`）：

```cpp
return maxDelayPs + *std::max_element(serialized.begin(), serialized.end()) + 1'000;
```

三项相加：最慢路径的传播时延、四条通道各自序列化总量中的最大值（逐帧累加）、再加 1000 ps 余量。

关键在于它读的是**配置值**，不是常量：

```cpp
const uint64_t linkBps = ns3::DataRate(m_config.dataRate).GetBitRate();
```

所以 `--dataRate` 改了之后，停止时刻会跟着变。传播时延没有对应的命令行参数，四条路径的时延写在 `LinkConfig` 的默认值里（练习 6.3 修改的就是这里）；`DrainGuardPs()` 同样从 `m_config` 读取这些值，源码改动后它会一并跟上。

把速率写死成默认的 100 Gbps 是个容易犯的错误。链路降到 10 Gbps 时实际序列化时延变为 10 倍，若仿真仍按原速估算停止时刻，会在帧到达之前结束，报出 `not every frame was delivered`，现象与丢包相同。

这个余量是**偏松**的上界，不是精确值：它把最坏情况的序列化总量与最坏情况的传播时延直接相加，而这两者不会同时发生在同一个帧上。取大一些只会让仿真多跑一会儿，不会算错结果。

#### 9.5.5　离线交接与在线联合仿真的取舍

两个仿真器也可以同时运行，通过套接字交换数据，用一对 ADVANCE / RESULT 消息构成虚拟时间屏障：RTL 侧声明"我的虚拟时间推进到 T，这段时间产生了这些帧"，ns-3 把自身仿真推进到 T 并回送这段时间内的到达事件，两端交替前进，每一步都对齐。

在线方式有一个离线方式给不了的性质：**绝对时间一致**。同一份负载，在线跑与离线跑得到的注入时刻与到达时刻逐个字段相等，因为两端共用一条时间轴。

本流水线没有采用在线方式，理由是三个阶段的**可独立重跑**更重要：

1. 改网络参数只需重跑 `make ns3`，不必再跑一遍 RTL 仿真；改 RTL 只需重跑 `make tx`。在线方式下两端必须一起启动，任何一侧改动都要整体重跑。
2. 每一阶段的输入是一份可以打开、可以直接 diff 的文件。套接字上的二进制流做不到这一点——出问题时只能看到两端对不上，看不到对不上的那一帧长什么样。
3. 三个阶段是三次独立的进程调用，彼此之间没有常驻状态。某一阶段崩溃只终止它自己，另外两个阶段已经写出的工件不受影响，可以单独重跑排查。

代价是丢掉了绝对时间：阶段 2 在另一个仿真器里从零重建相对时序。只要帧与帧之间的间隔准确，到达顺序、乱序深度与逐帧时延都不受影响——`reorder_inversions` 与 `reorder_max_depth` 这类统计量只依赖相对时序。

早期版本曾经用在线方式实现，代码保留在 `Ns3-lab/Lab6-OpenRdmaTrace/` 与 `Ns3-lab/Lab7-OnlineSocketCosim/`（见 2.7 节的目录说明），可以对照阅读。

#### 9.5.6　未覆盖的分支

`FourPathRoceNetwork` 中有一条丢包路径：设备的 `DropTailQueue` 满时 `Send()` 返回 false，代码记录一个 `DROPPED` 事件。

默认负载下 11 个帧全部送达，**这条路径一次都没有走到**。把队列容量从 100 个包一路压到 1 个包，结果仍然是 0 丢包：

```
--queueSize=100p  ->  dropped=0, reorder_inversions=13
--queueSize=2p    ->  dropped=0, reorder_inversions=13
--queueSize=1p    ->  dropped=0, reorder_inversions=13
```

原因在 9.5.2 节的表里：整场实验只有帧 5 与帧 7 需要等待，而它们各自等待时，通道上**只有一帧在发**。队列从来没有同时排列过两个帧，容量是 1 还是 100 没有区别。

因此这份负载不会使队列达到容量上限。要触发 `DROPPED` 分支，需要让同一通道在短时间内连续注入多个帧：缩短帧间隔，或把多条通道合并到同一条路径上。

### 9.6　练习

**练习 6.1　队列容量的下限**

`make ns3` 通过 `ARGS` 把额外参数转给 ns-3 程序：

```bash
make ns3 ARGS="--queueSize=1p"
```

容量从默认的 100 个包降到 2 个、再降到 1 个，`dropped_frames` 始终为 0，`reorder_inversions` 始终为 13。结合 9.5.2 节的表说明原因：在这份负载下，队列容量是否可能成为限制。

**练习 6.2　降低链路速率**

```bash
make ns3 ARGS="--dataRate=25Gbps"
make ns3 ARGS="--dataRate=10Gbps"
```

实测结果：

| 链路速率 | `reorder_inversions` |
|---|---|
| 100 Gbps（默认） | 13 |
| 25 Gbps | 11 |
| 10 Gbps | 9 |

链路越慢，乱序越少。用 9.5.2 节的公式重算 10 Gbps 那次的 11 个到达时刻，与 `results/rx-trace.csv` 对照，再手工统计逆序对数，应当得到 9。逆序对只能逐对计数：对每一个帧，数出注入在它之后、到达却先于它的帧有多少个，再全部相加。

最后说明速率下降为何会减少乱序。考察序列化时延变长之后，同一通道上相邻两帧的时间关系如何变化。

**练习 6.3　等时延的四条路径**

命令行只暴露了速率与队列（`offline-txrx.cc:625-627` 只覆盖 `dataRate` 与 `queueSize` 两个字段），传播时延要改源码：把 `ns3/common/four-path-roce.h` 中 `LinkConfig::delays` 的四个初值都改成 `"200ns"`，重新 `make ns3`。然后用 9.5.2 节的公式手算 11 个帧的到达时刻，与 `rx-trace.csv` 对照。

四条路径速度相同时，等待只由通道占用决定，而通道占用只由注入顺序决定，因此到达顺序会接近注入顺序，`reorder_inversions` 应当明显下降。可以先用 9.5.2 节的公式估一个值，再与实测对照。

---

## 10　Lab 7　ns-3 的输出回灌 RX RTL

### 10.1　目的

把上一章网络输出的帧，按到达时刻喂给真实的 RX RTL，验证接收侧能在乱序输入下把数据正确落进显存。

上一章验证的是"帧经网络后没坏、能重组"，这一章验证的是"对端硬件真的能收"。两者的区别在失败方式：网络侧的错误是字节层面的，接收侧的错误是**位置**层面的——数据可能完整无误地写到了错误的地址上。

### 10.2　拓扑与参数

承接上一章的第三阶段：

```
  results/rx-trace.csv -------+
  results/tx-frames.csv ------+
  results/tx-messages.csv ----+-->  RX RTL  (bsc + Verilator)
  results/tx-connection.csv --+     top-level: mkTestRxTop
                                    m0 -> m1 -> m2 -> m3 -> m4
                                              |
                                              v
                                    results/rx-replay.json
```

四份输入各管一件事：`rx-trace.csv` 决定帧什么时候到、从哪条 lane 进、字节是什么；`tx-frames.csv` 与 `tx-messages.csv` 提供 QPN、PSN、地址、长度、载荷这些期望值；`tx-connection.csv` 提供 MAC 与 IP 身份。

RX 侧是五个模块串联，顶层为 `rx/src/RxTop.bsv`（`mkRxTop`），本实验用的测试顶层是 `rx/tb/TestRxTop.bsv`（`mkTestRxTop`）：

| 模块 | 源码 | 职责 |
|---|---|---|
| m0 | `rx/m0_packet_buffer/RxPacketBuffer.bsv` | 收包缓冲，按 lane 存放待解析的帧 |
| m1 | `rx/m1_packet_parser/RxPacketParser.bsv` | 解析以太网 / IP / UDP / BTH / RETH |
| m2 | `rx/m2_rq_bitmap/RxRqBitmapManager.bsv` | 每 QP 一张接收位图，记录哪些 PSN 已到 |
| m3 | `rx/m3_dma_placement/RxDmaPlacement.bsv` | 算出目标地址，把载荷写进显存 |
| m4 | `rx/m4_ordered_queue/RxOrderedQueueManager.bsv` | 按 PSN 顺序退休，产生 CQE |

四个接收 lane 与上一章的四个发送通道、四条网络路径一一对应。`rx-trace.csv` 里的 `channel` 就是 lane 号。

回灌遵循一条规则：**帧与帧之间的相对间隔照搬，绝对时间重新起算**。`arrival_time_ps` 中最小的那个作为原点，其余帧按相对差值排队送进各自的 lane。这样 RX 看到的时序与网络上一致，但不必让 RTL 仿真空跑到 1.2 ms 才开始收第一个包。

### 10.3　运行

```bash
cd OpenRDMA-Offline
make rx
```

它编译 `mkTestRxTop`，跑 `ns3/cocotb/rx_replay.py`。这个阶段需要的输入全部来自 `results/`，没有网络参与。

三个阶段也可以一次跑完：

```bash
make            # 等价于 make tx && make ns3 && make rx
```

### 10.4　结果

```
RX replay: 11 frames, 11 DMA writes, 6 CQEs, retired=11, reordered=True
```

`results/rx-replay.json`：

| 字段 | 值 | 含义 |
|---|---|---|
| `status` | `PASS` | 机器可读的判定，与上面那行文字一致 |
| `lanes_used` | `[0,1,2,3]` | 四条 lane 都收到了帧 |
| `qpns` | `[0,1,2,3]` | 配置了四个 QP |
| `frames_replayed` | 11 | 灌进去的帧数 |
| `dma_writes` | 11 | 实际发生的显存写次数 |
| `cqes` | 6 | 产生的完成事件数 |
| `feedback_events` | 11 | DMA 侧的反馈节拍 |
| `credits` | 0 | 有序 flag 信用，见 10.5.6 |
| `retired` | 11 | 有序队列退休的条目数 |
| `dma_ack_psn0` | 1 | 0 号 QP 的 DMA 水位 |
| `reordered` | true | 到达顺序确实被打乱过 |
| `faulted` | 0 | 无 fault |

`dma_writes` 等于 `frames_replayed` 是意料之中的：每个帧携带一个包，落一次显存。`cqes` 是 6 而不是 11，因为完成事件按**消息**产生——6 笔消息，6 个 CQE。

### 10.5　分析

#### 10.5.1　连接上下文从工件读出

RX 要工作，得先知道本地 MAC、本地 IP、对端 MAC、对端 IP、每个 QP 的初始 PSN 与 PMTU、RKEY、虚拟基址、显存基址。这些参数在真实系统里由带外协商决定，在仿真里则来自工件：

| 参数 | 来源 |
|---|---|
| MAC / IP 身份 | `tx-connection.csv` |
| QPN、初始 PSN、PMTU | `tx-frames.csv`、`tx-messages.csv` |
| RKEY、远端地址、消息长度 | `tx-messages.csv` |
| 显存基址、区域大小 | `rx_replay.py` 自己的约定 |

关键在**方向**：连接身份是从 TX 声明的那份文件里读出来的，RX 配好之后，必须拿它去匹每一个收到的帧，而不是反过来从收到的帧里嗅探出地址再配给自己。

```python
connection = read_connection(artifact("tx-connection.csv"))
...
actual = frame_connection(row.frame)
if actual != connection:
    raise AssertionError(...)
```

反过来做——先看收到什么帧、再把 RX 配成接收那种帧——这个检查在任何输入下都成立，不具备判别能力。由被测对象定义期望值属于同义反复，是验证代码中反复出现的错误。

初始 PSN 的推导规则是：每个 QP 的初始 PSN 取"到该 QP 所有消息起点前向距离的最大值最小"的那个 PSN，其中前向距离按 24 位回绕计算：

```python
def psn_distance(start, current):
    return (current - start) & PSN_MASK     # PSN_MASK = 0xFFFFFF
```

拿 0 号 QP 举例。它有两笔消息，起点分别是 16777214（`0xFFFFFE`）和 1。两个候选：

- 取 16777214：到自己的距离是 0，到 1 的距离是 `(1 - 16777214) & 0xFFFFFF = 3`，最大值 3；
- 取 1：到自己的距离是 0，到 16777214 的距离是 16777213，最大值 16777213。

前者小得多，所以初始 PSN 定为 16777214。这正是 TX 侧配置的值。

#### 10.5.2　每个 (QPN, PSN) 恰好写一次

接收侧最容易出的错不是"写错字节"，而是"写到错的地方"。所以核对分三层，一层比一层细：

1. TX 发出的每个 `(QPN, PSN)`，RX 必须都产生过一次显存写；
2. 写的地址与字节数必须与 oracle 算出的相同；
3. 写进去的载荷必须与 TX 的载荷逐字节相等。

反过来也要查——多出来的写同样算错：

```python
extra = set(seen.dma) - expected_keys
if extra:
    raise AssertionError(f"DMA writes for packets the TX never sent: {extra}")
```

这份工作负载**有两处目标地址故意重叠**。若 RX 把某个包的偏移算错一页，写入位置会落在另一个包已经写过的区间上，地址检查立即报出。三层检查同时收紧，"恰好一次"才是一个可证伪的命题。

#### 10.5.3　CQE、水位与 PSN 回绕

完成事件按消息产生，一个消息一个 CQE，内容为 `(qpn, lastPsn, bytes)`。整个多重集必须与 oracle 精确相等——不只是数量相等：

```python
expected_cqes = sorted(item for context in contexts.values()
                       for item in context.expected_cqes())
actual_cqes = sorted((int(cqe.qpn), int(cqe.lastPsn), int(cqe.bytes))
                     for cqe in seen.cqes)
if actual_cqes != expected_cqes:
    raise AssertionError(...)
```

水位检查涉及 PSN 回绕。`dmaAckPsn0` 与 `progress0.ackPsn` 是 0 号 QP 上的两个进度指示，它们都必须停在 `last_psn()`——该 QP 上"从初始 PSN 出发前向距离最大"的那个 PSN：

```
last_psn() = argmax over psn of psn_distance(initial_psn, psn)
```

0 号 QP 的 PSN 流是 16777214 → 16777215 → 0（消息 0 的三个包），然后 1（消息 3 的单包）。以初始 PSN 16777214 起算，各点的前向距离是 0、1、2、3，最大的是 1。

所以检查通过时，读数是这样：

```
dmaAckPsn0 = 1     （初始 PSN 是 16777214）
```

**水位比初始值小**。如果按普通整数比较，这看起来像进度倒退了；只有按 24 位回绕来算，1 才是"最新的那个"。回绕不是边角情况，它就在这份工作负载的主路径上。

#### 10.5.4　乱序到达，有序退休

网络把到达顺序打乱成了 0, 4, 1, 5, 2, 6, 9, 10, 8, 3, 7（帧号）。RX 要做两件互相牵制的事：

- **落显存可以乱序**。每个包带着 QPN、PSN、地址、长度，独立可写，先到先写。这正是 RDMA 的写入语义。
- **退休必须有序**。m4 的有序队列按 PSN 顺序放行，`retired=11` 表示 11 个条目全部按序退完，`oooCount=0` 表示队列里没有残留的乱序包。

这两件事能同时成立，靠的是 m2 那张位图：包先到但前一个还没来时，它标记为"已到"并挂起，等缺口补上再一起放行。

`reordered` 字段来自一处独立判断——把每个帧的 `tx_order` 按到达顺序排成数组，看它是否仍然有序：

```python
def _arrival_reordered(arrivals):
    order = [row.tx_order for row in arrivals]
    return order != sorted(order)
```

本实验的 `tx_order` 序列是 0, 6, 1, 3, 2, 4, 9, 8, 10, 5, 7，不是递增序列，因此 `reordered=true`。这个字段要求为真的理由与 A.1 节所述一致：若网络没有打乱顺序，本章关于乱序处理的全部验证都不成立。

#### 10.5.5　TX 基线与当前 TX 的分叉

RX 树里有一份 `rx/upstream/`，是 RX 冻结的那一版 TX 源码快照。`rx/Makefile` 的 `preflight` 目标做两件事，而且两件事都发生在本树内部——不需要任何外部仓库：

1. `cd upstream && sha256sum -c BASELINE.sha256`，确认快照本身没被改动。这一条是**硬失败**，不一致就 `exit 1`。
2. 把快照里的四个文件与本树的 `tx/` 逐一比对，不一致时打印 `WARNING`。这一条**只提示，不拦截**。

四个文件按模块一一对应：

| 快照 | 对应的本树文件 | 快照行数 | 本树行数 |
|---|---|---|---|
| `CommonTypes.bsv` | `tx/m0/src/CommonTypes.bsv` | 87 | 87（一致） |
| `TxTypes.bsv` | `tx/m3/src/TxTypes.bsv` | 238 | 291 |
| `PacketAssembly.bsv` | `tx/m2/src/PacketAssembly.bsv` | 386 | 360 |
| `MrcTxScheduler.bsv` | `tx/m3/src/MrcTxScheduler.bsv` | 642 | 919 |

比对用 `tx/integration/cocotb/bsv_identical.py`。它不是语义比较，只是**在去掉注释与空行之后逐行比对文本**：`//` 行注释与 `/* */` 块注释被剥离，空行与行尾空白被忽略，字符串字面量里的 `//` 不会被误当成注释。因此"一边加了注释、另一边没有"不会误报，但代码本身的任何改动——包括行内的空格调整——都会报出来。这个脚本存在的理由是 BSC 按 `-p` 的顺序解析包，同一份类型定义如果有两份拷贝而内容不同，集成构建会静默地选中其中一份。

当前 TX 已明显领先于基线，而离线端到端仍然全部通过：11 个帧经四路径乱序网络后被 RX 全部正确解析、落显存、退休。这说明分叉位于 RX 不依赖的 TX 内部实现上，**wire 上可见的契约——BTH / RETH 字段布局、操作码语义、PSN 行为、载荷分片规则——仍然一致**。

反过来说：这份哈希描述的是旧契约。如果将来 TX 改了 wire 上的任何东西，这个提示是第一道警报，但它拦不住——真正拦住的是 10.5.2 节那些逐字节检查。

#### 10.5.6　未覆盖的部分

- **Ordered Flag barrier 路径没有被走到**。`orderedFlagIn` 不驱动，所以 `rx-replay.json` 里 `credits` 为 0 是正常的——credit 只在 DMA 侧消费 flag 请求时才产生。测试另外断言了 RX 不会自发产生 flag 请求。这条路径由 RX 自己的 `m4_ordered_queue` 单测与 `rx_top` / `tx_rx_loopback` 集成套件覆盖（见 `rx/VALIDATION.md`，RX 侧套件合计 37 条全通过）。
- **ICRC 与 FCS 不建模**，与上一章相同。
- **`TxCommit` 不是网络 ACK**。它表示 RTL 内部完成了发送提交，不代表对端收到了任何东西。
- **没有重传**。网络设为无损，丢包与重传路径都没有被这套负载走到。

### 10.6　练习

**练习 7.1　破坏工件后的首次断言**

三项改动，各自只改一个地方，跑 `make rx` 观察报错：

1. 在 `results/rx-trace.csv` 里删掉一整行；
2. 把某一行的 `frame_hex` 改动一个字节；
3. 把 `results/tx-connection.csv` 里的 `src_ip` 改掉。

三次报错分别来自哪一行代码，各自在防什么？三次都没有运行到 RTL——这些检查在仿真启动之前就已完成。判断这个顺序是有意设计还是偶然结果。

**练习 7.2　交换两个帧的到达顺序**

把 `results/rx-trace.csv` 里同一 lane 上相邻两帧的 `arrival_time_ps` 对调，重跑。结果仍然是 PASS——乱序本来就是 RX 要处理的正常情况。

对调 1 号 lane 上帧 1（774000 ps）与帧 5（778560 ps）的到达时刻后重跑，`rx-replay.json` 逐字段与改动前完全相同，`status` 仍为 `PASS`。

理由可以从回灌规则推出来。对调之后：

- 11 个帧一个不少，`frame_hex` 一个字节没改，`channel` 也没动——所以每条 lane 收到哪些帧完全不变，变的只是 1 号 lane 上的驱动先后；
- 到达时刻的**多重集**没变，最小值仍是帧 0 的 562000 ps，所以原点与总跨度不变，其余 lane 的相对时序逐拍不变；
- `dma_writes`、`cqes`、`retired`、`dma_ack_psn0` 这些量只取决于收到了哪些 `(QPN, PSN)` 与它们的载荷，与到达先后无关。

只有一样会变：1 号 lane 上帧 5 先于帧 1 进入 RX。而基线本身就已经是乱序回放（到达顺序 0, 4, 1, 5, 2, 6, 9, 10, 8, 3, 7），再多一处乱序不构成新的情况。

这与练习 7.1 的第 1 项（删掉一帧）形成对照：**乱序可以，缺失不行**。前者只需 RX 多等一会儿，后者会让某个 `(QPN, PSN)` 永远到不了，位图上留下一个补不上的洞。

**练习 7.3　追踪 0 号 QP 的 PSN 流**

从 `results/tx-frames.csv` 里筛出 `qpn` 为 0 的所有行，按 `psn` 排序，对照 10.5.3 节手算一遍各点的前向距离。再解释为什么 `dma_ack_psn0` 是 1 而不是 16777215。

---

## 11　常见问题

#### CMake 版本不足

报错信息包含 `CMake 3.25+ is required`。Ubuntu 22.04 的 apt 中没有 3.25 以上版本，用 pip 安装：

```bash
python3 -m pip install --user cmake==3.31.10
```

若系统 Python 没有 pip，先执行 `sudo apt install python3-pip`。安装后 Makefile 会在 `~/.local/bin/cmake` 查找。

#### make build 长时间没有输出

多数情况下是在编译 ns-3，并非卡死。首次编译有 515 个单元，8 核约六分钟。需要查看进度时去掉 `--quiet`：

```bash
cd ns-3 && ./ns3 build
```

#### 队列参数调整后没有反应

被自动安装的 queue disc 接管了。参见 5.5.3 节，需要先执行 `trafficControl.Uninstall(device)`。

#### Lab 3 修改 seed 或 run 后结果不变

`CreateObject<RateErrorModel>()` 写在 `SetSeed`/`SetRun` 之前，随机流已经被分配出去。把创建随机对象的代码移到后面即可，参见 6.5.1 节。

#### 报错 configured BSC is not executable

`bsc` 不在 PATH 中。`OpenRDMA-Offline/Makefile` 顶部的 `BSC` 变量可以覆盖：

```bash
make rx BSC=/path/to/bsc/bin/bsc
```

`bsc` 旁边还必须有 `lib/Verilog`（BSC 自带的 Verilog 原语库），构建脚本从 `bsc` 的位置推出它。

#### 报错 cocotb Python not found

`OpenRDMA-Offline/Makefile` 第 16 行的 `COCOTB_PYTHON` 默认值写死在一台具体机器上，显式传入即可：

```bash
make rx COCOTB_PYTHON=/path/to/your/python3
```

这个解释器必须装了 cocotb 2。它和用来跑 `offline_runner.py` 的是同一个——三阶段都由它启动。

#### 报错 is not a configured ns-3 build

`NS3_DIR` 指向的目录里没有 `build/include` 与 `build/lib`，说明那棵 ns-3 还没配置编译过。默认值是 `Ns3-lab/ns-3`：

```bash
make ns3 NS3_DIR=/path/to/your/ns-3
```

先在目标树里执行 `./ns3 configure && ./ns3 build`，参见 2.2 节。

#### Verilator 版本过低

cocotb 2 的 `--timing` 模式需要 Verilator 5.036 或更新，Ubuntu 22.04 apt 中的 4.038 不满足。可从源码编译，或：

```bash
conda install -c conda-forge verilator
```

#### PCAP 文件无法解析

分两种情况。

Lab 1 至 Lab 4 的包由 ns-3 自身的协议栈生成，链路前面只多一个 2 字节 PPP 头，tcpdump 解析正常。

Lab 5 与 Lab 6 的帧在二层直接注入，**帧内部已经包含一个完整的以太网头**，外层又被点对点链路套了一层 PPP。tcpdump 会打印 `IP0 (invalid)`——它在 PPP 之后找不到合法的 IP 头，因为先遇到的是 MAC 地址。这不是抓包文件损坏，可用 `tcpdump -x` 查看十六进制，或在 Wireshark 中指定按以太网解析。

Lab 6 的抓包文件是 `OpenRDMA-Offline/results/ns3-offline-ch0-0-0.pcap` 等四个，文件名里的 `ch0` 至 `ch3` 对应四条路径。

此外，PCAP 时间戳精度只到微秒。Lab 4 的包间隔为 100 ns，在抓包文件中会挤在同一个时间戳上。需要纳秒级时刻时，请读取 `results/*.json` 或 `results/*.csv`。

---

