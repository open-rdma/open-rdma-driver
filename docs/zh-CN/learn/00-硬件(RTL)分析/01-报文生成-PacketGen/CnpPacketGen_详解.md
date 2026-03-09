# CnpPacketGen.bsv 详解

## 文件概述

**位置**: `open-rdma-rtl/src/CnpPacketGen.bsv`

**功能**: CNP（Congestion Notification Packet，拥塞通知包）生成器，用于 RDMA 网络中的拥塞控制机制。

---

## CNP 拥塞控制机制

### 什么是 CNP？

CNP 是 RDMA 网络中用于拥塞控制的特殊数据包：
- 当网络交换机或接收端检测到拥塞时，会向发送端发送 CNP 包
- 发送端收到 CNP 后会降低发送速率，避免网络拥塞加剧
- 这是基于 ECN（Explicit Congestion Notification）机制的拥塞控制

### 工作原理

1. **拥塞检测**: 网络中的交换机或接收端检测到拥塞（通过 IP 头中的 ECN 标志）
2. **CNP 生成**: 本模块生成 CNP 包，通知对端降低发送速率
3. **速率限制**: 使用计数器防止 CNP 包泛滥，避免过度反应

---

## 数据结构

### 1. CnpPacketGenReq - CNP 生成请求

```bsv
typedef struct {
    ThinMacIpUdpMetaDataForRecv peerAddrInfo;  // 对端地址信息（MAC/IP/UDP）
    QPN                         peerQpn;        // 对端 QP 编号
    MSN                         peerMsn;        // 对端消息序列号
    UdpPort                     localUdpPort;   // 本地 UDP 端口
} CnpPacketGenReq deriving(Bits, FShow);
```

**作用**: 封装生成一个 CNP 包所需的全部信息。

### 2. CnpGenContextEntry - CNP 上下文条目

```bsv
typedef struct {
    CnpPauseCounter cnpPauseCounter;  // 上次发送 CNP 的时间戳
} CnpGenContextEntry deriving(Bits, FShow);
```

**作用**: 为每个 QP 维护一个上下文，记录上次发送 CNP 的时间，防止短时间内重复发送。

### 3. 关键常量

```bsv
typedef 32 CNP_PAUSE_COUNTER_WIDTH;           // 计数器位宽：32 位
typedef 8192 CNP_PAUSE_TICK_CNT;              // 暂停周期：8192 个时钟周期
```

**CNP_PAUSE_TICK_CNT**: 两次 CNP 发送之间的最小间隔（8192 个时钟周期）
- 防止 CNP 包泛滥
- 给网络足够的时间响应拥塞控制

---

## 模块接口

### CnpPacketGenerator 接口

```bsv
interface CnpPacketGenerator;
    // 输入：CNP 生成请求
    interface PipeInB0#(CnpPacketGenReq) genReqPipeIn;

    // 输出：MAC/IP/UDP 元数据
    interface PipeOut#(ThinMacIpUdpMetaDataForSend) macIpUdpMetaPipeOut;

    // 输出：RDMA 包元数据
    interface PipeOut#(RdmaSendPacketMeta) rdmaPacketMetaPipeOut;

    // 输出：RDMA 载荷（CNP 无载荷，此接口为空）
    interface PipeOut#(DataStream) rdmaPayloadPipeOut;
endinterface
```

---

## 模块实现

### 内部组件

```bsv
// 1. 输入请求队列
PipeInAdapterB0#(CnpPacketGenReq) genReqPipeInQueue <- mkPipeInAdapterB0;

// 2. 输出队列
FIFOF#(ThinMacIpUdpMetaDataForSend) macIpUdpMetaPipeOutQueue <- mkSizedFIFOF(4);
FIFOF#(RdmaSendPacketMeta) rdmaPacketMetaPipeOutQueue <- mkSizedFIFOF(4);
FIFOF#(DataStream) rdmaPayloadPipeOutQueue <- mkFIFOF;  // 哑队列，CNP 无载荷

// 3. 上下文存储（每个 QP 一个条目）
AutoInferBramQueuedOutput#(IndexQP, CnpGenContextEntry) storage <- ...;

// 4. 流水线队列
FIFOF#(CnpPacketGenReq) genPacketPipelineQueue <- mkSizedFIFOF(4);

// 5. 全局计数器（每时钟周期自增）
Reg#(CnpPauseCounter) cnpPauseCounterNowReg <- mkReg(0);
```

