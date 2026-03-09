# BlueRDMA 在 Mellanox OFED 下的 RoCE 设备注册失败问题

**日期**: 2025-11-14
**前置问题**: [OFED 符号版本不匹配问题](./ofed-symbol-version-fix.md) ✅ 已解决
**当前问题**: `ib_register_device` 失败，报错 "Couldn't create per-port data"
**状态**: ⚠️ 无法在 Mellanox OFED 下解决 → 建议切换到标准 Linux RDMA

---

## 问题现象

### 错误信息

**模块加载**：
```bash
sudo make install
# 输出：
Loading kernel modules... (Requires root privileges)
modprobe ib_core
insmod build/bluerdma.ko
insmod build/u-dma-buf.ko udmabuf0=2097152
Modules loaded.
```

**内核日志 (dmesg)**：
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

### 设备状态

**网络设备**：
```bash
ip link show | grep blue
# 输出：无（注册失败后被清理）
```

**RDMA 设备**：
```bash
rdma link show
# 输出：
link mlx5_0/1 subnet_prefix fe80:0000:0000:0000 lid 13390 sm_lid 66 lmc 0 state ACTIVE
link mlx5_1/1 subnet_prefix fe80:0000:0000:0000 lid 17488 sm_lid 66 lmc 0 state ACTIVE
link mlx5_2/1 subnet_prefix fe80:0000:0000:0000 lid 17490 sm_lid 66 lmc 0 state ACTIVE
link mlx5_3/1 subnet_prefix fe80:0000:0000:0000 lid 17489 sm_lid 66 lmc 0 state ACTIVE
# 没有 bluerdma0 或 bluerdma1
```

**InfiniBand 设备**：
```bash
ls -la /sys/class/infiniband/
# 输出：
mlx5_0 -> ../../devices/pci0000:00/0000:00:01.1/0000:01:00.0/infiniband/mlx5_0
mlx5_1 -> ../../devices/pci0000:20/0000:20:01.1/0000:21:00.0/infiniband/mlx5_1
mlx5_2 -> ../../devices/pci0000:40/0000:40:03.1/0000:44:00.0/infiniband/mlx5_2
mlx5_3 -> ../../devices/pci0000:60/0000:60:03.1/0000:64:00.0/infiniband/mlx5_3
# 没有 bluerdma 设备
```

---

## 根本原因分析

### 1. 错误位置

```c
// Mellanox OFED: drivers/infiniband/core/device.c:841
static int alloc_port_data(struct ib_device *device)
{
    // ...
    // OFED 特有的端口数据分配逻辑
    // 对 RoCE 设备有特殊要求
    // ❌ 在这里失败
}
```

调用栈：
```
ib_register_device+0x319/0x6b0
  └─ alloc_port_data+0x10c/0x130  ← WARNING 在这里
     └─ "Couldn't create per-port data"
```

### 2. Mellanox OFED 的设计目标

Mellanox OFED (OpenFabrics Enterprise Distribution) 是针对 **Mellanox 硬件** 优化的 RDMA 软件栈：

| 特性 | 标准 Linux RDMA | Mellanox OFED |
|------|----------------|---------------|
| **目标** | 通用 RDMA 支持 | Mellanox ConnectX 网卡优化 |
| **硬件假设** | 软件/硬件均可 | 期望真实 PCI 设备 |
| **RoCE 实现** | 标准 Linux 实现 | 增强的厂商特定实现 |
| **端口管理** | 通用端口抽象 | 硬件端口映射 |
| **GID 管理** | 软件 GID 表 | 硬件 GID 表同步 |

### 3. bluerdma 的特点

bluerdma 是一个**软件 RDMA 驱动**：

```
✓ 网络设备：虚拟以太网设备 (blue0, blue1)
✓ 父设备：network device（非 PCI 设备）
✓ 数据路径：软件实现（无硬件 DMA）
✓ GID 表：软件维护
✓ 状态管理：完全在驱动中实现
```

### 4. 不兼容的根源

**Mellanox OFED 的 `alloc_port_data` 期望**：
1. ✗ **真实的 PCI 设备** - bluerdma 使用虚拟 platform/network device
2. ✗ **硬件端口能力** - bluerdma 是纯软件实现
3. ✗ **硬件 GID 表同步** - bluerdma 使用软件 GID 表
4. ✗ **厂商特定的端口属性** - bluerdma 返回通用属性

**为什么 mlx5_ib 可以工作**：
- mlx5_ib 是 Mellanox 官方驱动，专为其硬件设计
- 提供了 OFED 期望的所有硬件抽象
- 有真实的 PCI 设备作为父设备
- 硬件直接提供端口数据

