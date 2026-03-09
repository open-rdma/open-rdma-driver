# Blue RDMA Rust Driver - Project Introduction

## Overview

`rust-driver` is the core RDMA (Remote Direct Memory Access) driver implementation written in Rust. It integrates with the standard libibverbs framework via FFI (Foreign Function Interface) as a high-performance library, providing a Rust-based RDMA verbs implementation for Blue RDMA hardware.

The driver supports three operating modes to accommodate different development scenarios:
- **Hardware mode** (`--features hw`): Uses a physical PCIe RDMA device
- **Simulation mode** (`--features sim`): Hardware emulation testing via RTL simulator using UDP and TCP communication
- **Mock mode** (`--features mock`): Pure software testing with no external dependencies

## Project Structure

### Core Modules

#### `verbs/` - RDMA Verbs API Layer
Implements an libibverbs-compatible RDMA verbs interface:
- `ffi.rs`: C ABI export functions for dynamic loading by libibverbs
- `ctx.rs`: `HwDeviceCtx` — core device context managing RDMA operations
- `dev.rs`: Device initialization (`PciHwDevice` for hardware mode, `EmulatedHwDevice` for simulation mode)
- `mock.rs`: `MockDeviceCtx` — device context implementation for mock mode (peer of `HwDeviceCtx`)

#### `csr/` - Control and Status Register Access
Hardware abstraction layer providing unified CSR access across different modes:
- `device_adaptor.rs`: Core `DeviceAdaptor` trait defining CSR read/write interface
- `ring_specs.rs`: Specification definitions for individual hardware ring queues (SendRing, CmdReqRing, MetaReportRing, etc.)
- `hardware.rs`: Hardware-mode CSR access implementation via PCIe MMIO through `/dev/mem` or VFIO
- `emulated.rs`: Simulation-mode CSR access implementation via UDP RPC with the RTL simulator (ports 7701/7702)
- `mode.rs`: Device operating mode configuration (100G/200G/400G)

The generic `Ring<Dev, Spec>` pattern provides type-safe ring buffer operations with compile-time direction checking.

#### `mem/` - Memory Management (currently somewhat disorganized)
DMA buffer allocation and virtual-to-physical address translation:
- `virt_to_phy.rs`: Address translation implementation
  - `PhysAddrResolverLinuxX86`: Both hardware and simulation modes use `/proc/self/pagemap` to obtain physical addresses for MRs (Memory Regions)
  - Note: In simulation mode, the "physical addresses" of ring buffers are virtual addresses passed to the simulator as-is
- `pa_va_map.rs`: Bidirectional physical-virtual address mapping, used for simulator access to host memory in sim mode

Supports 4KB and 2MB huge pages (default: 2MB for best performance), but the page size is fixed at compile time via features; runtime configuration may be needed in the future.

#### `workers/` - Background Processing Threads (Core Business Logic)
Asynchronous processing worker threads for RDMA operations:
- `completion.rs`: Completion queue (CQ) event handling, reporting operation completion status to the application layer
- `send/`: Send work request processing pipeline, converting Send WRs to hardware descriptors
- `rdma.rs`: Worker thread for RDMA Read/Write/Atomic operations
- `retransmit.rs`: Packet retransmission logic and timeout handling
- `ack_responder.rs`: ACK packet generation and response handling
- `qp_timeout.rs`: Queue Pair (QP) timeout management
- `meta_report/`: Metadata report processing, extracting BTH/RETH header information from hardware
- `spawner.rs`: Worker thread lifecycle management

#### `ringbuf/` - Ring Buffer Abstraction
Generic descriptor ring buffer management (note: the abstraction design may need improvement):
- `desc.rs`: Descriptor serialization/deserialization trait definitions
- `dma_rb.rs`: DMA-based ring buffer implementation using head/tail pointers for producer-consumer synchronization

#### Support Modules
- `config.rs`: Device configuration loader (reads from `/etc/bluerdma/config.toml`)
- `constants.rs`: Global constants and hardware address definitions
- `memory_proxy_simple.rs`: Memory proxy TCP server for DMA access in simulation mode

## Operating Modes

