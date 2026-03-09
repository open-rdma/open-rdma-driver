# WSL2 Kernel Headers Preparation Guide

## Background

When compiling Linux kernel modules in a WSL2 environment, kernel headers and build configuration are required. However, WSL2 does not provide the `/lib/modules/$(uname -r)/build` symlink by default, and the corresponding kernel headers package cannot be installed via `apt install linux-headers-$(uname -r)`.

Therefore, compiling the Blue RDMA driver module in WSL2 requires manually cloning the WSL2 kernel source and preparing it for building.

Alternatively, you can use the `make KBUILD_MODPOST_WARN=1` option to skip warnings, but this carries some risk.

## Applicability

- **WSL2 only** (Windows Subsystem for Linux 2)
- Native Linux systems can use the distro-provided `linux-headers` package directly; this procedure is not needed.

## Preparation Steps

### 1. Confirm the Current Kernel Version

First, identify your WSL2 kernel version so that you can clone the matching source:

```bash
uname -r
```

Example output:
```
6.6.87.2-microsoft-standard-WSL2
```

### 2. Install Kernel Build Dependencies

The following tools and libraries are required to build kernel modules:

```bash
sudo apt update
sudo apt install build-essential flex bison dwarves libssl-dev libelf-dev cpio bc kmod
```

**Package descriptions**:
- `build-essential`: Contains the GCC compiler, make, and other essential tools
- `flex` & `bison`: Lexer and parser generators required by the kernel build system
- `dwarves`: Provides the `pahole` tool for generating BTF debug information
- `libssl-dev`: OpenSSL development library for signing kernel modules
- `libelf-dev`: ELF file processing library
- `cpio`: Archive tool required by the kernel build
- `bc`: Basic calculator needed by certain kernel scripts
- `kmod`: Kernel module management tools (`modprobe`, `insmod`, etc.)

### 3. Clone the WSL2 Kernel Source

Based on your kernel version, clone the corresponding WSL2 kernel source branch:

```bash
# For 6.6.x kernels
git clone --depth 1 --branch linux-msft-wsl-6.6.y https://github.com/microsoft/WSL2-Linux-Kernel.git

# For other versions, see https://github.com/microsoft/WSL2-Linux-Kernel/branches
# e.g. 6.1.x: --branch linux-msft-wsl-6.1.y
#      5.15.x: --branch linux-msft-wsl-5.15.y
```

Enter the source directory:
```bash
cd WSL2-Linux-Kernel
```

### 4. Prepare the Kernel Module Build Environment

Set the kernel source path environment variable (an absolute path is recommended):

```bash
# Use the absolute path of the current directory
KERNEL_SRC=$(pwd)
echo "Kernel source path: $KERNEL_SRC"
```

**Prepare the kernel build environment**:

```bash
cd "$KERNEL_SRC"
sudo make KCONFIG_CONFIG=Microsoft/config-wsl modules_prepare -j$(nproc)
```

This step will:
- Generate the kernel configuration file
- Prepare kernel headers
- Generate the symbol table required for module builds

**Build kernel modules** (optional but recommended):

```bash
sudo make KCONFIG_CONFIG=Microsoft/config-wsl modules -j$(nproc)
```

This step builds all kernel modules to ensure the build environment is complete. If it takes too long, you can skip it, but doing so may cause problems when compiling the driver.

### 5. Create the Kernel Build Directory Symlink

Create a symlink so the module build system can find the kernel source:

```bash
sudo ln -s "$KERNEL_SRC" /lib/modules/$(uname -r)/build
```

Verify the symlink was created successfully:
```bash
ls -l /lib/modules/$(uname -r)/build
# Should display a link pointing to the kernel source directory
```

### 6. Verify the Kernel Headers Are Ready

Check that the key files exist:

```bash
# Check the kernel configuration file
ls /lib/modules/$(uname -r)/build/.config

# Check the module symbol table
ls /lib/modules/$(uname -r)/build/Module.symvers

# Check the kernel version header
ls /lib/modules/$(uname -r)/build/include/linux/version.h
```

If all of the above files exist, the kernel headers have been prepared successfully.

## Frequently Asked Questions

### Q1: How do I choose the correct kernel source branch?

Select based on the major version number shown by `uname -r`:
- 6.6.x → `linux-msft-wsl-6.6.y`
- 6.1.x → `linux-msft-wsl-6.1.y`
- 5.15.x → `linux-msft-wsl-5.15.y`

If you cannot find a matching branch, visit [WSL2-Linux-Kernel Branches](https://github.com/microsoft/WSL2-Linux-Kernel/branches) to see all available branches.

### Q2: What if `make modules_prepare` fails?

**Error**: `No rule to make target 'modules_prepare'`

**Cause**: The configuration file may not have been specified correctly.

**Solution**:
```bash
# Ensure the correct configuration file path is used
ls Microsoft/config-wsl  # Confirm the file exists
sudo make KCONFIG_CONFIG=Microsoft/config-wsl modules_prepare
```

### Q3: "ERROR: Kernel configuration is invalid" when building the driver

**Cause**: The kernel configuration file is incomplete or was not generated.

**Solution**:
```bash
cd /lib/modules/$(uname -r)/build
sudo make KCONFIG_CONFIG=Microsoft/config-wsl oldconfig
sudo make KCONFIG_CONFIG=Microsoft/config-wsl prepare
```

### Q4: Missing BTF information when building the driver

**Error**: `INFO: modpost: missing MODULE_LICENSE()` or BTF-related errors

**Solution**: Use `KBUILD_MODPOST_WARN=1` to skip warnings (some risk involved):
```bash
make KBUILD_MODPOST_WARN=1
```

### Q5: The symlink already exists — how do I recreate it?

If `/lib/modules/$(uname -r)/build` already exists but points to the wrong location:

```bash
# Remove the old symlink
sudo rm /lib/modules/$(uname -r)/build

# Create a new symlink
sudo ln -s /path/to/WSL2-Linux-Kernel /lib/modules/$(uname -r)/build
```

## Simplified Method for Native Linux Systems

If you are building the driver on a native Linux system (not WSL), you can install the kernel headers package directly:

**Ubuntu/Debian**:
```bash
sudo apt install linux-headers-$(uname -r)
```

**Fedora/RHEL**:
```bash
sudo dnf install kernel-devel-$(uname -r)
```

**Arch Linux**:
```bash
sudo pacman -S linux-headers
```

## Summary

Core steps for preparing WSL2 kernel headers:

1. Install build dependency tools
2. Clone the WSL2 kernel source for the matching version
3. Run `make modules_prepare` and `make modules`
4. Create the `/lib/modules/$(uname -r)/build` symlink
5. Verify that the key files exist

After completing these steps, you can build the Blue RDMA driver normally.
