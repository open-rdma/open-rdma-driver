# RCCL net_ib.cc FIFO-RDMA 机制深度解析

## 目录
1. [概述](#概述)
2. [核心数据结构](#核心数据结构)
3. [FIFO机制详解](#FIFO机制详解)
4. [RDMA操作流程](#RDMA操作流程)
5. [完整通信流程](#完整通信流程)
6. [性能优化技术](#性能优化技术)
7. [关键技术点](#关键技术点)

---

## 概述

`net_ib.cc` 是 RCCL（ROCm Communication Collectives Library）中实现基于 InfiniBand/RoCE 网络传输的核心文件。它实现了一个高性能的**信号-数据分离**的通信模型，其中：

- **FIFO（First-In-First-Out）**：作为控制平面，用于传递接收准备信号和元数据
- **RDMA（Remote Direct Memory Access）**：作为数据平面，用于高速数据传输

这种设计避免了传统的握手开销，实现了真正的零拷贝、单边通信。

### 设计哲学

```
接收方主动通知 → FIFO信号 → 发送方响应 → RDMA数据传输
```

与传统的"发送-接收匹配"不同，这里采用**接收方驱动**模型：
1. 接收方先准备好接收缓冲区
2. 通过RDMA WRITE更新发送方的FIFO
3. 发送方轮询本地FIFO，发现接收请求后直接RDMA WRITE数据

---

## 核心数据结构

### 1. FIFO 条目结构 (`ncclIbSendFifo`)

**位置**：`net_ib.cc:1115-1123`

```cpp
struct alignas(64) ncclIbSendFifo {
  uint64_t addr;                                    // 接收缓冲区地址
  uint64_t size;                                    // 缓冲区大小
  uint32_t rkeys[NCCL_IB_MAX_DEVS_PER_NIC];        // 远程访问密钥（每个设备一个）
  uint32_t nreqs;                                   // 请求数量（多接收场景）
  uint32_t tag;                                     // 消息标签（用于匹配）
  uint64_t idx;                                     // 序列号（用于确认FIFO已更新）
  char padding[16];                                 // 填充到64字节对齐
};
```

**关键设计点**：
- **64字节对齐**：确保单个FIFO条目不会跨越缓存行，避免在IB Relaxed Ordering模式下出现乱序写入
- **idx 序列号**：发送方通过检查 `idx` 是否等于预期值来判断FIFO是否已更新
- **多设备支持**：`rkeys` 数组支持每个物理设备有不同的内存注册密钥

### 2. 发送通信器 (`ncclIbSendComm`)

**位置**：`net_ib.cc:1171-1183`

```cpp
struct ncclIbSendComm {
  struct ncclIbNetCommBase base;

  // 本地FIFO数组：[MAX_REQUESTS][NCCL_NET_IB_MAX_RECVS]
  // 接收方通过RDMA WRITE更新这个FIFO
  struct ncclIbSendFifo fifo[MAX_REQUESTS][NCCL_NET_IB_MAX_RECVS];

  struct ibv_sge sges[NCCL_NET_IB_MAX_RECVS];
  struct ibv_send_wr wrs[NCCL_NET_IB_MAX_RECVS + 1];
  struct ncclIbSendCommDev devs[NCCL_IB_MAX_DEVS_PER_NIC];

  // 跟踪每个FIFO槽对应的请求
  struct ncclIbRequest* fifoReqs[MAX_REQUESTS][NCCL_NET_IB_MAX_RECVS];

  // 远程大小FIFO（用于多接收）
  struct ncclIbRemSizesFifo remSizesFifo;

  uint64_t fifoHead;  // FIFO头指针
  int ar;             // 自适应路由标志
};
```

**FIFO布局**：
```
fifo[0][0]  fifo[0][1]  ...  fifo[0][7]   ← Slot 0 (最多8个并发接收)
fifo[1][0]  fifo[1][1]  ...  fifo[1][7]   ← Slot 1
...
fifo[255][0] ...              fifo[255][7] ← Slot 255 (MAX_REQUESTS=256)
```

### 3. 接收通信器 (`ncclIbRecvComm`)

**位置**：`net_ib.cc:1217-1224`

```cpp
struct ncclIbRecvComm {
  struct ncclIbNetCommBase base;
  struct ncclIbRecvCommDev devs[NCCL_IB_MAX_DEVS_PER_NIC];

  // 远程FIFO的本地副本（用于写入到发送方）
  struct ncclIbRemFifo remFifo;

  // 接收大小数组（发送方通过RDMA WRITE更新）
  int sizesFifo[MAX_REQUESTS][NCCL_NET_IB_MAX_RECVS];

  int gpuFlushHostMem;
  int flushEnabled;
};
```

### 4. 远程FIFO结构 (`ncclIbRemFifo`)

**位置**：`net_ib.cc:1202-1207`

```cpp
struct ncclIbRemFifo {
  // 本地维护的FIFO条目（准备写入发送方）
  struct ncclIbSendFifo elems[MAX_REQUESTS][NCCL_NET_IB_MAX_RECVS];
  uint64_t fifoTail;   // FIFO尾指针
  uint64_t addr;       // 远程FIFO的起始地址
  uint32_t flags;      // 标志位（如IBV_SEND_INLINE）
};
```

---

## FIFO机制详解

### FIFO的角色和工作原理

FIFO在这里充当**信号通道**（signaling channel），其核心功能是：

1. **异步通知**：接收方通知发送方"我已经准备好接收数据了"
2. **元数据传递**：传递接收缓冲区地址、大小、rkey等
3. **流量控制**：通过FIFO深度（MAX_REQUESTS=256）限制并发请求数

### FIFO更新机制

#### 接收方更新发送方FIFO (`ncclIbPostFifo`)

**位置**：`net_ib.cc:2342-2422`

```cpp
ncclResult_t ncclIbPostFifo(struct ncclIbRecvComm* comm, int n,
                             void** data, size_t* sizes, int* tags,
                             void** mhandles, struct ncclIbRequest* req) {
  struct ibv_send_wr wr;
  memset(&wr, 0, sizeof(wr));

  // 1. 选择FIFO槽位（循环使用）
  int slot = comm->remFifo.fifoTail % MAX_REQUESTS;

  // 2. 初始化本地FIFO条目
  struct ncclIbSendFifo* localElem = comm->remFifo.elems[slot];

  // 3. 选择QP（轮询方式）
  ncclIbQp* ctsQp = comm->base.qps + comm->base.devIndex;
  comm->base.devIndex = (comm->base.devIndex + 1) % comm->base.vProps.ndevs;

  // 4. 填充FIFO条目（对于n个并发接收）
  for (int i = 0; i < n; i++) {
    struct ncclIbMrHandle* mhandleWrapper = (struct ncclIbMrHandle*)mhandles[i];
    localElem[i].addr = (uint64_t)data[i];         // 接收缓冲区地址
    localElem[i].size = sizes[i];                   // 缓冲区大小
    for (int j = 0; j < comm->base.nRemDevs; j++) {
      localElem[i].rkeys[j] = mhandleWrapper->mrs[j]->rkey; // 每个设备的rkey
    }
    localElem[i].nreqs = n;                         // 多接收数量
    localElem[i].tag = tags[i];                     // 消息标签
    localElem[i].idx = comm->remFifo.fifoTail + 1;  // 序列号（关键！）
  }

  // 5. 构造RDMA WRITE请求
  wr.wr.rdma.remote_addr = comm->remFifo.addr +
                           slot * NCCL_NET_IB_MAX_RECVS * sizeof(struct ncclIbSendFifo);
  wr.wr.rdma.rkey = comm->base.remDevs[ctsQp->remDevIdx].fifoRkey;

  // 6. 设置SGE（Scatter-Gather Element）
  comm->devs[ctsQp->devIndex].fifoSge.addr = (uint64_t)localElem;
  comm->devs[ctsQp->devIndex].fifoSge.length = n * sizeof(struct ncclIbSendFifo);
  wr.sg_list = &comm->devs[ctsQp->devIndex].fifoSge;
  wr.num_sge = 1;

  wr.opcode = IBV_WR_RDMA_WRITE;
  wr.send_flags = comm->remFifo.flags;  // 可能包含 IBV_SEND_INLINE

  // 7. 选择性地添加 SIGNALED 标志
  // 为了防止发送队列溢出，每隔一段时间需要一个有信号的操作
  if (slot == ctsQp->devIndex) {
    wr.send_flags |= IBV_SEND_SIGNALED;
    wr.wr_id = req - comm->base.reqs;
    ncclIbAddEvent(req, ctsQp->devIndex, &comm->devs[ctsQp->devIndex].base);
  }

  // 8. 提交RDMA WRITE
  struct ibv_send_wr* bad_wr;
  NCCLCHECK(wrap_ibv_post_send(ctsQp->qp, &wr, &bad_wr));

  // 9. 更新尾指针
  comm->remFifo.fifoTail++;

  return ncclSuccess;
}
```

**关键流程图**：
```
接收方                                     RDMA网络                                  发送方
   |                                          |                                        |
   | 1. 准备接收缓冲区                          |                                        |
   | 2. 填充localElem[]                        |                                        |
   |    - addr, size, rkey, idx               |                                        |
   | 3. ibv_post_send(RDMA_WRITE)             |                                        |
   |----------------------------------------->|                                        |
   |                                          | 4. 网络传输FIFO条目                      |
   |                                          |--------------------------------------->|
   |                                          |                                        | 5. FIFO条目写入
   |                                          |                                        |    comm->fifo[slot]
   |                                          |                                        | 6. 轮询检测到idx更新
```

### 发送方轮询FIFO (`ncclIbIsend`)

**位置**：`net_ib.cc:2249-2340`

```cpp
ncclResult_t ncclIbIsend(void* sendComm, void* data, size_t size,
                          int tag, void* mhandle, void* phandle, void** request) {
  struct ncclIbSendComm* comm = (struct ncclIbSendComm*)sendComm;

  // 1. 计算当前FIFO槽位
  int slot = (comm->fifoHead) % MAX_REQUESTS;
  struct ncclIbRequest** reqs = comm->fifoReqs[slot];
  volatile struct ncclIbSendFifo* slots = comm->fifo[slot];

  // 2. 检查FIFO是否已更新（通过idx序列号）
  uint64_t idx = comm->fifoHead + 1;
  if (slots[0].idx != idx) {
    *request = NULL;
    return ncclSuccess;  // 接收方还未准备好，返回NULL表示需要重试
  }

  // 3. 获取接收请求数量
  int nreqs = slots[0].nreqs;

  // 4. 等待所有并发接收请求就绪
  for (int r = 1; r < nreqs; r++) {
    while (slots[r].idx != idx);  // 自旋等待
  }
  __sync_synchronize();  // 内存屏障，确保后续读取的顺序

  // 5. 查找匹配的接收请求（通过tag）
  for (int r = 0; r < nreqs; r++) {
    if (reqs[r] != NULL || slots[r].tag != tag) continue;

    // 6. 大小协商（取较小值）
    if (size > slots[r].size) size = slots[r].size;

    // 7. 创建发送请求
    struct ncclIbRequest* req;
    NCCLCHECK(ncclIbGetRequest(&comm->base, &req));
    req->type = NCCL_NET_IB_REQ_SEND;
    req->send.size = size;
    req->send.data = data;
    req->send.offset = 0;

    // 8. 填充lkey（本地访问密钥）
    struct ncclIbMrHandle* mhandleWrapper = (struct ncclIbMrHandle*)mhandle;
    for (int i = 0; i < comm->base.vProps.ndevs; i++) {
      req->send.lkeys[i] = mhandleWrapper->mrs[i]->lkey;
    }

    *request = reqs[r] = req;

    // 9. 如果多接收全部匹配，执行发送
    for (int r = 0; r < nreqs; r++) {
      if (reqs[r] == NULL) return ncclSuccess;  // 还有未匹配的
    }

    // 10. 执行RDMA数据传输
    NCCLCHECK(ncclIbMultiSend(comm, slot));

    // 11. 清理FIFO槽位和请求
    memset((void*)slots, 0, sizeof(struct ncclIbSendFifo));
    memset(reqs, 0, NCCL_NET_IB_MAX_RECVS * sizeof(struct ncclIbRequest*));
    comm->fifoHead++;

    return ncclSuccess;
  }

  *request = NULL;
  return ncclSuccess;  // 未找到匹配的tag
}
```

**轮询机制**：
```
FIFO检查循环：
  ┌─────────────────────────────────┐
  │ fifoHead = 5                    │
  │ slot = 5 % 256 = 5              │
  │ expected_idx = 6                │
  │                                 │
  │ if (fifo[5][0].idx == 6) {     │
  │   // FIFO已更新，处理请求        │
  │ } else {                        │
  │   // FIFO未更新，返回NULL        │
  │   // 上层会重试                 │
  │ }                               │
  └─────────────────────────────────┘
```

---

## RDMA操作流程

### 1. 连接建立阶段

#### QP（Queue Pair）创建和状态转换

**位置**：`net_ib.cc:1268-1351`

```cpp
// 创建QP
ncclResult_t ncclIbCreateQp(uint8_t ib_port, struct ncclIbNetCommDevBase* base,
                             int access_flags, void* qp_context, struct ncclIbQp* qp) {
  struct ibv_qp_init_attr qpInitAttr;
  qpInitAttr.qp_type = IBV_QPT_RC;  // RC = Reliable Connection
  qpInitAttr.cap.max_send_wr = 2 * MAX_REQUESTS;  // 发送工作请求数
  qpInitAttr.cap.max_recv_wr = MAX_REQUESTS;      // 接收工作请求数
  qpInitAttr.cap.max_inline_data = ncclParamIbUseInline() ?
                                   sizeof(struct ncclIbSendFifo) : 0;

  NCCLCHECK(wrap_ibv_create_qp(&qp->qp, base->pd, &qpInitAttr));

  // QP状态：RESET → INIT
  struct ibv_qp_attr qpAttr;
  qpAttr.qp_state = IBV_QPS_INIT;
  qpAttr.port_num = ib_port;
  qpAttr.qp_access_flags = access_flags;  // IBV_ACCESS_REMOTE_WRITE
  NCCLCHECK(wrap_ibv_modify_qp(qp->qp, &qpAttr, ...));

  return ncclSuccess;
}

// QP状态：INIT → RTR (Ready to Receive)
ncclResult_t ncclIbRtrQp(struct ibv_qp* qp, ..., bool fifoTc, int tc, int sl) {
  qpAttr.qp_state = IBV_QPS_RTR;
  qpAttr.dest_qp_num = dest_qp_num;  // 远程QP号

  // 配置地址向量（Address Handle）
  if (info->link_layer == IBV_LINK_LAYER_ETHERNET) {  // RoCE
    qpAttr.ah_attr.is_global = 1;
    qpAttr.ah_attr.grh.dgid = info->gid;  // 远程GID
    qpAttr.ah_attr.grh.traffic_class = fifoTc && ncclParamIbFifoTc() != -1 ?
                                        ncclParamIbFifoTc() : tc;
  } else {  // InfiniBand
    qpAttr.ah_attr.dlid = info->lid;  // 远程LID
  }

  NCCLCHECK(wrap_ibv_modify_qp(qp, &qpAttr, ...));
  return ncclSuccess;
}

// QP状态：RTR → RTS (Ready to Send)
ncclResult_t ncclIbRtsQp(struct ibv_qp* qp) {
  qpAttr.qp_state = IBV_QPS_RTS;
  qpAttr.timeout = ncclParamIbTimeout();
  qpAttr.retry_cnt = ncclParamIbRetryCnt();
  NCCLCHECK(wrap_ibv_modify_qp(qp, &qpAttr, ...));
  return ncclSuccess;
}
```

**QP状态机**：
```
RESET → INIT → RTR → RTS
  |       |      |     |
  |       |      |     └─ 可以发送和接收
  |       |      └─ 可以接收（已配置远程信息）
  |       └─ 已初始化（配置本地端口和权限）
  └─ 初始状态
```

#### 元数据交换

**位置**：`net_ib.cc:1374-1534`

发送方和接收方通过TCP socket交换以下元数据：

```cpp
struct ncclIbConnectionMetadata {
  struct ncclIbQpInfo qpInfo[NCCL_IB_MAX_QPS];  // 每个QP的信息
  struct ncclIbDevInfo devs[NCCL_IB_MAX_DEVS_PER_NIC];  // 设备信息
  char devName[MAX_MERGED_DEV_NAME];
  uint64_t fifoAddr;  // ← 关键：FIFO的起始地址
  int ndevs;
  int tc;  // Traffic Class
  int sl;  // Service Level
  int isP2p;
};

struct ncclIbDevInfo {
  uint32_t lid;           // LID（InfiniBand）
  union ibv_gid gid;      // GID（RoCE）
  uint32_t fifoRkey;      // ← 关键：FIFO的远程访问密钥
  // ...
};
```

**元数据交换流程**：
```
发送方                      TCP Socket                      接收方
  |                            |                              |
  | 1. meta.fifoAddr =         |                              |
  |    (uint64_t)comm->fifo    |                              |
  | 2. meta.devs[i].fifoRkey = |                              |
  |    fifoMr->rkey           |                              |
  | 3. send(meta) ------------>|----------------------------> | 4. 保存远程FIFO信息
  |                            |                              |    remFifo.addr = meta.fifoAddr
  |                            |                              |    remFifo.rkey = meta.devs[i].fifoRkey
  | 5. recv(meta) <------------|<---------------------------- | 6. send(meta)
  |                            |                              |
  | 7. 保存远程大小FIFO信息       |                              |
  |    remSizesFifo.addr       |                              |
  |    remSizesFifo.rkeys[]    |                              |
```

### 2. 内存注册

**位置**：`net_ib.cc:1986-2062`

```cpp
ncclResult_t ncclIbRegMrInternal(ncclIbNetCommDevBase* base, void* data,
                                  size_t size, int type, ibv_mr** mhandle) {
  // 计算页对齐的地址和大小
  uintptr_t addr = (uintptr_t)data & -pageSize;
  size_t pages = ((uintptr_t)data + size - addr + pageSize - 1) / pageSize;

  // 查找MR缓存
  struct ncclIbMrCache* cache = &ncclIbDevs[base->ibDevN].mrCache;
  for (int slot = 0; slot < cache->population; slot++) {
    if (cache->slots[slot].addr == addr &&
        cache->slots[slot].pages == pages) {
      cache->slots[slot].refs++;  // 引用计数增加
      *mhandle = cache->slots[slot].mr;
      return ncclSuccess;
    }
  }

  // 未找到，注册新的MR
  int access_flags = IBV_ACCESS_LOCAL_WRITE |
                     IBV_ACCESS_REMOTE_WRITE |
                     IBV_ACCESS_REMOTE_READ;
  if (ncclIbRelaxedOrderingEnabled) {
    access_flags |= IBV_ACCESS_RELAXED_ORDERING;
  }

  NCCLCHECK(wrap_ibv_reg_mr(mhandle, base->pd, (void*)addr,
                            pages * pageSize, access_flags));

  // 添加到缓存
  // ...

  return ncclSuccess;
}
```

**内存注册的必要性**：
- RDMA操作需要物理地址保持固定（pinned memory）
- MR（Memory Region）包含虚拟地址到物理地址的映射
- lkey：本地访问密钥（用于本地RDMA操作）
- rkey：远程访问密钥（传递给对端，用于远程RDMA操作）

### 3. 数据传输 (`ncclIbMultiSend`)

**位置**：`net_ib.cc:2118-2247`

```cpp
ncclResult_t ncclIbMultiSend(struct ncclIbSendComm* comm, int slot) {
  struct ncclIbRequest** reqs = comm->fifoReqs[slot];
  volatile struct ncclIbSendFifo* slots = comm->fifo[slot];
  int nreqs = slots[0].nreqs;

  // 1. 构造工作请求（Work Request）
  uint64_t wr_id = 0ULL;
  for (int r = 0; r < nreqs; r++) {
    struct ibv_send_wr* wr = comm->wrs + r;
    struct ibv_sge* sge = comm->sges + r;

    // 2. 设置SGE（本地缓冲区）
    sge->addr = (uintptr_t)reqs[r]->send.data;
    sge->length = reqs[r]->send.size;
    // lkey 在后面根据设备设置

    // 3. 设置RDMA WRITE参数
    wr->opcode = IBV_WR_RDMA_WRITE;
    wr->wr.rdma.remote_addr = slots[r].addr;  // 接收方提供的地址
    // rkey 在后面根据设备设置
    wr->sg_list = sge;
    wr->num_sge = 1;
    wr->next = wr + 1;  // 链接到下一个WR

    // 4. 编码请求ID到wr_id
    wr_id |= (uint64_t)(reqs[r] - comm->base.reqs) << (r * 8);
  }

  // 5. 最后一个WR：带有立即数（immediate data）
  struct ibv_send_wr* lastWr = comm->wrs + nreqs - 1;
  lastWr->next = NULL;

  if (nreqs == 1) {
    // 单接收：立即数 = 数据大小
    lastWr->opcode = IBV_WR_RDMA_WRITE_WITH_IMM;
    lastWr->imm_data = reqs[0]->send.size;
  } else {
    // 多接收：先写大小数组
    lastWr++;  // 额外的WR用于写大小
    int* sizes = comm->remSizesFifo.elems[slot];
    for (int r = 0; r < nreqs; r++) sizes[r] = reqs[r]->send.size;

    comm->remSizesFifo.sge.addr = (uint64_t)sizes;
    comm->remSizesFifo.sge.length = nreqs * sizeof(int);
    lastWr->wr.rdma.remote_addr = comm->remSizesFifo.addr +
                                   slot * NCCL_NET_IB_MAX_RECVS * sizeof(int);
  }

  lastWr->send_flags = IBV_SEND_SIGNALED;  // 请求完成通知
  lastWr->wr_id = wr_id;

  // 6. 根据配置选择发送策略
  if (ncclParamIbSplitDataOnQps()) {
    // 数据分片到多个QP
    int nqps = comm->base.nqps;
    int chunkSize = DIVUP(DIVUP(size, nqps), align) * align;
    // ... 分片逻辑
  }

  // 7. 轮询QP发送
  int qpIndex = comm->base.qpIndex;
  for (int q = 0; q < comm->base.nDataQps; q++) {
    struct ncclIbQp* qp = comm->base.qps + qpIndex;
    int devIndex = qp->devIndex;

    // 8. 设置设备相关的key
    for (int r = 0; r < nreqs; r++) {
      comm->sges[r].lkey = reqs[r]->send.lkeys[devIndex];
      comm->wrs[r].wr.rdma.rkey = slots[r].rkeys[devIndex];
    }

    // 9. 提交发送请求
    struct ibv_send_wr* bad_wr;
    NCCLCHECK(wrap_ibv_post_send(qp->qp, comm->wrs, &bad_wr));

    // 10. 记录事件
    for (int r = 0; r < nreqs; r++) {
      ncclIbAddEvent(reqs[r], devIndex, &comm->devs[devIndex].base);
    }

    qpIndex = (qpIndex + 1) % comm->base.nqps;
  }

  comm->base.qpIndex = qpIndex;
  return ncclSuccess;
}
```

**RDMA WRITE 工作流程**：
```
发送方                          RDMA网卡                        接收方网卡                    接收方内存
  |                               |                               |                              |
  | 1. ibv_post_send()           |                               |                              |
  |   - opcode: RDMA_WRITE       |                               |                              |
  |   - local: sge.addr/lkey     |                               |                              |
  |   - remote: remote_addr/rkey |                               |                              |
  |---------------------------->|                               |                              |
  |                               | 2. DMA读取本地数据              |                              |
  |                               |<------------------------------|                              |
  |                               | 3. 通过网络发送                 |                              |
  |                               |------------------------------>|                              |
  |                               |                               | 4. DMA写入远程内存            |
  |                               |                               |----------------------------->|
  |                               |                               |                              | 5. 数据到达
  |                               | 6. 完成通知（如果SIGNALED）      |                              |
  |                               |<------------------------------|                              |
  | 7. CQ轮询得到完成              |                               |                              |
  |<-----------------------------|                               |                              |
```

### 4. 完成检测 (`ncclIbTest`)

**位置**：`net_ib.cc:2559-2680`

```cpp
ncclResult_t ncclIbTest(void* request, int* done, int* sizes) {
  struct ncclIbRequest* r = (struct ncclIbRequest*)request;
  *done = 0;

  while (1) {
    // 1. 检查所有设备的事件是否完成
    if (r->events[0] == 0 && r->events[1] == 0 &&
        r->events[2] == 0 && r->events[3] == 0) {
      *done = 1;

      // 2. 对于接收请求，返回实际接收大小
      if (sizes && r->type == NCCL_NET_IB_REQ_RECV) {
        for (int i = 0; i < r->nreqs; i++) {
          sizes[i] = r->recv.sizes[i];
        }
      }

      NCCLCHECK(ncclIbFreeRequest(r));
      return ncclSuccess;
    }

    // 3. 轮询所有设备的完成队列
    for (int i = 0; i < 4 && r->devBases[i] != NULL; i++) {
      struct ibv_cq* cq = r->devBases[i]->cq;
      int wrDone = 0;
      struct ibv_wc wcs[4];

      // 4. 批量获取完成事件
      NCCLCHECK(wrap_ibv_poll_cq(cq, 4, wcs, &wrDone));
      if (wrDone == 0) continue;

      // 5. 处理每个完成事件
      for (int w = 0; w < wrDone; w++) {
        struct ibv_wc* wc = wcs + w;

        // 6. 检查错误
        if (wc->status != IBV_WC_SUCCESS) {
          WARN("NET/IB: Got completion with status=%d opcode=%d",
               wc->status, wc->opcode);
          return ncclRemoteError;
        }

        // 7. 解码请求ID
        struct ncclIbRequest* req = r->base->reqs + (wc->wr_id & 0xff);

        // 8. 处理不同类型的完成
        if (req->type == NCCL_NET_IB_REQ_SEND) {
          // 发送完成：减少所有相关请求的事件计数
          for (int j = 0; j < req->nreqs; j++) {
            struct ncclIbRequest* sendReq =
              r->base->reqs + ((wc->wr_id >> (j * 8)) & 0xff);
            sendReq->events[i]--;
          }
        } else if (wc->opcode == IBV_WC_RECV_RDMA_WITH_IMM) {
          // 接收完成：保存立即数（数据大小）
          req->recv.sizes[0] = wc->imm_data;
          req->events[i]--;
        } else {
          // 其他完成（如FIFO写、FLUSH等）
          req->events[i]--;
        }
      }
    }

    // 9. 未完成，返回让上层重试
    return ncclSuccess;
  }
}
```

**完成队列（CQ）轮询**：
```
┌───────────────────────────────────────┐
│ CQ (Completion Queue)                 │
│ ┌───────┬───────┬───────┬───────┐    │
│ │ WC 1  │ WC 2  │ WC 3  │ WC 4  │... │
│ └───────┴───────┴───────┴───────┘    │
│   ↑ ibv_poll_cq()                    │
└───┼───────────────────────────────────┘
    │
    │ 返回已完成的WC (Work Completion)
    │
┌───┴───────────────────────────────────┐
│ struct ibv_wc {                       │
│   uint64_t  wr_id;     // 工作请求ID   │
│   enum ibv_wc_status status; // 状态  │
│   enum ibv_wc_opcode opcode; // 操作  │
│   uint32_t  byte_len;  // 传输字节数   │
│   uint32_t  imm_data;  // 立即数       │
│   uint32_t  qp_num;    // QP号        │
│   // ...                              │
│ }                                     │
└───────────────────────────────────────┘
```

---

## 完整通信流程

### 场景：Rank 0 发送 1MB 数据到 Rank 1

#### 阶段1：初始化和连接建立

```
Rank 0 (Sender)                                    Rank 1 (Receiver)
─────────────────────────────────────────────────────────────────────
1. ncclIbInit()                                     1. ncclIbInit()
   - 发现IB设备                                        - 发现IB设备
   - 创建ncclIbDevs[]                                 - 创建ncclIbDevs[]

2. ncclIbListen()                                   2. ncclIbConnect()
   - 创建TCP listen socket                            - 创建TCP socket
   - 绑定到本地地址                                    - 连接到Rank 1

3. ncclIbAccept()                              <--> 3. ncclIbConnect()
   - 交换设备列表                                      - 交换设备列表
   - ncclIbCalculateNqps()                            - ncclIbCalculateNqps()

4. 创建SendComm                                     4. 创建RecvComm
   - ncclIbInitCommDevBase()                          - ncclIbInitCommDevBase()
   - 分配PD, CQ                                       - 分配PD, CQ
   - 创建QP                                           - 创建QP

5. 注册FIFO内存                                     5. 注册FIFO内存
   - ibv_reg_mr(comm->fifo)                          - ibv_reg_mr(remFifo.elems)
   - 获取fifoMr->rkey                                 - 获取fifoMr->rkey

6. 交换元数据                                 <--> 6. 交换元数据
   meta.fifoAddr = (uint64_t)comm->fifo              meta.fifoAddr = (uint64_t)sizesFifo
   meta.devs[i].fifoRkey = fifoMr->rkey              meta.devs[i].fifoRkey = sizesFifoMr->rkey
   TCP send(meta) ────────────────────────────>      TCP recv(meta)
   TCP recv(meta) <────────────────────────────      TCP send(meta)

7. 保存远程FIFO信息                                  7. 保存远程FIFO信息
   remSizesFifo.addr = meta.fifoAddr                 remFifo.addr = meta.fifoAddr
   remSizesFifo.rkeys[] = meta.devs[].fifoRkey      remFifo.rkeys[] = meta.devs[].fifoRkey

8. QP状态转换                                       8. QP状态转换
   ncclIbRtrQp() : INIT→RTR                          ncclIbRtrQp() : INIT→RTR
   ncclIbRtsQp() : RTR→RTS                           ncclIbRtsQp() : RTR→RTS

9. comm->base.ready = 1                             9. comm->base.ready = 1
```

#### 阶段2：接收方准备接收

```
Rank 1 (Receiver)
─────────────────────────────────────────────────────
1. 应用层调用 ncclIbIrecv()
   参数:
   - data = 0x7f0000000000 (接收缓冲区)
   - size = 1MB
   - tag = 42

2. 注册接收缓冲区内存
   ncclIbRegMr()
   - ibv_reg_mr(data, 1MB)
   - 获取 mhandle->mrs[i]->rkey

3. Post接收工作请求
   ibv_post_recv()
   - wr.wr_id = req_index
   - wr.sg_list = {addr: data, length: 1MB}

4. 更新FIFO (ncclIbPostFifo)

   a) 选择FIFO槽位
      slot = remFifo.fifoTail % 256 = 5

   b) 填充本地FIFO条目
      localElem[0].addr = 0x7f0000000000
      localElem[0].size = 1048576
      localElem[0].rkeys[0] = mhandle->mrs[0]->rkey
      localElem[0].nreqs = 1
      localElem[0].tag = 42
      localElem[0].idx = 6  ← 序列号！

   c) 构造RDMA WRITE
      wr.opcode = IBV_WR_RDMA_WRITE
      wr.wr.rdma.remote_addr = remFifo.addr + 5 * 8 * 64
                             = Rank 0's fifo[5][0]
      wr.wr.rdma.rkey = remFifo.rkeys[0]
      wr.sg_list = {addr: localElem, length: 64}

   d) 提交RDMA WRITE
      ibv_post_send(qp, &wr) ─────────────────┐
                                               │
   e) remFifo.fifoTail++ = 6                  │
                                               │
5. 返回 request 给应用层                         │
                                               │
                                               │ RDMA网络传输
                                               ↓
```

#### 阶段3：发送方检测FIFO更新并发送数据

```
Rank 0 (Sender)
────────────────────────────────────────────────────────────────────
                                                ↓
                                        FIFO条目写入
                                        fifo[5][0].idx = 6
                                        fifo[5][0].addr = 0x7f0000000000
                                        fifo[5][0].size = 1048576
                                        fifo[5][0].rkeys[0] = 0x12345
                                        fifo[5][0].tag = 42

1. 应用层调用 ncclIbIsend()
   参数:
   - data = 0x7e0000000000 (发送缓冲区)
   - size = 1MB
   - tag = 42

2. 计算FIFO槽位
   slot = fifoHead % 256 = 5
   expected_idx = fifoHead + 1 = 6

3. 检查FIFO更新 (轮询)
   volatile struct ncclIbSendFifo* slots = fifo[5];

   循环: while (slots[0].idx != 6) {
     // CPU自旋等待
   }

   // 成功！FIFO已更新

4. 验证tag匹配
   if (slots[0].tag == 42) {  // 匹配！

5. 大小协商
   send_size = min(1MB, slots[0].size) = 1MB

6. 创建发送请求
   ncclIbGetRequest(&req)
   req->type = NCCL_NET_IB_REQ_SEND
   req->send.data = 0x7e0000000000
   req->send.size = 1MB

7. 执行RDMA数据传输 (ncclIbMultiSend)

   a) 构造工作请求
      wr.opcode = IBV_WR_RDMA_WRITE_WITH_IMM
      wr.imm_data = 1048576  ← 立即数 = 数据大小

      // 本地缓冲区
      sge.addr = 0x7e0000000000
      sge.length = 1048576
      sge.lkey = mhandle->mrs[0]->lkey

      // 远程缓冲区（从FIFO获取）
      wr.wr.rdma.remote_addr = slots[0].addr = 0x7f0000000000
      wr.wr.rdma.rkey = slots[0].rkeys[0] = 0x12345

      wr.send_flags = IBV_SEND_SIGNALED
      wr.wr_id = req_index

   b) 提交RDMA WRITE
      ibv_post_send(qp, &wr) ──────────────────┐
                                                │
   c) 清理FIFO槽位                               │
      memset(fifo[5], 0, ...)                   │
      fifoHead++ = 6                            │
                                                │
8. 返回 request 给应用层                          │
                                                │
                                                │ RDMA网络传输
                                                │ (1MB 数据)
                                                ↓
```

#### 阶段4：接收方检测完成

```
                                                ↓
                                        数据写入 0x7f0000000000
                                        (1MB 数据传输完成)

                                        RDMA-WITH-IMM 触发
                                        → CQ产生完成事件

Rank 1 (Receiver)
────────────────────────────────────────────────────────────────────
1. 应用层调用 ncclIbTest(request)

2. 检查事件计数
   if (req->events[0] == 0) {  // 还未完成

3. 轮询完成队列
   ibv_poll_cq(cq, 4, wcs, &wrDone)

   得到完成事件:
   wc.status = IBV_WC_SUCCESS
   wc.opcode = IBV_WC_RECV_RDMA_WITH_IMM
   wc.imm_data = 1048576  ← 发送方传递的数据大小
   wc.wr_id = req_index

4. 处理完成事件
   req = base->reqs + wc.wr_id
   req->recv.sizes[0] = wc.imm_data = 1048576
   req->events[0]--  (1 → 0)

5. 再次检查事件计数
   if (req->events[0] == 0) {  // 完成！
     *done = 1
     sizes[0] = 1048576  ← 返回实际接收大小
     ncclIbFreeRequest(req)

6. 应用层得到完成通知
   - 数据已在 0x7f0000000000
   - 实际大小 = 1048576 字节
```

#### 阶段5：发送方检测完成

```
Rank 0 (Sender)
────────────────────────────────────────────────────────────────────
1. 应用层调用 ncclIbTest(request)

2. 检查事件计数
   if (req->events[0] == 0) {  // 还未完成

3. 轮询完成队列
   ibv_poll_cq(cq, 4, wcs, &wrDone)

   得到完成事件:
   wc.status = IBV_WC_SUCCESS
   wc.opcode = IBV_WC_RDMA_WRITE
   wc.wr_id = req_index  (编码了所有相关请求)

4. 处理完成事件
   解码 wr_id 获取所有相关请求
   req = base->reqs + (wc.wr_id & 0xff)
   req->events[0]--  (1 → 0)

5. 再次检查事件计数
   if (req->events[0] == 0) {  // 完成！
     *done = 1
     ncclIbFreeRequest(req)

6. 应用层得到完成通知
   - 数据已发送完成
   - 发送缓冲区可复用
```

---

## 性能优化技术

### 1. 内存对齐和缓存优化

```cpp
// 64字节对齐（缓存行大小）
struct alignas(64) ncclIbSendFifo { ... };

// 确保FIFO不会跨缓存行分割
static_assert((sizeof(struct ncclIbSendFifo) % 32) == 0, ...);
static_assert((offsetof(struct ncclIbSendComm, fifo) % 32) == 0, ...);
```

**原因**：
- 避免false sharing（伪共享）
- IB Relaxed Ordering 模式下，确保原子性写入
- 提高缓存命中率

### 2. Inline Data

**位置**：`net_ib.cc:1280`

```cpp
qpInitAttr.cap.max_inline_data = ncclParamIbUseInline() ?
                                 sizeof(struct ncclIbSendFifo) : 0;
```

**ncclIbPostFifo** 中：
```cpp
wr.send_flags = comm->remFifo.flags;  // IBV_SEND_INLINE
```

**效果**：
- 小数据（FIFO条目 = 64字节）直接嵌入到WQE中
- 避免DMA读取，降低延迟
- 典型延迟改善：2-3μs → <1μs

### 3. 多QP并行

**位置**：`net_ib.cc:156-166`

```cpp
static int ncclIbCalculateNqps(int isP2p, int localNdevs, int remoteNdevs, ...) {
  auto qp_multiplier = (rcclParamIbQpsPerP2p() > 0 && isP2p) ?
                       rcclParamIbQpsPerP2p() : ncclParamIbQpsPerConn();
  int localNqps = qp_multiplier * localNdevs;
  int remoteNqps = qp_multiplier * remoteNdevs;
  return max(localNqps, remoteNqps);
}
```

**策略**：
- **轮询发送**：`comm->base.qpIndex = (qpIndex + 1) % nqps`
- **数据分片**：`NCCL_IB_SPLIT_DATA_ON_QPS=1` 时将大数据分割到多个QP
- **隔离故障**：单个QP错误不影响其他QP

**吞吐量提升**：
- 单QP：~12 GB/s (HDR-100)
- 4 QP：~45 GB/s (接近线速)

### 4. 批量操作

**位置**：`net_ib.cc:2118-2247`

```cpp
// 多接收（Multi-Recv）：一次FIFO更新，匹配多个发送
struct ncclIbSendFifo {
  uint32_t nreqs;  // ← 最多8个并发接收
};

// 发送端等待所有请求匹配后，批量发送
for (int r = 0; r < nreqs; r++) {
  if (reqs[r] == NULL) return ncclSuccess;  // 未全部匹配
}
ncclIbMultiSend(comm, slot);  // 批量发送
```

**批量CQ轮询**：
```cpp
struct ibv_wc wcs[4];
wrap_ibv_poll_cq(cq, 4, wcs, &wrDone);
```

**效果**：
- 减少系统调用次数
- 提高PCI-e总线利用率
- 降低CPU开销

### 5. 自适应路由（Adaptive Routing）

**位置**：`net_ib.cc:742-745, 1310`

```cpp
// 初始化时检测
ncclIbDevs[ncclNIbDevs].ar = (portAttr.link_layer == IBV_LINK_LAYER_INFINIBAND) ? 1 : 0;
if (ncclParamIbAdaptiveRouting() != -2)
  ncclIbDevs[ncclNIbDevs].ar = ncclParamIbAdaptiveRouting();

// RTR时配置traffic class
qpAttr.ah_attr.grh.traffic_class = fifoTc && ncclParamIbFifoTc() != -1 ?
                                    ncclParamIbFifoTc() : tc;
```

**原理**：
- IB交换机根据网络拥塞动态选择路径
- FIFO流量可配置独立TC（Traffic Class）
- 避免控制流和数据流相互干扰

### 6. GPU Direct RDMA (GDR)

**位置**：`net_ib.cc:883-889, 2493-2545`

```cpp
ncclResult_t ncclIbGdrSupport() {
  // 检测 nv_peer_mem / amdgpu_peer_mem 模块
  if (!ncclIbGdrModuleLoaded) return ncclSystemError;
  return ncclSuccess;
}

// Flush操作（确保GPU可见）
ncclResult_t ncclIbIflush(...) {
  // RDMA READ 操作强制刷新
  wr.opcode = IBV_WR_RDMA_READ;
  wr.wr.rdma.remote_addr = (uint64_t)comm->devs[i].gpuFlush.gpuFlushGpuMem;
  wrap_ibv_post_send(comm->devs[i].gpuFlush.qp.qp, &wr, &bad_wr);
}
```

**优势**：
- GPU内存直接注册为MR
- 网卡直接DMA到GPU内存
- 零拷贝，避免CPU参与

**性能对比**：
```
传统方式：GPU → CPU → 网卡 → 网络
  延迟：10-15μs，带宽：受限于PCI-e

GDR方式：GPU → 网卡 → 网络
  延迟：2-3μs，带宽：接近线速
```

### 7. Relaxed Ordering

**位置**：`net_ib.cc:532-540, 1187-1189`

```cpp
static int ncclIbRelaxedOrderingCapable(void) {
  int roMode = ncclParamIbPciRelaxedOrdering();
  if (roMode == 1 || roMode == 2) {
    // 测试 IBV_ACCESS_RELAXED_ORDERING 支持
    r = wrap_ibv_reg_mr_iova2(NULL, NULL, NULL, 0, 0, 0);
  }
  return r == ncclInternalError ? 0 : 1;
}

// MR注册时添加标志
if (ncclIbRelaxedOrderingEnabled) {
  access_flags |= IBV_ACCESS_RELAXED_ORDERING;
}
```

**效果**：
- 允许PCI-e事务乱序完成
- 提高PCI-e总线吞吐量
- 要求FIFO对齐以保证原子性

---

## 关键技术点

### 1. 为什么使用FIFO而不是传统的send/recv匹配？

**传统MPI模型**：
```
Send(data, dest, tag) → 查找匹配的Recv
Recv(buf, src, tag)   → 等待匹配的Send
```
问题：
- 需要软件匹配逻辑（开销大）
- Send必须等待Recv ready
- 双边操作，增加同步开销

**FIFO模型**：
```
Recv(buf, tag) → RDMA WRITE(FIFO) → 立即返回
Send(data, tag) → 轮询FIFO → RDMA WRITE(data)
```
优势：
- **单边操作**：Send侧主动轮询，无需Recv侧参与
- **零拷贝**：RDMA直接写入目标缓冲区
- **低延迟**：FIFO更新通过RDMA完成，~1μs

### 2. idx序列号的作用

```cpp
localElem[i].idx = comm->remFifo.fifoTail + 1;  // 接收方写入

// 发送方检查
uint64_t expected_idx = comm->fifoHead + 1;
if (slots[0].idx != expected_idx) {
  // FIFO未更新或被覆盖
}
```

**防止ABA问题**：
```
时间线：
t1: Recv1 写 FIFO slot 5, idx=6
t2: Send1 读 FIFO slot 5, idx=6 ✓
t3: Send1 清空 slot 5
t4: Recv2 写 FIFO slot 5 (256次循环后), idx=262
t5: Send2 期望 idx=7，但读到 idx=262 ✗
```

没有idx，Send2可能误读旧数据。

### 3. 多设备支持（Multi-Device）

**虚拟设备（Merged Device）**：
```
物理设备：mlx5_0, mlx5_1, mlx5_2
虚拟设备：mlx5_0+mlx5_1+mlx5_2
```

**每个物理设备独立的资源**：
```cpp
struct ncclIbSendComm {
  struct ncclIbSendCommDev devs[NCCL_IB_MAX_DEVS_PER_NIC];
};

struct ncclIbSendCommDev {
  struct ibv_pd* pd;        // Protection Domain
  struct ibv_cq* cq;        // Completion Queue
  struct ibv_mr* fifoMr;    // FIFO的Memory Region
};
```

**rkey数组**：
```cpp
uint32_t rkeys[NCCL_IB_MAX_DEVS_PER_NIC];
```
原因：同一块内存在不同设备上注册，会得到不同的rkey

**QP轮询**：
```cpp
// 创建QP时轮询设备
qp[0] → dev[0]
qp[1] → dev[1]
qp[2] → dev[2]
qp[3] → dev[0]  // 循环
...
```

### 4. 流量控制

**FIFO深度限制**：
```cpp
#define MAX_REQUESTS 256
```
发送方最多同时处理256个请求。

**背压机制**：
```cpp
if (slots[0].idx != expected_idx) {
  *request = NULL;  // 告诉上层：接收方未准备好
  return ncclSuccess;
}
```
上层会稍后重试。

**事件计数**：
```cpp
struct ncclIbRequest {
  int events[NCCL_IB_MAX_DEVS_PER_NIC];  // 每个设备的待完成事件数
};
```
只有 `events[i] == 0` 对所有设备，请求才算完成。

### 5. 错误处理

**异步事件线程**：
```cpp
pthread_create(&ncclIbAsyncThread, NULL, ncclIbAsyncThreadMain, dev);

void* ncclIbAsyncThreadMain(void* args) {
  while (1) {
    ibv_get_async_event(dev->context, &event);
    switch (event.event_type) {
      case IBV_EVENT_DEVICE_FATAL:
      case IBV_EVENT_CQ_ERR:
      case IBV_EVENT_QP_FATAL:
        // 标记设备故障
        ncclIbStatsFatalError(&dev->stats);
        break;
      // ...
    }
  }
}
```

**每次操作前检查**：
```cpp
NCCLCHECK(ncclIbStatsCheckFatalCount(&comm->base.stats, __func__));
```
如果检测到fatal error，立即返回 `ncclSystemError`。

### 6. FIFO Traffic Class 隔离

**配置独立TC**：
```cpp
qpAttr.ah_attr.grh.traffic_class =
  fifoTc && ncclParamIbFifoTc() != -1 ?
  ncclParamIbFifoTc() : tc;
```

**为什么需要**：
- FIFO是控制流，延迟敏感
- 数据流是吞吐敏感
- 不同TC可配置不同的QoS策略

**典型配置**：
- FIFO TC：高优先级，低延迟队列
- Data TC：最大吞吐量队列

---

## 总结

### 核心设计思想

1. **信号-数据分离**：
   - FIFO：轻量级控制平面
   - RDMA：高速数据平面

2. **接收方驱动**：
   - 接收方主动通知
   - 发送方被动响应
   - 减少同步开销

3. **单边通信**：
   - RDMA WRITE无需远端CPU参与
   - 真正的零拷贝
   - 最小化延迟

### 性能特点

| 特性 | 传统TCP | IB Verbs (双边) | FIFO-RDMA (单边) |
|-----|---------|----------------|------------------|
| 延迟 | ~50μs | ~2μs | **~1μs** |
| CPU使用率 | 高 | 中 | **低** |
| 吞吐量 | ~10 Gb/s | ~50 Gb/s | **~100+ Gb/s** |
| 零拷贝 | ✗ | ✓ | **✓** |
| GPU直达 | ✗ | ✗ | **✓ (GDR)** |

### 适用场景

- **理想场景**：
  - 大规模集群通信（NCCL All-Reduce）
  - GPU-GPU数据传输
  - 高频小消息（FIFO inline）

- **限制**：
  - 需要RDMA硬件支持
  - 内存必须预先注册
  - 不适合不可预测的通信模式

---

## 附录：环境变量

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `NCCL_IB_DISABLE` | 0 | 禁用IB传输 |
| `NCCL_IB_GID_INDEX` | -1 | GID索引（RoCE） |
| `NCCL_IB_TIMEOUT` | 20 | QP超时（2^timeout × 4.096μs） |
| `NCCL_IB_RETRY_CNT` | 7 | 重试次数 |
| `NCCL_IB_USE_INLINE` | 0 | 启用inline data |
| `NCCL_IB_FIFO_TC` | -1 | FIFO的Traffic Class |
| `NCCL_IB_QPS_PER_CONNECTION` | 1 | 每连接QP数 |
| `RCCL_IB_QPS_PER_P2P` | 0 | P2P连接QP数 |
| `NCCL_IB_SPLIT_DATA_ON_QPS` | 0 | 数据分片到多个QP |
| `NCCL_IB_ADAPTIVE_ROUTING` | -2 | 自适应路由 |
| `NCCL_IB_PCI_RELAXED_ORDERING` | 2 | Relaxed Ordering模式 |

---

**文档生成时间**：2026-01-19
**分析代码**：`/home/peng/projects/rdma_all/rccl/src/transport/net_ib.cc`