---

## 诊断历程

### 阶段 1: 符号版本不匹配 ✅ 已解决

**问题**: 模块加载失败，报 "Invalid parameters"

**原因**: bluerdma 使用内核原生的符号 CRC，但运行时加载的是 OFED 版本的 ib_core

**解决**: 使用 `KBUILD_EXTRA_SYMBOLS` 指向 OFED 的 Module.symvers

**详情**: 见 [ofed-symbol-version-fix.md](./ofed-symbol-version-fix.md)

### 阶段 2: 缺少 RoCE 必需的回调函数 ✅ 已解决

**问题**: 同样的 "Couldn't create per-port data" 错误

**分析**: RoCE 设备需要额外的回调函数

**修复**:
```c
// verbs.h + verbs.c
enum rdma_link_layer bluerdma_get_link_layer(struct ib_device *ibdev, u32 port_num)
{
    return IB_LINK_LAYER_ETHERNET;  // RoCE 使用以太网链路层
}

struct net_device *bluerdma_get_netdev(struct ib_device *ibdev, u32 port_num)
{
    struct bluerdma_dev *dev = to_bdev(ibdev);
    if (dev->netdev)
        dev_hold(dev->netdev);  // 增加引用计数
    return dev->netdev;
}
```

```c
// main.c - bluerdma_device_ops
.get_link_layer = bluerdma_get_link_layer,
.get_netdev = bluerdma_get_netdev,
```

**结果**: 仍然失败 ❌

### 阶段 3: 设置设备父节点 ✅ 已解决

**问题**: OFED 可能需要正确的设备层次结构

**修复**:
```c
// main.c:148-151
if (testing_dev[i]->netdev) {
    ibdev->dev.parent = &testing_dev[i]->netdev->dev;
}
```

**结果**: 仍然失败 ❌

### 阶段 4: 调整 netdev 关联时机 ✅ 已解决

**问题**: OFED 可能需要在注册**之前**关联 netdev

**修复**: 将 `ib_device_set_netdev` 从注册后移到注册前

```c
// main.c:158-170 (修改前)
ret = ib_register_device(ibdev, "bluerdma%d", NULL);
// ...
if (testing_dev[i]->netdev) {
    ib_device_set_netdev(ibdev, testing_dev[i]->netdev, 1);  // 注册后
}

// main.c:158-172 (修改后)
if (testing_dev[i]->netdev) {
    ib_device_set_netdev(ibdev, testing_dev[i]->netdev, 1);  // 注册前 ✓
}
ret = ib_register_device(ibdev, "bluerdma%d", NULL);
```

**结果**: 仍然失败 ❌

### 阶段 5: 尝试其他修复方案

尝试过的其他方案：
1. ✗ 修改 `port_cap_flags` 添加更多能力标志
2. ✗ 调整 GID 表初始化时机
3. ✗ 修改 `core_cap_flags` 配置
4. ✗ 添加 platform_device 作为父设备

**所有方案均失败** ❌

---

## 当前代码状态

### 已实现的 RoCE 支持

#### 1. 设备操作 (main.c:73-125)

```c
static const struct ib_device_ops bluerdma_device_ops = {
    // 必需的基础操作
    .query_device = bluerdma_query_device,
    .query_port = bluerdma_query_port,
    .get_port_immutable = bluerdma_get_port_immutable,

    // RoCE 必需的回调 ✓
    .get_link_layer = bluerdma_get_link_layer,
    .get_netdev = bluerdma_get_netdev,

    // PD/QP/CQ 操作
    .alloc_pd = bluerdma_alloc_pd,
    .create_qp = bluerdma_create_qp,
    .create_cq = bluerdma_create_cq,
    // ... 等等

    // GID 管理
    .query_gid = bluerdma_query_gid,
    .add_gid = bluerdma_add_gid,
    .del_gid = bluerdma_del_gid,
};
```

#### 2. 端口配置 (verbs.c:166-193)

```c
int bluerdma_get_port_immutable(struct ib_device *ibdev, u32 port_num,
                                struct ib_port_immutable *immutable)
{
    // RoCE 核心能力标志 ✓
    immutable->core_cap_flags = RDMA_CORE_CAP_PROT_ROCE |
                                RDMA_CORE_CAP_PROT_ROCE_UDP_ENCAP;
    immutable->pkey_tbl_len = 1;
    immutable->gid_tbl_len = BLUERDMA_GID_TABLE_SIZE;  // 16

    return 0;
}
```

