package RxTop;
import Vector::*;
import FIFOF::*;
import Connectable::*;
import CommonTypes::*;
import TxTypes::*;
import RxTypes::*;
import RxPacketBuffer::*;
import RxPacketParser::*;
import RxRqBitmapManager::*;
import RxDmaPlacement::*;
import RxOrderedQueueManager::*;

function Maybe#(Bit#(2)) rxPickReleaseSource(Bit#(3) ready, Bit#(2) cursor);
    Maybe#(Bit#(2)) result = tagged Invalid;
    for (Integer offset = 0; offset < 3; offset = offset + 1) begin
        Bit#(3) wide = zeroExtend(cursor) + fromInteger(offset);
        if (wide >= 3) wide = wide - 3;
        Bit#(2) source = truncate(wide);
        if (!isValid(result) && ready[source] == 1)
            result = tagged Valid source;
    end
    return result;
endfunction

function Maybe#(Bit#(4)) rxPickErrorSource(Bit#(11) ready, Bit#(4) cursor);
    Maybe#(Bit#(4)) result = tagged Invalid;
    for (Integer offset = 0; offset < 11; offset = offset + 1) begin
        Bit#(5) wide = zeroExtend(cursor) + fromInteger(offset);
        if (wide >= 11) wide = wide - 11;
        Bit#(4) source = truncate(wide);
        if (!isValid(result) && ready[source] == 1)
            result = tagged Valid source;
    end
    return result;
endfunction

interface RxTopIfc;
    method Action configureEpoch(RxEpoch value);
    method Action configureNetwork(RxNetworkConfig cfg);
    method Action configureQp(RxQpIndex index, RxQpConfig cfg);
    method Action start;
    method RxReceiveProgress receiveProgress(RxQpIndex index);
    method TxPsn dmaAckPsn(RxQpIndex index);
    method RxGlobalSeq nextGlobalSeq;
    method RxGlobalSeq retireHead;
    method Bool faulted;
    interface Vector#(4, PipeIn#(RxWireBeat)) ports;
    interface Vector#(4, PipeOut#(RxDmaWriteRequest)) dmaRequests;
    interface Vector#(4, PipeOut#(RxDmaPayloadBeat)) dmaPayloads;
    interface Vector#(4, PipeIn#(RxDmaCompletion)) dmaCompletions;
    interface PipeIn#(RxOrderedFlagIngress) orderedFlagIn;
    interface PipeOut#(RxFlagDmaRequest) flagDmaRequest;
    interface PipeIn#(RxFlagDmaResponse) flagDmaResponse;
    interface PipeOut#(RxOrderedCredit) orderedCredit;
    interface PipeOut#(RxCompletion) completion;
    interface PipeOut#(RxFeedback) feedback;
    interface PipeOut#(RxEcnEvent) ecn;
    interface PipeOut#(RxTxFeedback) txFeedback;
    interface PipeOut#(RxError) errors;
endinterface

