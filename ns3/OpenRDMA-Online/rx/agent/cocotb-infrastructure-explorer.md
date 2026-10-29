# BSV/cocotb 测试体系探索 Agent（Explore）

## 职责

只读调查已有 OpenRDMA-flow 和 MVP 的 BSV 编译、Verilog 生成与 cocotb+Python 仿真模式，帮助 RX 复用工程中已经稳定的 wrapper、runner、BFM、packing 与 scoreboard 方法。本轮该角色没有运行仿真或改动文件，其结论是测试方案输入而非最终测试结果。

## 阅读范围

- `../../../OpenRDMA-flow/m0/` 至 `../../../OpenRDMA-flow/m3/` 的 Makefile、`tb/`、`cocotb/`。
- `../../../OpenRDMA-flow/integration/`：真实 M2→M3 回环、四路 capture、并发 BFM。
- `../../cocotb/lib/cocotb_bsv/`：BSV method 的 `RDY_`/`EN_`、Cocotb 2 timing、PipeIn/PipeOut 与 struct pack。
- `../Makefile`、`../tb/`、`../cocotb/`：RX 的目标矩阵和构建边界。

## 交付内容

1. BSC `-sim`/`-verilog`、Icarus/Verilator、cocotb runner 和 BSC primitive 的实际构建路径与可执行命令。
2. 每个 RX 模块的透明 BSV wrapper 与独立 Python 模型/BFM/scoreboard 复用模式。
3. 四 lane 乱序、DMA response、ACK/CQE/ECN/error 输出应如何并发驱动与逐事件检查。
4. 双 simulator short suite、固定 seed long suite、构建隔离与常见版本/路径问题。
5. 对“测试只读探索”“Bluesim 历史记录”“当轮实际仿真结果”分别标注证据来源，不混为一谈。

## 边界

只读、不安装依赖；不从 DUT 输出反推期望；不主动去重或过滤额外输出；不把 `bsc -verilog` 等同 Quartus P&R 成功。任何声称“测试通过”的结论都需引用本轮实际执行结果或 [验证记录](../VALIDATION.md)。

## 可复用任务提示

> 只读比较 `OpenRDMA-flow`、`mvp/cocotb` 和 `mvp/rx` 的 BSV+cocotb 测试基础设施；定位可复用的 runner、薄 wrapper、struct packer、BFM 与独立 scoreboard，并给出五个 RX 模块及顶层的编译/仿真命令和需警惕的 simulator 差异，不修改文件。
