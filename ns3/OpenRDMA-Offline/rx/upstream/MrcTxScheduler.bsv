// MrcTxScheduler: M3 packet-level four-channel scheduler and encapsulator.

package MrcTxScheduler;

import Assert :: *;
import BRAMCore :: *;
import Connectable :: *;
import FIFOF :: *;
import Vector :: *;

import CommonTypes :: *;
import TxTypes :: *;

// Payload data lives in a per-channel BRAM. Ready queues carry metadata only.
typedef struct {
    PacketDescriptor descriptor;
    MacIpConfig      portConfig;
    Bit#(1)          slot;
    Bit#(6)          beatCount;
} BufferedPacket deriving(Bits, Eq, FShow);

// ---------------------------------------------------------------------------
// Header builders
// ---------------------------------------------------------------------------

typedef struct {
    Bit#(256) data;
    Bit#(32)  be;
} HeaderWord deriving(Bits, Eq, FShow);

typedef struct {
    Vector#(3, HeaderWord) words;
    Bit#(2) count;
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

function Bit#(64) buildUdpHeader(Bit#(16) udpLength);
    return {
        16'h0000,
        udpLength[7:0], udpLength[15:8],
        8'hB7, 8'h12,
        8'hE8, 8'hC1
    };
endfunction

function Bit#(96) buildBth(PktOpcode opcode, TxPsn psn, TxQpn destQpn);
    return {
        8'h00,
        psn[7:0], psn[15:8], psn[23:16],
        8'h00,
        destQpn[7:0], destQpn[15:8], destQpn[23:16],
        8'hFF, 8'hFF,
        8'h00,
        zeroExtend(pack(opcode))
    };
endfunction

function Bit#(128) buildReth(PacketDescriptor descriptor);
    Bit#(32) messageLength = zeroExtend(descriptor.messageLength);
    // Only First/Only packets carry RETH, where messageOffset is zero.
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

function Bit#(112) buildMacHeader(MacIpConfig cfg);
    return {
        8'h00, 8'h08,
        cfg.srcMac[7:0], cfg.srcMac[15:8], cfg.srcMac[23:16],
        cfg.srcMac[31:24], cfg.srcMac[39:32], cfg.srcMac[47:40],
        cfg.dstMac[7:0], cfg.dstMac[15:8], cfg.dstMac[23:16],
        cfg.dstMac[31:24], cfg.dstMac[39:32], cfg.dstMac[47:40]
    };
endfunction

function ComputedHeader computeHeader(
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
    Bit#(64) udp = buildUdpHeader(udpLength);
    Bit#(96) bth = buildBth(
        descriptor.opcode, descriptor.psn, descriptor.destQpn);
    Bit#(128) reth = buildReth(descriptor);
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
    Vector#(3, HeaderWord) words =
        cons(word0, cons(word1, cons(word2, nil)));
    return ComputedHeader {
        words: words,
        count: firstLayout == 1 ? 2'd3 : 2'd2
    };
endfunction

// ---------------------------------------------------------------------------
// Per-channel encapsulator and 1024-to-256 gearbox
// ---------------------------------------------------------------------------

typedef enum { E_IDLE, E_PREFETCH, E_HEADER, E_PAYLOAD } EncoderState
    deriving(Bits, Eq, FShow);

interface ChannelEncapsulatorIfc;
    interface PipeIn#(BufferedPacket) packetIn;
    interface PipeOut#(EthPacket) frameOut;
    method Action writePayload(
        Bit#(1) slot, Bit#(5) beatIndex, Bit#(1024) data);
    method Bit#(1) packetCompleted;
endinterface

module mkChannelEncapsulator(ChannelEncapsulatorIfc);
    // Port A stores ingress beats while port B independently serves this channel.
    BRAM_DUAL_PORT#(Bit#(6), Bit#(1024)) payloadMem <- mkBRAMCore2(64, False);
    FIFOF#(BufferedPacket) packetQ <- mkSizedFIFOF(2);
    FIFOF#(EthPacket) frameQ <- mkSizedFIFOF(2);

    Reg#(EncoderState) state <- mkReg(E_IDLE);
    Reg#(BufferedPacket) current <- mkReg(unpack(0));
    Reg#(Vector#(3, HeaderWord)) headerWords <- mkReg(replicate(unpack(0)));
    Reg#(Bit#(2)) headerCount <- mkReg(0);
    Reg#(Bit#(2)) headerIndex <- mkReg(0);
    Reg#(Bit#(13)) payloadOffset <- mkReg(0);
    Reg#(Bit#(13)) payloadRemaining <- mkReg(0);
    Reg#(Bit#(1024)) currentBeat <- mkReg(0);
    Reg#(Bit#(1024)) nextBeat <- mkReg(0);
    Reg#(Bit#(5)) currentBeatIndex <- mkReg(0);
    Reg#(Bit#(7)) intraBeatOffset <- mkReg(0);
    Reg#(Bool) nextBeatValid <- mkReg(False);
    Reg#(Bool) readPending <- mkReg(False);
    Wire#(Bool) completedPulse <- mkDWire(False);

    rule loadPacket (state == E_IDLE && packetQ.notEmpty);
        let packet = packetQ.first;
        let header = computeHeader(packet.descriptor, packet.portConfig);
        dynamicAssert(packet.descriptor.payloadLength != 0,
            "M3 zero-payload packets are not implemented");
        dynamicAssert(packet.beatCount != 0,
            "M3 buffered packet has no payload beats");
        packetQ.deq;
        current <= packet;
        headerWords <= header.words;
        headerCount <= header.count;
        headerIndex <= 0;
        payloadOffset <= 0;
        payloadRemaining <= packet.descriptor.payloadLength;
        currentBeatIndex <= 0;
        intraBeatOffset <= 0;
        nextBeatValid <= False;
        payloadMem.b.put(False, { packet.slot, 5'd0 }, 0);
        readPending <= True;
        state <= E_PREFETCH;
    endrule

    rule captureFirstBeat (state == E_PREFETCH && readPending);
        currentBeat <= payloadMem.b.read;
        readPending <= False;
        state <= E_HEADER;
    endrule

    rule requestNextBeat (state != E_IDLE && state != E_PREFETCH &&
                          !readPending && !nextBeatValid &&
                          zeroExtend(currentBeatIndex) + 1 < current.beatCount);
        Bit#(5) requestedIndex = currentBeatIndex + 1;
        payloadMem.b.put(False, { current.slot, requestedIndex }, 0);
        readPending <= True;
    endrule

    rule captureNextBeat (state != E_IDLE && state != E_PREFETCH && readPending);
        nextBeat <= payloadMem.b.read;
        nextBeatValid <= True;
        readPending <= False;
    endrule

    Bool lastHeader = state == E_HEADER && headerIndex == headerCount - 1;
    Bit#(6) slotBytes = lastHeader
        ? (headerCount == 3 ? 6'd26 : 6'd10) : 0;
    Bit#(6) maximumTake = state == E_PAYLOAD ? 6'd32 : slotBytes;
    Bit#(6) take = payloadRemaining < zeroExtend(maximumTake)
        ? truncate(payloadRemaining) : maximumTake;
    Bit#(8) offsetAfterTake = zeroExtend(intraBeatOffset) + zeroExtend(take);
    Bool packetEnd = take != 0 && payloadRemaining == zeroExtend(take);
    Bool needsNextBeat = offsetAfterTake > 8'd128 ||
        (offsetAfterTake == 8'd128 && !packetEnd);

    rule emitFrameWord (state != E_IDLE && state != E_PREFETCH &&
                        frameQ.notFull && (!needsNextBeat || nextBeatValid));
        Bit#(6) outputOffset = state == E_HEADER ? 6'd32 - slotBytes : 0;
        HeaderWord header = state == E_HEADER
            ? headerWords[headerIndex] : unpack(0);
        Bit#(1024) highBeat = nextBeatValid ? nextBeat : 0;
        Bit#(2048) payloadWindow = { highBeat, currentBeat };
        Bit#(11) payloadShift = 8 * zeroExtend(intraBeatOffset);
        Bit#(256) payload = truncate(payloadWindow >> payloadShift);
        Bit#(9) outputShift = 8 * zeroExtend(outputOffset);
        Bit#(256) data = header.data | (payload << outputShift);
        Bit#(32) be = header.be | (byteMask32(take) << outputOffset);
        Bool frameStart = state == E_HEADER && headerIndex == 0;
        let descriptor = current.descriptor;

        frameQ.enq(EthPacket {
            data: data,
            be: be,
            sof: pack(frameStart),
            eof: pack(packetEnd),
            requestId: descriptor.requestId,
            qpn: descriptor.qpn,
            destQpn: descriptor.destQpn,
            psn: descriptor.psn,
            isFirst: descriptor.isFirst,
            isLast: descriptor.isLast,
            trafficClass: descriptor.trafficClass
        });

        if (packetEnd) begin
            completedPulse <= True;
            nextBeatValid <= False;
            state <= E_IDLE;
        end else begin
            payloadOffset <= payloadOffset + zeroExtend(take);
            payloadRemaining <= payloadRemaining - zeroExtend(take);
            if (offsetAfterTake >= 8'd128) begin
                currentBeat <= nextBeat;
                currentBeatIndex <= currentBeatIndex + 1;
                intraBeatOffset <= truncate(offsetAfterTake - 8'd128);
                nextBeatValid <= False;
            end else begin
                intraBeatOffset <= truncate(offsetAfterTake);
            end
            if (state == E_HEADER) begin
                if (lastHeader)
                    state <= E_PAYLOAD;
                else
                    headerIndex <= headerIndex + 1;
            end
        end
    endrule

    interface packetIn = toPipeIn(packetQ);
    interface frameOut = toPipeOut(frameQ);
    method Action writePayload(
        Bit#(1) slot, Bit#(5) beatIndex, Bit#(1024) data);
        payloadMem.a.put(True, { slot, beatIndex }, data);
    endmethod
    method Bit#(1) packetCompleted = pack(completedPulse);
endmodule


// ---------------------------------------------------------------------------
// Four-channel scheduler
// ---------------------------------------------------------------------------

function Bit#(2) chooseChannel(Bit#(2) start, Bit#(4) eligible);
    Bit#(2) selected = 0;
    case (start)
        2'd0: selected = eligible[0] == 1 ? 0 :
                         eligible[1] == 1 ? 1 :
                         eligible[2] == 1 ? 2 : 3;
        2'd1: selected = eligible[1] == 1 ? 1 :
                         eligible[2] == 1 ? 2 :
                         eligible[3] == 1 ? 3 : 0;
        2'd2: selected = eligible[2] == 1 ? 2 :
                         eligible[3] == 1 ? 3 :
                         eligible[0] == 1 ? 0 : 1;
        default: selected = eligible[3] == 1 ? 3 :
                            eligible[0] == 1 ? 0 :
                            eligible[1] == 1 ? 1 : 2;
    endcase
    return selected;
endfunction

interface FourChannelTxSchedulerIfc;
    interface PipeIn#(PacketDescriptor) firstDescriptorIn;
    interface PipeIn#(PacketPayloadBeat) firstPayloadIn;
    interface PipeIn#(PacketDescriptor) retryDescriptorIn;
    interface PipeIn#(PacketPayloadBeat) retryPayloadIn;
    interface PipeIn#(PacketDescriptor) orderedDescriptorIn;
    interface PipeIn#(PacketPayloadBeat) orderedPayloadIn;
    interface PipeIn#(PacketDescriptor) controlDescriptorIn;
    interface PipeIn#(PacketPayloadBeat) controlPayloadIn;

    interface PipeOut#(EthPacket) channel0Out;
    interface PipeOut#(EthPacket) channel1Out;
    interface PipeOut#(EthPacket) channel2Out;
    interface PipeOut#(EthPacket) channel3Out;
    interface PipeOut#(TxCommit) txCommitOut;
    interface ServerP#(MrcTxCfgReq, CfgResp) configServer;
endinterface

module mkFourChannelTxScheduler(FourChannelTxSchedulerIfc);
    FIFOF#(PacketDescriptor) firstDescQ <- mkSizedFIFOF(2);
    FIFOF#(PacketPayloadBeat) firstPayloadQ <- mkSizedFIFOF(2);
    FIFOF#(PacketDescriptor) retryDescQ <- mkSizedFIFOF(2);
    FIFOF#(PacketPayloadBeat) retryPayloadQ <- mkSizedFIFOF(2);
    FIFOF#(PacketDescriptor) orderedDescQ <- mkSizedFIFOF(2);
    FIFOF#(PacketPayloadBeat) orderedPayloadQ <- mkSizedFIFOF(2);
    FIFOF#(PacketDescriptor) controlDescQ <- mkSizedFIFOF(2);
    FIFOF#(PacketPayloadBeat) controlPayloadQ <- mkSizedFIFOF(2);

    FIFOF#(PacketPayloadBeat) selectedPayloadQ <- mkFIFOF;
    FIFOF#(TxCommit) commitQ <- mkSizedFIFOF(8);
    FIFOF#(MrcTxCfgReq) configReqQ <- mkSizedFIFOF(2);
    FIFOF#(CfgResp) configRespQ <- mkSizedFIFOF(2);

    ChannelEncapsulatorIfc encoder0 <- mkChannelEncapsulator;
    ChannelEncapsulatorIfc encoder1 <- mkChannelEncapsulator;
    ChannelEncapsulatorIfc encoder2 <- mkChannelEncapsulator;
    ChannelEncapsulatorIfc encoder3 <- mkChannelEncapsulator;

    Reg#(MacIpConfig) portConfig <- mkReg(unpack(0));
    Reg#(Bit#(4)) channelEnable <- mkReg(4'hF);
    Reg#(Bit#(4)) maxHighBurst <- mkReg(4'd4);
    Reg#(Bit#(4)) highBurst <- mkReg(0);
    Reg#(Bit#(2)) roundRobin <- mkReg(0);
    Reg#(Bit#(3)) accepted0 <- mkReg(0);
    Reg#(Bit#(3)) accepted1 <- mkReg(0);
    Reg#(Bit#(3)) accepted2 <- mkReg(0);
    Reg#(Bit#(3)) accepted3 <- mkReg(0);
    Reg#(Bit#(3)) completed0 <- mkReg(0);
    Reg#(Bit#(3)) completed1 <- mkReg(0);
    Reg#(Bit#(3)) completed2 <- mkReg(0);
    Reg#(Bit#(3)) completed3 <- mkReg(0);

    Reg#(Bool) active <- mkReg(False);
    Reg#(Bit#(2)) activeSource <- mkReg(0);
    Reg#(Bit#(2)) activeChannel <- mkReg(0);
    Reg#(Bit#(1)) activeSlot <- mkReg(0);
    Reg#(PacketDescriptor) ingressDescriptor <- mkReg(unpack(0));
    Reg#(MacIpConfig) ingressConfig <- mkReg(unpack(0));
    Reg#(Bit#(5)) ingressBeatIndex <- mkReg(0);
    Reg#(Bit#(13)) ingressBytes <- mkReg(0);
    Reg#(Bool) selectedEopQueued <- mkReg(False);

    rule applyConfig (configReqQ.notEmpty && configRespQ.notFull);
        let request = configReqQ.first;
        configReqQ.deq;
        portConfig <= request.cfg;
        channelEnable <= request.channelEnable;
        maxHighBurst <= request.maxHighBurst;
        configRespQ.enq(CfgResp { success: 1 });
    endrule

    Bit#(3) outstanding0 = accepted0 - completed0;
    Bit#(3) outstanding1 = accepted1 - completed1;
    Bit#(3) outstanding2 = accepted2 - completed2;
    Bit#(3) outstanding3 = accepted3 - completed3;
    Bit#(4) freeMask = {
        pack(encoder3.packetIn.notFull == 1 && outstanding3 < 2),
        pack(encoder2.packetIn.notFull == 1 && outstanding2 < 2),
        pack(encoder1.packetIn.notFull == 1 && outstanding1 < 2),
        pack(encoder0.packetIn.notFull == 1 && outstanding0 < 2)
    };
    Bit#(4) eligibleMask = channelEnable & freeMask;
    Bool haveChannel = eligibleMask != 0;
    Bit#(2) selectedChannel = chooseChannel(roundRobin, eligibleMask);
    Bit#(1) selectedSlot = selectedChannel == 0 ? accepted0[0] :
                           selectedChannel == 1 ? accepted1[0] :
                           selectedChannel == 2 ? accepted2[0] : accepted3[0];
    Bool forceFirst = firstDescQ.notEmpty && highBurst >= maxHighBurst;

    (* descending_urgency = "startControl, startOrdered, startRetry, startFirst" *)
    rule startControl (!active && haveChannel &&
                       controlDescQ.notEmpty && !forceFirst);
        let descriptor = controlDescQ.first;
        dynamicAssert(descriptor.trafficClass == TRAFFIC_CONTROL,
            "M3 control input has the wrong traffic class");
        controlDescQ.deq;
        active <= True;
        activeSource <= 3;
        activeChannel <= selectedChannel;
        activeSlot <= selectedSlot;
        ingressDescriptor <= descriptor;
        ingressConfig <= portConfig;
        ingressBeatIndex <= 0;
        ingressBytes <= 0;
        highBurst <= highBurst == '1 ? highBurst : highBurst + 1;
    endrule

    rule startOrdered (!active && haveChannel &&
                       !controlDescQ.notEmpty && orderedDescQ.notEmpty &&
                       !forceFirst);
        let descriptor = orderedDescQ.first;
        dynamicAssert(descriptor.trafficClass == TRAFFIC_ORDERED,
            "M3 ordered input has the wrong traffic class");
        orderedDescQ.deq;
        active <= True;
        activeSource <= 2;
        activeChannel <= selectedChannel;
        activeSlot <= selectedSlot;
        ingressDescriptor <= descriptor;
        ingressConfig <= portConfig;
        ingressBeatIndex <= 0;
        ingressBytes <= 0;
        highBurst <= highBurst == '1 ? highBurst : highBurst + 1;
    endrule

    rule startRetry (!active && haveChannel &&
                     !controlDescQ.notEmpty && !orderedDescQ.notEmpty &&
                     retryDescQ.notEmpty && !forceFirst);
        let descriptor = retryDescQ.first;
        dynamicAssert(descriptor.trafficClass == TRAFFIC_RETRY,
            "M3 retry input has the wrong traffic class");
        retryDescQ.deq;
        active <= True;
        activeSource <= 1;
        activeChannel <= selectedChannel;
        activeSlot <= selectedSlot;
        ingressDescriptor <= descriptor;
        ingressConfig <= portConfig;
        ingressBeatIndex <= 0;
        ingressBytes <= 0;
        highBurst <= highBurst == '1 ? highBurst : highBurst + 1;
    endrule

    rule startFirst (!active && haveChannel &&
                     firstDescQ.notEmpty &&
                     (forceFirst || (!controlDescQ.notEmpty &&
                      !orderedDescQ.notEmpty && !retryDescQ.notEmpty)));
        let descriptor = firstDescQ.first;
        dynamicAssert(descriptor.trafficClass == TRAFFIC_FIRST,
            "M3 first input has the wrong traffic class");
        firstDescQ.deq;
        active <= True;
        activeSource <= 0;
        activeChannel <= selectedChannel;
        activeSlot <= selectedSlot;
        ingressDescriptor <= descriptor;
        ingressConfig <= portConfig;
        ingressBeatIndex <= 0;
        ingressBytes <= 0;
        highBurst <= 0;
    endrule

    rule selectFirstPayload (active && activeSource == 0 &&
                             firstPayloadQ.notEmpty && selectedPayloadQ.notFull &&
                             !selectedEopQueued);
        let beat = firstPayloadQ.first;
        selectedPayloadQ.enq(beat);
        firstPayloadQ.deq;
        if (beat.eop == 1)
            selectedEopQueued <= True;
    endrule

    rule selectRetryPayload (active && activeSource == 1 &&
                             retryPayloadQ.notEmpty && selectedPayloadQ.notFull &&
                             !selectedEopQueued);
        let beat = retryPayloadQ.first;
        selectedPayloadQ.enq(beat);
        retryPayloadQ.deq;
        if (beat.eop == 1)
            selectedEopQueued <= True;
    endrule

    rule selectOrderedPayload (active && activeSource == 2 &&
                               orderedPayloadQ.notEmpty && selectedPayloadQ.notFull &&
                               !selectedEopQueued);
        let beat = orderedPayloadQ.first;
        selectedPayloadQ.enq(beat);
        orderedPayloadQ.deq;
        if (beat.eop == 1)
            selectedEopQueued <= True;
    endrule

    rule selectControlPayload (active && activeSource == 3 &&
                               controlPayloadQ.notEmpty && selectedPayloadQ.notFull &&
                               !selectedEopQueued);
        let beat = controlPayloadQ.first;
        selectedPayloadQ.enq(beat);
        controlPayloadQ.deq;
        if (beat.eop == 1)
            selectedEopQueued <= True;
    endrule

    rule storePayload (active && selectedPayloadQ.notEmpty &&
                       selectedPayloadQ.first.eop == 0);
        let beat = selectedPayloadQ.first;
        selectedPayloadQ.deq;
        Bit#(8) validBytes = pack(countOnes(beat.keep));
        dynamicAssert(beat.requestId == ingressDescriptor.requestId &&
                beat.psn == ingressDescriptor.psn,
            "M3 descriptor/payload identity mismatch");
        dynamicAssert(beat.sop == pack(ingressBeatIndex == 0),
            "M3 malformed packet SOP");
        dynamicAssert(beat.keep == '1,
            "M3 non-EOP payload beat must be full width");
        dynamicAssert(ingressBeatIndex != 31,
            "M3 packet exceeds 32 payload beats");
        case (activeChannel)
            0: encoder0.writePayload(activeSlot, ingressBeatIndex, beat.data);
            1: encoder1.writePayload(activeSlot, ingressBeatIndex, beat.data);
            2: encoder2.writePayload(activeSlot, ingressBeatIndex, beat.data);
            default: encoder3.writePayload(activeSlot, ingressBeatIndex, beat.data);
        endcase
        ingressBeatIndex <= ingressBeatIndex + 1;
        ingressBytes <= ingressBytes + zeroExtend(validBytes);
    endrule

    Bool activePacketQueueReady = activeChannel == 0
        ? encoder0.packetIn.notFull == 1 : activeChannel == 1
        ? encoder1.packetIn.notFull == 1 : activeChannel == 2
        ? encoder2.packetIn.notFull == 1 : encoder3.packetIn.notFull == 1;

    rule finishPayload (active && selectedPayloadQ.notEmpty &&
                        selectedPayloadQ.first.eop == 1 &&
                        activePacketQueueReady && commitQ.notFull);
        let beat = selectedPayloadQ.first;
        selectedPayloadQ.deq;
        Bit#(8) validBytes = pack(countOnes(beat.keep));
        dynamicAssert(beat.requestId == ingressDescriptor.requestId &&
                beat.psn == ingressDescriptor.psn,
            "M3 descriptor/payload identity mismatch");
        dynamicAssert(beat.sop == pack(ingressBeatIndex == 0),
            "M3 malformed packet SOP");
        dynamicAssert(isPrefixKeep128(beat.keep),
            "M3 EOP keep must be prefix-contiguous");
        dynamicAssert(ingressBytes + zeroExtend(validBytes) ==
                zeroExtend(ingressDescriptor.payloadLength),
            "M3 packet payload length mismatch");

        BufferedPacket packet = BufferedPacket {
            descriptor: ingressDescriptor,
            portConfig: ingressConfig,
            slot: activeSlot,
            beatCount: zeroExtend(ingressBeatIndex) + 1
        };
        case (activeChannel)
            0: begin
                encoder0.writePayload(activeSlot, ingressBeatIndex, beat.data);
                encoder0.packetIn.enq(packet);
                accepted0 <= accepted0 + 1;
                roundRobin <= 1;
            end
            1: begin
                encoder1.writePayload(activeSlot, ingressBeatIndex, beat.data);
                encoder1.packetIn.enq(packet);
                accepted1 <= accepted1 + 1;
                roundRobin <= 2;
            end
            2: begin
                encoder2.writePayload(activeSlot, ingressBeatIndex, beat.data);
                encoder2.packetIn.enq(packet);
                accepted2 <= accepted2 + 1;
                roundRobin <= 3;
            end
            default: begin
                encoder3.writePayload(activeSlot, ingressBeatIndex, beat.data);
                encoder3.packetIn.enq(packet);
                accepted3 <= accepted3 + 1;
                roundRobin <= 0;
            end
        endcase
        commitQ.enq(TxCommit {
            requestId: ingressDescriptor.requestId,
            qpn: ingressDescriptor.qpn,
            psn: ingressDescriptor.psn,
            packetIndex: ingressDescriptor.packetIndex,
            channel: activeChannel,
            trafficClass: ingressDescriptor.trafficClass
        });
        active <= False;
        selectedEopQueued <= False;
    endrule

    rule releaseChannel0 (encoder0.packetCompleted == 1);
        completed0 <= completed0 + 1;
    endrule

    rule releaseChannel1 (encoder1.packetCompleted == 1);
        completed1 <= completed1 + 1;
    endrule

    rule releaseChannel2 (encoder2.packetCompleted == 1);
        completed2 <= completed2 + 1;
    endrule

    rule releaseChannel3 (encoder3.packetCompleted == 1);
        completed3 <= completed3 + 1;
    endrule

    interface firstDescriptorIn = toPipeIn(firstDescQ);
    interface firstPayloadIn = toPipeIn(firstPayloadQ);
    interface retryDescriptorIn = toPipeIn(retryDescQ);
    interface retryPayloadIn = toPipeIn(retryPayloadQ);
    interface orderedDescriptorIn = toPipeIn(orderedDescQ);
    interface orderedPayloadIn = toPipeIn(orderedPayloadQ);
    interface controlDescriptorIn = toPipeIn(controlDescQ);
    interface controlPayloadIn = toPipeIn(controlPayloadQ);

    interface channel0Out = encoder0.frameOut;
    interface channel1Out = encoder1.frameOut;
    interface channel2Out = encoder2.frameOut;
    interface channel3Out = encoder3.frameOut;
    interface txCommitOut = toPipeOut(commitQ);
    interface ServerP configServer;
        interface request = toPipeIn(configReqQ);
        interface response = toPipeOut(configRespQ);
    endinterface
endmodule

endpackage