### Hardware Mode (`--features hw`)
Uses physical RDMA hardware in production environments:
- **CSR Access**: Direct PCIe MMIO access via `/dev/mem` (SysfsPci) or VFIO
- **Device Type**: `PciHwDevice` (using the `pci-driver` crate)
- **Address Translation**: Physical addresses obtained by reading Linux page tables via `/proc/self/pagemap`
- **Network Interface**: Integrates with the real network stack via TAP device
- **Memory Management**: Uses `mlock()` to pin DMA buffers and prevent swapping

### Simulation Mode (`--features sim`)
Hardware verification in conjunction with the RTL simulator:
- **CSR Access**: Communicates with the simulator via UDP RPC (ports 7701/7702)
- **Device Type**: `EmulatedHwDevice` (UDP-based communication)
- **Address Translation**:
  - MR (Memory Region): Uses `/proc/self/pagemap` to obtain real physical addresses
  - Ringbuf: Virtual addresses passed directly as "physical addresses" to the simulator
  - PA↔VA mapping table: Used for simulator access to host memory through the memory proxy
- **Network Interface**: Exchanges packets with the simulator via UDP
- **Memory Proxy**: TCP server (ports 7003/7004) enabling the simulator to directly access host DMA memory

### Mock Mode (`--features mock`)
Pure software simulation for unit testing and CI/CD:
- **Device Type**: `MockDeviceCtx` (in-memory simulation implementation)
- **Address Translation**: Software simulation, no real physical addresses required
- **Characteristics**: No physical hardware or simulator required; fast startup; suitable for automated testing

## Key Architecture

### Ring Buffer System
The driver employs a type-safe ring buffer architecture with compile-time directional correctness guarantees:
- **Type-safe direction**: `RingSpecToCard` (driver writes) provides WriterOps; `RingSpecToHost` (hardware writes) provides ReaderOps
- **Ring queue types**: SendRing, MetaReportRing, CmdReqRing, CmdRespRing, SimpleNicTxRing, SimpleNicRxRing
- **Synchronization**: Head/tail pointers managed via hardware CSR registers, implementing lock-free producer-consumer pattern
- **Generic abstraction**: `Ring<Dev, Spec>` provides a unified, type-safe interface for all ring queues

### Memory Management
Different modes employ different memory management strategies:

**Hardware mode**:
- Uses `/proc/self/pagemap` to read Linux page tables for physical addresses
- Uses `mlock()` to pin DMA buffers and prevent swapping
- Note: For GPU memory and host memory managed by GPU drivers, physical address retrieval and pinning mechanisms may not apply

**Simulation mode**:
- MR (Memory Region): Uses `/proc/self/pagemap` to obtain real physical addresses
- Ringbuf: Virtual addresses used directly as physical addresses, simplifying simulator implementation
- PA↔VA mapping table: Maintains bidirectional mapping for use by the memory proxy server, allowing the simulator to access host memory via physical addresses

**Common characteristics**:
- DMA buffers require physically contiguous memory with known physical addresses
- Supports 4KB and 2MB huge pages (selected at compile time via features, default 2MB; only one can be chosen — may need modification later)

### FFI Integration
The driver integrates with the libibverbs C framework via FFI:

**Exported function examples**:
```rust
#[unsafe(export_name = "bluerdma_init")]
pub unsafe extern "C" fn init() // Global initialization

#[unsafe(export_name = "bluerdma_new")]
pub unsafe extern "C" fn new(sysfs_name: *const c_char) -> *mut c_void // Device creation
```

**Integration mechanism**:
- The C provider layer dynamically loads the Rust driver's shared library via `dlopen()`
- Implements the standard libibverbs provider interface, transparent to upper-layer applications
- All exported functions use `extern "C"` ABI to ensure C compatibility

## Component Architecture and Integration

The Blue RDMA driver uses a layered architecture design, with the kernel module, userspace Rust driver, C Provider layer, and standard libibverbs library working together.

### Architecture Overview

The entire system uses a mixed userspace-kernelspace architecture:

