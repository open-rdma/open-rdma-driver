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

### 2. 普通 SIM 测试所需 RTL 已生成

普通 base test 使用的 DUT 是：

- `mkBsvTopWithoutHardIpInstance`

对应的 Verilog 需要先在 `open-rdma-rtl/test/cocotb/` 下生成：

```bash
cd /path/to/open-rdma-rtl/test/cocotb
make verilog
```

这一步会在 `open-rdma-rtl/backend/verilog/` 下生成 `mkBsvTopWithoutHardIpInstance.v` 及相关依赖。

### 3. `loopback_pcie` 需要单独生成不同的 TOP

`loopback_pcie` 不使用普通 SIM 测试的 `mkBsvTopWithoutHardIpInstance`，而是需要 PCIe 相关 top。

在运行这类测试前，需要先生成：

```bash
cd /path/to/open-rdma-rtl/test/cocotb
make verilog TOP_MODULE=mkBsvTop
```

也就是说：

- 普通测试：`make verilog`
- `loopback_pcie`：`make verilog TOP_MODULE=mkBsvTop`

如果没有提前生成对应 top，后续脚本虽然会尝试启动 cocotb/Verilator，但可能因为顶层 Verilog 不存在或与当前测试模式不匹配而失败。

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
3. 启动 RTL 仿真器
4. 编译 base test 可执行程序
5. 启动测试程序并等待结束

RTL 启动逻辑在：

- `open-rdma-driver/tests/common/test_common.sh`

其中：

- 普通单实例 loopback 会调用 `make run_system_test_server_loopback`
- 双实例测试会调用 `make run_system_test_server_1` / `make run_system_test_server_2`
- PCIe loopback 会调用 `make run_pcie_system_test`

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

优先检查是否提前生成了正确的顶层：

- 普通测试：`make verilog`
- `loopback_pcie`：`make verilog TOP_MODULE=mkBsvTop`

## 推荐执行顺序

首次运行一套 base test 时，建议按下面顺序准备：

```bash
# 1. 生成普通 SIM RTL
cd /path/to/open-rdma-rtl/test/cocotb
make verilog

# 2. 如需运行 loopback_pcie，再额外生成 PCIe top
make verilog TOP_MODULE=mkBsvTop

# 3. 回到脚本目录运行测试
cd /path/to/open-rdma-driver/tests/base_test/scripts
./test_loopback_sim.sh 4096
```

如果要整套回归：

```bash
cd /path/to/open-rdma-driver/tests/base_test/scripts
./run_all_tests.sh
```
