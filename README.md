# Open RDMA Driver

[English](README.md) | [中文](README.zh-CN.md)

A Rust-based RDMA driver for the Open RDMA hardware platform. It implements the standard `libibverbs` provider interface and integrates with the Linux RDMA subsystem through a hybrid kernel/userspace architecture.

## Architecture

The driver stack consists of four layers:

```
User Application  (perftest, MPI, RCCL, ...)
       │ ibv_*() calls
       ▼
libibverbs + C Provider          (rdma-core / dtld-ibverbs)
       │ dlopen("libbluerdma_rust.so")
       ▼
Rust Driver                      (rust-driver / libbluerdma_rust.so)
       │ PCIe MMIO / UDP RPC / in-process mock
       ▼
Hardware / RTL Simulator / Mock
```

| Component | Location | Role |
|---|---|---|
| Kernel module (`bluerdma.ko`) | `kernel-driver/` | Registers IB device, exposes sysfs/uverbs interface |
| C Provider | `dtld-ibverbs/rdma-core-55.0/providers/bluerdma/` | Bridges libibverbs to Rust via `dlopen` |
| Rust driver (`libbluerdma_rust.so`) | `rust-driver/` | Core verbs logic, DMA, ring buffers, worker threads |

## Operation Modes

| Mode | Feature Flag | Use Case |
|---|---|---|
| **Mock** | `--features mock` | Unit testing and CI — no hardware or simulator required |
| **Sim** | `--features sim` | RTL simulation validation via UDP RPC |
| **Hardware** | `--features hw` | Physical PCIe RDMA device (experimental) |

## Prerequisites

- Linux kernel ≥ 6.6 (WSL2 supported with custom kernel build)
- Rust toolchain (`rustup`)
- System packages: `cmake`, `pkg-config`, `libnl-3-dev`, `libnl-route-3-dev`, `libclang-dev`, `libibverbs-dev`

## Quick Start

```bash
# 1. Clone the repository
git clone --recursive https://github.com/open-rdma/open-rdma-driver.git
cd open-rdma-driver
git checkout dev

# 2. Build and load the kernel module
make && sudo make install

# 3. Configure virtual network interfaces
sudo ip addr add 17.34.51.10/24 dev blue0
sudo ip addr add 17.34.51.11/24 dev blue1

# 4. Allocate hugepages (512 MB)
sudo ./scripts/hugepages.sh alloc 512

# 5. Build the userspace driver (mock mode recommended for first run)
cd dtld-ibverbs && cargo build --no-default-features --features mock && cd ..

# 6. Build rdma-core
cd dtld-ibverbs/rdma-core-55.0 && ./build.sh && cd ../..

# 7. Set up library paths
source ./scripts/setup-env.sh

# 8. Run the loopback example
cd examples && make && ./loopback 8192
```

> **WSL2 users**: Custom kernel headers are required before step 2.
> See the [Installation Guide](docs/en/installation.md) for details.

## Documentation

| Document | Description |
|---|---|
| [Driver Installation Guide](docs/en/installation.md) | Full installation, configuration, and troubleshooting |
| [RTL Simulation Guide](docs/en/rtl-simulation.md) | Setting up and running the RTL simulation environment |
| [Rust Driver Architecture](docs/en/introduction.md) | Internal architecture, modules, and design decisions |

## License

Licensed under the [GNU General Public License v2.0](LISENCE).
