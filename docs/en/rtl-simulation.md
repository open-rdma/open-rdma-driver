# Open RDMA RTL Hardware Simulation Project Installation

> **Note**: This document describes the standalone `open-rdma-rtl` hardware simulation project, which lives in a separate repository from `open-rdma-driver`.

## Installation Steps

### 1. Clone the Project

**Run from the directory where you want to place the project**:
```bash
git clone https://github.com/open-rdma/open-rdma-rtl.git
cd open-rdma-rtl
git checkout dev
```

### 2. Install BSC

**Run from the open-rdma-rtl project root**:
```bash
./setup.sh  # Installs bsc and adds environment variables to ~/.bashrc
```

**Note**: Make sure the BSC version matches your Ubuntu version (e.g., Ubuntu 22.04 requires bsc-2023.01-ubuntu-22.04).

### 3. Install Simulation Dependencies

**System dependencies**:
```bash
sudo apt install iverilog verilator zlib1g-dev tcl8.6 libtcl8.6
```

**Python dependencies**:

Install conda (other Python environments also work):
```bash
mkdir -p ~/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh
bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3
rm ~/miniconda3/miniconda.sh

source ~/miniconda3/bin/activate
conda init --all
```

```bash
pip install cocotb==1.9.2 cocotb-test cocotbext-pcie cocotbext-axi scapy
```
Note: the current test code is not compatible with cocotb 2.0.

**Notes**:
- `verilator` (not `iverilog`) is used for simulation
- `tcl8.6` and `libtcl8.6` are required for BSC backend compilation

### 4. Build the Backend

**Run from the open-rdma-rtl project root**:
```bash
cd test/cocotb && make verilog
```

The generated Verilog files are located in the `backend/verilog/` directory.

### 5. Run System-Level Tests

**Single-NIC loopback test** (recommended for quick verification):

**Run from the open-rdma-rtl project root**:
```bash
cd test/cocotb
make run_system_test_server_loopback
```

**Dual-NIC test** (requires two terminals running simultaneously):

**Terminal 1 (run from open-rdma-rtl project root)**:
```bash
# Start server 1 (INST_ID=1)
cd test/cocotb
make run_system_test_server_1
```

**Terminal 2 (run from open-rdma-rtl project root)**:
```bash
# Start server 2 (INST_ID=2)
cd test/cocotb
make run_system_test_server_2
```

Test logs are saved in the `test/cocotb/log/` directory (with `.loopback`, `.1`, `.2` suffixes).

## Using with Open RDMA Driver

The driver must first be built in sim mode, and all other driver setup must be completed.

**Run from the open-rdma-driver project root**:
```bash
cd dtld-ibverbs
cargo build --no-default-features --features sim
cd ..
```

The `sim` mode of Open RDMA Driver requires this project's simulator to be started first:

### Single-node test (loopback)

**Terminal 1 (run from open-rdma-rtl project root)**:
```bash
# Start the hardware simulator
cd test/cocotb
make run_system_test_server_loopback
```

**Terminal 2 (run from open-rdma-driver project root)**:
```bash
# Run driver test
cd examples
make
RUST_LOG=debug ./loopback 8192
```

### Two-node test (send_recv)

**Terminal 1 (run from open-rdma-rtl project root)**:
```bash
# Start hardware simulator 1
cd test/cocotb
make run_system_test_server_1
```

**Terminal 2 (run from open-rdma-rtl project root)**:
```bash
# Start hardware simulator 2
cd test/cocotb
make run_system_test_server_2
```

**Terminal 3 (run from open-rdma-driver project root)**:
```bash
# Build and run driver test server
cd examples
make
RUST_LOG=debug ./send_recv 8192
```

**Terminal 4 (run from open-rdma-driver project root)**:
```bash
# Run driver test client
cd examples
RUST_LOG=debug ./send_recv 8192 127.0.0.1
```
