# Open RDMA

[English](README.md) | [中文](README.zh-CN.md)

**Open RDMA** is an open-source RDMA (Remote Direct Memory Access) project covering the full hardware-to-driver stack — from RTL hardware logic to a Linux userspace driver — designed for GPU high-performance communication workloads.

Mainstream RDMA solutions (e.g. Mellanox/NVIDIA ConnectX series) are closed-source commercial products with no public hardware or driver implementations. Open RDMA aims to provide a fully open, inspectable, and customizable alternative, with end-to-end transparency from RTL to Linux driver. The driver is designed to keep business logic in userspace as much as possible, reducing kernel module dependencies for better portability and development velocity.

The project has completed prototype validation, supports most common `libibverbs` APIs, and can run RCCL-over-RDMA simulation tests as well as basic Send/Recv and RDMA Write scenarios. Active development is ongoing.

## Repositories

Open RDMA consists of two complementary repositories:

| Repository | Description |
|---|---|
| [open-rdma-rtl](https://github.com/open-rdma/open-rdma-rtl) | Hardware RTL implementation written in BSV, targeting Altera 400G, Xilinx 100G, Achronix, and other FPGA platforms — synthesizable for real hardware or usable in simulation |
| [open-rdma-driver](https://github.com/open-rdma/open-rdma-driver) (this repo) | Linux userspace driver written in Rust, implementing the standard `libibverbs` provider interface |

Together they implement a core subset of the RoCEv2 (RDMA over Converged Ethernet v2) protocol stack through hardware-software co-design, with RDMA semantics shared across both layers.

## Installation

- [Driver Installation Guide](docs/en/installation.md): environment requirements, build steps, and troubleshooting
- [RTL Simulation Guide](docs/en/rtl-simulation.md): setting up the RTL simulation environment and driver co-simulation (Sim mode)

## Architecture

```
User Application  (perftest, MPI, RCCL, ...)
       │ ibv_*() standard verbs calls
       ▼
libibverbs + C Provider          (rdma-core / dtld-ibverbs)
       │ dlopen("libbluerdma_rust.so")
       ▼
Rust Driver                      (rust-driver / libbluerdma_rust.so)
       │ PCIe MMIO / UDP RPC / software simulation
       ▼
Hardware layer (one of three)
  ├── Physical FPGA NIC           (open-rdma-rtl, synthesized)
  ├── RTL Simulator               (open-rdma-rtl, software simulation)
  └── Mock                        (pure software stub, no hardware required)
```

The driver is composed of three layers:

| Component | Location | Role |
|---|---|---|
| Kernel module (`bluerdma.ko`) | `kernel-driver/` | Registers the IB device with the Linux RDMA subsystem and exposes sysfs/uverbs interfaces (stub — contains no business logic) |
| C Provider | `dtld-ibverbs/rdma-core-55.0/providers/bluerdma/` | libibverbs provider glue layer — loads the Rust driver via `dlopen` and forwards calls |
| Rust driver (`libbluerdma_rust.so`) | `rust-driver/` | **Core implementation**: verbs business logic, DMA, ring buffers, and background worker threads |

## Operation Modes

| Mode | Feature Flag | Description |
|---|---|---|
| **Mock** | `--features mock` | Stub implementation requiring no hardware — used to validate that upper-layer application API calls are correct |
| **Sim** | `--features sim` | Co-simulation with the RTL simulator over RPC — used for hardware logic validation |
| **Hardware** | `--features hw` | Connects to a physical PCIe RDMA device (experimental) |

## Documentation

| Document | Description |
|---|---|
| [Driver Installation Guide](docs/en/installation.md) | Full installation, configuration, and troubleshooting |
| [RTL Simulation Guide](docs/en/rtl-simulation.md) | RTL simulation environment setup and driver co-simulation (Sim mode) |
| [Rust Driver Architecture](docs/en/introduction.md) | Internal architecture, module descriptions, and design decisions |
