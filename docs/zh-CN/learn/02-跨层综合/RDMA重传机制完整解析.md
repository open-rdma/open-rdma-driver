# RDMA 重传机制完整解析

> **文档类型**: 跨层综合分析
> **创建时间**: 2025-01-12
> **涉及模块**: 硬件 (AutoAckGenerator, PsnContinousChecker, RQ) + 软件 (retransmit, meta_report)
> **InfiniBand规范**: Volume 1, Section 9.4.4 (Reliable Connection)

---

## 📋 目录

- [1. 概述](#1-概述)
- [2. 硬件端重传机制](#2-硬件端重传机制)
  - [2.1 重传包标识](#21-重传包标识)
  - [2.2 PSN位图管理](#22-psn位图管理)
  - [2.3 丢包检测与ACK/NAK生成](#23-丢包检测与acknak生成)
  - [2.4 超时机制](#24-超时机制)
- [3. 软件端重传策略](#3-软件端重传策略)
  - [3.1 重传标志设置](#31-重传标志设置)
  - [3.2 包生成](#32-包生成)
  - [3.3 重传任务处理](#33-重传任务处理)
- [4. 边界条件处理](#4-边界条件处理)
  - [4.1 超出128位位图边界](#41-超出128位位图边界)
  - [4.2 窗口滑动后的重传包](#42-窗口滑动后的重传包)
  - [4.3 重复包处理](#43-重复包处理)
- [5. 数据写入与幂等性](#5-数据写入与幂等性)
- [6. 完整时序图](#6-完整时序图)
- [7. 关键代码索引](#7-关键代码索引)
- [8. 设计权衡与优化](#8-设计权衡与优化)

---

## 1. 概述

本文档详细解析 Open-RDMA 项目中的**包重传机制**，这是实现 InfiniBand RC (Reliable Connection) 语义的核心功能。重传机制横跨硬件和软件两层：

- **硬件层**：自动PSN追踪、位图管理、ACK/NAK生成
- **软件层**：重传策略、超时管理、包队列维护

### 1.1 核心设计目标

1. **可靠传输**: 保证所有包按序无丢失交付
2. **硬件加速**: ACK/NAK由硬件自动生成，减少CPU开销
3. **高吞吐量**: 位图机制支持选择性重传（SACK）
4. **边界处理**: 处理超出窗口范围的重传包

### 1.2 关键数据结构

| 参数 | 值 | 说明 |
|------|----|----|
| **PSN宽度** | 24位 | 包序列号范围：0 ~ 16,777,215 |
| **位图宽度** | 128位 | 一次可追踪128个连续PSN |
| **窗口步长** | 16位/步 | 位图覆盖8个步长 (128/16) |
| **边界阈值** | 8步长 | 超出则丢弃旧包 |
| **超时阈值** | 5000时钟周期 | 自动轮询超时 |

---

## 2. 硬件端重传机制

### 2.1 重传包标识

**文件**: `open-rdma-rtl/src/RdmaHeaders.bsv:158`

```bsv
typedef struct {
    TransType trans;
    RdmaOpCode opcode;
    Bool solicited;
    Bool isRetry;           // ⭐ 1位重传标志
    PAD padCnt;
    ReservedZero#(4) tver;
    MSN msn;
    ReservedZero#(1) fecn;
    ReservedZero#(1) becn;
    ReservedZero#(6) resv6;
    QPN dqpn;
    Bool ackReq;
    ReservedZero#(7) resv7;
    PSN psn;                // ⭐ 24位包序列号
} BTH deriving(Bits, Eq, Bounded, FShow);
```

**关键点**:
- `isRetry`: 标识包是否为重传（由软件设置）
- `psn`: 唯一标识每个包（重传时保持相同）

---

### 2.2 PSN位图管理

**文件**: `open-rdma-rtl/src/PsnContinousChecker.bsv`

#### 2.2.1 位图窗口结构

```bsv
typedef struct {
    tData       data;           // 128位ACK位图
    tBoundary   leftBound;      // 窗口左边界PSN（高20位）
    KeyQP       qpnKeyPart;     // QP标识
} BitmapWindowStorageEntry#(type tData, type tBoundary) deriving(Bits, FShow);
```

#### 2.2.2 PSN结构解析

```
PSN (24位) = [高20位: 窗口边界] | [低4位: 位图偏移]
                ────────────────   ───────────────
                窗口边界索引        位图内偏移(0-15)

示例:
PSN = 0x001234
  ├─ 边界 = 0x123 (高20位)
  └─ 偏移 = 0x4   (低4位) → 位图中的第4位

窗口覆盖范围:
- 边界=0x123 → 覆盖 PSN 0x1230 - 0x123F (16个PSN/步长)
- 位图128位 → 覆盖 8个步长 = 128个连续PSN
```

#### 2.2.3 位图更新流水线（3阶段）

**阶段1: 生成One-Hot位图** (`PsnContinousChecker.bsv:143-187`)

```bsv
rule sendBramQueryReqAndGenOneHotBitmap;
    let req = reqPipeInQueue.first;
    reqPipeInQueue.deq;

    tRowAddr rowAddr = unpack(zeroExtend(pack(getIndexQP(req.qpn))));
    storage[0].putReadReq(rowAddr);  // 读取旧位图

    // 生成新包的one-hot位图
    tBoundary leftBound = unpack(truncateLSB(req.psn));      // 高20位
    Bit#(TLog#(szStride)) offsetInStride = truncate(req.psn); // 低4位
    Bit#(szStride) oneHotStride = 1 << offsetInStride;        // One-hot
    tData bitmap = unpack(zeroExtendLSB(oneHotStride));       // 扩展到128位

    let pipelineEntryOut = BitmapWindowStorageStageOneToTwoPipelineEntry {
        rowAddr: rowAddr,
        newEntry: BitmapWindowStorageEntry{
            leftBound: leftBound,
            data: bitmap,
            qpnKeyPart: getKeyQP(req.qpn)
        },
        isReset: False
    };
    stageOneToTwoPipelineQueue.enq(pipelineEntryOut);
endrule
```

**阶段2: 位图合并与窗口滑动** (`PsnContinousChecker.bsv:190-278`)

```bsv
rule getBramQueryRespAndMergeThem;
    let pipelineEntryIn = stageOneToTwoPipelineQueue.first;
    stageOneToTwoPipelineQueue.deq;

    let entryFromBram = storage[0].readRespPipeOut.first;
    storage[0].readRespPipeOut.deq;

    let oldEntry = entryFromBram;
    let newEntry = pipelineEntryIn.newEntry;

    // 计算边界差值
    tBoundary boundaryDelta = newEntry.leftBound - oldEntry.leftBound;
    tBoundary boundaryDeltaNeg = oldEntry.leftBound - newEntry.leftBound;
    tBoundary boundaryDeltaAbs = msb(boundaryDelta) == 0 ?
                                 boundaryDelta : boundaryDeltaNeg;

    // 检查是否超出边界（8个步长 = 128位）
    let isShiftOutOfBoundary = msb(boundaryDelta) == 0 ? (
        boundaryDelta >= fromInteger(valueOf(TDiv#(szData, szStride)))  // >= 8
    ) : (
        boundaryDeltaNeg >= fromInteger(valueOf(TDiv#(szData, szStride)))
    );

    // 🔴 关键：判断是否丢弃过时的包
    let skipMergeStale = msb(boundaryDelta) == 1 && isShiftOutOfBoundary;

    if (skipMergeStale) begin
        // ⚠️ 包太旧，不更新位图
        let resp = BitmapWindowStorageUpdateResp {
            rowAddr                 : pipelineEntryIn.rowAddr,
            oldEntry                : oldEntry,
            windowShiftedOutData    : -1,
            isShiftOutOfBoundary    : True,
            isShiftWindow           : False,
            newEntry                : oldEntry  // 保持旧状态
        };
        respPipeOutQueue.enq(resp);
    end
    else begin
        // 判断是否需要窗口滑动
        let isShiftWindow = msb(boundaryDelta) == 0 && boundaryDelta > 0;

        if (!isShiftWindow) begin
            newEntry.leftBound = oldEntry.leftBound;  // 保持窗口边界
        end

        // 计算位移量
        Bit#(TLog#(szData)) bitShiftCnt = truncate(pack(boundaryDeltaAbs))
                                          << valueOf(TLog#(szStride));
        tData allOneData = unpack(-1);
        tData windowShiftedOutData = -1;

        if (isShiftOutOfBoundary) begin
            // 包超出边界，清空旧数据
            oldEntry.data = unpack(0);
            windowShiftedOutData = unpack(0);
        end
        else if (isShiftWindow) begin
            // 窗口滑动：检测滑出的位
            let tmpToShift = {pack(oldEntry.data), pack(allOneData)};
            tmpToShift = tmpToShift >> bitShiftCnt;
            oldEntry.data = unpack(truncateLSB(tmpToShift));
            windowShiftedOutData = unpack(truncate(tmpToShift));
        end
        else begin
            // 包在当前窗口内，调整新位图偏移
            newEntry.data = newEntry.data >> bitShiftCnt;
        end

        // 🔑 核心：OR操作合并位图（幂等去重）
        newEntry.data = newEntry.data | oldEntry.data;

        let resp = BitmapWindowStorageUpdateResp {
            rowAddr                 : pipelineEntryIn.rowAddr,
            oldEntry                : oldEntry,
            windowShiftedOutData    : windowShiftedOutData,
            isShiftOutOfBoundary    : isShiftOutOfBoundary,
            isShiftWindow           : isShiftWindow,
            newEntry                : newEntry
        };
        respPipeOutQueue.enq(resp);

        let bramWriteBackReq = BitmapWindowStorageStageTwoToThreePipelineEntry {
            rowAddr : pipelineEntryIn.rowAddr,
            newEntry: newEntry
        };
        stageTwoToThreePipelineQueue.enq(bramWriteBackReq);
    end
endrule
```

**阶段3: 写回BRAM** (`PsnContinousChecker.bsv:281-293`)

```bsv
rule doBramWriteBack;
    let writeBackReq = stageTwoToThreePipelineQueue.first;
    stageTwoToThreePipelineQueue.deq;

    storage[0].write(writeBackReq.rowAddr, writeBackReq.newEntry);
    storage[1].write(writeBackReq.rowAddr, writeBackReq.newEntry);  // 双份备份

    bitmapUpdateBusyReg <= False;
endrule
```

---

### 2.3 丢包检测与ACK/NAK生成

**文件**: `open-rdma-rtl/src/AutoAckGenerator.bsv`

#### 2.3.1 丢包检测逻辑 (`AutoAckGenerator.bsv:184-213`)

```bsv
rule handleMergedBitmap;
    let bitmapResp = bitmapStorage.respPipeOut.first;
    bitmapStorage.respPipeOut.deq;

    let pipelineEntryIn = handleMergedBitmapPipelineQueue.first;
    handleMergedBitmapPipelineQueue.deq;

    // 🔴 丢包检测：窗口滑动时有位被清零
    let hasPacketLost = bitmapResp.isShiftWindow &&
                        bitmapResp.windowShiftedOutData != -1;

    let needSendAckNow = hasPacketLost;  // 有丢包时立即发送ACK

    let autoAckMetaUpdateReq = AtomicUpdateStorageUpdateReq {
        rowAddr: bitmapResp.rowAddr,
        reqData: needSendAckNow
    };
    autoAckMetaAtomicUpdateStorage.reqPipeIn.enq(autoAckMetaUpdateReq);

    let outPipelineEntry = AutoAckGeneratorToGenAutoAckEthPacketPipelineEntry {
        qpn: pipelineEntryIn.qpn,
        qpc: pipelineEntryIn.qpc,
        bitmapUpdateResp: bitmapResp
    };
    genAutoAckEthPacketPipelineQueue.enq(outPipelineEntry);
endrule
```

#### 2.3.2 NAK包生成 (`AutoAckGenerator.bsv:215-294`)

```bsv
rule genAutoAckEthPacket;
    let pipelineEntryIn = genAutoAckEthPacketPipelineQueue.first;
    genAutoAckEthPacketPipelineQueue.deq;

    let bitmapInfo = pipelineEntryIn.bitmapUpdateResp;
    let autoAckMetaResp = autoAckMetaAtomicUpdateStorage.respPipeOut.first;
    autoAckMetaAtomicUpdateStorage.respPipeOut.deq;

    let needSendAckNow = autoAckMetaResp.newValue.hasReported;

    if (needSendAckNow) begin
        // 生成NAK以太网包
        let macIpUdpMeta = ThinMacIpUdpMetaDataForSend {
            dstMacAddr  : pipelineEntryIn.qpc.peerMacAddr,
            srcMacAddr  : localMacAddrReg,
            dstIpAddr   : pipelineEntryIn.qpc.peerIPAddr,
            srcIpAddr   : localIpAddrReg,
            dstUdpPort  : pipelineEntryIn.qpc.peerUdpPort,
            srcUdpPort  : pipelineEntryIn.qpc.localUdpPort,
            ipIdentification : unpack(0),
            ecn         : pipelineEntryIn.qpc.ecn
        };

        // 构建RDMA BTH头
        let rdmaSendPacketMeta = RdmaSendPacketMeta {
            header: RdmaBthAndExtendHeader {
                bth: BTH {
                    trans    : TRANS_TYPE_RC,
                    opcode   : ACKNOWLEDGE,          // ACK操作码
                    solicited: False,
                    isRetry  : False,
                    padCnt   : unpack(0),
                    tver     : unpack(0),
                    msn      : autoAckMetaResp.newValue.ackMsn,
                    fecn     : unpack(0),
                    becn     : unpack(0),
                    resv6    : unpack(0),
                    dqpn     : pipelineEntryIn.qpn,
                    ackReq   : False,
                    resv7    : unpack(0),
                    psn      : zeroExtendLSB(bitmapInfo.newEntry.leftBound)  // 窗口左边界
                },
                rdmaExtendHeaderBuf: buildRdmaExtendHeaderBuffer(pack(AETH{
                    preBitmap       : bitmapInfo.oldEntry.data,      // 滑动前的位图
                    newBitmap       : bitmapInfo.newEntry.data,      // 当前位图
                    isPacketLost    : True,                          // ⭐ 丢包标志
                    isWindowSlided  : True,                          // ⭐ 窗口滑动标志
                    isSendByDriver  : False,
                    resv0           : unpack(0),
                    preBitmapPsn    : bitmapInfo.oldEntry.leftBound  // 滑动前的PSN
                }))
            },
            hasPayload: False
        };

        macIpUdpMetaPipeOutQueue.enq(macIpUdpMeta);
        rdmaPacketMetaPipeOutQueue.enq(rdmaSendPacketMeta);
    end
endrule
```

#### 2.3.3 AETH扩展头结构 (`RdmaHeaders.bsv`)

```bsv
typedef struct {
    AckBitmap           preBitmap;      // 16 Bytes - 滑动前的位图
    AckBitmap           newBitmap;      // 16 Bytes - 当前位图
    Bool                isPacketLost;   // 1 Bit  - 是否检测到丢包
    Bool                isWindowSlided; // 1 Bit  - 窗口是否滑动
    Bool                isSendByDriver; // 1 Bit  - 是否由驱动发送
    ReservedZero#(5)    resv0;
    PSN                 preBitmapPsn;   // 3 Bytes - 滑动前的PSN边界
} AETH deriving(Bits, Bounded, FShow);
```

---

### 2.4 超时机制

**文件**: `open-rdma-rtl/src/AutoAckGenerator.bsv:409-469`

```bsv
typedef 5000 AUTO_ACK_POLLING_TIMEOUT_TICKS;  // 超时阈值

rule timerTask;
    curTimeReg <= curTimeReg + 1;  // 每个时钟周期递增
endrule

// 周期性轮询所有QP
rule sendPollingReq if (bramInitedReg &&
    backgroundPollingStateReg == AutoAckGenBackgroundPollingStateSendReadReq);

    bitmapStorage.readOnlyReqPipeIn.enq(pollingQpIdxReg);
    autoAckMetaAtomicUpdateStorage.readOnlyReqPipeIn.enq(pollingQpIdxReg);
    lastReportTimeStorage.putReadReq(pollingQpIdxReg);

    pollingQpIdxReg <= pollingQpIdxReg + 1;
    backgroundPollingStateReg <= AutoAckGenBackgroundPollingStateGetReadResp;
endrule

// 检测超时并生成ACK
rule handlePollingResult if (bramInitedReg &&
    backgroundPollingStateReg == AutoAckGenBackgroundPollingStateHandleResp);

    let bitmapInfo = pollingQueryRespPipelineReg.tpl_0;
    let ackMeta = pollingQueryRespPipelineReg.tpl_1;
    let lastReportTime = pollingQueryRespPipelineReg.tpl_2;

    let lastRecvPacketHasBeenReported = ackMeta.hasReported;
    let lastRecvPacketIsOldEnough =
        curTimeReg - ackMeta.lastEntryReceiveTime >
        fromInteger(valueOf(AUTO_ACK_POLLING_TIMEOUT_TICKS));

    if ((!lastRecvPacketHasBeenReported) && lastRecvPacketIsOldEnough) begin
        // ⏰ 超时，生成超时ACK
        let desc0 = MetaReportQueueAckDesc {
            isPacketLost    : False,
            isWindowSlided  : False,
            isSendByDriver  : False,
            isSendByLocalHw : True,
            // ...
        };
        pollingTimeoutDescQueue.enq(pack(desc0));
    end

    backgroundPollingStateReg <= AutoAckGenBackgroundPollingStateSendReadReq;
endrule
```

---

## 3. 软件端重传策略

### 3.1 重传标志设置

**文件**: `open-rdma-rtl/src/Descriptors.bsv:51`

```bsv
typedef struct {
    RingbufDescCommonHead      commonHeader;
    PMTU                       pmtu;
    Bool                       isFirst;
    Bool                       isLast;
    Bool                       isRetry;           // ⭐ 软件设置的重传标志
    Bool                       enableEcn;
    ReservedZero#(6)           reserved0;
    QPN                        sqpn;
    QPN                        dqpn;
    TypeQP                     qpType;
    MSN                        msn;
} SendQueueReqDescSeg1 deriving(Bits, FShow);
```

**文件**: `open-rdma-rtl/src/DescriptorParsers.bsv:76`

```bsv
rule forwardSQ;
    let desc = sqDescQueue.first;
    sqDescQueue.deq;

    let req = WorkQueueElem {
        // ...
        isRetry: desc1.isRetry,  // 从描述符读取重传标志
        // ...
    };
    reqPipeOut.enq(req);
endrule
```

---

### 3.2 包生成

**文件**: `open-rdma-rtl/src/PacketGenAndParse.bsv:214-251`

```bsv
function ActionValue#(Maybe#(BTH)) genRdmaBTH(
    WorkQueueElem wqe, Bool isFirst, Bool isLast,
    Bool solicited, PSN psn, PAD padCnt,
    Bool ackReq, ADDR remoteAddr, Length dlen
);
    return actionvalue
        Maybe#(TransType) maybeTrans = ?;
        Maybe#(RdmaOpCode) maybeOpcode = ?;

        case (wqe.qpType)
            IBV_QPT_RC: begin
                maybeTrans = tagged Valid TRANS_TYPE_RC;
                maybeOpcode = tagged Valid genRCOpCode(
                    wqe.opcode, isFirst, isLast, ackReq
                );
            end
            // ...
        endcase

        if (maybeTrans matches tagged Valid .trans &&&
            maybeOpcode matches tagged Valid .opcode) begin

            let bth = BTH {
                trans    : trans,
                opcode   : opcode,
                solicited: solicited,
                isRetry  : wqe.isRetry,    // ⭐ 直接使用WQE中的重传标志
                padCnt   : padCnt,
                tver     : unpack(0),
                msn      : wqe.msn,
                fecn     : unpack(0),
                becn     : unpack(0),
                resv6    : unpack(0),
                dqpn     : wqe.dqpn,
                ackReq   : ackReq,
                resv7    : unpack(0),
                psn      : psn              // ⭐ 重传时使用相同的PSN
            };
            return tagged Valid bth;
        end
        else begin
            return tagged Invalid;
        end
    endactionvalue;
endfunction
```

---

### 3.3 重传任务处理

**文件**: `open-rdma-driver/rust-driver/src/workers/retransmit.rs`

#### 3.3.1 重传任务类型

```rust
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum PacketRetransmitTask {
    /// 新的工作请求
    NewWr {
        qpn: u32,
        wr: SendQueueElem,
    },
    /// 范围重传（选择性重传）
    RetransmitRange {
        qpn: u32,
        psn_low: Psn,      // 包含
        psn_high: Psn,     // 不包含
    },
    /// 全部重传（Go-Back-N）
    RetransmitAll {
        qpn: u32,
    },
    /// ACK确认（释放已确认的包）
    Ack {
        qpn: u32,
        psn: Psn,
    },
}
```

#### 3.3.2 范围重传处理 (`retransmit.rs:70-84`)

```rust
PacketRetransmitTask::RetransmitRange { psn_low, psn_high, .. } => {
    debug!("retransmit range, qpn: {qpn}, low: {psn_low}, high: {psn_high}");

    // 从发送队列查找指定范围的包
    let sqes = sq.range(psn_low, psn_high);

    // 将WR分片成多个包
    let packets = sqes
        .into_iter()
        .flat_map(|sqe| WrPacketFragmenter::new(sqe.wr(), sqe.qp_param(), sqe.psn()))
        .skip_while(|x| x.psn < psn_low)
        .take_while(|x| x.psn < psn_high);

    for mut packet in packets {
        packet.set_is_retry();  // ⭐ 设置重传标志
        self.wr_sender.send(packet);
    }
}
```

#### 3.3.3 元数据报告处理 (`meta_report/worker.rs:147-169`)

```rust
fn handle_nak_remote_hw(&mut self, meta: NakMetaRemoteHw) -> Option<()> {
    debug!("nak remote hw: {meta:?}");

    let tracker = self.send_table.get_qp_mut(meta.qpn)?;

    // 更新发送端追踪器（标记已确认的包）
    if let Some(psn) = tracker.nak_bitmap(
        meta.msn,
        meta.psn_pre,       // 滑动前的PSN
        meta.pre_bitmap,    // 滑动前的位图
        meta.psn_now,       // 滑动后的PSN
        meta.now_bitmap,    // 滑动后的位图
    ) {
        self.sender_updates(meta.qpn, psn);
    }

    // 🔑 触发范围重传
    self.packet_retransmit_tx
        .send(PacketRetransmitTask::RetransmitRange {
            qpn: meta.qpn,
            psn_low: meta.psn_pre,          // 从旧窗口开始
            psn_high: meta.psn_now + 128,   // 到新窗口+128
        });

    Some(())
}
```

---

## 4. 边界条件处理

### 4.1 超出128位位图边界

**问题**: 如果重传包的PSN远远落后于当前窗口（超过8个步长），会发生什么？

**答案**: 硬件会**丢弃**该包，不更新位图。

#### 4.1.1 边界检测代码 (`PsnContinousChecker.bsv:218-236`)

```bsv
// 计算边界差值
tBoundary boundaryDelta = newPSN.leftBound - oldPSN.leftBound;
tBoundary boundaryDeltaNeg = oldPSN.leftBound - newPSN.leftBound;

// 检查是否超出边界（8个步长）
let isShiftOutOfBoundary = msb(boundaryDelta) == 0 ? (
    boundaryDelta >= 8    // TDiv#(128, 16) = 8
) : (
    boundaryDeltaNeg >= 8
);

// 新包落后于当前窗口 且 超出边界
let skipMergeStale = msb(boundaryDelta) == 1 && isShiftOutOfBoundary;

if (skipMergeStale) begin
    // ⚠️ 丢弃该包，不更新位图
    let resp = BitmapWindowStorageUpdateResp {
        rowAddr                 : pipelineEntryIn.rowAddr,
        oldEntry                : oldEntry,
        windowShiftedOutData    : -1,
        isShiftOutOfBoundary    : True,    // ⭐ 标记超出边界
        isShiftWindow           : False,
        newEntry                : oldEntry  // 保持旧状态
    };
    respPipeOutQueue.enq(resp);  // 仍然通知软件
end
```

#### 4.1.2 边界场景示例

```
当前窗口：边界=100 → 覆盖PSN 1600-1727

场景1: 收到PSN=1650 (边界=103)
  边界差 = 103 - 100 = 3 < 8 ✅
  结果：在窗口内，更新位图

场景2: 收到PSN=1500 (边界=93)
  边界差 = 93 - 100 = -7
  |差值| = 7 < 8 ✅
  结果：允许合并（窗口不回退）

场景3: 收到PSN=1400 (边界=87)
  边界差 = 87 - 100 = -13
  |差值| = 13 > 8 ❌
  skipMergeStale = True
  结果：硬件丢弃，通知软件重传
```

---

### 4.2 窗口滑动后的重传包

**问题**: 为什么重传包（PSN=1400）在第一次被丢弃后，重传时能被接受？

**答案**: NAK报告的是**窗口滑动过程中检测到的丢包**，软件重传时窗口已经向前移动，重传包刚好在新的8步长容忍范围内。

#### 4.2.1 NAK元数据结构 (`meta_report/types.rs:325-332`)

```rust
pub(crate) struct NakMetaRemoteHw {
    pub(crate) qpn: u32,
    pub(crate) msn: u16,
    pub(crate) psn_pre: Psn,      // ⭐ 滑动前的窗口边界
    pub(crate) pre_bitmap: u128,  // ⭐ 滑动前的位图
    pub(crate) psn_now: Psn,      // ⭐ 滑动后的窗口边界
    pub(crate) now_bitmap: u128,  // ⭐ 滑动后的位图
}
```

#### 4.2.2 窗口滑动时序

```
时刻1: 窗口边界=100 (覆盖PSN 1600-1727)
  位图：11111011... (位6=0，PSN=1606丢失)

时刻2: 收到PSN=1728
  ├─ 触发窗口滑动：边界 100 → 108
  ├─ 检测滑出数据：windowShiftedOutData
  │  └─ 发现位6=0 → 有包丢失 ❌
  │
  ├─ 生成NAK：
  │  ├─ psn_pre = 1600  (旧边界)
  │  ├─ psn_now = 1728  (新边界)
  │  └─ pre_bitmap = 滑出的128位
  │
  └─ 发送NAK给发送端

发送端收到NAK后:
  └─ 重传范围：[1600, 1728+128) = [1600, 1856)

接收端收到重传PSN=1606:
  ├─ 当前边界 = 108 (覆盖1728-1855)
  ├─ PSN=1606边界 = 100
  ├─ 边界差 = 100 - 108 = -8
  ├─ |-8| = 8 ✅ 刚好在边界！
  │
  └─ 窗口不回退，允许合并：
     ├─ isShiftWindow = False
     ├─ 位图右移对齐
     └─ OR合并到当前位图
```

#### 4.2.3 PSN重映射 (`meta_report/types.rs:45-49`)

```rust
fn remap_psn(psn: Psn) -> Psn {
    // 128 (window size) - 16 (first stride)
    const OFFSET: u32 = 112;
    psn - OFFSET  // 软件侧PSN偏移调整
}
```

---

### 4.3 重复包处理

**问题**: 如果同一个包（相同PSN）被接收多次（网络延迟导致），会发生什么？

**答案**:
1. **位图层面**：OR操作天然去重（1 | 1 = 1），幂等无副作用
2. **数据层面**：重复包的数据**会被再次写入MR**（覆盖相同地址）

#### 4.3.1 位图幂等性证明

```
定理：f(x) = x | bitmap_onehot(psn) 是幂等函数

证明：
  设 b = bitmap_onehot(psn)
  则 f(f(x)) = f(x | b) = (x | b) | b = x | b = f(x)

  ∴ 多次接收同一PSN不会改变最终位图状态
```

#### 4.3.2 重复包完整流程

```
场景：PSN=1650 的包被接收两次

第一次接收:
  1. issuePayloadConReqOrDiscard
     └─ 💾 写入数据到MR (第一次)
  2. handleConResp
     └─ 更新位图[50] = 1
  3. AutoAckGenerator
     └─ 位图合并：0 | 1 = 1

第二次接收（重复包）:
  1. issuePayloadConReqOrDiscard
     ⚠️ 没有检查PSN是否已收到
     └─ 💾 再次写入数据到MR (覆盖相同地址)
  2. handleConResp
     └─ 更新位图[50] | 1 = 1 (幂等)
  3. AutoAckGenerator
     └─ 位图合并：1 | 1 = 1 (无变化)
```

---

## 5. 数据写入与幂等性

### 5.1 数据写入路径不检查PSN

**文件**: `open-rdma-rtl/src/RQ.bsv:643-660`

```bsv
rule issuePayloadConReqOrDiscard;
    let rdmaPacketMeta = pipelineEntryIn.rdmaPacketMeta;
    let packetStatus = pipelineEntryIn.packetStatus;
    let bth = rdmaPacketMeta.header.bth;
    let reth = extractPriRETH(rdmaPacketMeta.header.rdmaExtendHeaderBuf, bth.trans);
    let mrEntry = pipelineEntryIn.mrEntry;

    if (rdmaPacketMeta.hasPayload) begin
        let isDiscard = !isRecvPacketStatusNormal(packetStatus);
        filterCmdQ.enq(isDiscard);

        if (!isDiscard) begin
            // ⚠️ 关键：这里没有检查PSN位图或isRetry！
            let payloadConReq = PayloadConReq{
                addr        : reth.va,      // 目标地址
                len         : pipelineEntryIn.packetLen,
                baseVA      : mrEntry.baseVA,
                pgtOffset   : mrEntry.pgtOffset
            };
            conReqPipeOutQ.enq(payloadConReq);  // 直接写入DMA请求
        end
    end
endrule
```

### 5.2 PSN更新在数据写入之后

**文件**: `open-rdma-rtl/src/RQ.bsv:728-736`

```bsv
rule handleConResp;
    let pipelineEntryIn = handleConRespPipeQ.first;
    let rdmaPacketMeta = pipelineEntryIn.rdmaPacketMeta;
    let bth = rdmaPacketMeta.header.bth;

    if (!isDiscard) begin
        // ⭐ 数据已经在上一个规则中写入了

        let needUpdatePsnBitmap = rdmaPacketMeta.hasPayload;
        if (needUpdatePsnBitmap) begin
            // ⭐ 在数据写入后才更新位图
            let autoAckReq = AutoAckGeneratorReq{
                psn: bth.psn,
                qpn: bth.dqpn,
                qpc: pipelineEntryIn.qpc
            };
            autoAckGenReqPipeOutQueue.enq(autoAckReq);
        end
    end
endrule
```

### 5.3 为什么允许重复写入？

#### 5.3.1 InfiniBand规范要求

根据 InfiniBand 规范（Volume 1, Section 9.4.4）：

> **Duplicate Packet Handling**:
> - 接收端必须处理重复包
> - 重复包的数据必须被写入（即使PSN已确认）
> - 只有在数据写入后才能生成ACK

#### 5.3.2 幂等性保证

RDMA Write 操作本质上是幂等的：

```
定理：对于相同PSN的包，数据内容必须相同

证明：
  - PSN唯一标识一个包
  - 发送端不会改变已发送包的内容
  - 重传时使用相同的源地址和数据

  ∴ 多次写入相同数据到相同地址 = 幂等操作
```

#### 5.3.3 硬件简化设计

如果在数据写入前检查PSN位图：

| 方案 | 优点 | 缺点 |
|------|------|------|
| **写入前检查PSN** | 避免重复写入 | 位图查询延迟增加<br>流水线复杂度增加<br>吞吐量下降 |
| **写入后更新PSN** ✅ | 硬件简单<br>流水线高效<br>数据/控制路径解耦 | 重复包会重复写入<br>浪费带宽 |

**设计选择**: 依赖幂等性，允许重复写入，简化硬件。

---

## 6. 完整时序图

```
发送端                          接收端                      处理结果
  │                              │
  ├─PSN=100 (isRetry=0)─────────>│
  │                              ├─更新位图[100]=1
  │                              │
  ├─PSN=101 (isRetry=0)─X 丢包
  │                              │
  ├─PSN=102 (isRetry=0)─────────>│
  │                              ├─更新位图[102]=1
  │                              │
  ├─PSN=110 (isRetry=0)─────────>│
  │                              ├─窗口滑动：边界0→1
  │                              ├─检测滑出位[101]=0 → 丢包 ❌
  │                              │
  │<──────NAK─────────────────────┤
  │  AETH:                        │
  │    psn_pre = 0                │
  │    psn_now = 16               │
  │    pre_bitmap = 0x5 (...0101) │  位1=0，PSN=101丢失
  │    now_bitmap = ...            │
  │    isPacketLost = True        │
  │                              │
  ├─解析NAK，发现PSN=101丢失      │
  │                              │
  ├─PSN=101 (isRetry=1)─────────>│  ← 重传包
  │                              ├─边界检查：|0-1|=1 < 8 ✅
  │                              ├─更新位图[101]=1 (右移合并)
  │                              │
  │<──────ACK─────────────────────┤
  │  位图：...0111 (全部收齐)     │
  │                              │
  └─完成                         └─完成
```

---

## 7. 关键代码索引

| 功能 | 文件 | 关键行 | 说明 |
|------|------|--------|------|
| **BTH头定义** | RdmaHeaders.bsv | 158 | isRetry + PSN |
| **PSN位图管理** | PsnContinousChecker.bsv | 143-278 | 3阶段流水线 |
| **边界检测** | PsnContinousChecker.bsv | 218-236 | skipMergeStale逻辑 |
| **NAK生成** | AutoAckGenerator.bsv | 184-294 | 丢包检测+ACK生成 |
| **超时检测** | AutoAckGenerator.bsv | 409-469 | 周期性轮询 |
| **包生成（重传标志）** | PacketGenAndParse.bsv | 214-251 | genRdmaBTH |
| **描述符解析** | DescriptorParsers.bsv | 76 | isRetry读取 |
| **数据写入路径** | RQ.bsv | 643-660 | issuePayloadConReqOrDiscard |
| **PSN更新** | RQ.bsv | 728-736 | handleConResp |
| **软件重传任务** | retransmit.rs | 70-84 | RetransmitRange处理 |
| **NAK处理** | meta_report/worker.rs | 147-169 | handle_nak_remote_hw |

---

## 8. 设计权衡与优化

### 8.1 硬件设计权衡

| 设计决策 | 优点 | 缺点 | 选择理由 |
|---------|------|------|---------|
| **位图宽度=128位** | 覆盖范围大<br>支持SACK | BRAM资源消耗 | 平衡性能与资源 |
| **8步长容忍度** | 处理乱序<br>减少重传 | 可能丢弃旧包 | 保护状态完整性 |
| **数据路径不检查PSN** | 流水线高效<br>硬件简单 | 重复包会重复写入 | 依赖幂等性 |
| **硬件自动ACK** | 减少CPU开销<br>低延迟 | 硬件复杂度增加 | 性能优先 |

### 8.2 软件设计权衡

| 设计决策 | 优点 | 缺点 | 选择理由 |
|---------|------|------|---------|
| **范围重传** | 精确重传丢失包<br>节省带宽 | 软件复杂度增加 | SACK优化 |
| **维护发送队列** | 支持重传<br>灵活策略 | 内存开销 | RC语义要求 |
| **PSN重映射** | 软硬件坐标对齐 | 理解难度增加 | 实现细节 |

### 8.3 性能优化要点

1. **减少重传**
   - 128位位图提供细粒度ACK信息
   - 选择性重传避免Go-Back-N的低效

2. **降低延迟**
   - 硬件自动生成ACK/NAK（无软件介入）
   - 流水线化的位图更新（3周期）

3. **提高吞吐**
   - 数据路径和控制路径解耦
   - 允许重复写入，避免位图查询阻塞

4. **可靠性保证**
   - 边界检测防止窗口污染
   - 超时机制确保不丢失包

---

## 9. 常见问题 FAQ

### Q1: 为什么位图是128位？

**A**: 平衡覆盖范围和资源消耗：
- 128位覆盖128个连续PSN
- 对于4KB PMTU，可覆盖512KB数据
- BRAM资源：每个QP仅需128位（16字节）

### Q2: 重传包一定会被接受吗？

**A**: 不一定：
- ✅ 在8步长容忍范围内：接受并合并
- ❌ 超出8步长：硬件丢弃，需再次重传

### Q3: 重复包会导致数据不一致吗？

**A**: 不会，前提是发送端遵守规范：
- InfiniBand要求：相同PSN必须有相同数据
- 重复写入 = 覆盖相同数据到相同地址 = 幂等

### Q4: 软件如何知道哪些包需要重传？

**A**: 通过NAK元数据：
- `psn_pre` + `pre_bitmap`：滑动前的状态
- `psn_now` + `now_bitmap`：滑动后的状态
- 位为0的位置 = 丢失的PSN

### Q5: 为什么不在数据写入前检查PSN？

**A**: 硬件简化设计：
- 检查PSN需要额外的BRAM读取（增加延迟）
- 数据路径和控制路径解耦（提高吞吐）
- 依赖幂等性，允许重复写入

---

## 10. 参考资料

1. **InfiniBand规范**: Volume 1, Section 9.4.4 - Reliable Connection
2. **TCP选择性确认**: RFC 2018 - TCP Selective Acknowledgment Options
3. **滑动窗口协议**: Computer Networks (Tanenbaum) - Chapter 3.4
4. **Bluespec参考**: BSV Reference Guide - Rules and Methods

---

## 附录 A: 位图窗口计算示例

```
给定: PSN = 0x001234 (十进制4660)

步骤1: 提取边界（高20位）
  边界 = PSN >> 4 = 0x123 (十进制291)

步骤2: 提取偏移（低4位）
  偏移 = PSN & 0xF = 0x4 (十进制4)

步骤3: 计算窗口范围
  窗口起始 = 边界 << 4 = 0x1230 (十进制4656)
  窗口结束 = 窗口起始 + 127 = 0x12AF (十进制4783)

步骤4: 计算位图位置
  位图索引 = 偏移 + (边界 % 8) * 16
           = 4 + (291 % 8) * 16
           = 4 + 3 * 16
           = 52

结论: PSN=0x001234 对应位图的第52位
```

---

**文档结束**
