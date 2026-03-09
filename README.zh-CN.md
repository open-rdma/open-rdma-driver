# Open RDMA

[English](README.md) | [中文](README.zh-CN.md)

**Open RDMA** 是一个开源 RDMA（远程直接内存访问）项目，覆盖从硬件 RTL 到用户态驱动的完整软硬件栈，面向 GPU 高性能通信场景设计。

当前主流 RDMA 方案（如 Mellanox/NVIDIA ConnectX 系列）均为闭源商业产品，硬件实现与驱动均不对外开放。Open RDMA 致力于提供一个完全开放、可研究、可定制的替代实现，从 RTL 硬件逻辑到 Linux 驱动全链路透明可见。在驱动设计上，项目尽可能将业务逻辑实现在用户态，减少对内核模块的依赖，以获得更好的可移植性与开发迭代效率。

项目目前已完成原型验证，支持大多数常用 libibverbs 接口，可运行 RCCL over RDMA 仿真测试及 Send/Recv、RDMA Write 等基础场景，并持续迭代中。

## 项目组成

Open RDMA 由两个相互配合的仓库构成：

| 仓库 | 说明 |
|---|---|
| [open-rdma-rtl](https://github.com/open-rdma/open-rdma-rtl) | 用 BSV 编写的硬件 RTL 实现，支持 Altera 400G、Xilinx 100G、Achronix 等 FPGA 平台，可综合上板或用于仿真验证 |
| [open-rdma-driver](https://github.com/open-rdma/open-rdma-driver)（本仓库）| 用 Rust 编写的 Linux 用户态驱动，对接标准 libibverbs 接口 |

两者通过软硬件协同共同实现 RoCEv2（RDMA over Converged Ethernet v2）协议栈的核心子集，RDMA 语义由软件与硬件共同承载。

## 安装

- [驱动安装指南](docs/zh-CN/installation.md)：环境要求、编译步骤及常见问题排查
- [RTL 仿真指南](docs/zh-CN/rtl-simulation.md)：RTL 仿真环境搭建与驱动联调（Sim 模式）

## 系统架构

```
用户应用程序  (perftest, MPI, RCCL, ...)
       │ ibv_*() 标准 verbs 调用
       ▼
libibverbs + C Provider          (rdma-core / dtld-ibverbs)
       │ dlopen("libbluerdma_rust.so")
       ▼
Rust 驱动                        (rust-driver / libbluerdma_rust.so)
       │ PCIe MMIO / UDP RPC / 软件模拟
       ▼
硬件层（三选一）
  ├── 物理 FPGA 网卡              (open-rdma-rtl，综合上板)
  ├── RTL 仿真器                  (open-rdma-rtl，软件仿真)
  └── Mock                        (纯软件桩，无硬件依赖)
```

驱动由三个层次组成：

| 组件 | 位置 | 职责 |
|---|---|---|
| 内核模块 (`bluerdma.ko`) | `kernel-driver/` | 向 Linux RDMA 子系统注册设备，提供 sysfs/uverbs 接口（桩实现，不含业务逻辑）|
| C Provider | `dtld-ibverbs/rdma-core-55.0/providers/bluerdma/` | libibverbs provider 胶水层，通过 `dlopen` 加载 Rust 驱动并转发调用 |
| Rust 驱动 (`libbluerdma_rust.so`) | `rust-driver/` | **核心实现**：verbs 业务逻辑、DMA、环形缓冲区、后台工作线程 |

## 运行模式

驱动支持三种运行模式：

| 模式 | Feature 标志 | 说明 |
|---|---|---|
| **Mock** | `--features mock` | 接口桩实现，无需任何硬件，用于验证上层应用的接口调用是否正确 |
| **Sim** | `--features sim` | 通过 RPC 与 RTL 仿真器联调，用于硬件逻辑验证 |
| **Hardware** | `--features hw` | 对接物理 PCIe RDMA 设备（实验性）|

## 文档

| 文档 | 说明 |
|---|---|
| [驱动安装指南](docs/zh-CN/installation.md) | 完整的安装、配置与常见问题排查 |
| [RTL 仿真指南](docs/zh-CN/rtl-simulation.md) | RTL 仿真环境搭建与驱动联调（Sim 模式）|
| [Rust 驱动架构](docs/zh-CN/introduction.md) | 内部架构、模块说明与设计决策 |

## 参与贡献

欢迎任何形式的贡献！如果您有问题、建议或想参与开发，欢迎添加**达坦小助手**微信，加入我们的开发者社区：

<div align="center">
  <img src="docs/images/wechat-qr.png" alt="达坦小助手微信二维码" width="200"/>
  <p>扫码添加达坦小助手</p>
</div>
