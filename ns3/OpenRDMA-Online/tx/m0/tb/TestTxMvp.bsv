import CommonTypes :: *;
import TxMvp :: *;
import TxMvpTypes :: *;

interface TestTxMvp;
    interface PipeIn#(SlotCfgReq) slotCfgReqPipeIn;
    interface PipeOut#(TxCfgResp) slotCfgRespPipeOut;
    interface PipeIn#(QpCfgReq) qpCfgReqPipeIn;
    interface PipeOut#(TxCfgResp) qpCfgRespPipeOut;
    interface PipeIn#(MrCfgReq) mrCfgReqPipeIn;
    interface PipeOut#(TxCfgResp) mrCfgRespPipeOut;
    interface PipeIn#(PgtCfgReq) pgtCfgReqPipeIn;
    interface PipeOut#(TxCfgResp) pgtCfgRespPipeOut;

    interface PipeIn#(DecodedGdrpSubmission) submissionPipeIn;
    interface PipeOut#(TxDmaReadRequest) dmaReadReqPipeOut;
endinterface

(* synthesize *)
module mkTestTxMvp(TestTxMvp);
    let dut <- mkTxMvp;

    interface slotCfgReqPipeIn = dut.slotConfig.request;
    interface slotCfgRespPipeOut = dut.slotConfig.response;
    interface qpCfgReqPipeIn = dut.qpConfig.request;
    interface qpCfgRespPipeOut = dut.qpConfig.response;
    interface mrCfgReqPipeIn = dut.mrConfig.request;
    interface mrCfgRespPipeOut = dut.mrConfig.response;
    interface pgtCfgReqPipeIn = dut.pgtConfig.request;
    interface pgtCfgRespPipeOut = dut.pgtConfig.response;

    interface submissionPipeIn = dut.submissionIn;
    interface dmaReadReqPipeOut = dut.dmaReadReqOut;
endmodule
