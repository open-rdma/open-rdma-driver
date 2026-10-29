// MrcTxScheduler: M3 packet-level four-channel scheduler and encapsulator.

package MrcTxScheduler;

import Assert :: *;
import BRAMCore :: *;
import Connectable :: *;
import FIFOF :: *;
import Vector :: *;

import CommonTypes :: *;
import EvPathManager :: *;
import MrcHeaders :: *;
import MrcTxTypes :: *;
import TxTypes :: *;

// Payload data lives in a per-channel BRAM. Ready queues carry metadata only.
typedef struct {
    PacketDescriptor descriptor;
    MacIpConfig      portConfig;
    MrcResolvedPath  path;
    Bit#(1)          slot;
    Bit#(6)          beatCount;
} BufferedPacket deriving(Bits, Eq, FShow);

// ---------------------------------------------------------------------------
// Per-channel encapsulator and 1024-to-256 gearbox
// ---------------------------------------------------------------------------

typedef enum { E_IDLE, E_PREFETCH, E_HEADER, E_PAYLOAD, E_TRAILER } EncoderState
    deriving(Bits, Eq, FShow);

interface ChannelEncapsulatorIfc;
    interface PipeIn#(BufferedPacket) packetIn;
    interface PipeOut#(EthPacket) frameOut;
    interface PipeOut#(TxEgressEvent) egressOut;
    method Action writePayload(
        Bit#(1) slot, Bit#(5) beatIndex, Bit#(1024) data);
    method Bit#(1) packetCompleted;
endinterface

module mkChannelEncapsulator#(Bit#(2) channel)(ChannelEncapsulatorIfc);
    // Port A stores ingress beats while port B independently serves this channel.
    BRAM_DUAL_PORT#(Bit#(6), Bit#(1024)) payloadMem <- mkBRAMCore2(64, False);
    FIFOF#(BufferedPacket) packetQ <- mkSizedFIFOF(2);
    FIFOF#(EthPacket) frameQ <- mkSizedFIFOF(2);
    FIFOF#(TxEgressEvent) egressQ <- mkSizedFIFOF(2);

    Reg#(EncoderState) state <- mkReg(E_IDLE);
    Reg#(BufferedPacket) current <- mkReg(unpack(0));
    Reg#(Vector#(4, HeaderWord)) headerWords <- mkReg(replicate(unpack(0)));
    Reg#(Bit#(3)) headerCount <- mkReg(0);
    Reg#(Bit#(3)) headerIndex <- mkReg(0);
    Reg#(Bit#(6)) lastHeaderBytes <- mkReg(0);
    Reg#(Bit#(2)) padRemaining <- mkReg(0);
    Reg#(Bit#(13)) payloadRemaining <- mkReg(0);
    Reg#(Bit#(1024)) currentBeat <- mkReg(0);
    Reg#(Bit#(1024)) nextBeat <- mkReg(0);
    Reg#(Bit#(5)) currentBeatIndex <- mkReg(0);
    Reg#(Bit#(7)) intraBeatOffset <- mkReg(0);
    Reg#(Bool) nextBeatValid <- mkReg(False);
    Reg#(Bool) readPending <- mkReg(False);
    Wire#(Bool) completedPulse <- mkDWire(False);
    // Egress events are produced when an EOF word is handed to `frameQ` but
    // become visible only once that word has been dequeued, so the event means
    // "this frame left the buffer" rather than "this frame was assembled".
    //
    // The release is tracked as released-vs-taken rather than emitted-vs-taken:
    // a single emitted/taken pair would let the consumer dequeue the EOF word
    // before the scheduler picks the event up, cancel the credit, and strand
    // the event in `egressQ` forever. Both counters advance only on their own
    // side of the handshake, so `releasedEofs != eofsTaken` is exactly "a
    // released event is waiting".
    Reg#(Bit#(3)) releasedEofs <- mkReg(0);
    Reg#(Bit#(3)) eofsTaken <- mkReg(0);
    Bit#(1) egressReady = pack(releasedEofs != eofsTaken);

    rule loadPacket (state == E_IDLE && packetQ.notEmpty);
        let packet = packetQ.first;
        let header = computeTxHeader(
            packet.descriptor, packet.portConfig, packet.path);
        Bool isMrc = packet.descriptor.wireProfile ==
            WIRE_MRC_IPV4_ECMP_NO_ICRC;
        dynamicAssert(packet.descriptor.payloadLength != 0,
            "M3 zero-payload packets are not implemented");
        dynamicAssert(packet.beatCount != 0,
            "M3 buffered packet has no payload beats");
        dynamicAssert(!isMrc || packet.descriptor.mrcMsnValid == 1,
            "M3 MRC packet has no valid MSN");
        dynamicAssert(!isMrc ||
                packet.descriptor.opcode == OP_MRC_WRITE_FIRST ||
                packet.descriptor.opcode == OP_MRC_WRITE_MIDDLE ||
                packet.descriptor.opcode == OP_MRC_WRITE_LAST ||
                packet.descriptor.opcode == OP_MRC_WRITE_ONLY,
            "M3 unsupported MRC data opcode");
        packetQ.deq;
        current <= packet;
        headerWords <= header.words;
        headerCount <= header.count;
        headerIndex <= 0;
        lastHeaderBytes <= header.lastWordBytes;
        padRemaining <= header.padBytes;
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
                          state != E_TRAILER && payloadRemaining != 0 &&
                          !readPending && !nextBeatValid &&
                          zeroExtend(currentBeatIndex) + 1 < current.beatCount);
        Bit#(5) requestedIndex = currentBeatIndex + 1;
        payloadMem.b.put(False, { current.slot, requestedIndex }, 0);
        readPending <= True;
    endrule

    rule captureNextBeat (state != E_IDLE && state != E_PREFETCH &&
                          state != E_TRAILER && readPending);
        nextBeat <= payloadMem.b.read;
        nextBeatValid <= True;
        readPending <= False;
    endrule

    Bool lastHeader = state == E_HEADER && headerIndex == headerCount - 1;
    Bit#(6) payloadSlots = lastHeader ? 6'd32 - lastHeaderBytes :
        state == E_PAYLOAD ? 6'd32 : 0;
    Bit#(6) maximumPayloadTake = payloadSlots;
    Bit#(6) payloadTake = payloadRemaining < zeroExtend(maximumPayloadTake)
        ? truncate(payloadRemaining) : maximumPayloadTake;
    Bit#(8) offsetAfterTake = zeroExtend(intraBeatOffset) +
        zeroExtend(payloadTake);
    Bool payloadEnd = payloadTake != 0 &&
        payloadRemaining == zeroExtend(payloadTake);
    // Only the final header word leaves room for payload behind the header
    // bytes; earlier header words shift the payload window entirely out.
    Bit#(6) outputOffset = state == E_HEADER
        ? (lastHeader ? lastHeaderBytes : 6'd32) : 0;
    Bit#(6) availableForPad = 6'd32 - outputOffset - payloadTake;
    Bit#(6) maximumPadTake = (payloadEnd || state == E_TRAILER)
        ? availableForPad : 0;
    Bit#(6) extendedPadRemaining = zeroExtend(padRemaining);
    Bit#(6) padTake = extendedPadRemaining < maximumPadTake
        ? extendedPadRemaining : maximumPadTake;
    Bool frameEnd = (payloadEnd || state == E_TRAILER) &&
        extendedPadRemaining == padTake;
    Bool needsNextBeat = payloadTake != 0 &&
        (offsetAfterTake > 8'd128 ||
         (offsetAfterTake == 8'd128 && !payloadEnd));

    rule emitFrameWord (state != E_IDLE && state != E_PREFETCH &&
                        frameQ.notFull && (!needsNextBeat || nextBeatValid) &&
                        (!frameEnd || egressQ.notFull));
        HeaderWord header = state == E_HEADER
            ? headerWords[headerIndex] : unpack(0);
        Bit#(256) nextLane = nextBeatValid ? nextBeat[255:0] : 0;
        Bit#(512) laneWindow = case (intraBeatOffset[6:5])
            2'd0: currentBeat[511:0];
            2'd1: currentBeat[767:256];
            2'd2: currentBeat[1023:512];
            default: { nextLane, currentBeat[1023:768] };
        endcase;
        Bit#(9) payloadShift = 8 * zeroExtend(intraBeatOffset[4:0]);
        Bit#(256) payload = truncate(laneWindow >> payloadShift);
        Bit#(9) outputShift = 8 * zeroExtend(outputOffset);
        Bit#(256) data = header.data | (payload << outputShift);
        Bit#(32) payloadBe = byteMask32(payloadTake) << outputOffset;
        Bit#(32) padBe = byteMask32(padTake) <<
            (outputOffset + payloadTake);
        Bit#(32) be = header.be | payloadBe | padBe;
        Bool frameStart = state == E_HEADER && headerIndex == 0;
        let descriptor = current.descriptor;

        frameQ.enq(EthPacket {
            data: data,
            be: be,
            sof: pack(frameStart),
            eof: pack(frameEnd),
            requestId: descriptor.requestId,
            qpn: descriptor.qpn,
            destQpn: descriptor.destQpn,
            psn: descriptor.psn,
            isFirst: descriptor.isFirst,
            isLast: descriptor.isLast,
            trafficClass: descriptor.trafficClass
        });

        if (frameEnd) begin
            egressQ.enq(TxEgressEvent {
                requestId: descriptor.requestId,
                qpn: descriptor.qpn,
                psn: descriptor.psn,
                packetIndex: descriptor.packetIndex,
                channel: channel,
                trafficClass: descriptor.trafficClass,
                evValid: pack(descriptor.wireProfile ==
                    WIRE_MRC_IPV4_ECMP_NO_ICRC),
                profileId: current.path.profileId,
                evIndex: current.path.entryIndex,
                evGeneration: current.path.generation,
                entropy: current.path.entropy
            });
            completedPulse <= True;
            nextBeatValid <= False;
            padRemaining <= 0;
            state <= E_IDLE;
        end else begin
            if (payloadTake != 0) begin
                payloadRemaining <= payloadRemaining - zeroExtend(payloadTake);
                if (offsetAfterTake >= 8'd128) begin
                    currentBeat <= nextBeat;
                    currentBeatIndex <= currentBeatIndex + 1;
                    intraBeatOffset <= truncate(offsetAfterTake - 8'd128);
                    nextBeatValid <= False;
                end else begin
                    intraBeatOffset <= truncate(offsetAfterTake);
                end
            end
            if (padTake != 0)
                padRemaining <= truncate(extendedPadRemaining - padTake);

            if (state == E_HEADER) begin
                if (lastHeader) begin
                    if (payloadEnd)
                        state <= E_TRAILER;
                    else
                        state <= E_PAYLOAD;
                end else begin
                    headerIndex <= headerIndex + 1;
                end
            end else if (state == E_PAYLOAD && payloadEnd) begin
                state <= E_TRAILER;
            end
        end
    endrule

    interface packetIn = toPipeIn(packetQ);
    interface PipeOut frameOut;
        method EthPacket first if (frameQ.notEmpty);
            return frameQ.first;
        endmethod
        method Action deq if (frameQ.notEmpty);
            let word = frameQ.first;
            frameQ.deq;
            if (word.eof == 1)
                releasedEofs <= releasedEofs + 1;
        endmethod
        method Bit#(1) notEmpty = pack(frameQ.notEmpty);
    endinterface
    interface PipeOut egressOut;
        method TxEgressEvent first if (egressQ.notEmpty);
            return egressQ.first;
        endmethod
        method Action deq if (egressQ.notEmpty && egressReady == 1);
            egressQ.deq;
            eofsTaken <= eofsTaken + 1;
        endmethod
        method Bit#(1) notEmpty =
            pack(egressQ.notEmpty && egressReady == 1);
    endinterface
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

function Bool packetDescriptorIsWellFormed(PacketDescriptor descriptor);
    Bool firstMatches = descriptor.isFirst == pack(descriptor.packetIndex == 0);
    Bool lastMatches = descriptor.packetCount != 0 &&
        descriptor.packetIndex < descriptor.packetCount &&
        descriptor.isLast == pack(
            descriptor.packetIndex + 1 == descriptor.packetCount);
    Bool opcodeMatches = isSupportedWireProfile(descriptor.wireProfile) &&
        descriptor.opcode == packetOpcodeForProfile(
            descriptor.wireProfile, descriptor.isFirst, descriptor.isLast);
    Bit#(13) payloadLimit = packetPayloadLimitForProfile(
        descriptor.wireProfile, descriptor.pmtu, descriptor.isFirst);
    Bool payloadMatches = descriptor.payloadLength != 0 &&
        descriptor.payloadLength <= payloadLimit &&
        (descriptor.isLast == 1 || descriptor.payloadLength == payloadLimit);
    Bit#(26) payloadEnd = zeroExtend(descriptor.messageOffset) +
        zeroExtend(descriptor.payloadLength);
    Bool messageRangeMatches = descriptor.messageLength != 0 &&
        payloadEnd <= zeroExtend(descriptor.messageLength) &&
        descriptor.isFirst == pack(descriptor.messageOffset == 0);
    return descriptor.pmtu <= 4 && firstMatches && lastMatches &&
        opcodeMatches && payloadMatches && messageRangeMatches;
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
    interface PipeOut#(TxEgressEvent) txEgressOut;
    interface PipeIn#(MrcSourceProfileConfig) sourceRouteConfigIn;
    interface PipeIn#(MrcEvEntryConfig) evEntryConfigIn;
    interface PipeIn#(MrcEvPolicyConfig) evPolicyConfigIn;
    interface PipeIn#(MrcPortStatusUpdate) portStatusIn;
    interface PipeIn#(MrcPathFeedback) pathFeedbackIn;
    interface PipeOut#(MrcProbeRequest) probeOut;
    interface PipeOut#(MrcNoPathEvent) noPathOut;
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
    FIFOF#(TxEgressEvent) egressQ <- mkSizedFIFOF(8);
    FIFOF#(MrcNoPathEvent) noPathQ <- mkSizedFIFOF(4);
    FIFOF#(MrcTxCfgReq) configReqQ <- mkSizedFIFOF(2);
    FIFOF#(CfgResp) configRespQ <- mkSizedFIFOF(2);

    ChannelEncapsulatorIfc encoder0 <- mkChannelEncapsulator(2'd0);
    ChannelEncapsulatorIfc encoder1 <- mkChannelEncapsulator(2'd1);
    ChannelEncapsulatorIfc encoder2 <- mkChannelEncapsulator(2'd2);
    ChannelEncapsulatorIfc encoder3 <- mkChannelEncapsulator(2'd3);
    EvPathManagerIfc pathManager <- mkEvPathManager;

    Reg#(MacIpConfig) portConfig <- mkReg(unpack(0));
    Reg#(Bit#(4)) channelEnable <- mkReg(4'hF);
    Reg#(MrcSourceProfileConfig) sourceRoute <- mkReg(MrcSourceProfileConfig {
        mrcMask: 0,
        firstProfileId: 0,
        retryProfileId: 0,
        orderedProfileId: 0,
        controlProfileId: 0
    });
    FIFOF#(MrcSourceProfileConfig) sourceRouteConfigQ <- mkSizedFIFOF(2);
    Reg#(Bit#(4)) noPathReportedMask <- mkReg(0);

    TxEvProfileId firstRouteProfile = sourceRoute.firstProfileId;
    TxEvProfileId retryRouteProfile = sourceRoute.retryProfileId;
    TxEvProfileId orderedRouteProfile = sourceRoute.orderedProfileId;
    TxEvProfileId controlRouteProfile = sourceRoute.controlProfileId;

    rule applySourceRoute (sourceRouteConfigQ.notEmpty);
        sourceRoute <= sourceRouteConfigQ.first;
        noPathReportedMask <= 0;
        sourceRouteConfigQ.deq;
    endrule
    Reg#(Bit#(4)) maxHighBurst <- mkReg(4'd4);
    Reg#(Bit#(4)) highBurst <- mkReg(0);
    Reg#(Bit#(2)) roundRobin <- mkReg(0);
    Reg#(Bit#(2)) egressRoundRobin <- mkReg(0);
    Reg#(Bit#(3)) accepted0 <- mkReg(0);
    Reg#(Bit#(3)) accepted1 <- mkReg(0);
    Reg#(Bit#(3)) accepted2 <- mkReg(0);
    Reg#(Bit#(3)) accepted3 <- mkReg(0);
    Reg#(Bit#(3)) completed0 <- mkReg(0);
    Reg#(Bit#(3)) completed1 <- mkReg(0);
    Reg#(Bit#(3)) completed2 <- mkReg(0);
    Reg#(Bit#(3)) completed3 <- mkReg(0);

    Reg#(Bool) active <- mkReg(False);
    Reg#(Bit#(2)) activeChannel <- mkReg(0);
    Reg#(MrcResolvedPath) activePath <- mkReg(unpack(0));
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
        pathManager.setAdminPortMask(request.channelEnable);
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
    Bool haveLegacyChannel = eligibleMask != 0;
    Bit#(2) selectedLegacyChannel = chooseChannel(roundRobin, eligibleMask);

    // Route selection uses one stable contract per traffic source. Reading a
    // descriptor FIFO's `first` method inside these cross-source guards makes
    // BSC collapse the start rules' CAN_FIRE terms; each start rule therefore
    // cross-checks the descriptor against this contract before admission.
    Bool firstIsMrc = sourceRoute.mrcMask[0] == 1;
    Bool retryIsMrc = sourceRoute.mrcMask[1] == 1;
    Bool orderedIsMrc = sourceRoute.mrcMask[2] == 1;
    Bool controlIsMrc = sourceRoute.mrcMask[3] == 1;

    MrcPathResolution firstResolution = pathManager.resolve(
        firstRouteProfile, eligibleMask, channelEnable);
    MrcPathResolution retryResolution = pathManager.resolve(
        retryRouteProfile, eligibleMask, channelEnable);
    MrcPathResolution orderedResolution = pathManager.resolve(
        orderedRouteProfile, eligibleMask, channelEnable);
    MrcPathResolution controlResolution = pathManager.resolve(
        controlRouteProfile, eligibleMask, channelEnable);
    Maybe#(MrcResolvedPath) firstPath = firstResolution.selected;
    Maybe#(MrcResolvedPath) retryPath = retryResolution.selected;
    Maybe#(MrcResolvedPath) orderedPath = orderedResolution.selected;
    Maybe#(MrcResolvedPath) controlPath = controlResolution.selected;

    Bool routeUpdatePending = sourceRouteConfigQ.notEmpty;
    Bool pathUpdatePending = pathManager.updatePending == 1 ||
        routeUpdatePending;

    Bool firstReady = !routeUpdatePending && firstDescQ.notEmpty &&
        firstPayloadQ.notEmpty &&
        ((!firstIsMrc && haveLegacyChannel) ||
         (firstIsMrc && !pathUpdatePending && isValid(firstPath)));
    Bool retryReady = !routeUpdatePending && retryDescQ.notEmpty &&
        retryPayloadQ.notEmpty &&
        ((!retryIsMrc && haveLegacyChannel) ||
         (retryIsMrc && !pathUpdatePending && isValid(retryPath)));
    Bool orderedReady = !routeUpdatePending && orderedDescQ.notEmpty &&
        orderedPayloadQ.notEmpty &&
        ((!orderedIsMrc && haveLegacyChannel) ||
         (orderedIsMrc && !pathUpdatePending && isValid(orderedPath)));
    Bool controlReady = !routeUpdatePending && controlDescQ.notEmpty &&
        controlPayloadQ.notEmpty &&
        ((!controlIsMrc && haveLegacyChannel) ||
         (controlIsMrc && !pathUpdatePending && isValid(controlPath)));
    Bool forceFirst = firstReady &&
        highBurst >= maxHighBurst;
    Bool noSourceReady = !controlReady && !orderedReady &&
        !retryReady && !firstReady;
    Bool controlNoPath = controlDescQ.notEmpty &&
        controlPayloadQ.notEmpty &&
        controlIsMrc && !pathUpdatePending && controlResolution.usable == 0;
    Bool orderedNoPath = orderedDescQ.notEmpty &&
        orderedPayloadQ.notEmpty &&
        orderedIsMrc && !pathUpdatePending && orderedResolution.usable == 0;
    Bool retryNoPath = retryDescQ.notEmpty &&
        retryPayloadQ.notEmpty &&
        retryIsMrc && !pathUpdatePending && retryResolution.usable == 0;
    Bool firstNoPath = firstDescQ.notEmpty &&
        firstPayloadQ.notEmpty &&
        firstIsMrc && !pathUpdatePending && firstResolution.usable == 0;
    Bool controlNeedsNoPath = controlNoPath && noPathReportedMask[3] == 0;
    Bool orderedNeedsNoPath = orderedNoPath && noPathReportedMask[2] == 0;
    Bool retryNeedsNoPath = retryNoPath && noPathReportedMask[1] == 0;
    Bool firstNeedsNoPath = firstNoPath && noPathReportedMask[0] == 0;

    rule reportControlNoPath (!active && noSourceReady &&
                             controlNeedsNoPath && noPathQ.notFull);
        noPathQ.enq(MrcNoPathEvent {
            trafficClass: TRAFFIC_CONTROL,
            profileId: controlRouteProfile
        });
        noPathReportedMask <= noPathReportedMask | 4'b1000;
    endrule

    rule reportOrderedNoPath (!active && noSourceReady &&
                             !controlNeedsNoPath && orderedNeedsNoPath &&
                             noPathQ.notFull);
        noPathQ.enq(MrcNoPathEvent {
            trafficClass: TRAFFIC_ORDERED,
            profileId: orderedRouteProfile
        });
        noPathReportedMask <= noPathReportedMask | 4'b0100;
    endrule

    rule reportRetryNoPath (!active && noSourceReady &&
                           !controlNeedsNoPath && !orderedNeedsNoPath &&
                           retryNeedsNoPath && noPathQ.notFull);
        noPathQ.enq(MrcNoPathEvent {
            trafficClass: TRAFFIC_RETRY,
            profileId: retryRouteProfile
        });
        noPathReportedMask <= noPathReportedMask | 4'b0010;
    endrule

    rule reportFirstNoPath (!active && noSourceReady &&
                           !controlNeedsNoPath && !orderedNeedsNoPath &&
                           !retryNeedsNoPath && firstNeedsNoPath &&
                           noPathQ.notFull);
        noPathQ.enq(MrcNoPathEvent {
            trafficClass: TRAFFIC_FIRST,
            profileId: firstRouteProfile
        });
        noPathReportedMask <= noPathReportedMask | 4'b0001;
    endrule

    rule startControl (!active && controlReady && !forceFirst);
        let descriptor = controlDescQ.first;
        let firstBeat = controlPayloadQ.first;
        MrcResolvedPath path = fromMaybe(unpack(0), controlPath);
        Bit#(2) selectedChannel = controlIsMrc
            ? path.channel : selectedLegacyChannel;
        Bit#(1) selectedSlot = selectedChannel == 0 ? accepted0[0] :
            selectedChannel == 1 ? accepted1[0] :
            selectedChannel == 2 ? accepted2[0] : accepted3[0];
        dynamicAssert(descriptor.trafficClass == TRAFFIC_CONTROL,
            "M3 control input has the wrong traffic class");
        dynamicAssert(packetDescriptorIsWellFormed(descriptor),
            "M3 control descriptor is malformed");
        dynamicAssert(pack(descriptor.wireProfile == WIRE_MRC_IPV4_ECMP_NO_ICRC)
                == pack(controlIsMrc),
            "M3 control wire profile disagrees with its source route config");
        dynamicAssert(!controlIsMrc ||
                descriptor.evProfileId == controlRouteProfile,
            "M3 control EV profile disagrees with its source route config");
        dynamicAssert(firstBeat.sop == 1 &&
                firstBeat.requestId == descriptor.requestId &&
                firstBeat.psn == descriptor.psn,
            "M3 control descriptor is not paired with its payload SOP");
        controlDescQ.deq;
        if (controlIsMrc)
            pathManager.commitSelection(path);
        active <= True;
        activeChannel <= selectedChannel;
        activePath <= path;
        activeSlot <= selectedSlot;
        ingressDescriptor <= descriptor;
        ingressConfig <= portConfig;
        ingressBeatIndex <= 0;
        ingressBytes <= 0;
        noPathReportedMask <= noPathReportedMask & 4'b0111;
        highBurst <= highBurst == '1 ? highBurst : highBurst + 1;
    endrule

    rule startOrdered (!active && !controlReady && orderedReady && !forceFirst);
        let descriptor = orderedDescQ.first;
        let firstBeat = orderedPayloadQ.first;
        MrcResolvedPath path = fromMaybe(unpack(0), orderedPath);
        Bit#(2) selectedChannel = orderedIsMrc
            ? path.channel : selectedLegacyChannel;
        Bit#(1) selectedSlot = selectedChannel == 0 ? accepted0[0] :
            selectedChannel == 1 ? accepted1[0] :
            selectedChannel == 2 ? accepted2[0] : accepted3[0];
        dynamicAssert(descriptor.trafficClass == TRAFFIC_ORDERED,
            "M3 ordered input has the wrong traffic class");
        dynamicAssert(packetDescriptorIsWellFormed(descriptor),
            "M3 ordered descriptor is malformed");
        dynamicAssert(pack(descriptor.wireProfile == WIRE_MRC_IPV4_ECMP_NO_ICRC)
                == pack(orderedIsMrc),
            "M3 ordered wire profile disagrees with its source route config");
        dynamicAssert(!orderedIsMrc ||
                descriptor.evProfileId == orderedRouteProfile,
            "M3 ordered EV profile disagrees with its source route config");
        dynamicAssert(firstBeat.sop == 1 &&
                firstBeat.requestId == descriptor.requestId &&
                firstBeat.psn == descriptor.psn,
            "M3 ordered descriptor is not paired with its payload SOP");
        orderedDescQ.deq;
        if (orderedIsMrc)
            pathManager.commitSelection(path);
        active <= True;
        activeChannel <= selectedChannel;
        activePath <= path;
        activeSlot <= selectedSlot;
        ingressDescriptor <= descriptor;
        ingressConfig <= portConfig;
        ingressBeatIndex <= 0;
        ingressBytes <= 0;
        noPathReportedMask <= noPathReportedMask & 4'b1011;
        highBurst <= highBurst == '1 ? highBurst : highBurst + 1;
    endrule

    rule startRetry (!active && !controlReady && !orderedReady &&
                     retryReady && !forceFirst);
        let descriptor = retryDescQ.first;
        let firstBeat = retryPayloadQ.first;
        MrcResolvedPath path = fromMaybe(unpack(0), retryPath);
        Bit#(2) selectedChannel = retryIsMrc
            ? path.channel : selectedLegacyChannel;
        Bit#(1) selectedSlot = selectedChannel == 0 ? accepted0[0] :
            selectedChannel == 1 ? accepted1[0] :
            selectedChannel == 2 ? accepted2[0] : accepted3[0];
        dynamicAssert(descriptor.trafficClass == TRAFFIC_RETRY,
            "M3 retry input has the wrong traffic class");
        dynamicAssert(packetDescriptorIsWellFormed(descriptor),
            "M3 retry descriptor is malformed");
        dynamicAssert(pack(descriptor.wireProfile == WIRE_MRC_IPV4_ECMP_NO_ICRC)
                == pack(retryIsMrc),
            "M3 retry wire profile disagrees with its source route config");
        dynamicAssert(!retryIsMrc ||
                descriptor.evProfileId == retryRouteProfile,
            "M3 retry EV profile disagrees with its source route config");
        dynamicAssert(firstBeat.sop == 1 &&
                firstBeat.requestId == descriptor.requestId &&
                firstBeat.psn == descriptor.psn,
            "M3 retry descriptor is not paired with its payload SOP");
        retryDescQ.deq;
        if (retryIsMrc)
            pathManager.commitSelection(path);
        active <= True;
        activeChannel <= selectedChannel;
        activePath <= path;
        activeSlot <= selectedSlot;
        ingressDescriptor <= descriptor;
        ingressConfig <= portConfig;
        ingressBeatIndex <= 0;
        ingressBytes <= 0;
        noPathReportedMask <= noPathReportedMask & 4'b1101;
        highBurst <= highBurst == '1 ? highBurst : highBurst + 1;
    endrule

    rule startFirst (!active && firstReady &&
                     (forceFirst ||
                      (!controlReady && !orderedReady && !retryReady)));
        let descriptor = firstDescQ.first;
        let firstBeat = firstPayloadQ.first;
        MrcResolvedPath path = fromMaybe(unpack(0), firstPath);
        Bit#(2) selectedChannel = firstIsMrc
            ? path.channel : selectedLegacyChannel;
        Bit#(1) selectedSlot = selectedChannel == 0 ? accepted0[0] :
            selectedChannel == 1 ? accepted1[0] :
            selectedChannel == 2 ? accepted2[0] : accepted3[0];
        dynamicAssert(descriptor.trafficClass == TRAFFIC_FIRST,
            "M3 first input has the wrong traffic class");
        dynamicAssert(packetDescriptorIsWellFormed(descriptor),
            "M3 first descriptor is malformed");
        dynamicAssert(pack(descriptor.wireProfile == WIRE_MRC_IPV4_ECMP_NO_ICRC)
                == pack(firstIsMrc),
            "M3 first wire profile disagrees with its source route config");
        dynamicAssert(!firstIsMrc ||
                descriptor.evProfileId == firstRouteProfile,
            "M3 first EV profile disagrees with its source route config");
        dynamicAssert(firstBeat.sop == 1 &&
                firstBeat.requestId == descriptor.requestId &&
                firstBeat.psn == descriptor.psn,
            "M3 first descriptor is not paired with its payload SOP");
        firstDescQ.deq;
        if (firstIsMrc)
            pathManager.commitSelection(path);
        active <= True;
        activeChannel <= selectedChannel;
        activePath <= path;
        activeSlot <= selectedSlot;
        ingressDescriptor <= descriptor;
        ingressConfig <= portConfig;
        ingressBeatIndex <= 0;
        ingressBytes <= 0;
        noPathReportedMask <= noPathReportedMask & 4'b1110;
        highBurst <= 0;
    endrule

    rule selectFirstPayload (active &&
                             ingressDescriptor.trafficClass == TRAFFIC_FIRST &&
                             firstPayloadQ.notEmpty && selectedPayloadQ.notFull &&
                             !selectedEopQueued);
        let beat = firstPayloadQ.first;
        selectedPayloadQ.enq(beat);
        firstPayloadQ.deq;
        if (beat.eop == 1)
            selectedEopQueued <= True;
    endrule

    rule selectRetryPayload (active &&
                             ingressDescriptor.trafficClass == TRAFFIC_RETRY &&
                             retryPayloadQ.notEmpty && selectedPayloadQ.notFull &&
                             !selectedEopQueued);
        let beat = retryPayloadQ.first;
        selectedPayloadQ.enq(beat);
        retryPayloadQ.deq;
        if (beat.eop == 1)
            selectedEopQueued <= True;
    endrule

    rule selectOrderedPayload (active &&
                               ingressDescriptor.trafficClass == TRAFFIC_ORDERED &&
                               orderedPayloadQ.notEmpty && selectedPayloadQ.notFull &&
                               !selectedEopQueued);
        let beat = orderedPayloadQ.first;
        selectedPayloadQ.enq(beat);
        orderedPayloadQ.deq;
        if (beat.eop == 1)
            selectedEopQueued <= True;
    endrule

    rule selectControlPayload (active &&
                               ingressDescriptor.trafficClass == TRAFFIC_CONTROL &&
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
        ingressBytes <= ingressBytes + 13'd128;
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
        Bit#(13) finalBytesWide = ingressDescriptor.payloadLength - ingressBytes;
        Bit#(8) finalBytes = truncate(finalBytesWide);
        dynamicAssert(ingressBytes < ingressDescriptor.payloadLength &&
                finalBytesWide <= 13'd128,
            "M3 EOP payload length is outside one beat");
        dynamicAssert(beat.requestId == ingressDescriptor.requestId &&
                beat.psn == ingressDescriptor.psn,
            "M3 descriptor/payload identity mismatch");
        dynamicAssert(beat.sop == pack(ingressBeatIndex == 0),
            "M3 malformed packet SOP");
        dynamicAssert(beat.keep == byteMask128(finalBytes),
            "M3 EOP keep does not match remaining payload bytes");
        dynamicAssert(ingressBytes + zeroExtend(finalBytes) ==
                zeroExtend(ingressDescriptor.payloadLength),
            "M3 packet payload length mismatch");

        BufferedPacket packet = BufferedPacket {
            descriptor: ingressDescriptor,
            portConfig: ingressConfig,
            path: activePath,
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

    Bit#(4) egressMask = {
        encoder3.egressOut.notEmpty,
        encoder2.egressOut.notEmpty,
        encoder1.egressOut.notEmpty,
        encoder0.egressOut.notEmpty
    };
    Bit#(2) selectedEgressChannel = chooseChannel(
        egressRoundRobin, egressMask);

    rule mergeEgress0 (egressMask != 0 && selectedEgressChannel == 0 &&
                       egressQ.notFull);
        egressQ.enq(encoder0.egressOut.first);
        encoder0.egressOut.deq;
        egressRoundRobin <= 1;
    endrule

    rule mergeEgress1 (egressMask != 0 && selectedEgressChannel == 1 &&
                       egressQ.notFull);
        egressQ.enq(encoder1.egressOut.first);
        encoder1.egressOut.deq;
        egressRoundRobin <= 2;
    endrule

    rule mergeEgress2 (egressMask != 0 && selectedEgressChannel == 2 &&
                       egressQ.notFull);
        egressQ.enq(encoder2.egressOut.first);
        encoder2.egressOut.deq;
        egressRoundRobin <= 3;
    endrule

    rule mergeEgress3 (egressMask != 0 && selectedEgressChannel == 3 &&
                       egressQ.notFull);
        egressQ.enq(encoder3.egressOut.first);
        encoder3.egressOut.deq;
        egressRoundRobin <= 0;
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
    interface txEgressOut = toPipeOut(egressQ);
    interface sourceRouteConfigIn = toPipeIn(sourceRouteConfigQ);
    interface evEntryConfigIn = pathManager.entryConfigIn;
    interface evPolicyConfigIn = pathManager.policyConfigIn;
    interface portStatusIn = pathManager.portStatusIn;
    interface pathFeedbackIn = pathManager.feedbackIn;
    interface probeOut = pathManager.probeOut;
    interface noPathOut = toPipeOut(noPathQ);
    interface ServerP configServer;
        interface request = toPipeIn(configReqQ);
        interface response = toPipeOut(configRespQ);
    endinterface
endmodule

endpackage
