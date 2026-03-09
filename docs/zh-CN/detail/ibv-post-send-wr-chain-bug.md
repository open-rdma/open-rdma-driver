# ibv_post_send WR链表处理Bug导致RCCL测试挂起

## 问题摘要

**症状**：RCCL测试在sim模式下挂起在 `hipStreamSynchronize()`，进程占用200-300% CPU，持续运行数周不退出。

**根本原因**：`rust-driver/src/verbs/core.rs` 的 `post_send` 函数只处理WR链表的第一个元素，忽略后续的WR节点，导致关键的 `RdmaWriteWithImm` 信号未被发送。

**影响范围**：所有依赖WR链表的RDMA操作，特别是RCCL的集合通信。

## 详细问题分析

### 1. 问题症状

#### 进程状态
```bash
$ ps aux | grep rccl
peng     1538039  211  0.0 18:23:49 ./build/rccl_nompi_test0  # Rank 0
peng     1538040  224  0.0 18:23:49 ./build/rccl_nompi_test1  # Rank 1
```

- 两个进程持续运行16+天
- CPU占用200-300%（多线程busy-wait）
- 进程未退出，未产生任何输出
- 主线程阻塞在 `hipStreamSynchronize()`

#### 日志对比

**Mock模式（成功）**：
```
[INFO] post send wr: Rdma(...opcode: RdmaWrite...), qpn: 1349
[INFO] post send wr: Rdma(...opcode: RdmaWrite...), qpn: 1349
[INFO] post send wr: Rdma(...opcode: RdmaWrite...), qpn: 297
[INFO] post send wr: Rdma(...opcode: RdmaWriteWithImm...), qpn: 297  ← 关键！
[INFO] poll cq returned [RdmaWrite { wr_id: 0 }]
[Rank 0] hipStreamSynchronize() => 0  ← 成功完成
```

**Sim模式（挂起）**：
```
[DEBUG] post_send called... opcode: RdmaWrite ..., qpn: 1283
[DEBUG] post_send called... opcode: RdmaWrite ..., qpn: 1283
[DEBUG] poll_cq returned [RdmaWrite { wr_id: 0 }]
# 缺少 RdmaWriteWithImm！
# hipStreamSynchronize() 永远阻塞
```

统计对比：
| 操作类型 | Mock模式 | Sim模式 | 差异 |
|---------|---------|---------|-----|
| RdmaWrite | 3次 | 2次 | -1 |
| RdmaWriteWithImm | 1次 | **0次** | **-1 (致命)** |

### 2. 根本原因定位

#### 2.1 RCCL代码行为

RCCL在 `ncclIbMultiSend` 函数（`rccl/src/transport/net_ib.cc:2118-2247`）中构造WR链表：

```c
// 构造多个 RdmaWrite WR
for (int r=0; r<nreqs; r++) {
    struct ibv_send_wr* wr = comm->wrs+r;
    memset(wr, 0, sizeof(struct ibv_send_wr));

    wr->opcode = IBV_WR_RDMA_WRITE;
    wr->send_flags = 0;
    wr->next = wr + 1;  // 链接到下一个WR
    wr_id += (reqs[r] - comm->base.reqs) << (r*8);
}

// 设置最后一个WR为 RdmaWriteWithImm（信号）
struct ibv_send_wr* lastWr = comm->wrs+nreqs-1;
if (nreqs > 1 || (comm->ar && reqs[0]->send.size > ncclParamIbArThreshold())) {
    // 当使用 ADAPTIVE_ROUTING 或 multi-send 时：
    // 先发送数据（RDMA_WRITE），然后用 0字节的 RDMA_WRITE_WITH_IMM 触发远端completion
    lastWr++;
    memset(lastWr, 0, sizeof(struct ibv_send_wr));
    if (nreqs > 1) {
        lastWr->wr.rdma.remote_addr = comm->remSizesFifo.addr + slot*...;
        lastWr->num_sge = 1;
        lastWr->sg_list = &comm->remSizesFifo.sge;
    }
}
lastWr->wr_id = wr_id;
lastWr->opcode = IBV_WR_RDMA_WRITE_WITH_IMM;  // 关键操作！
lastWr->imm_data = immData;
lastWr->next = NULL;  // 终止链表
lastWr->send_flags = IBV_SEND_SIGNALED;

// 一次性post整个WR链表
NCCLCHECK(wrap_ibv_post_send(qp->qp, comm->wrs, &bad_wr));
```

