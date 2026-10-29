# OpenRDMA-Online

OpenRDMA 收发 RTL 与 ns-3 的在线联合仿真。TX、网络和 RX 分别运行在独立进程中，通过 Unix domain socket 传递帧和时间戳。

```text
TX RTL → ns-3 四路径网络 → RX RTL
```

收发两侧使用 Bluespec、Verilator 和 cocotb。TX 按时间窗口提交帧，ns-3 模拟排队与传输，RX 按到达时间输入报文，并核对 DMA 地址、数据和消息完成结果。预期结果由独立的 Python 参考模型生成。

## 运行

需要 Linux、Bluespec、Verilator、cocotb 2，以及已配置并编译的 ns-3.48。

```bash
make COCOTB_PYTHON=/path/to/python3 NS3_DIR=/path/to/ns-3
```

默认使用 `../OpenRDMA-Offline/Ns3-lab/ns-3`。如需构建该目录下的 ns-3：

```bash
make -C ../OpenRDMA-Offline/Ns3-lab build
```

其他命令：

```bash
make test       # TX、RX 单元及集成测试，不经过 ns-3
make clean      # 清理构建产物和默认结果目录
```

也可在三个终端中分别启动，使用相同的 Python、ns-3 和结果目录配置：

```bash
make hub        # 先启动网络中枢，等待 TX、RX 连接
make tx
make rx
```

## 配置

以下变量可通过 `make 变量=值` 设置。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `COCOTB_PYTHON` | `/home/l/miniconda3/envs/cocotb2/bin/python3` | 安装了 cocotb 2 的解释器，按本机路径修改 |
| `NS3_DIR` | `../OpenRDMA-Offline/Ns3-lab/ns-3` | 已构建的 ns-3 目录 |
| `ONLINE_RESULTS` | `results/` | 报告与 socket 目录 |
| `ARGS` | 空 | 网络参数，如 `--dataRate=1Gbps --queueSize=1p` |
| `ONLINE_TX_WINDOW_PS` | `100000`（100 ns） | TX 提交窗口长度 |
| `ONLINE_RX_FREE_CLOCK` | 未设置 | 设为 `1` 关闭 RX 时钟门控 |

## 目录

| 路径 | 内容 |
|---|---|
| `tx/` | M0–M3 发送 RTL 与测试 |
| `rx/` | 接收 RTL 与测试 |
| `ns3/online-cosim.cc` | 网络中枢 |
| `ns3/common/` | 网络模型、帧格式和 socket 协议 |
| `ns3/cocotb/` | 收发驱动、协议客户端和启动器 |
| `results/` | 运行结果 |

## 结果与限制

默认网络包含四条 100 Gbps 路径，时延分别为 200 ns、400 ns、800 ns 和 2 µs，每条路径的队列容量为 100 个包。基线负载包含 6 笔消息、11 帧，覆盖四个 QP、PSN 回绕和目标地址重叠；预期全部送达，产生 13 对逆序。

运行成功时退出码为 0，`results/` 下三份 JSON 报告的 `status` 均为 `PASS`：

| 文件 | 内容 |
|---|---|
| `online-tx.json` | 发送帧、窗口数及传输结果 |
| `online-ns3.json` | 路径、时间戳、丢包与乱序统计 |
| `online-rx.json` | 接收校验与注入时间偏差 |
| `online-ns3.wire` | 进程间报文记录 |

当前未接入端到端重传。使用 `ARGS="--dataRate=1Gbps --queueSize=1p --lossless=0"` 可观察丢包：网络与 TX 保留诊断报告，RX 因缺帧校验失败，整次运行返回非零退出码。接收校验针对模拟 DMA 接口，不代表真实 PCIe 写入或 GPU 显存可见性验证。

网络参数与协议见 [ns-3 说明](ns3/README.md)。
