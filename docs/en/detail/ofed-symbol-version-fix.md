# BlueRDMA Module Symbol Version Mismatch with Mellanox OFED — Fix

**Date**: 2025-11-12
**Problem**: `bluerdma.ko` fails to load with "Invalid parameters"
**Status**: ✅ Resolved

---

## Symptoms

### Error Message

```bash
$ sudo insmod build/bluerdma.ko
insmod: ERROR: could not insert module build/bluerdma.ko: Invalid parameters
```

### Kernel Log (dmesg)

```
bluerdma: disagrees about version of symbol _ib_alloc_device
bluerdma: Unknown symbol _ib_alloc_device (err -22)
bluerdma: disagrees about version of symbol ib_unregister_device
bluerdma: Unknown symbol ib_unregister_device (err -22)
bluerdma: disagrees about version of symbol ib_register_device
bluerdma: Unknown symbol ib_register_device (err -22)
bluerdma: disagrees about version of symbol ib_device_set_netdev
bluerdma: Unknown symbol ib_device_set_netdev (err -22)
bluerdma: disagrees about version of symbol ib_query_port
bluerdma: Unknown symbol ib_query_port (err -22)
bluerdma: disagrees about version of symbol ib_dealloc_device
bluerdma: Unknown symbol ib_dealloc_device (err -22)
bluerdma: disagrees about version of symbol ib_get_eth_speed
bluerdma: Unknown symbol ib_get_eth_speed (err -22)
bluerdma: disagrees about version of symbol ib_set_device_ops
bluerdma: Unknown symbol ib_set_device_ops (err -22)
```

---

## Root Cause Analysis

### 1. Symbol Versioning Mechanism (CONFIG_MODVERSIONS)

The Linux kernel uses **CRC32 checksums** to ensure binary interface (ABI) compatibility between modules:

- Every exported symbol has a CRC32 value computed from its definition (function signature, struct layout, etc.)
- When loading a module, the kernel verifies that the symbol CRCs expected by the module match those provided by the kernel
- A CRC mismatch indicates ABI incompatibility; loading fails with `-EINVAL` (err -22)

### 2. Mellanox OFED vs. Mainline Kernel InfiniBand

The system has **Mellanox OFED (mlnx-ofed-kernel-dkms 24.10.OFED.24.10.1.1.4.1)** installed, which provides enhanced InfiniBand/RDMA drivers:

**Module loading priority**:
```
1. /lib/modules/6.8.0-58-generic/updates/dkms/    ← OFED modules (highest priority)
2. /lib/modules/6.8.0-58-generic/updates/
3. /lib/modules/6.8.0-58-generic/kernel/          ← Mainline kernel modules
```

**Symbol version differences**:

| Symbol | Mainline CRC | OFED CRC | CRC used when building bluerdma |
|--------|-------------|----------|---------------------------------|
| `ib_register_device` | `0x42ba7635` | `0xb78db345` | `0x42ba7635` ❌ |
| `ib_unregister_device` | `0x7d5ab2b1` | `0x429631a6` | `0x7d5ab2b1` ❌ |
| `ib_device_set_netdev` | `0x5f3d9143` | `0xe01d532e` | `0x5f3d9143` ❌ |
| `ib_query_port` | `0x8c5a5c42` | `0x7996ccf7` | `0x8c5a5c42` ❌ |

**Why are the CRCs different?**
- Mellanox OFED modified function signatures or related structures to support vendor-specific features
- OFED uses a different backport compatibility layer
- OFED includes performance and feature optimizations

### 3. Issue in the bluerdma Build Process

**Compilation stage (*.c → *.o)**:
```bash
ccflags-y += -I/usr/src/mlnx-ofed-kernel-24.10.OFED.24.10.1.1.4.1/include
```
✅ **Correct**: source code is compiled with OFED headers

**Linking stage (modpost)**:
```bash
# Uses the kernel's Module.symvers by default
/usr/src/linux-headers-6.8.0-58-generic/Module.symvers
```
❌ **Wrong**: the generated `bluerdma.mod.c` contains symbol CRCs from the **mainline kernel version**

**Runtime loading**:
```
bluerdma.ko expects symbol CRC: 0x42ba7635 (mainline version)
Actually loaded ib_core.ko provides: 0xb78db345 (OFED version)
```
❌ **Mismatch**: loading fails!

