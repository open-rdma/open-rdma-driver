# ns3/：在线四路径网络中枢与两阶段驱动

## 文件

| 文件 | 说明 |
|---|---|
| `online-cosim.cc` | ns-3 中枢：开两条 Unix socket，收 TX 的预言机与 ADVANCE，注入四路径网络，把每次到达即时推给 RX，跑完全部校验后写报告 |
| `common/roce-frame.h` | RoCE 帧构造/解析（Ethernet + IPv4 + UDP + BTH/RETH），与离线树同一份 |
| `common/lab-common.h` | lab 公共小工具，同上 |
| `common/four-path-roce.h` | `FourPathRoceNetwork`，同上（含 `LinkConfig` 与跟随配置的 `DrainGuardPs`） |
| `common/socket-protocol.h` | 帧格式、`Writer`/`Reader` 与 `Type` 枚举；本树在其上加了 DECLARE / DECLARE_ACK / EVENT / EOS 四个类型 |
| `cocotb/online_lib.py` | 两条链路共用的记录类型（`TraceRow`、`ArrivalRow`、`FrameOracleRow`、`MessageOracleRow`、`Connection`）、路径与结果目录 |
| `cocotb/online_protocol.py` | 客户端：报文编解码与阻塞收发（`OnlineClient`） |
| `cocotb/tx_stream.py` | TX 阶段测试模块：跑 workload，把帧按窗口送进中枢，核对返回的终态事件 |
| `cocotb/rx_stream.py` | RX 阶段测试模块：门控时钟下按到达时刻注入四条 lane，核对落显存结果 |
| `cocotb/online_runner.py` | 启动器；`make` 的 `all`/`hub`/`tx`/`rx` 都进这里 |
| `build/` | 中枢的编译输出（`online-cosim`），可删 |

编译不需要 cmake：`online_runner.py` 从 `NS3_DIR/build/lib` 里发现
`libns3.<版本>-<profile>` 前缀，把该前缀下的全部共享库（排除 `-test-`）连同
`build/include` 的头文件路径一起链接进来，所以换一个 ns-3 版本不用改代码。
`NS3_DIR` 默认 `../OpenRDMA-Offline/Ns3-lab/ns-3`。

## 三个进程

```
   tx/integration                     ns3/                        rx/
   mkTestM0Chain                 online-cosim                 mkTestRxTop
   ┌────────────┐   tx.sock   ┌──────────────────┐  rx.sock  ┌────────────┐
   │ TX RTL     │────────────►│ 四路径网络        │──────────►│ RX RTL     │
   │ + cocotb   │◄────────────│ 校验 + 转发       │◄──────────│ + cocotb   │
   └────────────┘             └──────────────────┘           └────────────┘
        ADVANCE / RESULT            EVENT / EOS                FINISH
```

TX 与 RX 是两个独立的进程，各自编译自己的 Verilator 模型，互不等待：
TX 一边跑一边把完成的帧交给中枢，中枢一边转发一边把到达推给 RX。
三者之间只走 socket，没有 CSV，也没有共享时钟。

## wire 协议

每条报文是 4 字节大端长度前缀加 12 字节头：magic `0x4E533743`（"NS7C"）、
版本 1、类型、requestId。长度上限 16 MiB，单帧上限 65535 字节，单次 ADVANCE
最多 4096 帧。requestId 从 HELLO 的 0 开始逐条加一，中枢逐条核对；跳号即失败。

