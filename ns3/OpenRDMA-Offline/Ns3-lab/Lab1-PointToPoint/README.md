# Lab 1：点对点 UDP

这个实验建立最小的 ns-3 网络：两个节点、一条 100 Gbit/s 链路和一条 UDP 流。
接收端检查序号与 payload，并把传播时延和序列化时延作为结果的物理下界。

```text
Sender -------- Receiver
       100 Gbps
         1 us
```

## 运行

```bash
make lab1
```

也可以覆盖参数：

```bash
make run LAB=lab1 ARGS='--dataRate=25Gbps --delay=2us --packetCount=200'
```

结果写入 `results/lab1.json`，PCAP 写入 `results/lab1-*.pcap`。

## 观察重点

- `Node`、`NetDevice` 和 `Channel` 的关系；
- UDP socket 如何绑定和发送；
- ns-3 的时间是离散事件时间，不是墙钟时间；
- 单路径无损网络中不应有丢包、重复或乱序。
