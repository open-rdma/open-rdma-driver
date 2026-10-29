package RxRqBitmapManager;
import Vector::*;
import FIFOF::*;
import RegFile::*;
import CommonTypes::*;
import TxTypes::*;
import RxTypes::*;

typedef struct {
    TxPsn ackPsn;
    Bit#(16) receiveBitmap;
    Bit#(7) oooCount;
    Bool contextValid;
    TxPsn startPsn;
    TxPsn lastPsn;
    Bit#(64) remoteAddr;
    Bit#(32) rkey;
    TxLength messageBytes;
} RxRqState deriving(Bits, Eq, FShow);

function RxFeedback rxFeedbackSnapshot(RxQpConfig cfg, RxRqState state,
        TxPsn triggerPsn, Bool nak);
    return RxFeedback{qpn:cfg.qpn,
        kind:nak ? RX_NACK : (state.receiveBitmap != 0 ? RX_SACK : RX_ACK),
        ackPsn:state.ackPsn, sackBase:state.ackPsn + 1,
        sackBitmap:zeroExtend(state.receiveBitmap), oooCount:state.oooCount,
        triggerPsn:triggerPsn};
endfunction

interface RxRqBitmapManagerIfc;
    method Action configure(RxQpIndex index, RxQpConfig cfg);
    method Action start;
    method RxReceiveProgress progress(RxQpIndex index);
    method RxGlobalSeq nextGlobalSeq;
    method Bool faulted;
    interface Vector#(4, PipeIn#(RxParsedPacket)) packets;
    interface Vector#(4, PipeOut#(RxAdmittedPacket)) admitted;
    interface Vector#(4, PipeOut#(RxBufferRelease)) releases;
    interface PipeOut#(RxFeedback) feedbackOut;
    interface PipeOut#(RxError) errorOut;
endinterface

