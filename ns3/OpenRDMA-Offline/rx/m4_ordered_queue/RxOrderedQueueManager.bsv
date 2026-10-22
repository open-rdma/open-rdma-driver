package RxOrderedQueueManager;
import Vector::*;
import FIFOF::*;
import RWire::*;
import CommonTypes::*;
import TxTypes::*;
import RxTypes::*;

typedef struct {
    RxOrderedSeq orderedSeq;
    TxQpn qpn;
    TxPsn barrierPsn;
    Bit#(64) dmaAddr;
    Bit#(32) value;
} RxOrderedEntry deriving(Bits, Eq, FShow);

function Bool rxOrderedFlagAccess(RxQpConfig cfg, RxOrderedFlagIngress flag);
    Bit#(65) offset = zeroExtend(flag.flagAddr) - zeroExtend(cfg.virtualBase);
    Bit#(65) endOffset = offset + 4;
    Bit#(66) physical = zeroExtend(cfg.physicalBase) + zeroExtend(offset);
    return cfg.remoteWrite && cfg.rkey == flag.rkey
        && flag.flagAddr[1:0] == 0 && flag.flagAddr >= cfg.virtualBase
        && endOffset <= zeroExtend(cfg.regionBytes)
        && physical + 4 <= 66'h10000000000000000;
endfunction

function Bit#(64) rxOrderedFlagPhysical(RxQpConfig cfg,
        RxOrderedFlagIngress flag);
    return cfg.physicalBase + (flag.flagAddr - cfg.virtualBase);
endfunction

interface RxOrderedQueueManagerIfc;
    method Action configureEpoch(RxEpoch value);
    method Action configure(RxQpIndex index, RxQpConfig cfg);
    method Action start;
    method TxPsn dmaAckPsn(RxQpIndex index);
    method Bool faulted;
    interface PipeIn#(RxDmaRetire) retireIn;
    interface PipeIn#(RxOrderedFlagIngress) orderedFlagIn;
    interface PipeOut#(RxFlagDmaRequest) flagRequestOut;
    interface PipeIn#(RxFlagDmaResponse) flagResponseIn;
    interface PipeOut#(RxOrderedCredit) creditOut;
    interface PipeOut#(RxCompletion) completionOut;
    interface PipeOut#(RxError) errorOut;
endinterface

