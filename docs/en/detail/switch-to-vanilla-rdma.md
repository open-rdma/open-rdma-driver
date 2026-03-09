# Switching to Mainline Linux RDMA — Quick Operations Guide

**Date**: 2025-11-14
**Goal**: Make bluerdma run under the standard Linux RDMA subsystem
**Status**: ✅ Verified

---

## Quick Steps

### 1. Unload OFED Modules

```bash
# Stop the openibd service (prevents automatic reload)
sudo /etc/init.d/openibd stop

# Unload all RDMA modules (including the OFED-specific mlx_compat)
sudo modprobe -r mlx_compat
sudo modprobe -r rdma_ucm rdma_cm iw_cm ib_ipoib ib_umad ib_uverbs ib_cm mlx5_ib mlx5_core ib_core

# Verify the unload was successful
cat /proc/modules | grep ib_core
# Should show no output
```

### 2. Load the Mainline ib_core Using insmod

```bash
# ✅ Verified: load the mainline version directly using insmod
sudo insmod /lib/modules/$(uname -r)/kernel/drivers/infiniband/core/ib_core.ko.zst
# This module exposes uverbs so userspace ibverbs can discover the device
sudo insmod /lib/modules/$(uname -r)/kernel/drivers/infiniband/core/ib_uverbs.ko.zst
```

**Key point**: `insmod` must be used instead of `modprobe`, because modprobe will prioritize the OFED version in `updates/dkms/`.

### 3. Verify the Mainline Version Is Loaded

```bash
# Check module status (key: should have NO (OE) tag)
cat /proc/modules | grep ib_core
# Expected output: ib_core 401408 0 - Live 0x0000000000000000
#                  ↑ Note: no (OE) tag

# Check for mlx_compat (mainline version should not have it)
lsmod | grep mlx_compat
# Should show no output
```

**Comparison table**:
| Check | OFED version | Mainline version |
|-------|-------------|-----------------|
| `/proc/modules` tag | `(OE)` | no tag |
| `mlx_compat` module | ✅ present | ❌ absent |
| Module path | `updates/dkms/` | `kernel/drivers/` |

---

## Why Is This Necessary?

### Root Cause

| Component | Mellanox OFED | Standard Linux RDMA |
|-----------|--------------|---------------------|
| **Design goal** | Optimized for Mellanox hardware | General RDMA framework |
| **Hardware assumptions** | Expects real PCI device | Supports software drivers |
| **bluerdma compatibility** | ❌ Incompatible | ✅ Compatible |

bluerdma is a **software RDMA driver** (no hardware dependency) and must use the standard Linux RDMA subsystem.

### Key Technical Points

1. **modprobe vs. insmod**:
   - `modprobe ib_core` → prioritizes loading the OFED version from `updates/dkms/`
   - `insmod /path/to/ib_core.ko.zst` → loads directly from the specified path

2. **Module tag meanings**:
   - `(OE)` = Out-of-tree + Unsigned (external modules such as OFED)
   - No tag = mainline kernel module

3. **mlx_compat**:
   - OFED-specific compatibility shim module
   - Does not exist in the mainline kernel
   - Its presence directly indicates that the OFED version is running

---

## Related Documents

- [OFED Symbol Version Fix](./ofed-symbol-version-fix.md) - First-stage issue
- [OFED RoCE Registration Issue](./ofed-roce-registration-issue.md) - Second-stage issue (architectural incompatibility)

---

**Document version**: 1.0
**Last updated**: 2025-11-14
**Verified on**: Ubuntu 6.8.0-58-generic ✅