| 类型 | 值 | 方向 | 载荷 |
|---|---|---|---|
| HELLO | 1 | 两阶段 → 中枢 | flags（必须为 0） |
| HELLO_ACK | 2 | 中枢 → 两阶段 | flags、通道数（4）、时间单位（1=ps）、链路速率、最大帧长、最大批帧数、四条路径的时延 |
| DECLARE | 8 | TX → 中枢 → RX | 连接 + 帧表 + 消息表（见下） |
| DECLARE_ACK | 9 | 收到 DECLARE 的一侧 → 中枢 | 空 |
| ADVANCE | 3 | TX → 中枢 | 目标时刻 + 本窗口的帧 |
| RESULT | 4 | 中枢 → TX | 水位 + 本窗口的终态事件 |
| EVENT | 10 | 中枢 → RX | 一次到达 |
| EOS | 11 | 中枢 → RX | 事件流结束，附中枢最终计数 |
| FINISH | 5 | 两阶段 → 中枢 | TX 空；RX 带自己实际灌入的帧数 |
| FINISH_ACK | 6 | 中枢 → 两阶段 | 提交/送达/丢弃帧数、最后序号、最终时刻、逆序数、最大深度、完整 message 数、用到过的通道位图、RX 灌入帧数 |
| ERROR | 7 | 中枢 → 两阶段 | 错误码与文本；任一校验失败时发出，随后进程非零退出 |

DECLARE 的载荷：

```
连接        src_mac u64, src_ip u32, dst_mac u64, dst_ip u32
帧表        u32 帧数, u32 消息数
  每帧      frame_id u64, message_id u32, packet_index u16, packet_count u16,
            request_id u32, qpn u32, psn u32, opcode u8, 3 字节保留,
            payload_offset u32, payload_length u32, u32 帧长 + 帧字节
  每条消息  message_id u32, request_id u32, qpn u32, start_psn u32,
            remote_addr u64, remote_key u32, message_length u32,
            u32 载荷长 + 载荷字节
```

ADVANCE 的载荷是 `u64 目标时刻, u32 帧数`，随后每帧
`frame_id u64, time_ps u64, channel u8, 3 字节保留, u32 帧长 + 帧字节`。
RESULT 与 EVENT 的载荷是 `u64 序号, u64 frame_id, u32 发送序, u8 终态, u8 channel,
u16 原因, u64 时刻, u32 帧长 + 帧字节`。

## 时间屏障

TX 的仿真时钟自走，中枢的仿真时钟只由 ADVANCE 推动，两者不共享时基。
一次 ADVANCE 的语义：

1. 目标时刻必须严格大于中枢当前时刻；
2. 本批帧必须按 `(time_ps, frame_id)` 严格递增，时刻落在 `(当前, 目标]` 内，
   frame_id 不得重复；
3. 每帧先与 DECLARE 的预言机逐项核对，再按 `time_ps` 排进四路径网络；
4. 中枢把仿真推进到目标时刻（`Simulator::Stop` + `Run`），返回水位
   （等于目标时刻）与本窗口产生的全部终态事件。

FINISH 时不允许有帧在飞：所有帧都必须已经送达或被拒收，否则中枢报错退出。
TX 因此要在最后一帧之后多推进一段排空窗口，长度由 `HELLO_ACK` 广播的速率与
时延算出（最长路径时延 + 最长串行化时间 + 1 ns，与
`FourPathRoceNetwork::DrainGuardPs` 同一口径）。这段窗口由客户端计算，中枢只负责
在 FINISH 时检查。

## 校验清单

**注入前**（每帧，`CheckInjection`）：

* 帧字节与 DECLARE 的 `expected_frame` 逐字节相等；
* 解析出的 QPN、PSN、opcode、载荷长度与声明的四项相等；
* 帧内 MAC/IP 与声明的连接一致；channel 落在 0..3；
* frame_id 未出现过。

DECLARE 本身先做一次自洽检查：帧 id 与消息 id 不重复，每帧指向已声明的消息，
同一消息的包数、requestId、QPN 一致，PSN 等于 `start_psn + packet_index`（24 位回绕），
opcode 序列为单包 0x09 / 首包 0x06 / 中间 0x07 / 末包 0x08。

**送达后**（`Finish` 时，`ValidateDeliveries`）：

