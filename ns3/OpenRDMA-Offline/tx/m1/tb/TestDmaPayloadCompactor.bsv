import CommonTypes :: *;
import DmaPayloadCompactor :: *;
import DmaTypes :: *;
import TxTypes :: *;

interface TestDmaPayloadCompactor;
    interface PipeIn#(DmaPayloadBeat) dmaPayloadPipeIn;
    interface PipeOut#(MessagePayloadBeat) messagePayloadPipeOut;
endinterface

(* synthesize *)
module mkTestDmaPayloadCompactor(TestDmaPayloadCompactor);
    let dut <- mkDmaPayloadCompactor;

    interface dmaPayloadPipeIn = dut.payloadIn;
    interface messagePayloadPipeOut = dut.payloadOut;
endmodule