---

## Solution

### Core Approach

Use the **KBUILD_EXTRA_SYMBOLS** mechanism to specify the OFED `Module.symvers` file at build time, ensuring the modpost tool uses the correct symbol CRC values.

### Modified Files

#### 1. Root-level Makefile

**File**: `/home/peng/projects/rdma_all/blue-rdma-driver/Makefile`

**Change**:

```diff
 KERNEL_SRC ?= /lib/modules/$(shell uname -r)/build

+# OFED Module.symvers for symbol version matching
+ARCH := $(shell uname -m)
+KVER := $(shell uname -r)
+OFED_SYMVERS := /usr/src/ofa_kernel/$(ARCH)/$(KVER)/Module.symvers
+
 BUILD_DIR := build
 BLUERDMA_SRC_DIR := kernel-driver
 UDMABUF_SRC_DIR := third_party/udmabuf
```

```diff
 bluerdma: $(BUILD_DIR)
-	$(MAKE) -C $(KERNEL_SRC) M=$(CURDIR)/$(BLUERDMA_SRC_DIR) modules
+	$(MAKE) -C $(KERNEL_SRC) M=$(CURDIR)/$(BLUERDMA_SRC_DIR) KBUILD_EXTRA_SYMBOLS=$(OFED_SYMVERS) modules
 	@mkdir -p $(BUILD_DIR)
 	cp $(BLUERDMA_SRC_DIR)/$(BLUERDMA_KO) $(BUILD_DIR)/
```

#### 2. kernel-driver Subdirectory Makefile

**File**: `/home/peng/projects/rdma_all/blue-rdma-driver/kernel-driver/Makefile`

**Change**:

```diff
 # Use Mellanox OFED kernel headers for compilation
 OFED_DIR := /usr/src/mlnx-ofed-kernel-24.10.OFED.24.10.1.1.4.1
 ccflags-y += -I$(OFED_DIR)/include
 ccflags-y += -I$(OFED_DIR)/include/rdma
 ccflags-y += -I$(OFED_DIR)/include/uapi

+# IMPORTANT: Use OFED Module.symvers for symbol CRC calculation
+ARCH := $(shell uname -m)
+KVER := $(shell uname -r)
+OFED_SYMVERS := /usr/src/ofa_kernel/$(ARCH)/$(KVER)/Module.symvers
+
 test: bluerdma.ko
```

```diff
-bluerdma.ko: main.c verbs.c
-	$(MAKE) -C $(KERNEL_DIR) M=$(PWD)
+bluerdma.ko: main.c verbs.c ethernet.c
+	$(MAKE) -C $(KERNEL_DIR) M=$(PWD) KBUILD_EXTRA_SYMBOLS=$(OFED_SYMVERS) modules
```

```diff
 clean:
-	rm -f bluerdma.ko
-	sudo rmmod bluerdma
+	$(MAKE) -C $(KERNEL_DIR) M=$(PWD) clean
+	rm -f *.o *.ko *.mod.* modules.order Module.symvers .*.cmd
```

---

## How It Works

### The KBUILD_EXTRA_SYMBOLS Mechanism

The kernel build system's `scripts/Makefile.modpost` processing logic:

```makefile
# If KBUILD_EXTRA_SYMBOLS is defined, add it to the modpost arguments
modpost-args += -e $(addprefix -i , $(KBUILD_EXTRA_SYMBOLS))
```

**modpost processing flow**:

1. Reads the `Module.symvers` file specified by `KBUILD_EXTRA_SYMBOLS`
2. Uses those CRC values for external module symbols (instead of the kernel defaults)
3. Generates `*.mod.c` files containing the correct CRCs
4. Compiles to produce the final `*.ko` file

### Complete Build Flow

```
At build time:
[OFED headers] → [gcc] → main.o, verbs.o, ethernet.o
                           |
                           ↓
[OFED Module.symvers] → [modpost] → bluerdma.mod.c (contains OFED CRCs)
                                      |
                                      ↓
                                   bluerdma.ko

At runtime:
[insmod bluerdma.ko]
  |
  ↓
Check dependency: ib_core
  |
  ↓
Load /lib/modules/.../updates/dkms/ib_core.ko.zst (OFED version)
  |
  ↓
Verify symbol: ib_register_device
  bluerdma.ko expects CRC: 0xb78db345 ✓
  ib_core.ko provides CRC: 0xb78db345 ✓
  |
  ↓
Load successful!
```

