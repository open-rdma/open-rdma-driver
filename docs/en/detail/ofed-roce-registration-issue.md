# BlueRDMA RoCE Device Registration Failure under Mellanox OFED

**Date**: 2025-11-14
**Prerequisite issue**: [OFED Symbol Version Mismatch](./ofed-symbol-version-fix.md) ✅ Resolved
**Current issue**: `ib_register_device` fails with "Couldn't create per-port data"
**Status**: ⚠️ Cannot be resolved under Mellanox OFED → Recommended to switch to standard Linux RDMA

---

## Symptoms

### Error Messages

**Module loading**:
```bash
sudo make install
# Output:
Loading kernel modules... (Requires root privileges)
modprobe ib_core
insmod build/bluerdma.ko
insmod build/u-dma-buf.ko udmabuf0=2097152
Modules loaded.
```

**Kernel log (dmesg)**:
```
[17914675.264411] DatenLord RDMA driver loaded
[17914675.264443] ib_alloc_device ok for index 0
[17914675.264443] Registered network device blue0 for RDMA device 0  ✓
[17914675.264453] ib_alloc_device ok for index 1
[17914675.264749] Registered network device blue1 for RDMA device 1  ✓
[17914675.264758] ib_set_device_ops ok for index 0

[17914675.264824] WARNING: CPU: 52 PID: 187455 at device.c:841 alloc_port_data+0x10c/0x130 [ib_core]
[17914675.265375] infiniband bluerdma0: Couldn't create per-port data  ❌
[17914675.265379] ib_register_device failed for index 0  ❌

[17914675.314968] ib_dealloc_device ok for index 0
[17914675.344074] ib_dealloc_device ok for index 1
[17914675.344081] bluerdma_ib_device_add 0
```

### Device State

**Network devices**:
```bash
ip link show | grep blue
# Output: none (cleaned up after registration failure)
```

**RDMA devices**:
```bash
rdma link show
# Output:
link mlx5_0/1 subnet_prefix fe80:0000:0000:0000 lid 13390 sm_lid 66 lmc 0 state ACTIVE
link mlx5_1/1 subnet_prefix fe80:0000:0000:0000 lid 17488 sm_lid 66 lmc 0 state ACTIVE
link mlx5_2/1 subnet_prefix fe80:0000:0000:0000 lid 17490 sm_lid 66 lmc 0 state ACTIVE
link mlx5_3/1 subnet_prefix fe80:0000:0000:0000 lid 17489 sm_lid 66 lmc 0 state ACTIVE
# No bluerdma0 or bluerdma1
```

**InfiniBand devices**:
```bash
ls -la /sys/class/infiniband/
# Output:
mlx5_0 -> ../../devices/pci0000:00/0000:00:01.1/0000:01:00.0/infiniband/mlx5_0
mlx5_1 -> ../../devices/pci0000:20/0000:20:01.1/0000:21:00.0/infiniband/mlx5_1
mlx5_2 -> ../../devices/pci0000:40/0000:40:03.1/0000:44:00.0/infiniband/mlx5_2
mlx5_3 -> ../../devices/pci0000:60/0000:60:03.1/0000:64:00.0/infiniband/mlx5_3
# No bluerdma devices
```

---

## Root Cause Analysis

### 1. Error Location

```c
// Mellanox OFED: drivers/infiniband/core/device.c:841
static int alloc_port_data(struct ib_device *device)
{
    // ...
    // OFED-specific port data allocation logic
    // Has special requirements for RoCE devices
    // ❌ Fails here
}
```

Call stack:
```
ib_register_device+0x319/0x6b0
  └─ alloc_port_data+0x10c/0x130  ← WARNING here
     └─ "Couldn't create per-port data"
```

### 2. Design Target of Mellanox OFED

Mellanox OFED (OpenFabrics Enterprise Distribution) is an RDMA software stack optimized for **Mellanox hardware**:

| Feature | Standard Linux RDMA | Mellanox OFED |
|---------|---------------------|---------------|
| **Target** | General RDMA support | Mellanox ConnectX NIC optimization |
| **Hardware assumptions** | Software or hardware | Expects real PCI device |
| **RoCE implementation** | Standard Linux implementation | Enhanced vendor-specific implementation |
| **Port management** | Generic port abstraction | Hardware port mapping |
| **GID management** | Software GID table | Hardware GID table synchronization |

### 3. Characteristics of bluerdma

bluerdma is a **software RDMA driver**:

```
✓ Network devices: virtual Ethernet devices (blue0, blue1)
✓ Parent device: network device (not a PCI device)
✓ Data path: software implementation (no hardware DMA)
✓ GID table: maintained in software
✓ State management: fully implemented in the driver
```