module mkRxOrderedQueueManager(RxOrderedQueueManagerIfc);
    Reg#(Bool) running <- mkReg(False);
    Reg#(Bool) failed <- mkReg(False);
    Reg#(Bool) epochConfigured <- mkReg(False);
    Reg#(RxEpoch) currentEpoch <- mkReg(0);
    Vector#(4, Reg#(RxQpConfig)) cfgs <- replicateM(mkReg(unpack(0)));
    Vector#(4, Reg#(TxPsn)) dmaAck <- replicateM(mkReg(0));
    Vector#(4, Reg#(Bit#(64))) grantLimit <- replicateM(mkReg(4));
    Vector#(4, Reg#(Bool)) lastBarrierValid <- replicateM(mkReg(False));
    Vector#(4, Reg#(TxPsn)) lastBarrier <- replicateM(mkReg(0));
    Vector#(4, FIFOF#(RxOrderedEntry)) orderedQ <- replicateM(mkSizedFIFOF(4));
    FIFOF#(RxDmaRetire) retireQ <- mkSizedFIFOF(8);
    FIFOF#(RxOrderedFlagIngress) ingressQ <- mkSizedFIFOF(8);
    FIFOF#(RxFlagDmaResponse) flagResponseQ <- mkSizedFIFOF(4);
    FIFOF#(RxOrderedCredit) creditQ <- mkSizedFIFOF(8);
    FIFOF#(RxCompletion) completionQ <- mkSizedFIFOF(8);
    FIFOF#(RxError) errorQ <- mkSizedFIFOF(16);

    Reg#(RxGlobalSeq) expectedRetire <- mkReg(0);
    Reg#(RxOrderedSeq) nextOrderedSeq <- mkReg(0);
    Reg#(RxQpIndex) selectionCursor <- mkReg(0);
    Reg#(Bool) selectedValid <- mkReg(False);
    Reg#(RxQpIndex) selectedQp <- mkReg(0);
    Reg#(RxOrderedEntry) selectedEntry <- mkReg(unpack(0));
    Reg#(Bool) flagOutstanding <- mkReg(False);
    Reg#(RxOrderedSeq) expectedFlagResponse <- mkReg(0);
    RWire#(RxQpIndex) dequeueQp <- mkRWire;

    for (Integer i = 0; i < 4; i = i + 1) begin
        rule dequeueAcceptedFlag(dequeueQp.wget matches tagged Valid .qp
                &&& qp == fromInteger(i));
            orderedQ[i].deq;
        endrule
    end

    rule processRetire(running && !failed);
        let retired = retireQ.first;
        retireQ.deq;
        if (!retired.success || retired.globalSeq != expectedRetire) begin
            errorQ.enq(RxError{qpn:retired.qpn, psn:retired.psn,
                code:RX_ORDERED_BAD_RETIRE});
            failed <= True;
        end
        else begin
            expectedRetire <= expectedRetire + 1;
            if (retired.ackValid)
                dmaAck[retired.qp] <= retired.ackPsnAfterMerge;
            if (retired.messageComplete)
                completionQ.enq(RxCompletion{qpn:retired.qpn,
                    lastPsn:retired.messageLastPsn,
                    bytes:retired.messageBytes, success:True});
        end
    endrule

    let nextFlag = ingressQ.first;
    Bool nextFlagMatch0 = cfgs[0].valid && cfgs[0].qpn == nextFlag.qpn;
    Bool nextFlagMatch1 = cfgs[1].valid && cfgs[1].qpn == nextFlag.qpn;
    Bool nextFlagMatch2 = cfgs[2].valid && cfgs[2].qpn == nextFlag.qpn;
    Bool nextFlagMatch3 = cfgs[3].valid && cfgs[3].qpn == nextFlag.qpn;
    Bit#(3) nextFlagMatchCount = zeroExtend(pack(nextFlagMatch0))
        + zeroExtend(pack(nextFlagMatch1)) + zeroExtend(pack(nextFlagMatch2))
        + zeroExtend(pack(nextFlagMatch3));
    Bool nextFlagOrder0 = !lastBarrierValid[0]
        || rxPsnAtOrAfter(nextFlag.barrierPsn, lastBarrier[0]);
    Bool nextFlagOrder1 = !lastBarrierValid[1]
        || rxPsnAtOrAfter(nextFlag.barrierPsn, lastBarrier[1]);
    Bool nextFlagOrder2 = !lastBarrierValid[2]
        || rxPsnAtOrAfter(nextFlag.barrierPsn, lastBarrier[2]);
    Bool nextFlagOrder3 = !lastBarrierValid[3]
        || rxPsnAtOrAfter(nextFlag.barrierPsn, lastBarrier[3]);
    Bit#(4) nextFlagOrderMask = {pack(nextFlagOrder3), pack(nextFlagOrder2),
        pack(nextFlagOrder1), pack(nextFlagOrder0)};
    Bool nextFlagHasAccess = (nextFlagMatch0 && nextFlagOrder0
            && rxOrderedFlagAccess(cfgs[0], nextFlag))
        || (nextFlagMatch1 && nextFlagOrder1
            && rxOrderedFlagAccess(cfgs[1], nextFlag))
        || (nextFlagMatch2 && nextFlagOrder2
            && rxOrderedFlagAccess(cfgs[2], nextFlag))
        || (nextFlagMatch3 && nextFlagOrder3
            && rxOrderedFlagAccess(cfgs[3], nextFlag));

    rule rejectInvalidFlag(running && !failed && ingressQ.notEmpty
            && (nextFlagMatchCount != 1 || !nextFlagHasAccess));
        ingressQ.deq;
        errorQ.enq(RxError{qpn:nextFlag.qpn, psn:nextFlag.barrierPsn,
            code:RX_ORDERED_BAD_FLAG});
        failed <= True;
    endrule

    for (Integer i = 0; i < 4; i = i + 1) begin
        rule admitOrderedFlag(running && !failed && ingressQ.notEmpty
                && nextFlagMatchCount == 1 && cfgs[i].valid
                && cfgs[i].qpn == nextFlag.qpn
                && nextFlagOrderMask[i] == 1
                && rxOrderedFlagAccess(cfgs[i], nextFlag)
                && orderedQ[i].notFull);
            ingressQ.deq;
            orderedQ[i].enq(RxOrderedEntry{orderedSeq:nextOrderedSeq,
                qpn:nextFlag.qpn, barrierPsn:nextFlag.barrierPsn,
                dmaAddr:rxOrderedFlagPhysical(cfgs[i], nextFlag),
                value:nextFlag.value});
            lastBarrier[i] <= nextFlag.barrierPsn;
            lastBarrierValid[i] <= True;
            nextOrderedSeq <= nextOrderedSeq + 1;
        endrule
        rule rejectOrderedOverflow(running && !failed && ingressQ.notEmpty
                && nextFlagMatchCount == 1 && cfgs[i].valid
                && cfgs[i].qpn == nextFlag.qpn
                && nextFlagOrderMask[i] == 1
                && rxOrderedFlagAccess(cfgs[i], nextFlag)
                && !orderedQ[i].notFull);
            ingressQ.deq;
            errorQ.enq(RxError{qpn:nextFlag.qpn, psn:nextFlag.barrierPsn,
                code:RX_ORDERED_QUEUE_FULL});
            failed <= True;
        endrule
    end

    for (Integer i = 0; i < 4; i = i + 1) begin
        rule selectEligibleFlag(running && !failed && !selectedValid
                && !flagOutstanding && selectionCursor == fromInteger(i)
                && orderedQ[i].notEmpty
                && rxPsnAtOrAfter(dmaAck[i], orderedQ[i].first.barrierPsn));
            selectedEntry <= orderedQ[i].first;
            selectedQp <= fromInteger(i);
            selectedValid <= True;
            selectionCursor <= fromInteger((i + 1) % 4);
        endrule
        rule skipEmptyQp(running && !failed && !selectedValid
                && !flagOutstanding && selectionCursor == fromInteger(i)
                && !orderedQ[i].notEmpty);
            selectionCursor <= fromInteger((i + 1) % 4);
        endrule
        rule skipBlockedQp(running && !failed && !selectedValid
                && !flagOutstanding && selectionCursor == fromInteger(i)
                && orderedQ[i].notEmpty
                && !rxPsnAtOrAfter(dmaAck[i], orderedQ[i].first.barrierPsn));
            selectionCursor <= fromInteger((i + 1) % 4);
        endrule
    end

    rule consumeFlagResponse(running && !failed && flagOutstanding);
        let response = flagResponseQ.first;
        flagResponseQ.deq;
        if (response.orderedSeq != expectedFlagResponse
                || response.epoch != currentEpoch || !response.success) begin
            errorQ.enq(RxError{qpn:selectedEntry.qpn,
                psn:selectedEntry.barrierPsn,
                code:response.success ? RX_FLAG_DMA_BAD_RESPONSE : RX_FLAG_DMA_FAILED});
            failed <= True;
        end
        else begin
            flagOutstanding <= False;
        end
    endrule

    rule rejectStaleFlagResponse(running && !failed && !flagOutstanding);
        let response = flagResponseQ.first;
        flagResponseQ.deq;
        errorQ.enq(RxError{qpn:0, psn:0, code:RX_FLAG_DMA_BAD_RESPONSE});
        failed <= True;
    endrule

    method Action configureEpoch(RxEpoch value) if (!running);
        currentEpoch <= value;
        epochConfigured <= True;
    endmethod
    method Action configure(RxQpIndex index, RxQpConfig cfg) if (!running);
        cfgs[index] <= cfg;
        dmaAck[index] <= cfg.initialPsn - 1;
        grantLimit[index] <= 4;
        lastBarrierValid[index] <= False;
        lastBarrier[index] <= 0;
    endmethod
    method Action start if (!running && epochConfigured);
        running <= True;
    endmethod
    method TxPsn dmaAckPsn(RxQpIndex index) = dmaAck[index];
    method Bool faulted = failed;
    interface retireIn = interface PipeIn;
        method Action enq(RxDmaRetire value) if (running && !failed && retireQ.notFull);
            retireQ.enq(value);
        endmethod
        method Bit#(1) notFull = pack(running && !failed && retireQ.notFull);
    endinterface;
    interface orderedFlagIn = interface PipeIn;
        method Action enq(RxOrderedFlagIngress value)
                if (running && !failed && ingressQ.notFull);
            ingressQ.enq(value);
        endmethod
        method Bit#(1) notFull = pack(running && !failed && ingressQ.notFull);
    endinterface;
    interface flagRequestOut = interface PipeOut;
        method RxFlagDmaRequest first if (running && !failed && selectedValid
                && creditQ.notFull);
            return RxFlagDmaRequest{orderedSeq:selectedEntry.orderedSeq,
                epoch:currentEpoch, qpn:selectedEntry.qpn, barrierPsn:selectedEntry.barrierPsn,
                addr:selectedEntry.dmaAddr, value:selectedEntry.value};
        endmethod
        method Action deq if (running && !failed && selectedValid
                && creditQ.notFull);
            dequeueQp.wset(selectedQp);
            let nextGrant = grantLimit[selectedQp] + 1;
            grantLimit[selectedQp] <= nextGrant;
            creditQ.enq(RxOrderedCredit{qpn:selectedEntry.qpn,
                orderedGrantLimit:nextGrant});
            expectedFlagResponse <= selectedEntry.orderedSeq;
            flagOutstanding <= True;
            selectedValid <= False;
        endmethod
        method Bit#(1) notEmpty = pack(running && !failed && selectedValid
            && creditQ.notFull);
    endinterface;
    interface flagResponseIn = toPipeIn(flagResponseQ);
    interface creditOut = toPipeOut(creditQ);
    interface completionOut = toPipeOut(completionQ);
    interface errorOut = toPipeOut(errorQ);
endmodule
endpackage