---

## Verification Steps

### 1. Build Verification

```bash
cd /home/peng/projects/rdma_all/blue-rdma-driver
make clean
make
```

**Expected output**:
```
make -C /lib/modules/6.8.0-58-generic/build M=.../kernel-driver KBUILD_EXTRA_SYMBOLS=/usr/src/ofa_kernel/x86_64/6.8.0-58-generic/Module.symvers modules
  CC [M]  .../kernel-driver/main.o
  CC [M]  .../kernel-driver/verbs.o
  CC [M]  .../kernel-driver/ethernet.o
  LD [M]  .../kernel-driver/bluerdma.o
  MODPOST .../kernel-driver/Module.symvers
  CC [M]  .../kernel-driver/bluerdma.mod.o
  LD [M]  .../kernel-driver/bluerdma.ko
```

### 2. Symbol CRC Verification

Enter the kernel-driver directory and verify the symbol versions:

```bash
cd kernel-driver

# Check the symbol CRCs in bluerdma.mod.c
python3 << 'EOF'
import struct

with open('bluerdma.mod.c', 'rb') as f:
    data = f.read()

symbols = {
    'ib_register_device': 0xb78db345,
    'ib_unregister_device': 0x429631a6,
    'ib_device_set_netdev': 0xe01d532e,
    'ib_query_port': 0x7996ccf7
}

print("Symbol CRC verification:")
all_correct = True
for sym, expected_crc in symbols.items():
    idx = data.find(sym.encode() + b'\x00')
    if idx > 4:
        actual_crc = struct.unpack('<I', data[idx-4:idx])[0]
        match = "✓" if actual_crc == expected_crc else "✗"
        print(f"{match} {sym}: 0x{actual_crc:08x} (expected: 0x{expected_crc:08x})")
        if actual_crc != expected_crc:
            all_correct = False

if all_correct:
    print("\n✓✓✓ All symbol CRCs are correct! Using OFED versions")
else:
    print("\n✗✗✗ Symbol CRCs are incorrect! Still using mainline versions")
EOF
```

**Expected output**:
```
Symbol CRC verification:
✓ ib_register_device: 0xb78db345 (expected: 0xb78db345)
✓ ib_unregister_device: 0x429631a6 (expected: 0x429631a6)
✓ ib_device_set_netdev: 0xe01d532e (expected: 0xe01d532e)
✓ ib_query_port: 0x7996ccf7 (expected: 0x7996ccf7)

✓✓✓ All symbol CRCs are correct! Using OFED versions
```

### 3. Module Load Test

```bash
cd /home/peng/projects/rdma_all/blue-rdma-driver
sudo make install
```

**Expected results**:
- ✅ No more "Invalid parameters" error
- ✅ No more "disagrees about version of symbol" kernel log messages
- ✅ Module loads successfully
- ✅ Device nodes created under `/dev/infiniband/`

---

## Key Takeaways

### Nature of the Problem

bluerdma was compiled using OFED **headers** but with the mainline kernel's **symbol version information** (Module.symvers), causing a runtime mismatch between the expected symbol versions in the module and those actually provided by the loaded OFED modules.

### Nature of the Solution

By passing the OFED Module.symvers via `KBUILD_EXTRA_SYMBOLS` during the link stage, the generated module's expected symbol CRCs are made consistent with the CRCs provided by the OFED modules actually running on the system.

### Key File Locations

| File type | Path | Purpose |
|-----------|------|---------|
| OFED headers | `/usr/src/mlnx-ofed-kernel-24.10.OFED.24.10.1.1.4.1/include/` | Used during compilation |
| OFED Module.symvers | `/usr/src/ofa_kernel/x86_64/6.8.0-58-generic/Module.symvers` | Symbol CRC definitions |
| OFED runtime module | `/lib/modules/6.8.0-58-generic/updates/dkms/ib_core.ko.zst` | Actually loaded module |
| Kernel headers | `/usr/src/linux-headers-6.8.0-58-generic/` | Build framework |
| Kernel Module.symvers | `/usr/src/linux-headers-6.8.0-58-generic/Module.symvers` | Mainline symbols (do not use) |

### Summary of Changes

