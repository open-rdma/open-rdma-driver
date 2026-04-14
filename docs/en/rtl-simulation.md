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

We recommend creating a dedicated Python environment to avoid conflicts with the system environment or other `cocotb` versions:

```bash
conda create -n cocotb2 python=3.13
conda activate cocotb2
```

First install the `cocotb` development version. This document currently uses commit `a18883468b7de9d4feca497c67db81faf392bdcf` from the `cocotb` repository:

```bash
python -m pip install "cocotb @ git+https://github.com/cocotb/cocotb@a18883468b7de9d4feca497c67db81faf392bdcf"
```

Then install the matching Python dependencies for the current simulation environment:

```bash
python -m pip install cocotb-test cocotbext-pcie cocotbext-axi scapy
```

You can verify the installation with:

```bash
python -m pip show cocotb cocotb-bus cocotbext-pcie cocotbext-axi cocotb-test
python -m pip check
cocotb-config --version
```

**Notes**:
- This document uses the `cocotb` development version instead of the PyPI stable release `1.9.2`
- A tested package combination is `cocotb-bus 0.3.0`, `cocotbext-axi 0.1.28`, `cocotbext-pcie 0.2.16`, and `cocotb-test 0.2.6`
- If the environment previously had `cocotb==1.9.2` or any other older version installed, it is safer to recreate the environment and reinstall from scratch
- If you hit `VerilatedVpi::*` build errors or `No GPI_USERS specified, exiting...`, see [cocotb dev GPI_USERS and Verilator compatibility note](./detail/cocotb-gpi-users-and-verilator-compat.md)

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
