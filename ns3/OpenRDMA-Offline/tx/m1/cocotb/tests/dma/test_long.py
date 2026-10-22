import os
import random

import cocotb

from bfm.dma import DmaTB, run_dma_scenario
from model.dma import make_message, split_message

SEED = int(os.getenv("DMA_MVP_SEED", "20260828"), 0)
MESSAGE_COUNT = int(os.getenv("DMA_MVP_MESSAGE_COUNT", "512"), 0)


@cocotb.test(timeout_time=120_000_000, timeout_unit="ns")
async def long_randomized_slot_reuse_and_max_length_split(dut):
    tb = DmaTB(dut)
    await tb.reset()

    # 先独立证明完整 25-bit length 的拆分计数、首尾和覆盖；DUT stress
    # 使用大量中等 message，避免在普通 long 回归中复制数十 MiB Python 对象。
    maximum = make_message(0xFFFE, 0xABCDE, 0x1FFF, 0x1FFFFFF)
    maximum_bursts = split_message(maximum)
    assert len(maximum_bursts) == 32769
    assert maximum_bursts[0].message_offset == 0
    assert maximum_bursts[-1].message_offset + maximum_bursts[-1].length == 0x1FFFFFF
    assert all(1 <= burst.length <= 1024 for burst in maximum_bursts)
    assert all(
        (burst.physical_addr & 0xFFF) + burst.length <= 4096
        for burst in maximum_bursts
    )

    rng = random.Random(SEED)
    messages = []
    for index in range(MESSAGE_COUNT):
        messages.append(
            make_message(
                index & 0xFFFF,
                rng.randrange(1 << 24),
                0x40000000 + index * 0x10000 + rng.randrange(4096),
                rng.randint(1, 4096),
            )
        )

    completion_rng = random.Random(SEED ^ 0xA5A5)
    stall_rng = random.Random(SEED ^ 0x5A5A)
    requests = await run_dma_scenario(
        tb,
        messages,
        completion_order="random",
        rng=completion_rng,
        completion_stall=lambda: stall_rng.randrange(2),
        payload_stall=lambda: stall_rng.randrange(3),
        done_stall=lambda: stall_rng.randrange(3),
        timeout_cycles=20_000_000,
        event=f"long seed={SEED}",
    )
    dut._log.info(
        "PASS long seed=%d messages=%d requests=%d",
        SEED,
        len(messages),
        len(requests),
    )
