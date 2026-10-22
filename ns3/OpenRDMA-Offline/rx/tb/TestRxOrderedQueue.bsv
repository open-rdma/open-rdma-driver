import CommonTypes::*;
import TxTypes::*;
import RxTypes::*;
import RxOrderedQueueManager::*;

interface TestRxOrderedQueue;
    method Action configureEpoch(RxEpoch value);
    method Action configure0(RxQpConfig cfg);
    method Action configure1(RxQpConfig cfg);
    method Action configure2(RxQpConfig cfg);
    method Action configure3(RxQpConfig cfg);
    method Action start;
    method TxPsn dmaAckPsn0;
    method TxPsn dmaAckPsn1;
    method TxPsn dmaAckPsn2;
    method TxPsn dmaAckPsn3;
    method Bool faulted;
    interface PipeIn#(RxDmaRetire) retireIn;
    interface PipeIn#(RxOrderedFlagIngress) orderedFlagIn;
    interface PipeOut#(RxFlagDmaRequest) flagRequestOut;
    interface PipeIn#(RxFlagDmaResponse) flagResponseIn;
    interface PipeOut#(RxOrderedCredit) creditOut;
    interface PipeOut#(RxCompletion) completionOut;
    interface PipeOut#(RxError) errorOut;
endinterface

(* synthesize *)
module mkTestRxOrderedQueue(TestRxOrderedQueue);
    let dut <- mkRxOrderedQueueManager;
    method Action configureEpoch(RxEpoch value); dut.configureEpoch(value); endmethod
    method Action configure0(RxQpConfig cfg); dut.configure(0, cfg); endmethod
    method Action configure1(RxQpConfig cfg); dut.configure(1, cfg); endmethod
    method Action configure2(RxQpConfig cfg); dut.configure(2, cfg); endmethod
    method Action configure3(RxQpConfig cfg); dut.configure(3, cfg); endmethod
    method Action start; dut.start; endmethod
    method TxPsn dmaAckPsn0 = dut.dmaAckPsn(0);
    method TxPsn dmaAckPsn1 = dut.dmaAckPsn(1);
    method TxPsn dmaAckPsn2 = dut.dmaAckPsn(2);
    method TxPsn dmaAckPsn3 = dut.dmaAckPsn(3);
    method Bool faulted = dut.faulted;
    interface retireIn = dut.retireIn;
    interface orderedFlagIn = dut.orderedFlagIn;
    interface flagRequestOut = dut.flagRequestOut;
    interface flagResponseIn = dut.flagResponseIn;
    interface creditOut = dut.creditOut;
    interface completionOut = dut.completionOut;
    interface errorOut = dut.errorOut;
endmodule
