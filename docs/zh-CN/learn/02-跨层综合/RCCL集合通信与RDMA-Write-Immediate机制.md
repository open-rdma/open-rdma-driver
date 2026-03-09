# RCCL 集合通信与 RDMA Write-Immediate 机制深度解析

**作者**: Claude Code
**日期**: 2025-01-14
**代码库**: rccl @ /home/peng/projects/rdma_all/rccl

## 概述

本文档深入分析 RCCL (ROCm Communication Collectives Library) 如何使用 RDMA Write with Immediate 实现高效的集合通信。通过研究 RCCL 的传输层代码，我们揭示了完整的数据流程、传输机制选择以及 GPU Direct RDMA 的启用条件。

### 关键发现

- RCCL 支持 **4 种传输层**：P2P、SHM、NET、CollNet
- RDMA Write with Immediate 在 **NET 传输层的数据发送阶段**使用
- 完整通信流程包含 **3 个阶段**：准备 (CTS)、传输 (Data + WriteImm)、刷新 (Flush)
- GPU Direct RDMA 需要 **peer memory 内核模块**或 **DMA-BUF 支持**

---

## 1. RCCL 传输架构

### 1.1 传输层分类

RCCL 根据通信拓扑自动选择最优传输方式 (`src/include/transport.h:16-32`)：

```c
#define TRANSPORT_P2P     0  // GPU 间直接传输
#define TRANSPORT_SHM     1  // 共享内存传输
#define TRANSPORT_NET     2  // 网络传输 (IB/Socket)
#define TRANSPORT_COLLNET 3  // 集合网络 (交换机卸载)
```

### 1.2 传输选择逻辑

```
拓扑判断:
  ├─ 同节点 + GPU直连?     → P2P Transport  (PCIe/NVLink)
  ├─ 同节点 + 无GPU直连?   → SHM Transport  (/dev/shm)
  ├─ 跨节点 + IB可用?      → NET Transport  (InfiniBand)
  ├─ 跨节点 + 无IB?        → NET Transport  (TCP Socket)
  └─ 支持硬件卸载?         → CollNet Transport (Sharp)
```

### 1.3 NET 传输层架构

**核心文件**: `src/transport/net_ib.cc` (2763 行)

```
NET Transport
  ├─ InfiniBand Backend (net_ib.cc)
  │    ├─ ncclIbIsend()      - 发送接口
  │    ├─ ncclIbIrecv()      - 接收接口
  │    ├─ ncclIbIflush()     - GPU Direct RDMA 刷新
  │    └─ ncclIbTest()       - 完成检测
  │
  └─ Socket Backend (net_socket.cc)
       └─ TCP/IP 备用方案
```

---

## 2. RDMA Write with Immediate 调用时机

### 2.1 三种 RDMA Write 使用场景

| 场景 | 函数 | 操作码 | 位置 | 用途 |
|-----|------|--------|------|------|
| **数据传输** | `ncclIbMultiSend` | `IBV_WR_RDMA_WRITE` | 2131行 | 发送数据块 |
| **完成通知** | `ncclIbMultiSend` | `IBV_WR_RDMA_WRITE_WITH_IMM` | 2168行 | 触发远端完成 ⭐ |
| **接收就绪** | `ncclIbPostFifo` | `IBV_WR_RDMA_WRITE` | 2380行 | CTS 消息 |
| **GPU 刷新** | `ncclIbIflush` | `IBV_WR_RDMA_WRITE` | 2516行 | Flush (Write) |
| **GPU 刷新** | `ncclIbIflush` | `IBV_WR_RDMA_READ` | 2532行 | Flush (Read) |

### 2.2 ncclIbMultiSend 核心代码

**位置**: `net_ib.cc:2118-2248`

