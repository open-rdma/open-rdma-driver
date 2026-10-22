# Lab 5：OpenRDMA RoCEv2 帧回放

这个实验读取 CSV 中的完整 M3 风格帧，按时间戳和 channel 注入四条 ns-3 链路。接收端不依赖
ns-3 的 UDP socket，而是从原始字节中解析 Ethernet、IPv4、UDP、BTH 和 RETH，然后按 24-bit
PSN 重排并重组 message。

样例 trace 包含：

- 一个跨 `0xfffffe -> 0xffffff -> 0x000000` 的三包 message；
- 一个 WRITE_ONLY message；
- FIRST 包走最慢路径，故意让 MIDDLE/LAST 先到达；
- 与当前 OpenRDMA M3 一致的 UDP 目的端口 4791 和 RETH 字节顺序。

## 运行

```bash
make lab5
```

重新生成输入 trace：

```bash
make lab5-trace
make lab5
```

结果写入 `results/lab5.json`。`arrival` 保留实际到达的 channel、QPN、PSN 和 opcode。

## 与真实硬件接口的边界

CSV 中 `frame_hex` 是被验证的 M3 frame bytes。实验使用 point-to-point NetDevice 作为透明载体，
该设备自己的 PPP framing 不是 OpenRDMA frame 的一部分。当前 OpenRDMA M3 没有生成 RoCE ICRC，
所以这个 Lab 也明确不校验 ICRC；不能把它当成完整商用 RoCE NIC 的 wire-compatibility 测试。

以后接 RTL 时，只需让 Cocotb 将四路 `EthPacket` 聚合为相同的 CSV 格式：

```text
time_ns,channel,frame_hex
```
