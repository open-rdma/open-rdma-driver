from __future__ import annotations

import os
import random

import cocotb
from cocotb.triggers import RisingEdge, with_timeout

from cocotb_bsv import ActionMethod, PipeIn, PipeOut, ValueMethod
from bfm.common import assert_pipe_empty, pipe_get, pipe_put, reset_dut
from model.rx_types import (
    RxAdmittedPacket,
    RxBufferRelease,
    RxError,
    RxFeedback,
    RxParsedPacket,
    RxQpConfig,
    RxReceiveProgress,
    tag,
)
from model.wire import OP_WRITE_FIRST, OP_WRITE_LAST, OP_WRITE_MIDDLE, OP_WRITE_ONLY


RKEY = 0x12345678


def qp_config(qpn: int, initial_psn: int, pmtu: int = 0) -> RxQpConfig:
    return RxQpConfig(
        valid=1,
        qpn=qpn,
        initialPsn=initial_psn,
        pmtu=pmtu,
        rkey=RKEY,
        virtualBase=0x1000,
        physicalBase=0x8000,
        regionBytes=0x20000,
        remoteWrite=1,
    )


def parsed(
    lane: int,
    psn: int,
    opcode: int,
    payload_bytes: int,
    *,
    qpn: int = 7,
    remote_addr: int = 0,
    rkey: int = 0,
    message_bytes: int = 0,
    generation: int = 0,
) -> RxParsedPacket:
    first = opcode in (OP_WRITE_FIRST, OP_WRITE_ONLY)
    last = opcode in (OP_WRITE_LAST, OP_WRITE_ONLY)
    return RxParsedPacket(
        payloadTag=tag(lane, psn & 0xF, generation),
        lane=lane,
        qpn=qpn,
        psn=psn & 0xFFFFFF,
        opcode=opcode,
        ecn=0,
        payloadStart=70 if first else 54,
        payloadBytes=payload_bytes,
        remoteAddr=remote_addr if first else 0,
        rkey=rkey if first else 0,
        messageBytes=message_bytes if first else 0,
        first=int(first),
        last=int(last),
    )


class RqTB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.config = [ActionMethod(dut, f"configure{i}", self.clock) for i in range(4)]
        self.start_method = ActionMethod(dut, "start", self.clock)
        self.progress_methods = [ValueMethod(dut, f"progress{i}", self.clock) for i in range(4)]
        self.next_seq = ValueMethod(dut, "nextGlobalSeq", self.clock)
        self.fault = ValueMethod(dut, "faulted", self.clock)
        self.inputs = [PipeIn(dut, f"lane{i}PacketIn", self.clock) for i in range(4)]
        self.admitted = [PipeOut(dut, f"lane{i}AdmittedOut", self.clock) for i in range(4)]
        self.releases = [PipeOut(dut, f"lane{i}ReleaseOut", self.clock) for i in range(4)]
        self.feedback = PipeOut(dut, "feedbackOut", self.clock)
        self.error = PipeOut(dut, "errorOut", self.clock)

    async def initialize(self, configs: list[RxQpConfig]) -> None:
        for index, cfg in enumerate(configs):
            await self.config[index](cfg=cfg.pack())
        await self.start_method()

    async def progress(self, qp: int) -> RxReceiveProgress:
        return RxReceiveProgress.unpack(await self.progress_methods[qp]())

    async def collect_admitted(self, count: int) -> list[RxAdmittedPacket]:
        result: list[RxAdmittedPacket] = []

        async def collect() -> None:
            while len(result) < count:
                found = False
                for lane in range(4):
                    if await self.admitted[lane].not_empty():
                        raw = await self.admitted[lane].first()
                        await self.admitted[lane].deq()
                        result.append(RxAdmittedPacket.unpack(raw))
                        found = True
                if not found:
                    await RisingEdge(self.clock)

        await with_timeout(collect(), 20_000, "ns")
        return result


