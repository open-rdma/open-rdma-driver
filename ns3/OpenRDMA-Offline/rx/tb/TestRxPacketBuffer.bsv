import CommonTypes::*;
import RxTypes::*;
import RxPacketBuffer::*;

interface TestRxPacketBuffer;
    interface PipeIn#(RxWireBeat) lane0In;
    interface PipeIn#(RxWireBeat) lane1In;
    interface PipeIn#(RxWireBeat) lane2In;
    interface PipeIn#(RxWireBeat) lane3In;
    interface PipeOut#(RxBufferedFrame) lane0Frame;
    interface PipeOut#(RxBufferedFrame) lane1Frame;
    interface PipeOut#(RxBufferedFrame) lane2Frame;
    interface PipeOut#(RxBufferedFrame) lane3Frame;
    interface PipeIn#(RxPayloadReadRequest) lane0ReadIn;
    interface PipeIn#(RxPayloadReadRequest) lane1ReadIn;
    interface PipeIn#(RxPayloadReadRequest) lane2ReadIn;
    interface PipeIn#(RxPayloadReadRequest) lane3ReadIn;
    interface PipeOut#(RxPayloadReadResponse) lane0ReadOut;
    interface PipeOut#(RxPayloadReadResponse) lane1ReadOut;
    interface PipeOut#(RxPayloadReadResponse) lane2ReadOut;
    interface PipeOut#(RxPayloadReadResponse) lane3ReadOut;
    interface PipeIn#(RxBufferRelease) lane0ReleaseIn;
    interface PipeIn#(RxBufferRelease) lane1ReleaseIn;
    interface PipeIn#(RxBufferRelease) lane2ReleaseIn;
    interface PipeIn#(RxBufferRelease) lane3ReleaseIn;
    interface PipeOut#(RxError) lane0Error;
    interface PipeOut#(RxError) lane1Error;
    interface PipeOut#(RxError) lane2Error;
    interface PipeOut#(RxError) lane3Error;
endinterface

(* synthesize *)
module mkTestRxPacketBuffer(TestRxPacketBuffer);
    let dut <- mkRxPacketBuffer;
    interface lane0In = dut.ports[0];
    interface lane1In = dut.ports[1];
    interface lane2In = dut.ports[2];
    interface lane3In = dut.ports[3];
    interface lane0Frame = dut.frames[0];
    interface lane1Frame = dut.frames[1];
    interface lane2Frame = dut.frames[2];
    interface lane3Frame = dut.frames[3];
    interface lane0ReadIn = dut.readRequests[0];
    interface lane1ReadIn = dut.readRequests[1];
    interface lane2ReadIn = dut.readRequests[2];
    interface lane3ReadIn = dut.readRequests[3];
    interface lane0ReadOut = dut.readResponses[0];
    interface lane1ReadOut = dut.readResponses[1];
    interface lane2ReadOut = dut.readResponses[2];
    interface lane3ReadOut = dut.readResponses[3];
    interface lane0ReleaseIn = dut.releases[0];
    interface lane1ReleaseIn = dut.releases[1];
    interface lane2ReleaseIn = dut.releases[2];
    interface lane3ReleaseIn = dut.releases[3];
    interface lane0Error = dut.errors[0];
    interface lane1Error = dut.errors[1];
    interface lane2Error = dut.errors[2];
    interface lane3Error = dut.errors[3];
endmodule