```cpp
ncclResult_t ncclIbMultiSend(struct ncclIbSendComm* comm, int slot) {
  struct ncclIbRequest** reqs = comm->fifoReqs[slot];
  volatile struct ncclIbSendFifo* slots = comm->fifo[slot];
  int nreqs = slots[0].nreqs;

  // ============ 构造多个 RDMA Write WR ============
  for (int r=0; r<nreqs; r++) {
    struct ibv_send_wr* wr = comm->wrs+r;
    wr->opcode = IBV_WR_RDMA_WRITE;           // 数据块
    wr->wr.rdma.remote_addr = slots[r].addr;  // 接收方提供的地址
    wr->wr.rdma.rkey = slots[r].rkeys[...];   // 接收方提供的 rkey
    wr->next = wr + 1;                        // 链接到下一个 WR
  }

  // ============ 构造 Immediate Data ============
  uint32_t immData = 0;
  if (nreqs == 1) {
    immData = reqs[0]->send.size;  // 单请求：直接携带数据大小
  } else {
    // 多请求：将大小数组写入远端 sizesFifo
    int* sizes = comm->remSizesFifo.elems[slot];
    for (int r=0; r<nreqs; r++) sizes[r] = reqs[r]->send.size;
  }

  // ============ 最后一个 WR 使用 Write with Immediate ============
  struct ibv_send_wr* lastWr = comm->wrs+nreqs-1;
  if (nreqs > 1 || (comm->ar && reqs[0]->send.size > ncclParamIbArThreshold())) {
    // 自适应路由模式：使用 0 字节的 Write with Immediate
    lastWr++;
    memset(lastWr, 0, sizeof(struct ibv_send_wr));
  }

  lastWr->opcode = IBV_WR_RDMA_WRITE_WITH_IMM;  // ⭐ 关键操作
  lastWr->imm_data = immData;                   // 携带元数据
  lastWr->send_flags = IBV_SEND_SIGNALED;       // 需要完成通知
  lastWr->next = NULL;                          // 链表终止

  // ============ 多 QP 并行发送 ============
  int nqps = ncclParamIbSplitDataOnQps() ? comm->base.nqps : comm->base.nDataQps;
  for (int i = 0; i < nqps; i++) {
    int qpIndex = comm->base.qpIndex;
    ncclIbQp* qp = comm->base.qps + qpIndex;

    // 发送到不同 QP
    NCCLCHECK(wrap_ibv_post_send(qp->qp, comm->wrs, &bad_wr));

    comm->base.qpIndex = (comm->base.qpIndex + 1) % comm->base.nqps;
  }

  return ncclSuccess;
}
```

### 2.3 Write with Immediate 的优势

1. **单次操作完成数据传输 + 通知**
   - 传统方法：Write + Send (两次操作)
   - Write with Immediate：一次操作完成

2. **减少延迟**
   - 减少一次 QP 操作开销
   - 减少一次网络往返

3. **携带元数据**
   - `imm_data` 字段携带数据大小
   - 接收方从 CQ 中直接获取

4. **自动触发接收方事件**
   - 产生 `IBV_WC_RECV_RDMA_WITH_IMM` 完成事件
   - 无需额外的 Send/Recv 队列

---

## 3. 集合通信完整数据流

### 3.1 时序图

```
接收方 (Receiver)                      发送方 (Sender)
    |                                      |
    | 1️⃣ ncclIbIrecv()                     |
    |    - 准备接收缓冲区                    |
    |    - 注册内存 (ibv_reg_mr)            |
    |                                      |
    | 2️⃣ ncclIbPostFifo()                  |
    |    ┌─────────────────────────────┐   |
    |    │ IBV_WR_RDMA_WRITE (CTS)    │   |
    |    │ 写入发送方的 FIFO            │   |
    |    │ 内容：addr, rkey, size      │   |
    |    └─────────────────────────────┘   |
    |                                      ▼
    |                              3️⃣ ncclIbIsend()
    |                                 - 轮询检查 FIFO
    |                                 - slots[0].idx == fifoHead+1?
    |                                      |
    |                              4️⃣ ncclIbMultiSend()
    |                                 ┌────────────────────┐
    |                              ◄──┤ IBV_WR_RDMA_WRITE  │
    |                              ◄──┤ (数据块1)          │
    |                              ◄──┤ IBV_WR_RDMA_WRITE  │
    |                              ◄──┤ (数据块2)          │
    |                              ◄──┤ ...                │
    |                              ◄──┤ IBV_WR_RDMA_WRITE  │
    |                              ◄──┤ _WITH_IMM          │
    |                                 │ imm_data = size    │
    |                                 └────────────────────┘
    ▼                                      |
 5️⃣ CQ Event: IBV_WC_RECV_RDMA_WITH_IMM    |
    - wc->imm_data = 数据大小                |
    - ncclIbTest() 轮询 CQ                 |
    |                                      |
    | 6️⃣ ncclIbIflush() [可选]             |
    |    (仅当 GPU Direct RDMA 时)         |
    |    ┌─────────────────────────────┐   |
    |    │ IBV_WR_RDMA_WRITE           │──►|
    |    │ (写 GPU flush 内存)          │   |
    |    └─────────────────────────────┘   |
    |    ┌─────────────────────────────┐   |
    |    │ IBV_WR_RDMA_READ            │──►|
    |    │ (读回确保可见性)             │   |
    |    └─────────────────────────────┘   |
    ▼                                      ▼
 数据在 GPU 上可见                     发送完成
```