@cocotb.test()
async def test_first_arrives_last_receive_ack_precedes_dma(dut):
    tb = RqTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 0xFFFFFE)])
    packets = [
        parsed(2, 0, OP_WRITE_LAST, 28, qpn=7),
        parsed(1, 0xFFFFFF, OP_WRITE_MIDDLE, 244, qpn=7),
    ]
    for packet in packets:
        await pipe_put(tb.inputs[int(packet.lane)], tb.clock, packet)
    for _ in range(24):
        await RisingEdge(tb.clock)
    assert not await tb.feedback.not_empty(), "continuation was ACKed before FIRST"
    for output in tb.admitted:
        assert not await output.not_empty()

    first = parsed(
        0,
        0xFFFFFE,
        OP_WRITE_FIRST,
        228,
        qpn=7,
        remote_addr=0x2000,
        rkey=RKEY,
        message_bytes=500,
    )
    await pipe_put(tb.inputs[0], tb.clock, first)
    admitted = await tb.collect_admitted(3)
    admitted.sort(key=lambda packet: int(packet.globalSeq))
    assert [int(packet.globalSeq) for packet in admitted] == [0, 1, 2]
    assert {int(packet.psn) for packet in admitted} == {0xFFFFFE, 0xFFFFFF, 0}
    assert {int(packet.dmaAddr) for packet in admitted} == {
        0x9000,
        0x9000 + 228,
        0x9000 + 228 + 244,
    }
    feedback = [await pipe_get(tb.feedback, tb.clock, RxFeedback) for _ in range(3)]
    assert int(feedback[-1].ackPsn) == 0
    progress = await tb.progress(0)
    assert int(progress.ackPsn) == 0
    assert int(progress.receiveBitmap) == 0
    assert int(progress.oooCount) == 0
    assert int(await tb.next_seq()) == 3
    assert int(await tb.fault()) == 0


@cocotb.test()
async def test_duplicate_window_edge_and_right_edge(dut):
    tb = RqTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(11, 200)])
    message_bytes = 228 + 16 * 244
    first = parsed(
        0,
        200,
        OP_WRITE_FIRST,
        228,
        qpn=11,
        remote_addr=0x3000,
        rkey=RKEY,
        message_bytes=message_bytes,
    )
    await pipe_put(tb.inputs[0], tb.clock, first)
    await tb.collect_admitted(1)
    await pipe_get(tb.feedback, tb.clock, RxFeedback)

    duplicate = parsed(
        1,
        200,
        OP_WRITE_FIRST,
        228,
        qpn=11,
        remote_addr=0x3000,
        rkey=RKEY,
        message_bytes=message_bytes,
        generation=1,
    )
    await pipe_put(tb.inputs[1], tb.clock, duplicate)
    release = await pipe_get(tb.releases[1], tb.clock, RxBufferRelease)
    assert int(release.tag.generation) == 1
    await pipe_get(tb.feedback, tb.clock, RxFeedback)
    assert int(await tb.next_seq()) == 1

    edge = parsed(2, 216, OP_WRITE_LAST, 244, qpn=11)
    await pipe_put(tb.inputs[2], tb.clock, edge)
    edge_admitted = await tb.collect_admitted(1)
    assert int(edge_admitted[0].globalSeq) == 1
    sack = await pipe_get(tb.feedback, tb.clock, RxFeedback)
    assert int(sack.kind) == 1
    assert int(sack.sackBitmap) == 1 << 15
    assert int(sack.oooCount) == 1

    outside = parsed(3, 217, OP_WRITE_MIDDLE, 244, qpn=11)
    await pipe_put(tb.inputs[3], tb.clock, outside)
    nak = await pipe_get(tb.feedback, tb.clock, RxFeedback)
    error = await pipe_get(tb.error, tb.clock, RxError)
    await pipe_get(tb.releases[3], tb.clock, RxBufferRelease)
    assert int(nak.kind) == 2
    assert int(error.code) == 3
    assert int(await tb.next_seq()) == 2


@cocotb.test()
async def test_admitted_backpressure_keeps_bitmap_commit_atomic(dut):
    tb = RqTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(5, 10)])
    for index in range(5):
        packet = parsed(
            index & 3,
            10 + index,
            OP_WRITE_ONLY,
            1,
            qpn=5,
            remote_addr=0x4000 + index,
            rkey=RKEY,
            message_bytes=1,
        )
        await pipe_put(tb.inputs[index & 3], tb.clock, packet)
    for _ in range(40):
        await RisingEdge(tb.clock)
    # Four lane queues can hold four each, so all five should commit; this also
    # proves one stalled lane cannot suppress commits routed to other lanes.
    assert int(await tb.next_seq()) == 5
    progress = await tb.progress(0)
    assert int(progress.ackPsn) == 14
    admitted = await tb.collect_admitted(5)
    assert sorted(int(packet.globalSeq) for packet in admitted) == list(range(5))


