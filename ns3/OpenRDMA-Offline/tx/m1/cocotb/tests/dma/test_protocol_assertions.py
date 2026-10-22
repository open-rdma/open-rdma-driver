import os

import cocotb
from cocotb.triggers import RisingEdge

from bfm.dma import DmaTB
from model.dma import DmaChunkReq, DmaCompletionBeat, completion_beats, make_message


async def _issued_request(tb: DmaTB, *, length: int = 300, addr: int = 0x100000):
    await tb.put_message(make_message(1, 2, addr, length))
    return await tb.get_request()


@cocotb.test()
async def selected_protocol_violation_triggers_dynamic_assert(dut):
    case = os.environ["DMA_NEGATIVE_CASE"]
    tb = DmaTB(dut)
    await tb.reset()

    if case == "unknown_tag":
        await tb.put_completion(
            DmaCompletionBeat(tag=0, windowAddr=0, data=0, keep=1, burstLast=1)
        )
    elif case == "zero_keep":
        request = await _issued_request(tb, length=1)
        await tb.put_completion(
            DmaCompletionBeat(
                tag=int(request.tag),
                windowAddr=int(request.physAddr) & ~0x7F,
                data=0,
                keep=0,
                burstLast=1,
            )
        )
    elif case == "noncontiguous_keep":
        request = await _issued_request(tb, length=3)
        await tb.put_completion(
            DmaCompletionBeat(
                tag=int(request.tag),
                windowAddr=int(request.physAddr) & ~0x7F,
                data=0,
                keep=0b101,
                burstLast=1,
            )
        )
    elif case == "early_last":
        request = await _issued_request(tb, length=300)
        first = completion_beats(request)[0]
        await tb.put_completion(
            DmaCompletionBeat(
                tag=int(first.tag),
                windowAddr=int(first.windowAddr),
                data=int(first.data),
                keep=int(first.keep),
                burstLast=1,
            )
        )
    elif case == "late_last":
        request = await _issued_request(tb, length=1)
        only = completion_beats(request)[0]
        await tb.put_completion(
            DmaCompletionBeat(
                tag=int(only.tag),
                windowAddr=int(only.windowAddr),
                data=int(only.data),
                keep=int(only.keep),
                burstLast=0,
            )
        )
    elif case == "over_completion":
        request = await _issued_request(tb, length=1)
        await tb.put_completion(
            DmaCompletionBeat(
                tag=int(request.tag),
                windowAddr=int(request.physAddr) & ~0x7F,
                data=0,
                keep=0b11,
                burstLast=1,
            )
        )
    elif case == "late_completion":
        request = await _issued_request(tb, length=1)
        only = completion_beats(request)[0]
        await tb.put_completion(only)
        for _ in range(12):
            await RisingEdge(tb.clock)
        await tb.put_completion(only)
    elif case == "duplicate_message_id_stalled_last":
        request = await _issued_request(tb, length=1)
        await tb.put_completion(completion_beats(request)[0])
        while not await tb.payload.not_empty():
            await RisingEdge(tb.clock)
        # 保持最后 payload valid但不 deq，此时 active ID 必须仍被占用。
        await tb.put_message(make_message(1, 3, 0x400000, 1))
    elif case == "duplicate_message_id":
        await tb.put_message(make_message(0x55, 1, 0x200000, 1))
        await tb.put_message(make_message(0x55, 2, 0x300000, 1))
    elif case == "address_overflow":
        await tb.put_chunk(
            DmaChunkReq(
                physAddr=(1 << 64) - 1,
                len=2,
                wqeTag=0x66,
                chunkSeq=0,
                isFirst=1,
                isLast=1,
            )
        )
    elif case == "zero_chunk":
        await tb.put_chunk(
            DmaChunkReq(
                physAddr=0x100000,
                len=0,
                wqeTag=0x70,
                chunkSeq=0,
                isFirst=1,
                isLast=1,
            )
        )
    elif case == "chunk_too_large":
        await tb.put_chunk(
            DmaChunkReq(
                physAddr=0x100000,
                len=1025,
                wqeTag=0x71,
                chunkSeq=0,
                isFirst=1,
                isLast=1,
            )
        )
    elif case == "chunk_crosses_4k":
        await tb.put_chunk(
            DmaChunkReq(
                physAddr=0x100FFF,
                len=2,
                wqeTag=0x72,
                chunkSeq=0,
                isFirst=1,
                isLast=1,
            )
        )
    elif case == "missing_first":
        await tb.put_chunk(
            DmaChunkReq(
                physAddr=0x100000,
                len=1,
                wqeTag=0x73,
                chunkSeq=0,
                isFirst=0,
                isLast=1,
            )
        )
    elif case == "chunk_sequence":
        await tb.put_chunk(
            DmaChunkReq(
                physAddr=0x100000,
                len=1,
                wqeTag=0x74,
                chunkSeq=0,
                isFirst=1,
                isLast=0,
            )
        )
        await tb.put_chunk(
            DmaChunkReq(
                physAddr=0x200000,
                len=1,
                wqeTag=0x74,
                chunkSeq=2,
                isFirst=0,
                isLast=1,
            )
        )
    elif case == "chunk_tag_changed":
        await tb.put_chunk(
            DmaChunkReq(
                physAddr=0x100000,
                len=1,
                wqeTag=0x75,
                chunkSeq=0,
                isFirst=1,
                isLast=0,
            )
        )
        await tb.put_chunk(
            DmaChunkReq(
                physAddr=0x200000,
                len=1,
                wqeTag=0x76,
                chunkSeq=1,
                isFirst=0,
                isLast=1,
            )
        )
    else:
        raise ValueError(f"unknown negative case: {case}")

    for _ in range(100):
        await RisingEdge(tb.clock)
    dut._log.error("DMA_ASSERTION_DID_NOT_FIRE_%s", case)
    raise AssertionError(f"expected dynamicAssert did not fire: {case}")
