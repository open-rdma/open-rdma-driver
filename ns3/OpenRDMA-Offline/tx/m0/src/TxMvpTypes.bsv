// Renamed from `package TxTypes` so that the glue between this MVP and the
// Phase-A TX chain (implementation/phasea_tx) can import BOTH this package and
// the Phase-A `TxTypes` package in one compile unit. BSC resolves packages by
// name, so the two can no longer share it.
package TxMvpTypes;

typedef Bit#(24) TxQpn;
typedef Bit#(18) TxSlotId;
typedef Bit#(24) TxPsn;
typedef Bit#(25) TxLength;
typedef Bit#(17) TxPsnCount;
typedef Bit#(16) TxRequestId;
typedef Bit#(16) TxDmaSeq;

typedef Bit#(2) TxQpIndex;
typedef Bit#(4) TxSlotIndex;
typedef Bit#(7) TxPteIndex;
typedef Bit#(13) TxMrLookupIndex;
typedef Bit#(19) TxMrKeyPart;
typedef Bit#(32) TxLocalLkey;

typedef struct {
    TxQpn qpn;
    Bit#(64) token;
} DecodedGdrpSubmission deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(1) valid;
    Bit#(1) allowWriteRelaxed;
    TxLocalLkey localLkey;
    Bit#(64) localOffset;
    Bit#(64) remoteBase;
    TxLength maxLen;
    Bit#(32) remoteKey;
} PayloadSlot deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(1) valid;
    Bit#(3) pmtu;
    TxPsn initialPsn;
} QpEntry deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(1) valid;
    TxMrKeyPart keyPart;
    Bit#(1) allowLocalRead;
    Bit#(64) baseVa;
    Bit#(64) length;
    TxPteIndex pgtOffset;
    Bit#(6) pageShift;
} MrEntry deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(1) valid;
    Bit#(64) physicalPageBase;
} PteEntry deriving(Bits, Eq, FShow);

typedef struct {
    TxSlotIndex index;
    PayloadSlot entry;
} SlotCfgReq deriving(Bits, Eq, FShow);

typedef struct {
    TxQpIndex index;
    QpEntry entry;
} QpCfgReq deriving(Bits, Eq, FShow);

typedef struct {
    TxMrLookupIndex index;
    MrEntry entry;
} MrCfgReq deriving(Bits, Eq, FShow);

typedef struct {
    TxPteIndex index;
    PteEntry entry;
} PgtCfgReq deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(1) success;
} TxCfgResp deriving(Bits, Eq, FShow);

typedef struct {
    TxSlotId slotId;
} SlotQueryReq deriving(Bits, Eq, FShow);

typedef struct {
    TxSlotId slotId;
    PayloadSlot entry;
} SlotQueryResp deriving(Bits, Eq, FShow);

typedef struct {
    TxLocalLkey localLkey;
} MrQueryReq deriving(Bits, Eq, FShow);

typedef struct {
    TxLocalLkey localLkey;
    MrEntry entry;
} MrQueryResp deriving(Bits, Eq, FShow);

typedef struct {
    TxQpn qpn;
    TxLength len;
} QpReserveReq deriving(Bits, Eq, FShow);

typedef struct {
    TxQpn qpn;
    Bit#(3) pmtu;
    TxPsn startPsn;
    TxPsnCount psnCount;
} QpReserveResp deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(64) pteIndex;
} PgtQueryReq deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(64) pteIndex;
    PteEntry entry;
} PgtQueryResp deriving(Bits, Eq, FShow);

typedef struct {
    TxQpn qpn;
    TxSlotId slotId;
    Bit#(4) debugSeq;
    TxLocalLkey localLkey;
    Bit#(64) localOffset;
    Bit#(64) remoteAddr;
    TxLength len;
    Bit#(32) remoteKey;
} TxWorkRequest deriving(Bits, Eq, FShow);

typedef struct {
    TxWorkRequest work;
    Bit#(64) localVa;
    Bit#(64) mrBaseVa;
    TxPteIndex pgtOffset;
    Bit#(6) pageShift;
} MrAdmittedRequest deriving(Bits, Eq, FShow);

typedef struct {
    TxRequestId requestId;
    TxQpn qpn;
    TxSlotId slotId;
    Bit#(4) debugSeq;
    Bit#(3) pmtu;
    TxPsn startPsn;
    TxPsnCount psnCount;
    Bit#(64) localVa;
    Bit#(64) mrBaseVa;
    TxPteIndex pgtOffset;
    Bit#(6) pageShift;
    Bit#(64) remoteAddr;
    TxLength len;
    Bit#(32) remoteKey;
} ParsedTxRequest deriving(Bits, Eq, FShow);

