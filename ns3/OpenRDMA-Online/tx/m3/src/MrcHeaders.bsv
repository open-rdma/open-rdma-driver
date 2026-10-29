package MrcHeaders;

import Vector :: *;

import MrcTxTypes :: *;
import TxTypes :: *;

typedef struct {
    Bit#(256) data;
    Bit#(32)  be;
} HeaderWord deriving(Bits, Eq, FShow);

typedef struct {
    Vector#(4, HeaderWord) words;
    Bit#(3)                count;
    Bit#(6)                lastWordBytes;
    Bit#(2)                padBytes;
} ComputedHeader deriving(Bits, Eq, FShow);

function Bit#(160) buildIpHeader(Bit#(16) totalLength, MacIpConfig cfg);
    return {
        cfg.dstIp[7:0], cfg.dstIp[15:8], cfg.dstIp[23:16], cfg.dstIp[31:24],
        cfg.srcIp[7:0], cfg.srcIp[15:8], cfg.srcIp[23:16], cfg.srcIp[31:24],
        8'h00, 8'h00,
        8'h11,
        8'd64,
        8'h00, 8'h40,
        16'h0000,
        totalLength[7:0], totalLength[15:8],
        8'h00, 8'h45
    };
endfunction

function Bit#(64) buildUdpHeader(
    Bit#(16) sourcePort, Bit#(16) destinationPort, Bit#(16) udpLength);
    return {
        16'h0000,
        udpLength[7:0], udpLength[15:8],
        destinationPort[7:0], destinationPort[15:8],
        sourcePort[7:0], sourcePort[15:8]
    };
endfunction

function Bit#(96) buildLegacyBth(
    PktOpcode opcode, TxPsn psn, TxQpn destQpn);
    return {
        8'h00,
        psn[7:0], psn[15:8], psn[23:16],
        8'h00,
        destQpn[7:0], destQpn[15:8], destQpn[23:16],
        8'hFF, 8'hFF,
        8'h00,
        pack(opcode)
    };
endfunction

function Bit#(96) buildMrcBth(
    PktOpcode opcode, TxPsn psn, TxQpn destQpn,
    Bit#(2) padBytes, Bit#(1) ackRequest, Bit#(1) retransmit);
    Bit#(8) padAndVersion = { 2'b00, padBytes, 4'b0000 };
    Bit#(8) flags = { ackRequest, 1'b0, retransmit, 1'b0, 4'b0000 };
    return {
        psn[7:0], psn[15:8], psn[23:16],
        flags,
        destQpn[7:0], destQpn[15:8], destQpn[23:16],
        8'h00,
        8'hFF, 8'hFF,
        padAndVersion,
        pack(opcode)
    };
endfunction

function Bit#(128) buildLegacyReth(PacketDescriptor descriptor);
    Bit#(32) messageLength = zeroExtend(descriptor.messageLength);
    return {
        messageLength[7:0], messageLength[15:8],
        messageLength[23:16], messageLength[31:24],
        descriptor.remoteAddr[7:0], descriptor.remoteAddr[15:8],
        descriptor.remoteAddr[23:16], descriptor.remoteAddr[31:24],
        descriptor.remoteAddr[39:32], descriptor.remoteAddr[47:40],
        descriptor.remoteAddr[55:48], descriptor.remoteAddr[63:56],
        descriptor.remoteKey[7:0], descriptor.remoteKey[15:8],
        descriptor.remoteKey[23:16], descriptor.remoteKey[31:24]
    };
endfunction

function Bit#(128) buildMrcReth(PacketDescriptor descriptor);
    Bit#(32) dmaLength = zeroExtend(descriptor.messageLength);
    return {
        dmaLength[7:0], dmaLength[15:8],
        dmaLength[23:16], dmaLength[31:24],
        descriptor.remoteKey[7:0], descriptor.remoteKey[15:8],
        descriptor.remoteKey[23:16], descriptor.remoteKey[31:24],
        descriptor.remoteAddr[7:0], descriptor.remoteAddr[15:8],
        descriptor.remoteAddr[23:16], descriptor.remoteAddr[31:24],
        descriptor.remoteAddr[39:32], descriptor.remoteAddr[47:40],
        descriptor.remoteAddr[55:48], descriptor.remoteAddr[63:56]
    };
endfunction