module mkRxRqBitmapManager(RxRqBitmapManagerIfc);
    Reg#(Bool) running <- mkReg(False);
    Reg#(Bool) failed <- mkReg(False);
    Vector#(4, Reg#(RxQpConfig)) cfgs <- replicateM(mkReg(unpack(0)));
    Vector#(4, Reg#(RxRqState)) states <- replicateM(mkReg(unpack(0)));
    Vector#(4, Reg#(Bit#(16))) pendingValid <- replicateM(mkReg(0));
    RegFile#(Bit#(6), RxParsedPacket) pendingPackets <- mkRegFileFull;
    RegFile#(Bit#(6), Bit#(8)) pendingAge <- mkRegFileFull;
    Vector#(4, FIFOF#(RxParsedPacket)) inputQ <- replicateM(mkSizedFIFOF(4));
    FIFOF#(RxParsedPacket) ingressQ <- mkSizedFIFOF(8);
    FIFOF#(RxAdmittedPacket) admittedRouteQ <- mkSizedFIFOF(32);
    FIFOF#(RxBufferRelease) releaseRouteQ <- mkSizedFIFOF(32);
    Vector#(4, FIFOF#(RxAdmittedPacket)) admittedQ <- replicateM(mkSizedFIFOF(4));
    Vector#(4, FIFOF#(RxBufferRelease)) releaseQ <- replicateM(mkSizedFIFOF(8));
    FIFOF#(RxFeedback) feedbackQ <- mkSizedFIFOF(32);
    FIFOF#(RxError) errorQ <- mkSizedFIFOF(16);
    Reg#(RxGlobalSeq) globalSeq <- mkReg(0);
    Reg#(RxLaneId) ingressCursor <- mkReg(0);
    Reg#(RxQpIndex) pendingScanQp <- mkReg(0);
    Reg#(RxWindowIndex) pendingScanIndex <- mkReg(0);
    Reg#(Bool) resolvePhase <- mkReg(False);
    Bool pendingExists = pendingValid[0] != 0 || pendingValid[1] != 0
        || pendingValid[2] != 0 || pendingValid[3] != 0;

    function Bit#(4) ingressReadyMask();
        Bit#(4) ready = 0;
        for (Integer i = 0; i < 4; i = i + 1)
            ready[i] = pack(inputQ[i].notEmpty);
        return ready;
    endfunction
    let selectedIngressLane = rxPickReadyLane(ingressReadyMask(), ingressCursor);
    for (Integer i = 0; i < 4; i = i + 1) begin
        rule mergeIngressLane(!resolvePhase && isValid(selectedIngressLane)
                && fromMaybe(0, selectedIngressLane) == fromInteger(i));
            ingressQ.enq(inputQ[i].first);
            inputQ[i].deq;
            ingressCursor <= fromInteger((i + 1) % 4);
        endrule
    end

    rule resolvePending(running && !failed && resolvePhase && pendingExists);
        let scanState = states[pendingScanQp];
        RxWindowIndex scanIndex = pendingScanIndex;
        let pendingPacket = pendingPackets.sub({pendingScanQp, scanIndex});
        let age = pendingAge.sub({pendingScanQp, scanIndex});
        TxPsn packetIndex = pendingPacket.psn - scanState.startPsn;
        Bool belongs = scanState.contextValid && packetIndex[23] == 0
            && rxPsnAtOrAfter(scanState.lastPsn, pendingPacket.psn);
        Bool starts = !scanState.contextValid && pendingPacket.first
            && pendingPacket.psn == scanState.ackPsn + 1;
        Bool validPending = pendingValid[pendingScanQp][scanIndex] == 1;
        if (validPending && (belongs || starts)) begin
            Bit#(16) bits = pendingValid[pendingScanQp];
            bits[scanIndex] = 0;
            pendingValid[pendingScanQp] <= bits;
            ingressQ.enq(pendingPacket);
        end
        else if (validPending && age == 15) begin
            Bit#(16) bits = pendingValid[pendingScanQp];
            bits[scanIndex] = 0;
            pendingValid[pendingScanQp] <= bits;
            releaseRouteQ.enq(RxBufferRelease{tag:pendingPacket.payloadTag});
            feedbackQ.enq(rxFeedbackSnapshot(cfgs[pendingScanQp], scanState,
                pendingPacket.psn, True));
            errorQ.enq(RxError{qpn:pendingPacket.qpn, psn:pendingPacket.psn,
                code:RX_PENDING_TIMEOUT});
        end
        else if (validPending)
            pendingAge.upd({pendingScanQp, scanIndex}, age + 1);
        pendingScanIndex <= pendingScanIndex + 1;
        if (pendingScanIndex == 15)
            pendingScanQp <= pendingScanQp + 1;
        resolvePhase <= False;
    endrule

    rule advanceIdleToResolve(running && !failed && !resolvePhase
            && pendingExists && !ingressQ.notEmpty);
        resolvePhase <= True;
    endrule

    rule processCandidate(running && !failed && !resolvePhase
            && ingressQ.notEmpty);
        let packet = ingressQ.first;
        ingressQ.deq;
        Bit#(3) matchCount = 0;
        RxQpIndex qp = 0;
        for (Integer i = 0; i < 4; i = i + 1) begin
            if (cfgs[i].valid && cfgs[i].qpn == packet.qpn) begin
                matchCount = matchCount + 1;
                qp = fromInteger(i);
            end
        end

            if (matchCount != 1) begin
                releaseRouteQ.enq(RxBufferRelease{tag:packet.payloadTag});
                errorQ.enq(RxError{qpn:packet.qpn, psn:packet.psn, code:RX_UNKNOWN_QP});
                failed <= True;
            end
            else begin
                let cfg = cfgs[qp];
                let state = states[qp];
                TxPsn nextPsn = state.ackPsn + 1;
                TxPsn windowOffset = packet.psn - nextPsn;
                Bool oldPacket = rxPsnBefore(packet.psn, nextPsn);
                Bool inWindow = rxPsnInWindow(state.ackPsn, packet.psn);
                RxWindowIndex ringIndex = truncate(packet.psn);
                RxWindowIndex bitmapIndex = truncate(windowOffset);
                Bool committedDuplicate = inWindow
                    && state.receiveBitmap[bitmapIndex] == 1;
                Bool pendingDuplicate = inWindow && pendingValid[qp][ringIndex] == 1;
                let existingPending = pendingPackets.sub({qp, ringIndex});
                Bool replacePending = pendingDuplicate && packet.first;

                if (replacePending) begin
                    releaseRouteQ.enq(RxBufferRelease{
                        tag:existingPending.payloadTag});
                    pendingPackets.upd({qp, ringIndex}, packet);
                    pendingAge.upd({qp, ringIndex}, 0);
                end
                else if (oldPacket || committedDuplicate || pendingDuplicate) begin
                    releaseRouteQ.enq(RxBufferRelease{tag:packet.payloadTag});
                    feedbackQ.enq(rxFeedbackSnapshot(cfg, state, packet.psn, False));
                end
                else if (!inWindow) begin
                    releaseRouteQ.enq(RxBufferRelease{tag:packet.payloadTag});
                    feedbackQ.enq(rxFeedbackSnapshot(cfg, state, packet.psn, True));
                    errorQ.enq(RxError{qpn:packet.qpn, psn:packet.psn,
                        code:RX_OUT_OF_WINDOW});
                end
                else begin
                    Bool establishing = !state.contextValid && packet.first
                        && packet.psn == nextPsn;
                    TxPsn contextStart = establishing ? packet.psn : state.startPsn;
                    Bit#(64) contextAddr = establishing ? packet.remoteAddr : state.remoteAddr;
                    Bit#(32) contextRkey = establishing ? packet.rkey : state.rkey;
                    TxLength contextBytes = establishing ? packet.messageBytes : state.messageBytes;
                    TxPsn contextLast = state.lastPsn;
                    if (establishing) begin
                        TxPsnCount packetCount = packetCountForMessage(contextBytes,
                            cfg.pmtu);
                        contextLast = contextStart + zeroExtend(packetCount) - 1;
                    end
                    TxPsn packetIndex = packet.psn - contextStart;
                    Bool belongs = (state.contextValid || establishing)
                        && packetIndex[23] == 0
                        && rxPsnAtOrAfter(contextLast, packet.psn);

                    if (!belongs) begin
                        pendingPackets.upd({qp, ringIndex}, packet);
                        pendingAge.upd({qp, ringIndex}, 0);
                        Bit#(16) bits = pendingValid[qp];
                        bits[ringIndex] = 1;
                        pendingValid[qp] <= bits;
                    end
                    else begin
                        Bit#(38) messageOffset = packetIndex == 0 ? 0
                            : zeroExtend(firstPayloadLimit(cfg.pmtu))
                                + (zeroExtend(packetIndex) - 1)
                                    * zeroExtend(contPayloadLimit(cfg.pmtu));
                        Bit#(38) packetLimit = zeroExtend(packetIndex == 0
                            ? firstPayloadLimit(cfg.pmtu) : contPayloadLimit(cfg.pmtu));
                        Bit#(38) messageWide = zeroExtend(contextBytes);
                        Bit#(38) remaining = messageOffset < messageWide
                            ? messageWide - messageOffset : 0;
                        Bit#(38) expectedBytes = remaining < packetLimit
                            ? remaining : packetLimit;
                        Bool expectedLast = messageOffset + expectedBytes == messageWide;
                        Bit#(65) regionEnd = zeroExtend(cfg.virtualBase)
                            + zeroExtend(cfg.regionBytes);
                        Bit#(65) messageEnd = zeroExtend(contextAddr)
                            + zeroExtend(contextBytes);
                        Bit#(65) mrOffset = zeroExtend(contextAddr)
                            - zeroExtend(cfg.virtualBase);
                        Bit#(66) physicalMessageStart = zeroExtend(cfg.physicalBase)
                            + zeroExtend(mrOffset);
                        Bit#(66) physicalMessageEnd = physicalMessageStart
                            + zeroExtend(contextBytes);
                        Bit#(66) physical = physicalMessageStart
                            + zeroExtend(messageOffset);
                        Bool access = cfg.pmtu <= 4 && cfg.remoteWrite
                            && cfg.rkey == contextRkey && contextBytes != 0
                            && contextAddr >= cfg.virtualBase
                            && messageEnd <= regionEnd
                            && messageEnd <= 65'h10000000000000000
                            && physicalMessageEnd
                                <= 66'h10000000000000000;
                        Bool segment = packet.first == (packetIndex == 0)
                            && packet.last == expectedLast
                            && (!packet.first || (packet.remoteAddr == contextAddr
                                && packet.rkey == contextRkey
                                && packet.messageBytes == contextBytes))
                            && expectedBytes != 0
                            && zeroExtend(packet.payloadBytes) == expectedBytes;

                        if (!access || !segment) begin
                            releaseRouteQ.enq(RxBufferRelease{tag:packet.payloadTag});
                            errorQ.enq(RxError{qpn:packet.qpn, psn:packet.psn,
                                code:access ? RX_BAD_SEGMENT : RX_ACCESS_DENIED});
                            failed <= True;
                        end
                        else begin
                            RxRqState nextState = state;
                            if (establishing) begin
                                nextState.contextValid = True;
                                nextState.startPsn = contextStart;
                                nextState.lastPsn = contextLast;
                                nextState.remoteAddr = contextAddr;
                                nextState.rkey = contextRkey;
                                nextState.messageBytes = contextBytes;
                            end
                            Bit#(16) merged = state.receiveBitmap;
                            merged[bitmapIndex] = 1;
                            TxPsn mergedAck = state.ackPsn;
                            for (Integer i = 0; i < 16; i = i + 1) begin
                                if (merged[0] == 1) begin
                                    merged = merged >> 1;
                                    mergedAck = mergedAck + 1;
                                end
                            end
                            nextState.ackPsn = mergedAck;
                            nextState.receiveBitmap = merged;
                            Bit#(5) mergedCount = pack(countOnes(merged));
                            nextState.oooCount = zeroExtend(mergedCount);
                            Bool ackValid = mergedAck != state.ackPsn;
                            Bool messageComplete = ackValid
                                && rxPsnAtOrAfter(mergedAck, contextLast);
                            if (messageComplete)
                                nextState.contextValid = False;
                            states[qp] <= nextState;

                            let admittedPacket = RxAdmittedPacket{
                                globalSeq:globalSeq, payloadTag:packet.payloadTag,
                                lane:packet.lane, qp:qp, qpn:packet.qpn, psn:packet.psn,
                                dmaAddr:truncate(physical), payloadStart:packet.payloadStart,
                                payloadBytes:packet.payloadBytes, messageBytes:contextBytes,
                                first:packet.first, last:packet.last, ackValid:ackValid,
                                ackPsnAfterMerge:mergedAck,
                                messageComplete:messageComplete,
                                messageLastPsn:contextLast};
                            admittedRouteQ.enq(admittedPacket);
                            feedbackQ.enq(rxFeedbackSnapshot(cfg, nextState,
                                packet.psn, False));
                            globalSeq <= globalSeq + 1;
                        end
                    end
                end
            end
        resolvePhase <= pendingExists;
    endrule

    for (Integer i = 0; i < 4; i = i + 1) begin
        rule routeAdmitted(admittedRouteQ.notEmpty
                && admittedRouteQ.first.lane == fromInteger(i));
            admittedQ[i].enq(admittedRouteQ.first);
            admittedRouteQ.deq;
        endrule
        rule routeRelease(releaseRouteQ.notEmpty
                && releaseRouteQ.first.tag.lane == fromInteger(i));
            releaseQ[i].enq(releaseRouteQ.first);
            releaseRouteQ.deq;
        endrule
    end

    Vector#(4, PipeIn#(RxParsedPacket)) packetInputs;
    Vector#(4, PipeOut#(RxAdmittedPacket)) admittedOutputs;
    Vector#(4, PipeOut#(RxBufferRelease)) releaseOutputs;
    for (Integer i = 0; i < 4; i = i + 1) begin
        packetInputs[i] = interface PipeIn;
            method Action enq(RxParsedPacket packet)
                    if (running && !failed && inputQ[i].notFull);
                inputQ[i].enq(packet);
            endmethod
            method Bit#(1) notFull = pack(running && !failed
                && inputQ[i].notFull);
        endinterface;
        admittedOutputs[i] = toPipeOut(admittedQ[i]);
        releaseOutputs[i] = toPipeOut(releaseQ[i]);
    end

    method Action configure(RxQpIndex index, RxQpConfig cfg) if (!running);
        cfgs[index] <= cfg;
        RxRqState stateInit = unpack(0);
        stateInit.ackPsn = cfg.initialPsn - 1;
        states[index] <= stateInit;
        pendingValid[index] <= 0;
    endmethod
    method Action start if (!running);
        running <= True;
    endmethod
    method RxReceiveProgress progress(RxQpIndex index);
        let state = states[index];
        return RxReceiveProgress{ackPsn:state.ackPsn,
            receiveBitmap:state.receiveBitmap, oooCount:state.oooCount};
    endmethod
    method RxGlobalSeq nextGlobalSeq = globalSeq;
    method Bool faulted = failed;
    interface packets = packetInputs;
    interface admitted = admittedOutputs;
    interface releases = releaseOutputs;
    interface feedbackOut = toPipeOut(feedbackQ);
    interface errorOut = toPipeOut(errorQ);
endmodule
endpackage
