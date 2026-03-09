# BlueRDMA 模块与 Mellanox OFED 符号版本不匹配问题解决方案

**日期**: 2025-11-12
**问题**: bluerdma.ko 模块加载失败，报错 "Invalid parameters"
**状态**: ✅ 已解决

---

## 问题现象

### 错误信息

```bash
$ sudo insmod build/bluerdma.ko
insmod: ERROR: could not insert module build/bluerdma.ko: Invalid parameters
```

### 内核日志 (dmesg)

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

## 根本原因分析

### 1. 符号版本机制 (CONFIG_MODVERSIONS)

Linux 内核使用 **CRC32 校验和**来确保模块间二进制接口 (ABI) 的兼容性：

- 每个导出符号都有一个基于其定义（函数签名、结构体布局等）计算的 CRC32 值
- 模块加载时，内核会验证模块期望的符号 CRC 是否与内核提供的符号 CRC 匹配
- CRC 不匹配表示 ABI 不兼容，加载会失败并报错 `-EINVAL` (err -22)

### 2. Mellanox OFED vs 内核原生 InfiniBand

系统安装了 **Mellanox OFED (mlnx-ofed-kernel-dkms 24.10.OFED.24.10.1.1.4.1)**，它提供了增强版的 InfiniBand/RDMA 驱动：

**模块加载优先级**：
```
1. /lib/modules/6.8.0-58-generic/updates/dkms/    ← OFED 模块（最高优先级）
2. /lib/modules/6.8.0-58-generic/updates/
3. /lib/modules/6.8.0-58-generic/kernel/          ← 内核原生模块
```

**符号版本差异**：

| 符号 | 内核原生 CRC | OFED CRC | bluerdma 编译使用 |
|------|-------------|----------|------------------|
| `ib_register_device` | `0x42ba7635` | `0xb78db345` | `0x42ba7635` ❌ |
| `ib_unregister_device` | `0x7d5ab2b1` | `0x429631a6` | `0x7d5ab2b1` ❌ |
| `ib_device_set_netdev` | `0x5f3d9143` | `0xe01d532e` | `0x5f3d9143` ❌ |
| `ib_query_port` | `0x8c5a5c42` | `0x7996ccf7` | `0x8c5a5c42` ❌ |

**为什么 CRC 不同？**
- Mellanox OFED 修改了函数签名或相关结构体以支持厂商特性
- OFED 使用了不同的 backport 兼容层
- OFED 针对性能和功能进行了优化

### 3. bluerdma 编译过程问题

**编译阶段 (*.c → *.o)**：
```bash
ccflags-y += -I/usr/src/mlnx-ofed-kernel-24.10.OFED.24.10.1.1.4.1/include
```
✅ **正确**：使用 OFED 头文件编译源代码

**链接阶段 (modpost)**：
```bash
# 默认使用内核的 Module.symvers
/usr/src/linux-headers-6.8.0-58-generic/Module.symvers
```
❌ **错误**：生成的 `bluerdma.mod.c` 包含**内核原生版本**的符号 CRC

**运行时加载**：
```
bluerdma.ko 期望符号 CRC: 0x42ba7635 (内核版本)
实际加载的 ib_core.ko 提供: 0xb78db345 (OFED 版本)
```
❌ **不匹配**：加载失败！

---

## 解决方案

### 核心思路

使用 **KBUILD_EXTRA_SYMBOLS** 机制，在编译时指定 OFED 的 `Module.symvers` 文件，确保 modpost 工具使用正确的符号 CRC 值。

### 修改的文件

#### 1. 根目录 Makefile

**文件**: `/home/peng/projects/rdma_all/blue-rdma-driver/Makefile`

**修改内容**：

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

#### 2. kernel-driver 子目录 Makefile

**文件**: `/home/peng/projects/rdma_all/blue-rdma-driver/kernel-driver/Makefile`

**修改内容**：

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

## 工作原理

### KBUILD_EXTRA_SYMBOLS 机制

内核构建系统的 `scripts/Makefile.modpost` 处理逻辑：

```makefile
# 如果定义了 KBUILD_EXTRA_SYMBOLS，添加到 modpost 参数
modpost-args += -e $(addprefix -i , $(KBUILD_EXTRA_SYMBOLS))
```

**modpost 工具的处理流程**：

