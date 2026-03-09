# Open RDMA RTL 硬件仿真项目安装

> **注意**：本文档描述的是独立的 `open-rdma-rtl` 硬件仿真项目，与 `open-rdma-driver` 项目位于不同仓库。

## 安装步骤

### 1. 克隆项目

**在你希望放置项目的目录下运行**：
```bash
git clone https://github.com/open-rdma/open-rdma-rtl.git
cd open-rdma-rtl
git checkout dev
```

### 2. 安装 BSC

**在 open-rdma-rtl 项目根目录下运行**：
```bash
./setup.sh  # 安装 bsc 并设置环境变量到 ~/.bashrc 中
```



**注意**：确认 bsc 版本与 Ubuntu 版本匹配（如 Ubuntu 22.04 需要 bsc-2023.01-ubuntu-22.04）。

### 3. 安装仿真依赖

**系统依赖**：
```bash
sudo apt install iverilog verilator zlib1g-dev tcl8.6 libtcl8.6
```

**Python 依赖**：

安装conda (其他python环境也可以)：
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
注意目前的测试代码不兼容 cocotb 2.0

**说明**：
- 使用 `verilator`（非 `iverilog`）进行仿真
- `tcl8.6` 和 `libtcl8.6` 是 BSC backend 编译所需

### 4. 编译 Backend

**在 open-rdma-rtl 项目根目录下运行**：
```bash
cd test/cocotb && make verilog
```

生成的 Verilog 文件位于 `backend/verilog/` 目录。

### 5. 运行系统级测试

**单卡回环测试**（推荐用于快速验证）：

**在 open-rdma-rtl 项目根目录下运行**：
```bash
cd test/cocotb
make run_system_test_server_loopback
```

**双卡测试**（需要两个终端同时运行）：

**终端 1（在 open-rdma-rtl 项目根目录下运行）**：
```bash
# 启动服务器 1 (INST_ID=1)
cd test/cocotb
make run_system_test_server_1
```

**终端 2（在 open-rdma-rtl 项目根目录下运行）**：
```bash
# 启动服务器 2 (INST_ID=2)
cd test/cocotb
make run_system_test_server_2
```

测试日志保存在 `test/cocotb/log/` 目录（`.loopback`、`.1`、`.2` 后缀）。

## 与 Open RDMA Driver 配合使用

需要先编译 driver 为 sim 模式，同时完成 driver 的其他设置。

**在 open-rdma-driver 项目根目录下运行**：
```bash
cd dtld-ibverbs
cargo build --no-default-features --features sim
cd ..
```

Open RDMA Driver 的 `sim` 模式需要先启动本项目的仿真器：

### 单端测试（loopback）

**终端 1（在 open-rdma-rtl 项目根目录下运行）**：
```bash
# 启动硬件仿真器
cd test/cocotb
make run_system_test_server_loopback
```

**终端 2（在 open-rdma-driver 项目根目录下运行）**：
```bash
# 运行驱动测试
cd examples
make
RUST_LOG=debug ./loopback 8192
```

### 双端测试（send_recv）

**终端 1（在 open-rdma-rtl 项目根目录下运行）**：
```bash
# 启动硬件仿真器1
cd test/cocotb
make run_system_test_server_1
```

**终端 2（在 open-rdma-rtl 项目根目录下运行）**：
```bash
# 启动硬件仿真器2
cd test/cocotb
make run_system_test_server_2
```

**终端 3（在 open-rdma-driver 项目根目录下运行）**：
```bash
# 编译并运行驱动测试 server
cd examples
make
RUST_LOG=debug ./send_recv 8192
```

**终端 4（在 open-rdma-driver 项目根目录下运行）**：
```bash
# 运行驱动测试 client
cd examples
RUST_LOG=debug ./send_recv 8192 127.0.0.1
```