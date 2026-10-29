package RxPacketBuffer;

import FIFOF::*;
import RegFile::*;
import Vector::*;

import CommonTypes::*;
import RxTypes::*;

typedef TMul#(RxPayloadSlots, RxFrameBeats) RxPayloadWords;
typedef TLog#(RxPayloadWords) RxPayloadWordAddrWidth;
typedef Bit#(RxPayloadWordAddrWidth) RxPayloadWordAddr;
typedef 4138 RxMaxFrameBytes;

function RxPayloadWordAddr rxBufferAddress(
    RxPayloadSlot slot,
    Bit#(8) wordIndex
);
    RxPayloadWordAddr base = zeroExtend(slot) * fromInteger(valueOf(RxFrameBeats));
    return base + zeroExtend(wordIndex);
endfunction

function Tuple2#(Bool, RxPayloadSlot) rxFindFreeSlot(
    Bit#(RxPayloadSlots) occupied
);
    Bool found = False;
    RxPayloadSlot slot = 0;
    for (Integer i = 0; i < valueOf(RxPayloadSlots); i = i + 1) begin
        if (!found && occupied[i] == 0) begin
            found = True;
            slot = fromInteger(i);
        end
    end
    return tuple2(found, slot);
endfunction

function RxError rxBufferError(RxErrorCode code);
    return RxError{qpn: 0, psn: 0, code: code};
endfunction

interface RxPortBufferIfc;
    interface PipeIn#(RxWireBeat) wireIn;
    interface PipeOut#(RxBufferedFrame) frameOut;
    interface PipeIn#(RxPayloadReadRequest) readRequest;
    interface PipeOut#(RxPayloadReadResponse) readResponse;
    interface PipeIn#(RxBufferRelease) releaseIn;
    interface PipeOut#(RxError) errorOut;
endinterface

