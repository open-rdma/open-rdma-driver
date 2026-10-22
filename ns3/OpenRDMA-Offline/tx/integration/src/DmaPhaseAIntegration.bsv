package DmaPhaseAIntegration;

import Assert :: *;
import Connectable :: *;
import FIFOF :: *;
import Vector :: *;

import CommonTypes :: *;
import DmaEngine :: *;
import DmaPayloadCompactor :: *;
import DmaTypes :: *;
import MrcTxScheduler :: *;
import MrcTxTypes :: *;
import PacketAssembly :: *;
import TxTypes :: *;

// 集成顶层把两条输入路径合并：DmaChunkReq进入DMA数据通路，完整
// MessageContext进入M2 packetization通路。两者在first chunk处按request ID
// 和总长度同步，随后连接DMA Engine、Compactor、新版M2和新版M3。
//
// DmaMessageDone、TxCommit和EthPacket分别代表DMA、packet buffer和frame接口
// 的不同提交点；本文件显式跟踪这些生命周期，避免过早复用request ID。

// Lifecycle record保存M3必须产生的packet数量；只有final packet commit后才
// 生成release，确保同request ID不会在旧message仍位于M2/M3时复用。
typedef struct {
    TxRequestId requestId;
    TxQpn       qpn;
    TxPsnCount  packetCount;
} TxMessageLifecycle deriving(Bits, Eq, FShow);

// Context 与 first chunk 在同一提交点分叉到 M2 和 DMA，并验证 message 总长。
interface ContextChunkSynchronizerIfc;
    interface PipeIn#(MessageContext) contextIn;
    interface PipeIn#(DmaChunkReq) chunkIn;
    interface PipeOut#(MessageContext) contextOut;
    interface PipeOut#(DmaChunkReq) chunkOut;
    interface PipeOut#(DmaMessageDone) expectedDoneOut;
    interface PipeOut#(TxMessageLifecycle) lifecycleOut;
    interface PipeIn#(TxRequestId) lifecycleReleaseIn;
endinterface

