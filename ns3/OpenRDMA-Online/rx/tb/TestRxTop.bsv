import CommonTypes::*;
import TxTypes::*;
import RxTypes::*;
import RxTop::*;

interface TestRxTop;
    method Action configureEpoch(RxEpoch value);
    method Action configureNetwork(RxNetworkConfig cfg);
    method Action configure0(RxQpConfig cfg);
    method Action configure1(RxQpConfig cfg);
    method Action configure2(RxQpConfig cfg);
    method Action configure3(RxQpConfig cfg);
    method Action start;
    method RxReceiveProgress progress0;
    method TxPsn dmaAckPsn0;
    method RxGlobalSeq nextGlobalSeq;
    method RxGlobalSeq retireHead;
    method Bool faulted;
    interface PipeIn#(RxWireBeat) lane0In;
    interface PipeIn#(RxWireBeat) lane1In;
    interface PipeIn#(RxWireBeat) lane2In;
    interface PipeIn#(RxWireBeat) lane3In;
    interface PipeOut#(RxDmaWriteRequest) lane0DmaRequest;
    interface PipeOut#(RxDmaWriteRequest) lane1DmaRequest;
    interface PipeOut#(RxDmaWriteRequest) lane2DmaRequest;
    interface PipeOut#(RxDmaWriteRequest) lane3DmaRequest;
    interface PipeOut#(RxDmaPayloadBeat) lane0DmaPayload;
    interface PipeOut#(RxDmaPayloadBeat) lane1DmaPayload;
    interface PipeOut#(RxDmaPayloadBeat) lane2DmaPayload;
    interface PipeOut#(RxDmaPayloadBeat) lane3DmaPayload;
    interface PipeIn#(RxDmaCompletion) lane0DmaCompletion;
    interface PipeIn#(RxDmaCompletion) lane1DmaCompletion;
    interface PipeIn#(RxDmaCompletion) lane2DmaCompletion;
    interface PipeIn#(RxDmaCompletion) lane3DmaCompletion;
    interface PipeIn#(RxOrderedFlagIngress) orderedFlagIn;
    interface PipeOut#(RxFlagDmaRequest) flagDmaRequest;
    interface PipeIn#(RxFlagDmaResponse) flagDmaResponse;
    interface PipeOut#(RxOrderedCredit) orderedCredit;
    interface PipeOut#(RxCompletion) completion;
    interface PipeOut#(RxFeedback) feedback;
    interface PipeOut#(RxEcnEvent) ecn;
    interface PipeOut#(RxTxFeedback) txFeedback;
    interface PipeOut#(RxError) errors;
endinterface

(* synthesize *)
module mkTestRxTop(TestRxTop);
    let dut <- mkRxTop;
    method Action configureEpoch(RxEpoch value); dut.configureEpoch(value); endmethod
    method Action configureNetwork(RxNetworkConfig cfg); dut.configureNetwork(cfg); endmethod
    method Action configure0(RxQpConfig cfg); dut.configureQp(0, cfg); endmethod
    method Action configure1(RxQpConfig cfg); dut.configureQp(1, cfg); endmethod
    method Action configure2(RxQpConfig cfg); dut.configureQp(2, cfg); endmethod
    method Action configure3(RxQpConfig cfg); dut.configureQp(3, cfg); endmethod
    method Action start; dut.start; endmethod
    method RxReceiveProgress progress0 = dut.receiveProgress(0);
    method TxPsn dmaAckPsn0 = dut.dmaAckPsn(0);
    method RxGlobalSeq nextGlobalSeq = dut.nextGlobalSeq;
    method RxGlobalSeq retireHead = dut.retireHead;
    method Bool faulted = dut.faulted;
    interface lane0In = dut.ports[0];
    interface lane1In = dut.ports[1];
    interface lane2In = dut.ports[2];
    interface lane3In = dut.ports[3];
    interface lane0DmaRequest = dut.dmaRequests[0];
    interface lane1DmaRequest = dut.dmaRequests[1];
    interface lane2DmaRequest = dut.dmaRequests[2];
    interface lane3DmaRequest = dut.dmaRequests[3];
    interface lane0DmaPayload = dut.dmaPayloads[0];
    interface lane1DmaPayload = dut.dmaPayloads[1];
    interface lane2DmaPayload = dut.dmaPayloads[2];
    interface lane3DmaPayload = dut.dmaPayloads[3];
    interface lane0DmaCompletion = dut.dmaCompletions[0];
    interface lane1DmaCompletion = dut.dmaCompletions[1];
    interface lane2DmaCompletion = dut.dmaCompletions[2];
    interface lane3DmaCompletion = dut.dmaCompletions[3];
    interface orderedFlagIn = dut.orderedFlagIn;
    interface flagDmaRequest = dut.flagDmaRequest;
    interface flagDmaResponse = dut.flagDmaResponse;
    interface orderedCredit = dut.orderedCredit;
    interface completion = dut.completion;
    interface feedback = dut.feedback;
    interface ecn = dut.ecn;
    interface txFeedback = dut.txFeedback;
    interface errors = dut.errors;
endmodule
