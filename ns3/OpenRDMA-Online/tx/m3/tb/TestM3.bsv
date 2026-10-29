// Transparent synthesis wrapper for the four-channel M3 scheduler.

import CommonTypes :: *;
import MrcTxScheduler :: *;
import MrcTxTypes :: *;
import TxTypes :: *;

interface TestM3;
    interface PipeIn#(PacketDescriptor) firstDescriptorPipeIn;
    interface PipeIn#(PacketPayloadBeat) firstPayloadPipeIn;
    interface PipeIn#(PacketDescriptor) retryDescriptorPipeIn;
    interface PipeIn#(PacketPayloadBeat) retryPayloadPipeIn;
    interface PipeIn#(PacketDescriptor) orderedDescriptorPipeIn;
    interface PipeIn#(PacketPayloadBeat) orderedPayloadPipeIn;
    interface PipeIn#(PacketDescriptor) controlDescriptorPipeIn;
    interface PipeIn#(PacketPayloadBeat) controlPayloadPipeIn;

    interface PipeOut#(EthPacket) channel0PipeOut;
    interface PipeOut#(EthPacket) channel1PipeOut;
    interface PipeOut#(EthPacket) channel2PipeOut;
    interface PipeOut#(EthPacket) channel3PipeOut;
    interface PipeOut#(TxCommit) txCommitPipeOut;
    interface PipeOut#(TxEgressEvent) txEgressPipeOut;
    interface PipeIn#(MrcSourceProfileConfig) sourceRouteConfigPipeIn;
    interface PipeIn#(MrcEvEntryConfig) evEntryConfigPipeIn;
    interface PipeIn#(MrcEvPolicyConfig) evPolicyConfigPipeIn;
    interface PipeIn#(MrcPortStatusUpdate) portStatusPipeIn;
    interface PipeIn#(MrcPathFeedback) pathFeedbackPipeIn;
    interface PipeOut#(MrcProbeRequest) probePipeOut;
    interface PipeOut#(MrcNoPathEvent) noPathPipeOut;
    interface PipeIn#(MrcTxCfgReq) configPipeIn;
    interface PipeOut#(CfgResp) configRespPipeOut;
endinterface

(* synthesize *)
module mkTestM3(TestM3);
    let scheduler <- mkFourChannelTxScheduler;

    interface firstDescriptorPipeIn = scheduler.firstDescriptorIn;
    interface firstPayloadPipeIn = scheduler.firstPayloadIn;
    interface retryDescriptorPipeIn = scheduler.retryDescriptorIn;
    interface retryPayloadPipeIn = scheduler.retryPayloadIn;
    interface orderedDescriptorPipeIn = scheduler.orderedDescriptorIn;
    interface orderedPayloadPipeIn = scheduler.orderedPayloadIn;
    interface controlDescriptorPipeIn = scheduler.controlDescriptorIn;
    interface controlPayloadPipeIn = scheduler.controlPayloadIn;

    interface channel0PipeOut = scheduler.channel0Out;
    interface channel1PipeOut = scheduler.channel1Out;
    interface channel2PipeOut = scheduler.channel2Out;
    interface channel3PipeOut = scheduler.channel3Out;
    interface txCommitPipeOut = scheduler.txCommitOut;
    interface txEgressPipeOut = scheduler.txEgressOut;
    interface sourceRouteConfigPipeIn = scheduler.sourceRouteConfigIn;
    interface evEntryConfigPipeIn = scheduler.evEntryConfigIn;
    interface evPolicyConfigPipeIn = scheduler.evPolicyConfigIn;
    interface portStatusPipeIn = scheduler.portStatusIn;
    interface pathFeedbackPipeIn = scheduler.pathFeedbackIn;
    interface probePipeOut = scheduler.probeOut;
    interface noPathPipeOut = scheduler.noPathOut;
    interface configPipeIn = scheduler.configServer.request;
    interface configRespPipeOut = scheduler.configServer.response;
endmodule
