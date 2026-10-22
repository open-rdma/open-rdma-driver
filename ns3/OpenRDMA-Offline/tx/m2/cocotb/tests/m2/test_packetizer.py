"""Fast M2 packetizer smoke tests."""

import cocotb
from cocotb.triggers import RisingEdge

from bfm.m2 import (
    TB,
    assert_packets_equal,
    collect_packets,
    drive_message,
    make_message_context,
    payload_bytes,
    pipe_get,
    run_message,
)
from model.m2 import (
    OP_WRITE_FIRST,
    OP_WRITE_LAST,
    OP_WRITE_MIDDLE,
    OP_WRITE_ONLY,
    PMTU_BYTES,
    WIRE_MRC_IPV4_ECMP_NO_ICRC,
    PacketDescriptor,
    packet_lengths,
    packetize,
)


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def packetizes_boundaries_and_wraps_psn(dut):
    tb = TB(dut)
    await tb.reset()
    payload = payload_bytes(1000)
    ctx = make_message_context(
        length=len(payload), pmtu=0, start_psn=0xFFFFFE, request_id=3
    )
    expected, actual = await run_message(tb, ctx, payload)
    assert_packets_equal(expected, actual)
    assert [int(packet.descriptor.psn) for packet in actual[:4]] == [
        0xFFFFFE,
        0xFFFFFF,
        0,
        1,
    ]


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def preserves_tail_keep_and_backpressure(dut):
    tb = TB(dut)
    await tb.reset()
    for request_id, length in enumerate((1, 31, 32, 33, 127, 128, 129)):
        payload = payload_bytes(length, seed=length)
        ctx = make_message_context(
            length=length, pmtu=4, request_id=request_id, start_psn=10 + request_id
        )
        expected, actual = await run_message(tb, ctx, payload, stall=2)
        assert_packets_equal(expected, actual)


@cocotb.test(timeout_time=400_000, timeout_unit="ns")
async def covers_every_pmtu_capacity_transition(dut):
    tb = TB(dut)
    await tb.reset()
    request_id = 100
    for pmtu, mtu_bytes in enumerate(PMTU_BYTES):
        first_limit = mtu_bytes - 28
        continuation_limit = mtu_bytes - 12
        for length in (
            first_limit,
            first_limit + 1,
            first_limit + continuation_limit + 1,
        ):
            payload = payload_bytes(length, seed=(pmtu << 16) | length)
            ctx = make_message_context(
                length=length,
                pmtu=pmtu,
                request_id=request_id,
                start_psn=0x1000 + request_id,
            )
            expected, actual = await run_message(tb, ctx, payload)
            assert_packets_equal(expected, actual)
            request_id += 1


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def handles_maximum_first_packet_payload(dut):
    tb = TB(dut)
    await tb.reset()
    length = PMTU_BYTES[4] - 28
    payload = payload_bytes(length, seed=0x4068)
    ctx = make_message_context(length=length, pmtu=4, request_id=200)
    expected, actual = await run_message(tb, ctx, payload)
    assert_packets_equal(expected, actual)
    assert len(actual) == 1
    assert len(actual[0].payload) == 4068


@cocotb.test(timeout_time=300_000, timeout_unit="ns")
async def isolates_back_to_back_message_residuals(dut):
    tb = TB(dut)
    await tb.reset()
    cases = []
    for request_id, length, pmtu in (
        (301, 100, 0),
        (302, 129, 4),
        (303, 500, 0),
        (304, 257, 1),
    ):
        payload = payload_bytes(length, seed=request_id)
        ctx = make_message_context(
            length=length,
            pmtu=pmtu,
            request_id=request_id,
            start_psn=0x200 + request_id,
        )
        cases.append((ctx, payload))

    expected = [packet for ctx, payload in cases for packet in packetize(ctx, payload)]

    async def produce():
        for ctx, payload in cases:
            await drive_message(tb, ctx, payload)

    producer = cocotb.start_soon(produce())
    actual = await collect_packets(tb, len(expected), stall=1)
    await producer
    assert_packets_equal(expected, actual)
    assert [int(packet.descriptor.requestId) for packet in actual] == [
        int(packet.descriptor.requestId) for packet in expected
    ]


@cocotb.test(timeout_time=300_000, timeout_unit="ns")
async def recovers_after_descriptor_and_payload_backpressure(dut):
    tb = TB(dut)
    await tb.reset()
    payload = payload_bytes(1500, seed=0xBACC)
    ctx = make_message_context(
        length=len(payload), pmtu=0, request_id=400, start_psn=0x123400
    )
    expected = packetize(ctx, payload)
    producer = cocotb.start_soon(drive_message(tb, ctx, payload))

    # Hold both M2 outputs long enough to fill their queues and propagate
    # backpressure into the message input.
    for _ in range(30):
        await RisingEdge(tb.clock)

    actual = await collect_packets(tb, len(expected), stall=3)
    await producer
    assert_packets_equal(expected, actual)


@cocotb.test(timeout_time=300_000, timeout_unit="ns")
async def accepts_paced_input_without_losing_beats(dut):
    tb = TB(dut)
    await tb.reset()
    payload = payload_bytes(1777, seed=0xFACE)
    ctx = make_message_context(
        length=len(payload), pmtu=1, request_id=500, start_psn=0xABCDEF
    )
    expected = packetize(ctx, payload)
    producer = cocotb.start_soon(drive_message(tb, ctx, payload, stride=3))
    actual = await collect_packets(tb, len(expected), stall=2)
    await producer
    assert_packets_equal(expected, actual)


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def emits_consistent_packet_metadata(dut):
    tb = TB(dut)
    await tb.reset()
    payload = payload_bytes(717, seed=0x717)
    base_address = 0x1020304050607080
    ctx = make_message_context(
        length=len(payload),
        pmtu=0,
        request_id=600,
        start_psn=0xFFFFFD,
        remote_addr=base_address,
    )
    expected, actual = await run_message(tb, ctx, payload)
    assert_packets_equal(expected, actual)

    lengths = packet_lengths(len(payload), 0)
    assert [int(packet.descriptor.payloadLength) for packet in actual] == lengths
    assert [int(packet.descriptor.packetIndex) for packet in actual] == list(range(len(lengths)))
    assert all(int(packet.descriptor.packetCount) == len(lengths) for packet in actual)
    offsets = [sum(lengths[:index]) for index in range(len(lengths))]
    assert [int(packet.descriptor.messageOffset) for packet in actual] == offsets
    assert [int(packet.descriptor.remoteAddr) for packet in actual] == [
        base_address + offset for offset in offsets
    ]
    expected_opcodes = [
        OP_WRITE_ONLY if len(lengths) == 1 else
        OP_WRITE_FIRST if index == 0 else
        OP_WRITE_LAST if index == len(lengths) - 1 else
        OP_WRITE_MIDDLE
        for index in range(len(lengths))
    ]
    assert [int(packet.descriptor.opcode) for packet in actual] == expected_opcodes


@cocotb.test(timeout_time=100_000, timeout_unit="ns")
async def preserves_maximum_mrc_packet_count_width(dut):
    tb = TB(dut)
    await tb.reset()
    length = (1 << 25) - 1
    ctx = make_message_context(
        length=length,
        pmtu=0,
        request_id=700,
        wire_profile=WIRE_MRC_IPV4_ECMP_NO_ICRC,
    )
    await tb.context.enq(ctx.pack())
    descriptor = PacketDescriptor.unpack(await pipe_get(tb.descriptor, tb.clock))
    assert int(descriptor.packetCount) == 131_072
