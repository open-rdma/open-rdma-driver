# Open RDMA Driver Installation Guide

This document provides quick installation steps for the Open RDMA Driver. For detailed technical information and troubleshooting, refer to the documents in the [detail](./detail/) folder.

## Requirements

- Linux system (WSL2 supported)
- Rust toolchain
- Kernel version >= 6.6 (WSL requires manually compiling the kernel module)

## Installation Steps

### 1. Install the Rust Toolchain

**Run from any directory**:
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env
```

### 2. Install System Dependencies

**Run from any directory**:
```bash
sudo apt install cmake pkg-config libnl-3-dev libnl-route-3-dev libclang-dev libibverbs-dev
```

### 3. Clone the Project and Initialize Submodules

**Run from the directory where you want to place the project** (a short path such as `/home/user/` is recommended):
```bash
git clone --recursive https://github.com/open-rdma/open-rdma-driver.git
cd open-rdma-driver
git checkout dev

# If --recursive was not used during clone, initialize manually
git submodule update --init --recursive
```

**Note**: The project path should not be too long; `/home/user/open-rdma-driver` is recommended over deeply nested paths. See: [Path Length Issue](./detail/path-length-issue.md)

### 4. Build and Load the Driver Module

**WSL2 environments** need to prepare kernel headers first. See: [WSL2 Kernel Headers Preparation Guide](./detail/wsl-kernel-headers.md). Alternatively, you can use `make KBUILD_MODPOST_WARN=1` to skip kernel header validation, though this carries some risk.

**Run from the open-rdma-driver project root**:
```bash
# Build the driver
make

# If BTF generation fails (common on WSL), use:
# make KBUILD_MODPOST_WARN=1

# Load the driver module
sudo make install
```

**Run from any directory to verify the driver loaded successfully**:
```bash
lsmod | grep bluerdma
# Should show: bluerdma
```

### 5. Configure Network Interfaces

Assign IP addresses to the Open RDMA virtual network interfaces.

**Run from any directory**:
```bash
sudo ip addr add 17.34.51.10/24 dev blue0
sudo ip addr add 17.34.51.11/24 dev blue1
```

**Run from any directory to verify the configuration**:
```bash
ip addr show blue0
ip addr show blue1

# In simulation mode you may need to bring the interfaces down to prevent
# the configuration from being cleared (the exact reason is unclear)
sudo ip link set dev blue0 down
sudo ip link set dev blue1 down
```

### 6. Allocate Huge Pages

The Open RDMA Driver requires huge page memory. Use the provided script to allocate 512 MB of huge pages.

**Run from the open-rdma-driver project root**:
```bash
sudo ./scripts/hugepages.sh alloc 512
```

**Run from any directory to verify the allocation**:
```bash
cat /proc/meminfo | grep Huge
```

### 7. Build the Userspace Library (dtld-ibverbs)

Choose a build mode based on your use case:

**Mock mode (recommended for development and testing)**:

**Run from the open-rdma-driver project root**:
```bash
cd dtld-ibverbs
cargo build --no-default-features --features mock
cd ..
```
- No dependency on real hardware or simulator
- Suitable for rapid development and functional testing
- Performance test results are not realistic

**Sim mode (for RTL simulator debugging)**:

**Run from the open-rdma-driver project root**:
```bash
cd dtld-ibverbs
cargo build --no-default-features --features sim
cd ..
```
- Requires the RTL simulator to be started first (the simulator from the achronix-400g project)
- Used for hardware logic verification
- The simulator must be started in a separate terminal before running tests (see achronix-400g project documentation)

**Hardware mode (hw)**:

**Run from the open-rdma-driver project root**:
```bash
cd dtld-ibverbs
cargo build --no-default-features --features hw
cd ..
```
- ⚠️ **Note**: Hardware mode has not been fully tested and may have issues
- Use only when a real hardware device is available

### 8. Build rdma-core

**Run from the open-rdma-driver project root**:
```bash
cd dtld-ibverbs/rdma-core-55.0

# Basic build
./build.sh

# To generate compile_commands.json for debugging:
# export EXTRA_CMAKE_FLAGS=-DCMAKE_EXPORT_COMPILE_COMMANDS=1
# ./build.sh

cd ../..
```

**Common issue**: If the build fails at around 81% with "size of unnamed array is negative", this is caused by a path that is too long. See: [Path Length Issue Details](./detail/path-length-issue.md)

### 9. Set Environment Variables

**Option 1: Permanent (recommended)**

Add the environment variables directly to `~/.bashrc` so they are loaded automatically every time a terminal is opened.

**Run from the open-rdma-driver project root**:
```bash
# Add to .bashrc
cat >> ~/.bashrc << EOF

# Open RDMA Driver Environment
if [ -z "\$LD_LIBRARY_PATH" ]; then
    export LD_LIBRARY_PATH="$(pwd)/dtld-ibverbs/target/debug:$(pwd)/dtld-ibverbs/rdma-core-55.0/build/lib"
