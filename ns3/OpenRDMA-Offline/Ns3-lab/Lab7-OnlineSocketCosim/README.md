# Lab 7：Socket 在线联合仿真

Lab 7 同时运行 OpenRDMA Verilator/cocotb 和 ns-3。M3 每完成一个帧，cocotb 都会通过 Unix
Domain Socket 把帧送入 ns-3；ns-3 按虚拟时间推进四路径网络，再把到达或丢包事件返回给
cocotb。

```text
Verilator/cocotb                 ns-3
M0 -> M1 -> M2 -> M3   <---->   4 x 100 Gbit/s path
        frame batches   socket   delivery/drop telemetry
```

运行：

```bash
make lab7
```

协议使用大端、长度前缀的二进制消息，并包含版本、request ID、frame ID 和皮秒时间戳。双方采用
`ADVANCE/RESULT` 屏障推进虚拟时间，不使用依赖主机负载的 real-time scheduler。

当前是 **TX 在线 + 到达/丢包回报**：网络事件会回到 cocotb 并被检查，但不会注入 RX RTL。
OpenRDMA 目前还没有完整的 Ethernet/RoCE RX、ACK/NAK 和重传闭环。ICRC 和 Ethernet FCS
同样不在本 Lab 的模型中。
