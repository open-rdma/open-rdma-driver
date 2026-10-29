import random

import cocotb
from cocotb.triggers import RisingEdge

from bfm.dma import DmaTB, run_dma_scenario
from model.dma import (
    DmaPayloadBeat,
    DmaScoreboard,
    completion_beats,
    expected_done,
    expected_payload,
    make_message,
)


@cocotb.test(timeout_time=8_000_000, timeout_unit="ns")
async def all_five_interfaces_propagate_backpressure(dut):
    tb = DmaTB(dut)
    await tb.reset()

    messages = [
        make_message(index, 0x500 + index, 0x500000 + index * 0x4000 + (index * 17 & 0x7F), 1800 + index * 37)
        for index in range(12)
    ]
    rng_message = random.Random(1)
    rng_request = random.Random(2)
    rng_completion = random.Random(3)
    rng_payload = random.Random(4)
    rng_done = random.Random(5)
    await run_dma_scenario(
        tb,
        messages,
        completion_order="random",
        rng=random.Random(6),
        message_stall=lambda: rng_message.randrange(3),
        request_stall=lambda: rng_request.randrange(5),
        completion_stall=lambda: rng_completion.randrange(4),
        payload_stall=lambda: rng_payload.randrange(6),
        done_stall=lambda: rng_done.randrange(8),
        event="all-interface-stalls",
    )


@cocotb.test(timeout_time=2_000_000, timeout_unit="ns")
async def stalled_payload_remains_bit_stable(dut):
    tb = DmaTB(dut)
    await tb.reset()

    message = make_message(0x44, 0x654, 0x700003F, 300)
    await tb.put_message(message)
    request = await tb.get_request()
    for completion in completion_beats(request):
        await tb.put_completion(completion)

    expected = expected_payload([message])
    for index, expected_beat in enumerate(expected[:-1]):
        actual = await tb.get_payload()
        DmaScoreboard.compare_struct(expected_beat, actual, "stable.payload", index)

    # 专门持有最后 payload；在它真正 deq 前，done 不得提前可见。
    while not await tb.payload.not_empty():
        await RisingEdge(tb.clock)
    held = await tb.payload.first()
    held_beat = DmaPayloadBeat.unpack(held)
    DmaScoreboard.compare_struct(
        expected[-1], held_beat, "stable.payload", len(expected) - 1
    )
    for _ in range(12):
        assert await tb.payload.not_empty()
        assert await tb.payload.first() == held
        assert not await tb.done.not_empty()
        await RisingEdge(tb.clock)

    actual_last = await tb.get_payload()
    DmaScoreboard.compare_struct(
        expected[-1], actual_last, "stable.payload", len(expected) - 1
    )
    actual_done = await tb.get_done()
    DmaScoreboard.compare_struct(expected_done([message])[0], actual_done, "stable.done", 0)


@cocotb.test(timeout_time=3_000_000, timeout_unit="ns")
async def full_done_fifo_blocks_only_the_last_payload_commit(dut):
    tb = DmaTB(dut)
    await tb.reset()

    messages = [
        make_message(index + 0x80, 0x700 + index, 0x800000 + index * 0x1000, 1)
        for index in range(3)
    ]

    async def respond() -> None:
        for _ in messages:
            request = await tb.get_request()
            beats = completion_beats(request)
            assert len(beats) == 1
            await tb.put_completion(beats[0])

    responder = cocotb.start_soon(respond())
    for message in messages:
        await tb.put_message(message)

    expected_payloads = expected_payload(messages)
    for index in range(2):
        actual = await tb.get_payload()
        DmaScoreboard.compare_struct(
            expected_payloads[index], actual, "done-full.payload", index
        )

    # 两条未消费 done 填满 doneQ；第三条 payload 仍 valid且稳定，但 deq
    # 的 RDY 必须为 0，直到下游释放一个 done slot。
    while not await tb.payload.not_empty():
        await RisingEdge(tb.clock)
    held = await tb.payload.first()
    for _ in range(8):
        assert await tb.payload.not_empty()
        assert await tb.payload.first() == held
        assert int(dut.RDY_payloadPipeOut_deq.value) == 0
        await RisingEdge(tb.clock)

    expected_dones = expected_done(messages)
    first_done = await tb.get_done()
    DmaScoreboard.compare_struct(expected_dones[0], first_done, "done-full.done", 0)

    third_payload = await tb.get_payload()
    DmaScoreboard.compare_struct(
        expected_payloads[2], third_payload, "done-full.payload", 2
    )
    for index in range(1, 3):
        actual_done = await tb.get_done()
        DmaScoreboard.compare_struct(
            expected_dones[index], actual_done, "done-full.done", index
        )
    await responder


@cocotb.test(timeout_time=2_000_000, timeout_unit="ns")
async def message_id_may_be_reused_after_last_payload_before_done_deq(dut):
    tb = DmaTB(dut)
    await tb.reset()

    first = make_message(0x99, 0x801, 0x900000, 1)
    second = make_message(0x99, 0x802, 0xA00000, 1)

    await tb.put_message(first)
    first_request = await tb.get_request()
    await tb.put_completion(completion_beats(first_request)[0])
    first_payload = await tb.get_payload()
    DmaScoreboard.compare_struct(
        expected_payload([first])[0], first_payload, "reuse.payload", 0
    )

    # 此时旧 done 尚未消费，但 last payload 已真正 deq，ID 已按契约释放。
    await tb.put_message(second)
    second_request = await tb.get_request()
    await tb.put_completion(completion_beats(second_request)[0])
    second_payload = await tb.get_payload()
    DmaScoreboard.compare_struct(
        expected_payload([second])[0], second_payload, "reuse.payload", 1
    )

    dones = expected_done([first, second])
    for index, expected_event in enumerate(dones):
        actual = await tb.get_done()
        DmaScoreboard.compare_struct(expected_event, actual, "reuse.done", index)
