# 去除 Shared Memory 的仿真模式设计

## 1. 背景与动机

### 1.1 当前仿真模式的限制

当前的仿真模式采用共享内存(shared memory)机制:仿真器将 PCIe 请求转换为对共享内存的直接修改。这种方式虽然简单,但存在以下局限性:

- 与真实硬件行为差异较大
- 无法准确模拟地址转换过程
- 难以检测内存越界访问

### 1.2 新设计方案

本设计提出一种更贴近真实硬件的仿真方式:

- 仿真器通过 **TCP** 将 PCIe 请求转发给驱动
- 驱动端执行**真实的内存读写**操作
- 读请求的结果通过网络回传给仿真器

**核心优势:**
- 更接近真实硬件的内存访问流程
- 可以完整模拟虚拟地址到物理地址的映射关系
- 提供内存访问边界检查能力

---

## 2. 地址映射问题分析

### 2.1 简化场景:固定偏移量映射

如果不考虑虚拟地址(VA)和物理地址(PA)的复杂映射关系,或者简单地假设两者只存在一个固定偏移量关系,则实现相对简单:

- 驱动端只需增加一个**线程**处理来自仿真器的 PCIe 请求
- 地址转换通过简单的加/减偏移量完成

### 2.2 真实场景:完整的 VA ↔ PA 映射

为了更贴近真实硬件行为,需要处理完整的虚拟地址到物理地址映射关系。

#### 2.2.1 VA → PA 的实现

驱动已经实现了通过 `/proc/<pid>/pagemap` 进行 VA → PA 转换(参见 `src/mem/virt_to_phy.rs`)。

#### 2.2.2 PA → VA 的挑战

当仿真器发送 PCIe 请求时,目标地址是**物理地址**,但驱动需要将其映射回**虚拟地址**才能访问实际内存。

**问题:** Linux 没有提供标准的 PA → VA 查询接口。

**可能的解决方案:**
- ❌ 暴力搜索 `/proc/<pid>/pagemap` 中的映射表(效率低)
- ✅ **维护一个双向映射表**(推荐)

---

## 3. 双向映射表设计

### 3.1 设计思路

建立一个 **PA ↔ VA 双向映射表**,在仿真模式下记录所有可能被仿真器访问的内存地址映射关系。

**优点:**
1. 高效的 PA → VA 查询
2. 可以检测仿真器的越界访问
3. 对硬件模式无影响(通过 feature flag 控制)

### 3.2 需要映射的内存类型

驱动中有两类内存可能被仿真器访问:

| 内存类型 | 生命周期 | 分配/释放时机 | 映射管理难度 |
|---------|---------|--------------|-------------|
| **DMA Buffer** | 随驱动进程 | 初始化时分配,驱动终止时释放 | 中等 |
| **Memory Region (MR)** | 用户控制 | 注册时 pin,解除注册时 unpin | 简单 |

---

## 4. 实现方案

### 4.1 MR 地址的映射管理

Memory Region 的管理相对简单,因为其生命周期明确:

```rust
// 伪代码示例
trait MemoryRegion {
    #[cfg(feature = "sim")]
    fn pin(&mut self, va: u64, size: usize) {
        let pa = virt_to_phys(va);
        BIDIRECTIONAL_MAP.insert(pa, va, size);
        // ... 原有逻辑
    }

    #[cfg(feature = "sim")]
    fn unpin(&mut self, va: u64, size: usize) {
        let pa = virt_to_phys(va);
        BIDIRECTIONAL_MAP.remove(pa, size);
        // ... 原有逻辑
    }
}
```

**关键点:**
- 在 `pin()` 时添加 PA ↔ VA 映射
- 在 `unpin()` 时删除映射
- 仅在仿真模式(`#[cfg(feature = "sim")]`)下启用

### 4.2 DMA Buffer 的映射管理

#### 4.2.1 现有架构

```rust
// 当前代码结构
trait PageAllocator {
    fn alloc(&mut self) -> io::Result<ContiguousPages>;
}

struct DmaBuf {
    ptr: *mut u8,
    size: usize,
}

impl Drop for DmaBuf {
    fn drop(&mut self) {
        unsafe { libc::munmap(self.ptr, self.size); }
    }
}
```

**挑战:**
- DMA Buffer 申请通过 trait 实现,可以在申请时 hook
- 但**析构函数固定**为 `libc::munmap`,难以在释放时更新映射表

#### 4.2.2 解决方案:预留空间映射

由于 DMA Buffer 的生命周期等同于驱动进程(永不主动释放),可以采用以下策略:

**实现要点:**
1. 在仿真模式下,为所有 DMA Buffer **预留一段虚拟地址空间**
2. 在预留空间**申请时**一次性建立完整的 PA ↔ VA 映射
3. 确保该空间在驱动运行期间**永不 unmap**

**优点:**
- 避免修改 `DmaBuf` 的 Drop 实现
- 符合 DMA Buffer 的实际使用模式

### 4.3 物理地址连续性问题

DMA Buffer 需要**物理地址连续**以满足硬件 DMA 传输要求。

#### 4.3.1 硬件模式的实现

硬件模式通过 **u-dma-buf** 内核模块保证物理地址连续性(参见 `src/mem/dmabuf.rs`)。

#### 4.3.2 当前的仿真模式的选择

当前仿真模式假设 PA = VA + 固定偏移量(参见 `src/mem/virt_to_phy.rs:PhysAddrResolverEmulated`)。

引入双向映射表后,有两种方案:

##### **方案 A: 使用 u-dma-buf**

```
优点: 保证物理地址真实连续,与硬件行为完全一致
缺点: 仿真模式也依赖内核模块,增加部署复杂度
适用: 需要高度仿真真实性的场景
```

##### **方案 B: 虚拟物理地址映射** (推荐)

```
实现: 将 DMA Buffer 映射到不存在的物理地址区域
示例: 如果真实物理内存最大为 16GB,则将 DMA Buffer
      映射到 16GB + 偏移量的地址空间

优点:
  - 无需内核模块依赖
  - 保持仿真模式的轻量级特性

注意事项:
  - 需要明确文档说明该地址为"虚拟物理地址"
  - 修改时需谨慎处理地址范围边界
  - 确保不与真实物理地址冲突
```

**推荐配置示例:**

```rust
#[cfg(feature = "sim")]
const SIMULATED_PHYS_BASE: u64 = 0x4_0000_0000; // 16GB
const DMABUF_REGION_SIZE: u64 = 0x1000_0000;    // 256MB

fn map_dmabuf_to_fake_phys(va: u64, offset: u64) -> u64 {
    SIMULATED_PHYS_BASE + offset
}
```

---

## 5. 实现路线图

### 5.1 Phase 1: 基础设施

- [ ] 实现双向映射表数据结构(建议使用 `HashMap` + `RangeMap`)
- [ ] 在 `src/mem/` 下添加 `pa_va_map.rs` 模块
- [ ] 添加 `#[cfg(feature = "sim")]` 条件编译保护

### 5.2 Phase 2: MR 映射集成

- [ ] 修改 MR 的 `pin`/`unpin` 实现
- [ ] 添加映射表更新逻辑
- [ ] 编写单元测试验证映射正确性

### 5.3 Phase 3: DMA Buffer 映射

- [ ] 确定物理地址分配策略(方案 A 或 B)
- [ ] 在预留空间申请时初始化映射表
- [ ] 验证 DMA Buffer 的地址转换正确性

### 5.4 Phase 4: PCIe 请求处理线程

- [ ] 实现 TCP 监听线程接收仿真器请求
- [ ] 使用双向映射表完成 PA → VA 转换
- [ ] 执行实际内存读写
- [ ] 通过 TCP 回传结果

### 5.5 Phase 5: 测试与验证

- [ ] 单元测试:地址映射正确性
- [ ] 集成测试:与仿真器通信
- [ ] 边界测试:越界访问检测
- [ ] 性能测试:映射表查询开销

---

## 6. 关键代码位置

| 功能 | 文件路径 |
|------|---------|
| VA → PA 转换 | `src/mem/virt_to_phy.rs` |
| DMA Buffer 分配 | `src/mem/dmabuf.rs` |
| u-dma-buf 实现 | `src/mem/u_dma_buf.rs` |
| MR 管理 | `src/verbs/core.rs` (reg_mr/dereg_mr) |

---

## 7. 未来优化方向

1. **性能优化:**
   - 使用更高效的映射表数据结构(如 B-Tree)
   - 考虑引入 TLB 缓存机制

2. **调试支持:**
   - 添加映射表状态导出功能
   - 提供内存访问日志记录

3. **扩展性:**
   - 支持多进程场景的地址隔离
   - 考虑支持非连续物理内存的 DMA Buffer

---

## 8. 参考资料

- Linux Pagemap Documentation: `/proc/<pid>/pagemap` 格式说明
- 现有实现: `src/mem/virt_to_phy.rs:PhysAddrResolverLinuxX86`
- u-dma-buf 内核模块文档