(* synthesize *)
module mkContextChunkSynchronizer(ContextChunkSynchronizerIfc);
    FIFOF#(MessageContext) contextQ <- mkSizedFIFOF(2);
    FIFOF#(DmaChunkReq) chunkQ <- mkSizedFIFOF(2);
    FIFOF#(MessageContext) contextOutQ <- mkSizedFIFOF(2);
    FIFOF#(DmaChunkReq) chunkOutQ <- mkSizedFIFOF(2);
    FIFOF#(DmaMessageDone) expectedDoneQ <- mkSizedFIFOF(8);
    FIFOF#(TxMessageLifecycle) lifecycleQ <- mkSizedFIFOF(8);
    FIFOF#(TxRequestId) releaseQ <- mkSizedFIFOF(2);

    // active-ID CAM覆盖从context/first-chunk admission到final TxCommit的完整
    // Phase-A生命周期，比DmaEngine自身的raw-payload生命周期更长。
    Vector#(8, Reg#(TxRequestId)) activeId <- replicateM(mkReg(0));
    Reg#(Bit#(8)) activeMask <- mkReg(0);
    Reg#(DmaSlot) activeCursor <- mkReg(0);

    // 输入chunk stream是message-major；这些寄存器描述当前正在同步的一条
    // message，并把32-bit chunk length累计值与25-bit MessageContext.len比较。
    Reg#(Bool) inMessage <- mkReg(False);
    Reg#(DmaWqeTag) currentTag <- mkReg(0);
    Reg#(DmaChunkSeq) expectedChunkSeq <- mkReg(0);
    Reg#(DmaMessageLength) expectedLength <- mkReg(0);
    Reg#(DmaMessageLength) runningLength <- mkReg(0);
    Reg#(DmaChunkCount) runningChunkCount <- mkReg(0);

    Maybe#(DmaSlot) selectedActiveSlot =
        dmaFindFreeSlot(~activeMask, activeCursor);

    // final TxCommit反馈在这里回收active-ID slot；release必须唯一命中，
    // 否则说明生命周期记录丢失、重复或次序错误。
    rule releaseLifecycle (releaseQ.notEmpty);
        let releaseEvent = releaseQ.first;
        Maybe#(DmaSlot) matched = tagged Invalid;
        Bit#(4) matchCount = 0;
        for (Integer i = 0; i < 8; i = i + 1) begin
            if (activeMask[i] == 1 && activeId[i] == releaseEvent) begin
                if (!isValid(matched))
                    matched = tagged Valid fromInteger(i);
                matchCount = matchCount + 1;
            end
        end
        dynamicAssert(matchCount == 1, "DMA_PHASEA_RELEASE_UNKNOWN_ID");
        DmaSlot slot = fromMaybe(0, matched);
        Bit#(8) nextMask = activeMask;
        nextMask[slot] = 0;
        activeMask <= nextMask;
        releaseQ.deq;
    endrule

    // 若contextQ head的ID仍active，保留context/chunk不动并自然反压上游，
    // 直到Commit Tracker返回release。
    Bool firstIdActive = False;
    for (Integer i = 0; i < 8; i = i + 1)
        if (contextQ.notEmpty && activeMask[i] == 1 &&
            activeId[i] == contextQ.first.requestId)
            firstIdActive = True;

    // first chunk必须与context在同一rule中同时进入DMA和M2路径；这样M2
    // 不可能在对应context尚未安装时看到该message的payload。
    rule startMessage (contextQ.notEmpty && chunkQ.notEmpty && !inMessage &&
                       chunkQ.first.isFirst && !firstIdActive &&
                       isValid(selectedActiveSlot) && contextOutQ.notFull &&
                       chunkOutQ.notFull && lifecycleQ.notFull &&
                       (!chunkQ.first.isLast || expectedDoneQ.notFull) &&
                       !releaseQ.notEmpty);
        let messageContext = contextQ.first;
        let chunk = chunkQ.first;
        DmaSlot slot = fromMaybe(0, selectedActiveSlot);
        Bit#(33) firstLengthWide = zeroExtend(chunk.len);
        DmaMessageLength totalLength = zeroExtend(messageContext.len);
        TxPsnCount packetCount = packetCountForContext(messageContext);

        dynamicAssert(messageContext.requestId == chunk.wqeTag,
            "DMA_PHASEA_CONTEXT_TAG_MISMATCH");
        dynamicAssert(messageContext.len != 0, "DMA_PHASEA_ZERO_CONTEXT_LENGTH");
        dynamicAssert(messageContext.pmtu <= 4, "DMA_PHASEA_INVALID_PMTU");
        dynamicAssert(messageContext.psnCount >= packetCount,
            "DMA_PHASEA_PSN_RANGE_TOO_SMALL");
        dynamicAssert(chunk.chunkSeq == 0, "DMA_PHASEA_FIRST_CHUNK_SEQUENCE");
        dynamicAssert(firstLengthWide <= zeroExtend(totalLength),
            "DMA_PHASEA_CHUNK_OVER_MESSAGE");
        if (chunk.isLast)
            dynamicAssert(firstLengthWide == zeroExtend(totalLength),
                "DMA_PHASEA_LAST_LENGTH_MISMATCH");
        else
            dynamicAssert(firstLengthWide < zeroExtend(totalLength),
                "DMA_PHASEA_MISSING_LAST_FLAG");

        contextOutQ.enq(messageContext);
        chunkOutQ.enq(chunk);
        lifecycleQ.enq(TxMessageLifecycle {
            requestId: messageContext.requestId,
            qpn: messageContext.qpn,
            packetCount: packetCount
        });
        contextQ.deq;
        chunkQ.deq;

        Bit#(8) nextMask = activeMask;
        nextMask[slot] = 1;
        activeMask <= nextMask;
        activeId[slot] <= messageContext.requestId;
        activeCursor <= slot + 1;

        if (chunk.isLast) begin
            expectedDoneQ.enq(DmaMessageDone {
                wqeTag: chunk.wqeTag,
                length: totalLength,
                chunkCount: 1
            });
            inMessage <= False;
            currentTag <= 0;
            expectedChunkSeq <= 0;
            expectedLength <= 0;
            runningLength <= 0;
            runningChunkCount <= 0;
        end else begin
            inMessage <= True;
            currentTag <= chunk.wqeTag;
            expectedChunkSeq <= 1;
            expectedLength <= totalLength;
            runningLength <= truncate(firstLengthWide);
            runningChunkCount <= 1;
        end
    endrule

    // continuation不重复发送context，只校验tag/seq和累计length并转发chunk；
    // last时生成一条expected done记录。
    rule continueMessage (chunkQ.notEmpty && inMessage &&
                          chunkOutQ.notFull &&
                          (!chunkQ.first.isLast || expectedDoneQ.notFull));
        let chunk = chunkQ.first;
        Bit#(33) nextLengthWide = zeroExtend(runningLength) +
            zeroExtend(chunk.len);
        DmaChunkCount nextChunkCount = runningChunkCount + 1;

        dynamicAssert(!chunk.isFirst, "DMA_PHASEA_DUPLICATE_CHUNK_FIRST");
        dynamicAssert(chunk.wqeTag == currentTag, "DMA_PHASEA_CHUNK_TAG_CHANGED");
        dynamicAssert(chunk.chunkSeq == expectedChunkSeq,
            "DMA_PHASEA_CHUNK_SEQUENCE");
        dynamicAssert(nextLengthWide <= zeroExtend(expectedLength),
            "DMA_PHASEA_CHUNK_OVER_MESSAGE");
        if (chunk.isLast)
            dynamicAssert(nextLengthWide == zeroExtend(expectedLength),
                "DMA_PHASEA_LAST_LENGTH_MISMATCH");
        else
            dynamicAssert(nextLengthWide < zeroExtend(expectedLength),
                "DMA_PHASEA_MISSING_LAST_FLAG");

        chunkOutQ.enq(chunk);
        chunkQ.deq;
        if (chunk.isLast) begin
            expectedDoneQ.enq(DmaMessageDone {
                wqeTag: chunk.wqeTag,
                length: expectedLength,
                chunkCount: nextChunkCount
            });
            inMessage <= False;
            currentTag <= 0;
            expectedChunkSeq <= 0;
            expectedLength <= 0;
            runningLength <= 0;
            runningChunkCount <= 0;
        end else begin
            expectedChunkSeq <= chunk.chunkSeq + 1;
            runningLength <= truncate(nextLengthWide);
            runningChunkCount <= nextChunkCount;
        end
    endrule

    interface contextIn = toPipeIn(contextQ);
    interface chunkIn = toPipeIn(chunkQ);
    interface contextOut = toPipeOut(contextOutQ);
    interface chunkOut = toPipeOut(chunkOutQ);
    interface expectedDoneOut = toPipeOut(expectedDoneQ);
    interface lifecycleOut = toPipeOut(lifecycleQ);
    interface lifecycleReleaseIn = toPipeIn(releaseQ);