**关键点**：
- RCCL设置 `NCCL_IB_AR_THRESHOLD=0`，启用Adaptive Routing优化
- 即使只有1个请求，条件 `(comm->ar && reqs[0]->send.size > 0)` 也会触发
- 会创建一个**额外的0字节RdmaWriteWithImm WR**作为GPU唤醒信号
- 所有WR通过 `wr->next` 指针链接成链表
- **一次post_send调用提交整个链表**

#### 2.2 驱动层Bug

查看 `rust-driver/src/verbs/core.rs:410-436` 的实现：

```rust
fn post_send(
    qp: *mut ibverbs_sys::ibv_qp,
    wr: *mut ibverbs_sys::ibv_send_wr,  // ← 链表头指针
    bad_wr: *mut *mut ibverbs_sys::ibv_send_wr,
) -> ::std::os::raw::c_int {
    let qp = deref_or_ret!(qp, libc::EINVAL);
    let wr_ptr = wr;
    let wr = deref_or_ret!(wr, libc::EINVAL);  // ← 只解引用第一个WR
    let context = qp.context;
    let qp_num = qp.qp_num;
    let mut bluerdma = get_device(context);

    // 只转换第一个WR
    let send_wr = match SendWr::new(wr) {
        Ok(wr) => wr,
        Err(err) => {
            error!("Invalid send WR: {err}");
            unsafe { *bad_wr = wr_ptr };
            return libc::EINVAL;
        }
    };

    // 只发送第一个WR！！！
    match bluerdma.post_send(qp_num, send_wr) {
        Ok(()) => 0,  // ← 直接返回，没有处理 wr->next
        Err(err) => {
            error!("Failed to post send WR: {err}");
            err.to_errno()
        }
    }
}
```

**Bug**：完全没有处理 `wr->next` 链表指针，只发送第一个WR就返回成功！

#### 2.3 标准ibv_post_send行为

参考标准InfiniBand Verbs API规范，`ibv_post_send` 应该：

```c
int ibv_post_send(struct ibv_qp *qp,
                  struct ibv_send_wr *wr,
                  struct ibv_send_wr **bad_wr)
{
    struct ibv_send_wr *current = wr;

    // 遍历整个链表
    while (current != NULL) {
        // 处理并发送当前WR
        if (process_and_post(qp, current) != 0) {
            *bad_wr = current;  // 设置失败的WR
            return errno;
        }
        current = current->next;  // 移动到下一个WR
    }

    *bad_wr = NULL;  // 全部成功
    return 0;
}
```

### 3. 为什么Mock模式能工作？

Mock模式实现在 `rust-driver/src/verbs/mock.rs:498`：

```rust
fn post_send(&mut self, qpn: u32, wr: SendWr) -> crate::error::Result<()> {
    info!("post send wr: {wr:?}, qpn: {qpn}");
    // Mock实现直接处理单个WR，不依赖链表遍历
    // ...
}
```

Mock模式之所以能工作，可能是因为：

1. **测试代码路径不同**：Mock模式可能触发了不同的代码分支，使RdmaWriteWithImm作为独立的post_send调用
2. **调度时序差异**：不同执行时序可能导致WR被分开处理
3. **巧合成功**：碰巧RdmaWriteWithImm不在链表中，而是单独post

但这只是巧合，不能掩盖驱动的根本bug。

### 4. 为什么导致挂起？

执行流程：

```
1. RCCL代理线程调用 ncclIbMultiSend()
2. 构造WR链表：[RdmaWrite] -> [RdmaWrite] -> [RdmaWriteWithImm]
3. 调用 ibv_post_send(qp, wr_chain, &bad_wr)
4. 驱动bug：只发送第一个RdmaWrite，忽略链表其余部分
5. GPU kernel在HIP stream上等待RDMA completion
6. RdmaWriteWithImm信号从未发送 → GPU永远不会被唤醒
7. hipStreamSynchronize() 阻塞主线程
8. 代理线程在 SingleThreadPollingWorker::run() 中busy-wait
9. 进程占用200-300% CPU，永不退出
```

相关代码位置：
- `rust-driver/src/workers/spawner.rs:30-36` - 无sleep的busy-wait循环
- RCCL主线程阻塞在 `hipStreamSynchronize()`

## 修复方案

### 方案1：正确实现WR链表遍历（推荐） ✅ 已实施

**修复时间**：2025-01-19

同时修改 `rust-driver/src/verbs/core.rs` 的 `post_send` 和 `post_recv` 函数：