```
Userspace                                        Kernel space
────────────────────────────────────────────────────────────────────────

┌─────────────────────────────────────┐
│   Application (perftest, MPI, etc.) │
│   - Uses standard libibverbs API    │
└─────────────────────────────────────┘
            │ ibv_*() calls
            ▼
┌─────────────────────────────────────┐       ┌─────────────────────────┐
│ libibverbs + C Provider             │       │ Kernel Driver           │
│ (libibverbs.so + statically linked  │       │ (bluerdma.ko)           │
│  provider)                          │       │ + ib_uverbs.ko          │
│                                     │       │                         │
│ ├─ libibverbs core  ─────────────[1]────────>├─────────────────────────┤
│ │  - Standard IB Verbs API          │ sysfs │ - Registers IB device   │
│ │  - Device discovery & mgmt <───[2]────────│ - Handles ioctl/write   │
│ │                                   │ ioctl │ - GID management        │
│ └─ Blue RDMA Provider               │       └─────────────────────────┘
│    (providers/bluerdma/)            │
│    - Statically linked at build     │
│    - bluerdma_device_alloc()        │
└─────────────────────────────────────┘
            │ dlopen("libbluerdma_rust.so")
            │ (the only dynamic load)
            ▼
┌─────────────────────────────────────┐
│ Rust Driver (rust-driver)           │
│ - Core business logic               │
│ - Hardware resource management      │
│ - Background worker threads         │
└─────────────────────────────────────┘
            │ PCIe MMIO / UDP / Mock
            ▼
┌─────────────────────────────────────┐
│ Hardware / Simulator / Mock         │
│ - Blue RDMA NIC                     │
│ - RTL simulator                     │
│ - Software simulation               │
└─────────────────────────────────────┘

Communication channel notes:
[1] Device discovery: /sys/class/infiniband_verbs/uverbs*
    - libibverbs scans this directory to discover devices
    - Reads dev attribute to get device number

[2] Device communication: /dev/infiniband/uverbs*
    - Character device opened during ibv_open_device()
    - ioctl/write syscalls for kernel communication
```

### Component Responsibilities

#### 1. kernel-driver (bluerdma.ko)
**Location**: `blue-rdma-driver/kernel-driver/`
**Build artifact**: `bluerdma.ko`

**Primary responsibilities**:
- Register IB device with the kernel RDMA subsystem (`ib_register_device`)
- Create character device `/dev/infiniband/uverbs*` (for ioctl communication)
- Create sysfs interfaces:
  - `/sys/class/infiniband/bluerdma*` (IB device information)
  - `/sys/class/infiniband_verbs/uverbs*` (device discovery entry point, scanned by libibverbs)
- Create and manage network devices (blue0, blue1)
- Handle GID (Global Identifier) table management

**Note**: The current kernel driver's verbs methods are mostly stub functions (only printing logs); actual business logic is implemented in the userspace Rust driver.

**Device discovery and access flow**:
1. libibverbs scans `/sys/class/infiniband_verbs/` to discover devices (e.g., `uverbs0`)
2. Reads `/sys/class/infiniband_verbs/uverbs0/dev` to get the device number (e.g., `231:0`)
3. Opens character device `/dev/infiniband/uverbs0` for ioctl communication (currently unused in simulation)

**Key code** (`main.c`):
```c
static int bluerdma_ib_device_add(struct pci_dev *pdev)
{
    // Allocate IB device structure
    dev = ib_alloc_device(bluerdma_dev, ibdev);

    // Set device operations table
    ib_set_device_ops(ibdev, &bluerdma_device_ops);

    // Register with kernel RDMA subsystem
    ret = ib_register_device(ibdev, "bluerdma%d", NULL);

    // Associate network device
    ib_device_set_netdev(ibdev, dev->netdev, 1);
}
```

#### 2. libibverbs + C Provider (rdma-core-55.0)
**Location**: `blue-rdma-driver/dtld-ibverbs/rdma-core-55.0/`
**Build artifact**: `libibverbs.so` and related libraries

**Components**:
- **libibverbs core**: Standard RDMA verbs API implementation
- **Blue RDMA Provider**: `providers/bluerdma/` directory, statically linked to rdma-core at build time

