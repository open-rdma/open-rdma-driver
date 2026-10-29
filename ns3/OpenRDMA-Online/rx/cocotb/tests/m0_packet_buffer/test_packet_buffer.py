from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge

from cocotb_bsv import PipeIn, PipeOut
from bfm.common import assert_pipe_empty, pipe_get, pipe_put, reset_dut
from model.rx_types import (
    RxBufferedFrame,
    RxBufferRelease,
    RxError,
    RxPayloadReadRequest,
    RxPayloadReadResponse,
    RxWireBeat,
)
from model.wire import (
    OP_WRITE_FIRST,
    build_frame,
    wire_beats,
)


class PacketBufferTB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.inputs = [PipeIn(dut, f"lane{i}In", self.clock) for i in range(4)]
        self.frames = [PipeOut(dut, f"lane{i}Frame", self.clock) for i in range(4)]
        self.read_inputs = [PipeIn(dut, f"lane{i}ReadIn", self.clock) for i in range(4)]
        self.read_outputs = [PipeOut(dut, f"lane{i}ReadOut", self.clock) for i in range(4)]
        self.releases = [PipeIn(dut, f"lane{i}ReleaseIn", self.clock) for i in range(4)]
        self.errors = [PipeOut(dut, f"lane{i}Error", self.clock) for i in range(4)]

    async def send(self, lane: int, frame: bytes) -> None:
        for beat in wire_beats(frame):
            await pipe_put(self.inputs[lane], self.clock, beat)

    async def read_word(self, lane: int, frame: RxBufferedFrame, word: int):
        request = RxPayloadReadRequest(tag=frame.tag, wordIndex=word)
        await pipe_put(self.read_inputs[lane], self.clock, request)
        return await pipe_get(
            self.read_outputs[lane], self.clock, RxPayloadReadResponse
        )


@cocotb.test()
async def test_four_lanes_store_and_read_exact_bytes(dut):
    tb = PacketBufferTB(dut)
    await reset_dut(dut)
    payloads = [
        bytes((lane * 37 + index) & 0xFF for index in range(length))
        for lane, length in enumerate((1, 33, 95, 4138))
    ]
    send_tasks = [
        cocotb.start_soon(tb.send(lane, payloads[lane])) for lane in range(4)
    ]
    for task in send_tasks:
        await task
    descriptor_tasks = [
        cocotb.start_soon(pipe_get(tb.frames[lane], tb.clock, RxBufferedFrame))
        for lane in range(4)
    ]
    descriptors = [await task for task in descriptor_tasks]
    for lane, descriptor in enumerate(descriptors):
        assert int(descriptor.tag.lane) == lane
        assert int(descriptor.bytes) == len(payloads[lane])
        assert int(descriptor.beats) == (len(payloads[lane]) + 31) // 32
        rebuilt = bytearray()
        for word in range(int(descriptor.beats)):
            response = await tb.read_word(lane, descriptor, word)
            assert int(response.valid) == 1
            assert int(response.tag.pack()) == int(descriptor.tag.pack())
            rebuilt += int(response.low).to_bytes(32, "little")
        assert bytes(rebuilt[: len(payloads[lane])]) == payloads[lane]
        await pipe_put(
            tb.releases[lane], tb.clock, RxBufferRelease(tag=descriptor.tag)
        )
    for output in tb.errors:
        await assert_pipe_empty(output, tb.clock)


@cocotb.test()
async def test_malformed_be_resynchronizes_and_generation_changes(dut):
    tb = PacketBufferTB(dut)
    await reset_dut(dut)
    malformed = RxWireBeat(data=0xAA, be=0x5, sof=1, eof=1)
    await pipe_put(tb.inputs[0], tb.clock, malformed)
    error = await pipe_get(tb.errors[0], tb.clock, RxError)
    assert int(error.code) == 0
    await assert_pipe_empty(tb.frames[0], tb.clock)

    first_payload = bytes(range(48))
    await tb.send(0, first_payload)
    first = await pipe_get(tb.frames[0], tb.clock, RxBufferedFrame)
    await pipe_put(tb.releases[0], tb.clock, RxBufferRelease(tag=first.tag))
    for _ in range(3):
        await RisingEdge(tb.clock)
    second_payload = bytes((255 - index) & 0xFF for index in range(48))
    await tb.send(0, second_payload)
    second = await pipe_get(tb.frames[0], tb.clock, RxBufferedFrame)
    assert int(second.tag.slot) == int(first.tag.slot)
    assert int(second.tag.generation) == (int(first.tag.generation) + 1) & 0xFF
    response = await tb.read_word(0, second, 0)
    assert int(response.low).to_bytes(32, "little") == second_payload[:32]
    await pipe_put(
        tb.read_inputs[0],
        tb.clock,
        RxPayloadReadRequest(tag=first.tag, wordIndex=0),
    )
    stale = await pipe_get(tb.read_outputs[0], tb.clock, RxPayloadReadResponse)
    stale_error = await pipe_get(tb.errors[0], tb.clock, RxError)
    assert int(stale.valid) == 0
    assert int(stale_error.code) == 6


