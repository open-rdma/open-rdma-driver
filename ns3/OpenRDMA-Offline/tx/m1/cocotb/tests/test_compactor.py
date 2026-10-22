import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, with_timeout
from cocotb_bsv import PipeIn, PipeOut

from bfm.m2 import pipe_get
from dma_phasea_model import DmaPayloadBeat, raw_payload_beats
from model.m2 import MessagePayloadBeat, message_beats


class TB:
    def __init__(self, dut):
        self.dut = dut
        self.clock = dut.CLK
        self.resetn = dut.RST_N
        self.raw = PipeIn(dut, "dmaPayloadPipeIn", self.clock)
        self.output = PipeOut(dut, "messagePayloadPipeOut", self.clock)

    async def reset(self):
        cocotb.start_soon(Clock(self.clock, 2, unit="ns").start())
        self.resetn.value = 0
        for _ in range(4):
            await RisingEdge(self.clock)
        self.resetn.value = 1
        for _ in range(4):
            await RisingEdge(self.clock)

    async def put(self, beat: DmaPayloadBeat):
        await self.raw.enq(beat.pack())

    async def get(self) -> MessagePayloadBeat:
        return MessagePayloadBeat.unpack(await pipe_get(self.output, self.clock))


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def compacts_nonprefix_fragments_and_flushes_exact_eop(dut):
    tb = TB(dut)
    await tb.reset()

    cases = (
        (1, bytes([0xA5]), [(127, 1)]),
        (2, bytes(range(128)), [(1, 127), (0, 1)]),
        (3, bytes((index * 7) & 0xFF for index in range(200)), [(63, 65), (0, 100), (10, 35)]),
        (4, bytes((index * 11) & 0xFF for index in range(256)), [(0, 128), (0, 128)]),
        (5, bytes((index * 13) & 0xFF for index in range(300)), [(28, 100), (1, 127), (55, 73)]),
    )
    raw = [
        beat
        for request_id, payload, fragments in cases
        for beat in raw_payload_beats(request_id, payload, fragments)
    ]
    expected = [
        beat
        for request_id, payload, _ in cases
        for beat in message_beats(payload, request_id)
    ]

    async def produce():
        for beat in raw:
            await tb.put(beat)

    async def consume():
        actual = []
        for index, expected_beat in enumerate(expected):
            for _ in range(index % 3):
                await RisingEdge(tb.clock)
            beat = await tb.get()
            assert beat.pack() == expected_beat.pack(), f"compacted beat {index} mismatch"
            actual.append(beat)
        return actual

    producer = cocotb.start_soon(produce())
    consumer = cocotb.start_soon(consume())
    await with_timeout(producer, 100_000, "ns")
    actual = await with_timeout(consumer, 100_000, "ns")
    assert sum(int(beat.messageEop) for beat in actual) == len(cases)
    for _ in range(8):
        await RisingEdge(tb.clock)
        assert not await tb.output.not_empty()


@cocotb.test(timeout_time=100_000, timeout_unit="ns")
async def sustains_full_width_stream_without_structural_bubbles(dut):
    tb = TB(dut)
    await tb.reset()

    payload = bytes((index * 17) & 0xFF for index in range(16 * 128))
    raw = raw_payload_beats(0x55, payload, [(0, 128)] * 16)
    expected = message_beats(payload, 0x55)
    completed = 0

    async def produce():
        for beat in raw:
            await tb.put(beat)

    async def consume():
        nonlocal completed
        for expected_beat in expected:
            beat = await tb.get()
            assert beat.pack() == expected_beat.pack()
            completed += 1

    producer = cocotb.start_soon(produce())
    consumer = cocotb.start_soon(consume())
    for _ in range(40):
        if completed == len(expected):
            break
        await RisingEdge(tb.clock)
    await producer
    await consumer
    assert completed == 16
