# WSL2 内核头文件准备指南

## 问题背景

在 WSL2 环境中编译 Linux 内核模块时，需要内核头文件和构建配置。然而，WSL2 默认不提供 `/lib/modules/$(uname -r)/build` 链接，也无法通过 `apt install linux-headers-$(uname -r)` 安装对应的内核头文件包。

因此，在 WSL2 中编译 Blue RDMA 驱动模块，需要手动克隆 WSL2 内核源码并编译准备。

或者可以使用 make KBUILD_MODPOST_WARN=1 选项来跳过警告，但有一定的风险

## 适用环境

- **仅限 WSL2 环境**（Windows Subsystem for Linux 2）
- 原生 Linux 系统可直接使用发行版提供的 `linux-headers` 包，无需此步骤

## 准备步骤

### 1. 确认当前内核版本

首先确认你的 WSL2 内核版本，以便克隆对应的源码：

```bash
uname -r
```

输出示例：
```
6.6.87.2-microsoft-standard-WSL2
```

### 2. 安装内核编译依赖

编译内核模块需要以下工具和库：

```bash
sudo apt update
sudo apt install build-essential flex bison dwarves libssl-dev libelf-dev cpio bc kmod
```

**依赖包说明**：
- `build-essential`: 包含 GCC 编译器、make 等基础工具
- `flex` & `bison`: 词法和语法分析器，内核构建系统需要
- `dwarves`: 提供 `pahole` 工具，用于生成 BTF 调试信息
- `libssl-dev`: OpenSSL 开发库，用于签名内核模块
- `libelf-dev`: ELF 文件处理库
- `cpio`: 归档工具，内核构建需要
- `bc`: 基础计算器，某些内核脚本需要
- `kmod`: 内核模块管理工具（`modprobe`, `insmod` 等）

### 3. 克隆 WSL2 内核源码

根据你的内核版本，克隆对应的 WSL2 内核源码分支：

```bash
# 对于 6.6.x 内核
git clone --depth 1 --branch linux-msft-wsl-6.6.y https://github.com/microsoft/WSL2-Linux-Kernel.git

# 对于其他版本，可查看 https://github.com/microsoft/WSL2-Linux-Kernel/branches
# 例如 6.1.x: --branch linux-msft-wsl-6.1.y
#      5.15.x: --branch linux-msft-wsl-5.15.y
```

进入源码目录：
```bash
cd WSL2-Linux-Kernel
```

### 4. 编译内核模块准备

设置内核源码路径环境变量（建议使用绝对路径）：

```bash
# 使用当前目录的绝对路径
KERNEL_SRC=$(pwd)
echo "内核源码路径: $KERNEL_SRC"
```

**准备内核构建环境**：

```bash
cd "$KERNEL_SRC"
sudo make KCONFIG_CONFIG=Microsoft/config-wsl modules_prepare -j$(nproc)
```

这一步会：
- 生成内核配置文件
- 准备内核头文件
- 生成模块构建所需的符号表

**编译内核模块**（可选但推荐）：

```bash
sudo make KCONFIG_CONFIG=Microsoft/config-wsl modules -j$(nproc)
```

这一步会编译所有内核模块，确保构建环境完整。如果时间较长，可以跳过此步骤，但某些情况下可能导致驱动编译问题。

### 5. 创建内核构建目录链接

创建符号链接，使模块构建系统能找到内核源码：

```bash
sudo ln -s "$KERNEL_SRC" /lib/modules/$(uname -r)/build
```

验证链接创建成功：
```bash
ls -l /lib/modules/$(uname -r)/build
# 应显示链接到内核源码目录
```

### 6. 验证内核头文件准备

检查关键文件是否存在：

```bash
# 检查内核配置文件
ls /lib/modules/$(uname -r)/build/.config

# 检查模块符号表
ls /lib/modules/$(uname -r)/build/Module.symvers

# 检查内核版本头文件
ls /lib/modules/$(uname -r)/build/include/linux/version.h
```

如果以上文件都存在，说明内核头文件准备成功。

## 常见问题

### Q1: 如何选择正确的内核源码分支？

根据 `uname -r` 输出的主版本号选择：
- 6.6.x → `linux-msft-wsl-6.6.y`
- 6.1.x → `linux-msft-wsl-6.1.y`
- 5.15.x → `linux-msft-wsl-5.15.y`

如果找不到对应分支，访问 [WSL2-Linux-Kernel Branches](https://github.com/microsoft/WSL2-Linux-Kernel/branches) 查看所有可用分支。

### Q2: make modules_prepare 失败怎么办？

**错误**：`No rule to make target 'modules_prepare'`

**原因**：可能未正确指定配置文件。

**解决**：
```bash
# 确保使用正确的配置文件路径
ls Microsoft/config-wsl  # 确认文件存在
sudo make KCONFIG_CONFIG=Microsoft/config-wsl modules_prepare
```

### Q3: 编译驱动时提示 "ERROR: Kernel configuration is invalid"

**原因**：内核配置文件不完整或未生成。

**解决**：
```bash
cd /lib/modules/$(uname -r)/build
sudo make KCONFIG_CONFIG=Microsoft/config-wsl oldconfig
sudo make KCONFIG_CONFIG=Microsoft/config-wsl prepare
```

### Q4: 编译驱动时提示缺少 BTF 信息

**错误**：`INFO: modpost: missing MODULE_LICENSE()` 或 BTF 相关错误

**解决**：使用 `KBUILD_MODPOST_WARN=1` 跳过警告，可能会有风险：
```bash
make KBUILD_MODPOST_WARN=1
```

### Q5: 链接已存在，如何重新创建？

如果 `/lib/modules/$(uname -r)/build` 链接已存在但指向错误位置：

```bash
# 删除旧链接
sudo rm /lib/modules/$(uname -r)/build

# 创建新链接
sudo ln -s /path/to/WSL2-Linux-Kernel /lib/modules/$(uname -r)/build
```

## 原生 Linux 系统的简化方法

如果你在原生 Linux 系统（非 WSL）上编译驱动，可以直接安装内核头文件包：

**Ubuntu/Debian**：
```bash
sudo apt install linux-headers-$(uname -r)
```

**Fedora/RHEL**：
```bash
sudo dnf install kernel-devel-$(uname -r)
```

**Arch Linux**：
```bash
sudo pacman -S linux-headers
```

## 总结

WSL2 内核头文件准备的核心步骤：

1. 安装编译依赖工具
2. 克隆对应版本的 WSL2 内核源码
3. 运行 `make modules_prepare` 和 `make modules`
4. 创建 `/lib/modules/$(uname -r)/build` 符号链接
5. 验证关键文件存在

完成这些步骤后，就可以正常编译 Blue RDMA 驱动了。