### 工作流程

#### Rule 1: incrCounter - 全局计数器

```bsv
rule incrCounter;
    cnpPauseCounterNowReg <= cnpPauseCounterNowReg + 1;
endrule
```

**作用**: 每个时钟周期自增全局计数器，用作时间戳。

#### Rule 2: genContextReadReq - 读取上下文

```bsv
rule genContextReadReq;
    let req = genReqPipeInQueue.first;
    genReqPipeInQueue.deq;
    storage.putReadReq(getIndexQP(req.peerQpn));  // 根据 QPN 读取上下文
    genPacketPipelineQueue.enq(req);              // 请求进入流水线
endrule
```

**作用**:
1. 接收 CNP 生成请求
2. 发起对应 QP 上下文的读取请求
3. 将请求传递到下一级流水线

#### Rule 3: genPacket - 生成 CNP 包

```bsv
rule genPacket;
    let req = genPacketPipelineQueue.first;
    genPacketPipelineQueue.deq;

    let cnpCtx = storage.readRespPipeOut.first;
    storage.readRespPipeOut.deq;

    // 关键逻辑：检查是否满足发送条件
    if (cnpPauseCounterNowReg - cnpCtx.cnpPauseCounter > fromInteger(valueOf(CNP_PAUSE_TICK_CNT))) begin
        // ... 生成 CNP 包 ...
        storage.write(getIndexQP(req.peerQpn),
                     CnpGenContextEntry{cnpPauseCounter: cnpPauseCounterNowReg});
    end
endrule
```

**核心逻辑**:

```
当前时间 - 上次发送时间 > 8192 时钟周期
```

- **满足条件**: 生成并发送 CNP 包，更新上下文时间戳
- **不满足**: 丢弃请求，避免频繁发送

---

## CNP 包结构

### 1. MAC/IP/UDP 元数据

```bsv
ThinMacIpUdpMetaDataForSend {
    dstMacAddr    : req.peerAddrInfo.srcMacAddr,  // 目标 MAC（对端 MAC）
    ipDscp        : 0,                             // DSCP 字段
    ipEcn         : pack(IpHeaderEcnFlagEnabled),  // ECN 标志（关键！）
    dstIpAddr     : req.peerAddrInfo.srcIpAddr,    // 目标 IP（对端 IP）
    srcPort       : req.localUdpPort,              // 源端口
    dstPort       : fromInteger(valueOf(UDP_PORT_RDMA)), // 目标端口（RDMA 标准端口）
    udpPayloadLen : fromInteger(valueOf(RDMA_FIXED_HEADER_BYTE_NUM)), // 载荷长度（仅头部）
    ethType       : fromInteger(valueOf(ETH_TYPE_IP))  // 以太网类型（IPv4）
}
```

**关键点**:
- `ipEcn`: 设置为 `IpHeaderEcnFlagEnabled`，表示这是一个 ECN 相关的包
- 目标地址是对端地址（源地址互换）

### 2. RDMA 包元数据

```bsv
RdmaSendPacketMeta {
    header: RdmaBthAndExtendHeader {
        bth: BTH {
            trans    : TRANS_TYPE_CNP,  // 传输类型：CNP
            opcode   : unpack(0),       // 操作码：0（CNP 特定）
            solicited: False,
            isRetry  : False,
            padCnt   : unpack(0),
            tver     : unpack(0),
            msn      : req.peerMsn,     // 对端消息序列号
            fecn     : unpack(0),
            becn     : unpack(0),
            resv6    : unpack(0),
            dqpn     : req.peerQpn,     // 目标 QPN（对端 QPN）
            ackReq   : False,           // 无需 ACK
            resv7    : unpack(0),
            psn      : unpack(0)        // PSN：0（CNP 不使用）
        },
        rdmaExtendHeaderBuf: unpack(0)
    },
    hasPayload: False  // CNP 无载荷
}
```

**关键点**:
- `trans`: 设置为 `TRANS_TYPE_CNP`，标识这是 CNP 包
- `dqpn`: 目标 QPN 是对端 QPN
- `hasPayload`: False，CNP 包只有头部，无载荷数据

---

## 流水线处理

### 流水线阶段

```
[请求输入] → [读上下文] → [生成 CNP] → [输出]
    ↓            ↓             ↓           ↓
genReqPipeIn  storage.read  genPacket  macIpUdpMetaPipeOut
                                        rdmaPacketMetaPipeOut
```