@cocotb.test()
async def test_one_full_lane_does_not_block_other_lanes(dut):
    tb = PacketBufferTB(dut)
    await reset_dut(dut)

    async def fill_lane_zero() -> list[RxBufferedFrame]:
        held = []
        for value in range(16):
            await tb.send(0, bytes([value]))
            held.append(await pipe_get(tb.frames[0], tb.clock, RxBufferedFrame))
        return held

    held = await fill_lane_zero()

    await tb.send(0, b"blocked")
    for _ in range(12):
        await RisingEdge(tb.clock)
    assert not await tb.frames[0].not_empty(), "lane produced a seventeenth owned slot"

    await tb.send(3, b"independent")
    independent = await pipe_get(tb.frames[3], tb.clock, RxBufferedFrame)
    assert int(independent.bytes) == len(b"independent")

    await pipe_put(tb.releases[0], tb.clock, RxBufferRelease(tag=held[0].tag))
    seventeenth = await pipe_get(tb.frames[0], tb.clock, RxBufferedFrame)
    assert int(seventeenth.tag.generation) == 1


@cocotb.test()
async def test_full_write_frame_after_malformed_beat(dut):
    tb = PacketBufferTB(dut)
    await reset_dut(dut)
    await pipe_put(
        tb.inputs[3], tb.clock, RxWireBeat(data=0x55, be=0x5, sof=1, eof=1)
    )
    await pipe_get(tb.errors[3], tb.clock, RxError)
    payload = b"x" * 228
    frame = build_frame(
        opcode=OP_WRITE_FIRST,
        qpn=7,
        psn=0xFFFFFE,
        payload=payload,
        remote_addr=0x2000,
        rkey=0x12345678,
        message_bytes=229,
    )
    await tb.send(3, frame)
    descriptor = await pipe_get(tb.frames[3], tb.clock, RxBufferedFrame)
    assert int(descriptor.bytes) == len(frame)
    assert int(descriptor.tag.generation) == 1
    rebuilt = bytearray()
    for word in range(int(descriptor.beats)):
        response = await tb.read_word(3, descriptor, word)
        rebuilt += int(response.low).to_bytes(32, "little")
    assert bytes(rebuilt[: len(frame)]) == frame


@cocotb.test()
async def test_payload_read_progresses_when_full_lane_has_waiting_ingress(dut):
    tb = PacketBufferTB(dut)
    await reset_dut(dut)
    held = []
    for value in range(16):
        await tb.send(0, bytes([value]))
        held.append(await pipe_get(tb.frames[0], tb.clock, RxBufferedFrame))

    await pipe_put(
        tb.read_inputs[0],
        tb.clock,
        RxPayloadReadRequest(tag=held[0].tag, wordIndex=0),
    )
    await tb.send(0, b"waiting")
    response = await pipe_get(tb.read_outputs[0], tb.clock, RxPayloadReadResponse)
    assert int(response.valid) == 1
    assert int(response.low) & 0xFF == 0

    await pipe_put(tb.releases[0], tb.clock, RxBufferRelease(tag=held[0].tag))
    waiting = await pipe_get(tb.frames[0], tb.clock, RxBufferedFrame)
    assert int(waiting.bytes) == len(b"waiting")


@cocotb.test()
async def test_out_of_range_read_returns_invalid_without_memory_access(dut):
    tb = PacketBufferTB(dut)
    await reset_dut(dut)
    await tb.send(0, b"owned")
    descriptor = await pipe_get(tb.frames[0], tb.clock, RxBufferedFrame)
    response = await tb.read_word(0, descriptor, 255)
    error = await pipe_get(tb.errors[0], tb.clock, RxError)
    assert int(response.valid) == 0
    assert int(response.low) == int(response.high) == 0
    assert int(error.code) == 6
    assert int(error.qpn) == 0
    await tb.read_word(0, descriptor, 0)
