# RCCL AllReduce 同步机制分析（CPU 内存中转模式）

## 问题背景

从测试日志 `rccl-1.log` (Line 933-959) 可以看到一个有趣的现象：

```
[2026-01-06T01:23:09.534865265Z] 程序执行到这里时，RDMA Write操作已经完成
[Rank 0] hipStreamSynchronize() => 0      # Line 940
[Rank 0] hipMemcpy(D2H) => 0              # Line 941
[Rank 0] ✓ Test PASSED: result[0] = 3.0  # Line 942 - 数据验证通过

... 然后很晚才看到 poll_cq
[2026-01-06T01:23:09.576003778Z] QP 422: Buffered operation to pending queue
[2026-01-06T01:23:09.585215843Z] poll_cq returned [RdmaWrite { wr_id: 0 }]  # Line 959
```

**关键问题**：为什么在 Line 942 就能验证数据正确（说明 AllReduce 已完成），而 poll_cq 在 Line 959 才返回？

**重要说明**：本测试使用的是 **CPU 内存中转模式**（非 GPU Direct RDMA），从日志可以看到使用了 `hipHostMalloc` 分配的 pinned memory：
```
INFO - [hack_libc] hipHostMalloc: allocated hugepage memory at 0x7eb25da00000
```

## RCCL 的工作机制

### 1. RCCL AllReduce 的异步执行模型

从测试代码 `normal_test_nompi.cpp` 可以看到：

```cpp
// Line 183: 启动 AllReduce（异步操作）
ret = ncclAllReduce(sendbuff, recvbuff, size, ncclFloat, ncclSum, comm, s);

// Line 187: 等待 HIP stream 完成
ret = hipStreamSynchronize(s);

// Line 192: 拷贝结果到主机
ret = hipMemcpy(result, recvbuff, size * sizeof(float), hipMemcpyDeviceToHost);

// Line 196-208: 验证结果
```

### 2. RCCL 的三层架构

RCCL 采用三层架构来实现集合通信：

#### Layer 1: GPU Kernel（设备端）
- GPU 上运行的 RCCL kernel 负责计算和数据移动
- Kernel 通过共享内存与 Proxy 线程通信
- 使用内存同步原语（memory fence）而非轮询 CQ

#### Layer 2: Proxy 线程（主机端）
- 负责处理所有 RDMA 操作（post_send, poll_cq）
- 与 GPU kernel 通过共享内存通信
- 异步处理网络传输，不阻塞 GPU

#### Layer 3: 网络传输（RDMA）
- 使用 InfiniBand Verbs API
- 通过 QP（Queue Pair）发送/接收数据
- 完成后通过 CQ（Completion Queue）通知

### 3. 关键同步机制

#### 3.1 GPU-CPU 同步：基于 GPU 内存的轮询

RCCL 不依赖 poll_cq 来同步 GPU，而是使用以下机制：

```
GPU Kernel          Shared Memory           Proxy Thread
    |                    |                        |
    | 1. 写数据到        |                        |
    |    共享内存         |                        |
    |------------------->|                        |
    |                    |                        |
    |                    | 2. Proxy 读取请求      |
    |                    |<-----------------------|
    |                    |                        |
    |                    |    3. post_send        |
    |                    |                     [RDMA HW]
    |                    |                        |
    | 4. GPU 轮询        |    5. 远端写入完成     |
    |    GPU 内存         |<----[RDMA WRITE]------|
    |    (不是CQ!)        |                        |
    |<-------------------|                        |
    | 5. 检测到数据      |                        |
    |    完成标志         |                        |
    |                    |                        |
  继续执行              |                        |
    |                    |    6. poll_cq          |
    |                    |    (可能在后面)         |
    |                    |                     [RDMA HW]
```

**关键代码实现** (来自 `rccl/src/device/prims_simple.h:910-912`):

```cpp
// GPU kernel 等待数据到达的代码
int spins = 0;
volatile uint64_t* tail = conn->tail;  // 指向远端的 tail 指针
volatile uint64_t* head = conn->head;  // 指向本地的 head 指针
while (*tail > *head)
    if (checkAbort(flags, Aborted, spins)) break;
```

这段代码展示了 GPU kernel 如何轮询内存：
- `conn->tail` 和 `conn->head` 是位于 GPU 可访问内存中的指针
- 远端通过 RDMA WRITE 更新这些值
- GPU 通过 `volatile` 读取检测变化，无需 CPU 参与

**数据结构** (来自 `rccl/src/include/device.h:183-184`):

```cpp
struct ncclDevChannel {
    uint64_t *tail;     // Local for recv, remote for send
    uint64_t *head;     // Local for send, remote for recv
    // ...
};
```

#### 3.2 CPU 内存中转模式的内存可见性机制

