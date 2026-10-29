import cocotb

from bfm.dma import DmaTB, run_dma_scenario
from model.dma import (
    DmaChunkReq,
    DmaCompletionBeat,
    DmaMessageDone,
    DmaPayloadBeat,
    DmaReadRequest,
    make_chunk_message,
    make_message,
)


@cocotb.test()
async def wire_widths_round_trip_and_idle_enables(dut):
    tb = DmaTB(dut)
    await tb.reset()

    assert DmaChunkReq.width() == 130
    assert DmaReadRequest.width() == 157
    assert DmaCompletionBeat.width() == 1233
    assert DmaPayloadBeat.width() == 1202
    assert DmaMessageDone.width() == 65

    sample = make_message(0x1234, 0, 0x123456789ABC, 0x1FFFF).chunks[0]
    unpacked = DmaChunkReq.unpack(sample.pack())
    for field in sample._members_def:
        assert int(getattr(unpacked, field)) == int(getattr(sample, field))

    for name in (
        "EN_chunkPipeIn_enq",
        "EN_dmaReadReqPipeOut_deq",
        "EN_dmaCompletionPipeIn_enq",
        "EN_payloadPipeOut_deq",
        "EN_messageDonePipeOut_deq",
    ):
        assert int(getattr(dut, name).value) == 0, f"{name} is not idle"


@cocotb.test(timeout_time=5_000_000, timeout_unit="ns")
async def directed_lengths_and_alignment_boundaries(dut):
    tb = DmaTB(dut)
    await tb.reset()

    lengths = (1, 31, 32, 127, 128, 129, 1023, 1024, 1025)
    messages = [
        make_message(index, 0x100 + index, 0x100000 + index * 0x10000, length)
        for index, length in enumerate(lengths)
    ]
    # 128B window offsets 1/63/127，以及距 physical 4KiB boundary
    # 1/127/128/1023B 的起点。
    extra = (
        (0x01, 300),
        (0x3F, 300),
        (0x7F, 300),
        (0xFFF, 300),
        (0xF81, 600),
        (0xF80, 600),
        (0xC01, 2500),
    )
    for local_index, (offset, length) in enumerate(extra, start=len(messages)):
        messages.append(
            make_message(
                local_index,
                0x200 + local_index,
                0x300000 + local_index * 0x10000 + offset,
                length,
            )
        )

    await run_dma_scenario(tb, messages, event="directed-boundaries")


@cocotb.test(timeout_time=2_000_000, timeout_unit="ns")
async def adjacent_bursts_may_share_one_memory_window(dut):
    tb = DmaTB(dut)
    await tb.reset()

    # 起点 offset=1，首个 1024B burst 的尾 window 和下一个 burst 的首
    # window相同，但两拍 keep 覆盖互不重叠的连续 byte run。
    messages = [make_message(0x55, 0x123, 0x800001, 2050)]
    await run_dma_scenario(tb, messages, event="shared-window")


@cocotb.test(timeout_time=2_000_000, timeout_unit="ns")
async def discontinuous_physical_chunks_preserve_logical_message_order(dut):
    tb = DmaTB(dut)
    await tb.reset()

    message = make_chunk_message(
        0x77,
        (
            (0x100007F, 513),
            (0x9000001, 1024),
            (0x3000F80, 128),
            (0xA00003F, 300),
        ),
    )
    requests = await run_dma_scenario(
        tb,
        [message],
        completion_order="reverse",
        event="discontinuous-pa",
    )
    assert [int(request.physAddr) for request in requests] == [
        0x100007F,
        0x9000001,
        0x3000F80,
        0xA00003F,
    ]
