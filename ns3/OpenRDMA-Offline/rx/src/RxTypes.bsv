package RxTypes;
import Vector::*;
import TxTypes::*;

// Functional MVP limits. The receive window and packet-buffer slot count match.
typedef 4 RxLaneCount;
typedef 4 RxQpCount;
typedef 16 RxWindowSize;
typedef 16 RxPayloadSlots;
typedef 130 RxFrameBeats;
typedef Bit#(2) RxLaneId;
typedef Bit#(2) RxQpIndex;
typedef Bit#(4) RxWindowIndex;
typedef Bit#(4) RxPayloadSlot;
typedef Bit#(8) RxPayloadGeneration;
typedef Bit#(64) RxGlobalSeq;
typedef Bit#(64) RxOrderedSeq;
typedef Bit#(16) RxEpoch;

typedef struct {
    Bit#(256) data;
    Bit#(32) be;
    Bit#(1) sof;
    Bit#(1) eof;
} RxWireBeat deriving(Bits, Eq, FShow);

function RxWireBeat rxFromEth(EthPacket p);
    // Monitoring sideband from M3 is deliberately not part of the wire contract.
    return RxWireBeat{data:p.data, be:p.be, sof:p.sof, eof:p.eof};
endfunction

typedef struct {
    RxLaneId lane;
    RxPayloadSlot slot;
    RxPayloadGeneration generation;
} RxPayloadTag deriving(Bits, Eq, FShow);

typedef struct {
    RxPayloadTag tag;
    Bit#(256) header0;
    Bit#(256) header1;
    Bit#(256) header2;
    Bit#(13) bytes;
    Bit#(8) beats;
} RxBufferedFrame deriving(Bits, Eq, FShow);

typedef struct {
    RxPayloadTag tag;
    Bit#(8) wordIndex;
} RxPayloadReadRequest deriving(Bits, Eq, FShow);

typedef struct {
    RxPayloadTag tag;
    Bit#(8) wordIndex;
    Bit#(256) low;
    Bit#(256) high;
    Bool valid;
} RxPayloadReadResponse deriving(Bits, Eq, FShow);

typedef struct {
    RxPayloadTag tag;
} RxBufferRelease deriving(Bits, Eq, FShow);

typedef struct {
    RxPayloadTag payloadTag;
    RxLaneId lane;
    TxQpn qpn;
    TxPsn psn;
    Bit#(8) opcode;
    Bit#(2) ecn;
    Bit#(7) payloadStart;
    Bit#(13) payloadBytes;
    Bit#(64) remoteAddr;
    Bit#(32) rkey;
    TxLength messageBytes;
    Bool first;
    Bool last;
} RxParsedPacket deriving(Bits, Eq, FShow);

typedef struct {
    Bool valid;
    TxQpn qpn;
    TxPsn initialPsn;
    TxPmtuCode pmtu;
    Bit#(32) rkey;
    Bit#(64) virtualBase;
    Bit#(64) physicalBase;
    Bit#(64) regionBytes;
    Bool remoteWrite;
} RxQpConfig deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(48) localMac;
    Bit#(32) localIp;
    Bit#(48) peerMac;
    Bit#(32) peerIp;
} RxNetworkConfig deriving(Bits, Eq, FShow);

typedef enum {
    RX_BAD_FRAME,
    RX_BAD_HEADER,
    RX_UNKNOWN_QP,
    RX_OUT_OF_WINDOW,
    RX_BAD_SEGMENT,
    RX_ACCESS_DENIED,
    RX_BUFFER_BAD_TAG,
    RX_BUFFER_OVERFLOW,
    RX_DMA_FAILED,
    RX_DMA_BAD_COMPLETION,
    RX_DMA_BAD_PAYLOAD,
    RX_ORDERED_QUEUE_FULL,
    RX_ORDERED_BAD_FLAG,
    RX_ORDERED_BAD_RETIRE,
    RX_FLAG_DMA_FAILED,
    RX_FLAG_DMA_BAD_RESPONSE,
    RX_FAULTED,
    RX_PENDING_TIMEOUT,
    RX_ERROR_RESERVED
} RxErrorCode deriving(Bits, Eq, FShow);

typedef struct {
    TxQpn qpn;
    TxPsn psn;
    RxErrorCode code;
} RxError deriving(Bits, Eq, FShow);

typedef enum { RX_ACK, RX_SACK, RX_NACK } RxFeedbackKind deriving(Bits, Eq, FShow);

typedef struct {
    TxQpn qpn;
    RxFeedbackKind kind;
    TxPsn ackPsn;
    TxPsn sackBase;
    Bit#(64) sackBitmap;
    Bit#(7) oooCount;
    TxPsn triggerPsn;
} RxFeedback deriving(Bits, Eq, FShow);

typedef struct {
    RxLaneId lane;
    TxQpn qpn;
    TxPsn psn;
    Bit#(2) ecn;
} RxEcnEvent deriving(Bits, Eq, FShow);

typedef struct {
    TxQpn qpn;
    TxPsn psn;
    Bit#(32) aeth;
} RxTxFeedback deriving(Bits, Eq, FShow);

typedef struct {
    RxGlobalSeq globalSeq;
    RxPayloadTag payloadTag;
    RxLaneId lane;
    RxQpIndex qp;
    TxQpn qpn;
    TxPsn psn;
    Bit#(64) dmaAddr;
    Bit#(7) payloadStart;
    Bit#(13) payloadBytes;
    TxLength messageBytes;
    Bool first;
    Bool last;
    Bool ackValid;
    TxPsn ackPsnAfterMerge;
    Bool messageComplete;
    TxPsn messageLastPsn;
} RxAdmittedPacket deriving(Bits, Eq, FShow);

