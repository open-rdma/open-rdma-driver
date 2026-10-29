package TestRx;
import RxTop::*;

// Compatibility elaboration target; executable regressions live in cocotb/tests.
(* synthesize *)
module mkTestRx(RxTopIfc);
    let dut <- mkRxTop;
    return dut;
endmodule
endpackage
