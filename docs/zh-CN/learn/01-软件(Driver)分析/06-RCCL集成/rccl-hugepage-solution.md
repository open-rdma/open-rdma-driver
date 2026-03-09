# RCCL CPU 内存大页解决方案分析

## 问题背景

### 当前状况
- **驱动要求**：注册到 RDMA 网卡的 MR（Memory Region）必须使用 2MB 大页
- **RCCL 行为**：在不满足 GPU Direct RDMA 条件时，使用 CPU 内存作为中转缓冲区
- **核心矛盾**：RCCL 默认使用标准内存分配（4KB 页），不满足驱动的 2MB 大页要求

### RCCL 内存注册流程

根据 [NCCL Net Plugin 文档](https://rocm.docs.amd.com/projects/rccl/en/develop/how-to/using-nccl.html)，RCCL 的内存注册流程如下：

```
1. RCCL 分配缓冲区（CPU 或 GPU 内存）
   └─> posix_memalign() 或类似函数

2. 调用 NET Plugin 的 regMr() 函数
   └─> 最终调用 ibv_reg_mr()

3. 使用 mhandle 标识已注册的内存
   └─> 用于后续 isend/irecv 操作
```

**关键点**：
- RCCL 在调用 `regMr()` 之前已经分配好内存
- 内存类型通过 `NCCL_PTR_HOST` 或 `NCCL_PTR_CUDA` 标识
- 对于 CPU 内存，使用标准的 `posix_memalign()` 分配

## 解决方案对比

### 方案 1：LD_PRELOAD 劫持内存分配函数 ✅ **推荐**

#### 原理
通过 `LD_PRELOAD` 替换 `posix_memalign`、`malloc`、`free` 等函数，使其自动分配 2MB 大页内存。

#### 实现方式

**方式 A：使用现有库 - libhugetlbfs**
```bash
# 安装 libhugetlbfs
apt-get install libhugetlbfs-dev  # Debian/Ubuntu
yum install libhugetlbfs-devel     # RHEL/CentOS

# 配置大页
echo 1024 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages

# 运行 RCCL 程序
LD_PRELOAD=libhugetlbfs.so \
HUGETLB_MORECORE=yes \
HUGETLB_DEFAULT_PAGE_SIZE=2M \
./rccl_application
```

**优点**：
- 无需编写代码，开箱即用
- 经过充分测试，稳定可靠
- 支持多种分配函数（malloc/calloc/posix_memalign）

**缺点**：
- 系统需要安装额外依赖
- 配置选项较多，可能需要调试

---

**方式 B：自定义 Wrapper 库（更灵活）**

创建一个轻量级的 wrapper 库，精确控制大页分配行为。

```c
// hugepage_allocator.c
#define _GNU_SOURCE
#include <stdlib.h>
#include <sys/mman.h>
#include <string.h>
#include <errno.h>
#include <stdio.h>
#include <dlfcn.h>

#define HUGE_PAGE_SIZE (2UL * 1024 * 1024)  // 2MB
#define MAP_HUGE_2MB (21 << MAP_HUGE_SHIFT)

// 原始函数指针
static int (*real_posix_memalign)(void **, size_t, size_t) = NULL;
static void (*real_free)(void *) = NULL;

// 初始化时查找原始函数
__attribute__((constructor))
static void init(void) {
    real_posix_memalign = dlsym(RTLD_NEXT, "posix_memalign");
    real_free = dlsym(RTLD_NEXT, "free");
}

// 跟踪通过 mmap 分配的内存（用于正确释放）
typedef struct {
    void *addr;
    size_t size;
} allocation_info_t;

#define MAX_ALLOCATIONS 10000
static allocation_info_t allocations[MAX_ALLOCATIONS];
static int alloc_count = 0;

// 记录分配
static void record_allocation(void *addr, size_t size) {
    if (alloc_count < MAX_ALLOCATIONS) {
        allocations[alloc_count].addr = addr;
        allocations[alloc_count].size = size;
        alloc_count++;
    }
}

// 查找分配记录
static allocation_info_t* find_allocation(void *addr) {
    for (int i = 0; i < alloc_count; i++) {
        if (allocations[i].addr == addr) {
            return &allocations[i];
        }
    }
    return NULL;
}

// 移除分配记录
static void remove_allocation(void *addr) {
    allocation_info_t *info = find_allocation(addr);
    if (info) {
        // 将最后一个元素移到当前位置
        *info = allocations[--alloc_count];
    }
}

int posix_memalign(void **memptr, size_t alignment, size_t size) {
    // 对于大于 1MB 的分配，使用 2MB 大页
    if (size >= 1024 * 1024) {
        // 向上对齐到 2MB 边界
        size_t aligned_size = (size + HUGE_PAGE_SIZE - 1) & ~(HUGE_PAGE_SIZE - 1);

        void *addr = mmap(
            NULL,
            aligned_size,
            PROT_READ | PROT_WRITE,
            MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB | MAP_HUGE_2MB,
            -1,
            0
        );

        if (addr == MAP_FAILED) {
            fprintf(stderr, "[HugePage] Failed to allocate %zu bytes with 2MB hugepages: %s\n",
                    aligned_size, strerror(errno));
            fprintf(stderr, "[HugePage] Falling back to standard allocation\n");
            return real_posix_memalign(memptr, alignment, size);
        }

        fprintf(stderr, "[HugePage] Allocated %zu bytes (aligned to %zu) at %p using 2MB hugepages\n",
                size, aligned_size, addr);

        *memptr = addr;
        record_allocation(addr, aligned_size);
        return 0;
    }

    // 小分配使用标准方法
    return real_posix_memalign(memptr, alignment, size);
}

void free(void *ptr) {
    if (ptr == NULL) return;

    allocation_info_t *info = find_allocation(ptr);
    if (info) {
        // 这是我们通过 mmap 分配的
        fprintf(stderr, "[HugePage] Freeing %zu bytes at %p (hugepage allocation)\n",
                info->size, ptr);
        munmap(ptr, info->size);
        remove_allocation(ptr);
    } else {
        // 标准分配，使用原始 free
        real_free(ptr);
    }
}

// 可选：支持 malloc（如果 RCCL 使用）
void* malloc(size_t size) {
    void *ptr = NULL;
    // 使用 4K 对齐（最小对齐要求）
    if (posix_memalign(&ptr, 4096, size) != 0) {
        return NULL;
    }
    return ptr;
}
```

**编译和使用**：
```bash
# 编译 wrapper 库
gcc -shared -fPIC -o libhugepage_alloc.so hugepage_allocator.c -ldl

# 配置系统大页
echo 1024 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages

# 使用
LD_PRELOAD=./libhugepage_alloc.so ./rccl_application
```

**优点**：
- 完全控制分配逻辑
- 可以添加调试日志
- 可以设置阈值（只对大分配使用大页）
- 无额外依赖

**缺点**：
- 需要维护代码
- 需要处理线程安全问题（生产环境需加锁）

#### 系统配置

无论使用哪种方式，都需要配置系统大页：

```bash
# 1. 检查当前大页配置
cat /proc/meminfo | grep Huge

# 2. 配置 2MB 大页数量（临时）
echo 1024 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages

# 3. 永久配置（添加到 /etc/sysctl.conf）
vm.nr_hugepages=1024

# 4. 验证
cat /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages
cat /sys/kernel/mm/hugepages/hugepages-2048kB/free_hugepages
```

---

### 方案 2：透明大页（THP） ⚠️ **不推荐**

#### 原理
Linux 内核自动将连续的 4KB 页合并为 2MB 大页。

#### 配置
```bash
# 启用 THP
echo always > /sys/kernel/mm/transparent_hugepage/enabled
echo always > /sys/kernel/mm/transparent_hugepage/defrag

# 或者使用 madvise 模式（应用需要主动调用）
echo madvise > /sys/kernel/mm/transparent_hugepage/enabled
```

#### 问题
- **不保证 100% 大页**：内核可能无法合并页面
- **驱动无法验证**：`ibv_reg_mr` 时无法确认是否真的是大页
- **性能不稳定**：页面合并有延迟，可能导致性能波动
- **内存碎片化**：长时间运行后，大页比例下降

**结论**：不适合对大页有强制要求的场景。

---

### 方案 3：修改 RCCL 源码 ❌ **不推荐**

#### 方式
在 RCCL 源码中修改内存分配函数，直接使用 `mmap` 分配大页。

#### 问题
- **维护成本高**：需要跟随 RCCL 上游更新
- **可能破坏兼容性**：RCCL 的内存管理有自己的假设
- **不灵活**：无法快速切换测试

**结论**：除非有特殊需求，否则应避免。

---

## 推荐方案：LD_PRELOAD + 自定义 Wrapper

### 为什么选择这个方案？

1. **无侵入性**：无需修改 RCCL 或驱动代码
2. **可控性强**：可以精确控制哪些分配使用大页
3. **易于调试**：可以添加详细日志
4. **灵活部署**：可以随时启用/禁用

### 实施步骤

#### 第一阶段：基础验证（1-2天）

1. **实现基础 wrapper**
   ```bash
   # 创建简化版本，只支持 posix_memalign
   gcc -shared -fPIC -o libhugepage_alloc.so hugepage_allocator.c -ldl
   ```

2. **单元测试**
   ```c
   // test_hugepage.c
   #include <stdio.h>
   #include <stdlib.h>

   int main() {
       void *ptr = NULL;
       size_t size = 4 * 1024 * 1024;  // 4MB

       if (posix_memalign(&ptr, 2*1024*1024, size) != 0) {
           fprintf(stderr, "Allocation failed\n");
           return 1;
       }

       printf("Allocated %zu bytes at %p\n", size, ptr);

       // 验证是否是大页（通过 /proc/self/smaps）
       system("cat /proc/self/smaps | grep -A 10 huge");

       free(ptr);
       return 0;
   }
   ```

   ```bash
   gcc -o test_hugepage test_hugepage.c
   LD_PRELOAD=./libhugepage_alloc.so ./test_hugepage
   ```

3. **验证大页分配**
   ```bash
   # 监控大页使用
   watch -n 1 'cat /proc/meminfo | grep Huge'
   ```

#### 第二阶段：RCCL 集成测试（2-3天）

1. **运行简单的 RCCL 测试**
   ```bash
   LD_PRELOAD=./libhugepage_alloc.so \
   ./rccl_test -b 8M -e 128M -f 2
   ```

2. **监控和调试**
   - 查看 wrapper 输出的日志
   - 检查是否有分配失败
   - 验证 `ibv_reg_mr` 是否成功

3. **性能测试**
   - 对比使用大页前后的带宽和延迟
   - 确认无性能退化

#### 第三阶段：生产优化（1-2天）

1. **添加线程安全**
   ```c
   #include <pthread.h>

   static pthread_mutex_t alloc_mutex = PTHREAD_MUTEX_INITIALIZER;

   int posix_memalign(void **memptr, size_t alignment, size_t size) {
       pthread_mutex_lock(&alloc_mutex);
       // ... allocation logic ...
       pthread_mutex_unlock(&alloc_mutex);
   }
   ```

2. **优化分配策略**
   - 只对 RDMA 相关的大分配使用大页
   - 可以通过环境变量控制阈值
   ```c
   static size_t get_hugepage_threshold(void) {
       char *env = getenv("HUGEPAGE_MIN_SIZE");
       return env ? atoi(env) : 1024*1024;  // 默认 1MB
   }
   ```

3. **错误处理**
   - 大页分配失败时自动降级
   - 记录详细错误日志

---

## 验证方法

### 1. 检查内存是否使用大页

```bash
# 方法 1：通过 /proc/PID/smaps
cat /proc/$(pidof rccl_test)/smaps | grep -A 10 "AnonHugePages"

# 方法 2：通过系统大页计数
watch -n 1 'cat /sys/kernel/mm/hugepages/hugepages-2048kB/free_hugepages'
```

### 2. 验证 ibv_reg_mr 成功

在驱动中添加验证逻辑：
```rust
// src/verbs/ctx.rs
pub fn reg_mr(&self, addr: VirtAddr, length: u64, access: u32) -> Result<MrHandle> {
    // 检查地址是否 2MB 对齐
    if !addr.is_aligned_to(2 * 1024 * 1024) {
        log::warn!("MR address {:?} is not 2MB aligned", addr);
        return Err(Error::InvalidAlignment);
    }

    // 检查长度是否是 2MB 的倍数
    if length % (2 * 1024 * 1024) != 0 {
        log::warn!("MR length {} is not multiple of 2MB", length);
        return Err(Error::InvalidLength);
    }

    // ... 现有注册逻辑 ...
}
```

### 3. 性能测试

```bash
# 使用 rccl-tests
git clone https://github.com/ROCm/rccl-tests.git
cd rccl-tests && make

# 对比测试
# 标准分配
./build/all_reduce_perf -b 8M -e 128M -f 2

# 大页分配
LD_PRELOAD=./libhugepage_alloc.so \
./build/all_reduce_perf -b 8M -e 128M -f 2
```

---

## 潜在问题和解决方案

### 问题 1：大页耗尽

**现象**：`mmap` 返回 `MAP_FAILED`，errno = `ENOMEM`

**解决**：
```c
// 自动降级策略
void *addr = mmap(...MAP_HUGETLB | MAP_HUGE_2MB...);
if (addr == MAP_FAILED && errno == ENOMEM) {
    // 尝试不使用大页
    addr = mmap(...); // 移除 MAP_HUGETLB 标志
}
```

**或者增加系统大页数量**：
```bash
# 计算需要的大页数（例如 RCCL 需要 8GB 缓冲区）
# 8GB / 2MB = 4096 pages
echo 4096 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages
```

### 问题 2：内存泄漏

**原因**：mmap 分配的内存未正确 munmap

**解决**：完善跟踪机制
```c
// 使用哈希表而不是数组（生产环境）
#include <search.h>  // hsearch/hcreate

// 或者在分配时添加元数据
typedef struct {
    size_t size;
    char magic[8];  // "HUGEPAGE"
} hugepage_header_t;

void* allocate_with_header(size_t size) {
    size_t total = size + sizeof(hugepage_header_t);
    void *addr = mmap(...total...);

    hugepage_header_t *hdr = (hugepage_header_t*)addr;
    strcpy(hdr->magic, "HUGEPAGE");
    hdr->size = total;

    return (char*)addr + sizeof(hugepage_header_t);
}
```

### 问题 3：与其他库冲突

**现象**：应用崩溃或行为异常

**解决**：限制 wrapper 作用范围
```c
// 只拦截 RCCL 相关的分配
int posix_memalign(void **memptr, size_t alignment, size_t size) {
    // 通过调用栈判断是否来自 RCCL
    void *callstack[10];
    int frames = backtrace(callstack, 10);

    if (is_rccl_caller(callstack, frames)) {
        // 使用大页
    } else {
        // 标准分配
        return real_posix_memalign(memptr, alignment, size);
    }
}
```

---

## 总结

| 方案 | 开发工作量 | 可靠性 | 性能 | 推荐度 |
|------|-----------|--------|------|--------|
| libhugetlbfs | 低（配置） | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| 自定义 Wrapper | 中 | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| 透明大页 | 低（配置） | ⭐⭐ | ⭐⭐⭐ | ⭐⭐ |
| 修改 RCCL | 高 | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐ |

**最终推荐**：
1. **快速验证**：使用 libhugetlbfs（1小时内完成）
2. **生产部署**：自定义 Wrapper（可控、可调试、高性能）

---

## 参考资料

- [RCCL NCCL Net Plugin Documentation](https://rocm.docs.amd.com/projects/rccl/en/develop/how-to/using-nccl.html)
- [NCCL User Buffer Registration](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/bufferreg.html)
- [Using Huge Pages on Linux - Erik Rigtorp](https://rigtorp.se/hugepages/)
- [Linux mmap manual page](https://man7.org/linux/man-pages/man2/mmap.2.html)
- [ibv_reg_mr documentation](https://www.rdmamojo.com/2012/09/07/ibv_reg_mr/)
- [Linux Kernel HugeTLB Documentation](https://www.kernel.org/doc/html/v4.18/admin-guide/mm/hugetlbpage.html)
