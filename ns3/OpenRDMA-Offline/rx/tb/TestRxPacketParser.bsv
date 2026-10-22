import CommonTypes::*;
import RxTypes::*;
import RxPacketParser::*;

interface TestRxPacketParser;
    method Action configure(RxNetworkConfig cfg);
    interface PipeIn#(RxBufferedFrame) lane0FrameIn;
    interface PipeIn#(RxBufferedFrame) lane1FrameIn;
    interface PipeIn#(RxBufferedFrame) lane2FrameIn;
    interface PipeIn#(RxBufferedFrame) lane3FrameIn;
    interface PipeOut#(RxParsedPacket) lane0PacketOut;
    interface PipeOut#(RxParsedPacket) lane1PacketOut;
    interface PipeOut#(RxParsedPacket) lane2PacketOut;
    interface PipeOut#(RxParsedPacket) lane3PacketOut;
    interface PipeOut#(RxBufferRelease) lane0ReleaseOut;
    interface PipeOut#(RxBufferRelease) lane1ReleaseOut;
    interface PipeOut#(RxBufferRelease) lane2ReleaseOut;
    interface PipeOut#(RxBufferRelease) lane3ReleaseOut;
    interface PipeOut#(RxTxFeedback) feedbackOut;
    interface PipeOut#(RxEcnEvent) ecnOut;
    interface PipeOut#(RxError) errorOut;
endinterface

(* synthesize *)
module mkTestRxPacketParser(TestRxPacketParser);
    let dut <- mkRxPacketParser;
    method Action configure(RxNetworkConfig cfg);
        dut.configure(cfg);
    endmethod
    interface lane0FrameIn = dut.frames[0];
    interface lane1FrameIn = dut.frames[1];
    interface lane2FrameIn = dut.frames[2];
    interface lane3FrameIn = dut.frames[3];
    interface lane0PacketOut = dut.packets[0];
    interface lane1PacketOut = dut.packets[1];
    interface lane2PacketOut = dut.packets[2];
    interface lane3PacketOut = dut.packets[3];
    interface lane0ReleaseOut = dut.releases[0];
    interface lane1ReleaseOut = dut.releases[1];
    interface lane2ReleaseOut = dut.releases[2];
    interface lane3ReleaseOut = dut.releases[3];
    interface feedbackOut = dut.feedbackOut;
    interface ecnOut = dut.ecnOut;
    interface errorOut = dut.errorOut;
endmodule