typedef struct {
    ParsedTxRequest parsed;
    Bit#(64) pageOffset;
    Bit#(64) currentRemoteAddr;
    TxLength fragmentLen;
    Bit#(1) isFirst;
    Bit#(1) isLast;
    Bit#(64) pteIndex;
} PendingPgtFragment deriving(Bits, Eq, FShow);

typedef struct {
    TxRequestId requestId;
    TxQpn qpn;
    TxSlotId slotId;
    Bit#(4) debugSeq;
    Bit#(3) pmtu;
    TxPsn startPsn;
    TxPsnCount psnCount;
    TxLength messageLength;
    Bit#(64) physicalAddr;
    Bit#(64) remoteAddr;
    TxLength len;
    Bit#(32) remoteKey;
    Bit#(1) isFirst;
    Bit#(1) isLast;
} TranslatedPageFragment deriving(Bits, Eq, FShow);

typedef struct {
    TxRequestId requestId;
    TxQpn qpn;
    TxSlotId slotId;
    Bit#(4) debugSeq;
    Bit#(3) pmtu;
    TxPsn startPsn;
    TxPsnCount psnCount;
    TxLength messageLength;
    TxDmaSeq dmaSeq;
    Bit#(64) physicalAddr;
    Bit#(64) remoteAddr;
    TxLength len;
    Bit#(32) remoteKey;
    Bit#(1) isFirst;
    Bit#(1) isLast;
} TxDmaReadRequest deriving(Bits, Eq, FShow);

function TxMrLookupIndex txLocalLkeyMrIndex(TxLocalLkey lkey);
    return lkey[12:0];
endfunction

function TxMrKeyPart txLocalLkeyKeyPart(TxLocalLkey lkey);
    return lkey[31:13];
endfunction

function Bit#(13) txPmtuBytes(Bit#(3) pmtu);
    case (pmtu)
        3'd0: return 13'd256;
        3'd1: return 13'd512;
        3'd2: return 13'd1024;
        3'd3: return 13'd2048;
        3'd4: return 13'd4096;
        default: return 13'd0;
    endcase
endfunction

function Bit#(4) txPmtuShift(Bit#(3) pmtu);
    case (pmtu)
        3'd0: return 4'd8;
        3'd1: return 4'd9;
        3'd2: return 4'd10;
        3'd3: return 4'd11;
        3'd4: return 4'd12;
        default: return 4'd0;
    endcase
endfunction

function Bool txPmtuIsValid(Bit#(3) pmtu);
    return pmtu <= 3'd4;
endfunction

// Header-aware packet count for a message of `len` bytes at PMTU code `pmtu`.
// RoCEv2 first packet carries BTH+RETH (28 B) and continuation packets BTH
// (12 B); the PMTU budget includes headers. The numerical formula MUST stay
// aligned with packetCountForMessage in the Phase-A TxTypes package. M0's
// 16 MiB limit fits its 17-bit count; the wider Phase-A interface uses 18 bits
// and M0MessageAdapter zero-extends this value.
function TxPsnCount txPacketCount(TxLength len, Bit#(3) pmtu);
    Bit#(13) firstLimit = txPmtuBytes(pmtu) - 13'd28;
    Bit#(13) contLimit = txPmtuBytes(pmtu) - 13'd12;
    if (len == 0)
        return 0;
    else if (len <= zeroExtend(firstLimit))
        return 1;
    else begin
        Bit#(26) remaining = zeroExtend(len - zeroExtend(firstLimit));
        Bit#(26) divisor = zeroExtend(contLimit);
        Bit#(26) continuation = (remaining + divisor - 1) / divisor;
        return 17'd1 + truncate(continuation);
    end
endfunction

function Bool txPageShiftIsValid(Bit#(6) pageShift);
    return pageShift == 6'd12 || pageShift == 6'd21;
endfunction

function Bit#(64) txPageBytes(Bit#(6) pageShift);
    case (pageShift)
        6'd12: return 64'd4096;
        6'd21: return 64'd2097152;
        default: return 0;
    endcase
endfunction

function Bool txAddressOverflows(Bit#(64) base, TxLength len);
    Bit#(65) lastAddr = zeroExtend(base) + zeroExtend(len) - 1;
    return lastAddr[64] == 1'b1;
endfunction

endpackage