typedef struct {
    RxGlobalSeq globalSeq;
    RxEpoch epoch;
    RxLaneId lane;
    TxQpn qpn;
    TxPsn psn;
    Bit#(64) addr;
    Bit#(13) bytes;
} RxDmaWriteRequest deriving(Bits, Eq, FShow);

typedef struct {
    RxGlobalSeq globalSeq;
    RxEpoch epoch;
    Bit#(256) data;
    Bit#(32) be;
    Bit#(1) sop;
    Bit#(1) eop;
} RxDmaPayloadBeat deriving(Bits, Eq, FShow);

typedef struct {
    RxGlobalSeq globalSeq;
    RxEpoch epoch;
    RxLaneId lane;
    Bool success;
} RxDmaCompletion deriving(Bits, Eq, FShow);

typedef struct {
    RxGlobalSeq globalSeq;
    RxQpIndex qp;
    TxQpn qpn;
    TxPsn psn;
    Bool success;
    Bool ackValid;
    TxPsn ackPsnAfterMerge;
    Bool messageComplete;
    TxPsn messageLastPsn;
    TxLength messageBytes;
} RxDmaRetire deriving(Bits, Eq, FShow);

typedef struct {
    TxQpn qpn;
    TxPsn lastPsn;
    TxLength bytes;
    Bool success;
} RxCompletion deriving(Bits, Eq, FShow);

typedef struct {
    TxQpn qpn;
    TxPsn barrierPsn;
    Bit#(64) flagAddr;
    Bit#(32) rkey;
    Bit#(32) value;
} RxOrderedFlagIngress deriving(Bits, Eq, FShow);

typedef struct {
    RxOrderedSeq orderedSeq;
    RxEpoch epoch;
    TxQpn qpn;
    TxPsn barrierPsn;
    Bit#(64) addr;
    Bit#(32) value;
} RxFlagDmaRequest deriving(Bits, Eq, FShow);

typedef struct {
    RxOrderedSeq orderedSeq;
    RxEpoch epoch;
    Bool success;
} RxFlagDmaResponse deriving(Bits, Eq, FShow);

typedef struct {
    TxQpn qpn;
    Bit#(64) orderedGrantLimit;
} RxOrderedCredit deriving(Bits, Eq, FShow);

typedef struct {
    TxPsn ackPsn;
    Bit#(16) receiveBitmap;
    Bit#(7) oooCount;
} RxReceiveProgress deriving(Bits, Eq, FShow);

function Bit#(8) rxHeaderByte(RxBufferedFrame frame, Integer offset);
    if (offset < 32)
        return truncate(frame.header0 >> (offset * 8));
    else if (offset < 64)
        return truncate(frame.header1 >> ((offset - 32) * 8));
    else
        return truncate(frame.header2 >> ((offset - 64) * 8));
endfunction

function Bit#(16) rxHeaderBE16(RxBufferedFrame frame, Integer offset);
    return {rxHeaderByte(frame, offset), rxHeaderByte(frame, offset + 1)};
endfunction

function Bit#(24) rxHeaderBE24(RxBufferedFrame frame, Integer offset);
    return {rxHeaderByte(frame, offset), rxHeaderByte(frame, offset + 1),
        rxHeaderByte(frame, offset + 2)};
endfunction

function Bit#(32) rxHeaderBE32(RxBufferedFrame frame, Integer offset);
    return {rxHeaderBE16(frame, offset), rxHeaderBE16(frame, offset + 2)};
endfunction

function Bit#(48) rxHeaderBE48(RxBufferedFrame frame, Integer offset);
    return {rxHeaderBE16(frame, offset), rxHeaderBE32(frame, offset + 2)};
endfunction

function Bit#(64) rxHeaderBE64(RxBufferedFrame frame, Integer offset);
    return {rxHeaderBE32(frame, offset), rxHeaderBE32(frame, offset + 4)};
endfunction

function TxPsn rxPsnDistance(TxPsn base, TxPsn psn);
    return psn - base;
endfunction

function Bool rxPsnBefore(TxPsn a, TxPsn b);
    TxPsn delta = a - b;
    return delta[23] == 1;
endfunction

function Bool rxPsnInWindow(TxPsn ackPsn, TxPsn psn);
    TxPsn delta = psn - (ackPsn + 1);
    return delta[23] == 0 && delta < fromInteger(valueOf(RxWindowSize));
endfunction

function Bool rxPsnAtOrAfter(TxPsn watermark, TxPsn barrier);
    TxPsn delta = watermark - barrier;
    return delta[23] == 0;
endfunction

function Maybe#(RxLaneId) rxPickReadyLane(Bit#(4) ready, RxLaneId cursor);
    Maybe#(RxLaneId) result = tagged Invalid;
    for (Integer offset = 0; offset < 4; offset = offset + 1) begin
        RxLaneId lane = cursor + fromInteger(offset);
        if (!isValid(result) && ready[lane] == 1)
            result = tagged Valid lane;
    end
    return result;
endfunction

function Bool rxPrefixBe32(Bit#(32) be);
    return be != 0 && (be & (be + 1)) == 0;
endfunction

endpackage
