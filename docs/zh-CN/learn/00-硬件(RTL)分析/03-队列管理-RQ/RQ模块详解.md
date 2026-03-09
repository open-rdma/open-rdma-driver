# RQ (接收队列) 模块详解

## 目录
1. [模块概述](#模块概述)
2. [接口定义](#接口定义)
3. [核心组件](#核心组件)
4. [流水线架构](#流水线架构)
5. [数据流详解](#数据流详解)
6. [安全验证机制](#安全验证机制)
7. [错误处理](#错误处理)
8. [工作流程图](#工作流程图)

---

## 模块概述

**文件位置**: `open-rdma-rtl/src/RQ.bsv`

**模块定义**: `module mkRQ#(Word channelIdx)(RQ)`

RQ（Receive Queue，接收队列）是RDMA系统接收路径的核心模块，负责：
- 接收和解析来自网络的RDMA数据包
- 验证包的合法性（QP上下文、内存区域权限等）
- 处理载荷数据的DMA写入
- 生成ACK和元数据报告
- 实现拥塞控制（CNP包生成）

**参数化设计**:
- `channelIdx`: 通道索引，支持多通道并行处理
- `(* synthesize *)` 属性表示该模块可以独立综合

---

## 接口定义

```bsv
interface RQ;
    // CSR (Control and Status Register) 访问接口
    interface BlueRdmaCsrUpStreamPort csrUpStreamPort;

    // 表查询接口
    interface ClientP#(ReadReqQPC, Maybe#(EntryQPC)) qpcQueryClt;
    interface ClientP#(MrTableQueryReq, Maybe#(MemRegionTableEntry)) mrTableQueryClt;

    // 数据输入接口
    interface PipeInB0#(IoChannelEthDataStream) ethernetFramePipeIn;
    interface PipeOut#(DataStream) otherRawPacketPipeOut;
    method Action setLocalNetworkSettings(LocalNetworkSettings networkSettings);

    // 载荷处理接口
    interface PipeOut#(PayloadConReq) payloadConReqPipeOut;
    interface PipeOut#(DataStream) payloadConStreamPipeOut;
    interface PipeIn#(Bool) payloadConRespPipeIn;

    // 输出接口
    interface PipeOut#(RingbufRawDescriptor) metaReportDescPipeOut;
    interface PipeOut#(AutoAckGeneratorReq) autoAckGenReqPipeOut;
    interface PipeOut#(CnpPacketGenReq) genCnpReqPipeOut;
endinterface
```

### 接口功能说明

| 接口 | 方向 | 功能 |
|------|------|------|
| `csrUpStreamPort` | 双向 | CSR访问，用于配置和监控 |
| `qpcQueryClt` | 输出 | 查询QP上下文（QPC） |
| `mrTableQueryClt` | 输出 | 查询内存区域表（MR Table） |
| `ethernetFramePipeIn` | 输入 | 接收以太网帧数据流 |
| `otherRawPacketPipeOut` | 输出 | 输出非RDMA包（如ARP、ICMP） |
| `payloadConReqPipeOut` | 输出 | 载荷消费请求（DMA写请求） |
| `payloadConStreamPipeOut` | 输出 | 载荷数据流 |
| `payloadConRespPipeIn` | 输入 | 载荷消费响应 |
| `metaReportDescPipeOut` | 输出 | 元数据报告描述符（给软件驱动） |
| `autoAckGenReqPipeOut` | 输出 | 自动ACK生成请求 |
| `genCnpReqPipeOut` | 输出 | CNP包生成请求（拥塞通知） |

---

## 核心组件

### 1. 包解析器 (PacketParse)
```bsv
PacketParse packetParser <- mkPacketParse;
```
- 解析以太网帧，提取RDMA头部信息
- 分离包头和载荷数据
- 输出三个流：
  - `rdmaPacketMetaPipeOut`: RDMA包元数据
  - `rdmaMacIpUdpMetaPipeOut`: MAC/IP/UDP层信息
  - `rdmaPayloadPipeOut`: 载荷数据流

### 2. 查询客户端
```bsv
QueuedClientP#(ReadReqQPC, Maybe#(EntryQPC)) qpcQueryCltInst;
QueuedClientP#(MrTableQueryReq, Maybe#(MemRegionTableEntry)) mrTableQueryCltInst;
```
- **QPC查询**: 获取QP配置信息（PMTU、访问权限、对端QPN等）
- **MR表查询**: 获取内存区域信息（基地址、长度、访问权限）

### 3. 载荷存储队列
```bsv
FIFOF#(DataStream) payloadStorage <- mkSizedFIFOF(valueOf(PAYLOAD_STORAGE_CAPACITY_FOR_RQ_INPUT_DATA_STREAM_BUF));
```
- 缓冲载荷数据，等待验证完成
- 容量根据系统参数配置
- 与包解析器直接连接

### 4. 流水线队列

| 队列名 | 深度 | 功能 |
|--------|------|------|
| `checkQpcAndMrTablePipeQ` | 4 | 存储等待QPC/MR表查询结果的包 |
| `checkMrTableStep2PipeQ` | 2 | MR边界检查第2步 |
| `checkMrTableStep3PipeQ` | 2 | MR边界检查第3步 |
| `issuePayloadConReqOrDiscardPipeQ` | 2 | 决定是否消费载荷 |
| `handleConRespPipeQ` | 动态 | 处理消费响应（深度基于MAX_PMTU） |
| `handleGenMetaReportQueueDescPipeQ` | LFIFOF | 生成元数据描述符 |

### 5. 元数据报告MIMO队列
```bsv
MIMO#(NUMERIC_TYPE_TWO, NUMERIC_TYPE_ONE, NUMERIC_TYPE_FOUR, RingbufRawDescriptor) metaReportMimoQueue;
```
- 支持一次入队2个描述符（如READ_REQUEST需要扩展描述符）
- 一次出队1个描述符
- 深度为4

### 6. 性能计数器

模块包含丰富的错误计数器用于调试和监控：

```bsv
Reg#(Dword) metricsInvalidQpAccessCntReg        // QP访问权限错误
Reg#(Dword) metricsInvalidOpcodeCntReg          // 非法操作码
Reg#(Dword) metricsInvalidMrKeyCntReg           // MR密钥错误
Reg#(Dword) metricsMemAccessOutOfBoundCntReg    // 内存访问越界
Reg#(Dword) metricsInvalidMrAccessFlagCntReg    // MR访问权限错误
Reg#(Dword) metricsInvalidHeaderCntReg          // 包头格式错误
Reg#(Dword) metricsInvalidQpContextCntReg       // QP上下文无效
Reg#(Dword) metricsCorruptPktLengthCntReg       // 包长度错误
Reg#(Dword) metricsUnknownErrorCntReg           // 未知错误
```

这些计数器通过CSR接口暴露给软件，可实时监控接收队列的健康状态。

---

## 流水线架构

RQ模块采用9级流水线设计，每个阶段处理特定的验证和转换任务：

```
[包解析] → [QPC/MR查询] → [权限检查] → [MR边界检查Step2] → [MR边界检查Step3]
    → [发起消费请求] → [处理消费响应] → [生成元数据描述符] → [输出描述符]
```

### 流水线数据结构演进

```
CheckQpcAndMrTablePipelineEntry (L38-46)
  ├─ rdmaPacketMeta         // RDMA包元数据
  ├─ packetStatus           // 包状态（正常/错误类型）
  ├─ isNeedQueryMrTable     // 是否需要MR表查询
  ├─ isZeroPayload          // 是否零长度载荷
  ├─ isFirstPacket          // 是否首包
  └─ peerMacIpUdpMeta       // 对端网络信息
      ↓
CheckMrTableStep2PipelineEntry (L48-61)
  ├─ [继承上述字段]
  ├─ mrEntry                // MR表项（查询结果）
  ├─ qpc                    // QP上下文（查询结果）
  ├─ isMrLowerAddrBoundOk   // MR下界检查结果
  ├─ zerobasedExpectedPayloadBeatNum  // 预期载荷beat数
  ├─ packetLen              // 包长度
  └─ deltaLen               // 地址偏移量（中间值）
      ↓
CheckMrTableStep3PipelineEntry (L63-76)
  └─ [字段相同，用于流水线时序平衡]
      ↓
IssuePayloadConReqOrDiscardPipelineEntry (L78-89)
  └─ [移除isMrLowerAddrBoundOk，已完成检查]
      ↓
HandleConRespPipelineEntry (L91-102)
  └─ [字段相同]
      ↓
GenMetaReportQueueDescPipelineEntry (L104-107)
  └─ rdmaPacketMeta         // 只保留元数据，生成描述符
```

---

## 数据流详解

### Stage 1: 包解析与查询发起 (`sendQpcQueryReqAndSomeSimpleParse`)

**文件位置**: RQ.bsv:275-340

**功能**: 接收包解析器输出，发起QPC和MR表查询

**详细流程**:

```bsv
rule sendQpcQueryReqAndSomeSimpleParse;
    // 1. 从包解析器获取元数据
    let rdmaPacketMeta = packetParser.rdmaPacketMetaPipeOut.first;
    let peerMacIpUdpMeta = packetParser.rdmaMacIpUdpMetaPipeOut.first;

    // 2. 提取关键头部字段
    let bth = rdmaPacketMeta.header.bth;           // Base Transport Header
    let reth = extractPriRETH(...);                // RDMA Extended Transport Header

    // 3. 发起QPC查询
    qpcQueryCltInst.putReq(ReadReqQPC{
        qpn: bth.dqpn,         // 目标QP号
        needCheckKey: True     // 需要验证密钥
    });

    // 4. 判断是否需要MR表查询
    let isRespNeedDMAWrite = rdmaRespNeedDmaWrite(bth.opcode);  // RDMA READ响应
    let isReqNeedDMAWrite = rdmaReqNeedDmaWrite(bth.opcode);    // SEND/WRITE请求
    let isNeedQueryMrTable = isRespNeedDMAWrite || isReqNeedDMAWrite;

    // 5. 如果需要，发起MR表查询
    if (isNeedQueryMrTable) begin
        mrTableQueryCltInst.putReq(MrTableQueryReq{
            idx: rkey2IndexMR(reth.rkey)
        });

        // 范围检查：MR索引是否溢出
        if (getMrRawIndexPartFromRkey(reth.rkey) >= MAX_MR) begin
            packetStatus = RdmaRecvPacketStatusMrIdxOverflow;
        end
    end

    // 6. 范围检查：QPN是否溢出
    if (getQpnRawIndexPart(bth.dqpn) >= MAX_QP) begin
        packetStatus = RdmaRecvPacketStatusQpIdxOverflow;
    end

    // 7. 构造流水线条目，传递给下一级
    checkQpcAndMrTablePipeQ.enq(pipelineEntryOut);
endrule
```

**关键判断**:

| 操作类型 | isReqNeedDMAWrite | isRespNeedDMAWrite | 需要MR查询 |
|----------|-------------------|--------------------| --------- |
| SEND | ✓ | ✗ | ✓ |
| WRITE | ✓ | ✗ | ✓ |
| READ请求 | ✗ | ✗ | ✗ |
| READ响应 | ✗ | ✓ | ✓ |
| ACK | ✗ | ✗ | ✗ |

**时序特性**:
- 查询是异步的，响应会在后续周期返回
- QPC查询延迟：最坏11个周期
- MR表查询延迟：最坏11个周期
- 流水线并行处理，不需要等待

---

### Stage 2: 权限检查 (`checkQpcAndMrTable`)

**文件位置**: RQ.bsv:342-523

**功能**: 接收查询响应，验证QP和MR权限，计算包长度

**详细流程**:

```bsv
rule checkQpcAndMrTable;
    let pipelineEntryIn = checkQpcAndMrTablePipeQ.first;

    // 1. 获取QPC查询结果
    let qpcMaybe <- qpcQueryCltInst.getResp;
    if (qpcMaybe matches tagged Valid .qpc) begin
        // 1.1 检查QP密钥
        if (getKeyQP(bth.dqpn) == qpc.qpnKeyPart) begin
            isQpKeyCheckPass = True;
        end

        // 1.2 检查QP访问权限
        if (isNeedQueryMrTable) begin
            case ({isSendReq||isWriteReq, isReadReq, isAtomicReq, isReadResp})
                4'b1000: isQpAccCheckPass = qpc.rqAccessFlags含IBV_ACCESS_REMOTE_WRITE;
                4'b0100: isQpAccCheckPass = qpc.rqAccessFlags含IBV_ACCESS_REMOTE_READ;
                4'b0010: isQpAccCheckPass = qpc.rqAccessFlags含IBV_ACCESS_REMOTE_ATOMIC;
                4'b0001: isQpAccCheckPass = qpc.rqAccessFlags含IBV_ACCESS_LOCAL_WRITE;
            endcase
        end

        // 1.3 计算包长度（重要！）
        ChunkAlignLogValue pmtuAlignLogVal = getPmtuSizeByPmtuEnum(qpc.pmtu);
        Length pmtuByteSize = 1 << pmtuAlignLogVal;  // 256/512/1024/2048/4096

        // 对于FIRST类型包，需要计算实际长度
        if (isFirstPacket) begin
            // 起始地址在PMTU块内的偏移
            let startAddrOffsetAlignedToPmtu = reth.va & (pmtuByteSize - 1);
            // 第一个包的长度 = PMTU - 偏移量
            packetLen = pmtuByteSize - truncate(startAddrOffsetAlignedToPmtu);
        end else begin
            // 非FIRST包，长度直接从RETH获取
            packetLen = reth.dlen;
        end

        // 1.4 计算预期的载荷beat数（用于后续验证）
        ADDR rethEndAddr = reth.va + zeroExtend(packetLen) - 1;
        Word startAddrAsDwordOffset = truncate(reth.va >> 2);
        Word endAddrAsDwordOffset = truncate(rethEndAddr >> 2);
        Word zeroBasedDwordCnt = endAddrAsDwordOffset - startAddrAsDwordOffset;
        // 假设每个beat包含8个DWORD，右移3位
        zerobasedExpectedPayloadBeatNum = truncate(zeroBasedDwordCnt >> 3);

        // 2. 如果需要MR表，获取查询结果
        if (isNeedQueryMrTable) begin
            let mrEntryMaybe <- mrTableQueryCltInst.getResp;
            if (mrEntryMaybe matches tagged Valid .mrEntry) begin
                // 2.1 检查MR密钥
                if (rkey2KeyPartMR(reth.rkey) == mrEntry.keyPart) begin
                    isMrKeyCheckPass = True;
                end

                // 2.2 检查MR访问权限
                case ({isSendReq, isReadReq, isWriteReq||isReadResp, isAtomicReq})
                    4'b1000: isMrAccCheckPass = mrEntry含IBV_ACCESS_LOCAL_WRITE;
                    4'b0100: isMrAccCheckPass = mrEntry含IBV_ACCESS_REMOTE_READ;
                    4'b0010: isMrAccCheckPass = mrEntry含IBV_ACCESS_REMOTE_WRITE;
                    4'b0001: isMrAccCheckPass = mrEntry含IBV_ACCESS_REMOTE_ATOMIC;
                endcase

                // 2.3 检查MR地址下界
                isMrLowerAddrBoundOk = reth.va >= mrEntry.baseVA;

                // 2.4 计算地址偏移（为下一级准备）
                // 使用33位来处理溢出（类似环形缓冲区的地址计算）
                TruncatedAddrForMrBoundCheck shortMrStartVa = truncate(mrEntry.baseVA);
                TruncatedAddrForMrBoundCheck shortReqStartVa = truncate(reth.va);
                deltaLen = shortReqStartVa - shortMrStartVa;
            end
        end
    end

    // 3. 更新包状态
    if (isRecvPacketStatusNormal(packetStatus)) begin
        if (!isQpKeyCheckPass)
            packetStatus = RdmaRecvPacketStatusInvalidQpContext;
        else if (!isQpAccCheckPass)
            packetStatus = RdmaRecvPacketStatusInvalidQpAccessFlag;
        else if (!isMrKeyCheckPass)
            packetStatus = RdmaRecvPacketStatusInvalidMrKey;
        else if (!isMrAccCheckPass)
            packetStatus = RdmaRecvPacketStatusInvalidMrAccessFlag;
    end

    checkMrTableStep2PipeQ.enq(pipelineEntryOut);
endrule
```

**PMTU与包长度计算示例**:

```
假设: PMTU = 1024字节, reth.va = 0x10000300, reth.dlen = 4096 (总长度)

First包:
  startAddrOffsetAlignedToPmtu = 0x300 = 768
  packetLen = 1024 - 768 = 256字节

Middle包:
  packetLen = 1024字节 (完整PMTU)

Last包:
  剩余长度 = 4096 - 256 - 1024 - 1024 - 1024 = 768字节
  packetLen = 768字节
```

**权限检查矩阵**:

| 操作 | QP权限要求 | MR权限要求 |
|------|-----------|-----------|
| SEND | REMOTE_WRITE | LOCAL_WRITE |
| WRITE | REMOTE_WRITE | REMOTE_WRITE |
| READ请求 | REMOTE_READ | REMOTE_READ |
| READ响应 | LOCAL_WRITE | LOCAL_WRITE |
| ATOMIC | REMOTE_ATOMIC | REMOTE_ATOMIC |

---

### Stage 3: MR边界检查步骤2 (`checkMrTableStep2`)

**文件位置**: RQ.bsv:526-557

**功能**: 完成MR上界检查的加法运算（时序优化）

**详细流程**:

```bsv
rule checkMrTableStep2;
    let pipelineEntryIn = checkMrTableStep2PipeQ.first;

    // 关键计算：deltaLen = (req.addr - mr.baseVA) + req.len
    deltaLen = pipelineEntryIn.deltaLen + zeroExtend(packetLen);

    // 这个值将在下一级与 mrEntry.len 比较
    checkMrTableStep3PipeQ.enq(pipelineEntryOut);
endrule
```

**为什么需要这一级？**

MR边界检查的完整逻辑是：
```
请求访问范围 = [reth.va, reth.va + packetLen)
MR允许范围 = [mrEntry.baseVA, mrEntry.baseVA + mrEntry.len)
```

传统做法：
```
检查: (reth.va + packetLen) <= (mrEntry.baseVA + mrEntry.len)
```

**问题**：
1. 需要两个64位加法器
2. 可能溢出（需要65位）
3. 时序压力大

**优化做法（分两步）**：
```
Step1 (在checkQpcAndMrTable): deltaAddr = reth.va - mrEntry.baseVA
Step2 (在checkMrTableStep2):  deltaLen = deltaAddr + packetLen
Step3 (在checkMrTableStep3):  检查 deltaLen <= mrEntry.len
```

**优势**：
- 只需要33位运算（Length是32位）
- 分摊到两个周期，降低时序压力
- 利用模运算性质处理溢出

**数学原理**（环形地址空间）：
```
在2^33模空间中：
  deltaLen = (reth.va - mrEntry.baseVA + packetLen) mod 2^33

如果 deltaLen > mrEntry.len，则越界
这个检查等价于原始的范围检查，但避免了溢出问题
```

---

### Stage 4: MR边界检查步骤3与载荷验证 (`checkMrTableStep3`)

**文件位置**: RQ.bsv:559-627

**功能**: 完成MR上界检查，验证载荷长度

**详细流程**:

```bsv
rule checkMrTableStep3;
    let pipelineEntryIn = checkMrTableStep3PipeQ.first;

    // 1. MR上界检查
    Bool isMrUpperAddrBoundOk = deltaLen <= zeroExtend(mrEntry.len);

    // 2. 载荷beat数验证
    Bool isPacketBeatCountCheckPass = False;

    if (rdmaPacketMeta.hasPayload) begin
        // 从包解析器获取实际的载荷尾部元数据
        packetTailMeta = packetParser.rdmaPacketTailMetaPipeOut.first;
        packetParser.rdmaPacketTailMetaPipeOut.deq;

        // 验证：实际beat数是否与预期一致
        if (packetTailMeta.beatCnt - 1 == zerobasedExpectedPayloadBeatNum) begin
            isPacketBeatCountCheckPass = True;
        end
    end else begin
        // 无载荷包，自动通过
        isPacketBeatCountCheckPass = True;
    end

    // 3. 综合检查结果
    if (isRecvPacketStatusNormal(packetStatus)) begin
        if (isNeedQueryMrTable) begin
            isAccessRangeCheckPass = isMrLowerAddrBoundOk && isMrUpperAddrBoundOk;

            if (!isAccessRangeCheckPass) begin
                packetStatus = RdmaRecvPacketStatusMemAccessOutOfBound;
            end
            else if (!isPacketBeatCountCheckPass) begin
                packetStatus = RdmaRecvPacketStatusCorruptPktLength;
            end
        end
    end

    issuePayloadConReqOrDiscardPipeQ.enq(pipelineEntryOut);
endrule
```

**载荷beat数验证的重要性**:

这是检测攻击和错误的最后一道防线：

```
场景1：恶意包声称长度过大
  包头声称: reth.dlen = 4096
  实际发送: 只有256字节载荷
  预期beat数: (4096-1)/32 = 127 beats
  实际beat数: (256-1)/32 = 7 beats
  检测: 不匹配 → 拒绝包

场景2：畸形包长度不对齐
  起始地址: 0x1003 (未对齐到DWORD)
  包长度: 257字节
  预期beat数: 计算错误
  实际beat数: 实际解析的beats
  检测: 不匹配 → 拒绝包

场景3：正常包
  所有计算一致
  检测: 通过 → 继续处理
```

**与L128 FIXME的关系**:

这个阶段部分实现了FIXME中提到的"只信任实际接收的数据"：
- ✓ 验证了实际beat数
- ✗ 但仍然信任包头中的长度字段来计算预期beat数
- ⚠️ 改进方向：应该直接使用实际beat数，而不是用包头长度计算预期值

---

### Stage 5: 发起消费请求或丢弃 (`issuePayloadConReqOrDiscard`)

**文件位置**: RQ.bsv:631-700

**功能**: 根据验证结果决定是否消费载荷，更新错误计数器

**详细流程**:

```bsv
rule issuePayloadConReqOrDiscard;
    let pipelineEntryIn = issuePayloadConReqOrDiscardPipeQ.first;
    let rdmaPacketMeta = pipelineEntryIn.rdmaPacketMeta;
    let packetStatus = pipelineEntryIn.packetStatus;

    if (rdmaPacketMeta.hasPayload) begin
        let isDiscard = !isRecvPacketStatusNormal(packetStatus);

        // 给载荷过滤器发送指令
        filterCmdQ.enq(isDiscard);

        if (!isDiscard) begin
            // 包验证通过，发起DMA写请求
            let payloadConReq = PayloadConReq{
                addr        : reth.va,              // 写入的虚拟地址
                len         : packetLen,            // 写入长度
                baseVA      : mrEntry.baseVA,       // MR基地址
                pgtOffset   : mrEntry.pgtOffset,    // 页表偏移
                fpDebugTime : curFpDebugTime
            };
            conReqPipeOutQ.enq(payloadConReq);
        end
        // 如果isDiscard=True，载荷会被filterDiscardedPayloadStream规则丢弃
    end

    // 更新错误计数器（用于CSR读取和调试）
    case (packetStatus)
        RdmaRecvPacketStatusInvalidQpAccessFlag : metricsInvalidQpAccessCntReg++;
        RdmaRecvPacketStatusInvalidOpcode       : metricsInvalidOpcodeCntReg++;
        RdmaRecvPacketStatusInvalidMrKey        : metricsInvalidMrKeyCntReg++;
        RdmaRecvPacketStatusMemAccessOutOfBound : metricsMemAccessOutOfBoundCntReg++;
        RdmaRecvPacketStatusInvalidMrAccessFlag : metricsInvalidMrAccessFlagCntReg++;
        RdmaRecvPacketStatusInvalidHeader       : metricsInvalidHeaderCntReg++;
        RdmaRecvPacketStatusInvalidQpContext    : metricsInvalidQpContextCntReg++;
        RdmaRecvPacketStatusCorruptPktLength    : metricsCorruptPktLengthCntReg++;
        RdmaRecvPacketStatusUnknown             : metricsUnknownErrorCntReg++;
    endcase

    handleConRespPipeQ.enq(pipelineEntryOut);
endrule
```

**载荷处理路径**:

```
                  [issuePayloadConReqOrDiscard]
                           |
                  包是否有载荷？
                    /        \
                  Yes         No
                   |           |
           验证是否通过？      直接传递
              /      \          ↓
            Pass    Fail      [handleConResp]
             |        |
      发起消费请求  标记丢弃
             |        |
      conReqPipeOutQ  |
             |        |
             +-filterCmdQ (False/True)
                      |
                      ↓
             [filterDiscardedPayloadStream]
                      |
              isDiscard判断
                /          \
            False          True
              |              |
         转发载荷        丢弃载荷
              ↓              ↓
    filteredDataStreamForConsumeQ  (无操作)
              ↓
      payloadConStreamPipeOut
              ↓
         [DMA写模块]
```

**重要设计点**:

1. **载荷与元数据分离**:
   - 载荷数据在`payloadStorage`中等待
   - 元数据在流水线中流动
   - 通过`filterCmdQ`同步两者

2. **丢弃机制**:
   - 不是立即丢弃，而是标记后由专门的过滤规则处理
   - 保证流水线不会死锁

3. **计数器的作用**:
   - 实时监控各类错误
   - 通过CSR接口暴露给驱动
   - 用于性能调优和问题诊断

---

### Stage 6: 处理消费响应 (`handleConResp`)

**文件位置**: RQ.bsv:702-755

**功能**: 接收DMA写完成响应，生成ACK和CNP请求

**详细流程**:

```bsv
rule handleConResp;
    let pipelineEntryIn = handleConRespPipeQ.first;
    handleConRespPipeQ.deq;

    let rdmaPacketMeta = pipelineEntryIn.rdmaPacketMeta;
    let packetStatus = pipelineEntryIn.packetStatus;
    let isDiscard = !isRecvPacketStatusNormal(packetStatus);
    let bth = rdmaPacketMeta.header.bth;

    // 1. 如果有载荷且没有丢弃，等待消费响应
    if (rdmaPacketMeta.hasPayload) begin
        if (!isDiscard) begin
            let resp = conRespPipeInQ.first;
            conRespPipeInQ.deq;
            // resp为True表示DMA写成功
        end
    end

    // 2. 对于正常包，生成后续处理
    if (!isDiscard) begin
        // 2.1 传递给元数据报告生成阶段
        handleGenMetaReportQueueDescPipeQ.enq(GenMetaReportQueueDescPipelineEntry{
            rdmaPacketMeta: rdmaPacketMeta,
            fpDebugTime: curFpDebugTime
        });

        // 2.2 对于有载荷的包，生成自动ACK请求
        let needUpdatePsnBitmap = rdmaPacketMeta.hasPayload;
        if (needUpdatePsnBitmap) begin
            autoAckGenReqPipeOutQueue.enq(AutoAckGeneratorReq{
                psn: bth.psn,          // 包序列号
                qpn: bth.dqpn,         // QP号
                qpc: pipelineEntryIn.qpc
            });
        end

        // 2.3 如果包被ECN标记，生成CNP包请求（拥塞通知）
        if (rdmaPacketMeta.isEcnMarked && bth.trans != TRANS_TYPE_CNP) begin
            genCnpReqPipeOutQueue.enq(CnpPacketGenReq{
                peerAddrInfo: pipelineEntryIn.peerMacIpUdpMeta,
                peerQpn: pipelineEntryIn.qpc.peerQPN,
                peerMsn: bth.msn,
                localUdpPort: pipelineEntryIn.qpc.localUdpPort
            });
        end
    end
endrule
```

**关键机制**:

1. **ACK生成策略**:
   ```
   只有载荷包才更新PSN位图和生成ACK
   原因：
   - 无载荷包（如ACK、READ请求）不需要确认
   - PSN位图用于跟踪哪些数据包已接收
   - 避免ACK风暴
   ```

2. **ECN与CNP**:
   ```
   ECN (Explicit Congestion Notification) - 显式拥塞通知
   - 网络交换机在拥塞时标记IP包的ECN位
   - RQ检测到ECN标记后生成CNP包

   CNP (Congestion Notification Packet) - 拥塞通知包
   - 发送回源端，通知其降低发送速率
   - 实现DCQCN拥塞控制算法
   - 防止CNP包循环：检查bth.trans != TRANS_TYPE_CNP
   ```

3. **同步机制**:
   ```
   载荷消费响应 ←→ 元数据流水线

   通过conRespPipeInQ同步：
   - 发送消费请求时，元数据进入handleConRespPipeQ
   - DMA写完成时，响应进入conRespPipeInQ
   - 规则同时deq两个队列，保证顺序一致
   ```

---

### Stage 7: 生成元数据报告描述符 (`genMetaReportQueueDesc`)

**文件位置**: RQ.bsv:758-939

**功能**: 将接收的RDMA包信息编码为驱动可读的描述符格式

**详细流程**:

```bsv
rule genMetaReportQueueDesc;
    let pipelineEntryIn = handleGenMetaReportQueueDescPipeQ.first;
    let rdmaPacketMeta = pipelineEntryIn.rdmaPacketMeta;
    let bth = rdmaPacketMeta.header.bth;
    let opcode = {pack(bth.trans), pack(bth.opcode)};

    // 提取扩展头部
    let reth = extractPriRETH(...);   // RDMA Extended Transport Header
    let rreth = extractRRETH(...);    // READ的本地地址信息
    let aeth = extractAETH(...);      // ACK Extended Transport Header
    let immDT = extractImmDt(...);    // Immediate Data

    // 根据操作码生成不同类型的描述符
    Maybe#(RingbufRawDescriptor) rawDescToEnqueueMaybe0 = tagged Invalid;
    Maybe#(RingbufRawDescriptor) rawDescToEnqueueMaybe1 = tagged Invalid;

    case (opcode)
        // 数据包类型（SEND/WRITE/READ）
        RC_SEND_FIRST, RC_SEND_LAST, RC_SEND_ONLY,
        RC_RDMA_WRITE_FIRST, RC_RDMA_WRITE_LAST, RC_RDMA_WRITE_ONLY,
        RC_RDMA_READ_REQUEST, RC_RDMA_READ_RESPONSE_FIRST, ...:
        begin
            // 生成基本信息描述符
            let desc0 = MetaReportQueuePacketBasicInfoDesc{
                immData: immDT,           // 立即数
                rkey: reth.rkey,          // 远端密钥
                raddr: reth.va,           // 远端地址
                totalLen: reth.dlen,      // 总长度
                dqpn: bth.dqpn,           // 目标QP号
                ackReq: bth.ackReq,       // 是否需要ACK
                solicited: bth.solicited, // 是否solicited
                isRetry: bth.isRetry,     // 是否重传
                psn: bth.psn,             // 包序列号
                msn: bth.msn,             // 消息序列号
                commonHeader: commonHeader
            };

            // READ请求需要扩展描述符
            if (opcode == RC_RDMA_READ_REQUEST) begin
                desc0.commonHeader.hasNextFrag = True;
                let desc1 = MetaReportQueueReadReqExtendInfoDesc{
                    lkey: rreth.lkey,     // 本地密钥
                    laddr: rreth.va,      // 本地地址
                    totalLen: reth.dlen,  // 读取长度
                    commonHeader: commonHeader
                };
                rawDescToEnqueueMaybe1 = tagged Valid unpack(pack(desc1));
            end
            rawDescToEnqueueMaybe0 = tagged Valid unpack(pack(desc0));
        end

        // 中间包（MIDDLE）
        RC_SEND_MIDDLE, RC_RDMA_WRITE_MIDDLE, RC_RDMA_READ_RESPONSE_MIDDLE:
        begin
            // 只有重传的中间包才生成描述符
            if (bth.isRetry) begin
                rawDescToEnqueueMaybe0 = tagged Valid ...;
            end else begin
                noNeedToGenDesc = True;
            end
        end

        // ACK包
        RC_ACKNOWLEDGE:
        begin
            let desc0 = MetaReportQueueAckDesc{
                nowBitmap: aeth.newBitmap,          // 当前PSN位图
                msn: bth.msn,
                qpn: bth.dqpn,
                psnNow: bth.psn,
                psnBeforeSlide: aeth.preBitmapPsn,  // 滑窗前的PSN
                isPacketLost: aeth.isPacketLost,    // 是否检测到丢包
                isWindowSlided: aeth.isWindowSlided, // 是否滑窗
                isSendByDriver: aeth.isSendByDriver,
                commonHeader: commonHeader
            };

            // 如果滑窗，需要附加前一个位图
            if (aeth.isWindowSlided) begin
                desc0.commonHeader.hasNextFrag = True;
                let desc1 = MetaReportQueueAckExtraDesc{
                    preBitmap: aeth.preBitmap,
                    commonHeader: commonHeader
                };
                rawDescToEnqueueMaybe1 = tagged Valid unpack(pack(desc1));
            end
            rawDescToEnqueueMaybe0 = tagged Valid unpack(pack(desc0));
        end

        default: begin
            decodeSuccess = False;
        end
    endcase

    // 入队到MIMO队列（支持一次入队2个描述符）
    if (decodeSuccess) begin
        if (isValid(vecToEnqMaybe[0]) && isValid(vecToEnqMaybe[1])) begin
            if (metaReportMimoQueue.enqReadyN(2)) begin
                metaReportMimoQueue.enq(2, vecToEnq);
                handleGenMetaReportQueueDescPipeQ.deq;
            end
        end
        else if (isValid(vecToEnqMaybe[0])) begin
            if (metaReportMimoQueue.enqReadyN(1)) begin
                metaReportMimoQueue.enq(1, vecToEnq);
                handleGenMetaReportQueueDescPipeQ.deq;
            end
        end
        else if (noNeedToGenDesc) begin
            handleGenMetaReportQueueDescPipeQ.deq;
        end
    end
endrule
```

**描述符类型**:

| 描述符类型 | 用途 | 何时生成 |
|-----------|------|---------|
| `MetaReportQueuePacketBasicInfoDesc` | 基本包信息 | 所有数据包 |
| `MetaReportQueueReadReqExtendInfoDesc` | READ请求扩展信息 | READ请求的第2个描述符 |
| `MetaReportQueueAckDesc` | ACK信息 | ACK包 |
| `MetaReportQueueAckExtraDesc` | ACK滑窗信息 | ACK且滑窗时的第2个描述符 |

**MIDDLE包的特殊处理**:

```
为什么MIDDLE包通常不生成描述符？

在多包消息中（FIRST → MIDDLE → LAST）：
- FIRST包：包含完整的RETH信息（地址、长度、密钥）
- MIDDLE包：只有BTH，没有RETH
- LAST包：标记消息结束

驱动只需要知道：
✓ FIRST包 - 开始新消息
✓ LAST包 - 结束消息
✗ MIDDLE包 - 只是中间数据，不需要通知

例外：重传的MIDDLE包需要通知驱动
```

**MIMO队列的使用**:

```bsv
MIMO (Multiple Input Multiple Output) 队列配置：
- 入队: 最多2个描述符/周期
- 出队: 1个描述符/周期
- 深度: 4个描述符

为什么需要MIMO？
1. READ请求需要2个描述符（基本+扩展）
2. ACK滑窗需要2个描述符（基本+位图）
3. 一次性入队保证原子性
4. 避免描述符被分散
```

---

### Stage 8: 转发元数据描述符 (`forwardMetaReportDescToOutput`)

**文件位置**: RQ.bsv:941-953

**功能**: 从MIMO队列取出描述符，转发到输出队列

**详细流程**:

```bsv
rule forwardMetaReportDescToOutput;
    if (metaReportMimoQueue.deqReadyN(1)) begin
        metaReportMimoQueue.deq(1);
        let desc = metaReportMimoQueue.first[0];
        metaReportDescPipeOutQueue.enq(desc);
    end
endrule
```

**设计原因**:

```
为什么需要这个额外的转发阶段？

MIMO队列 → 输出队列的解耦：
1. MIMO支持可变宽度操作（1或2个描述符）
2. 输出队列是固定宽度的FIFOF
3. 转发规则实现宽度适配
4. 保证下游模块接口简单
```

**最终输出**:

```
metaReportDescPipeOut → [驱动软件]

驱动通过以下方式读取描述符：
1. 通过DMA环形缓冲区
2. 硬件写入，软件读取
3. 描述符包含：
   - 接收到的包信息
   - 需要软件处理的事件
   - 完成队列条目（CQE）的原始数据
```

---

### Stage 9: 过滤丢弃的载荷 (`filterDiscardedPayloadStream`)

**文件位置**: RQ.bsv:955-975

**功能**: 根据验证结果过滤载荷数据流

**详细流程**:

```bsv
rule filterDiscardedPayloadStream;
    let isDiscard = filterCmdQ.first;        // 从stage 5得到的丢弃指令
    let ds = payloadStorage.first;           // 从包解析器缓冲的载荷
    payloadStorage.deq;

    if (!isDiscard) begin
        // 验证通过，转发载荷
        filteredDataStreamForConsumeQ.enq(ds);
    end
    // 如果isDiscard=True，直接丢弃（deq但不enq）

    // 当遇到载荷的最后一个beat时，消费掉过滤指令
    if (ds.isLast) begin
        filterCmdQ.deq;
    end
endrule
```

**同步机制**:

```
载荷流与指令流的同步：

时间轴视图：
T0:  包1头部进入流水线 → stage5发送 filterCmd[0]=False
T1:  包1载荷beat1到达payloadStorage
T2:  包1载荷beat2到达payloadStorage
     包2头部进入流水线 → stage5发送 filterCmd[1]=True
T3:  包1载荷beat3(last)到达payloadStorage
T4:  包2载荷beat1到达payloadStorage

过滤器处理：
T1: deq payloadStorage[beat1], filterCmd[0]=False → 转发beat1
T2: deq payloadStorage[beat2], filterCmd[0]=False → 转发beat2
T3: deq payloadStorage[beat3], filterCmd[0]=False → 转发beat3
    beat3.isLast=True → deq filterCmd[0]
T4: deq payloadStorage[beat1], filterCmd[1]=True → 丢弃beat1
    ...继续丢弃直到isLast

关键：
- 一个filterCmd对应一个完整包的所有beats
- 只有在遇到isLast时才deq filterCmd
- 保证了多包并行时的正确同步
```

**为什么不立即丢弃？**

```
方案A（当前设计）：缓冲后过滤
优点：
✓ 流水线并行，不阻塞解析
✓ 错误检测在后续阶段，解析时未知
✓ 简化包解析器设计

方案B（立即丢弃）：
缺点：
✗ 需要包解析器等待验证结果
✗ 降低吞吐量
✗ 增加解析器复杂度

结论：缓冲是必要的
```

---

## 安全验证机制

RQ模块实现了多层安全验证，防止恶意包攻击：

### 验证层次结构

```
Layer 1: 范围检查
  ├─ QPN索引溢出检查 (L314-316)
  └─ MR索引溢出检查 (L308-310)
      ↓
Layer 2: 密钥验证
  ├─ QP密钥检查 (L373-375)
  └─ MR密钥检查 (L429-431)
      ↓
Layer 3: 权限验证
  ├─ QP访问权限检查 (L378-403)
  └─ MR访问权限检查 (L433-449)
      ↓
Layer 4: 地址范围验证
  ├─ MR下界检查 (L451)
  └─ MR上界检查 (L573)
      ↓
Layer 5: 数据完整性验证
  └─ 载荷长度检查 (L584-592)
```

### 详细验证规则

#### 1. 索引溢出检查

```bsv
// QPN索引检查
if (getQpnRawIndexPart(bth.dqpn) >= MAX_QP) begin
    packetStatus = RdmaRecvPacketStatusQpIdxOverflow;
end

// MR索引检查
if (getMrRawIndexPartFromRkey(reth.rkey) >= MAX_MR) begin
    packetStatus = RdmaRecvPacketStatusMrIdxOverflow;
end
```

**目的**: 防止数组越界访问，避免读取未初始化的内存

**攻击场景**:
```
攻击者发送: dqpn = 0xFFFFFF (远超MAX_QP=4096)
不检查时: qpcTable[0xFFFFFF] → 访问非法内存
有检查时: 立即标记为错误，拒绝包
```

#### 2. 密钥验证

```bsv
// QP密钥验证
if (getKeyQP(bth.dqpn) == qpc.qpnKeyPart) begin
    isQpKeyCheckPass = True;
end

// MR密钥验证
if (rkey2KeyPartMR(reth.rkey) == mrEntry.keyPart) begin
    isMrKeyCheckPass = True;
end
```

**目的**: 防止伪造QP号和MR密钥

**密钥结构**:
```
QPN (24位):
  ├─ Index部分 (12位): qpcTable的索引
  └─ Key部分 (12位): 随机密钥

RKey (32位):
  ├─ Index部分 (20位): mrTable的索引
  └─ Key部分 (12位): 随机密钥

验证过程：
1. 从QPN/RKey提取索引部分
2. 使用索引查表获取条目
3. 比较条目中的keyPart与QPN/RKey中的密钥部分
4. 完全匹配才通过
```

**攻击防御**:
```
攻击场景: 攻击者猜测QPN
  合法QPN: 0x123ABC (index=0x123, key=0xABC)
  攻击QPN: 0x123000 (index=0x123, key=0x000)

  查表: qpcTable[0x123] → key=0xABC
  比较: 0x000 != 0xABC → 拒绝

成功率: 1/4096 (12位密钥空间)
```

#### 3. 访问权限验证

**QP权限检查**:

```bsv
case ({pack(isSendReq||isWriteReq), pack(isReadReq), pack(isAtomicReq), pack(isReadResp)})
    4'b1000: isQpAccCheckPass = containAccessTypeFlag(qpc.rqAccessFlags, IBV_ACCESS_REMOTE_WRITE);
    4'b0100: isQpAccCheckPass = containAccessTypeFlag(qpc.rqAccessFlags, IBV_ACCESS_REMOTE_READ);
    4'b0010: isQpAccCheckPass = containAccessTypeFlag(qpc.rqAccessFlags, IBV_ACCESS_REMOTE_ATOMIC);
    4'b0001: isQpAccCheckPass = containAccessTypeFlag(qpc.rqAccessFlags, IBV_ACCESS_LOCAL_WRITE);
endcase
```

**MR权限检查**:

```bsv
case ({pack(isSendReq), pack(isReadReq), pack(isWriteReq||isReadResp), pack(isAtomicReq)})
    4'b1000: isMrAccCheckPass = containAccessTypeFlag(mrEntry.accFlags, IBV_ACCESS_LOCAL_WRITE);
    4'b0100: isMrAccCheckPass = containAccessTypeFlag(mrEntry.accFlags, IBV_ACCESS_REMOTE_READ);
    4'b0010: isMrAccCheckPass = containAccessTypeFlag(mrEntry.accFlags, IBV_ACCESS_REMOTE_WRITE);
    4'b0001: isMrAccCheckPass = containAccessTypeFlag(mrEntry.accFlags, IBV_ACCESS_REMOTE_ATOMIC);
endcase
```

**权限矩阵**:

| 操作 | QP权限 | MR权限 | 说明 |
|------|--------|--------|------|
| SEND | REMOTE_WRITE | LOCAL_WRITE | 远端写，本地接收 |
| WRITE | REMOTE_WRITE | REMOTE_WRITE | 远端直接写本地内存 |
| READ请求 | REMOTE_READ | REMOTE_READ | 远端读本地内存 |
| READ响应 | LOCAL_WRITE | LOCAL_WRITE | 本地接收读取的数据 |
| ATOMIC | REMOTE_ATOMIC | REMOTE_ATOMIC | 远端原子操作 |

**攻击防御**:
```
场景: 攻击者尝试未授权的WRITE操作
  QP配置: rqAccessFlags = IBV_ACCESS_REMOTE_READ (只允许READ)
  MR配置: accFlags = IBV_ACCESS_REMOTE_WRITE (允许WRITE)
  攻击包: opcode = RDMA_WRITE

  QP检查: REMOTE_READ != REMOTE_WRITE → 失败
  结果: 包被拒绝，即使MR允许WRITE

保护: 需要QP和MR都授权才能通过
```

#### 4. 地址范围验证

**完整的边界检查**:

```bsv
// 下界检查
isMrLowerAddrBoundOk = reth.va >= mrEntry.baseVA;

// 上界检查（分两步）
// Step 1: 计算偏移
deltaLen = truncate(reth.va) - truncate(mrEntry.baseVA);
// Step 2: 加上长度
deltaLen = deltaLen + zeroExtend(packetLen);
// Step 3: 比较
isMrUpperAddrBoundOk = deltaLen <= zeroExtend(mrEntry.len);

// 综合结果
isAccessRangeCheckPass = isMrLowerAddrBoundOk && isMrUpperAddrBoundOk;
```

**数学证明**:

```
定义：
  MR范围: [baseVA, baseVA + len)
  请求范围: [reth.va, reth.va + packetLen)

要求：
  reth.va >= baseVA                    (下界)
  reth.va + packetLen <= baseVA + len  (上界)

变换：
  上界 ⟺ reth.va + packetLen - baseVA <= len
       ⟺ (reth.va - baseVA) + packetLen <= len

令 deltaAddr = reth.va - baseVA:
  上界 ⟺ deltaAddr + packetLen <= len

实现（使用33位运算避免溢出）:
  deltaLen = truncate33(reth.va - baseVA + packetLen)
  检查: deltaLen <= len
```

**攻击场景与防御**:

```
场景1: 下界攻击
  MR: baseVA=0x10000000, len=4096
  攻击: reth.va=0x0FFFFFFF (在MR之前)
  检查: 0x0FFFFFFF < 0x10000000 → 拒绝

场景2: 上界攻击
  MR: baseVA=0x10000000, len=4096
  攻击: reth.va=0x10000F00, packetLen=256
  计算: deltaLen = 0xF00 + 256 = 0x1000 = 4096
  检查: 4096 <= 4096 → 通过（边界情况正确）

  攻击: reth.va=0x10000F00, packetLen=257
  计算: deltaLen = 0xF00 + 257 = 0x1001 = 4097
  检查: 4097 > 4096 → 拒绝（越界）

场景3: 整数溢出攻击
  MR: baseVA=0x10000000, len=4096
  攻击: reth.va=0xFFFFFFFF, packetLen=0xFFFFFFFF
  传统方法: (0xFFFFFFFF + 0xFFFFFFFF) 溢出为 0xFFFFFFFE
            可能绕过检查

  本实现: 使用33位运算
  deltaLen = truncate33(0xFFFFFFFF - 0x10000000 + 0xFFFFFFFF)
           = truncate33(0x1EFFFFFFE)
           = 0x0EFFFFFFE (保留溢出位)
  检查: 0x0EFFFFFFE > 4096 → 正确拒绝
```

#### 5. 载荷完整性验证

```bsv
if (rdmaPacketMeta.hasPayload) begin
    packetTailMeta = packetParser.rdmaPacketTailMetaPipeOut.first;

    // 预期beat数（基于包头声称的长度）
    Word zeroBasedBeatCnt = (endAddrAsDwordOffset - startAddrAsDwordOffset) >> 3;
    zerobasedExpectedPayloadBeatNum = truncate(zeroBasedBeatCnt);

    // 实际beat数（基于实际接收的数据）
    actualBeatCnt = packetTailMeta.beatCnt;

    // 验证
    if (actualBeatCnt - 1 == zerobasedExpectedPayloadBeatNum) begin
        isPacketBeatCountCheckPass = True;
    end
end
```

**检测的攻击**:

```
攻击1: 声称长度过大
  包头: reth.dlen = 4096
  实际: 只发送256字节
  预期beats: (4096-1)/32 = 127
  实际beats: (256-1)/32 = 7
  检测: 7 != 127 → 拒绝

攻击2: 声称长度过小
  包头: reth.dlen = 256
  实际: 发送4096字节
  预期beats: 7
  实际beats: 127
  检测: 127 != 7 → 拒绝

保护: 防止缓冲区溢出和数据截断
```

---

## 错误处理

### 错误类型枚举

```bsv
typedef enum {
    RdmaRecvPacketStatusNormal,              // 正常
    RdmaRecvPacketStatusQpIdxOverflow,       // QP索引溢出
    RdmaRecvPacketStatusMrIdxOverflow,       // MR索引溢出
    RdmaRecvPacketStatusInvalidQpContext,    // QP上下文无效（密钥错误）
    RdmaRecvPacketStatusInvalidQpAccessFlag, // QP访问权限不足
    RdmaRecvPacketStatusInvalidMrKey,        // MR密钥错误
    RdmaRecvPacketStatusInvalidMrAccessFlag, // MR访问权限不足
    RdmaRecvPacketStatusMemAccessOutOfBound, // 内存访问越界
    RdmaRecvPacketStatusCorruptPktLength,    // 包长度错误
    RdmaRecvPacketStatusInvalidHeader,       // 包头格式错误
    RdmaRecvPacketStatusInvalidOpcode,       // 非法操作码
    RdmaRecvPacketStatusUnknown              // 未知错误
} RdmaRecvPacketStatus;
```

### 错误计数器

每种错误都有对应的计数器，通过CSR接口暴露：

```bsv
Reg#(Dword) metricsInvalidQpAccessCntReg
Reg#(Dword) metricsInvalidOpcodeCntReg
Reg#(Dword) metricsInvalidMrKeyCntReg
Reg#(Dword) metricsMemAccessOutOfBoundCntReg
Reg#(Dword) metricsInvalidMrAccessFlagCntReg
Reg#(Dword) metricsInvalidHeaderCntReg
Reg#(Dword) metricsInvalidQpContextCntReg
Reg#(Dword) metricsCorruptPktLengthCntReg
Reg#(Dword) metricsUnknownErrorCntReg
```

### CSR地址映射

```bsv
CSR_ADDR_OFFSET_METRICS_RQ_PACKET_VERIFY_INV_QP_ACCESS_FLAG_CNT
CSR_ADDR_OFFSET_METRICS_RQ_PACKET_VERIFY_INV_OPCODE_CNT
CSR_ADDR_OFFSET_METRICS_RQ_PACKET_VERIFY_INV_MR_KEY_CNT
CSR_ADDR_OFFSET_METRICS_RQ_PACKET_VERIFY_MEM_OOB_CNT
CSR_ADDR_OFFSET_METRICS_RQ_PACKET_VERIFY_INV_MR_ACCESS_FLAG_CNT
CSR_ADDR_OFFSET_METRICS_RQ_PACKET_VERIFY_INV_HEADER_CNT
CSR_ADDR_OFFSET_METRICS_RQ_PACKET_VERIFY_INV_QP_CTX_CNT
CSR_ADDR_OFFSET_METRICS_RQ_PACKET_VERIFY_PKT_LEN_ERR_CNT
CSR_ADDR_OFFSET_METRICS_RQ_PACKET_VERIFY_UNKNOWN_ERR_CNT
```

**用途**:
- 驱动可以通过MMIO读取这些计数器
- 用于诊断网络问题和安全攻击
- 性能调优和监控

### 错误处理流程

```
[检测错误] → [更新packetStatus] → [继续流水线] → [issuePayloadConReqOrDiscard]
                                                              ↓
                                                    [更新错误计数器]
                                                              ↓
                                                    [丢弃载荷]
                                                              ↓
                                                    [不生成元数据报告]
                                                              ↓
                                                    [静默丢弃包]
```

**设计哲学**:
- **Fail-safe**: 错误包被静默丢弃，不影响正常流量
- **可观察性**: 错误被计数，便于诊断
- **防御深度**: 多层验证，即使一层失败也有后续保护
- **性能优先**: 错误处理不阻塞流水线

---

## 工作流程图

### 完整数据流图

```
                    ┌─────────────────────────────────────────┐
                    │    以太网帧输入 (ethernetFramePipeIn)    │
                    └──────────────────┬──────────────────────┘
                                       ↓
                    ┌──────────────────────────────────────────┐
                    │          PacketParse (包解析器)           │
                    │  - 解析以太网/IP/UDP/BTH/扩展头部           │
                    │  - 分离包头和载荷                         │
                    └─────┬────────────┬──────────┬─────────────┘
                          │            │          │
                          │            │          └──→ otherRawPacketPipeOut
                          │            │                (非RDMA包)
                          │            │
           ┌──────────────┘            └───────────┐
           │                                       │
           │ rdmaPacketMetaPipeOut                 │ rdmaPayloadPipeOut
           │ rdmaMacIpUdpMetaPipeOut               ↓
           ↓                                  payloadStorage
    ┌─────────────────────────────────┐      (FIFOF缓冲)
    │ Stage 1: sendQpcQueryReq        │
    │ - 发起QPC查询                    │
    │ - 发起MR表查询（如需要）          │
    │ - 索引溢出检查                   │
    └──────────┬──────────────────────┘
               │ checkQpcAndMrTablePipeQ
               ↓
    ┌─────────────────────────────────┐
    │ Stage 2: checkQpcAndMrTable     │
    │ - QP密钥检查                     │
    │ - QP权限检查                     │
    │ - MR密钥检查                     │
    │ - MR权限检查                     │
    │ - 计算包长度                     │
    │ - MR下界检查                     │
    │ - 预期beat数计算                 │
    └──────────┬──────────────────────┘
               │ checkMrTableStep2PipeQ
               ↓
    ┌─────────────────────────────────┐
    │ Stage 3: checkMrTableStep2      │
    │ - MR上界检查（加法）              │
    └──────────┬──────────────────────┘
               │ checkMrTableStep3PipeQ
               ↓
    ┌─────────────────────────────────┐
    │ Stage 4: checkMrTableStep3      │
    │ - MR上界检查（比较）              │
    │ - 载荷beat数验证                 │
    └──────────┬──────────────────────┘
               │ issuePayloadConReqOrDiscardPipeQ
               ↓
    ┌─────────────────────────────────┐          ┌─────────────────┐
    │ Stage 5: issuePayloadConReq     │          │ filterCmdQ      │
    │ - 发起消费请求（如通过）          │ ─────→  │ (True/False)    │
    │ - 标记丢弃（如失败）              │          └────────┬────────┘
    │ - 更新错误计数器                 │                   │
    └──────────┬──────────────────────┘                   │
               │ handleConRespPipeQ                       │
               │                                          │
               │ ┌─────────────────────────────────────┐  │
               │ │ Stage 9: filterDiscardedPayload     │  │
               │ │ - 根据filterCmd过滤载荷              │←─┘
               │ │ - 转发或丢弃                        │
               │ └─────────────┬───────────────────────┘
               │               │ filteredDataStreamForConsumeQ
               │               ↓
               │         payloadConStreamPipeOut
               │         (给DMA写模块)
               ↓
    ┌─────────────────────────────────┐
    │ Stage 6: handleConResp          │
    │ - 接收消费响应                   │
    │ - 生成ACK请求                    │  ─→ autoAckGenReqPipeOut
    │ - 生成CNP请求（如ECN标记）        │  ─→ genCnpReqPipeOut
    └──────────┬──────────────────────┘
               │ handleGenMetaReportQueueDescPipeQ
               ↓
    ┌─────────────────────────────────┐
    │ Stage 7: genMetaReportQueueDesc │
    │ - 编码元数据描述符                │
    │ - 处理扩展描述符                  │
    └──────────┬──────────────────────┘
               │ metaReportMimoQueue (MIMO)
               ↓
    ┌─────────────────────────────────┐
    │ Stage 8: forwardMetaReportDesc  │
    │ - MIMO队列到输出队列              │
    └──────────┬──────────────────────┘
               │
               ↓
        metaReportDescPipeOut
        (给驱动软件)
```

### 时序图示例

**正常包处理时序**:

```
时钟周期:  T0    T1    T2    T3    T4    T5    T6    T7    T8    T9    T10   T11   T12

包A:       Parse S1    S2    S3    S4    S5    S6    S7    S8    Done
包B:             Parse S1    S2    S3    S4    S5    S6    S7    S8    Done
包C:                   Parse S1    S2    S3    S4    S5    S6    S7    S8    Done

QPC查询:         发起←─────────────(延迟11周期)─────────────→响应
MR查询:          发起←─────────────(延迟11周期)─────────────→响应

载荷:      Buf0  Buf1  Buf2  Buf3  Filter Filter Filter Out   Out   Out
```

**流水线并行度**:
- 理论最大吞吐量: 1包/周期
- 实际吞吐量受限于:
  - QPC/MR表查询延迟
  - 载荷缓冲区大小
  - 输出队列容量

---

## 关键设计特点总结

### 1. 流水线设计
- **9级深度流水线**: 平衡吞吐量和时序
- **异步查询**: QPC和MR查询不阻塞流水线
- **解耦缓冲**: 载荷与元数据分离处理

### 2. 安全机制
- **多层验证**: 索引→密钥→权限→地址→数据
- **防溢出设计**: 33位算术处理边界检查
- **静默丢弃**: 错误包不影响正常流量

### 3. 性能优化
- **流水线并行**: 支持多包同时处理
- **MIMO队列**: 批量处理扩展描述符
- **预先过滤**: 早期检测索引溢出

### 4. 可观察性
- **丰富的计数器**: 9种错误类型分类统计
- **CSR接口**: 驱动可实时监控
- **调试计数器**: 8个额外的debug计数器

### 5. 功能完整性
- **ACK生成**: 支持自动ACK
- **拥塞控制**: ECN检测和CNP生成
- **元数据报告**: 完整的包信息给驱动
- **多操作码支持**: SEND/WRITE/READ/ATOMIC/ACK

---

## 已知问题与改进方向

### L128-130 FIXME

**问题1**: 仍然信任包头长度字段
```
当前: 使用reth.dlen计算预期beat数，然后验证
改进: 直接使用实际beat数，忽略包头声称的长度
```

**问题2**: 异常包可能导致死锁
```
当前: 没有统一的流水线清理机制
改进: 实现错误包的流水线flush机制
      使用唯一ID跟踪包在各队列中的位置
```

### 其他潜在改进

1. **载荷长度验证增强**:
   ```
   当前验证: 预期beats == 实际beats
   可能遗漏: 包头声称的长度与实际beat数的byte-level一致性
   改进: 增加byte级别的长度验证
   ```

2. **错误包统计**:
   ```
   当前: 全局计数器
   改进: 每个QP的错误计数
         便于诊断哪个连接有问题
   ```

3. **流水线深度平衡**:
   ```
   当前: 各阶段队列深度不一
   改进: 根据实际延迟动态调整队列大小
         减少资源浪费
   ```

---

生成时间: 2026-01-06
文件位置: open-rdma-rtl/src/RQ.bsv
总行数: 996行
流水线阶段: 9级
安全检查层: 5层
支持操作码: 15种
