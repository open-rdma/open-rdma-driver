// TxTypes: shared wire types for the Phase-A TX packet path.
//
// Byte i is stored in data[8*i +: 8]. Multi-byte protocol fields are placed
// on the wire in network byte order by the M3 encapsulator.

package TxTypes;

import Vector :: *;

// ---------------------------------------------------------------------------
// Basic widths
// ---------------------------------------------------------------------------

typedef Bit#(24) TxQpn;
typedef Bit#(18) TxSlotId;
typedef Bit#(24) TxPsn;
typedef Bit#(25) TxLength;
typedef Bit#(18) TxPsnCount;
typedef Bit#(16) TxRequestId;
typedef Bit#(3)  TxPmtuCode;
typedef Bit#(2)  TxEvProfileId;
typedef Bit#(16) TxMsn;

typedef enum {
    WIRE_LEGACY_PRIVATE          = 2'd0,
    WIRE_MRC_IPV4_ECMP_NO_ICRC  = 2'd1,
    WIRE_RESERVED_2              = 2'd2,
    WIRE_RESERVED_3              = 2'd3
} TxWireProfile deriving(Bits, Eq, FShow);

// ---------------------------------------------------------------------------
// Message-level context
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
    Bit#(64)       remoteAddr;
    Bit#(32)       remoteKey;
    TxLength       len;
    TxWireProfile  wireProfile;
    TxEvProfileId  evProfileId;
    TxMsn          mrcMsn;
    Bit#(1)        mrcMsnValid;
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

// Legacy private opcodes and the MRC normal-Write subset used by this TX MVP.
typedef enum {
    OP_WRITE_FIRST       = 8'h06,
    OP_WRITE_MIDDLE      = 8'h07,
    OP_WRITE_LAST        = 8'h08,
    OP_WRITE_ONLY        = 8'h09,
    OP_MRC_WRITE_FIRST   = 8'hC6,
    OP_MRC_WRITE_MIDDLE  = 8'hC7,
    OP_MRC_WRITE_LAST    = 8'hC8,
    OP_MRC_WRITE_ONLY    = 8'hCA
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
    Bit#(1)        isFirst;
    Bit#(1)        isLast;
    TxTrafficClass trafficClass;
    TxWireProfile  wireProfile;
    TxEvProfileId  evProfileId;
    TxMsn          mrcMsn;
    Bit#(1)        mrcMsnValid;
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

function Bool isSupportedWireProfile(TxWireProfile profile);
    return profile == WIRE_LEGACY_PRIVATE ||
           profile == WIRE_MRC_IPV4_ECMP_NO_ICRC;
endfunction

function Bit#(13) packetPayloadLimitForProfile(
    TxWireProfile profile, TxPmtuCode pmtu, Bit#(1) isFirst);
    if (profile == WIRE_MRC_IPV4_ECMP_NO_ICRC)
        return txPmtuBytes(pmtu);
    else
        return isFirst == 1 ? firstPayloadLimit(pmtu) : contPayloadLimit(pmtu);
endfunction

function TxPsnCount packetCountForProfile(
    TxLength len, TxPmtuCode pmtu, TxWireProfile profile);
    Bit#(13) firstLimit = packetPayloadLimitForProfile(profile, pmtu, 1);
    Bit#(13) contLimit = packetPayloadLimitForProfile(profile, pmtu, 0);
    if (len == 0 || firstLimit == 0 || contLimit == 0)
        return 0;
    else if (len <= zeroExtend(firstLimit))
        return 1;
    else begin
        Bit#(26) remaining = zeroExtend(len - zeroExtend(firstLimit));
        Bit#(26) divisor = zeroExtend(contLimit);
        Bit#(26) continuation = (remaining + divisor - 1) / divisor;
        return 18'd1 + truncate(continuation);
    end
endfunction

function TxPsnCount packetCountForMessage(TxLength len, TxPmtuCode pmtu);
    return packetCountForProfile(len, pmtu, WIRE_LEGACY_PRIVATE);
endfunction

function TxPsnCount packetCountForContext(MessageContext ctx);
    return packetCountForProfile(ctx.len, ctx.pmtu, ctx.wireProfile);
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

function PktOpcode packetOpcodeForProfile(
    TxWireProfile profile, Bit#(1) isFirst, Bit#(1) isLast);
    if (profile == WIRE_MRC_IPV4_ECMP_NO_ICRC) begin
        if (isFirst == 1 && isLast == 1)
            return OP_MRC_WRITE_ONLY;
        else if (isFirst == 1)
            return OP_MRC_WRITE_FIRST;
        else if (isLast == 1)
            return OP_MRC_WRITE_LAST;
        else
            return OP_MRC_WRITE_MIDDLE;
    end else
        return packetOpcode(isFirst, isLast);
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
    Vector#(10, Bit#(16)) words = newVector;
    for (Integer i = 0; i < 10; i = i + 1) begin
        Bit#(16) raw = header[(16 * i + 15) : (16 * i)];
        words[i] = { raw[7:0], raw[15:8] };
    end

    // Use a balanced tree instead of a ten-word accumulator chain. The full
    // unsigned sum fits in 20 bits; two end-around folds complete the one's-
    // complement reduction.
    Bit#(17) pair01 = zeroExtend(words[0]) + zeroExtend(words[1]);
    Bit#(17) pair23 = zeroExtend(words[2]) + zeroExtend(words[3]);
    Bit#(17) pair45 = zeroExtend(words[4]) + zeroExtend(words[5]);
    Bit#(17) pair67 = zeroExtend(words[6]) + zeroExtend(words[7]);
    Bit#(17) pair89 = zeroExtend(words[8]) + zeroExtend(words[9]);
    Bit#(18) quad03 = zeroExtend(pair01) + zeroExtend(pair23);
    Bit#(18) quad47 = zeroExtend(pair45) + zeroExtend(pair67);
    Bit#(19) oct07 = zeroExtend(quad03) + zeroExtend(quad47);
    Bit#(20) sum = zeroExtend(oct07) + zeroExtend(pair89);
    Bit#(17) fold1 = zeroExtend(sum[15:0]) + zeroExtend(sum[19:16]);
    Bit#(17) fold2 = zeroExtend(fold1[15:0]) + zeroExtend(fold1[16]);
    return ~fold2[15:0];
endfunction

endpackage
