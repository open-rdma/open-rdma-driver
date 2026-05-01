# Base Test Script Guide

This document explains how to run base tests through the scripts under `open-rdma-driver/tests/base_test/scripts/`, and what needs to be prepared in addition to the usual environment variable setup.

## Paths and Entry Points

The main base test entrypoints are:

- `open-rdma-driver/tests/base_test/scripts/test_loopback_sim.sh`
- `open-rdma-driver/tests/base_test/scripts/test_send_recv_sim.sh`
- `open-rdma-driver/tests/base_test/scripts/test_rdma_write_sim.sh`
- `open-rdma-driver/tests/base_test/scripts/test_write_imm_sim.sh`
- `open-rdma-driver/tests/base_test/scripts/run_all_tests.sh`

These scripts automatically invoke:

- `open-rdma-driver/tests/common/test_common.sh`
- `open-rdma-rtl/test/cocotb/Makefile`

to start the RTL simulator, build the Rust driver, compile the test programs, and run the tests.

## Prerequisites

Besides the usual environment variables and Python/Rust/Verilator/conda setup, confirm the following items.

### 1. Repository layout is correct

By default, the two repositories are expected to be placed side by side:

```text
<workspace>/
├── open-rdma-driver/
└── open-rdma-rtl/
```

If `open-rdma-rtl` is not in the default location, set:

```bash
export RTL_DIR=/path/to/open-rdma-rtl
```

### 2. The RTL build toolchain is available

The one-click scripts now front-load BSV/Verilog compilation by invoking the `verilog` target in `open-rdma-rtl/test/cocotb/Makefile` before starting the RTL simulator.

Because of this, you no longer need to run:

```bash
make verilog
```

manually before running the tests.

However, the following toolchain components must still be available:

- `bsc` / `bluetcl`
- Python cocotb dependencies
- The selected simulator (such as `iverilog` or `verilator`)

Each script run triggers the `verilog` target once; whether a real rebuild happens is decided by the cache/stamp mechanism in `open-rdma-rtl/backend`.

## Running the Scripts

Enter the script directory:

```bash
cd /path/to/open-rdma-driver/tests/base_test/scripts
```

### Run a single test

```bash
./test_loopback_sim.sh 4096
./test_send_recv_sim.sh 4096
./test_rdma_write_sim.sh 4096 5
./test_write_imm_sim.sh 4096
```

Notes:

- `test_loopback_sim.sh 4096`
  - single RTL instance
  - runs loopback
- `test_send_recv_sim.sh 4096`
  - two RTL instances
  - runs server/client send-recv
- `test_rdma_write_sim.sh 4096 5`
  - two RTL instances
  - runs 5 rounds of RDMA WRITE
- `test_write_imm_sim.sh 4096`
  - two RTL instances
  - runs WRITE with Immediate

### Run the full test suite

```bash
./run_all_tests.sh
```

This script invokes multiple individual test scripts in sequence and marks the failed items in the final summary if any test fails.

## What the Scripts Actually Do

Using `test_loopback_sim.sh` as an example, the script performs the following steps:

1. Initialize the test environment
2. Build the Rust driver (with the `sim` feature)
3. Front-load RTL BSV/Verilog compilation
4. Start the RTL simulator
5. Build the base test executable
6. Start the test program and wait for it to finish

The RTL startup logic lives in:

- `open-rdma-driver/tests/common/test_common.sh`

In particular:

- Normal single-instance loopback calls `make run_system_test_server_loopback`
- Two-instance tests call `make run_system_test_server_1` / `make run_system_test_server_2`
- PCIe loopback calls `make run_pcie_system_test`

For PCIe loopback:

- `FLOW=pcie` is used
- `mkBsvTop` and `top_mkBsvTopWithResetBuffer` are compiled
- `BLUERDMA_IMMFAIL_ENABLE_TIME` is fixed inside the cocotb `Makefile`, not supplied through an external environment variable

## Log Locations

Logs are written by default to:

- `open-rdma-driver/tests/base_test/log/sim/`

Common files include:

- `log/sim/loopback/loopback.log`
- `log/sim/loopback/rtl-loopback.log`
- `log/sim/send_recv/server.log`
- `log/sim/send_recv/client.log`
- `log/sim/send_recv/rtl-server.log`
- `log/sim/send_recv/rtl-client.log`

For PCIe-related tests, you will usually also see:

- `rtl-pcie_loopback.log`

## Common Issues

### 1. `make verilog` does not actually rebuild

If you see this under `open-rdma-rtl/test/cocotb`:

```text
make: 'verilog' is up to date.
```

it is usually because a directory named `verilog/` exists in the current directory, and the `verilog` target in the `Makefile` is not being treated as a phony target correctly. Make sure the `Makefile` includes:

```make
.PHONY: verilog
```

### 2. The script starts but cannot find the correct DUT

First check whether the RTL front-load compilation stage in the script logs succeeded:

- Normal tests should use `FLOW=default`
- `loopback_pcie` should use `FLOW=pcie`
- If PCIe-side reset timing changes, also review the fixed `BLUERDMA_IMMFAIL_ENABLE_TIME` value in the cocotb `Makefile`

## Recommended Execution Order

When running a base test suite for the first time, the recommended order is:

```bash
# 1. Return to the script directory and run the test
cd /path/to/open-rdma-driver/tests/base_test/scripts
./test_loopback_sim.sh 4096
```

To run the full regression:

```bash
cd /path/to/open-rdma-driver/tests/base_test/scripts
./run_all_tests.sh
```