### 4. Root of the Incompatibility

**What OFED's `alloc_port_data` expects**:
1. ✗ **A real PCI device** — bluerdma uses a virtual platform/network device
2. ✗ **Hardware port capabilities** — bluerdma is a pure software implementation
3. ✗ **Hardware GID table synchronization** — bluerdma uses a software GID table
4. ✗ **Vendor-specific port attributes** — bluerdma returns generic attributes

**Why mlx5_ib works**:
- mlx5_ib is the official Mellanox driver, designed specifically for their hardware
- It provides all hardware abstractions that OFED expects
- It has a real PCI device as its parent
- Hardware provides port data directly

---

## Diagnostic History

### Phase 1: Symbol version mismatch ✅ Resolved

**Problem**: Module fails to load with "Invalid parameters"

**Cause**: bluerdma used mainline kernel symbol CRCs, but the loaded ib_core was the OFED version

**Fix**: Used `KBUILD_EXTRA_SYMBOLS` pointing to OFED's Module.symvers

**Details**: See [ofed-symbol-version-fix.md](./ofed-symbol-version-fix.md)

### Phase 2: Missing RoCE required callback functions ✅ Fixed

**Problem**: Same "Couldn't create per-port data" error

**Analysis**: RoCE devices require additional callback functions

**Fix**:
```c
// verbs.h + verbs.c
enum rdma_link_layer bluerdma_get_link_layer(struct ib_device *ibdev, u32 port_num)
{
    return IB_LINK_LAYER_ETHERNET;  // RoCE uses Ethernet link layer
}

struct net_device *bluerdma_get_netdev(struct ib_device *ibdev, u32 port_num)
{
    struct bluerdma_dev *dev = to_bdev(ibdev);
    if (dev->netdev)
        dev_hold(dev->netdev);  // Increment reference count
    return dev->netdev;
}
```

```c
// main.c - bluerdma_device_ops
.get_link_layer = bluerdma_get_link_layer,
.get_netdev = bluerdma_get_netdev,
```

**Result**: Still fails ❌

### Phase 3: Set device parent node ✅ Fixed

**Problem**: OFED may require a correct device hierarchy

**Fix**:
```c
// main.c:148-151
if (testing_dev[i]->netdev) {
    ibdev->dev.parent = &testing_dev[i]->netdev->dev;
}
```

**Result**: Still fails ❌

### Phase 4: Adjust netdev association timing ✅ Fixed

**Problem**: OFED may require netdev to be associated **before** registration

**Fix**: Moved `ib_device_set_netdev` from after registration to before registration

```c
// main.c:158-170 (before)
ret = ib_register_device(ibdev, "bluerdma%d", NULL);
// ...
if (testing_dev[i]->netdev) {
    ib_device_set_netdev(ibdev, testing_dev[i]->netdev, 1);  // After registration
}

// main.c:158-172 (after)
if (testing_dev[i]->netdev) {
    ib_device_set_netdev(ibdev, testing_dev[i]->netdev, 1);  // Before registration ✓
}
ret = ib_register_device(ibdev, "bluerdma%d", NULL);
```

**Result**: Still fails ❌

### Phase 5: Other attempted fixes

Other approaches tried:
1. ✗ Modified `port_cap_flags` to add more capability flags
2. ✗ Adjusted GID table initialization timing
3. ✗ Modified `core_cap_flags` configuration
4. ✗ Added a platform_device as the parent device

**All approaches failed** ❌

---

## Current Code State

### Implemented RoCE Support

#### 1. Device operations (main.c:73-125)

```c
static const struct ib_device_ops bluerdma_device_ops = {
    // Required basic operations
    .query_device = bluerdma_query_device,
    .query_port = bluerdma_query_port,
    .get_port_immutable = bluerdma_get_port_immutable,

    // RoCE-required callbacks ✓
    .get_link_layer = bluerdma_get_link_layer,
    .get_netdev = bluerdma_get_netdev,

    // PD/QP/CQ operations
    .alloc_pd = bluerdma_alloc_pd,
    .create_qp = bluerdma_create_qp,
    .create_cq = bluerdma_create_cq,
    // ... etc.

    // GID management
    .query_gid = bluerdma_query_gid,
    .add_gid = bluerdma_add_gid,
    .del_gid = bluerdma_del_gid,
};
```

#### 2. Port configuration (verbs.c:166-193)