**关键技术点**：

##### (1) Pinned Memory（固定内存）
从日志看，RCCL 使用 `hipHostMalloc` 分配 pinned memory：
- 这种内存被锁定在物理内存中，不会被换出
- GPU 可以通过 PCIe 直接访问这些 CPU 内存
- RDMA 也可以直接 DMA 访问这些内存

##### (2) GPU 内存屏障指令

**代码位置**: `rccl/src/device/op128.h:389-391`
```cpp
__device__ __forceinline__ void fence_acq_rel_sys() {
    //asm volatile("membar.sys;" ::: "memory");
}
```

**代码位置**: `rccl/src/device/prims_simple.h:214`
```cpp
#ifdef __GFX9__
    __threadfence();
#else
    __threadfence_system();
#endif
```

这些内存屏障的作用：
- `__threadfence_system()`：确保 GPU 所有之前的内存写入对系统中所有设备可见
- 在读取 RDMA 写入的数据之前，确保 GPU 缓存失效，读取最新数据

##### (3) Volatile 内存访问

**代码位置**: `rccl/src/device/op128.h:352` 和 `225`
```cpp
template<int Size> __device__ BytePack<Size> ld_volatile_global(uintptr_t addr);

__device__ __forceinline__ uint64_t ld_volatile_global(uint64_t *ptr) {
  return __atomic_load_n(ptr, __ATOMIC_SEQ_CST);
}
```

**代码位置**: `rccl/src/device/prims_simple.h:503`
```cpp
while (ld_volatile_global(peerPtr->recv[connIndex].tail) < peerPtr->recv[connIndex].step) {
    int abort = 0;
    // 轮询等待
}
```

GPU kernel 通过 volatile 读取来检测 RDMA 更新：
- 每次读取都从内存获取，不使用缓存值
- 使用 `__ATOMIC_SEQ_CST` 确保顺序一致性
- 即使 RDMA 写入 CPU 内存，GPU 也能及时看到

#### 3.3 为什么不需要 poll_cq？

从日志分析，RCCL 使用了 **RDMA WRITE** 语义：

```
Line 933: Meta Handler got meta = HeaderWriteMeta {
  header_type: Write,    # RDMA WRITE 操作
  imm: 0,                # 立即数
  dqpn: 422,             # 目标 QP
  total_len: 64          # 数据长度
}
```

**关键点**：
1. **RDMA WRITE 是单边操作**：数据直接 DMA 写入远端 CPU 内存，不需要远端 CPU 参与
2. **GPU 通过 PCIe 访问 CPU 内存**：GPU kernel 轮询 CPU 内存中的标志位来检测完成
3. **内存屏障保证可见性**：`__threadfence_system()` 确保 GPU 能看到 RDMA 的写入
4. **poll_cq 只是为了清理资源**：确认 WQE（Work Queue Element）完成，回收发送缓冲区

### 4. hipStreamSynchronize 的作用

`hipStreamSynchronize(s)` 的作用是：
- 等待 HIP stream `s` 中所有已提交的操作完成
- 包括 GPU kernel 的执行
- GPU kernel 内部会轮询内存标志位，确保所有数据传输完成

**同步流程**：
```
ncclAllReduce()
  └─> 启动 GPU kernel（在 stream s 中）
       └─> GPU kernel 等待 RDMA 数据到达（轮询 GPU 内存）
            └─> 数据到达后，kernel 完成计算并返回

hipStreamSynchronize(s)
  └─> 等待 stream s 中所有操作完成
       └─> 包括上面的 GPU kernel
            └─> kernel 已经确认数据完成 ✓

hipMemcpy(D2H)
  └─> 此时数据已经准备好，可以安全拷贝

verify result
  └─> 验证通过 ✓
```

### 5. poll_cq 发生在后面的原因

从日志看，poll_cq (Line 959) 发生在数据验证 (Line 942) 之后，这是因为：

1. **Proxy 线程异步工作**：Proxy 线程在后台处理 RDMA 完成事件
2. **GPU 不依赖 CQ**：GPU 通过内存标志位完成同步，比 poll_cq 更快
3. **poll_cq 用于资源管理**：清理发送 WQE，更新信用（credit），但不影响数据可用性

**时间线**：
```
T0: RDMA WRITE 数据到达远端 CPU 内存（pinned memory）
T1: GPU kernel 通过 PCIe 读取 CPU 内存，检测到标志位变化，确认完成
T2: GPU kernel 返回
T3: hipStreamSynchronize 返回 ✓
T4: hipMemcpy 从 GPU 拷贝结果到 CPU（此时数据已在 GPU 内存中）
T5: 数据验证通过 ✓
...
T6: Proxy 线程 poll_cq，清理 WQE（这是异步的，可能晚一些）
```

## 结论

