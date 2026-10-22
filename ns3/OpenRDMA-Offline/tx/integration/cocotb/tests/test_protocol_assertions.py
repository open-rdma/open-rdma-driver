import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from cocotb_bsv import PipeIn

from bfm.m2 import make_message_context
from dma_phasea_model import DmaChunkReq


CASES = {
    "context_tag_mismatch": [
        dict(physAddr=0x100000, len=10, wqeTag=2, chunkSeq=0, isFirst=1, isLast=1)
    ],
    "last_length_mismatch": [
        dict(physAddr=0x100000, len=9, wqeTag=1, chunkSeq=0, isFirst=1, isLast=1)
    ],
    "missing_last_flag": [
        dict(physAddr=0x100000, len=10, wqeTag=1, chunkSeq=0, isFirst=1, isLast=0)
    ],
    "chunk_sequence": [
        dict(physAddr=0x100000, len=5, wqeTag=1, chunkSeq=0, isFirst=1, isLast=0),
        dict(physAddr=0x200000, len=5, wqeTag=1, chunkSeq=2, isFirst=0, isLast=1),
    ],
}


@cocotb.test(timeout_time=100_000, timeout_unit="ns")
async def selected_integration_violation_triggers_assert(dut):
    clock = dut.CLK
    resetn = dut.RST_N
    context_pipe = PipeIn(dut, "contextPipeIn", clock)
    chunk_pipe = PipeIn(dut, "chunkPipeIn", clock)
    cocotb.start_soon(Clock(clock, 2, unit="ns").start())
    resetn.value = 0
    for _ in range(4):
        await RisingEdge(clock)
    resetn.value = 1
    for _ in range(4):
        await RisingEdge(clock)

    case = os.environ["DMA_PHASEA_NEGATIVE_CASE"]
    if case not in CASES:
        raise RuntimeError(f"unknown negative case: {case}")

    context = make_message_context(
        length=10,
        request_id=1,
        qpn=2,
        dest_qpn=3,
        pmtu=0,
    )
    await context_pipe.enq(context.pack())
    for fields in CASES[case]:
        await chunk_pipe.enq(DmaChunkReq(**fields).pack())

    for _ in range(30):
        await RisingEdge(clock)
    raise AssertionError(f"DMA_PHASEA_ASSERTION_DID_NOT_FIRE_{case}")