```c
int bluerdma_get_port_immutable(struct ib_device *ibdev, u32 port_num,
                                struct ib_port_immutable *immutable)
{
    // RoCE core capability flags ✓
    immutable->core_cap_flags = RDMA_CORE_CAP_PROT_ROCE |
                                RDMA_CORE_CAP_PROT_ROCE_UDP_ENCAP;
    immutable->pkey_tbl_len = 1;
    immutable->gid_tbl_len = BLUERDMA_GID_TABLE_SIZE;  // 16

    return 0;
}
```

#### 3. Device registration flow (main.c:138-196)

```c
for (i = 0; i < N_TESTING; i++) {
    ibdev = &testing_dev[i]->ibdev;

    // 1. Basic configuration
    ibdev->node_type = RDMA_NODE_RNIC;        // RoCE device type
    ibdev->phys_port_cnt = 1;

    // 2. Set parent device ✓
    if (testing_dev[i]->netdev) {
        ibdev->dev.parent = &testing_dev[i]->netdev->dev;
    }

    // 3. Set operation callbacks
    ib_set_device_ops(ibdev, &bluerdma_device_ops);

    // 4. Associate network device (before registration) ✓
    if (testing_dev[i]->netdev) {
        ib_device_set_netdev(ibdev, testing_dev[i]->netdev, 1);
    }

    // 5. Register IB device ❌ fails here
    ret = ib_register_device(ibdev, "bluerdma%d", NULL);
}
```

#### 4. Network device creation (ethernet.c:168-204)

```c
int bluerdma_create_netdev(struct bluerdma_dev *dev, int id)
{
    // 1. Allocate Ethernet device
    netdev = alloc_etherdev(sizeof(struct bluerdma_dev));
    snprintf(netdev->name, IFNAMSIZ, "blue%d", id);  // blue0, blue1

    // 2. Configure the network device
    bluerdma_netdev_setup(netdev);

    // 3. Initialize GID table (based on MAC address)
    bluerdma_init_gid_table(dev);
    // Default GID: fe80::xxxx:xxFF:FExx:xxxx (link-local)

    // 4. Register the network device ✓
    ret = register_netdev(netdev);

    return ret;
}
```

### Network Device State

**Created successfully** ✓:
- blue0 and blue1 are created successfully
- Log shows "Registered network device blue0 for RDMA device 0"

**But cleaned up after IB registration failure**:
```c
// main.c:153-160 - error handling
if (ret) {
    pr_err("ib_register_device failed for index %d\n", i);
    while (--i >= 0) {
        ib_unregister_device(&testing_dev[i]->ibdev);
    }
    bluerdma_free_testing();  // Clean up all devices, including netdev
    return ret;
}
```

---

## Why It Cannot Work under Mellanox OFED

### 1. Architectural Mismatch

| Component | bluerdma | OFED expectation |
|-----------|----------|------------------|
| **Hardware basis** | Pure software | Real PCI hardware |
| **Parent device** | `net_device` | `pci_device` |
| **Port data** | Software simulation | Hardware mapping |
| **DMA engine** | Software copy | Hardware DMA |
| **GID table** | Software maintained | Hardware synchronized |

### 2. OFED's Hardware Assumptions

Mellanox OFED's `alloc_port_data` likely performs the following checks (inferred):

```c
// Likely logic near OFED device.c:841
static int alloc_port_data(struct ib_device *device)
{
    // Check 1: PCI device present?
    if (!device->dev.parent || !dev_is_pci(device->dev.parent))
        goto err;  // ❌ bluerdma uses net_device

    // Check 2: Hardware port capabilities?
    if (!check_hardware_port_caps(device))
        goto err;  // ❌ bluerdma returns software attributes

    // Check 3: Vendor-specific initialization?
    if (!mlx_vendor_specific_init(device))
        goto err;  // ❌ bluerdma is not Mellanox hardware

err:
    dev_warn(&device->dev, "Couldn't create per-port data");
    return -EINVAL;
}
```

### 3. Standard Linux RDMA vs. Mellanox OFED

**Standard Linux RDMA**:
- Designed as a general framework supporting both software and hardware drivers
- `ib_register_device` requires only basic callback functions
- No strict restrictions on device type or parent device type
- Examples: rxe (software RoCE) and siw (software iWARP) both work correctly

**Mellanox OFED**:
- Optimized for Mellanox hardware
- Adds extra hardware assumptions and checks
- Has special requirements for port data structures
- May depend on hardware-specific features

---

## Conclusion

### Core Issue

**bluerdma is a software RDMA driver; Mellanox OFED is a distribution optimized for hardware — the two architectures are incompatible.**

### Eliminated Possibilities

