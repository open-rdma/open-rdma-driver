package RxPacketParser;

import FIFOF::*;
import Vector::*;

import CommonTypes::*;
import RxTypes::*;
import TxTypes::*;

typedef 14 RxEthernetHeaderBytes;
typedef 20 RxIpv4HeaderBytes;
typedef 54 RxWriteContinuationHeaderBytes;
typedef 58 RxAckLogicalBytes;
typedef 60 RxEthernetMinFrameBytes;
typedef 70 RxWriteFirstHeaderBytes;
typedef 4084 RxMaxWritePayloadBytes;
typedef 4138 RxMaxFrameBytes;
typedef 16777216 RxMaxMessageBytes;

typedef enum {
    RX_DECODE_DATA,
    RX_DECODE_DATA_ECN,
    RX_DECODE_ACK,
    RX_DECODE_ACK_ECN,
    RX_DECODE_ERROR
} RxDecodeKind deriving(Bits, Eq, FShow);

typedef struct {
    RxDecodeKind kind;
    RxParsedPacket packet;
    RxTxFeedback feedback;
    RxEcnEvent ecn;
    RxBufferRelease releaseEvent;
    RxError error;
} RxDecodeResult deriving(Bits, Eq, FShow);

function Bool rxIsWriteFirst(Bit#(8) opcode);
    return opcode == zeroExtend(pack(OP_WRITE_FIRST))
        || opcode == zeroExtend(pack(OP_WRITE_ONLY));
endfunction

function Bool rxIsWriteLast(Bit#(8) opcode);
    return opcode == zeroExtend(pack(OP_WRITE_LAST))
        || opcode == zeroExtend(pack(OP_WRITE_ONLY));
endfunction

function Bool rxIsWriteOpcode(Bit#(8) opcode);
    return rxIsWriteFirst(opcode)
        || rxIsWriteLast(opcode)
        || opcode == zeroExtend(pack(OP_WRITE_MIDDLE));
endfunction

// Decode one buffered Ethernet frame. Header offsets are the fixed legacy TX
// wire contract documented in README.md; all multibyte fields are big-endian.
function RxDecodeResult rxDecodeFrame(
    RxBufferedFrame frame,
    RxNetworkConfig networkConfig
);
    let opcode = rxHeaderByte(frame, 42);
    let qpn = rxHeaderBE24(frame, 46);
    let psn = rxHeaderBE24(frame, 50);
    let ecn = rxHeaderByte(frame, 15)[1:0];

    Bool firstPacket = rxIsWriteFirst(opcode);
    Bool lastPacket = rxIsWriteLast(opcode);
    Bool writeOpcode = rxIsWriteOpcode(opcode);
    Bool ackOpcode = opcode == 8'h11;
    Bit#(7) headerBytes = firstPacket
        ? fromInteger(valueOf(RxWriteFirstHeaderBytes))
        : fromInteger(valueOf(RxWriteContinuationHeaderBytes));

    Bit#(16) ipBytes = rxHeaderBE16(frame, 16);
    Bit#(16) udpBytes = rxHeaderBE16(frame, 38);
    Bit#(17) logicalBytesWide = zeroExtend(ipBytes)
        + fromInteger(valueOf(RxEthernetHeaderBytes));
    // envelope requires logicalBytesWide <= RxMaxFrameBytes before any
    // truncated length is allowed onto the valid packet path.
    Bit#(13) logicalBytes = truncate(logicalBytesWide);
    Bit#(13) payloadBytes = logicalBytesWide >= zeroExtend(headerBytes)
        ? logicalBytes - zeroExtend(headerBytes)
        : 0;

    Bool paddingIsZero = True;
    for (Integer i = valueOf(RxWriteContinuationHeaderBytes);
            i < valueOf(RxEthernetMinFrameBytes); i = i + 1) begin
        Bit#(17) byteOffset = fromInteger(i);
        if (byteOffset >= logicalBytesWide
                && byteOffset < zeroExtend(frame.bytes)
                && rxHeaderByte(frame, i) != 0) begin
            paddingIsZero = False;
        end
    end
    Bool physicalLengthOk = logicalBytesWide == zeroExtend(frame.bytes)
        || (logicalBytesWide < fromInteger(valueOf(RxEthernetMinFrameBytes))
            && frame.bytes == fromInteger(valueOf(RxEthernetMinFrameBytes))
            && paddingIsZero);

    Bit#(160) ipHeader = 0;
    for (Integer i = 0; i < valueOf(RxIpv4HeaderBytes); i = i + 1) begin
        ipHeader[8 * i + 7 : 8 * i] = rxHeaderByte(frame, 14 + i);
    end

    Bool envelopeValid = frame.bytes
            >= fromInteger(valueOf(RxWriteContinuationHeaderBytes))
        && physicalLengthOk
        && ipBytes >= 40
        && logicalBytesWide <= fromInteger(valueOf(RxMaxFrameBytes))
        && rxHeaderBE48(frame, 0) == networkConfig.localMac
        && rxHeaderBE48(frame, 6) == networkConfig.peerMac
        && rxHeaderBE16(frame, 12) == 16'h0800
        && rxHeaderByte(frame, 14) == 8'h45
        && (rxHeaderBE16(frame, 20) & 16'hbfff) == 0
        && rxHeaderByte(frame, 23) == 17
        && ipv4Checksum(ipHeader) == 0
        && rxHeaderBE32(frame, 26) == networkConfig.peerIp
        && rxHeaderBE32(frame, 30) == networkConfig.localIp
        && rxHeaderBE16(frame, 34) == 16'hc1e8
        && rxHeaderBE16(frame, 36) == 4791
        && udpBytes == ipBytes - fromInteger(valueOf(RxIpv4HeaderBytes))
        && rxHeaderBE16(frame, 40) == 0
        && rxHeaderByte(frame, 43) == 0
        && rxHeaderBE16(frame, 44) == 16'hffff
        && rxHeaderByte(frame, 49) == 0
        && rxHeaderByte(frame, 53) == 0;

    Bit#(32) messageBytes = rxHeaderBE32(frame, 66);
    Bool validWrite = writeOpcode
        && logicalBytesWide >= zeroExtend(headerBytes)
        && payloadBytes != 0
        && payloadBytes <= fromInteger(valueOf(RxMaxWritePayloadBytes))
        && (!firstPacket
            || (messageBytes != 0
                && messageBytes <= fromInteger(valueOf(RxMaxMessageBytes))));
    Bool validAck = ackOpcode
        && logicalBytesWide == fromInteger(valueOf(RxAckLogicalBytes));

    RxParsedPacket parsedPacket = RxParsedPacket{
        payloadTag: frame.tag,
        lane: frame.tag.lane,
        qpn: qpn,
        psn: psn,
        opcode: opcode,
        ecn: ecn,
        payloadStart: headerBytes,
        payloadBytes: payloadBytes,
        remoteAddr: firstPacket ? rxHeaderBE64(frame, 58) : 0,
        rkey: firstPacket ? rxHeaderBE32(frame, 54) : 0,
        // messageBytes is range-checked above on every valid first packet.
        messageBytes: firstPacket ? truncate(messageBytes) : 0,
        first: firstPacket,
        last: lastPacket
    };
    RxTxFeedback feedbackEvent = RxTxFeedback{
        qpn: qpn,
        psn: psn,
        aeth: rxHeaderBE32(frame, 54)
    };
    RxEcnEvent ecnEvent = RxEcnEvent{
        lane: frame.tag.lane,
        qpn: qpn,
        psn: psn,
        ecn: ecn
    };
    RxBufferRelease releaseEvent = RxBufferRelease{tag: frame.tag};
    RxError errorEvent = RxError{
        qpn: qpn,
        psn: psn,
        code: RX_BAD_HEADER
    };

    RxDecodeKind kind = RX_DECODE_ERROR;
    if (envelopeValid && validAck) begin
        kind = ecn == 3 ? RX_DECODE_ACK_ECN : RX_DECODE_ACK;
    end
    else if (envelopeValid && validWrite) begin
        kind = ecn == 3 ? RX_DECODE_DATA_ECN : RX_DECODE_DATA;
    end

    return RxDecodeResult{
        kind: kind,
        packet: parsedPacket,
        feedback: feedbackEvent,
        ecn: ecnEvent,
        releaseEvent: releaseEvent,
        error: errorEvent
    };
endfunction

interface RxFrameDecoderIfc;
    method Action configure(RxNetworkConfig cfg);
    interface PipeIn#(RxBufferedFrame) frameIn;
    interface PipeOut#(RxParsedPacket) packetOut;
    interface PipeOut#(RxTxFeedback) feedbackOut;
    interface PipeOut#(RxEcnEvent) ecnOut;
    interface PipeOut#(RxBufferRelease) releaseOut;
    interface PipeOut#(RxError) errorOut;
endinterface

module mkRxFrameDecoder(RxFrameDecoderIfc);
    Reg#(RxNetworkConfig) networkConfigReg <- mkReg(RxNetworkConfig{
        localMac: 0,
        localIp: 0,
        peerMac: 0,
        peerIp: 0
    });

    // All queues are elastic and use the module clock. resultQ decouples
    // combinational header validation from independently backpressured outputs.
    FIFOF#(RxBufferedFrame) inputFrameQ <- mkFIFOF;
    FIFOF#(RxDecodeResult) resultQ <- mkSizedFIFOF(4);
    FIFOF#(RxParsedPacket) packetQ <- mkFIFOF;
    FIFOF#(RxTxFeedback) feedbackQ <- mkSizedFIFOF(4);
    FIFOF#(RxEcnEvent) ecnQ <- mkSizedFIFOF(4);
    FIFOF#(RxBufferRelease) releaseQ <- mkSizedFIFOF(4);
    FIFOF#(RxError) errorQ <- mkSizedFIFOF(4);

    // Input: inputFrameQ. Output: resultQ. Latency: one rule firing.
    rule decodeFrame;
        let frame = inputFrameQ.first;
        inputFrameQ.deq;
        resultQ.enq(rxDecodeFrame(frame, networkConfigReg));
    endrule

    // Each route rule consumes one result atomically with every required
    // output, so packet metadata, ECN, release, and error events cannot split.
    rule routeData(resultQ.first.kind == RX_DECODE_DATA);
        let result = resultQ.first;
        resultQ.deq;
        packetQ.enq(result.packet);
    endrule

    rule routeDataEcn(resultQ.first.kind == RX_DECODE_DATA_ECN);
        let result = resultQ.first;
        resultQ.deq;
        packetQ.enq(result.packet);
        ecnQ.enq(result.ecn);
    endrule

    rule routeAck(resultQ.first.kind == RX_DECODE_ACK);
        let result = resultQ.first;
        resultQ.deq;
        feedbackQ.enq(result.feedback);
        releaseQ.enq(result.releaseEvent);
    endrule

    rule routeAckEcn(resultQ.first.kind == RX_DECODE_ACK_ECN);
        let result = resultQ.first;
        resultQ.deq;
        feedbackQ.enq(result.feedback);
        ecnQ.enq(result.ecn);
        releaseQ.enq(result.releaseEvent);
    endrule

    rule routeError(resultQ.first.kind == RX_DECODE_ERROR);
        let result = resultQ.first;
        resultQ.deq;
        errorQ.enq(result.error);
        releaseQ.enq(result.releaseEvent);
    endrule

    method Action configure(RxNetworkConfig value);
        networkConfigReg <= value;
    endmethod

    interface frameIn = toPipeIn(inputFrameQ);
    interface packetOut = toPipeOut(packetQ);
    interface feedbackOut = toPipeOut(feedbackQ);
    interface ecnOut = toPipeOut(ecnQ);
    interface releaseOut = toPipeOut(releaseQ);
    interface errorOut = toPipeOut(errorQ);
endmodule

interface RxPacketParserIfc;
    method Action configure(RxNetworkConfig cfg);
    interface Vector#(RxLaneCount, PipeIn#(RxBufferedFrame)) frames;
    interface Vector#(RxLaneCount, PipeOut#(RxParsedPacket)) packets;
    interface PipeOut#(RxTxFeedback) feedbackOut;
    interface PipeOut#(RxEcnEvent) ecnOut;
    interface Vector#(RxLaneCount, PipeOut#(RxBufferRelease)) releases;
    interface PipeOut#(RxError) errorOut;
endinterface

module mkRxPacketParser(RxPacketParserIfc);
    Vector#(RxLaneCount, RxFrameDecoderIfc) laneDecoders <-
        replicateM(mkRxFrameDecoder);
    FIFOF#(RxTxFeedback) feedbackQ <- mkSizedFIFOF(8);
    FIFOF#(RxEcnEvent) ecnQ <- mkSizedFIFOF(8);
    FIFOF#(RxError) errorQ <- mkSizedFIFOF(8);
    Reg#(RxLaneId) feedbackCursorReg <- mkReg(0);
    Reg#(RxLaneId) ecnCursorReg <- mkReg(0);
    Reg#(RxLaneId) errorCursorReg <- mkReg(0);

    function Bit#(RxLaneCount) feedbackReadyMask();
        Bit#(RxLaneCount) readyMask = 0;
        for (Integer i = 0; i < valueOf(RxLaneCount); i = i + 1) begin
            readyMask[i] = laneDecoders[i].feedbackOut.notEmpty;
        end
        return readyMask;
    endfunction

    function Bit#(RxLaneCount) ecnReadyMask();
        Bit#(RxLaneCount) readyMask = 0;
        for (Integer i = 0; i < valueOf(RxLaneCount); i = i + 1) begin
            readyMask[i] = laneDecoders[i].ecnOut.notEmpty;
        end
        return readyMask;
    endfunction

    function Bit#(RxLaneCount) errorReadyMask();
        Bit#(RxLaneCount) readyMask = 0;
        for (Integer i = 0; i < valueOf(RxLaneCount); i = i + 1) begin
            readyMask[i] = laneDecoders[i].errorOut.notEmpty;
        end
        return readyMask;
    endfunction

    let selectedFeedback = rxPickReadyLane(
        feedbackReadyMask(),
        feedbackCursorReg
    );
    let selectedEcn = rxPickReadyLane(ecnReadyMask(), ecnCursorReg);
    let selectedError = rxPickReadyLane(errorReadyMask(), errorCursorReg);

    // One event per class is merged each cycle. The cursor advances only after
    // dequeue, providing round-robin service across the four physical lanes.
    for (Integer i = 0; i < valueOf(RxLaneCount); i = i + 1) begin
        rule mergeFeedbackLane(isValid(selectedFeedback)
                && fromMaybe(0, selectedFeedback) == fromInteger(i));
            feedbackQ.enq(laneDecoders[i].feedbackOut.first);
            laneDecoders[i].feedbackOut.deq;
            feedbackCursorReg <= fromInteger(
                (i + 1) % valueOf(RxLaneCount)
            );
        endrule

        rule mergeEcnLane(isValid(selectedEcn)
                && fromMaybe(0, selectedEcn) == fromInteger(i));
            ecnQ.enq(laneDecoders[i].ecnOut.first);
            laneDecoders[i].ecnOut.deq;
            ecnCursorReg <= fromInteger(
                (i + 1) % valueOf(RxLaneCount)
            );
        endrule

        rule mergeErrorLane(isValid(selectedError)
                && fromMaybe(0, selectedError) == fromInteger(i));
            errorQ.enq(laneDecoders[i].errorOut.first);
            laneDecoders[i].errorOut.deq;
            errorCursorReg <= fromInteger(
                (i + 1) % valueOf(RxLaneCount)
            );
        endrule
    end

    Vector#(RxLaneCount, PipeIn#(RxBufferedFrame)) frameInputs;
    Vector#(RxLaneCount, PipeOut#(RxParsedPacket)) packetOutputs;
    Vector#(RxLaneCount, PipeOut#(RxBufferRelease)) releaseOutputs;
    for (Integer i = 0; i < valueOf(RxLaneCount); i = i + 1) begin
        frameInputs[i] = laneDecoders[i].frameIn;
        packetOutputs[i] = laneDecoders[i].packetOut;
        releaseOutputs[i] = laneDecoders[i].releaseOut;
    end

    method Action configure(RxNetworkConfig value);
        for (Integer i = 0; i < valueOf(RxLaneCount); i = i + 1) begin
            laneDecoders[i].configure(value);
        end
    endmethod

    interface frames = frameInputs;
    interface packets = packetOutputs;
    interface feedbackOut = toPipeOut(feedbackQ);
    interface ecnOut = toPipeOut(ecnQ);
    interface releases = releaseOutputs;
    interface errorOut = toPipeOut(errorQ);
endmodule

endpackage
