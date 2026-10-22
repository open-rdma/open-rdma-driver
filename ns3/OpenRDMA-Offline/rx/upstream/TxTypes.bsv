// TxTypes: shared wire types for the Phase-A TX packet path.
//
// Byte i is stored in data[8*i +: 8]. Multi-byte protocol fields are placed
// on the wire in network byte order by the M3 encapsulator.

package TxTypes;

// ---------------------------------------------------------------------------
// Basic widths
// ---------------------------------------------------------------------------

typedef Bit#(24) TxQpn;
typedef Bit#(18) TxSlotId;
typedef Bit#(24) TxPsn;
typedef Bit#(25) TxLength;
typedef Bit#(17) TxPsnCount;
typedef Bit#(16) TxRequestId;
typedef Bit#(16) TxDmaSeq;
typedef Bit#(3)  TxPmtuCode;

// ---------------------------------------------------------------------------
// M0 compatibility types
// ---------------------------------------------------------------------------

typedef struct {
    TxRequestId requestId;
    TxQpn       qpn;
    TxQpn       destQpn;
    TxSlotId    slotId;
    Bit#(4)     debugSeq;
    TxPmtuCode  pmtu;
    TxPsn       startPsn;
    TxPsnCount  psnCount;
    TxDmaSeq    dmaSeq;
    Bit#(64)    physicalAddr;
    Bit#(64)    remoteAddr;
    TxLength    len;
    Bit#(32)    remoteKey;
    Bit#(1)     isFirst;
    Bit#(1)     isLast;
} TxDmaReadRequest deriving(Bits, Eq, FShow);

typedef struct {
    TxRequestId requestId;
    TxQpn       qpn;
    TxQpn       destQpn;
    TxSlotId    slotId;
    Bit#(4)     debugSeq;
    TxPmtuCode  pmtu;
    TxPsn       startPsn;
    TxPsnCount  psnCount;
    Bit#(64)    remoteAddr;
    Bit#(32)    remoteKey;
    TxLength    len;
} MessageContext deriving(Bits, Eq, FShow);

// ---------------------------------------------------------------------------
// M1 -> M2 and M2 -> M3 contracts
// ---------------------------------------------------------------------------

// M1 emits one globally ordered message stream. keep must be a non-zero,
// prefix-contiguous mask; every non-EOP beat is expected to be full width.
typedef struct {
    TxRequestId requestId;
    TxLength    messageOffset;
    Bit#(1024)  data;
    Bit#(128)   keep;
    Bit#(1)     messageSop;
    Bit#(1)     messageEop;
} MessagePayloadBeat deriving(Bits, Eq, FShow);

typedef enum {
    TRAFFIC_FIRST   = 2'd0,
    TRAFFIC_RETRY   = 2'd1,
    TRAFFIC_ORDERED = 2'd2,
    TRAFFIC_CONTROL = 2'd3
} TxTrafficClass deriving(Bits, Eq, FShow);

// IBTA BTH opcodes used by the MVP RDMA Write path.
typedef enum {
    OP_WRITE_FIRST  = 8'h06,
    OP_WRITE_MIDDLE = 8'h07,
    OP_WRITE_LAST   = 8'h08,
    OP_WRITE_ONLY   = 8'h09
} PktOpcode deriving(Bits, Eq, FShow);

typedef struct {
    TxRequestId   requestId;
    TxQpn         qpn;
    TxQpn         destQpn;
    TxSlotId      slotId;
    Bit#(4)       debugSeq;
    TxPmtuCode    pmtu;
    TxPsn         psn;
    TxPsnCount    packetIndex;
    TxPsnCount    packetCount;
    PktOpcode     opcode;
    Bit#(64)      remoteAddr;
    Bit#(32)      remoteKey;
    TxLength      messageOffset;
    Bit#(13)      payloadLength;
    TxLength      messageLength;
    Bit#(1)       isFirst;
    Bit#(1)       isLast;
    TxTrafficClass trafficClass;
} PacketDescriptor deriving(Bits, Eq, FShow);

