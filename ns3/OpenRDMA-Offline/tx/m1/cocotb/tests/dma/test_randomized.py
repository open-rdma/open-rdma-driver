import os
import random

import cocotb

from bfm.dma import DmaTB, run_dma_scenario
from model.dma import make_chunk_message

SEED = int(os.getenv("DMA_MVP_SEED", "20260828"), 0)
MESSAGE_COUNT = int(os.getenv("DMA_MVP_MESSAGE_COUNT", "32"), 0)


@cocotb.test(timeout_time=30_000_000, timeout_unit="ns")
async def randomized_messages_match_reference_under_reorder_and_stalls(dut):
    tb = DmaTB(dut)
    await tb.reset()

    rng = random.Random(SEED)
    lengths = (1, 2, 31, 32, 127, 128, 129, 300, 1023, 1024, 1025, 4097, 8192, 16383)
    messages = []
    base = 0x10000000
    for index in range(MESSAGE_COUNT):
        length = rng.choice(lengths) if rng.random() < 0.7 else rng.randint(1, 20000)
        remaining = length
        extents = []
        chunk_index = 0
        while remaining:
            chunk_length = min(remaining, rng.randint(1, 1024))
            page_base = base + index * 0x200000 + chunk_index * 0x2000
            page_offset = rng.randrange(4096 - chunk_length + 1)
            extents.append((page_base + page_offset, chunk_length))
            remaining -= chunk_length
            chunk_index += 1
        messages.append(make_chunk_message(index, extents))

    completion_rng = random.Random(SEED ^ 0x1111)
    request_rng = random.Random(SEED ^ 0x2222)
    input_rng = random.Random(SEED ^ 0x3333)
    completion_stall_rng = random.Random(SEED ^ 0x4444)
    payload_rng = random.Random(SEED ^ 0x5555)
    done_rng = random.Random(SEED ^ 0x6666)
    requests = await run_dma_scenario(
        tb,
        messages,
        completion_order="random",
        rng=completion_rng,
        message_stall=lambda: input_rng.randrange(3),
        request_stall=lambda: request_rng.randrange(3),
        completion_stall=lambda: completion_stall_rng.randrange(3),
        payload_stall=lambda: payload_rng.randrange(4),
        done_stall=lambda: done_rng.randrange(5),
        event=f"random seed={SEED}",
    )
    dut._log.info(
        "PASS seed=%d messages=%d requests=%d",
        SEED,
        len(messages),
        len(requests),
    )
