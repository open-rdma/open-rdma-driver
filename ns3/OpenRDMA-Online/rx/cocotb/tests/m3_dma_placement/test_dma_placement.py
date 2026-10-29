from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge, with_timeout

from cocotb_bsv import ActionMethod, PipeIn, PipeOut, ValueMethod
from bfm.common import assert_pipe_empty, pipe_get, pipe_put, reset_dut
from model.rx_types import (
    RxAdmittedPacket,
    RxBufferRelease,
    RxDmaCompletion,
    RxDmaPayloadBeat,
    RxDmaRetire,
    RxDmaWriteRequest,
    RxError,
    RxPayloadReadRequest,
    RxPayloadReadResponse,
    tag,
)


class DmaTB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.configure_epoch = ActionMethod(dut, "configureEpoch", self.clock)
        self.admitted = [PipeIn(dut, f"lane{i}AdmittedIn", self.clock) for i in range(4)]
        self.requests = [PipeOut(dut, f"lane{i}RequestOut", self.clock) for i in range(4)]
        self.payloads = [PipeOut(dut, f"lane{i}PayloadOut", self.clock) for i in range(4)]
        self.completions = [PipeIn(dut, f"lane{i}CompletionIn", self.clock) for i in range(4)]
        self.read_requests = [PipeOut(dut, f"lane{i}ReadRequestOut", self.clock) for i in range(4)]
        self.read_responses = [PipeIn(dut, f"lane{i}ReadResponseIn", self.clock) for i in range(4)]
        self.releases = [PipeOut(dut, f"lane{i}ReleaseOut", self.clock) for i in range(4)]
        self.errors = [PipeOut(dut, f"lane{i}ErrorOut", self.clock) for i in range(4)]
        self.retire = PipeOut(dut, "retireOut", self.clock)
        self.head = ValueMethod(dut, "retireHead", self.clock)
        self.fault = ValueMethod(dut, "faulted", self.clock)

    async def initialize(self, epoch: int = 1) -> None:
        await self.configure_epoch(value=epoch)

    async def service_packet(self, lane: int, frame: bytes, expected_payload: bytes):
        request = await pipe_get(self.requests[lane], self.clock, RxDmaWriteRequest)
        rebuilt = bytearray()
        while len(rebuilt) < len(expected_payload):
            read = await pipe_get(
                self.read_requests[lane], self.clock, RxPayloadReadRequest
            )
            word = int(read.wordIndex)
            low = frame[word * 32 : word * 32 + 32].ljust(32, b"\x00")
            high = frame[(word + 1) * 32 : (word + 2) * 32].ljust(32, b"\x00")
            response = RxPayloadReadResponse(
                tag=read.tag,
                wordIndex=word,
                low=int.from_bytes(low, "little"),
                high=int.from_bytes(high, "little"),
                valid=1,
            )
            await pipe_put(self.read_responses[lane], self.clock, response)
            beat = await pipe_get(self.payloads[lane], self.clock, RxDmaPayloadBeat)
            assert int(beat.epoch) == int(request.epoch)
            valid = int(beat.be).bit_count()
            assert int(beat.be) == (1 << valid) - 1
            rebuilt += int(beat.data).to_bytes(32, "little")[:valid]
        assert bytes(rebuilt) == expected_payload
        return request


def admitted_packet(
    lane: int,
    seq: int,
    psn: int,
    payload_start: int,
    payload_bytes: int,
    *,
    message_complete: int = 0,
) -> RxAdmittedPacket:
    return RxAdmittedPacket(
        globalSeq=seq,
        payloadTag=tag(lane, seq & 0xF, 3),
        lane=lane,
        qp=lane,
        qpn=10 + lane,
        psn=psn,
        dmaAddr=0x8000 + seq * 0x1000,
        payloadStart=payload_start,
        payloadBytes=payload_bytes,
        messageBytes=payload_bytes,
        first=1,
        last=1,
        ackValid=1,
        ackPsnAfterMerge=psn,
        messageComplete=message_complete,
        messageLastPsn=psn,
    )


