"""Cocotb BFM for the channel-neutral M2 packetizer."""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, with_timeout

from cocotb_bsv import ActionMethod, ActionValueMethod, PipeIn, PipeOut, ValueMethod
from model.m2 import (
    MessageContext,
    Packet,
    PacketDescriptor,
    PacketPayloadBeat,
    WIRE_LEGACY_PRIVATE,
    WIRE_MRC_IPV4_ECMP_NO_ICRC,
    bytes_from_word,
    message_beats,
    packet_lengths,
    packetize,
)


class TB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.resetn = dut.RST_N
        self.context = PipeIn(dut, "contextPipeIn", self.clock)
        self.message_payload = PipeIn(dut, "messagePayloadPipeIn", self.clock)
        self.descriptor = PipeOut(dut, "descriptorPipeOut", self.clock)
        self.packet_payload = PipeOut(dut, "packetPayloadPipeOut", self.clock)
        self.aligner_payload = PipeIn(dut, "alignerPayloadPipeIn", self.clock)
        self.aligner_request = ActionValueMethod(dut, "alignerRequest", self.clock)
        self.aligner_flush = ActionMethod(dut, "alignerFlush", self.clock)
        self.aligner_residual = ValueMethod(dut, "alignerResidualBytes", self.clock)
        self.aligner_supply = ValueMethod(dut, "alignerSupplyBytes", self.clock)

    async def reset(self) -> None:
        cocotb.start_soon(Clock(self.clock, 2, unit="ns").start())
        self.resetn.value = 0
        for _ in range(4):
            await RisingEdge(self.clock)
        self.resetn.value = 1
        for _ in range(4):
            await RisingEdge(self.clock)


async def pipe_get(pipe: PipeOut, clock, cycles: int = 20_000) -> int:
    async def get_one() -> int:
        # ValueMethod/ActionMethod already wait for RDY; a separate notEmpty
        # poll duplicates one GPI round trip per item.
        value = await pipe.first()
        await pipe.deq()
        return value

    return await with_timeout(get_one(), cycles * 2, "ns")


async def drive_message(tb: TB, ctx: MessageContext, payload: bytes, stride: int = 0) -> None:
    await tb.context.enq(ctx.pack())
    for beat in message_beats(payload, int(ctx.requestId)):
        await tb.message_payload.enq(beat.pack())
        for _ in range(stride):
            await RisingEdge(tb.clock)


async def collect_packets(tb: TB, count: int, stall: int = 0) -> list[Packet]:
    packets = []
    for _ in range(count):
        descriptor = PacketDescriptor.unpack(await pipe_get(tb.descriptor, tb.clock))
        payload = bytearray()
        first = True
        while True:
            beat = PacketPayloadBeat.unpack(
                await pipe_get(tb.packet_payload, tb.clock)
            )
            assert int(beat.requestId) == int(descriptor.requestId)
            assert int(beat.psn) == int(descriptor.psn)
            assert int(beat.sop) == int(first)
            payload += bytes_from_word(int(beat.data), int(beat.keep))
            first = False
            if int(beat.eop):
                break
            for _ in range(stall):
                await RisingEdge(tb.clock)
        packets.append(Packet(descriptor, bytes(payload)))
    return packets


async def run_message(tb: TB, ctx: MessageContext, payload: bytes, stall: int = 0):
    expected = packetize(ctx, payload)
    producer = cocotb.start_soon(drive_message(tb, ctx, payload))
    consumer = cocotb.start_soon(collect_packets(tb, len(expected), stall=stall))
    await producer
    actual = await consumer
    return expected, actual


def assert_packets_equal(expected: list[Packet], actual: list[Packet]) -> None:
    assert len(actual) == len(expected)
    for exp, got in zip(expected, actual, strict=True):
        assert got.descriptor.pack() == exp.descriptor.pack()
        assert got.payload == exp.payload


def make_message_context(
    *,
    length: int,
    request_id: int = 0,
    qpn: int = 1,
    dest_qpn: int = 7,
    slot_id: int = 0,
    debug_seq: int = 0,
    pmtu: int = 0,
    start_psn: int = 0,
    remote_addr: int = 0x2000_0000_0000,
    remote_key: int = 0x5566_7788,
    wire_profile: int = WIRE_LEGACY_PRIVATE,
    ev_profile_id: int = 0,
    mrc_msn: int = 0,
    mrc_msn_valid: int | None = None,
) -> MessageContext:
    count = len(packet_lengths(length, pmtu, wire_profile))
    if mrc_msn_valid is None:
        mrc_msn_valid = int(wire_profile == WIRE_MRC_IPV4_ECMP_NO_ICRC)
    return MessageContext(
        requestId=request_id,
        qpn=qpn,
        destQpn=dest_qpn,
        slotId=slot_id,
        debugSeq=debug_seq,
        pmtu=pmtu,
        startPsn=start_psn,
        psnCount=count,
        remoteAddr=remote_addr,
        remoteKey=remote_key,
        len=length,
        wireProfile=wire_profile,
        evProfileId=ev_profile_id,
        mrcMsn=mrc_msn,
        mrcMsnValid=mrc_msn_valid,
    )


def payload_bytes(length: int, seed: int = 0x5A5A) -> bytes:
    return random.Random(seed).randbytes(length)