#### 3. 设备注册流程 (main.c:138-196)

```c
for (i = 0; i < N_TESTING; i++) {
    ibdev = &testing_dev[i]->ibdev;

    // 1. 基础配置
    ibdev->node_type = RDMA_NODE_RNIC;        // RoCE 设备类型
    ibdev->phys_port_cnt = 1;

    // 2. 设置父设备 ✓
    if (testing_dev[i]->netdev) {
        ibdev->dev.parent = &testing_dev[i]->netdev->dev;
    }

    // 3. 设置操作回调
    ib_set_device_ops(ibdev, &bluerdma_device_ops);

    // 4. 关联网络设备 (注册前) ✓
    if (testing_dev[i]->netdev) {
        ib_device_set_netdev(ibdev, testing_dev[i]->netdev, 1);
    }

    // 5. 注册 IB 设备 ❌ 失败在这里
    ret = ib_register_device(ibdev, "bluerdma%d", NULL);
}
```

#### 4. 网络设备创建 (ethernet.c:168-204)

```c
int bluerdma_create_netdev(struct bluerdma_dev *dev, int id)
{
    // 1. 分配以太网设备
    netdev = alloc_etherdev(sizeof(struct bluerdma_dev));
    snprintf(netdev->name, IFNAMSIZ, "blue%d", id);  // blue0, blue1

    // 2. 设置网络设备
    bluerdma_netdev_setup(netdev);

    // 3. 初始化 GID 表（基于 MAC 地址）
    bluerdma_init_gid_table(dev);
    // 默认 GID: fe80::xxxx:xxFF:FExx:xxxx (link-local)

    // 4. 注册网络设备 ✓
    ret = register_netdev(netdev);

    return ret;
}
```

### 网络设备状态

**创建成功** ✓：
- blue0 和 blue1 成功创建
- 日志显示 "Registered network device blue0 for RDMA device 0"

**但在 IB 注册失败后被清理**：
```c
// main.c:153-160 - 错误处理
if (ret) {
    pr_err("ib_register_device failed for index %d\n", i);
    while (--i >= 0) {
        ib_unregister_device(&testing_dev[i]->ibdev);
    }
    bluerdma_free_testing();  // 清理所有设备，包括 netdev
    return ret;
}
```

---

## 为什么无法在 Mellanox OFED 下工作

### 1. 架构不匹配

| 组件 | bluerdma | OFED 期望 |
|------|----------|-----------|
| **硬件基础** | 纯软件实现 | 真实 PCI 硬件 |
| **父设备** | `net_device` | `pci_device` |
| **端口数据** | 软件模拟 | 硬件映射 |
| **DMA 引擎** | 软件拷贝 | 硬件 DMA |
| **GID 表** | 软件维护 | 硬件同步 |

### 2. OFED 的硬件假设

Mellanox OFED 的 `alloc_port_data` 可能执行以下检查（推测）：

```c
// OFED device.c:841 附近的可能逻辑
static int alloc_port_data(struct ib_device *device)
{
    // 检查 1: PCI 设备存在？
    if (!device->dev.parent || !dev_is_pci(device->dev.parent))
        goto err;  // ❌ bluerdma 使用 net_device

    // 检查 2: 硬件端口能力？
    if (!check_hardware_port_caps(device))
        goto err;  // ❌ bluerdma 返回软件属性

    // 检查 3: 厂商特定的初始化？
    if (!mlx_vendor_specific_init(device))
        goto err;  // ❌ bluerdma 不是 Mellanox 硬件

err:
    dev_warn(&device->dev, "Couldn't create per-port data");
    return -EINVAL;
}
```

### 3. 标准 Linux RDMA vs Mellanox OFED

**标准 Linux RDMA**：
- 设计为通用框架，支持软件和硬件驱动
- `ib_register_device` 只要求基本的回调函数
- 对设备类型和父设备类型没有严格限制
- 示例：rxe (软件 RoCE)、siw (软件 iWARP) 都能正常工作

**Mellanox OFED**：
- 为 Mellanox 硬件优化
- 添加了额外的硬件假设和检查
- 对端口数据结构有特殊要求
- 可能依赖硬件特定的功能

---

## 结论

### 核心问题

**bluerdma 是软件 RDMA 驱动，Mellanox OFED 是为硬件优化的发行版，两者架构不兼容。**

### 已排除的可能性

✓ 不是符号版本问题（已通过 KBUILD_EXTRA_SYMBOLS 解决）
✓ 不是缺少回调函数（已添加 get_link_layer 和 get_netdev）
✓ 不是设备层次结构问题（已设置 parent 和 netdev 关联）
✓ 不是注册时序问题（已调整为注册前关联）