else
    export LD_LIBRARY_PATH="$(pwd)/dtld-ibverbs/target/debug:$(pwd)/dtld-ibverbs/rdma-core-55.0/build/lib:\$LD_LIBRARY_PATH"
fi
EOF

# Apply immediately
source ~/.bashrc
```

**Option 2: Temporary (current terminal only)**

**Run from the open-rdma-driver project root**:
```bash
# Use the provided script
source ./scripts/setup-env.sh

# Or set manually
export LD_LIBRARY_PATH=$PWD/dtld-ibverbs/target/debug:$PWD/dtld-ibverbs/rdma-core-55.0/build/lib
```

### 10. Verify the Installation

#### Option 1: Use the Automated Test Framework (recommended for Sim mode)

The Open RDMA Driver provides an automated test framework that can automatically start the RTL simulator, build the driver and test programs, run tests, and collect logs. The test framework is located in the `tests/base_test/` directory.

**Environment preparation**:

The test framework needs access to the RTL simulator code (`open-rdma-rtl` repository). There are two ways to configure the RTL path:

**Method 1: Use the default path (recommended)**

Clone the `open-rdma-rtl` repository into the same parent directory as `open-rdma-driver`:

**Run from the parent directory** (if already in the `open-rdma-driver` directory, first run `cd ..`):
```bash
git clone https://github.com/open-rdma/open-rdma-rtl.git
```

The directory structure should be:
```
parent-directory/
├── open-rdma-driver/
└── open-rdma-rtl/
```

**Method 2: Custom RTL path**

If the RTL repository is in a different location, set the `RTL_DIR` environment variable:

**Set before running tests**:
```bash
export RTL_DIR="/path/to/your/open-rdma-rtl"
```

Or specify it each time when running tests:
```bash
RTL_DIR="/path/to/your/open-rdma-rtl" ./scripts/test_loopback_sim.sh
```

**Running tests**:

**Run from the open-rdma-driver/tests/base_test directory**:

```bash
# Enter the test directory
cd tests/base_test

# Run individual tests
./scripts/test_loopback_sim.sh 4096              # Loopback test
./scripts/test_send_recv_sim.sh 4096             # Send/Recv test
./scripts/test_rdma_write_sim.sh 4096 5          # RDMA Write test (5 rounds)
./scripts/test_write_imm_sim.sh 4096             # Write with Immediate test

# Run all tests
./scripts/run_all_tests.sh
```

Test logs are automatically saved in the `tests/base_test/log/sim/` directory, where you can view detailed test output and RTL simulator logs.

**Viewing test logs**:
```bash
# View loopback test log
cat log/sim/rtl-loopback.log

# View send_recv test server log
cat log/sim/send_recv/server.log

# View send_recv test client log
cat log/sim/send_recv/client.log
```

**Note**: The automated test framework will:
- Automatically build the Rust driver (sim mode)
- Automatically start and stop the RTL simulator
- Automatically build test programs
- Automatically run server and client
- Collect all logs to the specified directory

#### Option 2: Manually Run Example Programs

**Run from the open-rdma-driver project root to build example programs**:
```bash
cd examples
make
```

**Running example programs**:

Run according to the mode selected at build time:

##### Mock mode

**Single-node loopback test**:

**Run from the open-rdma-driver/examples directory**:
```bash
./loopback 8192
```

**Two-node test (send_recv)**:

**Terminal 1 (run from open-rdma-driver/examples directory)**:
```bash
# Start server
./send_recv 8192
```

**Terminal 2 (run from open-rdma-driver/examples directory)**:
```bash
# Start client, connect to local server
./send_recv 8192 127.0.0.1
```

##### Sim mode

**Single-node loopback test**:
```bash
# 1. First start the simulator in a separate terminal (in the achronix-400g project)
# See the achronix-400g project documentation for the specific start command

# 2. Run the test from open-rdma-driver/examples
./loopback 8192
```

**Two-node test (send_recv)**:
Requires starting two separate simulator instances (see achronix-400g project documentation), then run:

**Terminal 3 (run from open-rdma-driver/examples directory)**:
```bash
./send_recv 8192
```

**Terminal 4 (run from open-rdma-driver/examples directory)**:
```bash
./send_recv 8192 127.0.0.1
```

##### Debug options

**To see detailed logs, add the environment variable**:
```bash
RUST_LOG=debug ./loopback 8192
# or
RUST_LOG=debug ./send_recv 8192
```

A successful run will display output from the RDMA operations.

## Quick Command Reference

**Note**: The commands below must be run from specific directories; pay attention to the directory notes.

```bash
# 1. Environment preparation (run from any directory)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
sudo apt install cmake pkg-config libnl-3-dev libnl-route-3-dev libclang-dev libibverbs-dev