**RCCL 不需要 poll_cq 就能确定 AllReduce 完成的原因**（在 CPU 内存中转模式下）：

1. **RDMA WRITE 语义**：数据直接 DMA 写入远端 CPU 内存（pinned memory）
2. **GPU 通过 PCIe 访问 CPU 内存**：GPU kernel 通过 PCIe 轮询 CPU 内存中的标志位来检测完成
3. **内存屏障保证可见性**：`__threadfence_system()` + `__ATOMIC_SEQ_CST` 确保 GPU 能看到 RDMA 写入
4. **hipStreamSynchronize 保证**：等待 GPU kernel 完成，而 kernel 已经确认数据到达
5. **poll_cq 是异步的**：Proxy 线程在后台清理资源，不阻塞数据通路

这种设计的优势：
- **低延迟**：GPU 通过 PCIe 直接读取 CPU 内存，无需 CPU 主动参与
- **高吞吐**：Proxy 线程异步处理 CQ，不阻塞 GPU 执行
- **简化同步**：应用只需调用 `hipStreamSynchronize`，RCCL 内部处理所有细节
- **灵活性**：同样的机制可以支持 GPU Direct RDMA（直接写 GPU 内存）和 CPU 中转模式

## CPU 内存中转 vs GPU Direct RDMA

| 特性 | CPU 内存中转模式 | GPU Direct RDMA 模式 |
|------|-----------------|---------------------|
| RDMA 目标 | CPU pinned memory | GPU device memory |
| GPU 访问方式 | PCIe 读取 CPU 内存 | 直接读取 GPU 内存 |
| 内存拷贝 | 需要 CPU↔GPU 拷贝 | 无需额外拷贝 |
| 延迟 | 较高（PCIe 往返） | 较低（GPU 本地访问）|
| 兼容性 | 所有 GPU | 需要硬件支持 |
| 同步机制 | 相同（都是 GPU 轮询）| 相同（都是 GPU 轮询）|

**共同点**：两种模式都使用 GPU kernel 轮询内存标志位，而不是 poll_cq 来检测完成。

## 完整数据流图（CPU 内存中转模式）

```
┌─────────────────────────────────────────────────────────────────────┐
│ Rank 0 (发送端)                                                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  GPU 0                     CPU 0                    RDMA NIC 0      │
│  ┌────┐                   ┌─────────┐              ┌──────┐        │
│  │GPU │  ①数据准备         │ Pinned  │  ③post_send  │ RDMA │        │
│  │Mem │ ────────────────> │ Memory  │ ──────────>  │  QP  │        │
│  │    │                   │(host)   │              │      │        │
│  └────┘                   └─────────┘              └──────┘        │
│                                │                       │            │
│                                │                       │            │
│                           ②Proxy 线程                  │④DMA传输    │
│                           监控标志位                   │            │
└─────────────────────────────────────────────────────────────────────┘
                                                         │
                                    网络传输（IB/RoCE） │
                                                         ↓
┌─────────────────────────────────────────────────────────────────────┐
│ Rank 1 (接收端)                                                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  RDMA NIC 1               CPU 1                     GPU 1           │
│  ┌──────┐               ┌─────────┐               ┌────┐           │
│  │ RDMA │  ⑤DMA写入      │ Pinned  │  ⑥GPU轮询     │GPU │           │
│  │  QP  │ ──────────>   │ Memory  │ <─────────── │Kernel│          │
│  │      │               │(host)   │  通过PCIe     │(AllRe│          │
│  └──────┘               └─────────┘               │duce) │          │
│                             │   ↑                 └────┘           │
│                             │   │                   │              │
│                        ⑦更新│   │⑥轮询              │              │
│                        标志位│   │检测变化           │⑧kernel完成   │
│                             ↓   │                   ↓              │
│                         ┌────────────┐         hipStream-          │
│                         │tail/head   │         Synchronize          │
│                         │指针        │         等待完成 ✓           │
│                         └────────────┘                              │
│                                                                      │
│                         Proxy 线程（后台）                           │
│                         ⑨poll_cq                                    │
│                         清理WQE（异步）                              │
└─────────────────────────────────────────────────────────────────────┘

关键点：
- ⑥⑦：GPU kernel 通过 PCIe 直接读取 CPU pinned memory，检测 tail/head 指针变化
- ⑧：GPU kernel 在检测到数据到达后就返回，不需要等待 poll_cq
- ⑨：poll_cq 在后台异步执行，用于资源管理，不影响数据路径延迟
```

## 为什么不使用 poll_cq 来同步？

### 传统方案的问题（CPU 轮询 CQ）

如果使用 CPU 轮询 CQ 来检测完成，流程会是：
```
1. RDMA WRITE 完成
2. RDMA 硬件向 CQ 写入完成事件
3. CPU 调用 poll_cq 读取完成事件
4. CPU 通知 GPU 数据已到达
5. GPU kernel 继续执行
```

