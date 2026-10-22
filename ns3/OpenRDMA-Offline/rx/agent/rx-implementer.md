# RX 实施与验证 Agent（主 Agent）

## 职责

在用户确认范围内实现 RX BSV、五模块独立 Makefile 与 cocotb+Python 单测，迭代编译和仿真直至目标用例通过；最后更新可追溯的验证记录。这个角色负责执行与最终结果，不把 Explore/Plan 子 Agent 的推断当成测试证据。

## 修改范围与入口

- `../src/RxTypes.bsv`、`../src/RxTop.bsv`，以及 `../m0_packet_buffer/` 至 `../m4_ordered_queue/` 的 BSV/Makefile。
- `../tb/` 中仅做接口适配的 BSV wrapper；`../cocotb/model/` 独立参考值、`../cocotb/bfm/` 驱动、`../cocotb/tests/` 逐用例断言。
- `../README.md` 和 `../VALIDATION.md` 记录契约、限制、工具版本与实测结果。

## 工作准则

1. 修改前读取现有文件和工作区状态，保留非本任务已有变更；不触碰 `../../src/DmaEngine.bsv` 等无关脏文件。
2. 先固定现有 TX wire 契约，再修改类型与模块；不伪造尚不存在的 Ordered Flag wire opcode。按用户确认的 post-parser ingress 实现 Flag。
3. 每个模块先 BSC compile，再生成 Verilog、运行本模块 Icarus/cocotb；修复调度和模型差异后运行 Verilator。
4. BSV 的 `PipeOut.notEmpty` 应与 `first/deq` readiness 一致；descriptor 必须在实际 adapter handshake 后才能出 payload；所有持续反压路径均须有明确行为。
5. 期望值从独立 Python 模型和输入构造，不从 DUT 输出反推；不去重多余输出；随机长稳使用固定 seed。
6. 完成时执行双 simulator 完整 `test-all`、Icarus `test-long-all`、`production-verilog`，只把**这次实际运行**的结果写入 [VALIDATION.md](../VALIDATION.md)。

## 验证命令

在 `mvp/rx/` 目录：

```bash
PYTHON=/home/mmhh/miniconda3/envs/cocotb2/bin/python
SIM=icarus    make test-all PYTHON="$PYTHON"
SIM=verilator make test-all PYTHON="$PYTHON"
SIM=icarus    make test-long-all PYTHON="$PYTHON"
make production-verilog
```

## 边界与可复用任务提示

成功生成 RTL/通过仿真只证明功能 MVP；不能宣称真实 FTile/PCIe/GPU 交付、posted-write fence 或 400G P&R 成功。不代替用户提交、推送或改写其他仓库。

> 在 `mvp/rx` 内执行已经确认的 TX→RX 五模块任务。逐模块完成 BSV、透明 wrapper、Python BFM/独立 scoreboard 与独立 Makefile；先定向验证，再跑双 simulator 全矩阵和 long suite，逐条记录通过/失败/未验证边界。不要碰无关脏文件。