### 3.2 阶段详解

#### 阶段 1: 接收准备 (CTS - Clear To Send)

**接收方代码**: `net_ib.cc:2342-2422`

```cpp
ncclResult_t ncclIbPostFifo(struct ncclIbRecvComm* comm, ...) {
  // 填充本地 FIFO 信息
  struct ncclIbSendFifo* localElem = comm->remFifo.elems[slot];

  for (int i=0; i<n; i++) {
    localElem[i].addr = (uint64_t)data[i];        // 接收缓冲区地址
    localElem[i].rkeys[j] = mhandle->mrs[j]->rkey; // rkey 数组
    localElem[i].size = sizes[i];                 // 缓冲区大小
    localElem[i].tag = tags[i];                   // 匹配标签
    localElem[i].idx = comm->remFifo.fifoTail+1;  // 序列号
  }

  // ⭐ 使用 RDMA Write 将 CTS 信息写入发送方的 FIFO
  struct ibv_send_wr wr;
  wr.opcode = IBV_WR_RDMA_WRITE;
  wr.wr.rdma.remote_addr = comm->remFifo.addr + slot*...;  // 发送方 FIFO 地址
  wr.wr.rdma.rkey = comm->base.remDevs[...].fifoRkey;      // 发送方 FIFO rkey
  wr.sg_list = &comm->devs[...].fifoSge;                   // 本地数据
  wr.send_flags = IBV_SEND_INLINE;                         // 小数据 inline

  ibv_post_send(ctsQp->qp, &wr, &bad_wr);

  comm->remFifo.fifoTail++;
  return ncclSuccess;
}
```

**关键机制**：
- **单向通信**：接收方主动告知，无需应答
- **FIFO 设计**：使用环形缓冲区管理多个请求
- **Inline 优化**：CTS 消息很小，直接嵌入 WQE

#### 阶段 2: 数据发送

**发送方代码**: `net_ib.cc:2249-2340`

```cpp
ncclResult_t ncclIbIsend(void* sendComm, void* data, size_t size, ...) {
  struct ncclIbSendComm* comm = (struct ncclIbSendComm*)sendComm;

  // ⭐ 轮询等待接收方的 FIFO 更新
  int slot = (comm->fifoHead) % MAX_REQUESTS;
  volatile struct ncclIbSendFifo* slots = comm->fifo[slot];
  uint64_t idx = comm->fifoHead+1;

  if (slots[0].idx != idx) {
    // 接收方还没准备好
    *request = NULL;
    return ncclSuccess;  // 稍后重试
  }

  // 等待所有请求的 FIFO 到达
  int nreqs = slots[0].nreqs;
  for (int r=1; r<nreqs; r++)
    while(slots[r].idx != idx);  // 忙等待

  __sync_synchronize();  // 内存屏障

  // 检查接收方提供的信息
  for (int r=0; r<nreqs; r++) {
    if (slots[r].tag != tag) continue;

    // 创建发送请求
    struct ncclIbRequest* req;
    NCCLCHECK(ncclIbGetRequest(&comm->base, &req));
    req->type = NCCL_NET_IB_REQ_SEND;
    req->send.data = data;
    req->send.size = size;

    *request = reqs[r] = req;
  }

  // 所有请求都匹配后，开始发送
  NCCLCHECK(ncclIbMultiSend(comm, slot));

  // 清空 FIFO
  memset((void*)slots, 0, sizeof(struct ncclIbSendFifo));
  comm->fifoHead++;

  return ncclSuccess;
}
```