function Bit#(32) buildMrcMeth(TxMsn msn);
    // RQMSN is undefined for normal Write and is deterministically encoded as 0.
    return { msn[7:0], msn[15:8], 16'h0000 };
endfunction

function Bit#(112) buildMacHeader(MacIpConfig cfg);
    return {
        8'h00, 8'h08,
        cfg.srcMac[7:0], cfg.srcMac[15:8], cfg.srcMac[23:16],
        cfg.srcMac[31:24], cfg.srcMac[39:32], cfg.srcMac[47:40],
        cfg.dstMac[7:0], cfg.dstMac[15:8], cfg.dstMac[23:16],
        cfg.dstMac[31:24], cfg.dstMac[39:32], cfg.dstMac[47:40]
    };
endfunction

function Bit#(2) mrcPadBytes(Bit#(13) payloadLength);
    case (payloadLength[1:0])
        2'd0: return 0;
        2'd1: return 3;
        2'd2: return 2;
        default: return 1;
    endcase
endfunction

function ComputedHeader computeLegacyHeader(
    PacketDescriptor descriptor, MacIpConfig cfg);
    Bit#(1) firstLayout = pack(
        descriptor.opcode == OP_WRITE_ONLY ||
        descriptor.opcode == OP_WRITE_FIRST);
    Bit#(16) ipLength = zeroExtend(descriptor.payloadLength)
        + (firstLayout == 1 ? 16'd56 : 16'd40);
    Bit#(16) udpLength = zeroExtend(descriptor.payloadLength)
        + (firstLayout == 1 ? 16'd36 : 16'd20);

    Bit#(160) ip = buildIpHeader(ipLength, cfg);
    Bit#(16) checksum = ipv4Checksum({ ip[159:96], 16'h0000, ip[79:0] });
    Bit#(160) finalIp = {
        ip[159:96], checksum[7:0], checksum[15:8], ip[79:0]
    };
    Bit#(64) udp = buildUdpHeader(16'hC1E8, 16'd4791, udpLength);
    Bit#(96) bth = buildLegacyBth(
        descriptor.opcode, descriptor.psn, descriptor.destQpn);
    Bit#(128) reth = buildLegacyReth(descriptor);
    Bit#(112) mac = buildMacHeader(cfg);

    HeaderWord word0 = HeaderWord {
        data: { finalIp[143:0], mac },
        be: 32'hFFFFFFFF
    };
    HeaderWord word1 = HeaderWord {
        data: firstLayout == 1
            ? { reth[79:0], bth, udp, finalIp[159:144] }
            : { 80'd0, bth, udp, finalIp[159:144] },
        be: firstLayout == 1 ? 32'hFFFFFFFF : byteMask32(22)
    };
    HeaderWord word2 = HeaderWord {
        data: { 208'd0, reth[127:80] },
        be: byteMask32(6)
    };
    Vector#(4, HeaderWord) words = cons(
        word0, cons(word1, cons(word2, cons(unpack(0), nil))));
    return ComputedHeader {
        words: words,
        count: firstLayout == 1 ? 3 : 2,
        lastWordBytes: firstLayout == 1 ? 6 : 22,
        padBytes: 0
    };
endfunction

function ComputedHeader computeMrcHeader(
    PacketDescriptor descriptor, MacIpConfig cfg, MrcResolvedPath path);
    Bit#(2) padBytes = mrcPadBytes(descriptor.payloadLength);
    Bit#(16) ipLength = zeroExtend(descriptor.payloadLength) +
        zeroExtend(padBytes) + 16'd60;
    Bit#(16) udpLength = zeroExtend(descriptor.payloadLength) +
        zeroExtend(padBytes) + 16'd40;

    Bit#(160) ip = buildIpHeader(ipLength, cfg);
    Bit#(16) checksum = ipv4Checksum({ ip[159:96], 16'h0000, ip[79:0] });
    Bit#(160) finalIp = {
        ip[159:96], checksum[7:0], checksum[15:8], ip[79:0]
    };
    Bit#(64) udp = buildUdpHeader(
        truncate(path.entropy), 16'd4791, udpLength);
    Bit#(96) bth = buildMrcBth(
        descriptor.opcode, descriptor.psn, descriptor.destQpn,
        padBytes, descriptor.isLast,
        pack(descriptor.trafficClass == TRAFFIC_RETRY));
    Bit#(32) meth = buildMrcMeth(descriptor.mrcMsn);
    Bit#(128) reth = buildMrcReth(descriptor);
    Bit#(112) mac = buildMacHeader(cfg);

    HeaderWord word0 = HeaderWord {
        data: { finalIp[143:0], mac },
        be: 32'hFFFFFFFF
    };
    HeaderWord word1 = HeaderWord {
        data: { reth[47:0], meth, bth, udp, finalIp[159:144] },
        be: 32'hFFFFFFFF
    };
    HeaderWord word2 = HeaderWord {
        data: { 176'd0, reth[127:48] },
        be: byteMask32(10)
    };
    Vector#(4, HeaderWord) words = cons(
        word0, cons(word1, cons(word2, cons(unpack(0), nil))));
    return ComputedHeader {
        words: words,
        count: 3,
        lastWordBytes: 10,
        padBytes: padBytes
    };
endfunction

function ComputedHeader computeTxHeader(
    PacketDescriptor descriptor, MacIpConfig cfg, MrcResolvedPath path);
    if (descriptor.wireProfile == WIRE_MRC_IPV4_ECMP_NO_ICRC)
        return computeMrcHeader(descriptor, cfg, path);
    else
        return computeLegacyHeader(descriptor, cfg);
endfunction

endpackage