* 无损模式下每帧都必须送达、不得被拒收；
* 字节不变、到达时刻不早于注入时刻、每帧恰好一个终态事件；
* 同一条路径上的到达顺序与注入顺序一致（PointToPoint 链路是 FIFO，这条是
  "四条独立路径"而不是"一条队列贴了四个标号"的证据）；
* 按 message 重组：载荷偏移连续、总长等于声明长度、逐字节相等，首包的 RETH
  `messageLength`/`remoteAddress`/`rkey` 与声明一致；
* 四条通道都载过流量（TX 与中枢各查一次）；
* 写报告前不作弊：任一条不通过就抛异常，`status: PASS` 的报告不会落盘。

**乱序统计**（`AnalyzeReorder`）：按送达顺序对注入顺序造成对逆序数与最大回退深度。
口径与离线树一致：`reorder_max_depth` 是"某帧在到达序里相对注入序往回退了多少"的
最大值，不是文献里 max displacement 的定义，别直接拿去跟论文比，只在本流程内部
纵向比较。

### 在线做不了的一条

离线流程有一条交叉核对：`tx-trace.csv` 的 `channel` 列必须与 `tx-frames.csv`
（预言机）的 `channel` 列一致——两个文件是分开写的，如果 trace 在落盘途中把通道
压平了，这条会抓到。在线没有中间的 trace 文件：channel 与帧字节在同一个 ADVANCE
报文里，只有一个来源，因此这条核对无从谈起。中枢改为检查 channel 落在 0..3、
四条通道都被用到，TX 阶段则检查自己交给中枢的 channel 与 commit 流一致。

## RX 侧的门控时钟

RX 的仿真时钟不在测试开始时自由跑：`GatedClock` 持有 `CLK`，只有等到达时刻与把
帧推进流水线的这两段才打开它。每个仿真周期因此都有一个明确的归属，流水线只在
回放指定的地方前进。

真正让 socket 等待不消耗仿真时间的不是门控，而是 cocotb 的 `bridge`：被 bridge
包住的阻塞调用在运行期间占住调度器，仿真器无法推进——这条是实测的（接收阶段在
复位后量一次：等 10 ns 的时钟走 10 ns，被 bridge 包住的 0.05 s 主机休眠走 0 ps，
两个数都写进 `online-rx.json`）。所以关掉门控（`ONLINE_RX_FREE_CLOCK=1`）之后
注入时刻逐位相同：回放里没有任何一处去等一个"裸的时刻"，时钟自由跑也没有东西
可跑。门控排掉的是这种意外——将来有人在等待里加一个按时刻的轮询，门控与
三个偏差数会立刻指出来。

每条 lane 一个驱动任务，各等自己的到达时刻。等待的任务自己把门打开，所以时钟只
会走到它要的那个时刻就停；别的 lane 在灌帧时时钟确实会继续走，但那是在做真实的
流水线工作，不是空转。（四条 lane 共用一个时钟，不等于"到一个时刻"要由一个任务
统一决定：能推动时钟的只有"某一拍要灌"和"某个任务在等自己的时刻"，前者是硬件
行为，后者到点即停。）

RX 的时基原点定在第一次到达：`injection_target_ps = replay_start_ps +
(event.time_ps - origin_ps)`，到达间隔与网络一致。到达事件只有在网络把帧送达之后
才存在，因此这个时刻也是回放**允许使用的最早起点**——离线回放拿着整条 trace，
本可以让一帧的第一拍提前，使最后一拍正好落在到达时刻；在线回放不能提前使用还没
收到的信息。

于是每帧记录三个时刻：目标时刻、实际灌入时刻、该 lane 上一次空出来的时刻。
`online-rx.json` 里对应：

| 字段 | 含义 |
|---|---|
| `max_injection_slack_ps` | 实际灌入时刻与目标时刻之差的最大值 |
| `lane_contention_ps` | 其中"该 lane 还在灌上一帧"能解释的部分 |
| `unexplained_slack_ps` | 两者之差；测试断言它不超过一个 2 ns 周期 |
| `clocked_wait_ps` | 复位后等 10 ns 的时钟实际走了多少（时钟在走，约 10 ns） |
| `bridged_wait_ps` | 同一次运行里被 bridge 包住的 0.05 s 主机休眠走了多少（0） |

