# ns3/：离线四路径网络与三阶段驱动

## 文件

| 文件 | 说明 |
|---|---|
| `offline-txrx.cc` | ns-3 程序：读 TX 工件 → 四路径网络 → 校验 → 写 RX 工件 |
| `common/roce-frame.h` | RoCE 帧构造/解析（Ethernet + IPv4 + UDP + BTH/RETH），自 `Ns3-lab/common` 复制 |
| `common/lab-common.h` | lab 公共小工具，同上 |
| `common/four-path-roce.h` | `FourPathRoceNetwork`；本树加了 `LinkConfig`（速率/队列/时延）与对应构造函数，`DrainGuardPs` 也改为跟随配置的速率/时延 |
| `cocotb/offline_lib.py` | 工件格式与共享常量（三阶段都用） |
| `cocotb/tx_export.py` | 阶段 1 测试模块（TX RTL 导出） |
| `cocotb/rx_replay.py` | 阶段 3 测试模块（RX RTL 回放） |
| `cocotb/offline_runner.py` | 阶段启动器；`make` 的 `tx`/`ns3`/`rx`/`all` 都进这里 |
| `build/` | ns-3 程序的编译输出（`ns3.48-offline-txrx`），可删 |

编译不需要 cmake：`offline_runner.py` 从 `NS3_DIR/build/lib` 里发现 `libns3.<版本>-<profile>`
前缀，把该前缀下的全部共享库（排除 `-test-`）连同 `build/include` 的头文件路径一起链接进来，
所以换一个 ns-3 版本不用改代码。`NS3_DIR` 默认 `Ns3-lab/ns-3`。

## ns-3 程序

```
ns3.48-offline-txrx --txTrace=... --frameOracle=... --messageOracle=... \
                    --connection=... --rxTrace=... --output=... [--pcapPrefix=...] \
                    [--dataRate=100Gbps] [--queueSize=100p] [--lossless=1]
```

拓扑：2 个节点、4 条 PointToPoint；每个包的 M3 出口 channel 固定对应一条路径
（channel k ↔ 路径 k ↔ RX lane k；同一个 QP 的连续包可以走不同路径），
时延默认 `{200ns, 400ns, 800ns, 2µs}`，队列 DropTail 100 包。帧按 `eof_time_ps` 注入，
`FrameIdTag` 随包携带 id，接收侧按到达顺序编号写 `rx-trace.csv`（只写真正到达的帧）。

校验（任一不通过即失败退出）：

* **注入前**：每帧字节与 oracle 一致；QPN/PSN/opcode/payload 长度与 oracle 自洽；
  trace 的 channel 与 frame oracle 的 channel 一致、四通道全部出现；每帧的 MAC/IP
  与 `tx-connection.csv` 声明的连接一致；
* **送达后**：字节未变、到达时间不早于注入时间、每帧恰好一个终态事件；
  同一条路径上的到达顺序必须与注入顺序一致（PointToPoint 是 FIFO，这是"四条独立
  路径"而不是"一条队列贴了四个标号"的证据）；按 message 重组的 payload 与 oracle
  逐字节相等、每个 QP 的 PSN 与 opcode 序列正确、RETH 的 rkey/addr/length 正确；
* 统计成对逆序数与最大乱序深度写进 JSON（用来证明网络确实重排了）。注意
  `reorder_max_depth` 的口径是"某帧在到达序里相对注入序往回退了多少"的最大值，
  不是文献里 max displacement 的定义，别直接拿数字跟论文比；`reorder_inversions`
  也一样，只在本流程内部纵向比较（换链路参数时同一口径前后对比）。

### 丢包模式

默认 `--lossless=1`：任何一帧被拒收就失败，且要求每帧都送达、每条 message 都完整重组。

`--lossless=0` 是诊断模式：允许链路队列满时拒收（DropTailQueue 的行为），此时
* 仍逐字节校验**已送达**的帧（字节、路径、时间、每路径 FIFO 顺序都不放过）；
* 只对**收齐了的** message 做重组校验，没收齐的计入 `messages_complete` 而不是失败；
* `rx-trace.csv` 只写到达的帧 —— 没到的帧不会被送给 RX 回放；
* JSON 里 `lossless:false`、`dropped_frames`、`messages_complete` 说明丢了什么。

要看丢包得同时把速率调慢 + 队列调小，例如 `--dataRate=1Mbps --queueSize=1p`
（在这个 workload 下丢 3 帧、6 条 message 完成 4 条）。注意此时 **RX 回放阶段仍要求
无损 trace**：RX 没有重传逻辑，缺 PSN 的 message 永远不 retire，所以 `--lossless=0`
的输出是给网络分析用的，不是给端到端流程用的。