**Primary responsibilities**:
- Provide standard libibverbs API to applications
- Scan `/sys/class/infiniband_verbs/` to discover RDMA devices
- Open `/dev/infiniband/uverbs*` character devices for communication
- Blue RDMA Provider is responsible for dynamically loading the Rust driver library

**Note**: This is a modified version of upstream rdma-core. Blue RDMA's provider code is integrated into the source tree and built together at compile time — it is **not** a plugin loaded dynamically at runtime.

**Key function calls** (`init.c` and `device.c`):
```c
// device.c:73 - get device list
struct ibv_device **ibverbs_get_device_list(int *num_devices) {
    return ibverbs_init(&drivers_list, num_devices);
}

// init.c:204-238 - scan sysfs to discover devices
static int find_sysfs_devs(struct list_head *tmp_sysfs_dev_list) {
    // Build path: /sys/class/infiniband_verbs
    if (!check_snprintf(class_path, sizeof(class_path),
                        "%s/class/infiniband_verbs", ibv_get_sysfs_path()))
        return ENOMEM;

    class_dir = opendir(class_path);
    // Iterate over uverbs0, uverbs1, etc.
    while ((dent = readdir(class_dir))) {
        setup_sysfs_dev(dirfd(class_dir), dent->d_name, ...);
    }
}

// device.c:335 - open character device
cmd_fd = open_cdev(verbs_device->sysfs->sysfs_name,  // "uverbs0"
                   verbs_device->sysfs->sysfs_cdev);   // device number

// open_cdev.c:134-146 - actually opens /dev/infiniband/uverbs*
int open_cdev(const char *devname_hint, dev_t cdev) {
    // Build path: /dev/infiniband/uverbs0
    if (asprintf(&devpath, RDMA_CDEV_DIR "/%s", devname_hint) < 0)
        return -1;
    fd = open_cdev_internal(devpath, cdev);  // open("/dev/infiniband/uverbs0", ...)
    return fd;
}
```

#### 3. Blue RDMA Provider (C Bridge Layer)
**Location**: `blue-rdma-driver/dtld-ibverbs/rdma-core-55.0/providers/bluerdma/`
**Core file**: `bluerdma.c`
**Build method**: Statically linked to libibverbs at compile time

**Primary responsibilities**:
- Implement the provider interface, responding to device allocation requests
- **Dynamically load the Rust driver library** (`libbluerdma_rust.so`) — the only `dlopen` call in the system
- Provide C ABI bridge, forwarding libibverbs calls to the Rust driver
- Handle device initialization and context allocation

**Key code** (`bluerdma.c:393-467`):
```c
static struct verbs_device *
bluerdma_device_alloc(struct verbs_sysfs_dev *sysfs_dev)
{
    struct bluerdma_device *dev;
    void *dl_handler;
    void *(*driver_new)(char *);
    void (*driver_init)(void);

    // Dynamically load Rust driver library (the only dlopen call in the system)
    dl_handler = dlopen("libbluerdma_rust.so", RTLD_NOW);
    if (!dl_handler) {
        printf("dlopen failed: %s\n", dlerror());
        goto err_dev;
    }

    // Get function pointers exported by Rust
    driver_init = dlsym(dl_handler, "bluerdma_init");
    driver_new = dlsym(dl_handler, "bluerdma_new");

    // Call Rust initialization function
    driver_init();

    // Dynamically load all verbs operations
    bluerdma_set_ops(dl_handler, ops);

    return &dev->ibv_dev;
}

// Dynamically set all verbs operations
static void bluerdma_set_ops(void *dl_handler, struct verbs_context_ops *ops)
{
    void *fn = NULL;

    // Load function pointer for each operation from the Rust library
    fn = dlsym(dl_handler, "bluerdma_alloc_pd");
    if (fn) ops->alloc_pd = fn;

    fn = dlsym(dl_handler, "bluerdma_reg_mr");
    if (fn) ops->reg_mr = fn;

    fn = dlsym(dl_handler, "bluerdma_create_qp");
    if (fn) ops->create_qp = fn;

    fn = dlsym(dl_handler, "bluerdma_post_send");
    if (fn) ops->post_send = fn;

    fn = dlsym(dl_handler, "bluerdma_poll_cq");
    if (fn) ops->poll_cq = fn;

    // ... other verbs operations
}
```