**关键机制**：
- **被动触发**：发送方等待接收方准备好
- **Zero-copy**：直接写入接收方提供的地址
- **多请求合并**：一次发送多个匹配的请求

#### 阶段 3: GPU 内存刷新 (可选)

**位置**: `net_ib.cc:2493-2545`

```cpp
ncclResult_t ncclIbIflush(void* recvComm, int n, void** data, ...) {
  struct ncclIbRecvComm* comm = (struct ncclIbRecvComm*)recvComm;

  // ⭐ 检查是否需要 flush
  int last = -1;
  for (int i=0; i<n; i++) if (sizes[i]) last = i;
  if (comm->flushEnabled == 0 || last == -1)
    return ncclSuccess;  // 跳过

  // 为每个设备执行 flush
  for (int i = 0; i < comm->base.vProps.ndevs; i++) {
    struct ibv_send_wr wr;

    // ⭐ 第一步：Write 到 GPU flush 内存
    if (rcclParamIbGdrFlushGpuMemNoRelaxedOrdering()) {
      wr.opcode = IBV_WR_RDMA_WRITE;
      wr.wr.rdma.remote_addr = comm->devs[i].gpuFlush.gpuFlushGpuMem;
      wr.wr.rdma.rkey = comm->devs[i].gpuFlush.gpuMr->rkey;
      ibv_post_send(comm->devs[i].gpuFlush.qp.qp, &wr, &bad_wr);
    }

    // ⭐ 第二步：Read 回来确保可见性
    wr.opcode = IBV_WR_RDMA_READ;
    wr.send_flags = IBV_SEND_SIGNALED;
    ibv_post_send(comm->devs[i].gpuFlush.qp.qp, &wr, &bad_wr);

    ncclIbAddEvent(req, i, &comm->devs[i].base);
  }

  return ncclSuccess;
}
```

**关键机制**：
- **Write + Read 组合**：确保 PCI-E write combining buffer 刷新
- **专用 QP**：flush 使用独立的 QP，不干扰数据传输
- **条件执行**：仅在 GPU Direct RDMA 启用时执行

---

## 4. GPU Direct RDMA 启用条件

### 4.1 核心判断逻辑

**位置**: `net_ib.cc:1842-1843`

```cpp
useDmaBuf = (ncclIbDmaBufSupport(lComm->dev) == ncclSuccess);
rComm->flushEnabled = ((ncclIbGdrSupport() == ncclSuccess || useDmaBuf)
                        && (ncclParamIbGdrFlushDisable() == 0)) ? 1 : 0;
```

**翻译**：
```
flushEnabled = (GDR 模块支持 OR DMA-BUF 支持) AND (未手动禁用)
```

### 4.2 GDR 模块检测 (AMD ROCm 平台)

**位置**: `net_ib.cc:812-880`

#### 检测方法 1: 环境变量强制启用

```bash
export RCCL_FORCE_ENABLE_GDRDMA=1  # 仅用于调试
```

#### 检测方法 2: memory_peers 目录

```bash
# 检查以下路径（按优先级）：
/sys/kernel/mm/memory_peers/amdkfd/version      # Ubuntu 22.04 (Kernel 5.15)
/sys/kernel/memory_peers/amdkfd/version         # Ubuntu 22.04.4 HWE (Kernel 6.5)
/sys/memory_peers/amdkfd/version                # Ubuntu 24.04 (Kernel 6.8)
```

**代码**：
```cpp
const char* memory_peers_paths[] = {
  "/sys/kernel/mm/memory_peers/amdkfd/version",
  "/sys/kernel/memory_peers/amdkfd/version",
  "/sys/memory_peers/amdkfd/version",
  NULL
};

int i = 0;
while (memory_peers_paths[i]) {
  if (access(memory_peers_paths[i], F_OK) == 0) {
    ncclIbGdrModuleLoaded = 1;
    INFO(NCCL_INIT, "Found %s", memory_peers_paths[i]);
    break;
  }
  ++i;
}
```

#### 检测方法 3: ib_peer_mem 符号

```bash
# 检查内核符号表
grep "ib_register_peer_memory_client" /proc/kallsyms
```

