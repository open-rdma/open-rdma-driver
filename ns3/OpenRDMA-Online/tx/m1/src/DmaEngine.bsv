package DmaEngine;  // 包是 bsv 中编译和命名空间的基本单位

import Assert :: *; // 导入所有公开符号，通常用于断言、形式验证或仿真检查，例如 assert、assume、cover 等。
import Connectable :: *;    // 提供类型类 Connectable#(a, b) 和连接函数 mkConnection，用于把两个模块或接口连接起来。
import FIFOF :: *;  // 用于 FIFO 队列的实现和操作，例如 mkSizedFIFOF、enq、deq、notEmpty、notFull 等。
import Vector :: *; // 用于向量类型的定义和操作，例如 Vector#(N, T)、replicateM、unpack、pack 等。

import CommonTypes :: *;    // 自定义公共类型包
import DmaTypes :: *;   // 自定义 DMA 相关类型包

// DMA Engine 接收已经完成 VA->PA translation 和 boundary split 的
// Wqe-shaped physical chunk stream。它不再拆分地址，只负责内部 tag 分配、
// external DMA completion reorder、message 顺序恢复和 raw payload completion。
//
// 顶层数据流：
//   DmaChunkReq -> Normalizer -> Scheduler -> Completion Reorder -> Tracker
// external DMA 通过 DmaReadRequest / DmaCompletionBeat 与 Reorder 双向交互。

// ============================================================================
// 公共辅助函数和内部类型
// ============================================================================

// generation 为有限 epoch；外部必须保证 burstLast 后不再返回旧 tag，且
// response 生命周期短于同一 slot 的 8192 次回收周期。
typedef Bit#(13) DmaGeneration;

function Bit#(1024) dmaMaskData(Bit#(1024) data, Bit#(128) keep);
    Vector#(128, Bit#(8)) bytes = unpack(data);
    for (Integer byteIndex = 0; byteIndex < 128; byteIndex = byteIndex + 1)
        if (keep[byteIndex] == 0)
            bytes[byteIndex] = 0;
    return pack(bytes);
endfunction

// Normalizer 为每个上游 chunk 补充逻辑 messageOffset；physicalAddr 只描述
// 当前 chunk，因此相邻 descriptor 的 PA 不要求连续。
typedef struct {
    DmaWqeTag        wqeTag;
    DmaChunkSeq      chunkSeq;
    Bit#(64)         physicalAddr;
    DmaBurstLength   length;
    DmaMessageLength messageOffset;
    Bit#(1)          isFirst;
    Bit#(1)          isLast;
} DmaBurstDescriptor deriving(Bits, Eq, FShow);

// Scheduler 分配内部 transaction tag 后形成 ScheduledBurst；该 tag 的
// 生命周期只覆盖一次 external DMA chunk request。
typedef struct {
    DmaTag             tag;
    DmaBurstDescriptor burst;
} ScheduledBurst deriving(Bits, Eq, FShow);

typedef struct {
    DmaTag tag;
} DmaSlotRetire deriving(Bits, Eq, FShow);

// validBytes 在 completion admission 时计算并随 beat 保存，避免 drain 路径
// 为每个 slot 再综合一次 128-bit popcount。
typedef struct {
    DmaCompletionBeat completion;
    Bit#(8)           validBytes;
} BufferedCompletion deriving(Bits, Eq, FShow);

// Registration 和 retire 只携带 message ID：完整 chunk metadata 由 Scheduler/
// Reorder context 保存，message CAM 只负责阻止 active ID 过早复用。
typedef struct {
    DmaWqeTag wqeTag;
} DmaMessageRegistration deriving(Bits, Eq, FShow);

typedef struct {
    DmaWqeTag wqeTag;
} DmaMessageRetire deriving(Bits, Eq, FShow);

// Reorder 输出仍保留 chunk 边界标志；Tracker 再把它转换为整条 message 的
// first/last，并检查 chunkSeq 与 messageOffset 连续性。
typedef struct {
    DmaTag          tag;
    DmaWqeTag       wqeTag;
    DmaChunkSeq     chunkSeq;
    DmaMessageLength messageOffset;
    Bit#(1024)      data;
    Bit#(128)       keep;
    Bit#(1)         burstFirst;
    Bit#(1)         burstLast;
    Bit#(1)         isFirst;
    Bit#(1)         isLast;
} DmaOrderedBeat deriving(Bits, Eq, FShow);

// payload、有效字节数和候选 done 保存在同一 queue entry 中，使 public
// payload handshake 可以原子提交 byte accounting 与最终完成事件。
typedef struct {
    DmaPayloadBeat payload;
    Bit#(8)        validBytes;
    DmaMessageDone done;
} TrackedPayloadBeat deriving(Bits, Eq, FShow);

// ============================================================================
// Chunk Normalizer：WqeDmaRead physical chunk -> 内部 burst descriptor
// ============================================================================