1. 读取 `KBUILD_EXTRA_SYMBOLS` 指定的 `Module.symvers` 文件
2. 为外部模块的符号使用这些 CRC 值（而不是内核默认的）
3. 生成包含正确 CRC 的 `*.mod.c` 文件
4. 编译生成最终的 `*.ko` 文件

### 完整的编译流程

```
编译时：
[OFED headers] → [gcc] → main.o, verbs.o, ethernet.o
                           |
                           ↓
[OFED Module.symvers] → [modpost] → bluerdma.mod.c (包含 OFED CRC)
                                      |
                                      ↓
                                   bluerdma.ko

运行时：
[insmod bluerdma.ko]
  |
  ↓
检查依赖: ib_core
  |
  ↓
加载 /lib/modules/.../updates/dkms/ib_core.ko.zst (OFED 版本)
  |
  ↓
验证符号: ib_register_device
  bluerdma.ko 期望 CRC: 0xb78db345 ✓
  ib_core.ko 提供 CRC:  0xb78db345 ✓
  |
  ↓
加载成功！
```

---

## 验证步骤

### 1. 编译验证

```bash
cd /home/peng/projects/rdma_all/blue-rdma-driver
make clean
make
```

**预期输出**：
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

### 2. 符号 CRC 验证

进入 kernel-driver 目录验证符号版本：

```bash
cd kernel-driver

# 检查 bluerdma.mod.c 中的符号 CRC
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

print("符号 CRC 验证:")
all_correct = True
for sym, expected_crc in symbols.items():
    idx = data.find(sym.encode() + b'\x00')
    if idx > 4:
        actual_crc = struct.unpack('<I', data[idx-4:idx])[0]
        match = "✓" if actual_crc == expected_crc else "✗"
        print(f"{match} {sym}: 0x{actual_crc:08x} (期望: 0x{expected_crc:08x})")
        if actual_crc != expected_crc:
            all_correct = False

if all_correct:
    print("\n✓✓✓ 所有符号 CRC 正确！使用了 OFED 版本")
else:
    print("\n✗✗✗ 符号 CRC 不正确！仍在使用内核版本")
EOF
```

**预期输出**：
```
符号 CRC 验证:
✓ ib_register_device: 0xb78db345 (期望: 0xb78db345)
✓ ib_unregister_device: 0x429631a6 (期望: 0x429631a6)
✓ ib_device_set_netdev: 0xe01d532e (期望: 0xe01d532e)
✓ ib_query_port: 0x7996ccf7 (期望: 0x7996ccf7)

✓✓✓ 所有符号 CRC 正确！使用了 OFED 版本
```

### 3. 模块加载测试

```bash
cd /home/peng/projects/rdma_all/blue-rdma-driver
sudo make install
```

**预期结果**：
- ✅ 不再报告 "Invalid parameters" 错误
- ✅ 不再出现 "disagrees about version of symbol" 内核日志
- ✅ 模块成功加载
- ✅ `/dev/infiniband/` 下创建了设备节点

---

## 关键要点总结

### 问题的本质

bluerdma 使用了 OFED 的**头文件**进行编译，但使用了内核原生的**符号版本信息** (Module.symvers)，导致运行时与实际加载的 OFED 模块符号版本不匹配。

### 解决方案的本质

通过 `KBUILD_EXTRA_SYMBOLS` 参数，在编译链接阶段使用 OFED 的 Module.symvers，确保生成的模块期望的符号 CRC 与实际运行的 OFED 模块提供的 CRC 一致。

### 关键文件位置

| 文件类型 | 路径 | 用途 |
|---------|------|------|
| OFED 头文件 | `/usr/src/mlnx-ofed-kernel-24.10.OFED.24.10.1.1.4.1/include/` | 编译时使用 |
| OFED Module.symvers | `/usr/src/ofa_kernel/x86_64/6.8.0-58-generic/Module.symvers` | 符号 CRC 定义 |
| OFED 运行时模块 | `/lib/modules/6.8.0-58-generic/updates/dkms/ib_core.ko.zst` | 实际加载的模块 |
| 内核头文件 | `/usr/src/linux-headers-6.8.0-58-generic/` | 编译框架 |
| 内核 Module.symvers | `/usr/src/linux-headers-6.8.0-58-generic/Module.symvers` | 内核原生符号（不应使用） |

### 修改要点

