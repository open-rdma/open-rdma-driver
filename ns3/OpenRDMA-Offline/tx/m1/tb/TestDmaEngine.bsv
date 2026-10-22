import CommonTypes :: *;
import DmaEngine :: *;
import DmaTypes :: *;

interface TestDmaEngine;
    interface PipeIn#(DmaChunkReq) chunkPipeIn;
    interface PipeOut#(DmaReadRequest) dmaReadReqPipeOut;
    interface PipeIn#(DmaCompletionBeat) dmaCompletionPipeIn;
    interface PipeOut#(DmaPayloadBeat) payloadPipeOut;
    interface PipeOut#(DmaMessageDone) messageDonePipeOut;
endinterface

(* synthesize *)
module mkTestDmaEngine(TestDmaEngine);
    let dut <- mkDmaEngine;

    interface chunkPipeIn = dut.chunkIn;
    interface dmaReadReqPipeOut = dut.dmaReadReqOut;
    interface dmaCompletionPipeIn = dut.dmaCompletionIn;
    interface payloadPipeOut = dut.payloadOut;
    interface messageDonePipeOut = dut.messageDoneOut;
endmodule
