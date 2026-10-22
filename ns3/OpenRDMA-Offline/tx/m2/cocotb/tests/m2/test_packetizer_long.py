"""Randomized M2 regression over both wire profiles.

`make test-long` used to re-run the short module, so it proved nothing extra.
This suite is the real long run: a fixed seed sweeps message lengths, PMTUs,
wire profiles, input pacing and output backpressure, and compares every
descriptor and payload beat against the reference model.
"""

import random

import cocotb

from bfm.m2 import (
    TB,
    assert_packets_equal,
    collect_packets,
    drive_message,
    make_message_context,
    payload_bytes,
)
from model.m2 import (
    OP_MRC_WRITE_FIRST,
    OP_MRC_WRITE_LAST,
    OP_MRC_WRITE_MIDDLE,
    OP_MRC_WRITE_ONLY,
    PMTU_BYTES,
    WIRE_LEGACY_PRIVATE,
    WIRE_MRC_IPV4_ECMP_NO_ICRC,
    packet_lengths,
    packetize,
)

def check_placement(packets, profile, length, msn):
    count = len(packets)
    for index, packet in enumerate(packets):
        descriptor = packet.descriptor
        assert int(descriptor.packetCount) == count
        assert int(descriptor.packetIndex) == index
        assert int(descriptor.wireProfile) == profile
        if index == 0:
            assert int(descriptor.remoteAddr) == 0x2000_0000_0000
        else:
            assert int(descriptor.messageOffset) == sum(
                int(p.descriptor.payloadLength) for p in packets[:index]
            )
            assert int(descriptor.remoteAddr) == 0x2000_0000_0000 + int(
                descriptor.messageOffset
            )
        assert int(descriptor.messageLength) == length
        if profile == WIRE_LEGACY_PRIVATE:
            continue
        assert int(descriptor.mrcMsnValid) == 1
        assert int(descriptor.mrcMsn) == msn
        if count == 1:
            assert int(descriptor.opcode) == OP_MRC_WRITE_ONLY
        elif index == 0:
            assert int(descriptor.opcode) == OP_MRC_WRITE_FIRST
        elif index == count - 1:
            assert int(descriptor.opcode) == OP_MRC_WRITE_LAST
        else:
            assert int(descriptor.opcode) == OP_MRC_WRITE_MIDDLE


def check_legacy_limits(packets, pmtu_code):
    limit = PMTU_BYTES[pmtu_code]
    for index, packet in enumerate(packets):
        length = int(packet.descriptor.payloadLength)
        if index == 0:
            assert length <= limit - 28
        else:
            assert length <= limit - 12
        assert length > 0


def check_mrc_limits(packets, pmtu_code):
    limit = PMTU_BYTES[pmtu_code]
    count = len(packets)
    for index, packet in enumerate(packets):
        length = int(packet.descriptor.payloadLength)
        assert 0 < length <= limit
        if index != count - 1:
            assert length == limit


@cocotb.test(timeout_time=2_000_000, timeout_unit="ns")
async def randomized_long_legacy_and_mrc_sweep(dut):
    tb = TB(dut)
    await tb.reset()
    rng = random.Random(0x5EED_1234)

    cases = []
    for index in range(60):
        pmtu = rng.randrange(len(PMTU_BYTES))
        profile = rng.choice(
            [WIRE_LEGACY_PRIVATE, WIRE_MRC_IPV4_ECMP_NO_ICRC]
        )
        # Bias towards small messages and PMTU boundaries so the sweep keeps
        # hitting the single-packet, first/continuation and last-packet cases.
        if index % 3 == 0:
            length = rng.choice([1, 2, 3, 255, 256, 257, 4095, 4096, 4097])
        else:
            length = rng.randrange(1, 9000)
        ctx = make_message_context(
            length=length,
            pmtu=pmtu,
            request_id=index,
            start_psn=rng.choice([0, 0xFFFFF0, rng.randrange(1 << 24)]),
            wire_profile=profile,
            ev_profile_id=rng.randrange(4),
            mrc_msn=rng.randrange(1 << 16),
        )
        cases.append((ctx, payload_bytes(length, seed=index)))

    expected = [
        packet for ctx, payload in cases for packet in packetize(ctx, payload)
    ]

    async def produce():
        for ctx, payload in cases:
            stride = rng.randrange(2) if int(ctx.requestId) % 4 == 0 else 0
            await drive_message(tb, ctx, payload, stride=stride)

    producer = cocotb.start_soon(produce())
    actual = await collect_packets(tb, len(expected), stall=1)
    await producer
    assert_packets_equal(expected, actual)

    cursor = 0
    for ctx, payload in cases:
        packets = packetize(ctx, payload)
        chunk = actual[cursor : cursor + len(packets)]
        cursor += len(packets)
        length = len(payload)
        pmtu_code = int(ctx.pmtu)
        check_placement(
            chunk, int(ctx.wireProfile), length, int(ctx.mrcMsn)
        )
        assert [len(p.payload) for p in chunk] == packet_lengths(
            length, pmtu_code, int(ctx.wireProfile)
        )
        if int(ctx.wireProfile) == WIRE_LEGACY_PRIVATE:
            check_legacy_limits(chunk, pmtu_code)
        else:
            check_mrc_limits(chunk, pmtu_code)
    assert cursor == len(actual)


@cocotb.test(timeout_time=1_000_000, timeout_unit="ns")
async def randomized_psn_wrap_and_backpressure(dut):
    tb = TB(dut)
    await tb.reset()
    rng = random.Random(0x0BAD_C0DE)

    cases = []
    for index in range(12):
        pmtu = rng.randrange(len(PMTU_BYTES))
        profile = rng.choice(
            [WIRE_LEGACY_PRIVATE, WIRE_MRC_IPV4_ECMP_NO_ICRC]
        )
        length = rng.choice([1, 200, 1000, 5000])
        ctx = make_message_context(
            length=length,
            pmtu=pmtu,
            request_id=1000 + index,
            start_psn=0xFFFFFF - 2,
            wire_profile=profile,
            mrc_msn=index,
        )
        cases.append((ctx, payload_bytes(length, seed=1000 + index)))

    expected = [
        packet for ctx, payload in cases for packet in packetize(ctx, payload)
    ]

    async def produce():
        for ctx, payload in cases:
            await drive_message(tb, ctx, payload)

    producer = cocotb.start_soon(produce())
    actual = await collect_packets(tb, len(expected), stall=2)
    await producer
    assert_packets_equal(expected, actual)
    psns = [int(packet.descriptor.psn) for packet in actual]
    assert any(psn > 0xFFFF00 for psn in psns) and any(psn < 0x100 for psn in psns)