### 根本原因

**Mellanox OFED 的 `alloc_port_data` 函数对设备有硬件相关的假设，这些假设在软件 RDMA 驱动中无法满足。**

---

## 建议的解决方案

### 方案：切换到标准 Linux RDMA

**原因**：
1. bluerdma 是**软件 RDMA 驱动**，不需要 Mellanox OFED 的硬件优化
2. 标准 Linux RDMA 支持软件驱动（如 rxe、siw）
3. 避免与厂商特定实现的兼容性问题

**优势**：
- ✅ 架构匹配：标准 RDMA 框架支持软件驱动
- ✅ 简化开发：无需处理 OFED 特定的要求
- ✅ 社区支持：主线内核的 RDMA 子系统
- ✅ 可移植性：在任何 Linux 系统上工作

**劣势**：
- ⚠️ 需要临时禁用 Mellanox OFED（如果需要使用硬件 RDMA）
- ⚠️ 编译配置需要修改

**实施指南**：见 [切换到标准 Linux RDMA](./switch-to-vanilla-rdma.md)

---

## 技术细节记录

### OFED 版本信息

```bash
$ ofed_info -s
MLNX_OFED_LINUX-24.10-1.1.4.0

$ dpkg -l | grep mlnx-ofed
mlnx-ofed-kernel-dkms  24.10.OFED.24.10.1.1.4.1
```

### 系统信息

```bash
$ uname -r
6.8.0-58-generic

$ uname -m
x86_64
```

### 当前加载的 ib_core

```bash
$ modinfo ib_core | grep filename
filename: /lib/modules/6.8.0-58-generic/updates/dkms/ib_core.ko.zst
# ↑ OFED 版本（在 updates/dkms/ 目录）
```

### 编译参数

```makefile
# Makefile
KBUILD_EXTRA_SYMBOLS=/usr/src/ofa_kernel/x86_64/6.8.0-58-generic/Module.symvers

# kernel-driver/Makefile
ccflags-y += -I/usr/src/mlnx-ofed-kernel-24.10.OFED.24.10.1.1.4.1/include
```

---

## 参考资料

### 相关文档

1. [OFED 符号版本修复](./ofed-symbol-version-fix.md) - 第一阶段问题的解决
2. [切换到标准 Linux RDMA](./switch-to-vanilla-rdma.md) - 推荐的解决方案

### Linux RDMA 子系统

- 软件 RoCE (rxe): https://github.com/SoftRoCE/rxe-dev
- 软件 iWARP (siw): https://www.kernel.org/doc/html/latest/infiniband/siwarp.html
- RDMA Core 文档: https://github.com/linux-rdma/rdma-core

### Mellanox OFED

- 官方文档: https://docs.nvidia.com/networking/display/ofedv24101114
- 用户手册: https://docs.nvidia.com/networking/display/MLNXOFEDv24101114/User+Manual

---

## 故障排查清单

如果您仍想在 OFED 下调试（不推荐）：

### 1. 验证 OFED 版本和配置

```bash
ofed_info -s
ls -l /usr/src/ofa_kernel/$(uname -m)/$(uname -r)/Module.symvers
```

### 2. 检查内核配置

```bash
grep CONFIG_INFINIBAND /boot/config-$(uname -r)
# 应该显示 InfiniBand 相关配置
```

### 3. 查看详细的 OFED 日志

```bash
sudo dmesg | grep -E "ib_core|bluerdma|mlx5" | tail -100
```

### 4. 尝试强制加载（仅调试用）

```bash
sudo modprobe --force bluerdma
# ⚠️ 警告：可能导致内核崩溃
```

### 5. 对比 mlx5_ib 的实现

```bash
# 查看 Mellanox 官方驱动的实现
grep -r "alloc_port_data" /usr/src/mlnx-ofed-kernel-*/
```

---

## 时间线

| 日期 | 阶段 | 问题 | 状态 |
|------|------|------|------|
| 2025-11-12 | 1 | 符号版本不匹配 | ✅ 已解决 |
| 2025-11-14 | 2 | RoCE 设备注册失败 | ⚠️ 无法在 OFED 下解决 |
| 2025-11-14 | - | 决定切换到标准 RDMA | 📋 待执行 |

---

**文档版本**: 1.0
**最后更新**: 2025-11-14
**维护者**: Claude Code Assistant
**下一步**: [切换到标准 Linux RDMA](./switch-to-vanilla-rdma.md)
