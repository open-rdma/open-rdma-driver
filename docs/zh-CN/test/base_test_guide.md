# base_test 脚本运行指南

本文说明如何通过 `open-rdma-driver/tests/base_test/scripts/` 下的脚本运行 base test，以及在“环境变量配置”之外，还需要提前完成哪些准备工作。

## 目录与入口

base test 的主要入口在：

- `open-rdma-driver/tests/base_test/scripts/test_loopback_sim.sh`
- `open-rdma-driver/tests/base_test/scripts/test_send_recv_sim.sh`
- `open-rdma-driver/tests/base_test/scripts/test_rdma_write_sim.sh`
- `open-rdma-driver/tests/base_test/scripts/test_write_imm_sim.sh`
- `open-rdma-driver/tests/base_test/scripts/run_all_tests.sh`

这些脚本会自动调用：

- `open-rdma-driver/tests/common/test_common.sh`
- `open-rdma-rtl/test/cocotb/Makefile`

来完成 RTL 仿真器启动、Rust 驱动编译、测试程序编译与运行。

## 运行前准备

除了常规环境变量、Python/Rust/Verilator/conda 环境准备之外，还需要确认下面几项。

### 1. 目录关系正确

默认假设两个仓库并列放置：

```text
<workspace>/
├── open-rdma-driver/
└── open-rdma-rtl/
```

如果 `open-rdma-rtl` 不在默认位置，需要手动设置：

```bash
export RTL_DIR=/path/to/open-rdma-rtl
```

### 2. RTL 编译工具链可用

一键脚本现在会在启动 RTL 仿真器之前，自动调用 `open-rdma-rtl/test/cocotb/Makefile` 的 `verilog` 目标来前置编译 BSV/Verilog。

因此运行前不再要求手工执行：

```bash
make verilog
```

但仍需保证以下工具链可用：

- `bsc` / `bluetcl`
- Python cocotb 依赖
- 选定的仿真器（如 `iverilog`、`verilator`）

脚本每次都会触发一次 `verilog` 目标；是否真正重编由 `open-rdma-rtl/backend` 的 cache/stamp 机制决定。

## 如何通过脚本运行

进入脚本目录：

```bash
cd /path/to/open-rdma-driver/tests/base_test/scripts
```

### 单独运行某个测试

```bash
./test_loopback_sim.sh 4096
./test_send_recv_sim.sh 4096
./test_rdma_write_sim.sh 4096 5
./test_write_imm_sim.sh 4096
```

说明：

- `test_loopback_sim.sh 4096`
  - 单实例 RTL
  - 运行 loopback
- `test_send_recv_sim.sh 4096`
  - 双实例 RTL
  - 运行 server/client send-recv
- `test_rdma_write_sim.sh 4096 5`
  - 双实例 RTL
  - 运行 5 轮 RDMA WRITE
- `test_write_imm_sim.sh 4096`
  - 双实例 RTL
  - 运行 WRITE with Immediate

### 运行整套测试

```bash
./run_all_tests.sh
```

该脚本会顺序调用多个单项脚本，并在任一测试失败后在总结中标记失败项。

## 脚本实际会做什么

以 `test_loopback_sim.sh` 为例，脚本会依次执行：

1. 初始化测试环境
2. 编译 Rust 驱动（`sim` feature）
3. 前置编译 RTL 的 BSV/Verilog
4. 启动 RTL 仿真器
5. 编译 base test 可执行程序
6. 启动测试程序并等待结束

RTL 启动逻辑在：

- `open-rdma-driver/tests/common/test_common.sh`

其中：

- 普通单实例 loopback 会调用 `make run_system_test_server_loopback`
- 双实例测试会调用 `make run_system_test_server_1` / `make run_system_test_server_2`
- PCIe loopback 会调用 `make run_pcie_system_test`

对于 PCIe loopback：

- 会使用 `FLOW=pcie`
- 会编译 `mkBsvTop` + `top_mkBsvTopWithResetBuffer`
- `BLUERDMA_IMMFAIL_ENABLE_TIME` 由 cocotb `Makefile` 内部固定配置，不依赖外部环境变量

## 日志位置

日志默认写到：

- `open-rdma-driver/tests/base_test/log/sim/`

常见文件包括：

- `log/sim/loopback/loopback.log`
- `log/sim/loopback/rtl-loopback.log`
- `log/sim/send_recv/server.log`
- `log/sim/send_recv/client.log`
- `log/sim/send_recv/rtl-server.log`
- `log/sim/send_recv/rtl-client.log`

如果是 PCIe 相关测试，通常还会看到：

- `rtl-pcie_loopback.log`

## 常见问题

### 1. `make verilog` 没有真正执行

如果在 `open-rdma-rtl/test/cocotb` 下看到：

```text
make: 'verilog' is up to date.
```

通常是因为当前目录下存在同名目录 `verilog/`，而 `Makefile` 的 `verilog` 目标没有正确被当作伪目标处理。需要确认 `Makefile` 已包含：

```make
.PHONY: verilog
```

### 2. 脚本能启动，但找不到正确的 DUT

优先检查脚本日志中的 RTL 前置编译阶段是否成功：

- 普通测试应走 `FLOW=default`
- `loopback_pcie` 应走 `FLOW=pcie`
- 若 PCIe 侧 reset 时序有变化，需要同步检查 cocotb `Makefile` 中固定的 `BLUERDMA_IMMFAIL_ENABLE_TIME`

## 推荐执行顺序

首次运行一套 base test 时，建议按下面顺序准备：

```bash
# 1. 回到脚本目录运行测试
cd /path/to/open-rdma-driver/tests/base_test/scripts
./test_loopback_sim.sh 4096
```

如果要整套回归：

```bash
cd /path/to/open-rdma-driver/tests/base_test/scripts
./run_all_tests.sh
```
