# 切换到主线 Linux RDMA - 快速操作指南

**日期**: 2025-11-14
**目标**: 使 bluerdma 在标准 Linux RDMA 子系统下运行
**状态**: ✅ 已验证

---

## 快速操作步骤

### 1. 卸载 OFED 模块

```bash
# 停止 openibd 服务（防止自动重新加载）
sudo /etc/init.d/openibd stop

# 卸载所有 RDMA 模块（包括 OFED 特有的 mlx_compat）
sudo modprobe -r mlx_compat
sudo modprobe -r rdma_ucm rdma_cm iw_cm ib_ipoib ib_umad ib_uverbs ib_cm mlx5_ib mlx5_core ib_core

# 验证卸载成功
cat /proc/modules | grep ib_core
# 应该没有输出
```

### 2. 使用 insmod 加载主线 ib_core

```bash
# ✅ 已验证：直接使用 insmod 加载主线版本
sudo insmod /lib/modules/$(uname -r)/kernel/drivers/infiniband/core/ib_core.ko.zst
# 这个模块用于暴露 uverbs，以便用户态ibverbs可以识别到设备
sudo insmod /lib/modules/$(uname -r)/kernel/drivers/infiniband/core/ib_uverbs.ko.zst
```

**关键**: 必须使用 `insmod` 而非 `modprobe`，因为 modprobe 会优先加载 OFED 版本。

### 3. 验证主线版本已加载

```bash
# 检查模块状态（关键：应该没有 (OE) 标记）
cat /proc/modules | grep ib_core
# 预期输出: ib_core 401408 0 - Live 0x0000000000000000
#          ↑ 注意：没有 (OE) 标记

# 检查 mlx_compat（主线版本不应该有）
lsmod | grep mlx_compat
# 应该没有输出
```

**对比表**:
| 检查项 | OFED 版本 | 主线版本 |
|-------|----------|---------|
| `/proc/modules` 标记 | `(OE)` | 无标记 |
| `mlx_compat` 模块 | ✅ 存在 | ❌ 不存在 |
| 模块路径 | `updates/dkms/` | `kernel/drivers/` |


---

## 为什么需要这样做？

### 问题根源

| 组件 | Mellanox OFED | 标准 Linux RDMA |
|------|--------------|----------------|
| **设计目标** | Mellanox 硬件优化 | 通用 RDMA 框架 |
| **硬件假设** | 期望真实 PCI 设备 | 支持软件驱动 |
| **bluerdma 兼容性** | ❌ 不兼容 | ✅ 兼容 |

bluerdma 是**软件 RDMA 驱动**（无硬件依赖），必须使用标准 Linux RDMA 子系统。

### 关键技术点

1. **modprobe vs insmod**:
   - `modprobe ib_core` → 优先加载 `updates/dkms/` 下的 OFED 版本
   - `insmod /path/to/ib_core.ko.zst` → 直接加载指定路径的模块

2. **模块标记含义**:
   - `(OE)` = Out-of-tree + Unsigned（OFED 等外部模块）
   - 无标记 = 内核主线模块

3. **mlx_compat**:
   - OFED 特有的兼容层模块
   - 主线内核不存在此模块
   - 其存在直接证明运行的是 OFED 版本

---

## 相关文档

- [OFED 符号版本修复](./ofed-symbol-version-fix.md) - 第一阶段问题
- [OFED RoCE 注册问题](./ofed-roce-registration-issue.md) - 第二阶段问题（架构不兼容）

---

**文档版本**: 1.0
**最后更新**: 2025-11-14
**验证状态**: ✅ 已在 Ubuntu 6.8.0-58-generic 上验证