@cocotb.test()
async def test_descriptor_handshake_precedes_payload_read_and_write(dut):
    tb = DmaTB(dut)
    await reset_dut(dut)
    await tb.initialize()
    payload = bytes(range(33))
    packet = admitted_packet(0, 0, 40, 70, len(payload))
    await pipe_put(tb.admitted[0], tb.clock, packet)

    for _ in range(20):
        await RisingEdge(tb.clock)
    assert await tb.requests[0].not_empty(), "DMA descriptor missing"
    request_before = RxDmaWriteRequest.unpack(await tb.requests[0].first())
    for _ in range(12):
        await RisingEdge(tb.clock)
        request_now = RxDmaWriteRequest.unpack(await tb.requests[0].first())
        assert request_now.pack() == request_before.pack(), "descriptor changed under stall"
        assert not await tb.read_requests[0].not_empty(), "buffer read before descriptor acceptance"
        assert not await tb.payloads[0].not_empty(), "payload overtook stalled descriptor"

    await tb.requests[0].deq()
    frame = bytes(70) + payload
    rebuilt = bytearray()
    while len(rebuilt) < len(payload):
        read = await pipe_get(tb.read_requests[0], tb.clock, RxPayloadReadRequest)
        word = int(read.wordIndex)
        await pipe_put(
            tb.read_responses[0],
            tb.clock,
            RxPayloadReadResponse(
                tag=read.tag,
                wordIndex=word,
                low=int.from_bytes(frame[32 * word : 32 * word + 32].ljust(32, b"\0"), "little"),
                high=int.from_bytes(frame[32 * (word + 1) : 32 * (word + 2)].ljust(32, b"\0"), "little"),
                valid=1,
            ),
        )
        beat = await pipe_get(tb.payloads[0], tb.clock, RxDmaPayloadBeat)
        assert int(beat.epoch) == int(request_before.epoch)
        valid_bytes = int(beat.be).bit_count()
        rebuilt.extend(int(beat.data).to_bytes(32, "little")[:valid_bytes])
    assert bytes(rebuilt) == payload
    await pipe_put(
        tb.completions[0], tb.clock,
        RxDmaCompletion(globalSeq=0, epoch=1, lane=0, success=1),
    )
    retired = await pipe_get(tb.retire, tb.clock, RxDmaRetire)
    assert int(retired.globalSeq) == 0
    await assert_pipe_empty(tb.errors[0], tb.clock)


@cocotb.test()
async def test_four_dma_lanes_reverse_completion_ordered_retire(dut):
    tb = DmaTB(dut)
    await reset_dut(dut)
    await tb.initialize()
    sizes = [1, 33, 228, 244]
    starts = [70, 54, 70, 54]
    frames = []
    payloads = []
    packets = []
    for lane in range(4):
        payload = bytes((lane * 53 + index) & 0xFF for index in range(sizes[lane]))
        frame = bytes((index * 7) & 0xFF for index in range(starts[lane])) + payload
        packet = admitted_packet(lane, lane, 100 + lane, starts[lane], len(payload))
        frames.append(frame)
        payloads.append(payload)
        packets.append(packet)
        await pipe_put(tb.admitted[lane], tb.clock, packet)

    tasks = [
        cocotb.start_soon(tb.service_packet(lane, frames[lane], payloads[lane]))
        for lane in range(4)
    ]
    requests = [await task for task in tasks]
    for lane, request in enumerate(requests):
        assert int(request.globalSeq) == lane
        assert int(request.lane) == lane
        assert int(request.addr) == 0x8000 + lane * 0x1000
        assert int(request.bytes) == sizes[lane]

    completion_cycles = {}
    for lane in (3, 2, 1):
        await pipe_put(
            tb.completions[lane],
            tb.clock,
            RxDmaCompletion(globalSeq=lane, epoch=1, lane=lane, success=1),
        )
        completion_cycles[lane] = int(cocotb.utils.get_sim_time(unit="ns"))
    for _ in range(8):
        await RisingEdge(tb.clock)
    assert int(await tb.head()) == 0
    assert not await tb.retire.not_empty(), "retired across a global sequence gap"

    await pipe_put(
        tb.completions[0],
        tb.clock,
        RxDmaCompletion(globalSeq=0, epoch=1, lane=0, success=1),
    )
    retired = [await pipe_get(tb.retire, tb.clock, RxDmaRetire) for _ in range(4)]
    assert [int(item.globalSeq) for item in retired] == [0, 1, 2, 3]
    assert int(await tb.head()) == 4
    for lane in range(4):
        release = await pipe_get(tb.releases[lane], tb.clock, RxBufferRelease)
        assert int(release.tag.pack()) == int(packets[lane].payloadTag.pack())
        await assert_pipe_empty(tb.errors[lane], tb.clock)
    assert int(await tb.fault()) == 0