interface DmaChunkNormalizerIfc;
    interface PipeIn#(DmaChunkReq) chunkIn;
    interface PipeOut#(DmaBurstDescriptor) burstOut;
    interface PipeOut#(DmaMessageRegistration) registrationOut;
    interface PipeIn#(DmaMessageRetire) messageRetireIn;
endinterface

(* synthesize *)
module mkDmaChunkNormalizer(DmaChunkNormalizerIfc);
    FIFOF#(DmaChunkReq) chunkQ <- mkSizedFIFOF(2);
    FIFOF#(DmaBurstDescriptor) burstQ <- mkSizedFIFOF(2);
    FIFOF#(DmaMessageRegistration) registrationQ <- mkSizedFIFOF(8);
    FIFOF#(DmaMessageRetire) retireQ <- mkSizedFIFOF(2);

    // active CAM 的生命周期从 first chunk admission 开始，到最后 raw payload
    // 被下游接受并返回 message retire 为止。
    Vector#(8, Reg#(DmaWqeTag)) activeTag <- replicateM(mkReg(0));
    Reg#(Bit#(8)) activeMask <- mkReg(0);
    Reg#(DmaSlot) admissionCursor <- mkReg(0);

    // 下列寄存器只描述当前正在接收的一个 message。上游契约是
    // message-major，因此不同 message 的 chunk 不允许交错。
    Reg#(Bool) inMessage <- mkReg(False);
    Reg#(DmaWqeTag) currentTag <- mkReg(0);
    Reg#(DmaChunkSeq) expectedChunkSeq <- mkReg(0);
    Reg#(DmaMessageLength) nextMessageOffset <- mkReg(0);

    Maybe#(DmaSlot) selectedMessageSlot =
        dmaFindFreeSlot(~activeMask, admissionCursor);

    // 协议错误不应因 burstQ 或 active CAM 反压而被隐藏。
    rule validateChunk (chunkQ.notEmpty);
        let chunk = chunkQ.first;
        Bit#(65) endExclusive = zeroExtend(chunk.physAddr) +
            zeroExtend(chunk.len);
        Bit#(33) boundaryEnd = zeroExtend(chunk.physAddr[11:0]) +
            zeroExtend(chunk.len);

        dynamicAssert(chunk.len != 0, "DMA_ASSERT_ZERO_CHUNK");
        dynamicAssert(chunk.len <= 32'd1024, "DMA_ASSERT_CHUNK_TOO_LARGE");
        dynamicAssert(endExclusive[64] == 0, "DMA_ASSERT_CHUNK_ADDRESS_OVERFLOW");
        dynamicAssert(boundaryEnd <= 33'd4096, "DMA_ASSERT_CHUNK_CROSSES_4K");

        if (!inMessage) begin
            dynamicAssert(chunk.isFirst, "DMA_ASSERT_MISSING_CHUNK_FIRST");
            dynamicAssert(chunk.chunkSeq == 0, "DMA_ASSERT_FIRST_CHUNK_SEQUENCE");
        end else begin
            dynamicAssert(!chunk.isFirst, "DMA_ASSERT_DUPLICATE_CHUNK_FIRST");
            dynamicAssert(chunk.wqeTag == currentTag, "DMA_ASSERT_CHUNK_TAG_CHANGED");
            dynamicAssert(chunk.chunkSeq == expectedChunkSeq,
                "DMA_ASSERT_CHUNK_SEQUENCE");
        end
        if (!chunk.isLast)
            dynamicAssert(chunk.chunkSeq != '1, "DMA_ASSERT_CHUNK_SEQUENCE_OVERFLOW");
    endrule

    // Message retire 优先于新 first chunk admission，避免同拍 CAM 多写。
    rule retireMessage (retireQ.notEmpty);
        let retired = retireQ.first;
        Maybe#(DmaSlot) matched = tagged Invalid;
        Bit#(4) matchCount = 0;
        for (Integer i = 0; i < 8; i = i + 1) begin
            if (activeMask[i] == 1 && activeTag[i] == retired.wqeTag) begin
                if (!isValid(matched))
                    matched = tagged Valid fromInteger(i);
                matchCount = matchCount + 1;
            end
        end
        dynamicAssert(matchCount == 1, "DMA_ASSERT_RETIRE_UNKNOWN_MESSAGE");
        DmaSlot slot = fromMaybe(0, matched);
        Bit#(8) nextActiveMask = activeMask;
        nextActiveMask[slot] = 0;
        activeMask <= nextActiveMask;
        retireQ.deq;
    endrule

    // first chunk 同时完成三件事：分配 message CAM slot、注册全局 message
    // 顺序、向 Scheduler 发布 offset=0 的内部 descriptor。
    rule acceptFirstChunk (chunkQ.notEmpty && !inMessage &&
                           chunkQ.first.isFirst && burstQ.notFull &&
                           registrationQ.notFull && isValid(selectedMessageSlot) &&
                           !retireQ.notEmpty);
        let chunk = chunkQ.first;
        DmaSlot slot = fromMaybe(0, selectedMessageSlot);
        Bool duplicate = False;
        for (Integer i = 0; i < 8; i = i + 1)
            if (activeMask[i] == 1 && activeTag[i] == chunk.wqeTag)
                duplicate = True;
        dynamicAssert(!duplicate, "DMA_ASSERT_DUPLICATE_WQE_TAG");
        Bit#(33) nextOffsetWide = zeroExtend(chunk.len);
        dynamicAssert(nextOffsetWide[32] == 0, "DMA_ASSERT_MESSAGE_LENGTH_OVERFLOW");

        burstQ.enq(DmaBurstDescriptor {
            wqeTag: chunk.wqeTag,
            chunkSeq: chunk.chunkSeq,
            physicalAddr: chunk.physAddr,
            length: truncate(chunk.len),
            messageOffset: 0,
            isFirst: 1,
            isLast: pack(chunk.isLast)
        });
        registrationQ.enq(DmaMessageRegistration { wqeTag: chunk.wqeTag });
        chunkQ.deq;

        Bit#(8) nextActiveMask = activeMask;
        nextActiveMask[slot] = 1;
        activeMask <= nextActiveMask;
        activeTag[slot] <= chunk.wqeTag;
        admissionCursor <= slot + 1;

        if (chunk.isLast) begin
            inMessage <= False;
            currentTag <= 0;
            expectedChunkSeq <= 0;
            nextMessageOffset <= 0;
        end else begin
            inMessage <= True;
            currentTag <= chunk.wqeTag;
            expectedChunkSeq <= chunk.chunkSeq + 1;
            nextMessageOffset <= truncate(nextOffsetWide);
        end
    endrule

    // continuation 只推进逻辑 offset/sequence；physicalAddr 直接来自当前
    // chunk，不能由上一条 physicalAddr 加 length 推导。
    rule acceptContinuationChunk (chunkQ.notEmpty && inMessage &&
                                  !chunkQ.first.isFirst && burstQ.notFull);
        let chunk = chunkQ.first;
        Bit#(33) nextOffsetWide = zeroExtend(nextMessageOffset) +
            zeroExtend(chunk.len);
        dynamicAssert(nextOffsetWide[32] == 0, "DMA_ASSERT_MESSAGE_LENGTH_OVERFLOW");

        burstQ.enq(DmaBurstDescriptor {
            wqeTag: chunk.wqeTag,
            chunkSeq: chunk.chunkSeq,
            physicalAddr: chunk.physAddr,
            length: truncate(chunk.len),
            messageOffset: nextMessageOffset,
            isFirst: 0,
            isLast: pack(chunk.isLast)
        });
        chunkQ.deq;

        if (chunk.isLast) begin
            inMessage <= False;
            currentTag <= 0;
            expectedChunkSeq <= 0;
            nextMessageOffset <= 0;
        end else begin
            expectedChunkSeq <= chunk.chunkSeq + 1;
            nextMessageOffset <= truncate(nextOffsetWide);
        end
    endrule

    interface chunkIn = toPipeIn(chunkQ);
    interface burstOut = toPipeOut(burstQ);
    interface registrationOut = toPipeOut(registrationQ);
    interface messageRetireIn = toPipeIn(retireQ);
endmodule

// ============================================================================
// Ordered Scheduler：显式 slot/generation tag 分配与回收
// ============================================================================

interface DmaOrderedSchedulerIfc;
    interface PipeIn#(DmaBurstDescriptor) burstIn;
    interface PipeOut#(ScheduledBurst) scheduledOut;
    interface PipeIn#(DmaSlotRetire) retireIn;
endinterface

(* synthesize *)
module mkOrderedDmaReadScheduler(DmaOrderedSchedulerIfc);
    FIFOF#(DmaBurstDescriptor) burstQ <- mkSizedFIFOF(2);
    FIFOF#(ScheduledBurst) scheduledQ <- mkSizedFIFOF(2);
    FIFOF#(DmaSlotRetire) retireQ <- mkSizedFIFOF(2);

    // freeMask=1 表示可分配。每个 slot 独立维护 generation，slot 回收时
    // generation+1，避免普通 late completion 命中新事务。
    Reg#(Bit#(8)) freeMask <- mkReg('1);
    Vector#(8, Reg#(DmaGeneration)) generation <- replicateM(mkReg(0));
    Reg#(DmaSlot) allocCursor <- mkReg(0);

    Maybe#(DmaSlot) selectedFree = dmaFindFreeSlot(freeMask, allocCursor);

    rule retireSlot (retireQ.notEmpty);
        let retire = retireQ.first;
        DmaSlot slot = retire.tag[2:0];
        DmaGeneration tagGeneration = retire.tag[15:3];
        dynamicAssert(freeMask[slot] == 0, "DMA_ASSERT_RETIRE_FREE_SLOT");
        dynamicAssert(generation[slot] == tagGeneration,
            "DMA_ASSERT_RETIRE_GENERATION");

        Bit#(8) nextFreeMask = freeMask;
        nextFreeMask[slot] = 1;
        freeMask <= nextFreeMask;
        generation[slot] <= generation[slot] + 1;
        retireQ.deq;
    endrule

    // allocation 只发布带 tag 的 descriptor；真正安装 Reorder context 和
    // 对外可见的 DMA request 由下一级在同一原子rule中完成。
    rule allocateBurst (burstQ.notEmpty && scheduledQ.notFull &&
                        isValid(selectedFree) && !retireQ.notEmpty);
        let burst = burstQ.first;
        DmaSlot slot = fromMaybe(0, selectedFree);
        DmaTag tag = { generation[slot], slot };
        Bit#(8) nextFreeMask = freeMask;
        nextFreeMask[slot] = 0;

        burstQ.deq;
        freeMask <= nextFreeMask;
        allocCursor <= slot + 1;
        scheduledQ.enq(ScheduledBurst { tag: tag, burst: burst });
    endrule

    interface burstIn = toPipeIn(burstQ);
    interface scheduledOut = toPipeOut(scheduledQ);
    interface retireIn = toPipeIn(retireQ);
endmodule

// ============================================================================
// Completion Reorder：按 tag 收 completion，按 issue order 输出
// ============================================================================

interface DmaCompletionReorderIfc;
    interface PipeIn#(ScheduledBurst) scheduledIn;
    interface PipeOut#(DmaReadRequest) dmaReadReqOut;
    interface PipeIn#(DmaCompletionBeat) dmaCompletionIn;
    interface PipeOut#(DmaOrderedBeat) orderedBeatOut;
    interface PipeOut#(DmaSlotRetire) retireOut;
endinterface

(* synthesize *)
module mkDmaCompletionReorder(DmaCompletionReorderIfc);
    FIFOF#(ScheduledBurst) scheduledQ <- mkSizedFIFOF(2);
    FIFOF#(DmaReadRequest) dmaReqQ <- mkSizedFIFOF(2);
    FIFOF#(DmaCompletionBeat) completionQ <- mkSizedFIFOF(2);
    FIFOF#(DmaOrderedBeat) orderedQ <- mkSizedFIFOF(2);
    FIFOF#(DmaSlotRetire) retireQ <- mkSizedFIFOF(2);
    FIFOF#(DmaTag) issueOrderQ <- mkSizedFIFOF(8);

    // ctx* 保存每个 outstanding chunk 的恢复信息；receive* 只负责接收
    // completion，drain* 只负责按 issueOrderQ 顺序输出，两组计数独立。
    Vector#(8, Reg#(Bool)) ctxValid <- replicateM(mkReg(False));
    Vector#(8, Reg#(DmaTag)) ctxTag <- replicateM(mkReg(0));
    Vector#(8, Reg#(DmaBurstDescriptor)) ctxBurst <- replicateM(mkReg(unpack(0)));
    Vector#(8, Reg#(DmaBurstLength)) recvBytes <- replicateM(mkReg(0));
    Vector#(8, Reg#(Bit#(4))) recvBeatCount <- replicateM(mkReg(0));
    Vector#(8, Reg#(Bool)) recvComplete <- replicateM(mkReg(False));
    Vector#(8, Reg#(DmaBurstLength)) drainBytes <- replicateM(mkReg(0));
    Vector#(8, Reg#(Bit#(4))) drainBeatCount <- replicateM(mkReg(0));
    Vector#(8, FIFOF#(BufferedCompletion)) beatQ <- replicateM(mkSizedFIFOF(16));

    // Context、issue order 与外部 request 在同一原子提交点安装。
    for (Integer i = 0; i < 8; i = i + 1) begin
        rule installScheduledBurst (scheduledQ.notEmpty &&
                                    scheduledQ.first.tag[2:0] == fromInteger(i) &&
                                    !ctxValid[i]);
            let scheduled = scheduledQ.first;
            let burst = scheduled.burst;

            ctxValid[i] <= True;
            ctxTag[i] <= scheduled.tag;
            ctxBurst[i] <= burst;
            recvBytes[i] <= 0;
            recvBeatCount[i] <= 0;
            recvComplete[i] <= False;
            drainBytes[i] <= 0;
            drainBeatCount[i] <= 0;
            issueOrderQ.enq(scheduled.tag);
            dmaReqQ.enq(DmaReadRequest {
                tag: scheduled.tag,
                wqeTag: burst.wqeTag,
                chunkSeq: burst.chunkSeq,
                physAddr: burst.physicalAddr,
                len: burst.length,
                messageOffset: burst.messageOffset,
                isFirst: burst.isFirst,
                isLast: burst.isLast
            });
            scheduledQ.deq;
        endrule
    end

    // 非法 tag 即使目标 FIFO 不可写也会立即报错。
    rule validateCompletionTarget (completionQ.notEmpty);
        let completion = completionQ.first;
        DmaSlot slot = completion.tag[2:0];
        dynamicAssert(ctxValid[slot], "DMA_ASSERT_UNKNOWN_TAG");
        dynamicAssert(ctxTag[slot] == completion.tag, "DMA_ASSERT_TAG_GENERATION");
        dynamicAssert(!recvComplete[slot], "DMA_ASSERT_COMPLETION_AFTER_LAST");
    endrule

    // 每个 slot 的 receive rule 只在完整 tag 和低3-bit slot同时匹配时触发。
    // expectedKeep 精确描述当前128B window中属于该chunk的有效byte。
    for (Integer i = 0; i < 8; i = i + 1) begin
        rule receiveCompletion (completionQ.notEmpty && ctxValid[i] &&
                                !recvComplete[i] &&
                                ctxTag[i] == completionQ.first.tag &&
                                completionQ.first.tag[2:0] == fromInteger(i));
            let completion = completionQ.first;
            let burst = ctxBurst[i];
            DmaBurstLength received = recvBytes[i];
            DmaBurstLength remaining = burst.length - received;
            Bit#(64) expectedAddr = burst.physicalAddr + zeroExtend(received);
            Bit#(64) expectedWindow = expectedAddr & 64'hFFFFFFFFFFFFFF80;
            Bit#(8) expectedOffset = zeroExtend(expectedAddr[6:0]);
            Bit#(8) toWindowEnd = 8'd128 - zeroExtend(expectedAddr[6:0]);
            DmaBurstLength runWide = remaining < zeroExtend(toWindowEnd)
                ? remaining
                : zeroExtend(toWindowEnd);
            Bit#(8) expectedRun = truncate(runWide);
            Bit#(128) expectedKeep = dmaKeepMask(expectedOffset, expectedRun);
            Bit#(8) validBytes = dmaKeepCount(completion.keep);
            DmaBurstLength receivedNext = received + zeroExtend(validBytes);
            Bool expectedLast = (receivedNext == burst.length);

            dynamicAssert(completion.windowAddr[6:0] == 0,
                "DMA_ASSERT_WINDOW_UNALIGNED");
            dynamicAssert(completion.keep != 0, "DMA_ASSERT_ZERO_KEEP");
            dynamicAssert(dmaKeepIsContiguous(completion.keep),
                "DMA_ASSERT_NONCONTIGUOUS_KEEP");
            dynamicAssert(completion.windowAddr == expectedWindow,
                "DMA_ASSERT_WINDOW_ADDRESS");
            dynamicAssert(receivedNext <= burst.length, "DMA_ASSERT_OVER_COMPLETION");
            dynamicAssert(completion.keep == expectedKeep, "DMA_ASSERT_KEEP_RANGE");
            dynamicAssert(completion.burstLast == pack(expectedLast),
                "DMA_ASSERT_BURST_LAST");
            dynamicAssert(recvBeatCount[i] < 4'd9, "DMA_ASSERT_TOO_MANY_BEATS");

            completionQ.deq;
            beatQ[i].enq(BufferedCompletion {
                completion: completion,
                validBytes: validBytes
            });
            recvBytes[i] <= receivedNext;
            recvBeatCount[i] <= recvBeatCount[i] + 1;
            if (expectedLast)
                recvComplete[i] <= True;
        endrule
    end

    // 只有 issue-order head 且已经完整接收的 slot 可以 drain。middle beat
    // 不依赖 retireQ；last beat 原子校验总长度、弹出issue order并回收slot。
    for (Integer i = 0; i < 8; i = i + 1) begin
        rule drainMiddleBeat (issueOrderQ.notEmpty && ctxValid[i] && recvComplete[i] &&
                              issueOrderQ.first[2:0] == fromInteger(i) &&
                              issueOrderQ.first == ctxTag[i] && beatQ[i].notEmpty &&
                              beatQ[i].first.completion.burstLast == 0 && orderedQ.notFull);
            let buffered = beatQ[i].first;
            let completion = buffered.completion;
            let burst = ctxBurst[i];
            Bit#(8) validBytes = buffered.validBytes;
            orderedQ.enq(DmaOrderedBeat {
                tag: ctxTag[i],
                wqeTag: burst.wqeTag,
                chunkSeq: burst.chunkSeq,
                messageOffset: burst.messageOffset + zeroExtend(drainBytes[i]),
                data: dmaMaskData(completion.data, completion.keep),
                keep: completion.keep,
                burstFirst: pack(drainBeatCount[i] == 0),
                burstLast: 0,
                isFirst: burst.isFirst,
                isLast: burst.isLast
            });
            beatQ[i].deq;
            drainBytes[i] <= drainBytes[i] + zeroExtend(validBytes);
            drainBeatCount[i] <= drainBeatCount[i] + 1;
        endrule

        rule drainLastBeat (issueOrderQ.notEmpty && ctxValid[i] && recvComplete[i] &&
                            issueOrderQ.first[2:0] == fromInteger(i) &&
                            issueOrderQ.first == ctxTag[i] && beatQ[i].notEmpty &&
                            beatQ[i].first.completion.burstLast == 1 && orderedQ.notFull &&
                            retireQ.notFull);
            let buffered = beatQ[i].first;
            let completion = buffered.completion;
            let burst = ctxBurst[i];
            Bit#(8) validBytes = buffered.validBytes;
            DmaBurstLength drainedNext = drainBytes[i] + zeroExtend(validBytes);
            Bit#(4) beatCountNext = drainBeatCount[i] + 1;
            dynamicAssert(drainedNext == burst.length, "DMA_ASSERT_DRAIN_LENGTH");
            dynamicAssert(beatCountNext == recvBeatCount[i],
                "DMA_ASSERT_DRAIN_BEAT_COUNT");

            orderedQ.enq(DmaOrderedBeat {
                tag: ctxTag[i],
                wqeTag: burst.wqeTag,
                chunkSeq: burst.chunkSeq,
                messageOffset: burst.messageOffset + zeroExtend(drainBytes[i]),
                data: dmaMaskData(completion.data, completion.keep),
                keep: completion.keep,
                burstFirst: pack(drainBeatCount[i] == 0),
                burstLast: 1,
                isFirst: burst.isFirst,
                isLast: burst.isLast
            });
            beatQ[i].deq;
            issueOrderQ.deq;
            retireQ.enq(DmaSlotRetire { tag: ctxTag[i] });
            ctxValid[i] <= False;
            recvBytes[i] <= 0;
            recvBeatCount[i] <= 0;
            recvComplete[i] <= False;
            drainBytes[i] <= 0;
            drainBeatCount[i] <= 0;
        endrule
    end

    interface scheduledIn = toPipeIn(scheduledQ);
    interface dmaReadReqOut = toPipeOut(dmaReqQ);
    interface dmaCompletionIn = toPipeIn(completionQ);
    interface orderedBeatOut = toPipeOut(orderedQ);
    interface retireOut = toPipeOut(retireQ);
endmodule

// ============================================================================
// Message Transfer Tracker：message 顺序检查和最终 commit
// ============================================================================

interface DmaMessageTrackerIfc;
    interface PipeIn#(DmaMessageRegistration) registrationIn;
    interface PipeIn#(DmaOrderedBeat) orderedBeatIn;
    interface PipeOut#(DmaPayloadBeat) payloadOut;
    interface PipeOut#(DmaMessageDone) messageDoneOut;
    interface PipeOut#(DmaMessageRetire) messageRetireOut;
endinterface

(* synthesize *)
module mkDmaMessageTransferTracker(DmaMessageTrackerIfc);
    FIFOF#(DmaMessageRegistration) registrationStageQ <- mkSizedFIFOF(2);
    FIFOF#(DmaMessageRegistration) registrationQ <- mkSizedFIFOF(8);
    FIFOF#(DmaOrderedBeat) orderedQ <- mkSizedFIFOF(2);

    // payload queue 使用显式 head/tail + DWire commit，允许 consumer deq 与
    // stageOrderedBeat enq 同拍提交，避免普通FIFOF method conflict造成半速。
    Reg#(TrackedPayloadBeat) payloadHead <- mkReg(unpack(0));
    Reg#(TrackedPayloadBeat) payloadTail <- mkReg(unpack(0));
    Reg#(Bool) payloadHeadValid <- mkReg(False);
    Reg#(Bool) payloadTailValid <- mkReg(False);
    Wire#(Maybe#(TrackedPayloadBeat)) payloadEnqReq <- mkDWire(tagged Invalid);
    Wire#(Bool) payloadDeqReq <- mkDWire(False);
    FIFOF#(DmaMessageDone) doneStageQ <- mkSizedFIFOF(1);
    FIFOF#(DmaMessageDone) doneQ <- mkSizedFIFOF(1);
    FIFOF#(DmaMessageRetire) retireQ <- mkSizedFIFOF(2);

    // stagedBytes 统计已进入public payload queue的byte；acceptedBytes只在
    // payloadOut.deq后推进，因此done严格晚于最后一拍真实handshake。
    Reg#(DmaMessageLength) stagedBytes <- mkReg(0);
    Reg#(DmaMessageLength) acceptedBytes <- mkReg(0);
    Reg#(DmaChunkSeq) expectedChunkSeq <- mkReg(0);
    Reg#(DmaChunkCount) chunkCount <- mkReg(0);
    Reg#(Bool) inBurst <- mkReg(False);
    Reg#(Bool) stageComplete <- mkReg(False);

    // 统一处理四种队列动作：idle、仅enq、仅deq、同拍enq+deq。
    // steady state通常保持一个head entry，使上下游可每拍各完成一次handshake。
    rule commitPayloadQueue;
        if (payloadDeqReq) begin
            case (payloadEnqReq) matches
                tagged Valid .value: begin
                    if (payloadTailValid) begin
                        payloadHead <= payloadTail;
                        payloadHeadValid <= True;
                        payloadTail <= value;
                        payloadTailValid <= True;
                    end else begin
                        payloadHead <= value;
                        payloadHeadValid <= True;
                        payloadTailValid <= False;
                    end
                end
                tagged Invalid: begin
                    payloadHead <= payloadTail;
                    payloadHeadValid <= payloadTailValid;
                    payloadTailValid <= False;
                end
            endcase
        end else begin
            case (payloadEnqReq) matches
                tagged Valid .value: begin
                    if (payloadHeadValid) begin
                        payloadTail <= value;
                        payloadTailValid <= True;
                    end else begin
                        payloadHead <= value;
                        payloadHeadValid <= True;
                    end
                end
            endcase
        end
    endrule

    // 隔离 public registration enq 与最后 payload commit 对 order FIFO 的操作。
    rule forwardRegistration (registrationStageQ.notEmpty &&
                              registrationQ.notFull && !payloadDeqReq);
        registrationQ.enq(registrationStageQ.first);
        registrationStageQ.deq;
    endrule

    // 将一个ordered beat变成public raw payload。messageLast只在最后chunk的
    // burstLast beat成立；messageOffset必须恰好等于此前staged byte总数。
    rule stageOrderedBeat (orderedQ.notEmpty && registrationQ.notEmpty &&
                           !payloadTailValid && !stageComplete);
        let ordered = orderedQ.first;
        let registration = registrationQ.first;
        Bit#(8) validBytes = dmaKeepCount(ordered.keep);
        Bit#(33) stagedNextWide = zeroExtend(stagedBytes) +
            zeroExtend(validBytes);
        DmaMessageLength stagedNext = truncate(stagedNextWide);
        Bool messageFirst = (stagedBytes == 0);
        Bool messageLast = (ordered.isLast == 1 && ordered.burstLast == 1);
        DmaChunkCount chunkCountNext = ordered.burstFirst == 1
            ? chunkCount + 1
            : chunkCount;

        dynamicAssert(stagedNextWide[32] == 0, "DMA_ASSERT_TRACKER_OVERFLOW");
        dynamicAssert(ordered.wqeTag == registration.wqeTag,
            "DMA_ASSERT_MESSAGE_ORDER");
        dynamicAssert(ordered.messageOffset == stagedBytes,
            "DMA_ASSERT_MESSAGE_OFFSET");
        dynamicAssert(ordered.keep != 0, "DMA_ASSERT_TRACKER_ZERO_KEEP");
        dynamicAssert(dmaKeepIsContiguous(ordered.keep),
            "DMA_ASSERT_TRACKER_KEEP");
        dynamicAssert(ordered.isFirst == pack(ordered.chunkSeq == 0),
            "DMA_ASSERT_FIRST_CHUNK_FLAG");

        if (ordered.burstFirst == 1) begin
            dynamicAssert(!inBurst, "DMA_ASSERT_NESTED_BURST");
            dynamicAssert(ordered.chunkSeq == expectedChunkSeq,
                "DMA_ASSERT_DMA_SEQUENCE");
        end else begin
            dynamicAssert(inBurst, "DMA_ASSERT_MISSING_BURST_FIRST");
            dynamicAssert(ordered.chunkSeq == expectedChunkSeq,
                "DMA_ASSERT_BURST_SEQUENCE_CHANGED");
        end

        dynamicAssert(!messageFirst ||
                (ordered.burstFirst == 1 && ordered.isFirst == 1),
            "DMA_ASSERT_MESSAGE_FIRST_BEAT");

        payloadEnqReq <= tagged Valid TrackedPayloadBeat {
            payload: DmaPayloadBeat {
                wqeTag: ordered.wqeTag,
                messageOffset: ordered.messageOffset,
                data: ordered.data,
                keep: ordered.keep,
                first: pack(messageFirst),
                last: pack(messageLast)
            },
            validBytes: validBytes,
            done: DmaMessageDone {
                wqeTag: ordered.wqeTag,
                length: stagedNext,
                chunkCount: chunkCountNext
            }
        };
        orderedQ.deq;
        stagedBytes <= stagedNext;
        chunkCount <= chunkCountNext;
        if (ordered.burstLast == 1) begin
            inBurst <= False;
            expectedChunkSeq <= ordered.chunkSeq + 1;
        end else begin
            inBurst <= True;
        end
        if (messageLast)
            stageComplete <= True;
    endrule

    // middle handshake只推进accepted byte accounting，不触碰message order。
    rule commitPayloadMiddle (payloadDeqReq && payloadHeadValid &&
                              payloadHead.payload.last == 0);
        let tracked = payloadHead;
        Bit#(33) acceptedNextWide = zeroExtend(acceptedBytes) +
            zeroExtend(tracked.validBytes);
        dynamicAssert(acceptedNextWide[32] == 0,
            "DMA_ASSERT_MIDDLE_ACCEPT_OVERFLOW");
        acceptedBytes <= truncate(acceptedNextWide);
    endrule

    // last handshake必须同时有done和retire存储空间；提交后才释放message ID、
    // 弹出registration order并复位message级状态。
    rule commitPayloadLast (payloadDeqReq && payloadHeadValid &&
                            payloadHead.payload.last == 1 &&
                            doneStageQ.notFull && retireQ.notFull &&
                            registrationQ.notEmpty && stageComplete);
        let tracked = payloadHead;
        let registration = registrationQ.first;
        Bit#(33) acceptedNextWide = zeroExtend(acceptedBytes) +
            zeroExtend(tracked.validBytes);
        DmaMessageLength acceptedNext = truncate(acceptedNextWide);
        dynamicAssert(acceptedNextWide[32] == 0,
            "DMA_ASSERT_DONE_ACCEPT_OVERFLOW");
        dynamicAssert(tracked.payload.wqeTag == registration.wqeTag,
            "DMA_ASSERT_DONE_MESSAGE_ORDER");
        dynamicAssert(acceptedNext == tracked.done.length,
            "DMA_ASSERT_DONE_BYTE_COUNT");
        dynamicAssert(stageComplete, "DMA_ASSERT_DONE_BEFORE_STAGED");

        doneStageQ.enq(tracked.done);
        retireQ.enq(DmaMessageRetire { wqeTag: tracked.payload.wqeTag });
        registrationQ.deq;
        stagedBytes <= 0;
        acceptedBytes <= 0;
        expectedChunkSeq <= 0;
        chunkCount <= 0;
        inBurst <= False;
        stageComplete <= False;
    endrule

    rule forwardDone (doneStageQ.notEmpty && doneQ.notFull && !payloadDeqReq);
        doneQ.enq(doneStageQ.first);
        doneStageQ.deq;
    endrule

    interface registrationIn = toPipeIn(registrationStageQ);
    interface orderedBeatIn = toPipeIn(orderedQ);

    interface PipeOut payloadOut;
        method DmaPayloadBeat first if (payloadHeadValid);
            return payloadHead.payload;
        endmethod
        method Action deq if (payloadHeadValid &&
            (payloadHead.payload.last == 0 ||
             (doneStageQ.notFull && retireQ.notFull)));
            payloadDeqReq <= True;
        endmethod
        method Bit#(1) notEmpty = pack(payloadHeadValid);
    endinterface

    interface messageDoneOut = toPipeOut(doneQ);
    interface messageRetireOut = toPipeOut(retireQ);
endmodule

// ============================================================================
// 顶层：chunk admission -> scheduler -> reorder -> tracker
// ============================================================================

interface DmaEngineIfc;
    interface PipeIn#(DmaChunkReq) chunkIn;
    interface PipeOut#(DmaReadRequest) dmaReadReqOut;
    interface PipeIn#(DmaCompletionBeat) dmaCompletionIn;
    interface PipeOut#(DmaPayloadBeat) payloadOut;
    interface PipeOut#(DmaMessageDone) messageDoneOut;
endinterface

(* synthesize *)
module mkDmaEngine(DmaEngineIfc);
    DmaChunkNormalizerIfc normalizer <- mkDmaChunkNormalizer;
    DmaOrderedSchedulerIfc scheduler <- mkOrderedDmaReadScheduler;
    DmaCompletionReorderIfc reorder <- mkDmaCompletionReorder;
    DmaMessageTrackerIfc tracker <- mkDmaMessageTransferTracker;

    // 主数据路径和两条反馈路径全部使用bounded Pipe连接；任一下游阻塞都会
    // 逐级反压到chunkIn。slot retire与message retire属于不同生命周期。
    mkConnection(normalizer.burstOut, scheduler.burstIn);
    mkConnection(normalizer.registrationOut, tracker.registrationIn);
    mkConnection(scheduler.scheduledOut, reorder.scheduledIn);
    mkConnection(reorder.retireOut, scheduler.retireIn);
    mkConnection(reorder.orderedBeatOut, tracker.orderedBeatIn);
    mkConnection(tracker.messageRetireOut, normalizer.messageRetireIn);

    interface chunkIn = normalizer.chunkIn;
    interface dmaReadReqOut = reorder.dmaReadReqOut;
    interface dmaCompletionIn = reorder.dmaCompletionIn;
    interface payloadOut = tracker.payloadOut;
    interface messageDoneOut = tracker.messageDoneOut;
endmodule

endpackage