背靠背到达同一 lane 的两帧会落在 `lane_contention_ps` 里：到达时刻标的是帧的
**最后一个字节**，回放从这一刻才开始灌，所以第二帧到达时上一帧可能还没灌完。
这是可复现的确定量（本工作负载 26640 ps），门控要排除的是没有来由的迟到。

时基原点不是装饰：把 `injection_target_ps` 里的 `replay_start_ps` 去掉（直接用
`到达时刻 − 网络原点`），第一帧的目标时刻就成了 0，早于接收侧当时的仿真时间
（复位结束是 38000 ps），只能当场灌入，于是被记成 38000 ps 的迟到：

```
AssertionError: frame 0 went in 38000 ps after the earliest instant the replay
allowed (arrival 0, lane free at None)
```

后面的帧只是整条时间线前移、间隔不变，所以失败点就只有这一帧——原点的作用正是
把网络的到达时刻翻译成接收侧自己时间线上"还来得及"的时刻。实验三的第三题走的
就是这条路。

## 丢包模式

默认无损：任何一帧被 DropTail 拒收即失败。中枢在收到 TX 的 FINISH 时检查
（`lossless online run dropped a frame`），抛异常、给两条链路发 ERROR 然后非零退出；
两个阶段据此立刻失败，而不是等到超时。这一次运行不留下任何报告。

`make ARGS="--lossless=0"` 是诊断模式：允许链路队列满时拒收，此时

* 仍逐字节校验**已送达**的帧（字节、路径、时间、每路径 FIFO 顺序都不放过）；
* 只对**收齐了的** message 做重组校验，没收齐的计入 `messages_complete` 而不算失败；
* 丢帧也照常作为 EVENT 推给 RX；RX 记下缺了哪几帧并保持握手（它要等中枢把报告
  写完），在事件流结束、对完账之后才抛出失败。有损流不能用于回放：RX 没有重传逻辑，
  缺 PSN 的 message 永远不 retire。
* 于是诊断运行留下两份报告：中枢的 `status: PASS`（带 `lossless: false` 与丢帧
  明细）与 TX 的 `status: "PASS (lossy)"`；RX 不写报告，启动器非零退出。

丢不丢包取决于**队列占用**，而不是某一组固定参数：同一路径上后一帧到达时若前面
还有帧在等，队列就多占一个包位。按到达间隔与串行化时间算，本树负载在 100 Gbps 下
每路径最多占 1 个包位（只有 ch1 的帧 1→5 差 2 ns 排空、ch3 的帧 3→7 差 22 ns 排空），
所以默认的 100 包队列远没到上限。把速率降到 1 Gbps，串行化时间放大 100 倍，占用
变成 2 个包位：

```
make ARGS="--dataRate=1Gbps --queueSize=1p --lossless=0"
```

帧 8、9、10 分别在 ch0、ch1、ch2 上被拒收（第三个到达的帧遇上已经占满的 1 个包位），
6 笔 message 只剩 4 笔完整，报告里是 `dropped_frames: 3`、`messages_complete: 4`、
`rx_frames: 8`。

## 报文轨迹

中枢默认把两条链路上跨过的每一条报文按顺序追加到 `results/online-ns3.wire`：