endmodule

interface DmaDoneCheckerIfc;
    interface PipeIn#(DmaMessageDone) expectedIn;
    interface PipeIn#(DmaMessageDone) actualIn;
    interface PipeOut#(DmaMessageDone) checkedOut;
endinterface

(* synthesize *)
module mkDmaDoneChecker(DmaDoneCheckerIfc);
    // expected和actual都保持message order；不按ID查CAM，因而同一ID在合法
    // release后复用时仍按occurrence逐条比较。
    FIFOF#(DmaMessageDone) expectedQ <- mkSizedFIFOF(8);
    FIFOF#(DmaMessageDone) actualQ <- mkSizedFIFOF(2);
    FIFOF#(DmaMessageDone) checkedQ <- mkSizedFIFOF(2);

    rule checkDone (expectedQ.notEmpty && actualQ.notEmpty && checkedQ.notFull);
        let expected = expectedQ.first;
        let actual = actualQ.first;
        dynamicAssert(actual.wqeTag == expected.wqeTag,
            "DMA_PHASEA_DONE_TAG_MISMATCH");
        dynamicAssert(actual.length == expected.length,
            "DMA_PHASEA_DONE_LENGTH_MISMATCH");
        dynamicAssert(actual.chunkCount == expected.chunkCount,
            "DMA_PHASEA_DONE_CHUNK_COUNT_MISMATCH");
        expectedQ.deq;
        actualQ.deq;
        checkedQ.enq(actual);
    endrule

    interface expectedIn = toPipeIn(expectedQ);
    interface actualIn = toPipeIn(actualQ);
    interface checkedOut = toPipeOut(checkedQ);
