import Vector::*;
import Connectable::*;
import CommonTypes::*;
import TxTypes::*;
import RxTypes::*;
import PacketAssembly::*;
import MrcTxScheduler::*;
import RxTop::*;

interface TestTxRxLoopback;
    interface PipeIn#(MessageContext) txContextIn;
    interface PipeIn#(MessagePayloadBeat) txPayloadIn;
    interface PipeIn#(MrcTxCfgReq) txConfigIn;
    interface PipeOut#(CfgResp) txConfigOut;
    interface PipeOut#(EthPacket) txChannel0;
    interface PipeOut#(EthPacket) txChannel1;
    interface PipeOut#(EthPacket) txChannel2;
    interface PipeOut#(EthPacket) txChannel3;
    interface PipeOut#(TxCommit) txCommit;
    method Action configureEpoch(RxEpoch value);
    method Action configureNetwork(RxNetworkConfig cfg);
    method Action configureQp0(RxQpConfig cfg);
    method Action startRx;
    interface PipeIn#(RxWireBeat) rxLane0;
    interface PipeIn#(RxWireBeat) rxLane1;
    interface PipeIn#(RxWireBeat) rxLane2;
    interface PipeIn#(RxWireBeat) rxLane3;
    interface PipeOut#(RxDmaWriteRequest) dmaRequest0;
    interface PipeOut#(RxDmaWriteRequest) dmaRequest1;
    interface PipeOut#(RxDmaWriteRequest) dmaRequest2;
    interface PipeOut#(RxDmaWriteRequest) dmaRequest3;
    interface PipeOut#(RxDmaPayloadBeat) dmaPayload0;
    interface PipeOut#(RxDmaPayloadBeat) dmaPayload1;
    interface PipeOut#(RxDmaPayloadBeat) dmaPayload2;
    interface PipeOut#(RxDmaPayloadBeat) dmaPayload3;
    interface PipeIn#(RxDmaCompletion) dmaCompletion0;
    interface PipeIn#(RxDmaCompletion) dmaCompletion1;
    interface PipeIn#(RxDmaCompletion) dmaCompletion2;
    interface PipeIn#(RxDmaCompletion) dmaCompletion3;
    interface PipeOut#(RxFeedback) feedback;
    interface PipeOut#(RxCompletion) completion;
    interface PipeOut#(RxError) errors;
endinterface

(* synthesize *)
module mkTestTxRxLoopback(TestTxRxLoopback);
    let m2 <- mkPacketAssemblyEngine;
    let m3 <- mkFourChannelTxScheduler;
    let rx <- mkRxTop;
    mkConnection(m2.descriptorOut, m3.firstDescriptorIn);
    mkConnection(m2.payloadOut, m3.firstPayloadIn);

    interface txContextIn = m2.contextIn;
    interface txPayloadIn = m2.payloadIn;
    interface txConfigIn = m3.configServer.request;
    interface txConfigOut = m3.configServer.response;
    interface txChannel0 = m3.channel0Out;
    interface txChannel1 = m3.channel1Out;
    interface txChannel2 = m3.channel2Out;
    interface txChannel3 = m3.channel3Out;
    interface txCommit = m3.txCommitOut;
    method Action configureEpoch(RxEpoch value); rx.configureEpoch(value); endmethod
    method Action configureNetwork(RxNetworkConfig cfg); rx.configureNetwork(cfg); endmethod
    method Action configureQp0(RxQpConfig cfg); rx.configureQp(0, cfg); endmethod
    method Action startRx; rx.start; endmethod
    interface rxLane0 = rx.ports[0];
    interface rxLane1 = rx.ports[1];
    interface rxLane2 = rx.ports[2];
    interface rxLane3 = rx.ports[3];
    interface dmaRequest0 = rx.dmaRequests[0];
    interface dmaRequest1 = rx.dmaRequests[1];
    interface dmaRequest2 = rx.dmaRequests[2];
    interface dmaRequest3 = rx.dmaRequests[3];
    interface dmaPayload0 = rx.dmaPayloads[0];
    interface dmaPayload1 = rx.dmaPayloads[1];
    interface dmaPayload2 = rx.dmaPayloads[2];
    interface dmaPayload3 = rx.dmaPayloads[3];
    interface dmaCompletion0 = rx.dmaCompletions[0];
    interface dmaCompletion1 = rx.dmaCompletions[1];
    interface dmaCompletion2 = rx.dmaCompletions[2];
    interface dmaCompletion3 = rx.dmaCompletions[3];
    interface feedback = rx.feedback;
    interface completion = rx.completion;
    interface errors = rx.errors;
endmodule