(* synthesize *)
module mkRxTop(RxTopIfc);
    let buffer <- mkRxPacketBuffer;
    let parser <- mkRxPacketParser;
    let window <- mkRxRqBitmapManager;
    let dma <- mkRxDmaPlacement;
    let ordered <- mkRxOrderedQueueManager;
    Reg#(Bool) running <- mkReg(False);
    Reg#(Bool) epochConfigured <- mkReg(False);
    Reg#(Bool) networkConfigured <- mkReg(False);
    Reg#(Bit#(4)) qpConfigured <- mkReg(0);
    Vector#(4, Reg#(TxQpn)) configuredQpn <- replicateM(mkReg(0));
    Bool uniqueQpn = True;
    for (Integer i = 0; i < 4; i = i + 1)
        for (Integer j = i + 1; j < 4; j = j + 1)
            if (qpConfigured[i] == 1 && qpConfigured[j] == 1
                    && configuredQpn[i] == configuredQpn[j])
                uniqueQpn = False;
    FIFOF#(RxError) errorQ <- mkSizedFIFOF(32);
    Reg#(Bit#(4)) errorCursor <- mkReg(0);
    Vector#(4, Reg#(Bit#(2))) releaseCursor <- replicateM(mkReg(0));
    Bool anyFault = window.faulted || dma.faulted || ordered.faulted;

    for (Integer i = 0; i < 4; i = i + 1) begin
        mkConnection(buffer.frames[i], parser.frames[i]);
        rule connectParsedPacket(!anyFault);
            window.packets[i].enq(parser.packets[i].first);
            parser.packets[i].deq;
        endrule
        mkConnection(window.admitted[i], dma.admitted[i]);
        mkConnection(dma.readRequests[i], buffer.readRequests[i]);
        mkConnection(buffer.readResponses[i], dma.readResponses[i]);

        let selectedRelease = rxPickReleaseSource(
            {dma.releases[i].notEmpty, window.releases[i].notEmpty,
                parser.releases[i].notEmpty}, releaseCursor[i]);
        rule mergeParserRelease(isValid(selectedRelease)
                && fromMaybe(0, selectedRelease) == 0);
            buffer.releases[i].enq(parser.releases[i].first);
            parser.releases[i].deq;
            releaseCursor[i] <= 1;
        endrule
        rule mergeWindowRelease(isValid(selectedRelease)
                && fromMaybe(0, selectedRelease) == 1);
            buffer.releases[i].enq(window.releases[i].first);
            window.releases[i].deq;
            releaseCursor[i] <= 2;
        endrule
        rule mergeDmaRelease(isValid(selectedRelease)
                && fromMaybe(0, selectedRelease) == 2);
            buffer.releases[i].enq(dma.releases[i].first);
            dma.releases[i].deq;
            releaseCursor[i] <= 0;
        endrule
    end
    mkConnection(dma.retireOut, ordered.retireIn);

    function Bit#(11) errorReadyMask();
        Bit#(11) ready = 0;
        ready[0] = buffer.errors[0].notEmpty;
        ready[1] = buffer.errors[1].notEmpty;
        ready[2] = buffer.errors[2].notEmpty;
        ready[3] = buffer.errors[3].notEmpty;
        ready[4] = parser.errorOut.notEmpty;
        ready[5] = window.errorOut.notEmpty;
        ready[6] = dma.errors[0].notEmpty;
        ready[7] = dma.errors[1].notEmpty;
        ready[8] = dma.errors[2].notEmpty;
        ready[9] = dma.errors[3].notEmpty;
        ready[10] = ordered.errorOut.notEmpty;
        return ready;
    endfunction
    let selectedError = rxPickErrorSource(errorReadyMask(), errorCursor);

    rule mergeBufferError0(isValid(selectedError) && fromMaybe(0, selectedError) == 0);
        errorQ.enq(buffer.errors[0].first); buffer.errors[0].deq; errorCursor <= 1;
    endrule
    rule mergeBufferError1(isValid(selectedError) && fromMaybe(0, selectedError) == 1);
        errorQ.enq(buffer.errors[1].first); buffer.errors[1].deq; errorCursor <= 2;
    endrule
    rule mergeBufferError2(isValid(selectedError) && fromMaybe(0, selectedError) == 2);
        errorQ.enq(buffer.errors[2].first); buffer.errors[2].deq; errorCursor <= 3;
    endrule
    rule mergeBufferError3(isValid(selectedError) && fromMaybe(0, selectedError) == 3);
        errorQ.enq(buffer.errors[3].first); buffer.errors[3].deq; errorCursor <= 4;
    endrule
    rule mergeParserError(isValid(selectedError) && fromMaybe(0, selectedError) == 4);
        errorQ.enq(parser.errorOut.first); parser.errorOut.deq; errorCursor <= 5;
    endrule
    rule mergeWindowError(isValid(selectedError) && fromMaybe(0, selectedError) == 5);
        errorQ.enq(window.errorOut.first); window.errorOut.deq; errorCursor <= 6;
    endrule
    rule mergeDmaError0(isValid(selectedError) && fromMaybe(0, selectedError) == 6);
        errorQ.enq(dma.errors[0].first); dma.errors[0].deq; errorCursor <= 7;
    endrule
    rule mergeDmaError1(isValid(selectedError) && fromMaybe(0, selectedError) == 7);
        errorQ.enq(dma.errors[1].first); dma.errors[1].deq; errorCursor <= 8;
    endrule
    rule mergeDmaError2(isValid(selectedError) && fromMaybe(0, selectedError) == 8);
        errorQ.enq(dma.errors[2].first); dma.errors[2].deq; errorCursor <= 9;
    endrule
    rule mergeDmaError3(isValid(selectedError) && fromMaybe(0, selectedError) == 9);
        errorQ.enq(dma.errors[3].first); dma.errors[3].deq; errorCursor <= 10;
    endrule
    rule mergeOrderedError(isValid(selectedError) && fromMaybe(0, selectedError) == 10);
        errorQ.enq(ordered.errorOut.first); ordered.errorOut.deq; errorCursor <= 0;
    endrule

    Vector#(4, PipeIn#(RxWireBeat)) wireInputs;
    for (Integer i = 0; i < 4; i = i + 1) begin
        wireInputs[i] = interface PipeIn;
            method Action enq(RxWireBeat beat)
                    if (running && !anyFault && buffer.ports[i].notFull == 1);
                buffer.ports[i].enq(beat);
            endmethod
            method Bit#(1) notFull = pack(running && !anyFault
                && buffer.ports[i].notFull == 1);
        endinterface;
    end

    method Action configureEpoch(RxEpoch value) if (!running);
        dma.configureEpoch(value);
        ordered.configureEpoch(value);
        epochConfigured <= True;
    endmethod
    method Action configureNetwork(RxNetworkConfig cfg) if (!running);
        parser.configure(cfg);
        networkConfigured <= True;
    endmethod
    method Action configureQp(RxQpIndex index, RxQpConfig cfg) if (!running);
        window.configure(index, cfg);
        ordered.configure(index, cfg);
        Bit#(4) configured = qpConfigured;
        configured[index] = pack(cfg.valid);
        qpConfigured <= configured;
        configuredQpn[index] <= cfg.qpn;
    endmethod
    method Action start if (!running && epochConfigured
            && networkConfigured && qpConfigured != 0 && uniqueQpn);
        running <= True;
        window.start;
        ordered.start;
    endmethod
    method RxReceiveProgress receiveProgress(RxQpIndex index) = window.progress(index);
    method TxPsn dmaAckPsn(RxQpIndex index) = ordered.dmaAckPsn(index);
    method RxGlobalSeq nextGlobalSeq = window.nextGlobalSeq;
    method RxGlobalSeq retireHead = dma.retireHead;
    method Bool faulted = anyFault;
    interface ports = wireInputs;
    interface dmaRequests = dma.requests;
    interface dmaPayloads = dma.payloads;
    interface dmaCompletions = dma.completions;
    interface orderedFlagIn = interface PipeIn;
        method Action enq(RxOrderedFlagIngress value)
                if (!anyFault && ordered.orderedFlagIn.notFull == 1);
            ordered.orderedFlagIn.enq(value);
        endmethod
        method Bit#(1) notFull = pack(!anyFault
            && ordered.orderedFlagIn.notFull == 1);
    endinterface;
    interface flagDmaRequest = interface PipeOut;
        method RxFlagDmaRequest first
                if (!anyFault && ordered.flagRequestOut.notEmpty == 1);
            return ordered.flagRequestOut.first;
        endmethod
        method Action deq
                if (!anyFault && ordered.flagRequestOut.notEmpty == 1);
            ordered.flagRequestOut.deq;
        endmethod
        method Bit#(1) notEmpty = pack(!anyFault
            && ordered.flagRequestOut.notEmpty == 1);
    endinterface;
    interface flagDmaResponse = ordered.flagResponseIn;
    interface orderedCredit = ordered.creditOut;
    interface completion = ordered.completionOut;
    interface feedback = window.feedbackOut;
    interface ecn = parser.ecnOut;
    interface txFeedback = parser.feedbackOut;
    interface errors = toPipeOut(errorQ);
endmodule
endpackage