endmodule

interface TxCommitTrackerIfc;
    interface PipeIn#(TxMessageLifecycle) lifecycleIn;
    interface PipeIn#(TxCommit) commitIn;
    interface PipeOut#(TxCommit) commitOut;
    interface PipeOut#(TxRequestId) releaseOut;
endinterface

(* synthesize *)
module mkTxCommitTracker(TxCommitTrackerIfc);
    // M3每个packet产生一次TxCommit。Tracker按message lifecycle head验证
    // packetIndex从0递增，并在最后一个packet后产生唯一release。
    FIFOF#(TxMessageLifecycle) lifecycleQ <- mkSizedFIFOF(8);
    FIFOF#(TxCommit) commitQ <- mkSizedFIFOF(2);
    FIFOF#(TxCommit) checkedQ <- mkSizedFIFOF(2);
    FIFOF#(TxRequestId) releaseQ <- mkSizedFIFOF(2);
    Reg#(TxPsnCount) expectedPacketIndex <- mkReg(0);

    rule checkCommit (lifecycleQ.notEmpty && commitQ.notEmpty &&
                      checkedQ.notFull &&
                      (commitQ.first.packetIndex + 1 !=
                           lifecycleQ.first.packetCount || releaseQ.notFull));
        let lifecycle = lifecycleQ.first;
        let commit = commitQ.first;
        Bool isLast = commit.packetIndex + 1 == lifecycle.packetCount;
        dynamicAssert(commit.requestId == lifecycle.requestId,
            "DMA_PHASEA_COMMIT_ID_MISMATCH");
        dynamicAssert(commit.qpn == lifecycle.qpn,
            "DMA_PHASEA_COMMIT_QPN_MISMATCH");
        dynamicAssert(commit.packetIndex == expectedPacketIndex,
            "DMA_PHASEA_COMMIT_PACKET_INDEX");
        dynamicAssert(commit.packetIndex < lifecycle.packetCount,
            "DMA_PHASEA_COMMIT_AFTER_LAST");

        commitQ.deq;
        checkedQ.enq(commit);
        if (isLast) begin
            lifecycleQ.deq;
            releaseQ.enq(lifecycle.requestId);
            expectedPacketIndex <= 0;
        end else begin
            expectedPacketIndex <= expectedPacketIndex + 1;
        end
    endrule

    interface lifecycleIn = toPipeIn(lifecycleQ);
    interface commitIn = toPipeIn(commitQ);
    interface commitOut = toPipeOut(checkedQ);
    interface releaseOut = toPipeOut(releaseQ);
endmodule