```
# tx=10 rx=11
<- fd=10 HELLO requestId=0 body=4
-> fd=10 HELLO_ACK requestId=0 body=56
<- fd=11 HELLO requestId=0 body=4
-> fd=11 HELLO_ACK requestId=0 body=56
<- fd=10 DECLARE requestId=1 body=16144
-> fd=10 DECLARE_ACK requestId=1 body=0
-> fd=11 DECLARE requestId=0 body=16144
<- fd=11 DECLARE_ACK requestId=1 body=0
<- fd=10 ADVANCE requestId=2 body=12
-> fd=10 RESULT requestId=2 body=12
<- fd=10 ADVANCE requestId=3 body=962
-> fd=10 RESULT requestId=3 body=12
<- fd=10 ADVANCE requestId=4 body=1627
-> fd=10 RESULT requestId=4 body=12
<- fd=10 ADVANCE requestId=5 body=12
-> fd=10 RESULT requestId=5 body=346
-> fd=11 EVENT requestId=0 body=334
<- fd=10 ADVANCE requestId=6 body=12
-> fd=10 RESULT requestId=6 body=12
<- fd=10 ADVANCE requestId=7 body=590
-> fd=10 RESULT requestId=7 body=602
-> fd=11 EVENT requestId=0 body=590
<- fd=10 ADVANCE requestId=8 body=1022
-> fd=10 RESULT requestId=8 body=437
-> fd=11 EVENT requestId=0 body=334
-> fd=11 EVENT requestId=0 body=91
...
<- fd=10 FINISH requestId=14 body=0
-> fd=11 EOS requestId=0 body=40
-> fd=10 FINISH_ACK requestId=14 body=76
<- fd=11 FINISH requestId=2 body=8
-> fd=11 FINISH_ACK requestId=2 body=76
```

`->` 是中枢发出的，`<-` 是中枢收到的；第一行的 `# tx=10 rx=11` 是两条链路与文件
描述符的对应关系（描述符每次运行都可能不同）。这份文件是 8 章实验五的核对对象。
读的时候有四条规则：

* **requestId 由发起方维护**。TX 链路上 0 是 HELLO、1 是 DECLARE、2 起是 12 次
  ADVANCE、14 是 FINISH；RX 链路上客户端只发起三条（HELLO 0、DECLARE_ACK 1、
  FINISH 2）。中枢在收到的报文上逐条核对这个计数。
* **RX 链路上中枢发出的非应答报文一律带 requestId 0**：转发过去的 DECLARE、
  11 条 EVENT、最后的 EOS。它们不是对 RX 某个请求的回复，用一个固定值表示
  "推送"。HELLO_ACK 是对 HELLO 的回复，所以也是 0。
* **body 长度可以直接验算**。HELLO 是 flags 4 字节，HELLO_ACK 56 字节；
  ADVANCE = `12 + 24 × 帧数 + 帧字节和`，例如 requestId=3 那次是
  `12 + 24×3 + (298+298+282) = 962`，requestId=4 那次是
  `12 + 24×5 + (554+554+55+198+134) = 1627`（那一批里的 55 字节帧是 QPN 1 的
  985 字节消息分片后剩下的 1 字节尾包）；RESULT 与 EVENT 是同一次到达的两种投递
  （RESULT 给 TX、EVENT 给 RX），条目都是 `36 + 帧字节`，例如 requestId=5 的
  RESULT 是 `12 + 36 + 298 = 346`，随后 RX 链路上的 EVENT 正是 334。
* **DECLARE 在两个方向上长度相同**（16144），因为中枢原样转发，一个字都不改。

顺序上还有一处值得注意：TX 的 FINISH 一到，中枢先把 EOS 推给 RX（附中枢最终计数），
再回 FINISH_ACK——RX 因此能在自己收尾之前先知道网络侧的账目。

不想要这份文件就传 `--wireTrace=`。

## 中枢命令行

```
online-cosim --txSocket=... --rxSocket=... --output=... [--pcapPrefix=...] \
             [--wireTrace=...] [--dataRate=100Gbps] [--queueSize=100p] [--lossless=1]
```

`--pcapPrefix` 为空字符串时不写 pcap。拓扑与离线树相同：2 个节点、4 条
PointToPoint，时延 `{200ns, 400ns, 800ns, 2µs}`，队列 DropTail 100 包，
channel k ↔ 路径 k ↔ RX lane k。