```rust
fn post_send(
    qp: *mut ibverbs_sys::ibv_qp,
    wr: *mut ibverbs_sys::ibv_send_wr,
    bad_wr: *mut *mut ibverbs_sys::ibv_send_wr,
) -> ::std::os::raw::c_int {
    let qp = deref_or_ret!(qp, libc::EINVAL);
    let context = qp.context;
    let qp_num = qp.qp_num;
    let mut bluerdma = get_device(context);

    // 遍历整个WR链表
    let mut current_wr_ptr = wr;
    while !current_wr_ptr.is_null() {
        let current_wr = unsafe { &*current_wr_ptr };

        // 转换当前WR
        let send_wr = match SendWr::new(*current_wr) {
            Ok(wr) => wr,
            Err(err) => {
                error!("Invalid send WR: {err}");
                unsafe { *bad_wr = current_wr_ptr };
                return libc::EINVAL;
            }
        };

        // 发送当前WR
        if let Err(err) = bluerdma.post_send(qp_num, send_wr) {
            error!("Failed to post send WR: {err}");
            unsafe { *bad_wr = current_wr_ptr };
            return err.to_errno();
        }

        // 移动到链表下一个节点
        current_wr_ptr = current_wr.next;
    }

    // 全部成功
    unsafe { *bad_wr = std::ptr::null_mut() };
    0
}
```

### 方案2：临时workaround（不推荐）

如果无法立即修改驱动，可以在RCCL中禁用AR优化：

```bash
export NCCL_IB_ADAPTIVE_ROUTING=0
# 或者
export NCCL_IB_AR_THRESHOLD=999999999
```

但这会牺牲性能，并且不能解决其他使用WR链表的场景。

## 测试验证

### 复现步骤

1. 编译RCCL测试：
```bash
cd /home/peng/projects/rdma_all/open-rdma-driver/tests/rccl_test
make clean && make
```

2. 运行sim模式测试：
```bash
# Terminal 1
make sim_rank0

# Terminal 2
make sim_rank1
```

3. 观察现象：
```bash
# 进程挂起，不退出
ps aux | grep rccl

# 检查日志
grep "RdmaWriteWithImm" log/sim/rccl-*.log  # 应该找不到
```

### 验证修复

1. 应用上述patch到 `rust-driver/src/verbs/core.rs`
2. 重新编译驱动：
```bash
cd /home/peng/projects/rdma_all/open-rdma-driver/rust-driver
cargo build --release --features sim
```
3. 重新运行RCCL测试
4. 验证点：
   - 日志中应该出现RdmaWriteWithImm操作
   - `hipStreamSynchronize()` 应该成功返回
   - 测试应该在数秒内完成
   - CPU占用应该正常

## 相关文件

### 驱动层
- `rust-driver/src/verbs/core.rs:410-436` - **Bug所在位置**
- `rust-driver/src/verbs/ctx.rs:649-688` - VerbsOps trait实现
- `rust-driver/src/verbs/mock.rs:498-520` - Mock模式实现
- `rust-driver/src/rdma_utils/types.rs:23-93` - SendWr结构定义

### RCCL层
- `rccl/src/transport/net_ib.cc:2118-2247` - ncclIbMultiSend()函数
- `rccl/src/transport/net_ib.cc:2153-2171` - RdmaWriteWithImm WR构造
- `rccl/src/include/ibvwrap.h:74-81` - wrap_ibv_post_send()宏定义

### 日志
- `tests/rccl_test/log/sim/rccl-1.log` - Sim模式日志（bug复现）
- `tests/rccl_test/log/mock/rccl-1.log` - Mock模式日志（正常工作）

## 时间线

- **2025-01-05**：发现sim模式RCCL测试挂起，进程持续运行数周
- **2025-01-18**：更新mock模式日志，发现RdmaWriteWithImm差异
- **2025-01-19**：定位到core.rs的post_send未处理WR链表
- **2025-01-19**：记录详细分析文档

## 参考资料

1. [InfiniBand Verbs Specification](https://www.openfabrics.org/downloads/verbs_interface.pdf) - Section on ibv_post_send
2. RCCL文档：`build-docs/learn/02-跨层综合/RCCL集合通信与RDMA-Write-Immediate机制.md`
3. libibverbs源码：`rdma-core/libibverbs/verbs.c` - 标准实现参考

## 附录：环境配置

测试环境变量：
```bash
NCCL_IB_ADAPTIVE_ROUTING=1
NCCL_IB_AR_THRESHOLD=0      # 触发bug的关键配置
NCCL_IB_DISABLE=0
NCCL_P2P_LEVEL=LOC
NCCL_SHM_DISABLE=1
RUST_LOG=debug
```

硬件：
- GPU: AMD (ROCm支持)
- RDMA设备: bluerdma0 (自研RDMA卡)
- 连接方式: Sim模式通过UDP连接RTL仿真器

软件版本：
- RCCL: Custom build based on ROCm RCCL
- rust-driver: 自研Rust RDMA驱动
- 内核: Linux 6.8.0-88-generic