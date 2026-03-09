# Base Test Scripts

Automated scripts for running RDMA base tests, primarily for the RTL simulator environment (Sim mode).

## Environment Setup

### RTL Simulator Path Configuration

The test scripts need access to the RTL simulator code (`open-rdma-rtl` repository).

**Method 1: Default path (recommended)**

Clone `open-rdma-rtl` into the same parent directory as `open-rdma-driver`:

```bash
cd /path/to/parent-directory
git clone https://github.com/open-rdma/open-rdma-rtl.git
```

Directory structure:
```
parent-directory/
├── open-rdma-driver/
└── open-rdma-rtl/
```

**Method 2: Custom path**

Set the `RTL_DIR` environment variable:

```bash
export RTL_DIR=/path/to/your/open-rdma-rtl
# Or specify it at runtime
RTL_DIR=/custom/path ./test_loopback_sim.sh
```

## Quick Usage

### Run Individual Tests

```bash
./test_loopback_sim.sh [msg_len]
./test_send_recv_sim.sh [msg_len]
./test_rdma_write_sim.sh [msg_len] [rounds]
./test_write_imm_sim.sh [msg_len]
```

**Examples**:
```bash
./test_loopback_sim.sh 4096              # Loopback, 4KB message
./test_send_recv_sim.sh 8192             # Send/Recv, 8KB message
./test_rdma_write_sim.sh 4096 10         # RDMA Write, 4KB, 10 rounds
./test_write_imm_sim.sh 0                # Write with Imm, zero-length
```

### Run All Tests

```bash
./run_all_tests.sh
```

Example output:
```
==========================================
          Test Suite Summary
==========================================
PASS: Loopback (4KB)
PASS: Send/Recv (4KB)
PASS: RDMA WRITE (4KB, 5 rounds)
PASS: WRITE with IMM (4KB)
==========================================
Total:  4
Passed: 4
Failed: 0
==========================================
```

## Script Descriptions

### test_loopback_sim.sh
Single-node loopback test — two QPs on one device communicate with each other.

**Parameters**:
- `msg_len`: message length in bytes (default: 4096)

### test_send_recv_sim.sh
Two-node Send/Recv test.

**Parameters**:
- `msg_len`: message length in bytes (default: 4096)

### test_rdma_write_sim.sh
Two-node RDMA WRITE multi-round test.

**Parameters**:
- `msg_len`: message length in bytes (default: 4096)
- `rounds`: number of test rounds (default: 5)

### test_write_imm_sim.sh
Two-node RDMA WRITE with Immediate test.

**Parameters**:
- `msg_len`: message length in bytes (default: 4096)
  - Can be set to 0 for a zero-length test (transfers only the immediate value)

### run_dual_sim_test.sh
General two-node test framework used as the basis for the other scripts.

**Usage**:
```bash
./run_dual_sim_test.sh <test_program> [args...]
```

## Test Logs

Logs are saved in the `../log/sim/` directory:

```
log/sim/
├── rtl-loopback.log           # Loopback RTL log
├── send_recv/
│   ├── server.log             # Server application log
│   ├── client.log             # Client application log
│   ├── rtl-server.log         # Server RTL log
│   └── rtl-client.log         # Client RTL log
└── rdma_write/
    └── ...
```

**Viewing logs**:
```bash
cat ../log/sim/rtl-loopback.log               # View log
tail -f ../log/sim/send_recv/server.log       # Live view
```

## What the Scripts Do

All test scripts automatically perform the following steps:

1. **Initialize environment**: Set DRIVER_DIR and RTL_DIR paths
2. **Build Rust driver**: Compile dtld-ibverbs with the sim feature
3. **Start RTL simulator**: Automatically start the required number of RTL instances
4. **Build test programs**: Compile the base_test test programs
5. **Run tests**: Start server/client processes (for two-node tests)
6. **Collect logs**: Save all output to log files
7. **Clean up**: Automatically stop the RTL simulator after the test

## Environment Variables

### RTL_DIR
RTL repository path (optional; defaults to `../../../open-rdma-rtl`)

```bash
export RTL_DIR=/path/to/open-rdma-rtl
```

### RUST_LOG
Rust driver log level (default: `info`)

```bash
RUST_LOG=debug ./test_loopback_sim.sh
```

Available levels: `trace`, `debug`, `info`, `warn`, `error`

## Troubleshooting

### RTL directory not found
```
Error: RTL directory not found: /path/to/open-rdma-rtl
```
**Solution**:
- Confirm the RTL repository has been cloned
- Check the directory structure or set the `RTL_DIR` environment variable

### RTL fails to start
```
Error: RTL process failed to start or died
```
**Solution**:
- View RTL logs: `cat ../log/sim/rtl-*.log`
- Ensure the RTL repository is complete (including submodules)

### Test timeout
**Solution**:
- Check application logs: `tail ../log/sim/<test>/server.log`
- Check RTL logs for errors

### Data validation failure
The test will automatically display byte-level differences. Check:
- Detailed diff information in the logs
- Whether the RTL simulator is working correctly

## References

- [../README.md](../README.md) - Test framework overview
- [../../common/test_common.sh](../../common/test_common.sh) - Common test function library