**代码**：
```cpp
FILE *fp = fopen("/proc/kallsyms", "r");
char buf[256];
while (fgets(buf, sizeof(buf), fp) != NULL) {
  if (strstr(buf, "t ib_register_peer_memory_client") != NULL ||
      strstr(buf, "T ib_register_peer_memory_client") != NULL) {
    ncclIbGdrModuleLoaded = 1;
    INFO(NCCL_INIT, "Found ib_register_peer_memory_client in /proc/kallsyms");
    break;
  }
}
```

### 4.3 GDR 模块检测 (NVIDIA CUDA 平台)

**位置**: `net_ib.cc:876-879`

```cpp
ncclIbGdrModuleLoaded =
  KNL_MODULE_LOADED("/sys/kernel/mm/memory_peers/nv_mem/version") ||
  KNL_MODULE_LOADED("/sys/kernel/mm/memory_peers/nv_mem_nc/version") ||
  KNL_MODULE_LOADED("/sys/module/nvidia_peermem/version");
```

### 4.4 DMA-BUF 支持检测

**位置**: `net_ib.cc:891-920`

```cpp
static void ibDmaBufSupportInitOnce() {
  ncclIbDev* ibDev = ncclIbDevs + mergedDev->vProps.devs[0];
  struct ibv_pd* pd;
  struct ibv_context* ctx = ibDev->context;

  // 尝试创建 PD
  NCCLCHECKGOTO(wrap_ibv_alloc_pd(&pd, ctx), res, exit);

  // 测试 DMA-BUF 注册
  // ... 尝试 ibv_reg_dmabuf_mr() ...

  // 成功则标记支持
  ibDev->dmaBufSupported = 1;

exit:
  if (pd) wrap_ibv_dealloc_pd(pd);
}
```

### 4.5 启用条件总结表

| 平台 | 检测方式 | 路径/符号 |
|-----|---------|----------|
| **AMD ROCm** | memory_peers 文件 | `/sys/.../memory_peers/amdkfd/version` |
| **AMD ROCm** | 内核符号 | `ib_register_peer_memory_client` |
| **AMD ROCm** | 环境变量 | `RCCL_FORCE_ENABLE_GDRDMA=1` |
| **NVIDIA CUDA** | nv_peer_mem 模块 | `/sys/module/nvidia_peermem/` |
| **NVIDIA CUDA** | nv_mem 模块 | `/sys/kernel/mm/memory_peers/nv_mem/` |
| **通用** | DMA-BUF | `ibv_reg_dmabuf_mr()` 可用 |

### 4.6 相关环境变量

| 变量名 | 默认值 | 说明 |
|-------|-------|------|
| `RCCL_FORCE_ENABLE_GDRDMA` | -1 | 强制启用 GDR (仅调试) |
| `NCCL_GDR_FLUSH_DISABLE` | 0 | 禁用 GDR flush |
| `RCCL_GDR_FLUSH_GPU_MEM_NO_RELAXED_ORDERING` | 1 | 使用 GPU 内存刷新模式 |

---

## 5. 多 QP 并行机制

### 5.1 多 QP 设计目的

**位置**: `net_ib.cc:2175-2248`

```cpp
// Multi-QP: make sure IB writes are multiples of 128B
// so that LL and LL128 protocols still work
const int align = 128;
int nqps = ncclParamIbSplitDataOnQps() ?
           comm->base.nqps : comm->base.nDataQps;

for (int i = 0; i < nqps; i++) {
  int qpIndex = comm->base.qpIndex;
  ncclIbQp* qp = comm->base.qps + qpIndex;
  int devIndex = qp->devIndex;

  for (int r=0; r<nreqs; r++) {
    // 计算每个 QP 的数据块大小
    int chunkSize = DIVUP(DIVUP(reqs[r]->send.size, nqps), align) * align;
    int length = std::min(reqs[r]->send.size - reqs[r]->send.offset, chunkSize);

    // 更新偏移量
    reqs[r]->send.offset += length;
    comm->wrs[r].wr.rdma.remote_addr += length;
    comm->sges[r].addr += length;
  }

  // 发送到当前 QP
  ibv_post_send(qp->qp, comm->wrs, &bad_wr);

  // 轮转到下一个 QP
  comm->base.qpIndex = (comm->base.qpIndex + 1) % comm->base.nqps;
}
```

### 5.2 优势分析

1. **带宽聚合**
   - 单 QP 带宽受限于硬件队列深度
   - 多 QP 可以充分利用 IB 链路带宽