interface DmaPhaseAIntegrationIfc;
    interface PipeIn#(MessageContext) contextIn;
    interface PipeIn#(DmaChunkReq) chunkIn;
    interface PipeOut#(DmaReadRequest) dmaReadReqOut;
    interface PipeIn#(DmaCompletionBeat) dmaCompletionIn;
    interface PipeOut#(EthPacket) channel0Out;
    interface PipeOut#(EthPacket) channel1Out;
    interface PipeOut#(EthPacket) channel2Out;
    interface PipeOut#(EthPacket) channel3Out;
    interface PipeOut#(TxCommit) txCommitOut;
    interface PipeOut#(TxEgressEvent) txEgressOut;
    interface PipeOut#(DmaMessageDone) dmaMessageDoneOut;
    // MRC experimental-profile control plane. The legacy path leaves these
    // ports idle; the direct integration tests drive them.
    interface PipeIn#(MrcSourceProfileConfig) sourceRouteConfigIn;
    interface PipeIn#(MrcEvEntryConfig) evEntryConfigIn;
    interface PipeIn#(MrcEvPolicyConfig) evPolicyConfigIn;
    interface PipeIn#(MrcPortStatusUpdate) portStatusIn;
    interface PipeIn#(MrcPathFeedback) pathFeedbackIn;
    interface PipeOut#(MrcProbeRequest) probeOut;
    interface PipeOut#(MrcNoPathEvent) noPathOut;
    interface ServerP#(MrcTxCfgReq, CfgResp) configServer;
endinterface

(* synthesize *)
module mkDmaPhaseAIntegration(DmaPhaseAIntegrationIfc);
    ContextChunkSynchronizerIfc synchronizer <- mkContextChunkSynchronizer;
    DmaEngineIfc dma <- mkDmaEngine;
    DmaPayloadCompactorIfc compactor <- mkDmaPayloadCompactor;
    PacketAssemblyEngineIfc packetizer <- mkPacketAssemblyEngine;
    FourChannelTxSchedulerIfc scheduler <- mkFourChannelTxScheduler;
    DmaDoneCheckerIfc doneChecker <- mkDmaDoneChecker;
    TxCommitTrackerIfc commitTracker <- mkTxCommitTracker;

    // 主payload路径：physical chunks -> ordered raw payload -> canonical message
    // stream -> PMTU packet stream -> four-channel frame path。
    mkConnection(synchronizer.chunkOut, dma.chunkIn);
    mkConnection(synchronizer.contextOut, packetizer.contextIn);
    mkConnection(dma.payloadOut, compactor.payloadIn);
    mkConnection(compactor.payloadOut, packetizer.payloadIn);
    mkConnection(packetizer.descriptorOut, scheduler.firstDescriptorIn);
    mkConnection(packetizer.payloadOut, scheduler.firstPayloadIn);

    // 控制/完成路径：独立检查DMA done；逐packet检查TxCommit，并把final
    // commit转换成request-ID release反馈给Synchronizer。
    mkConnection(synchronizer.expectedDoneOut, doneChecker.expectedIn);
    mkConnection(dma.messageDoneOut, doneChecker.actualIn);
    mkConnection(synchronizer.lifecycleOut, commitTracker.lifecycleIn);
    mkConnection(scheduler.txCommitOut, commitTracker.commitIn);
    mkConnection(commitTracker.releaseOut, synchronizer.lifecycleReleaseIn);

    interface contextIn = synchronizer.contextIn;
    interface chunkIn = synchronizer.chunkIn;
    interface dmaReadReqOut = dma.dmaReadReqOut;
    interface dmaCompletionIn = dma.dmaCompletionIn;
    interface channel0Out = scheduler.channel0Out;
    interface channel1Out = scheduler.channel1Out;
    interface channel2Out = scheduler.channel2Out;
    interface channel3Out = scheduler.channel3Out;
    interface txCommitOut = commitTracker.commitOut;
    interface txEgressOut = scheduler.txEgressOut;
    interface dmaMessageDoneOut = doneChecker.checkedOut;
    interface sourceRouteConfigIn = scheduler.sourceRouteConfigIn;
    interface evEntryConfigIn = scheduler.evEntryConfigIn;
    interface evPolicyConfigIn = scheduler.evPolicyConfigIn;
    interface portStatusIn = scheduler.portStatusIn;
    interface pathFeedbackIn = scheduler.pathFeedbackIn;
    interface probeOut = scheduler.probeOut;
    interface noPathOut = scheduler.noPathOut;
    interface configServer = scheduler.configServer;
endmodule

endpackage
