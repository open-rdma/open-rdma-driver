# Base Test Refactoring

## 概述

本次重构旨在消除 base_test 目录中大量重复的代码,提供一个模块化、可维护的 RDMA 测试框架。

## 问题分析

原有测试代码存在以下问题:

1. **大量重复代码**: 每个测试文件都重复实现了设备初始化、QP 管理、TCP 连接等功能
2. **难以维护**: 修改公共逻辑需要在多个文件中重复修改
3. **代码冗长**: 单个测试文件动态辄 500-700 行,大部分是样板代码
4. **缺乏抽象**: 没有清晰的抽象层次,混杂了底层操作和测试逻辑

## 新架构

### 模块组织

```
base_test/
├── rdma_common.h/c      - RDMA 基础操作
├── rdma_transport.h/c   - 传输层抽象(TCP)
├── rdma_debug.h/c       - 调试和验证工具
├── loopback_new.c       - 重构后的 loopback 测试
├── send_recv_new.c      - 重构后的 send/recv 测试
└── [原有测试文件保留用于对比]
```

### 模块说明

#### 1. rdma_common (RDMA 基础操作)

**功能:**
- 设备枚举和打开
- Protection Domain (PD) 分配
- Completion Queue (CQ) 创建
- Queue Pair (QP) 创建和状态转换
- Memory Region (MR) 注册
- Buffer 管理

**核心 API:**
```c
// 配置结构
struct rdma_config {
    int dev_index;
    size_t buffer_size;
    int max_send_wr;
    int max_recv_wr;
    // ...
};

// 初始化和清理
int rdma_init_context(struct rdma_context *ctx, const struct rdma_config *config);
void rdma_destroy_context(struct rdma_context *ctx);

// QP 状态转换
int rdma_qp_to_init(struct ibv_qp *qp);
int rdma_qp_to_rtr(struct ibv_qp *qp, uint32_t dest_qp_num);
int rdma_qp_to_rts(struct ibv_qp *qp);
int rdma_connect_qp(struct ibv_qp *qp, uint32_t dest_qp_num);  // 组合操作
```

**优势:**
- 封装了繁琐的设备初始化流程
- 提供了统一的 QP 状态转换接口
- 支持灵活的配置选项
- 内置错误处理和资源清理

#### 2. rdma_transport (传输层抽象)

**功能:**
- TCP 连接管理 (client/server)
- QP 信息交换
- 同步原语

**核心 API:**
```c
// TCP 传输
int tcp_server_init(struct tcp_transport *transport, int port);
int tcp_server_accept(struct tcp_transport *transport);
int tcp_client_connect(struct tcp_transport *transport, const char *server_ip,
                      int port, int max_retries);
void tcp_transport_close(struct tcp_transport *transport);

// QP 信息交换
int rdma_exchange_qp_info(int sock_fd, const struct qp_info *local_info,
                          struct qp_info *remote_info);

// 同步原语
int rdma_handshake(int sock_fd);
int rdma_sync_client_to_server(int sock_fd);
int rdma_sync_server_to_client(int sock_fd);
```

**优势:**
- 抽象了 TCP 连接细节
- 统一的信息交换协议
- 内置重试机制
- 明确的同步语义

#### 3. rdma_debug (调试工具)

**功能:**
- 内存对比和差异可视化
- 十六进制内存打印
- 零字节范围检测

**核心 API:**
```c
// 内存对比(返回差异字节数)
size_t rdma_memory_diff(const char *buf1, const char *buf2, size_t length);

// 内存打印
void rdma_print_memory_hex(const void *start_addr, size_t length);

// 零字节检测
void rdma_print_zero_ranges(const char *buffer, size_t length);

// 编译器屏障
#define COMPILER_BARRIER() asm volatile("" ::: "memory")
```

**优势:**
- 彩色差异显示
- 上下文感知的内存打印
- 方便的调试辅助工具

## 代码对比

### 原有代码 (send_recv.c - 559 行)

```c
// 设备初始化 - 约 90 行
void setup_ib(struct rdma_context *ctx, bool is_client, int msg_len) {
    struct ibv_device **dev_list = ibv_get_device_list(NULL);
    // ... 大量重复代码
    ctx->ctx = ibv_open_device(dev_list[dev_index]);
    ctx->pd = ibv_alloc_pd(ctx->ctx);
    // ... 更多样板代码
}

// QP 设置 - 约 67 行
void setup_qp(struct rdma_context *ctx, uint32_t dqpn, bool is_client) {
    struct ibv_qp_attr attr = {/* ... */};
    if (ibv_modify_qp(ctx->qp, &attr, /* flags */)) die("...");
    // ... 三次状态转换,大量重复参数
}

// Server 函数 - 约 120 行
void run_server(int msg_len) {
    // TCP 设置
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    // ... bind, listen, accept

    // IB 设置
    setup_ib(&ctx, false, msg_len);

    // ... 更多代码
}
```

### 重构后代码 (send_recv_new.c - 约 200 行)

```c
int run_server(int msg_len) {
    struct rdma_context ctx;
    struct rdma_config config;
    struct tcp_transport transport;

    // 简洁的配置
    rdma_default_config(&config);
    config.buffer_size = msg_len;

    // 一行初始化
    if (rdma_init_context(&ctx, &config) < 0) return -1;

    // 一行 TCP 设置
    if (tcp_server_init(&transport, DEFAULT_PORT) < 0) { /* ... */ }
    if (tcp_server_accept(&transport) < 0) { /* ... */ }

    // 一行 QP 连接
    if (rdma_connect_qp(ctx.qp, remote_info.qp_num) < 0) { /* ... */ }

    // 核心测试逻辑
    // ...

    // 一行清理
    rdma_destroy_context(&ctx);
}
```

### 代码量对比

| 测试 | 原版行数 | 重构后行数 | 减少比例 |
|------|---------|-----------|---------|
| loopback | ~600 | ~180 | 70% |
| send_recv | ~559 | ~220 | 61% |

**注:** 重构后的代码更清晰,注释更多,实际核心代码量减少更明显。

## 使用示例

### Loopback 测试

```bash
# 编译新测试
make loopback_new

# 运行(4KB 消息, 5 轮测试)
./build/loopback_new 4096 5
```

### Send/Recv 测试

```bash
# 编译
make send_recv_new

# Server 端
./build/send_recv_new 4096

# Client 端(另一个终端)
./build/send_recv_new 4096 127.0.0.1
```

## 构建系统

### Makefile 目标

```bash
make all        # 构建所有测试(新旧版本)
make new        # 只构建重构后的测试
make legacy     # 只构建原有测试
make clean      # 清理构建产物
```

### 编译产物

```
build/
├── obj/                    # 公共库对象文件
│   ├── rdma_common.o
│   ├── rdma_transport.o
│   └── rdma_debug.o
├── loopback_new           # 新版测试
├── send_recv_new
├── loopback               # 旧版测试(保留)
└── send_recv
```

## 迁移指南

### 将现有测试迁移到新框架

1. **包含头文件**
   ```c
   #include "rdma_common.h"
   #include "rdma_transport.h"  // 如果需要 TCP
   #include "rdma_debug.h"      // 如果需要调试工具
   ```
