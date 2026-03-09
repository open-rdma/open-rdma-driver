

在wsl中配置环境

### 尝试编译rust-driver

在 dtld-ibverbs 文件夹下
使用命令： cargo build --no-default-features --features sim

```log
   Compiling pnet_datalink v0.35.0
error: failed to run custom build command for `ibverbs-sys v0.3.1+55.0 (https://github.com/bsbds/rust-ibverbs.git?rev=ea06bdc#ea06bdc4)`

Caused by:
  process didn't exit successfully: `/home/peng/projects/rdma_all/blue-rdma-driver/rust-driver/target/debug/build/ibverbs-sys-b7e3cdb5ebd320a7/build-script-build` (exit status: 101)
  --- stdout
  cargo:include=/home/peng/.cargo/git/checkouts/rust-ibverbs-74ab10162b145359/ea06bdc/ibverbs-sys/vendor/rdma-core/build/include
  cargo:rustc-link-search=native=/home/peng/.cargo/git/checkouts/rust-ibverbs-74ab10162b145359/ea06bdc/ibverbs-sys/vendor/rdma-core/build/lib
  cargo:rustc-link-lib=ibverbs
  CMAKE_TOOLCHAIN_FILE_x86_64-unknown-linux-gnu = None
  CMAKE_TOOLCHAIN_FILE_x86_64_unknown_linux_gnu = None
  HOST_CMAKE_TOOLCHAIN_FILE = None
  CMAKE_TOOLCHAIN_FILE = None
  CMAKE_GENERATOR_x86_64-unknown-linux-gnu = None
  CMAKE_GENERATOR_x86_64_unknown_linux_gnu = None
  HOST_CMAKE_GENERATOR = None
  CMAKE_GENERATOR = None
  CMAKE_PREFIX_PATH_x86_64-unknown-linux-gnu = None
  CMAKE_PREFIX_PATH_x86_64_unknown_linux_gnu = None
  HOST_CMAKE_PREFIX_PATH = None
  CMAKE_PREFIX_PATH = None
  CMAKE_x86_64-unknown-linux-gnu = None
  CMAKE_x86_64_unknown_linux_gnu = None
  HOST_CMAKE = None
  CMAKE = None
  running: cd "/home/peng/projects/rdma_all/blue-rdma-driver/rust-driver/target/debug/build/ibverbs-sys-e2baf84cd41f263d/out/build" && CMAKE_PREFIX_PATH="" LC_ALL="C" "cmake" "/home/peng/.cargo/git/checkouts/rust-ibverbs-74ab10162b145359/ea06bdc/ibverbs-sys/vendor/rdma-core" "-DNO_MAN_PAGES=1" "-DCMAKE_INSTALL_PREFIX=/usr" "-DCMAKE_C_FLAGS= -ffunction-sections -fdata-sections -fPIC -m64" "-DCMAKE_C_COMPILER=/usr/bin/cc" "-DCMAKE_CXX_FLAGS= -ffunction-sections -fdata-sections -fPIC -m64" "-DCMAKE_CXX_COMPILER=/usr/bin/c++" "-DCMAKE_ASM_FLAGS= -ffunction-sections -fdata-sections -fPIC -m64" "-DCMAKE_ASM_COMPILER=/usr/bin/cc" "-DCMAKE_BUILD_TYPE=Debug"

  --- stderr
  run cmake

  thread 'main' panicked at /home/peng/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/cmake-0.1.52/src/lib.rs:1115:5:

  failed to execute command: No such file or directory (os error 2)
  is `cmake` not installed?

  build script failed, must exit now
  note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace
warning: build failed, waiting for other jobs to finish...
```

需要安装cmake


出现错误 （并没有代表性应该）
```
peng@DESKTOP-M211L3D:~/projects/rdma_all/blue-rdma-driver/rust-driver$ pkg-config --version
bash: /mnt/c/Strawberry/perl/bin/pkg-config: cannot execute: required file not found
```

之后出现错误
```log
error: failed to run custom build command for `ibverbs-sys v0.3.1+55.0 (https://github.com/bsbds/rust-ibverbs.git?rev=ea06bdc#ea06bdc4)`
...

  --- stderr
  run cmake
  CMake Error at /usr/share/cmake-3.28/Modules/FindPkgConfig.cmake:619 (message):
    The following required packages were not found:

     - libnl-3.0
     - libnl-route-3.0
```

sudo apt install -y libnl-3-dev libnl-route-3-dev

需要安装这两个库


之后缺少 libclang 用于 bindgen库


之后 
```
  cargo:root=/home/peng/projects/rdma_all/blue-rdma-driver/rust-driver/target/debug/build/ibverbs-sys-e2baf84cd41f263d/out

  --- stderr
  run cmake
  run bindgen
  vendor/rdma-core/libibverbs/verbs.h:48:10: fatal error: 'infiniband/verbs_api.h' file not found

  thread 'main' panicked at /home/peng/.cargo/git/checkouts/rust-ibverbs-74ab10162b145359/ea06bdc/ibverbs-sys/build.rs:77:10:
  Unable to generate bindings: ClangDiagnostic("vendor/rdma-core/libibverbs/verbs.h:48:10: fatal error: 'infiniband/verbs_api.h' file not found\n")
  note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace
warning: build failed, waiting for other jobs to finish...
```
是由于需要，readme里有提示
```bash
sudo apt install libibverbs-dev
```
之后编译成功



之后编译特制 rdma-core-55.0
```bash
cd dtld-ibverbs/rdma-core-55.0
# uncomment following line to generate compile_commands.json for debug
# export EXTRA_CMAKE_FLAGS=-DCMAKE_EXPORT_COMPILE_COMMANDS=1
./build.sh
cd -
```
成功

之后创建大页


### 尝试直接 make


```log
peng@DESKTOP-M211L3D:~/projects/rdma_all/blue-rdma-driver$ make
mkdir -p build
make -C /lib/modules/6.6.87.2-microsoft-standard-WSL2/build M=/home/peng/projects/rdma_all/blue-rdma-driver/kernel-driver modules
make[1]: *** /lib/modules/6.6.87.2-microsoft-standard-WSL2/build: No such file or directory.  Stop.
make: *** [Makefile:38: bluerdma] Error 2
```

### 运行仿真模式的 loopback 示例

在编译好 rust-driver (sim feature) 和 rdma-core-55.0，分配大页后，运行 loopback 示例：

```bash
cd examples
make
export LD_LIBRARY_PATH=../dtld-ibverbs/target/debug:../dtld-ibverbs/rdma-core-55.0/build/lib
RUST_LOG=debug ./loopback 8192
```

#### 问题：Found 0 RDMA devices

**错误现象**：
```log
before ibv_get_device_list
Found 0 RDMA devices
No RDMA devices found!
```

**问题分析**：

通过分析代码流程，发现了根本原因：

1. **libibverbs 设备发现机制**：
   - `ibv_get_device_list()` 通过扫描 `/sys/class/infiniband_verbs/` 目录来发现 RDMA 设备
   - 代码路径：`libibverbs/device.c:73` → `libibverbs/init.c:560 find_sysfs_devs()` → 扫描 sysfs
   - 只有在 sysfs 中存在设备节点（如 uverbs0, uverbs1）后，才会调用 bluerdma provider
   - bluerdma provider 再加载 `libbluerdma_rust.so` 并调用 Rust 代码

2. **完整的调用链**：
   ```
   loopback.c: ibv_get_device_list()
     ↓
   libibverbs/device.c:73: ibverbs_get_device_list()
     ↓
   libibverbs/init.c:560: find_sysfs_devs()
     扫描 /sys/class/infiniband_verbs/uverbs*
     ↓
   libibverbs/init.c:541: try_drivers() 匹配驱动
     ↓
   providers/bluerdma/bluerdma.c:393: bluerdma_device_alloc()
     ↓
   bluerdma.c:407: dlopen("libbluerdma_rust.so")
     ↓
   bluerdma.c:422: bluerdma_init() [Rust]
     ↓
   bluerdma.c:359: bluerdma_new("uverbs0") [Rust]
     ↓
   rust-driver/src/verbs/core.rs:81: 连接 UDP 127.0.0.1:7701
   ```

3. **当前状态**：
   - ✓ achronix-400g 仿真器正在运行，监听 UDP 7701 端口
   - ✓ Rust 驱动代码期望连接 7701 和 7702 端口（对应 uverbs0 和 uverbs1）
   - ✗ **但 `/sys/class/infiniband_verbs/` 目录为空**
   - ✗ 没有 uverbs0、uverbs1 设备节点

4. **根本原因**：
   - **设备发现完全依赖 sysfs**，Rust 代码不负责创建虚拟设备
   - 硬件模式下，内核模块会创建 sysfs 设备节点
   - 仿真模式下，仍然需要某种方式创建 sysfs 设备节点
   - 但当前 WSL 环境无法编译内核模块（缺少内核头文件）

5. **设备通信机制**：
   - 仿真模式使用 **UDP RPC** 通信（而非 PCIe 或共享内存）
   - 协议：JSON 序列化的 `CsrAccessRpcMessage` over UDP
   - EmulatedDevice 在 `rust-driver/src/csr/emulated/mod.rs` 中实现
   - 读写寄存器通过 UDP 发送请求到仿真器，接收响应

**尝试的解决方案**：

需要安装ko 模块

---

## 解决内核模块编译问题（WSL2 环境）

### 问题描述

尝试编译内核模块时报错：
```
make -C /lib/modules/6.6.87.2-microsoft-standard-WSL2/build M=/home/peng/projects/rdma_all/blue-rdma-driver/kernel-driver modules
make[1]: *** /lib/modules/6.6.87.2-microsoft-standard-WSL2/build: No such file or directory.  Stop.
```

### 问题根源

1. **WSL2 使用 Microsoft 定制内核**，版本为 `6.6.87.2-microsoft-standard-WSL2`
2. **apt 安装的内核头文件不匹配**：
   - `sudo apt install linux-headers-generic` 安装的是 Ubuntu 6.8.0-85 内核头文件
   - 与运行的 WSL2 内核版本不兼容
3. **缺少内核构建目录**：`/lib/modules/$(uname -r)/build` 符号链接不存在

### 解决步骤

#### 1. 克隆 WSL2 内核源码

```bash
sudo apt install git rsync
mkdir ~/src
cd ~/src
git clone https://github.com/microsoft/WSL2-Linux-Kernel --depth 1
cd WSL2-Linux-Kernel
```

克隆的版本：`linux-msft-wsl-6.6.87.2`（与运行内核完全匹配）

#### 2. 安装构建依赖

```bash
sudo apt install -y build-essential flex bison dwarves libssl-dev libelf-dev cpio bc kmod
```

各依赖的作用：
- `build-essential`: GCC 编译器和基本构建工具
- `flex` 和 `bison`: 内核配置解析器生成工具
- `dwarves`: 提供 `pahole` 工具，用于生成 BTF（BPF Type Format）
- `libssl-dev`: 内核模块签名
- `libelf-dev`: 处理 ELF 格式文件
- `cpio`: 打包 initramfs
- `bc`: 内核构建计算工具
- `kmod`: 内核模块管理工具

#### 3. 配置和准备内核构建环境

```bash
cd ~/src/WSL2-Linux-Kernel

# 使用 Microsoft 提供的 WSL2 专用配置
sudo make KCONFIG_CONFIG=Microsoft/config-wsl modules_prepare -j$(nproc)
```

输出示例：
```
DESCEND objtool
DESCEND bpf/resolve_btfids
INSTALL libsubcmd_headers
CALL    scripts/checksyscalls.sh
```

**注意**：使用 `KCONFIG_CONFIG=Microsoft/config-wsl` 时，不会生成 `.config` 文件，内核构建系统直接从 `Microsoft/config-wsl` 读取配置。

#### 4. 生成 Module.symvers（关键步骤）

**问题**：`make modules_prepare` 只准备构建环境，不生成 `Module.symvers`，导致外部模块编译时报符号未定义错误：
```
ERROR: modpost: "unregister_netdev" [.../bluerdma.ko] undefined!
ERROR: modpost: "kfree" [.../bluerdma.ko] undefined!
...（共 46 个未解析符号）
```

**解决方案**：编译完整的内核模块以生成 `Module.symvers`

```bash
cd ~/src/WSL2-Linux-Kernel
sudo make KCONFIG_CONFIG=Microsoft/config-wsl modules -j$(nproc)
```

**预计耗时**：20-60 分钟（取决于 CPU 核心数）
**磁盘占用**：约 15-20 GB

#### 5. 创建 build 符号链接

```bash
sudo ln -s /home/peng/src/WSL2-Linux-Kernel /lib/modules/6.6.87.2-microsoft-standard-WSL2/build
```

验证：
```bash
ls -la /lib/modules/$(uname -r)/build
# 输出: lrwxrwxrwx 1 root root 35 ... /lib/modules/6.6.87.2-microsoft-standard-WSL2/build -> /home/peng/src/WSL2-Linux-Kernel
```

#### 6. 验证编译环境（待完成）

```bash
cd ~/projects/rdma_all/blue-rdma-driver
make clean
make
```

预期成功生成：
- `build/bluerdma.ko`
- `build/u-dma-buf.ko`

### 当前状态

✅ WSL2 内核源码已克隆（版本 6.6.87.2）
✅ 构建依赖已安装
✅ 内核构建环境已准备（`make modules_prepare` 完成）
✅ build 符号链接已创建
⏳ **正在编译内核模块以生成 Module.symvers**（`make modules` 进行中）
⏸️ 验证成功

### 技术说明

**为什么需要 Module.symvers？**
- `Module.symvers` 记录内核导出的所有符号（函数、变量）及其 CRC 校验和
- 外部模块编译时，`modpost` 工具需要此文件来解析依赖关系
- 确保模块与内核的 ABI（应用二进制接口）兼容

**为什么空的 Module.symvers 不够？**
- 尝试了 `touch Module.symvers` 创建空文件，但编译失败
- `modpost` 必须查找每个符号的 CRC 来验证版本兼容性
- 空文件意味着没有符号信息，无法解析 `kfree`、`unregister_netdev` 等核心内核函数

**WSL2 内核模块编译的特殊性**：
- WSL2 不预装内核头文件和 Module.symvers
- 必须手动构建完整的内核构建树
- 不同于标准 Linux 发行版（通常只需 `apt install linux-headers-$(uname -r)`）



或者使用 
```shell
make KBUILD_MODPOST_WARN=1
```
直接跳过验证
出现
```log
Skipping BTF generation for /home/peng/projects/rdma_all/blue-rdma-driver/kernel-driver/bluerdma.ko due to unavailability of vmlinux
make[1]: Leaving directory '/home/peng/src/WSL2-Linux-Kernel'
cp kernel-driver/bluerdma.ko build/
cd third_party/udmabuf && make KERNEL_SRC_DIR=/lib/modules/6.6.87.2-microsoft-standard-WSL2/build all
make[1]: Entering directory '/home/peng/projects/rdma_all/blue-rdma-driver/third_party/udmabuf'
make[1]: *** No rule to make target 'all'.  Stop.
make[1]: Leaving directory '/home/peng/projects/rdma_all/blue-rdma-driver/third_party/udmabuf'
make: *** [Makefile:43: udmabuf] Error 2
```
需要执行 git submodule update --init --recursive 


需要
```
sudo ip addr add 17.34.51.10/24 dev blue0
sudo ip addr add 17.34.51.11/24 dev blue1
```

之后