@cocotb.test()
async def test_bad_rkey_does_not_commit_and_enters_fail_stop(dut):
    tb = RqTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 50)])
    bad = parsed(
        0,
        50,
        OP_WRITE_ONLY,
        4,
        qpn=7,
        remote_addr=0x2000,
        rkey=0xDEADBEEF,
        message_bytes=4,
    )
    await pipe_put(tb.inputs[0], tb.clock, bad)
    error = await pipe_get(tb.error, tb.clock, RxError)
    await pipe_get(tb.releases[0], tb.clock, RxBufferRelease)
    assert int(error.code) == 5
    assert int(await tb.fault()) == 1
    assert not await tb.inputs[0].not_full()
    assert int(await tb.next_seq()) == 0
    progress = await tb.progress(0)
    assert int(progress.ackPsn) == 49
    await assert_pipe_empty(tb.feedback, tb.clock)
    for output in tb.admitted:
        await assert_pipe_empty(output, tb.clock)


@cocotb.test()
async def test_first_replaces_same_psn_pending_continuation(dut):
    tb = RqTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100)])
    poison = parsed(1, 100, OP_WRITE_MIDDLE, 244, qpn=7)
    await pipe_put(tb.inputs[1], tb.clock, poison)
    for _ in range(12):
        await RisingEdge(tb.clock)
    first = parsed(
        0,
        100,
        OP_WRITE_FIRST,
        228,
        qpn=7,
        remote_addr=0x2000,
        rkey=RKEY,
        message_bytes=500,
        generation=1,
    )
    await pipe_put(tb.inputs[0], tb.clock, first)
    released = await pipe_get(tb.releases[1], tb.clock, RxBufferRelease)
    assert int(released.tag.pack()) == int(poison.payloadTag.pack())
    admitted = await tb.collect_admitted(1)
    assert int(admitted[0].psn) == 100
    assert int(admitted[0].first) == 1
    assert int(await tb.next_seq()) == 1


@cocotb.test()
async def test_missing_first_pending_times_out_and_releases_slot(dut):
    tb = RqTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100)])
    orphan = parsed(2, 100, OP_WRITE_MIDDLE, 244, qpn=7)
    await pipe_put(tb.inputs[2], tb.clock, orphan)
    error = await pipe_get(tb.error, tb.clock, RxError)
    feedback = await pipe_get(tb.feedback, tb.clock, RxFeedback)
    released = await pipe_get(tb.releases[2], tb.clock, RxBufferRelease)
    assert int(error.code) == 17
    assert int(feedback.kind) == 2
    assert int(released.tag.pack()) == int(orphan.payloadTag.pack())
    assert int(await tb.next_seq()) == 0

    retry = parsed(
        0,
        100,
        OP_WRITE_ONLY,
        4,
        qpn=7,
        remote_addr=0x2000,
        rkey=RKEY,
        message_bytes=4,
        generation=1,
    )
    await pipe_put(tb.inputs[0], tb.clock, retry)
    admitted = await tb.collect_admitted(1)
    assert int(admitted[0].globalSeq) == 0


