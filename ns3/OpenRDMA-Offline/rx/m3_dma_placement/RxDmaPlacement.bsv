package RxDmaPlacement;
import Vector::*;
import FIFOF::*;
import CommonTypes::*;
import TxTypes::*;
import RxTypes::*;

interface RxDmaLaneIfc;
    method Action configureEpoch(RxEpoch value);
    interface PipeIn#(RxAdmittedPacket) admittedIn;
    interface PipeOut#(RxDmaWriteRequest) requestOut;
    interface PipeOut#(RxDmaPayloadBeat) payloadOut;
    interface PipeIn#(RxDmaCompletion) completionIn;
    interface PipeOut#(RxPayloadReadRequest) readRequestOut;
    interface PipeIn#(RxPayloadReadResponse) readResponseIn;
    interface PipeOut#(RxBufferRelease) releaseOut;
    interface PipeOut#(RxDmaRetire) completedOut;
    interface PipeOut#(RxError) errorOut;
    method Bool faulted;
endinterface

module mkRxDmaLane#(RxLaneId lane)(RxDmaLaneIfc);
    FIFOF#(RxAdmittedPacket) admittedQ <- mkSizedFIFOF(4);
    FIFOF#(RxDmaWriteRequest) requestQ <- mkFIFOF;
    FIFOF#(RxDmaPayloadBeat) payloadQ <- mkSizedFIFOF(4);
    FIFOF#(RxDmaCompletion) completionQ <- mkSizedFIFOF(4);
    FIFOF#(RxPayloadReadRequest) readRequestQ <- mkSizedFIFOF(4);
    FIFOF#(RxPayloadReadResponse) readResponseQ <- mkSizedFIFOF(4);
    FIFOF#(RxBufferRelease) releaseQ <- mkSizedFIFOF(4);
    FIFOF#(RxDmaRetire) completedQ <- mkSizedFIFOF(4);
    FIFOF#(RxError) errorQ <- mkSizedFIFOF(8);

    Reg#(Bool) failed <- mkReg(False);
    Reg#(Bool) epochConfigured <- mkReg(False);
    Reg#(RxEpoch) currentEpoch <- mkReg(0);
    Reg#(Bool) active <- mkReg(False);
    Reg#(Bool) descriptorSent <- mkReg(False);
    Reg#(Bool) descriptorAccepted <- mkReg(False);
    Reg#(Bool) payloadAccepted <- mkReg(False);
    Reg#(Bool) waitingRead <- mkReg(False);
    Reg#(Bool) payloadDone <- mkReg(False);
    Reg#(RxAdmittedPacket) current <- mkReg(unpack(0));
    Reg#(Bit#(13)) payloadOffset <- mkReg(0);
    Reg#(Bit#(8)) expectedWord <- mkReg(0);

    rule beginPacket(!failed && epochConfigured && !active);
        let packet = admittedQ.first;
        admittedQ.deq;
        if (packet.lane != lane) begin
            errorQ.enq(RxError{qpn:packet.qpn, psn:packet.psn,
                code:RX_DMA_BAD_PAYLOAD});
            releaseQ.enq(RxBufferRelease{tag:packet.payloadTag});
            failed <= True;
        end
        else begin
            current <= packet;
            payloadOffset <= 0;
            descriptorSent <= False;
            descriptorAccepted <= False;
            payloadAccepted <= False;
            waitingRead <= False;
            payloadDone <= False;
            active <= True;
        end
    endrule

    rule sendDescriptor(!failed && active && !descriptorSent);
        requestQ.enq(RxDmaWriteRequest{globalSeq:current.globalSeq,
            epoch:currentEpoch, lane:lane, qpn:current.qpn, psn:current.psn,
            addr:current.dmaAddr, bytes:current.payloadBytes});
        descriptorSent <= True;
    endrule

    rule requestPayload(!failed && active && descriptorAccepted
            && !waitingRead && !payloadDone);
        Bit#(13) bytePosition = zeroExtend(current.payloadStart) + payloadOffset;
        Bit#(8) word = truncate(bytePosition >> 5);
        readRequestQ.enq(RxPayloadReadRequest{tag:current.payloadTag,
            wordIndex:word});
        expectedWord <= word;
        waitingRead <= True;
    endrule

    rule consumePayloadRead(!failed && active && waitingRead);
        let response = readResponseQ.first;
        readResponseQ.deq;
        Bool valid = response.valid && response.tag == current.payloadTag
            && response.wordIndex == expectedWord;
        if (!valid) begin
            errorQ.enq(RxError{qpn:current.qpn, psn:current.psn,
                code:RX_DMA_BAD_PAYLOAD});
            releaseQ.enq(RxBufferRelease{tag:current.payloadTag});
            waitingRead <= False;
            active <= False;
            failed <= True;
        end
        else begin
            Bit#(13) remaining = current.payloadBytes - payloadOffset;
            Bit#(6) take = remaining < 32 ? truncate(remaining) : 32;
            Bit#(13) bytePosition = zeroExtend(current.payloadStart) + payloadOffset;
            Bit#(5) intraWord = truncate(bytePosition);
            Bit#(512) window = {response.high, response.low};
            Bit#(9) shift = zeroExtend(intraWord) * 8;
            Bit#(13) nextOffset = payloadOffset + zeroExtend(take);
            Bool firstBeat = payloadOffset == 0;
            Bool lastBeat = nextOffset == current.payloadBytes;
            payloadQ.enq(RxDmaPayloadBeat{globalSeq:current.globalSeq,
                epoch:currentEpoch, data:truncate(window >> shift), be:byteMask32(take),
                sop:pack(firstBeat), eop:pack(lastBeat)});
            payloadOffset <= nextOffset;
            waitingRead <= False;
            if (lastBeat)
                payloadDone <= True;
        end
    endrule

    rule finishPacket(!failed && active && payloadDone && payloadAccepted);
        let completion = completionQ.first;
        completionQ.deq;
        Bool identityOk = completion.globalSeq == current.globalSeq
            && completion.epoch == currentEpoch && completion.lane == lane;
        if (!identityOk || !completion.success) begin
            errorQ.enq(RxError{qpn:current.qpn, psn:current.psn,
                code:identityOk ? RX_DMA_FAILED : RX_DMA_BAD_COMPLETION});
            failed <= True;
        end
        else begin
            completedQ.enq(RxDmaRetire{globalSeq:current.globalSeq,
                qp:current.qp, qpn:current.qpn, psn:current.psn, success:True,
                ackValid:current.ackValid,
                ackPsnAfterMerge:current.ackPsnAfterMerge,
                messageComplete:current.messageComplete,
                messageLastPsn:current.messageLastPsn,
                messageBytes:current.messageBytes});
        end
        releaseQ.enq(RxBufferRelease{tag:current.payloadTag});
        active <= False;
        payloadDone <= False;
    endrule

    rule rejectUnexpectedCompletion(!failed && completionQ.notEmpty
            && (!active || !payloadDone));
        let completion = completionQ.first;
        completionQ.deq;
        errorQ.enq(RxError{qpn:active ? current.qpn : 0,
            psn:active ? current.psn : 0, code:RX_DMA_BAD_COMPLETION});
        if (active)
            releaseQ.enq(RxBufferRelease{tag:current.payloadTag});
        active <= False;
        failed <= True;
    endrule

    rule rejectUnexpectedRead(!failed && readResponseQ.notEmpty && !waitingRead);
        readResponseQ.deq;
        errorQ.enq(RxError{qpn:active ? current.qpn : 0,
            psn:active ? current.psn : 0, code:RX_DMA_BAD_PAYLOAD});
        if (active)
            releaseQ.enq(RxBufferRelease{tag:current.payloadTag});
        active <= False;
        failed <= True;
    endrule

    method Action configureEpoch(RxEpoch value) if (!active);
        currentEpoch <= value;
        epochConfigured <= True;
    endmethod
    interface admittedIn = interface PipeIn;
        method Action enq(RxAdmittedPacket packet)
                if (!failed && epochConfigured && admittedQ.notFull);
            admittedQ.enq(packet);
        endmethod
        method Bit#(1) notFull = pack(!failed && epochConfigured
            && admittedQ.notFull);
    endinterface;
    interface requestOut = interface PipeOut;
        method RxDmaWriteRequest first if (requestQ.notEmpty);
            return requestQ.first;
        endmethod
        method Action deq if (requestQ.notEmpty);
            requestQ.deq;
            descriptorAccepted <= True;
        endmethod
        method Bit#(1) notEmpty = pack(requestQ.notEmpty);
    endinterface;
    interface payloadOut = interface PipeOut;
        method RxDmaPayloadBeat first if (payloadQ.notEmpty);
            return payloadQ.first;
        endmethod
        method Action deq if (payloadQ.notEmpty);
            if (payloadQ.first.eop == 1)
                payloadAccepted <= True;
            payloadQ.deq;
        endmethod
        method Bit#(1) notEmpty = pack(payloadQ.notEmpty);
    endinterface;
    interface completionIn = toPipeIn(completionQ);
    interface readRequestOut = toPipeOut(readRequestQ);
    interface readResponseIn = toPipeIn(readResponseQ);
    interface releaseOut = toPipeOut(releaseQ);
    interface completedOut = toPipeOut(completedQ);
    interface errorOut = toPipeOut(errorQ);
    method Bool faulted = failed;