2. **负载均衡**
   - 数据均匀分布到多个 QP
   - 减少单个 QP 的拥塞

3. **协议兼容**
   - 128 字节对齐确保 LL/LL128 协议正常工作
   - 支持不同的通信协议切换

---

## 6. 代码位置索引

### 6.1 核心函数位置

| 函数名 | 位置 | 功能 |
|-------|------|------|
| `ncclIbMultiSend` | net_ib.cc:2118-2248 | RDMA Write with Immediate 数据发送 |
| `ncclIbIsend` | net_ib.cc:2249-2340 | 发送接口 (轮询 FIFO) |
| `ncclIbPostFifo` | net_ib.cc:2342-2422 | CTS 消息发送 |
| `ncclIbIrecv` | net_ib.cc:2424-2442 | 接收接口 (触发 PostFifo) |
| `ncclIbIflush` | net_ib.cc:2493-2545 | GPU Direct RDMA 刷新 |
| `ncclIbTest` | net_ib.cc:2559-2640 | 完成检测 (轮询 CQ) |
| `ncclIbGdrSupport` | net_ib.cc:883-889 | GDR 模块检测 |
| `ncclIbDmaBufSupport` | net_ib.cc:921-920 | DMA-BUF 支持检测 |

### 6.2 关键数据结构

| 结构体 | 位置 | 说明 |
|-------|------|------|
| `ncclIbSendComm` | net_ib.cc:1171-1190 | 发送通信器 |
| `ncclIbRecvComm` | net_ib.cc:1217-1224 | 接收通信器 |
| `ncclIbSendFifo` | net_ib.cc:1115-1120 | FIFO 元素 (addr, rkey, size) |
| `ncclIbGpuFlush` | net_ib.cc:1193-1199 | GPU flush 结构 |
| `ncclIbRequest` | net_ib.cc:1142-1168 | 请求对象 |

### 6.3 重要参数

| 参数名 | 位置 | 默认值 | 说明 |
|-------|------|--------|------|
| `IbGdrFlushDisable` | net_ib.cc:1676 | 0 | 禁用 GDR flush |
| `IbGdrFlushGpuMemNoRelaxedOrdering` | net_ib.cc:1677 | 1 | GPU 内存刷新模式 |
| `ForceEnableGdrdma` | net_ib.cc:803 | -1 | 强制启用 GDR |
| `IbArThreshold` | - | - | 自适应路由阈值 |
| `IbSplitDataOnQps` | - | - | 数据分条到多 QP |

---

## 7. 性能优化技术

### 7.1 Inline Send

**位置**: `net_ib.cc:1280` & `net_ib.cc:2381`

```cpp
qpInitAttr.cap.max_inline_data = ncclParamIbUseInline() ?
                                 sizeof(struct ncclIbSendFifo) : 0;

// 使用 inline 发送 CTS 消息
wr.send_flags = comm->remFifo.flags; // IBV_SEND_INLINE
```

**优点**：
- 小数据直接嵌入 WQE，无需额外 DMA
- 减少一次内存访问
- 降低延迟

### 7.2 自适应路由 (Adaptive Routing)

**位置**: `net_ib.cc:2154-2166`

```cpp
if (nreqs > 1 || (comm->ar && reqs[0]->send.size > ncclParamIbArThreshold())) {
  // 使用 0 字节的 Write with Immediate 触发完成
  lastWr++;
  memset(lastWr, 0, sizeof(struct ibv_send_wr));
  lastWr->opcode = IBV_WR_RDMA_WRITE_WITH_IMM;
  lastWr->imm_data = immData;
}
```

**机制**：
- 大数据：先发送数据，再发送 0 字节 WriteImm
- 小数据：直接使用 WriteImm
- 利用网络自适应路由特性

### 7.3 Unsignaled Completion

**位置**: `net_ib.cc:2383-2410`

```cpp
// 大部分 WR 不产生 completion
wr.send_flags = comm->remFifo.flags; // 不含 IBV_SEND_SIGNALED

// 定期产生 signaled completion 防止 SQ 满
if (slot == ctsQp->devIndex) {
  wr.send_flags |= IBV_SEND_SIGNALED;
  wr.wr_id = req - comm->base.reqs;
}
```

