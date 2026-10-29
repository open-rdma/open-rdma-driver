from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge

from cocotb_bsv import ActionMethod, PipeIn, PipeOut
from bfm.common import assert_pipe_empty, pipe_get, pipe_put, reset_dut
from model.rx_types import (
    RxBufferRelease,
    RxBufferedFrame,
    RxEcnEvent,
    RxError,
    RxNetworkConfig,
    RxParsedPacket,
    RxTxFeedback,
    tag,
)
from model.wire import (
    NetworkConfig,
    OP_WRITE_FIRST,
    OP_WRITE_LAST,
    OP_WRITE_MIDDLE,
    OP_WRITE_ONLY,
    buffered_frame,
    build_ack_frame,
    build_frame,
)


CFG = NetworkConfig()


class ParserTB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.configure_method = ActionMethod(dut, "configure", self.clock)
        self.inputs = [PipeIn(dut, f"lane{i}FrameIn", self.clock) for i in range(4)]
        self.packets = [PipeOut(dut, f"lane{i}PacketOut", self.clock) for i in range(4)]
        self.releases = [PipeOut(dut, f"lane{i}ReleaseOut", self.clock) for i in range(4)]
        self.feedback = PipeOut(dut, "feedbackOut", self.clock)
        self.ecn = PipeOut(dut, "ecnOut", self.clock)
        self.error = PipeOut(dut, "errorOut", self.clock)

    async def configure(self) -> None:
        value = RxNetworkConfig(
            localMac=CFG.local_mac,
            localIp=CFG.local_ip,
            peerMac=CFG.peer_mac,
            peerIp=CFG.peer_ip,
        )
        await self.configure_method(cfg=value.pack())


@cocotb.test()
async def test_all_write_opcodes_decode_on_independent_lanes(dut):
    tb = ParserTB(dut)
    await reset_dut(dut)
    await tb.configure()
    cases = [
        (OP_WRITE_FIRST, 0, 100, b"first", 0x2000, 0x12345678, 500, 1, 0, 70),
        (OP_WRITE_MIDDLE, 1, 101, b"middle", 0, 0, 0, 0, 0, 54),
        (OP_WRITE_LAST, 2, 102, b"last", 0, 0, 0, 0, 1, 54),
        (OP_WRITE_ONLY, 3, 200, b"only", 0x2800, 0x12345678, 4, 1, 1, 70),
    ]
    for opcode, lane, psn, payload, addr, rkey, message_bytes, *_ in cases:
        frame = build_frame(
            opcode=opcode,
            qpn=7,
            psn=psn,
            payload=payload,
            remote_addr=addr,
            rkey=rkey,
            message_bytes=message_bytes,
        )
        if opcode == OP_WRITE_LAST:
            frame = frame.ljust(60, b"\x00")
        await pipe_put(
            tb.inputs[lane], tb.clock, buffered_frame(frame, tag(lane, lane, 9))
        )

    for opcode, lane, psn, payload, addr, rkey, message_bytes, first, last, start in cases:
        packet = await pipe_get(tb.packets[lane], tb.clock, RxParsedPacket)
        assert int(packet.lane) == lane
        assert int(packet.qpn) == 7
        assert int(packet.psn) == psn
        assert int(packet.opcode) == opcode
        assert int(packet.payloadStart) == start
        assert int(packet.payloadBytes) == len(payload)
        assert int(packet.remoteAddr) == addr
        assert int(packet.rkey) == rkey
        assert int(packet.messageBytes) == message_bytes
        assert int(packet.first) == first
        assert int(packet.last) == last
    await assert_pipe_empty(tb.error, tb.clock)
    for release in tb.releases:
        await assert_pipe_empty(release, tb.clock)


@cocotb.test()
async def test_ecn_ce_is_forwarded_without_dropping_data_or_ack(dut):
    tb = ParserTB(dut)
    await reset_dut(dut)
    await tb.configure()
    frame = build_frame(
        opcode=OP_WRITE_ONLY,
        qpn=9,
        psn=0xFFFFFE,
        payload=b"ecn-data",
        remote_addr=0x3000,
        rkey=0xAABBCCDD,
        message_bytes=8,
        ecn=3,
    )
    await pipe_put(tb.inputs[3], tb.clock, buffered_frame(frame, tag(3, 1)))
    packet = await pipe_get(tb.packets[3], tb.clock, RxParsedPacket)
    event = await pipe_get(tb.ecn, tb.clock, RxEcnEvent)
    assert int(packet.ecn) == 3
    assert (int(event.lane), int(event.qpn), int(event.psn), int(event.ecn)) == (
        3,
        9,
        0xFFFFFE,
        3,
    )

    ack = build_ack_frame(qpn=9, psn=5, aeth=0x11223344, ecn=3).ljust(
        60, b"\x00"
    )
    await pipe_put(tb.inputs[1], tb.clock, buffered_frame(ack, tag(1, 4, 2)))
    feedback = await pipe_get(tb.feedback, tb.clock, RxTxFeedback)
    ack_ecn = await pipe_get(tb.ecn, tb.clock, RxEcnEvent)
    release = await pipe_get(tb.releases[1], tb.clock, RxBufferRelease)
    assert (int(feedback.qpn), int(feedback.psn), int(feedback.aeth)) == (
        9,
        5,
        0x11223344,
    )
    assert int(ack_ecn.lane) == 1
    assert int(release.tag.slot) == 4


@cocotb.test()
async def test_invalid_header_releases_slot_and_reports_exact_error(dut):
    tb = ParserTB(dut)
    await reset_dut(dut)
    await tb.configure()
    good = bytearray(
        build_frame(
            opcode=OP_WRITE_ONLY,
            qpn=7,
            psn=1,
            payload=b"payload",
            remote_addr=0x2000,
            rkey=0x12345678,
            message_bytes=7,
        )
    )
    good[24] ^= 0x01
    descriptor = buffered_frame(bytes(good), tag(2, 6, 3))
    await pipe_put(tb.inputs[2], tb.clock, descriptor)
    error = await pipe_get(tb.error, tb.clock, RxError)
    release = await pipe_get(tb.releases[2], tb.clock, RxBufferRelease)
    assert int(error.code) == 1
    assert int(error.qpn) == 7
    assert int(release.tag.pack()) == int(descriptor.tag.pack())
    await assert_pipe_empty(tb.packets[2], tb.clock)

    # An ECT-marked packet is not CE and must not create a congestion event.
    ect = build_frame(
        opcode=OP_WRITE_ONLY,
        qpn=7,
        psn=2,
        payload=b"x",
        remote_addr=0x2100,
        rkey=0x12345678,
        message_bytes=1,
        ecn=1,
    )
    await pipe_put(tb.inputs[0], tb.clock, buffered_frame(ect, tag(0, 1)))
    await pipe_get(tb.packets[0], tb.clock, RxParsedPacket)
    for _ in range(8):
        await RisingEdge(tb.clock)
    assert not await tb.ecn.not_empty()

    oversized = build_frame(
        opcode=OP_WRITE_FIRST,
        qpn=7,
        psn=3,
        payload=b"x",
        remote_addr=0x2100,
        rkey=0x12345678,
        message_bytes=0x01000001,
    )
    await pipe_put(tb.inputs[0], tb.clock, buffered_frame(oversized, tag(0, 2)))
    oversized_error = await pipe_get(tb.error, tb.clock, RxError)
    await pipe_get(tb.releases[0], tb.clock, RxBufferRelease)
    assert int(oversized_error.code) == 1
