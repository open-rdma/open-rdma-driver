// Transparent synthesis wrapper for the channel-neutral M2 packetizer.

import CommonTypes :: *;
import PacketAssembly :: *;
import TxTypes :: *;

interface TestM2;
    interface PipeIn#(MessageContext) contextPipeIn;
    interface PipeIn#(MessagePayloadBeat) messagePayloadPipeIn;
    interface PipeOut#(PacketDescriptor) descriptorPipeOut;
    interface PipeOut#(PacketPayloadBeat) packetPayloadPipeOut;

    interface PipeIn#(MessagePayloadBeat) alignerPayloadPipeIn;
    method ActionValue#(AlignedBeat) alignerRequest(
        Bit#(8) need, TxRequestId expectedId, TxLength expectedOffset);
    method Action alignerFlush;
    method Bit#(8) alignerResidualBytes;
    method Bit#(9) alignerSupplyBytes;
endinterface

(* synthesize *)
module mkTestM2(TestM2);
    let engine <- mkPacketAssemblyEngine;
    let aligner <- mkByteAligner;

    interface contextPipeIn = engine.contextIn;
    interface messagePayloadPipeIn = engine.payloadIn;
    interface descriptorPipeOut = engine.descriptorOut;
    interface packetPayloadPipeOut = engine.payloadOut;

    interface alignerPayloadPipeIn = aligner.payloadIn;
    method alignerRequest = aligner.request;
    method alignerFlush = aligner.flush;
    method alignerResidualBytes = aligner.residualBytes;
    method alignerSupplyBytes = aligner.supplyBytes;
endmodule