typedef struct {
    TxRequestId requestId;
    TxPsn       psn;
    Bit#(1024)  data;
    Bit#(128)   keep;
    Bit#(1)     sop;
    Bit#(1)     eop;
} PacketPayloadBeat deriving(Bits, Eq, FShow);

// M3 -> physical channel frame stream. The metadata is monitoring sideband;
// data/be/sof/eof are the actual channel payload contract.
typedef struct {
    Bit#(256)    data;
    Bit#(32)     be;
    Bit#(1)      sof;
    Bit#(1)      eof;
    TxRequestId  requestId;
    TxQpn        qpn;
    TxQpn        destQpn;
    TxPsn        psn;
    Bit#(1)      isFirst;
    Bit#(1)      isLast;
    TxTrafficClass trafficClass;
} EthPacket deriving(Bits, Eq, FShow);

typedef struct {
    TxRequestId   requestId;
    TxQpn         qpn;
    TxPsn         psn;
    TxPsnCount    packetIndex;
    Bit#(2)       channel;
    TxTrafficClass trafficClass;
} TxCommit deriving(Bits, Eq, FShow);

// ---------------------------------------------------------------------------
// M3 configuration
// ---------------------------------------------------------------------------

typedef struct {
    Bit#(48) srcMac;
    Bit#(48) dstMac;
    Bit#(32) srcIp;
    Bit#(32) dstIp;
} MacIpConfig deriving(Bits, Eq, FShow);

typedef struct {
    MacIpConfig cfg;
    Bit#(4)     channelEnable;
    Bit#(4)     maxHighBurst;
} MrcTxCfgReq deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(1) success;
} CfgResp deriving(Bits, Eq, FShow);

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

function Bit#(13) txPmtuBytes(TxPmtuCode pmtu);
    case (pmtu)
        3'd0: return 13'd256;
        3'd1: return 13'd512;
        3'd2: return 13'd1024;
        3'd3: return 13'd2048;
        3'd4: return 13'd4096;
        default: return 13'd0;
    endcase
endfunction

function Bit#(13) firstPayloadLimit(TxPmtuCode pmtu);
    return txPmtuBytes(pmtu) - 13'd28;
endfunction

function Bit#(13) contPayloadLimit(TxPmtuCode pmtu);
    return txPmtuBytes(pmtu) - 13'd12;
endfunction

function TxPsnCount packetCountForMessage(TxLength len, TxPmtuCode pmtu);
    Bit#(13) firstLimit = firstPayloadLimit(pmtu);
    Bit#(13) contLimit = contPayloadLimit(pmtu);
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

function PktOpcode packetOpcode(Bit#(1) isFirst, Bit#(1) isLast);
    if (isFirst == 1 && isLast == 1)
        return OP_WRITE_ONLY;
    else if (isFirst == 1)
        return OP_WRITE_FIRST;
    else if (isLast == 1)
        return OP_WRITE_LAST;
    else
        return OP_WRITE_MIDDLE;
endfunction

function Bit#(32) byteMask32(Bit#(6) n);
    Bit#(33) shift = zeroExtend(n);
    return truncate((33'h1 << shift) - 33'h1);
endfunction

function Bit#(128) byteMask128(Bit#(8) n);
    Bit#(129) shift = zeroExtend(n);
    return truncate((129'h1 << shift) - 129'h1);
endfunction

function Bool isPrefixKeep128(Bit#(128) keep);
    return keep != 0 && ((keep & (keep + 1)) == 0);
endfunction

function Bit#(16) ipv4Checksum(Bit#(160) header);
    Bit#(20) sum = 20'd0;
    for (Integer i = 0; i < 10; i = i + 1) begin
        Bit#(16) raw = header[(16 * i + 15) : (16 * i)];
        Bit#(16) slice = { raw[7:0], raw[15:8] };
        sum = sum + zeroExtend(slice);
    end
    Bit#(17) fold = zeroExtend(sum[15:0]) + zeroExtend(sum[19:16]);
    Bit#(16) result = fold[16] == 1 ? truncate(fold) + 1 : truncate(fold);
    return ~result;
endfunction

endpackage