**问题**：
- CPU-GPU 通信开销大
- CPU 需要持续轮询 CQ（占用 CPU 资源）
- GPU 需要等待 CPU 通知（增加延迟）

### RCCL 的改进方案（GPU 直接轮询内存）

RCCL 的方案：
```
1. RDMA WRITE 完成，数据写入 CPU pinned memory
2. GPU kernel 直接通过 PCIe 读取 CPU 内存
3. GPU kernel 检测到标志位变化
4. GPU kernel 继续执行
（poll_cq 在后台异步执行）
```

**优势**：
- GPU 不依赖 CPU，减少 CPU-GPU 通信
- GPU 直接检测数据到达，延迟更低
- poll_cq 异步执行，不阻塞关键路径

### 关键性能优化

| 方案 | GPU 唤醒路径 | 延迟 | CPU 占用 |
|------|-------------|------|---------|
| CPU poll_cq | RDMA → CQ → CPU → GPU | 高（多次通信） | 高（轮询 CQ）|
| GPU 轮询内存 | RDMA → GPU 直接读取 | 低（一次 PCIe）| 低（后台异步）|

**实测证据**（从日志）：
- Line 933-937: RDMA Write 元数据到达（~08:821ms）
- Line 940: hipStreamSynchronize 返回（~09:534ms）← GPU 已经检测到完成
- Line 959: poll_cq 返回（~09:585ms）← poll_cq 滞后约 50ms

这证明了 GPU 轮询内存比 poll_cq 更快感知到数据到达。

## 核心技术总结

### 1. 内存层次结构

```
┌──────────────────────────────────────────────┐
│  应用层                                       │
│  ├─ ncclAllReduce()                          │
│  └─ hipStreamSynchronize() ← 应用只需关心这里 │
└──────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────┐
│  RCCL GPU Kernel                             │
│  ├─ ld_volatile_global() ← GPU 轮询          │
│  ├─ __threadfence_system() ← 内存屏障       │
│  └─ __ATOMIC_SEQ_CST ← 原子操作              │
└──────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────┐
│  CPU Pinned Memory (通过 PCIe 访问)          │
│  ├─ tail/head 指针 ← RDMA 更新               │
│  └─ 数据缓冲区                                │
└──────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────┐
│  RDMA 硬件层                                  │
│  ├─ RDMA WRITE ← 单边操作                    │
│  ├─ DMA Engine ← 直接内存访问                │
│  └─ CQ (后台异步 poll)                        │
└──────────────────────────────────────────────┘
```

### 2. 同步原语的作用

| 原语 | 位置 | 作用 |
|------|------|------|
| `__threadfence_system()` | GPU kernel | 确保 GPU 写入对系统可见，刷新 GPU L2 缓存 |
| `__ATOMIC_SEQ_CST` | GPU kernel | 顺序一致性，防止编译器重排序 |
| `ld_volatile_global()` | GPU kernel | 每次从内存读取，不使用缓存值 |
| `st_relaxed_sys_global()` | GPU kernel | 写入系统内存，确保 RDMA 可见 |

### 3. 关键设计决策

1. **为什么 GPU 轮询而不是中断？**
   - RDMA 完成不产生 GPU 中断
   - GPU kernel 本就在运行，轮询开销小

2. **为什么使用 pinned memory？**
   - GPU 可以通过 PCIe 直接访问
   - RDMA 可以 DMA 访问（不会被换出）
   - 无需 CPU 拷贝

3. **为什么 poll_cq 在后台？**
   - GPU 不需要 CQ 信息就能继续
   - poll_cq 用于资源管理（回收 WQE）
   - 异步执行不增加延迟

## 参考代码位置

- **测试代码**：`open-rdma-driver/tests/rccl_test/normal_test_nompi.cpp:183-188`
- **RCCL 网络层**：`rccl/src/transport/net_ib.cc`
  - Line 2493-2540: `ncclIbIflush` - GPU 缓存刷新
- **RCCL GPU Kernel**：
  - `rccl/src/device/prims_simple.h:910-912` - GPU 轮询逻辑
  - `rccl/src/device/prims_simple.h:503` - Volatile 读取
  - `rccl/src/device/op128.h:389-391` - 内存屏障
  - `rccl/src/device/op128.h:352` - Atomic 读取
- **驱动层**：
  - `open-rdma-driver/rust-driver/src/workers/meta_report/worker.rs` - RDMA 元数据处理
  - `open-rdma-driver/rust-driver/src/workers/completion.rs` - 完成队列处理
- **日志分析**：`open-rdma-driver/tests/rccl_test/log/sim/rccl-1.log:933-959`