// All queues are elastic in the module clock domain. Slots remain owned until
// releaseIn accepts the matching {lane, slot, generation} tag.
module mkRxPortBuffer#(RxLaneId lane)(RxPortBufferIfc);
    // Four entries absorb short ingress/consumer stalls; release/error queues
    // are deeper so cleanup cannot be blocked by a brief downstream stall.
    FIFOF#(RxWireBeat) inQ <- mkSizedFIFOF(4);
    FIFOF#(RxBufferedFrame) frameQ <- mkSizedFIFOF(4);
    FIFOF#(RxPayloadReadRequest) readQ <- mkSizedFIFOF(4);
    FIFOF#(RxPayloadReadResponse) readResponseQ <- mkSizedFIFOF(4);
    FIFOF#(RxBufferRelease) releaseQ <- mkSizedFIFOF(8);
    FIFOF#(RxError) errorQ <- mkSizedFIFOF(8);

    // Functional storage model: 16 slots x 130 256-bit words, asynchronous
    // reads through RegFile. Production RTL must replace this with a BRAM
    // wrapper whose read latency and collision behavior are explicit.
    RegFile#(RxPayloadWordAddr, Bit#(256)) payloadMem <-
        mkRegFile(0, fromInteger(valueOf(RxPayloadWords) - 1));
    Reg#(Bit#(RxPayloadSlots)) occupiedReg <- mkReg(0);
    Vector#(RxPayloadSlots, Reg#(RxPayloadGeneration)) generationRegs <-
        replicateM(mkReg(0));
    Vector#(RxPayloadSlots, Reg#(Bit#(8))) slotBeatCountRegs <-
        replicateM(mkReg(0));

    Reg#(Bool) activeReg <- mkReg(False);
    Reg#(Bool) frameValidReg <- mkReg(True);
    Reg#(RxPayloadSlot) currentSlotReg <- mkReg(0);
    Reg#(RxPayloadGeneration) currentGenerationReg <- mkReg(0);
    Reg#(Bit#(8)) wordIndexReg <- mkReg(0);
    Reg#(Bit#(14)) byteCountReg <- mkReg(0);
    Reg#(Bit#(256)) header0Reg <- mkReg(0);
    Reg#(Bit#(256)) header1Reg <- mkReg(0);
    Reg#(Bit#(256)) header2Reg <- mkReg(0);

    // These rules can update slot ownership and errorQ. Cleanup and reads take
    // priority over ingress so a full lane can always make forward progress.
    (* descending_urgency = "releaseSlot, serveRead, assembleFrame" *)

    // Input: releaseQ. Output: slot ownership update or RX_BUFFER_BAD_TAG.
    rule releaseSlot;
        let releaseReq = releaseQ.first;
        releaseQ.deq;

        let tag = releaseReq.tag;
        Bool validTag = tag.lane == lane
            && occupiedReg[tag.slot] == 1
            && generationRegs[tag.slot] == tag.generation;
        if (validTag) begin
            Bit#(RxPayloadSlots) nextOccupied = occupiedReg;
            nextOccupied[tag.slot] = 0;
            occupiedReg <= nextOccupied;
            generationRegs[tag.slot] <= generationRegs[tag.slot] + 1;
        end
        else begin
            errorQ.enq(rxBufferError(RX_BUFFER_BAD_TAG));
        end
    endrule

    // Input: inQ. Output: frameQ, payloadMem, and optional RX_BAD_FRAME.
    // A complete frame owns one slot until an explicit release arrives.
    rule assembleFrame;
        let beat = inQ.first;
        let freeSlotResult = rxFindFreeSlot(occupiedReg);
        Bool haveFreeSlot = tpl_1(freeSlotResult);
        RxPayloadSlot freeSlot = tpl_2(freeSlotResult);
        Bool startsFrame = beat.sof == 1;
        Bool canConsume = activeReg || !startsFrame || haveFreeSlot;

        if (canConsume) begin
            inQ.deq;
            if (!activeReg && !startsFrame) begin
                errorQ.enq(rxBufferError(RX_BAD_FRAME));
            end
            else begin
                Bool abandonsFrame = startsFrame && activeReg;
                RxPayloadSlot slot = startsFrame
                    ? (activeReg ? currentSlotReg : freeSlot)
                    : currentSlotReg;
                RxPayloadGeneration generation = startsFrame
                    ? (activeReg
                        ? generationRegs[slot] + 1
                        : generationRegs[slot])
                    : currentGenerationReg;
                Bit#(8) wordIndex = startsFrame ? 0 : wordIndexReg;

                Bit#(6) validBytes = pack(countOnes(beat.be));
                Bit#(14) totalBytes = (startsFrame ? 0 : byteCountReg)
                    + zeroExtend(validBytes);
                Bool validBeat = beat.be != 0
                    && rxPrefixBe32(beat.be)
                    && (beat.eof == 1 || beat.be == '1)
                    && wordIndex < fromInteger(valueOf(RxFrameBeats))
                    && totalBytes <= fromInteger(valueOf(RxMaxFrameBytes));
                Bool frameIsValid = (startsFrame ? True : frameValidReg)
                    && validBeat;

                Bit#(256) header0 = startsFrame ? 0 : header0Reg;
                Bit#(256) header1 = startsFrame ? 0 : header1Reg;
                Bit#(256) header2 = startsFrame ? 0 : header2Reg;
                if (wordIndex == 0) begin
                    header0 = beat.data;
                end
                if (wordIndex == 1) begin
                    header1 = beat.data;
                end
                if (wordIndex == 2) begin
                    header2 = beat.data;
                end

                if (wordIndex < fromInteger(valueOf(RxFrameBeats))) begin
                    payloadMem.upd(rxBufferAddress(slot, wordIndex), beat.data);
                end

                Bit#(RxPayloadSlots) nextOccupied = occupiedReg;
                Bool updateGeneration = startsFrame;
                RxPayloadGeneration nextGeneration = generation;
                if (startsFrame) begin
                    nextOccupied[slot] = 1;
                end

                if (beat.eof == 1) begin
                    if (frameIsValid) begin
                        Bit#(8) beatCount = wordIndex + 1;
                        slotBeatCountRegs[slot] <= beatCount;
                        frameQ.enq(RxBufferedFrame{
                            tag: RxPayloadTag{
                                lane: lane,
                                slot: slot,
                                generation: generation
                            },
                            header0: header0,
                            header1: header1,
                            header2: header2,
                            // totalBytes is bounded by RxMaxFrameBytes (13 bits).
                            bytes: truncate(totalBytes),
                            beats: beatCount
                        });
                    end
                    else begin
                        nextOccupied[slot] = 0;
                        updateGeneration = True;
                        nextGeneration = generation + 1;
                    end

                    if (abandonsFrame || !frameIsValid) begin
                        errorQ.enq(rxBufferError(RX_BAD_FRAME));
                    end
                    activeReg <= False;
                    frameValidReg <= True;
                    wordIndexReg <= 0;
                    byteCountReg <= 0;
                    header0Reg <= 0;
                    header1Reg <= 0;
                    header2Reg <= 0;
                end
                else begin
                    if (abandonsFrame) begin
                        errorQ.enq(rxBufferError(RX_BAD_FRAME));
                    end
                    activeReg <= True;
                    frameValidReg <= frameIsValid;
                    currentSlotReg <= slot;
                    currentGenerationReg <= generation;
                    wordIndexReg <= wordIndex + 1;
                    byteCountReg <= totalBytes;
                    header0Reg <= header0;
                    header1Reg <= header1;
                    header2Reg <= header2;
                end

                if (updateGeneration) begin
                    generationRegs[slot] <= nextGeneration;
                end
                occupiedReg <= nextOccupied;
            end
        end
    endrule

    // Input: readQ. Output: readResponseQ and optional RX_BUFFER_BAD_TAG.
    // A valid response contains the requested word and its successor so the
    // DMA placement stage can align payload without another memory lookup.
    rule serveRead;
        let request = readQ.first;
        readQ.deq;

        let tag = request.tag;
        Bool validTag = tag.lane == lane
            && occupiedReg[tag.slot] == 1
            && generationRegs[tag.slot] == tag.generation
            && request.wordIndex < slotBeatCountRegs[tag.slot];
        Bit#(8) safeIndex = validTag ? request.wordIndex : 0;
        Bit#(256) lowWord = payloadMem.sub(rxBufferAddress(tag.slot, safeIndex));
        Bit#(256) highWord = 0;
        if (validTag
            && request.wordIndex + 1 < slotBeatCountRegs[tag.slot]) begin
            highWord = payloadMem.sub(rxBufferAddress(tag.slot, safeIndex + 1));
        end

        readResponseQ.enq(RxPayloadReadResponse{
            tag: tag,
            wordIndex: request.wordIndex,
            low: validTag ? lowWord : 0,
            high: highWord,
            valid: validTag
        });
        if (!validTag) begin
            errorQ.enq(rxBufferError(RX_BUFFER_BAD_TAG));
        end
    endrule

    interface wireIn = toPipeIn(inQ);
    interface frameOut = toPipeOut(frameQ);
    interface readRequest = toPipeIn(readQ);
    interface readResponse = toPipeOut(readResponseQ);
    interface releaseIn = toPipeIn(releaseQ);
    interface errorOut = toPipeOut(errorQ);
endmodule

interface RxPacketBufferIfc;
    interface Vector#(RxLaneCount, PipeIn#(RxWireBeat)) ports;
    interface Vector#(RxLaneCount, PipeOut#(RxBufferedFrame)) frames;
    interface Vector#(RxLaneCount, PipeIn#(RxPayloadReadRequest)) readRequests;
    interface Vector#(RxLaneCount, PipeOut#(RxPayloadReadResponse)) readResponses;
    interface Vector#(RxLaneCount, PipeIn#(RxBufferRelease)) releases;
    interface Vector#(RxLaneCount, PipeOut#(RxError)) errors;
endinterface

module mkRxPacketBuffer(RxPacketBufferIfc);
    Vector#(RxLaneCount, RxPortBufferIfc) laneBuffers;
    Vector#(RxLaneCount, PipeIn#(RxWireBeat)) inputPorts;
    Vector#(RxLaneCount, PipeOut#(RxBufferedFrame)) framePorts;
    Vector#(RxLaneCount, PipeIn#(RxPayloadReadRequest)) readRequestPorts;
    Vector#(RxLaneCount, PipeOut#(RxPayloadReadResponse)) readResponsePorts;
    Vector#(RxLaneCount, PipeIn#(RxBufferRelease)) releasePorts;
    Vector#(RxLaneCount, PipeOut#(RxError)) errorPorts;

    for (Integer i = 0; i < valueOf(RxLaneCount); i = i + 1) begin
        laneBuffers[i] <- mkRxPortBuffer(fromInteger(i));
        inputPorts[i] = laneBuffers[i].wireIn;
        framePorts[i] = laneBuffers[i].frameOut;
        readRequestPorts[i] = laneBuffers[i].readRequest;
        readResponsePorts[i] = laneBuffers[i].readResponse;
        releasePorts[i] = laneBuffers[i].releaseIn;
        errorPorts[i] = laneBuffers[i].errorOut;
    end

    interface ports = inputPorts;
    interface frames = framePorts;
    interface readRequests = readRequestPorts;
    interface readResponses = readResponsePorts;
    interface releases = releasePorts;
    interface errors = errorPorts;
endmodule

endpackage
