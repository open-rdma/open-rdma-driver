# Lab 4：四路径乱序

发送端和接收端之间有四条独立路径，速率相同但传播时延不同。包按
`channel = sequence % 4` 分发，因此每条路径内部保序，而全局接收顺序会改变。

```text
             +---- 200 ns ----+
             +---- 400 ns ----+
Sender ------+---- 800 ns ----+------ Receiver
             +------ 2 us ----+
```

## 运行

```bash
make lab4
```

结果写入 `results/lab4.json`，其中 `arrival_order` 可以直接看到包的实际到达顺序，
`max_reorder_depth` 表示观测到的最大乱序距离。

## 观察重点

- 路由会根据四个目标 IP 地址选择不同 NetDevice；
- 包一旦进入一条路径，就不会在包中途换路；
- per-path ordering 与 global ordering 是两个不同性质；
- 这正是 OpenRDMA 接收窗口、SACK bitmap 和重传逻辑面对的输入形态。
