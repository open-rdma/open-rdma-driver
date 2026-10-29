import CommonTypes::*;
import RxTypes::*;
import RxRqBitmapManager::*;

interface TestRxRqBitmap;
    method Action configure0(RxQpConfig cfg);
    method Action configure1(RxQpConfig cfg);
    method Action configure2(RxQpConfig cfg);
    method Action configure3(RxQpConfig cfg);
    method Action start;
    method RxReceiveProgress progress0;
    method RxReceiveProgress progress1;
    method RxReceiveProgress progress2;
    method RxReceiveProgress progress3;
    method RxGlobalSeq nextGlobalSeq;
    method Bool faulted;
    interface PipeIn#(RxParsedPacket) lane0PacketIn;
    interface PipeIn#(RxParsedPacket) lane1PacketIn;
    interface PipeIn#(RxParsedPacket) lane2PacketIn;
    interface PipeIn#(RxParsedPacket) lane3PacketIn;
    interface PipeOut#(RxAdmittedPacket) lane0AdmittedOut;
    interface PipeOut#(RxAdmittedPacket) lane1AdmittedOut;
    interface PipeOut#(RxAdmittedPacket) lane2AdmittedOut;
    interface PipeOut#(RxAdmittedPacket) lane3AdmittedOut;
    interface PipeOut#(RxBufferRelease) lane0ReleaseOut;
    interface PipeOut#(RxBufferRelease) lane1ReleaseOut;
    interface PipeOut#(RxBufferRelease) lane2ReleaseOut;
    interface PipeOut#(RxBufferRelease) lane3ReleaseOut;
    interface PipeOut#(RxFeedback) feedbackOut;
    interface PipeOut#(RxError) errorOut;
endinterface

(* synthesize *)
module mkTestRxRqBitmap(TestRxRqBitmap);
    let dut <- mkRxRqBitmapManager;
    method Action configure0(RxQpConfig cfg); dut.configure(0, cfg); endmethod
    method Action configure1(RxQpConfig cfg); dut.configure(1, cfg); endmethod
    method Action configure2(RxQpConfig cfg); dut.configure(2, cfg); endmethod
    method Action configure3(RxQpConfig cfg); dut.configure(3, cfg); endmethod
    method Action start; dut.start; endmethod
    method RxReceiveProgress progress0 = dut.progress(0);
    method RxReceiveProgress progress1 = dut.progress(1);
    method RxReceiveProgress progress2 = dut.progress(2);
    method RxReceiveProgress progress3 = dut.progress(3);
    method RxGlobalSeq nextGlobalSeq = dut.nextGlobalSeq;
    method Bool faulted = dut.faulted;
    interface lane0PacketIn = dut.packets[0];
    interface lane1PacketIn = dut.packets[1];
    interface lane2PacketIn = dut.packets[2];
    interface lane3PacketIn = dut.packets[3];
    interface lane0AdmittedOut = dut.admitted[0];
    interface lane1AdmittedOut = dut.admitted[1];
    interface lane2AdmittedOut = dut.admitted[2];
    interface lane3AdmittedOut = dut.admitted[3];
    interface lane0ReleaseOut = dut.releases[0];
    interface lane1ReleaseOut = dut.releases[1];
    interface lane2ReleaseOut = dut.releases[2];
    interface lane3ReleaseOut = dut.releases[3];
    interface feedbackOut = dut.feedbackOut;
    interface errorOut = dut.errorOut;
endmodule
