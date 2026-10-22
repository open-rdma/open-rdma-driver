# Lab 6：Verilator 单向 Trace 联合仿真

Lab 6 从真实的 OpenRDMA `mkTestM0Chain` 获取 M0→M3 输出，而不是由 Python 手工构造帧。
Cocotb 在每个 M3 帧的 EOF 被取走时记录时间、通道和完整帧，随后 ns-3 离线回放这批数据。

```text
OpenRDMA M0 -> M1 -> M2 -> M3 (Verilator)
                         |
                         +-- trace/oracle CSV
                                   |
                                   v
                         ns-3 四路径网络
```

运行：

```bash
make lab6
```

自动检查包括：

- Verilator 输出与独立 Python 参考模型逐帧一致；
- 四个 M3 通道均被使用；
- PSN 从 `0xffffff` 回绕到 `0`；
- 同一 QP 上存在多条消息；
- ns-3 前后帧字节、frame ID 和通道保持一致；
- 乱序到达后按显式 message/packet oracle 重组，payload 逐字节一致。

时间戳定义为 **M3 EOF dequeue**。因此网络边界是 store-and-forward，不是从 SOF 开始按 beat
进入网络。当前不建模 RoCE ICRC 和 Ethernet FCS。