**Provider registration**:
```c
static const struct verbs_device_ops bluerdma_dev_ops = {
    .name = "bluerdma",
    .match_min_abi_version = 1,
    .match_max_abi_version = 1,
    .alloc_device = bluerdma_device_alloc,
    .alloc_context = bluerdma_alloc_context,
};

// Automatically register provider using macro (executed at dlopen time)
PROVIDER_DRIVER(bluerdma, bluerdma_dev_ops);
```

#### 4. rust-driver (Core Driver)
**Location**: `blue-rdma-driver/rust-driver/`
**Build artifact**: `libbluerdma_rust.so`

**Primary responsibilities**:
- Implement core business logic for all RDMA verbs operations
- Manage hardware resources (QP, CQ, MR, PD, etc.)
- Handle memory registration and address translation
- Manage DMA buffers and ring queues
- Run background worker threads (send, retransmit, completion handling, etc.)
- Communicate with hardware via CSR (or with the simulator via UDP)

**FFI exports** (`src/rxe/ctx_ops.rs`):
```rust
// Global initialization
#[unsafe(export_name = "bluerdma_init")]
pub unsafe extern "C" fn init() {
    let _ = env_logger::builder()
        .format_timestamp(Some(env_logger::TimestampPrecision::Nanos))
        .try_init();
}

// Create device context
#[unsafe(export_name = "bluerdma_new")]
pub unsafe extern "C" fn new(sysfs_name: *const c_char) -> *mut c_void {
    BlueRdmaCore::new(sysfs_name)
}

// Allocate Protection Domain
#[unsafe(export_name = "bluerdma_alloc_pd")]
pub unsafe extern "C" fn alloc_pd(ctx: *mut ffi::ibv_context) -> *mut ffi::ibv_pd {
    BlueRdmaCore::alloc_pd(ctx)
}

// Register Memory Region
#[unsafe(export_name = "bluerdma_reg_mr")]
pub unsafe extern "C" fn reg_mr(
    pd: *mut ffi::ibv_pd,
    addr: *mut c_void,
    length: usize,
    access: i32,
) -> *mut ffi::ibv_mr {
    BlueRdmaCore::reg_mr(pd, addr, length, access)
}

// Create Queue Pair
#[unsafe(export_name = "bluerdma_create_qp")]
pub unsafe extern "C" fn create_qp(
    pd: *mut ffi::ibv_pd,
    init_attr: *mut ffi::ibv_qp_init_attr,
) -> *mut ffi::ibv_qp {
    BlueRdmaCore::create_qp(pd, init_attr)
}

// Post send request
#[unsafe(export_name = "bluerdma_post_send")]
pub unsafe extern "C" fn post_send(
    qp: *mut ffi::ibv_qp,
    wr: *mut ffi::ibv_send_wr,
    bad_wr: *mut *mut ffi::ibv_send_wr,
) -> c_int {
    BlueRdmaCore::post_send(qp, wr, bad_wr)
}

// Poll completion queue
#[unsafe(export_name = "bluerdma_poll_cq")]
pub unsafe extern "C" fn poll_cq(
    cq: *mut ffi::ibv_cq,
    num_entries: i32,
    wc: *mut ffi::ibv_wc,
) -> i32 {
    BlueRdmaCore::poll_cq(cq, num_entries, wc)
}
```

### Complete Call Chain

Using `ibv_post_send()` as an example, showing the complete call chain from application to hardware:

```
Application
    │
    ├─ ibv_post_send(qp, wr, bad_wr)
    │
    ▼
┌─────────────────────────────────────────┐
│ libibverbs                              │
│   verbs_post_send()                     │
│     └─> ctx->ops->post_send()           │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│ C Provider (bluerdma.c)                 │
│   ops->post_send = bluerdma_post_send   │
│     └─> forwards directly to Rust       │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│ Rust Driver                             │
│   bluerdma_post_send()                  │
│     └─> BlueRdmaCore::post_send()       │
│           └─> HwDeviceCtx::post_send()  │
│                 ├─ Parse SendWr         │
│                 ├─ Generate HW descriptor│
│                 ├─ Write to Send Ring   │
│                 └─ Notify SendWorker    │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│ SendWorker (background thread)          │
│   ├─ Read descriptor from Ring Buffer   │
│   ├─ DMA read user data                 │
│   ├─ Update hardware CSR (tail pointer) │
│   └─ Register timeout detection        │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│ Hardware / Simulator                    │
│   ├─ Read descriptor                    │
│   ├─ DMA read data                      │
│   ├─ Encapsulate network packet         │
│   └─ Send over Ethernet                 │
└─────────────────────────────────────────┘
```

### Initialization Flow

**Phase 1: Kernel module loading**
```bash
insmod bluerdma.ko
```
1. `bluerdma_init_module()` executes
2. `bluerdma_probe()` creates test device
3. `bluerdma_ib_device_add()` registers IB device
4. Creates sysfs interfaces:
   - `/sys/class/infiniband/bluerdma0` (IB device information)
   - `/sys/class/infiniband_verbs/uverbs0` (libibverbs scans this directory)
5. Creates character device `/dev/infiniband/uverbs0`
6. Creates network devices `blue0`, `blue1`

**Phase 2: Application opens device**
```c
struct ibv_device **dev_list = ibv_get_device_list(NULL);
struct ibv_context *ctx = ibv_open_device(dev_list[0]);
```

Detailed call chain:
1. **Application**: `ibv_get_device_list()`
2. **libibverbs** (`device.c:73`): `ibverbs_get_device_list()`
3. **libibverbs** (`init.c:560`): `find_sysfs_devs()`
   - Scans `/sys/class/infiniband_verbs/uverbs*`
   - Note: scans `infiniband_verbs`, not `infiniband`
4. **libibverbs** (`init.c:541`): `try_drivers()` matches provider
5. **C Provider** (`bluerdma.c:393`): `bluerdma_device_alloc()`
   - Dynamically loads Rust driver library
6. **C Provider** (`bluerdma.c:407`): `dlopen("libbluerdma_rust.so")`
7. **C Provider** (`bluerdma.c:422`): calls `bluerdma_init()` [Rust FFI]
8. **C Provider** (`bluerdma.c:359`): calls `bluerdma_new("uverbs0")` [Rust FFI]
9. **Rust Driver** (`core.rs:81`): `BlueRdmaCore::new()`
   - Initializes hardware adaptor (PCIe/UDP/Mock)
   - Example for sim mode: connects UDP `127.0.0.1:7701`
10. **Rust Driver**: `HwDeviceCtx::initialize()`
    - Allocates DMA buffers and ring queues
    - Starts background worker threads
    - Initializes resource managers (QP/CQ/MR/PD)

**Phase 3: Runtime background threads**
The following worker threads run continuously in the background:
- `SendWorker`: Processes the send queue
- `RdmaWriteWorker`: Handles RDMA Write operations
- `CompletionWorker`: Handles completion events
- `PacketRetransmitWorker`: Timeout-based retransmission
- `AckResponder`: Generates and sends ACKs
- `QpAckTimeoutWorker`: Detects ACK timeouts
- `MetaReportWorker`: Reads metadata reports from hardware

### Key Design Features

**1. Hybrid architecture**
- Kernel layer: Provides device framework and character device interface
- Userspace layer: Implements core business logic with higher performance and more development flexibility

**2. Single dynamic load**
- Blue RDMA Provider is statically linked to libibverbs at build time
- At runtime there is only one `dlopen`: the Provider loading the Rust driver (`libbluerdma_rust.so`)

**3. Zero-copy data path**
- Application data transferred directly to hardware via DMA
- Lock-free communication via ring buffers
- Background threads process asynchronously without blocking the application

**4. Multi-mode support**
- Hardware mode: PCIe MMIO access to real NIC
- Simulation mode: UDP communication with RTL simulator (ports 7701/7702)
- Mock mode: Pure software simulation for CI/CD

**5. TCP auxiliary channel**
For Send/Recv semantics, TCP connections are used to pass `post_recv` information between QP peers, enabling the sender to match receive buffers.