@cocotb.test()
async def test_correct_first_replaces_bad_pending_first(dut):
    tb = RqTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100)])
    current_first = parsed(
        0, 100, OP_WRITE_FIRST, 228, qpn=7,
        remote_addr=0x2000, rkey=RKEY, message_bytes=500,
    )
    await pipe_put(tb.inputs[0], tb.clock, current_first)
    await tb.collect_admitted(1)
    await pipe_get(tb.feedback, tb.clock, RxFeedback)

    bad_next = parsed(
        1, 103, OP_WRITE_FIRST, 228, qpn=7,
        remote_addr=0x3000, rkey=0xDEADBEEF, message_bytes=500,
    )
    await pipe_put(tb.inputs[1], tb.clock, bad_next)
    for _ in range(12):
        await RisingEdge(tb.clock)
    good_next = parsed(
        2, 103, OP_WRITE_FIRST, 228, qpn=7,
        remote_addr=0x3000, rkey=RKEY, message_bytes=500, generation=1,
    )
    await pipe_put(tb.inputs[2], tb.clock, good_next)
    released = await pipe_get(tb.releases[1], tb.clock, RxBufferRelease)
    assert int(released.tag.pack()) == int(bad_next.payloadTag.pack())

    await pipe_put(tb.inputs[3], tb.clock, parsed(3, 101, OP_WRITE_MIDDLE, 244, qpn=7))
    await pipe_put(tb.inputs[0], tb.clock, parsed(0, 102, OP_WRITE_LAST, 28, qpn=7))
    admitted = await tb.collect_admitted(3)
    assert {int(packet.psn) for packet in admitted} == {101, 102, 103}
    next_first = next(packet for packet in admitted if int(packet.psn) == 103)
    assert int(next_first.first) == 1
    assert int(next_first.globalSeq) == 3
    assert int(await tb.fault()) == 0


@cocotb.test()
async def test_whole_message_physical_overflow_fails_before_commit(dut):
    tb = RqTB(dut)
    await reset_dut(dut)
    cfg = RxQpConfig(
        valid=1,
        qpn=7,
        initialPsn=50,
        pmtu=0,
        rkey=RKEY,
        virtualBase=0x1000,
        physicalBase=(1 << 64) - 200,
        regionBytes=0x10000,
        remoteWrite=1,
    )
    await tb.initialize([cfg])
    first = parsed(
        0,
        50,
        OP_WRITE_FIRST,
        228,
        qpn=7,
        remote_addr=0x1000,
        rkey=RKEY,
        message_bytes=500,
    )
    await pipe_put(tb.inputs[0], tb.clock, first)
    error = await pipe_get(tb.error, tb.clock, RxError)
    await pipe_get(tb.releases[0], tb.clock, RxBufferRelease)
    assert int(error.code) == 5
    assert int(await tb.next_seq()) == 0
    progress = await tb.progress(0)
    assert int(progress.ackPsn) == 49


@cocotb.test(skip=os.getenv("RX_MVP_SUITE") != "long")
async def test_long_fixed_seed_reorder_and_lane_mix(dut):
    seed = 0x52444D41
    rng = random.Random(seed)
    dut._log.info("RX RQ long-test seed=%#x", seed)
    tb = RqTB(dut)
    await reset_dut(dut)
    initial_psn = 1000
    await tb.initialize([qp_config(7, initial_psn)])
    expected_seq = 0
    for message_index in range(32):
        base = initial_psn + message_index * 3
        address = 0x2000 + message_index * 0x200
        packets = [
            parsed(
                rng.randrange(4),
                base,
                OP_WRITE_FIRST,
                228,
                qpn=7,
                remote_addr=address,
                rkey=RKEY,
                message_bytes=500,
                generation=message_index,
            ),
            parsed(
                rng.randrange(4),
                base + 1,
                OP_WRITE_MIDDLE,
                244,
                qpn=7,
                generation=message_index,
            ),
            parsed(
                rng.randrange(4),
                base + 2,
                OP_WRITE_LAST,
                28,
                qpn=7,
                generation=message_index,
            ),
        ]
        rng.shuffle(packets)
        for packet in packets:
            await pipe_put(tb.inputs[int(packet.lane)], tb.clock, packet)
        admitted = await tb.collect_admitted(3)
        seqs = sorted(int(packet.globalSeq) for packet in admitted)
        assert seqs == list(range(expected_seq, expected_seq + 3))
        expected_seq += 3
        feedback = [await pipe_get(tb.feedback, tb.clock, RxFeedback) for _ in range(3)]
        assert int(feedback[-1].ackPsn) == (base + 2) & 0xFFFFFF
    assert int(await tb.next_seq()) == expected_seq
    progress = await tb.progress(0)
    assert int(progress.ackPsn) == (initial_psn + 32 * 3 - 1) & 0xFFFFFF
    assert int(progress.receiveBitmap) == 0
    assert int(await tb.fault()) == 0