**优点**：
- 减少 CQ 事件数量
- 降低 CPU 开销
- 提高吞吐量

---

## 8. 调试与验证

### 8.1 检查 GPU Direct RDMA 是否启用

```bash
# 设置日志级别
export NCCL_DEBUG=INFO

# 运行 RCCL 程序
./your_rccl_app

# 查找关键日志：
# - "Found memory_peers/amdkfd/version"
# - "Found ib_register_peer_memory_client"
# - "GPU Direct RDMA enabled"
```

### 8.2 手动检查内核模块

```bash
# AMD 平台
ls -la /sys/kernel/mm/memory_peers/amdkfd/version 2>/dev/null || \
ls -la /sys/kernel/memory_peers/amdkfd/version 2>/dev/null || \
ls -la /sys/memory_peers/amdkfd/version 2>/dev/null

# NVIDIA 平台
ls -la /sys/module/nvidia_peermem/ 2>/dev/null

# 检查符号
grep "ib_register_peer_memory_client" /proc/kallsyms
```

### 8.3 禁用 GPU Direct RDMA (调试用)

```bash
# 方法 1: 环境变量
export NCCL_GDR_FLUSH_DISABLE=1

# 方法 2: 编译时禁用 (修改代码)
rComm->flushEnabled = 0;
```

---

## 9. 与 Open RDMA 的关系

### 9.1 RCCL 使用的 RDMA 操作

RCCL 通过 `libibverbs` 接口调用 RDMA 驱动：

```
RCCL (net_ib.cc)
    ↓ wrap_ibv_post_send()
libibverbs
    ↓ ioctl()
内核 RDMA 子系统
    ↓
Open RDMA Driver (rust-driver)
    ↓
Open RDMA RTL
```

### 9.2 Open RDMA Driver 中的对应实现

**文件**: `rust-driver/src/verbs/core.rs`

```rust
impl BlueRdmaCore {
    pub fn post_send(&self, qp: u32, wr: &ibv_send_wr) -> Result<()> {
        match wr.opcode {
            IBV_WR_RDMA_WRITE => {
                // 处理 RDMA Write
                self.handle_rdma_write(qp, wr)?;
            }
            IBV_WR_RDMA_WRITE_WITH_IMM => {
                // 处理 RDMA Write with Immediate
                self.handle_rdma_write_with_imm(qp, wr)?;

                // 设置 immediate data
                let imm = wr.imm_data;
                // ...
            }
            IBV_WR_RDMA_READ => {
                // 处理 RDMA Read
                self.handle_rdma_read(qp, wr)?;
            }
            _ => return Err(Error::NotSupported),
        }
        Ok(())
    }
}
```

### 9.3 测试程序

**文件**: `open-rdma-driver/tests/base_test/write_imm.c`

这是一个完整的 Write with Immediate 测试程序，可以用来验证：
- QP 状态转换
- RDMA Write with Immediate 操作
- Immediate data 接收

**运行**：
```bash
cd open-rdma-driver/tests/base_test
./scripts/test_write_imm_sim.sh
```

---

## 10. 总结

### 10.1 关键要点

1. **RCCL 使用 Write with Immediate 实现高效的单次数据传输 + 通知**
2. **完整通信流程包含 3 个阶段：CTS → Data+WriteImm → Flush(可选)**
3. **GPU Direct RDMA 需要 peer memory 内核模块或 DMA-BUF 支持**
4. **多 QP 并行提高带宽利用率**
5. **性能优化技术：Inline Send、自适应路由、Unsignaled Completion**

### 10.2 设计精髓

RCCL 的 RDMA 传输设计体现了以下原则：

- **Zero-copy**：数据直接写入目标内存
- **异步通信**：发送方被动触发，接收方主动控制
- **批处理**：多请求合并发送
- **负载均衡**：多 QP 分条传输
- **硬件卸载**：充分利用 RDMA 硬件特性

### 10.3 参考资料

- RCCL 源码：`/home/peng/projects/rdma_all/rccl`
- Open RDMA Driver：`/home/peng/projects/rdma_all/open-rdma-driver`
- InfiniBand 规范：RDMA Consortium
- ROCm 文档：AMD ROCm Documentation

---

**文档版本**: 1.0
**最后更新**: 2025-01-14
**作者**: Claude Code
