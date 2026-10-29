import random

import cocotb

from bfm.dma import DmaTB, run_dma_scenario
from model.dma import make_message


@cocotb.test(timeout_time=5_000_000, timeout_unit="ns")
async def eight_outstanding_bursts_complete_in_reverse_order(dut):
    tb = DmaTB(dut)
    await tb.reset()

    # 7168B 从 offset=1 开始会产生 8 个 burst，其中多个 1024B burst
    # 各覆盖 9 个 memory window，先填满整个 outstanding window。
    messages = [make_message(1, 0x101, 0x1000001, 7168)]
    requests = await run_dma_scenario(
        tb,
        messages,
        completion_order="reverse",
        event="reverse-eight",
    )
    assert len(requests) == 8
    assert [int(req.chunkSeq) for req in requests] == list(range(8))


@cocotb.test(timeout_time=5_000_000, timeout_unit="ns")
async def completion_beats_interleave_across_tags(dut):
    tb = DmaTB(dut)
    await tb.reset()

    messages = [make_message(2, 0x202, 0x2000001, 7168)]
    await run_dma_scenario(
        tb,
        messages,
        completion_order="round_robin",
        event="beat-interleave",
    )


@cocotb.test(timeout_time=5_000_000, timeout_unit="ns")
async def later_messages_complete_before_global_head(dut):
    tb = DmaTB(dut)
    await tb.reset()

    messages = [
        make_message(index + 10, 0x300 + index, 0x300000 + index * 0x2000 + 0x7F, 300)
        for index in range(8)
    ]
    await run_dma_scenario(
        tb,
        messages,
        completion_order="head_last",
        event="message-head-last",
    )


@cocotb.test(timeout_time=5_000_000, timeout_unit="ns")
async def randomized_tag_interleave_preserves_issue_order(dut):
    tb = DmaTB(dut)
    await tb.reset()

    rng = random.Random(0xD0A0)
    messages = [make_message(100, 0x444, 0x440007F, 9000)]
    await run_dma_scenario(
        tb,
        messages,
        completion_order="random",
        rng=rng,
        event="random-interleave",
    )