### 流水线优势

1. **并行处理**: 多个请求可以同时处于不同阶段
2. **高吞吐**: 避免阻塞等待
3. **BRAM 访问优化**: 读上下文和生成包分离，允许 BRAM 流水线访问

---

## 速率限制机制

### 为什么需要速率限制？

1. **防止 CNP 泛滥**: 如果每次检测到拥塞都发送 CNP，可能会产生大量 CNP 包
2. **避免震荡**: 给发送端足够的时间响应第一个 CNP，避免过度调整
3. **网络稳定性**: 保持拥塞控制的稳定性

### 实现方式

```bsv
if (cnpPauseCounterNowReg - cnpCtx.cnpPauseCounter > fromInteger(valueOf(CNP_PAUSE_TICK_CNT)))
```

- **全局计数器**: `cnpPauseCounterNowReg`（每时钟周期 +1）
- **上次发送时间**: `cnpCtx.cnpPauseCounter`
- **最小间隔**: 8192 个时钟周期

### 时间计算示例

假设时钟频率为 250 MHz：
- 1 个时钟周期 = 4 ns
- 8192 个时钟周期 = 32.768 μs
- **最小 CNP 间隔**: 约 33 微秒

---

## 上下文存储管理

### 存储结构

```bsv
AutoInferBramQueuedOutput#(IndexQP, CnpGenContextEntry) storage
```

- **索引**: QP 索引（`getIndexQP(req.peerQpn)`）
- **内容**: `CnpGenContextEntry`（包含上次发送时间戳）
- **类型**: BRAM（Block RAM），硬件片上存储

### 访问模式

1. **读取**: `storage.putReadReq(qpIndex)` → `storage.readRespPipeOut.first`
2. **写入**: `storage.write(qpIndex, entry)` - 更新时间戳
3. **并发**: 支持流水线读写

---

## 与 RDMA 拥塞控制的关系

### 拥塞检测触发点

本模块接收 `CnpPacketGenReq` 请求，这些请求通常来自：

1. **接收端拥塞检测**:
   - 接收队列满
   - 丢包检测
   - 延迟增加

2. **ECN 标记检测**:
   - IP 头部 ECN 字段被交换机标记
   - 表明网络路径上有拥塞

### CNP 的后续处理

1. **发送到对端**: CNP 包通过网络发送到拥塞源（发送端）
2. **对端响应**: 发送端收到 CNP 后降低发送速率
3. **速率恢复**: 在一定时间后逐步恢复发送速率

---

## 设计要点总结

### 1. 速率限制
- 使用全局计数器和 per-QP 时间戳
- 8192 时钟周期的最小间隔
- 防止 CNP 包泛滥

### 2. 流水线设计
- 请求接收 → 上下文读取 → 包生成
- 提高吞吐量
- BRAM 访问优化

### 3. 简洁的包结构
- CNP 包只有头部，无载荷
- 使用特殊的传输类型（TRANS_TYPE_CNP）
- ECN 标志启用

### 4. 上下文管理
- Per-QP 上下文存储
- 使用 BRAM 存储
- 自动推断存储类型

---

## 应用场景

### 典型流程

```
1. [交换机/接收端] 检测到拥塞
   ↓
2. [本模块] 接收 CnpPacketGenReq
   ↓
3. [本模块] 检查是否满足发送条件（速率限制）
   ↓
4. [本模块] 生成 CNP 包
   ↓
5. [网络] CNP 包发送到对端
   ↓
6. [对端] 降低发送速率
   ↓
7. [网络] 拥塞缓解
```

### 与其他模块的协作

- **输入来源**: 拥塞检测模块（检测到拥塞后发起请求）
- **输出去向**: 发送引擎（将 CNP 包封装并发送）
- **配合机制**: ECN 标记、速率控制算法

---

## 总结

`CnpPacketGen.bsv` 是 RDMA 网络中拥塞控制的关键组件：

1. **功能**: 生成 CNP（拥塞通知包），通知对端降低发送速率
2. **机制**: 基于 ECN 的显式拥塞通知
3. **优化**: 速率限制防止 CNP 泛滥，流水线设计提高性能
4. **存储**: Per-QP 上下文管理，记录发送历史
5. **意义**: 保证 RDMA 网络的稳定性和公平性

这个模块体现了硬件拥塞控制的精妙设计：简单高效、低延迟、高可靠。