# 2. Clone project (run from your chosen directory; a short path is recommended)
git clone --recursive https://github.com/open-rdma/open-rdma-driver.git
cd open-rdma-driver
git checkout dev

# ========== The following commands run from the open-rdma-driver project root ==========

# 3. Build and load driver (WSL2 needs kernel headers first)
make && sudo make install

# 4. Configure network (run from any directory)
sudo ip addr add 17.34.51.10/24 dev blue0
sudo ip addr add 17.34.51.11/24 dev blue1

# 5. Allocate huge pages
sudo ./scripts/hugepages.sh alloc 512

# 6. Build userspace library (choose mode: mock/sim/hw)
# Mock mode (recommended):
cd dtld-ibverbs && cargo build --no-default-features --features mock && cd ..
# Sim mode (requires simulator to be started first):
# cd dtld-ibverbs && cargo build --no-default-features --features sim && cd ..
# Hardware mode (untested):
# cd dtld-ibverbs && cargo build --no-default-features --features hw && cd ..

# 7. Build rdma-core
cd dtld-ibverbs/rdma-core-55.0 && ./build.sh && cd ../..

# 8. Set environment variables (permanent) - run from open-rdma-driver project root
cat >> ~/.bashrc << EOF

# Open RDMA Driver Environment
if [ -z "\$LD_LIBRARY_PATH" ]; then
    export LD_LIBRARY_PATH="$(pwd)/dtld-ibverbs/target/debug:$(pwd)/dtld-ibverbs/rdma-core-55.0/build/lib"
else
    export LD_LIBRARY_PATH="$(pwd)/dtld-ibverbs/target/debug:$(pwd)/dtld-ibverbs/rdma-core-55.0/build/lib:\$LD_LIBRARY_PATH"
fi
EOF
source ~/.bashrc

# 9. Run examples - run from open-rdma-driver project root
cd examples && make && ./loopback 8192

# 10. (Optional) Clone RTL repository for automated testing
cd .. && git clone https://github.com/open-rdma/open-rdma-rtl.git
```

## Frequently Asked Questions

### Q1: rdma-core build fails at 81%
**Cause**: Project path is too long, causing the Unix socket path to exceed the limit.
**Solution**: Move the project to a shorter path (e.g., `/home/user/open-rdma-driver`).
**See**: [Path Length Issue](./detail/path-length-issue.md)

### Q2: Cannot find `infiniband/verbs_api.h`
**Cause**: The `libibverbs-dev` package is missing.
**Solution**: `sudo apt install libibverbs-dev`

### Q3: Driver build fails under WSL with "kernel headers not found"
**Cause**: WSL does not provide kernel headers by default.
**Solution**: Follow step 3 to build the WSL2 kernel and link the headers.
**See**: [WSL2 Kernel Headers Preparation Guide](./detail/wsl-kernel-headers.md)

### Q4: No RDMA devices found when running examples, or shared library not found
**Cause**: `LD_LIBRARY_PATH` may not be set.
**Solution**: Run `source ./scripts/setup-env.sh`

### Q5: OFED conflicts with vanilla RDMA
**See**: [Switching to vanilla RDMA](./detail/switch-to-vanilla-rdma.md)

### Q6: How do I choose a build mode (mock/sim/hw)?
**Mock mode**: Recommended for development and functional testing; no hardware or simulator required
**Sim mode**: For RTL simulation verification; requires the simulator to be started first
**Hardware mode**: Use only when a real hardware device is available; ⚠️ not fully tested yet

### Q7: Example program cannot run in Sim mode
**Cause**: The RTL simulator has not been started
**Solution**:
1. **Recommended**: Use the automated test framework (see step 10, Option 1) — it starts and manages the RTL simulator automatically
2. **Manual**: Start the simulator in a separate terminal (see the open-rdma-driver project installation documentation) before running the test program

### Q8: Automated test reports "RTL directory not found"
**Cause**: The RTL repository path is not configured correctly
**Solution**:
1. **Option 1**: Clone the `open-rdma-rtl` repository into the same parent directory as `open-rdma-driver`:
   ```bash
   cd /path/to/parent-directory
   git clone https://github.com/open-rdma/open-rdma-rtl.git
   ```
2. **Option 2**: Set the `RTL_DIR` environment variable to point to your RTL repository:
   ```bash
   export "RTL_DIR=/path/to/your/open-rdma-rtl"
   ```

## Related Documentation

- [WSL2 Kernel Headers Preparation Guide](./detail/wsl-kernel-headers.md)
- [Path Length Issue Details](./detail/path-length-issue.md)
- [OFED Symbol Version Fix](./detail/ofed-symbol-version-fix.md)
- [OFED RoCE Registration Issue](./detail/ofed-roce-registration-issue.md)
- [Switching to vanilla RDMA](./detail/switch-to-vanilla-rdma.md)
- [Automated Test Framework Overview](../../tests/base_test/README.md)
- [Test Script Usage Guide](../../tests/base_test/scripts/README.md)