1. **Root-level Makefile**: Add `KBUILD_EXTRA_SYMBOLS=$(OFED_SYMVERS)` to the `bluerdma` target
2. **kernel-driver/Makefile**:
   - Define the `OFED_SYMVERS` variable
   - Add `KBUILD_EXTRA_SYMBOLS=$(OFED_SYMVERS)` to the `bluerdma.ko` target
   - Update the `clean` target to use the kernel clean mechanism

---

## References

### Relevant Kernel Documentation

- [Module versioning & Module.symvers](https://www.kernel.org/doc/html/latest/kbuild/modules.html#symbols-from-another-external-module)
- [CONFIG_MODVERSIONS](https://cateee.net/lkddb/web-lkddb/MODVERSIONS.html)

### Mellanox OFED

- **Version**: MLNX_OFED_LINUX-24.10-1.1.4.0
- **Package**: mlnx-ofed-kernel-dkms 24.10.OFED.24.10.1.1.4.1
- **Official docs**: https://docs.nvidia.com/networking/display/ofedv24101114

### DKMS (Dynamic Kernel Module Support)

- DKMS modules are installed in `/lib/modules/$(uname -r)/updates/dkms/`
- Higher priority than mainline kernel modules
- Automatically recompiled after each kernel update

---

## Follow-up Notes

### 1. Kernel Updates

After a kernel update, if the OFED version remains the same:
- OFED will be automatically recompiled for the new kernel (via DKMS)
- bluerdma must be recompiled for the new kernel
- The Module.symvers path will change (it contains the kernel version)

### 2. OFED Updates

An OFED update may change symbol CRCs, requiring:
- Recompilation of bluerdma
- Verification of symbol version matching
- Module load testing

### 3. Portability

This solution depends on:
- OFED being correctly installed
- The Module.symvers file existing at the expected path
- The system architecture being x86_64

These prerequisites must be confirmed when deploying on other systems.

---

## Troubleshooting Guide

### If symbol mismatch errors persist

1. **Check that the OFED Module.symvers exists**:
   ```bash
   ls -lh /usr/src/ofa_kernel/$(uname -m)/$(uname -r)/Module.symvers
   ```

2. **Verify the OFED module is being loaded**:
   ```bash
   lsmod | grep ib_core
   modinfo ib_core | grep filename
   # Should show /lib/modules/.../updates/dkms/ib_core.ko.zst
   ```

3. **Check that the correct KBUILD_EXTRA_SYMBOLS was used at build time**:
   ```bash
   # You should see output similar to:
   # KBUILD_EXTRA_SYMBOLS=/usr/src/ofa_kernel/x86_64/6.8.0-58-generic/Module.symvers
   ```

4. **Clean and rebuild**:
   ```bash
   make clean
   rm -rf build/
   make
   ```

### Emergency temporary workaround (not recommended for production)

If you need to quickly test functionality (bypassing version checks):

```bash
sudo modprobe --force-modversion bluerdma
```

**Warning**: This disables ABI compatibility checks and may cause runtime crashes! Use for debugging only.

---

## Follow-on Issue

### RoCE Device Registration Failure

After resolving the symbol version issue, a new problem was encountered: **`ib_register_device` failure**

**Symptom**:
```
WARNING at device.c:841 alloc_port_data+0x10c/0x130 [ib_core]
infiniband bluerdma0: Couldn't create per-port data
ib_register_device failed for index 0
```

**Attempted fixes**:
1. ✅ Added RoCE-required callback functions (`get_link_layer`, `get_netdev`)
2. ✅ Set the device parent node (`ibdev->dev.parent`)
3. ✅ Adjusted netdev association timing (associate before registration)
4. ❌ All fixes ineffective

**Root cause**:
- Mellanox OFED is designed for **hardware RDMA devices**
- bluerdma is a **software RDMA driver**; the architectures are incompatible
- OFED's `alloc_port_data` makes hardware-specific assumptions about devices

**Solution**:
Switch to the **standard Linux RDMA subsystem**, which supports software RDMA drivers (such as rxe, siw)

**Detailed documentation**: [OFED RoCE Registration Issue](./ofed-roce-registration-issue.md)

**Migration guide**: [Switching to Standard Linux RDMA](./switch-to-vanilla-rdma.md)

---

**Document version**: 1.1
**Last updated**: 2025-11-14
**Maintainer**: Claude Code Assistant
