import CommonTypes :: *;
import DmaPhaseAIntegration :: *;
import DmaTypes :: *;
import MrcTxTypes :: *;
import TxTypes :: *;

interface TestDmaPhaseAIntegration;
    interface PipeIn#(MessageContext) contextPipeIn;
    interface PipeIn#(DmaChunkReq) chunkPipeIn;
    interface PipeOut#(DmaReadRequest) dmaReadReqPipeOut;
    interface PipeIn#(DmaCompletionBeat) dmaCompletionPipeIn;
    interface PipeOut#(EthPacket) channel0PipeOut;
    interface PipeOut#(EthPacket) channel1PipeOut;
    interface PipeOut#(EthPacket) channel2PipeOut;
    interface PipeOut#(EthPacket) channel3PipeOut;
    interface PipeOut#(TxCommit) txCommitPipeOut;
    interface PipeOut#(TxEgressEvent) txEgressPipeOut;
    interface PipeOut#(DmaMessageDone) dmaMessageDonePipeOut;
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
module mkTestDmaPhaseAIntegration(TestDmaPhaseAIntegration);
    let dut <- mkDmaPhaseAIntegration;

    interface contextPipeIn = dut.contextIn;
    interface chunkPipeIn = dut.chunkIn;
    interface dmaReadReqPipeOut = dut.dmaReadReqOut;
    interface dmaCompletionPipeIn = dut.dmaCompletionIn;
    interface channel0PipeOut = dut.channel0Out;
    interface channel1PipeOut = dut.channel1Out;
    interface channel2PipeOut = dut.channel2Out;
    interface channel3PipeOut = dut.channel3Out;
    interface txCommitPipeOut = dut.txCommitOut;
    interface txEgressPipeOut = dut.txEgressOut;
    interface dmaMessageDonePipeOut = dut.dmaMessageDoneOut;
    interface sourceRouteConfigPipeIn = dut.sourceRouteConfigIn;
    interface evEntryConfigPipeIn = dut.evEntryConfigIn;
    interface evPolicyConfigPipeIn = dut.evPolicyConfigIn;
    interface portStatusPipeIn = dut.portStatusIn;
    interface pathFeedbackPipeIn = dut.pathFeedbackIn;
    interface probePipeOut = dut.probeOut;
    interface noPathPipeOut = dut.noPathOut;
    interface configPipeIn = dut.configServer.request;
    interface configRespPipeOut = dut.configServer.response;
endmodule