@cocotb.test()
async def test_completion_identity_mismatch_is_fail_stop(dut):
    tb = DmaTB(dut)
    await reset_dut(dut)
    await tb.initialize()
    payload = bytes(range(40))
    start = 70
    frame = bytes(start) + payload
    packet = admitted_packet(0, 0, 7, start, len(payload))
    await pipe_put(tb.admitted[0], tb.clock, packet)
    await tb.service_packet(0, frame, payload)
    await pipe_put(
        tb.completions[0],
        tb.clock,
        RxDmaCompletion(globalSeq=99, epoch=1, lane=0, success=1),
    )
    error = await pipe_get(tb.errors[0], tb.clock, RxError)
    release = await pipe_get(tb.releases[0], tb.clock, RxBufferRelease)
    assert int(error.code) == 9
    assert int(release.tag.pack()) == int(packet.payloadTag.pack())
    assert int(await tb.fault()) == 1
    assert int(await tb.head()) == 0
    await assert_pipe_empty(tb.retire, tb.clock)


@cocotb.test()
async def test_max_payload_alignment_and_tail_mask(dut):
    tb = DmaTB(dut)
    await reset_dut(dut)
    await tb.initialize()
    size = 4084
    start = 54
    payload = bytes((index * 13 + 5) & 0xFF for index in range(size))
    frame = bytes((index * 3) & 0xFF for index in range(start)) + payload
    packet = admitted_packet(2, 0, 55, start, size, message_complete=1)
    await pipe_put(tb.admitted[2], tb.clock, packet)
    request = await tb.service_packet(2, frame, payload)
    assert int(request.bytes) == size
    await pipe_put(
        tb.completions[2],
        tb.clock,
        RxDmaCompletion(globalSeq=0, epoch=1, lane=2, success=1),
    )
    retired = await pipe_get(tb.retire, tb.clock, RxDmaRetire)
    assert int(retired.globalSeq) == 0
    assert int(retired.messageComplete) == 1


@cocotb.test()
async def test_stale_completion_from_previous_reset_epoch_is_rejected(dut):
    tb = DmaTB(dut)
    await reset_dut(dut)
    await tb.initialize(epoch=1)
    payload = b"old-epoch"
    frame = bytes(70) + payload
    old_packet = admitted_packet(0, 0, 10, 70, len(payload))
    await pipe_put(tb.admitted[0], tb.clock, old_packet)
    old_request = await tb.service_packet(0, frame, payload)
    assert int(old_request.epoch) == 1

    dut.RST_N.value = 0
    for _ in range(4):
        await RisingEdge(tb.clock)
    dut.RST_N.value = 1
    for _ in range(4):
        await RisingEdge(tb.clock)

    tb = DmaTB(dut)
    await tb.initialize(epoch=2)
    new_payload = b"new-epoch"
    new_frame = bytes(70) + new_payload
    new_packet = admitted_packet(0, 0, 11, 70, len(new_payload))
    await pipe_put(tb.admitted[0], tb.clock, new_packet)
    new_request = await tb.service_packet(0, new_frame, new_payload)
    assert int(new_request.epoch) == 2
    await pipe_put(
        tb.completions[0],
        tb.clock,
        RxDmaCompletion(globalSeq=0, epoch=1, lane=0, success=1),
    )
    error = await pipe_get(tb.errors[0], tb.clock, RxError)
    assert int(error.code) == 9
    assert int(await tb.fault()) == 1
    assert int(await tb.head()) == 0