endmodule

interface RxDmaPlacementIfc;
    method Action configureEpoch(RxEpoch value);
    method RxGlobalSeq retireHead;
    method Bool faulted;
    interface Vector#(4, PipeIn#(RxAdmittedPacket)) admitted;
    interface Vector#(4, PipeOut#(RxDmaWriteRequest)) requests;
    interface Vector#(4, PipeOut#(RxDmaPayloadBeat)) payloads;
    interface Vector#(4, PipeIn#(RxDmaCompletion)) completions;
    interface Vector#(4, PipeOut#(RxPayloadReadRequest)) readRequests;
    interface Vector#(4, PipeIn#(RxPayloadReadResponse)) readResponses;
    interface Vector#(4, PipeOut#(RxBufferRelease)) releases;
    interface Vector#(4, PipeOut#(RxError)) errors;
    interface PipeOut#(RxDmaRetire) retireOut;
endinterface

module mkRxDmaPlacement(RxDmaPlacementIfc);
    Vector#(4, RxDmaLaneIfc) lanes;
    for (Integer i = 0; i < 4; i = i + 1)
        lanes[i] <- mkRxDmaLane(fromInteger(i));
    FIFOF#(RxDmaRetire) retireQ <- mkSizedFIFOF(8);
    Reg#(RxGlobalSeq) head <- mkReg(0);

    Bool anyFault = False;
    for (Integer i = 0; i < 4; i = i + 1)
        anyFault = anyFault || lanes[i].faulted;

    for (Integer i = 0; i < 4; i = i + 1) begin
        rule retireCompletedLane(!anyFault && lanes[i].completedOut.notEmpty == 1
                && lanes[i].completedOut.first.globalSeq == head);
            retireQ.enq(lanes[i].completedOut.first);
            lanes[i].completedOut.deq;
            head <= head + 1;
        endrule
    end

    Vector#(4, PipeIn#(RxAdmittedPacket)) admittedInputs;
    Vector#(4, PipeOut#(RxDmaWriteRequest)) requestOutputs;
    Vector#(4, PipeOut#(RxDmaPayloadBeat)) payloadOutputs;
    Vector#(4, PipeIn#(RxDmaCompletion)) completionInputs;
    Vector#(4, PipeOut#(RxPayloadReadRequest)) readRequestOutputs;
    Vector#(4, PipeIn#(RxPayloadReadResponse)) readResponseInputs;
    Vector#(4, PipeOut#(RxBufferRelease)) releaseOutputs;
    Vector#(4, PipeOut#(RxError)) errorOutputs;
    for (Integer i = 0; i < 4; i = i + 1) begin
        admittedInputs[i] = interface PipeIn;
            method Action enq(RxAdmittedPacket packet)
                    if (!anyFault && lanes[i].admittedIn.notFull == 1);
                lanes[i].admittedIn.enq(packet);
            endmethod
            method Bit#(1) notFull = pack(!anyFault
                && lanes[i].admittedIn.notFull == 1);
        endinterface;
        requestOutputs[i] = lanes[i].requestOut;
        payloadOutputs[i] = lanes[i].payloadOut;
        completionInputs[i] = lanes[i].completionIn;
        readRequestOutputs[i] = lanes[i].readRequestOut;
        readResponseInputs[i] = lanes[i].readResponseIn;
        releaseOutputs[i] = lanes[i].releaseOut;
        errorOutputs[i] = lanes[i].errorOut;
    end

    method Action configureEpoch(RxEpoch value);
        for (Integer i = 0; i < 4; i = i + 1)
            lanes[i].configureEpoch(value);
    endmethod
    method RxGlobalSeq retireHead = head;
    method Bool faulted = anyFault;
    interface admitted = admittedInputs;
    interface requests = requestOutputs;
    interface payloads = payloadOutputs;
    interface completions = completionInputs;
    interface readRequests = readRequestOutputs;
    interface readResponses = readResponseInputs;
    interface releases = releaseOutputs;
    interface errors = errorOutputs;
    interface retireOut = toPipeOut(retireQ);
endmodule
endpackage