1. **根目录 Makefile**: 在 `bluerdma` 目标中添加 `KBUILD_EXTRA_SYMBOLS=$(OFED_SYMVERS)`
2. **kernel-driver/Makefile**:
   - 定义 `OFED_SYMVERS` 变量
   - 在 `bluerdma.ko` 目标中添加 `KBUILD_EXTRA_SYMBOLS=$(OFED_SYMVERS)`
   - 更新 `clean` 目标使用内核清理机制

---

## 参考资料

### 相关内核文档

- [Module versioning & Module.symvers](https://www.kernel.org/doc/html/latest/kbuild/modules.html#symbols-from-another-external-module)
- [CONFIG_MODVERSIONS](https://cateee.net/lkddb/web-lkddb/MODVERSIONS.html)

### Mellanox OFED

- **版本**: MLNX_OFED_LINUX-24.10-1.1.4.0
- **安装包**: mlnx-ofed-kernel-dkms 24.10.OFED.24.10.1.1.4.1
- **官方文档**: https://docs.nvidia.com/networking/display/ofedv24101114

### DKMS (Dynamic Kernel Module Support)

- DKMS 模块会在 `/lib/modules/$(uname -r)/updates/dkms/` 中安装
- 优先级高于内核原生模块
- 每次内核更新后会自动重新编译

---

## 后续注意事项

### 1. 内核更新

内核更新后，如果 OFED 版本保持不变：
- OFED 会自动为新内核编译（通过 DKMS）
- bluerdma 需要针对新内核重新编译
- Module.symvers 路径会变化（包含内核版本号）

### 2. OFED 更新

OFED 更新可能改变符号 CRC，需要：
- 重新编译 bluerdma
- 验证符号版本匹配
- 测试模块加载

### 3. 可移植性

这个解决方案依赖于：
- OFED 已正确安装
- Module.symvers 文件存在于预期路径
- 系统架构为 x86_64

在其他系统上部署时需要确认这些前提条件。

---

## 故障排查指南

### 如果仍然出现符号不匹配错误

1. **检查 OFED Module.symvers 是否存在**：
   ```bash
   ls -lh /usr/src/ofa_kernel/$(uname -m)/$(uname -r)/Module.symvers
   ```

2. **验证 OFED 模块是否被加载**：
   ```bash
   lsmod | grep ib_core
   modinfo ib_core | grep filename
   # 应该显示 /lib/modules/.../updates/dkms/ib_core.ko.zst
   ```

3. **检查编译时是否使用了正确的 KBUILD_EXTRA_SYMBOLS**：
   ```bash
   # 编译时应该看到类似输出：
   # KBUILD_EXTRA_SYMBOLS=/usr/src/ofa_kernel/x86_64/6.8.0-58-generic/Module.symvers
   ```

4. **清理并重新编译**：
   ```bash
   make clean
   rm -rf build/
   make
   ```

### 紧急临时方案（不推荐生产环境）

如果需要紧急测试功能（绕过版本检查）：

```bash
sudo modprobe --force-modversion bluerdma
```

**警告**: 这会丧失 ABI 兼容性检查，可能导致运行时崩溃！仅用于调试验证。

---

## 后续问题

### RoCE 设备注册失败

符号版本问题解决后，遇到了新的问题：**ib_register_device 失败**

**问题现象**:
```
WARNING at device.c:841 alloc_port_data+0x10c/0x130 [ib_core]
infiniband bluerdma0: Couldn't create per-port data
ib_register_device failed for index 0
```

**已尝试的修复**:
1. ✅ 添加 RoCE 必需的回调函数 (`get_link_layer`, `get_netdev`)
2. ✅ 设置设备父节点 (`ibdev->dev.parent`)
3. ✅ 调整 netdev 关联时机（注册前关联）
4. ❌ 所有修复均无效

**根本原因**:
- Mellanox OFED 是为**硬件 RDMA 设备**设计的
- bluerdma 是**软件 RDMA 驱动**，架构不匹配
- OFED 的 `alloc_port_data` 对设备有硬件相关的假设

**解决方案**:
切换到**标准 Linux RDMA 子系统**，它支持软件 RDMA 驱动（如 rxe、siw）

**详细文档**: [OFED RoCE 注册问题](./ofed-roce-registration-issue.md)

**迁移指南**: [切换到标准 Linux RDMA](./switch-to-vanilla-rdma.md)

---

**文档版本**: 1.1
**最后更新**: 2025-11-14
**维护者**: Claude Code Assistant
