import os
import random

import cocotb

from dma_phasea_bfm import DmaPhaseATB, run_integration_scenario
from tests.test_end_to_end import CFG, make_case


@cocotb.test(timeout_time=20_000_000, timeout_unit="ns")
async def long_randomized_dma_phasea_pipeline(dut):
    tb = DmaPhaseATB(dut)
    await tb.reset()

    seed = int(os.getenv("DMA_PHASEA_SEED", "20260829"), 0)
    message_count = int(os.getenv("DMA_PHASEA_MESSAGE_COUNT", "128"), 0)
    rng = random.Random(seed)
    messages = []
    for index in range(message_count):
        request_id = (0x4000 + index) & 0xFFFF
        remaining = rng.randint(1, 8192)
        extents = []
        chunk_index = 0
        while remaining:
            length = min(remaining, rng.randint(1, 1024))
            page = 0x400000000 + index * 0x400000 + chunk_index * 0x4000
            page_offset = rng.randrange(4096 - length + 1)
            extents.append((page + page_offset, length))
            remaining -= length
            chunk_index += 1
        messages.append(
            make_case(
                request_id,
                extents,
                pmtu=rng.randrange(5),
                start_psn=rng.randrange(1 << 24),
                qpn=rng.randrange(1, 1 << 24),
                dest_qpn=rng.randrange(1, 1 << 24),
            )
        )

    await run_integration_scenario(
        tb,
        messages,
        CFG,
        timeout_ns=18_000_000,
    )
    dut._log.info(
        "PASS DMA Phase-A long seed=%d messages=%d chunks=%d packets=%d",
        seed,
        len(messages),
        sum(len(message.chunks) for message in messages),
        sum(len(message.packets) for message in messages),
    )
