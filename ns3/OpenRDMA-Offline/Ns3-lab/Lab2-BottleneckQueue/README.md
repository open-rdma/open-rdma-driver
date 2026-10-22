# Lab 2：瓶颈链路与队列

这个实验用三节点拓扑制造一个确定性的瓶颈。发送端按 40 Gbit/s 提供流量，路由器的出口只有
25 Gbit/s，出口 FifoQueueDisc 最多保存 32 个包。

```text
Sender --100 Gbit/s-- Router --25 Gbit/s-- Receiver
```

## 运行

```bash
make lab2
```

结果包括：

- `results/lab2.json`：收发、丢包、吞吐和最大队列深度；
- `results/lab2-queue.csv`：每次队列深度变化的时间戳；
- FlowMonitor 的 IP 层丢包统计。

## 观察重点

- 高于瓶颈速率的流量为什么会积累；
- DropTail 在队列满时如何丢包；
- queue disc trace 和 FlowMonitor 观察的是不同层级；
- 应用结束后要给在途包留出排空时间，否则 FlowMonitor 会把它们记作丢失。