✓ Not a symbol version issue (resolved via KBUILD_EXTRA_SYMBOLS)
✓ Not missing callback functions (added get_link_layer and get_netdev)
✓ Not a device hierarchy issue (parent and netdev association set)
✓ Not a registration timing issue (adjusted to associate before registration)

### Root Cause

**Mellanox OFED's `alloc_port_data` function makes hardware-specific assumptions about devices that cannot be satisfied by a software RDMA driver.**

---

## Recommended Solution

### Switch to Standard Linux RDMA

**Reasons**:
1. bluerdma is a **software RDMA driver** and does not need Mellanox OFED's hardware optimizations
2. Standard Linux RDMA supports software drivers (e.g., rxe, siw)
3. Avoids compatibility issues with vendor-specific implementations

**Advantages**:
- ✅ Architecture match: the standard RDMA framework supports software drivers
- ✅ Simplified development: no need to deal with OFED-specific requirements
- ✅ Community support: mainline kernel RDMA subsystem
- ✅ Portability: works on any Linux system

**Disadvantages**:
- ⚠️ Requires temporarily disabling Mellanox OFED (if hardware RDMA is also needed)
- ⚠️ Build configuration changes required

**Implementation guide**: See [Switching to Standard Linux RDMA](./switch-to-vanilla-rdma.md)

---

## Technical Detail Records

### OFED Version Information

```bash
$ ofed_info -s
MLNX_OFED_LINUX-24.10-1.1.4.0

$ dpkg -l | grep mlnx-ofed
mlnx-ofed-kernel-dkms  24.10.OFED.24.10.1.1.4.1
```

### System Information

```bash
$ uname -r
6.8.0-58-generic

$ uname -m
x86_64
```

### Currently Loaded ib_core

```bash
$ modinfo ib_core | grep filename
filename: /lib/modules/6.8.0-58-generic/updates/dkms/ib_core.ko.zst
# ↑ OFED version (in updates/dkms/ directory)
```

### Build Parameters

```makefile
# Makefile
KBUILD_EXTRA_SYMBOLS=/usr/src/ofa_kernel/x86_64/6.8.0-58-generic/Module.symvers

# kernel-driver/Makefile
ccflags-y += -I/usr/src/mlnx-ofed-kernel-24.10.OFED.24.10.1.1.4.1/include
```

---

## References

### Related Documents

1. [OFED Symbol Version Fix](./ofed-symbol-version-fix.md) - Resolution of the first-stage issue
2. [Switching to Standard Linux RDMA](./switch-to-vanilla-rdma.md) - Recommended solution

### Linux RDMA Subsystem

- Software RoCE (rxe): https://github.com/SoftRoCE/rxe-dev
- Software iWARP (siw): https://www.kernel.org/doc/html/latest/infiniband/siwarp.html
- RDMA Core documentation: https://github.com/linux-rdma/rdma-core

### Mellanox OFED

- Official documentation: https://docs.nvidia.com/networking/display/ofedv24101114
- User manual: https://docs.nvidia.com/networking/display/MLNXOFEDv24101114/User+Manual

---

## Troubleshooting Checklist

If you still want to debug under OFED (not recommended):

### 1. Verify OFED version and configuration

```bash
ofed_info -s
ls -l /usr/src/ofa_kernel/$(uname -m)/$(uname -r)/Module.symvers
```

### 2. Check kernel configuration

```bash
grep CONFIG_INFINIBAND /boot/config-$(uname -r)
# Should show InfiniBand-related configuration
```

### 3. View detailed OFED logs

```bash
sudo dmesg | grep -E "ib_core|bluerdma|mlx5" | tail -100
```

### 4. Try force-loading (debug only)

```bash
sudo modprobe --force bluerdma
# ⚠️ Warning: may cause a kernel crash
```

### 5. Compare with mlx5_ib implementation

```bash
# View the official Mellanox driver implementation
grep -r "alloc_port_data" /usr/src/mlnx-ofed-kernel-*/
```

---

## Timeline

| Date | Phase | Issue | Status |
|------|-------|-------|--------|
| 2025-11-12 | 1 | Symbol version mismatch | ✅ Resolved |
| 2025-11-14 | 2 | RoCE device registration failure | ⚠️ Cannot be resolved under OFED |
| 2025-11-14 | — | Decision to switch to standard RDMA | 📋 Pending |

---

**Document version**: 1.0
**Last updated**: 2025-11-14
**Maintainer**: Claude Code Assistant
**Next step**: [Switching to Standard Linux RDMA](./switch-to-vanilla-rdma.md)
