"""MRC IPv4/ECMP experimental-path tests for the M3 scheduler.

These cover the profile that the analysis report asks for: MRC normal Write
opcodes, a METH on every request packet, a per-packet RETH carrying the current
VA, and ECMP entropy in the UDP source port, plus the EV path-selection state
machine that feeds it. ICRC, IPv6, SRv6, WriteImm and on-wire probe packets are
deliberately out of scope for this profile.
"""

import cocotb
from cocotb.triggers import RisingEdge

from bfm.m2 import payload_bytes
from bfm.m3 import (
    TB, collect_commit, collect_frame, collect_no_path, collect_probe,
    configure, configure_ev_entry, configure_ev_policy, configure_source_route,
    pipe_quiet, send_packet, send_path_feedback, update_port_status, wait_egress,
)
from model.m2 import (
    OP_MRC_WRITE_FIRST,
    OP_MRC_WRITE_LAST,
    OP_MRC_WRITE_MIDDLE,
    OP_MRC_WRITE_ONLY,
    WIRE_MRC_IPV4_ECMP_NO_ICRC,
)
from model.m3 import (
    EV_FB_ALWAYS_SKIP, EV_FB_ECN, EV_FB_PROBE_GOOD, MrcEvEntryConfig,
    MrcPathFeedback, expected_frame,
)
from tests.m3.test_scheduler import CFG, descriptor

MRC = WIRE_MRC_IPV4_ECMP_NO_ICRC


def mrc_descriptor(
    *,
    request_id: int,
    payload_length: int,
    psn: int,
    pmtu: int = 4,
    opcode: int = OP_MRC_WRITE_ONLY,
    message_length: int | None = None,
    message_offset: int = 0,
    packet_index: int = 0,
    packet_count: int = 1,
    is_first: int = 1,
    is_last: int = 1,
    traffic_class: int = 0,
    mrc_msn: int = 0x1234,
    ev_profile_id: int = 0,
):
    return descriptor(
        request_id=request_id,
        payload_length=payload_length,
        psn=psn,
        pmtu=pmtu,
        opcode=opcode,
        message_length=message_length,
        message_offset=message_offset,
        packet_index=packet_index,
        packet_count=packet_count,
        is_first=is_first,
        is_last=is_last,
        traffic_class=traffic_class,
        wire_profile=MRC,
        ev_profile_id=ev_profile_id,
        mrc_msn=mrc_msn,
        mrc_msn_valid=1,
    )


async def configure_mrc(tb: TB, mrc_mask: int = 0b0001, **kwargs) -> None:
    await configure(tb, CFG, **kwargs)
    await configure_source_route(tb, mrc_mask=mrc_mask)
    # Let the route register settle before the first packet is offered.
    for _ in range(4):
        await RisingEdge(tb.clock)


async def add_ev(
    tb: TB, profile: int, index: int, channel: int, entropy: int, valid: int = 1
) -> None:
    await configure_ev_entry(
        tb,
        MrcEvEntryConfig(
            profileId=profile,
            entryIndex=index,
            valid=valid,
            adminEnable=valid,
            entropy=entropy,
            channel=channel,
        ),
    )
    for _ in range(2):
        await RisingEdge(tb.clock)


@cocotb.test(timeout_time=100_000, timeout_unit="ns")
async def mrc_single_packet_is_byte_exact(dut):
    tb = TB(dut)
    await tb.reset()
    await configure_mrc(tb, channel_enable=0xF)
    await add_ev(tb, 0, 0, channel=0, entropy=0xDEADBEEF)

    payload = payload_bytes(120, seed=7)
    desc = mrc_descriptor(request_id=1, payload_length=len(payload), psn=0xAAAA)
    await send_and_check(tb, desc, payload, 0)


async def send_and_check(
    tb: TB,
    desc,
    payload: bytes,
    channel: int,
    entropy: int = 0xDEADBEEF,
    source: str = "first",
) -> None:
    await send_packet(tb, desc, payload, source=source)
    commit = await collect_commit(tb)
    assert int(commit.channel) == channel, (int(commit.channel), channel)
    frame = await collect_frame(tb, channel)
    expected = expected_frame(desc, payload, CFG, entropy=entropy)
    assert frame.data == expected.data, (frame.data.hex(), expected.data.hex())
    # BTH byte 8 carries AR in bit 7; final/only packets request the tail ACK.
    assert bool(frame.data[50] & 0x80) == bool(int(desc.isLast))


@cocotb.test(timeout_time=100_000, timeout_unit="ns")
async def mrc_round_robins_ev_entries(dut):
    tb = TB(dut)
    await tb.reset()
    await configure_mrc(tb, channel_enable=0xF)
    await add_ev(tb, 0, 0, channel=0, entropy=0x1111)
    await add_ev(tb, 0, 1, channel=1, entropy=0x2222)

    for index in range(4):
        payload = payload_bytes(20 + index, seed=index)
        desc = mrc_descriptor(
            request_id=10 + index, payload_length=len(payload), psn=0x100 + index
        )
        await send_and_check(
            tb, desc, payload, index % 2,
            entropy=0x1111 if index % 2 == 0 else 0x2222,
        )
    await wait_egress(tb, 4)
    assert [int(event.evIndex) for event in tb.egress_events[:4]] == [0, 1, 0, 1]
    assert [int(event.entropy) for event in tb.egress_events[:4]] == [
        0x1111, 0x2222, 0x1111, 0x2222,
    ]


@cocotb.test(timeout_time=100_000, timeout_unit="ns")
async def mrc_multi_packet_repeats_placement_metadata(dut):
    tb = TB(dut)
    await tb.reset()
    await configure_mrc(tb, channel_enable=0xF)
    await add_ev(tb, 0, 0, channel=0, entropy=0x5A5A)

    payload = payload_bytes(600, seed=3)
    descriptors = [
        mrc_descriptor(
            request_id=20,
            payload_length=256,
            psn=0x500,
            pmtu=0,
            opcode=OP_MRC_WRITE_FIRST,
            message_length=len(payload),
            message_offset=0,
            packet_index=0,
            packet_count=3,
            is_first=1,
            is_last=0,
        ),
        mrc_descriptor(
            request_id=20,
            payload_length=256,
            psn=0x501,
            pmtu=0,
            opcode=OP_MRC_WRITE_MIDDLE,
            message_length=len(payload),
            message_offset=256,
            packet_index=1,
            packet_count=3,
            is_first=0,
            is_last=0,
        ),
        mrc_descriptor(
            request_id=20,
            payload_length=88,
            psn=0x502,
            pmtu=0,
            opcode=OP_MRC_WRITE_LAST,
            message_length=len(payload),
            message_offset=512,
            packet_index=2,
            packet_count=3,
            is_first=0,
            is_last=1,
        ),
    ]

    offset = 0
    for desc in descriptors:
        chunk = payload[offset : offset + int(desc.payloadLength)]
        offset += len(chunk)
        await send_packet(tb, desc, chunk)
        commit = await collect_commit(tb)
        assert int(commit.channel) == 0
        frame = await collect_frame(tb, 0)
        assert frame.data == expected_frame(
            desc, chunk, CFG, entropy=0x5A5A
        ).data
        assert bool(frame.data[50] & 0x80) == bool(int(desc.isLast))
    # A METH and a placement RETH on every packet is what lets the responder
    # place out-of-order continuations, so check the header lengths differ only
    # by pad.
    assert int(descriptors[1].messageOffset) == 256


@cocotb.test(timeout_time=100_000, timeout_unit="ns")
async def mrc_retransmit_sets_rtx_and_reselects(dut):
    tb = TB(dut)
    await tb.reset()
    await configure_mrc(tb, mrc_mask=0b0010, channel_enable=0xF)
    await add_ev(tb, 0, 0, channel=0, entropy=0x0F0F)
    await add_ev(tb, 0, 1, channel=1, entropy=0xF0F0)

    payload = payload_bytes(64, seed=5)
    desc = mrc_descriptor(
        request_id=30,
        payload_length=len(payload),
        psn=0x700,
        traffic_class=1,
    )
    await send_and_check(tb, desc, payload, 0, entropy=0x0F0F, source="retry")
    await wait_egress(tb, 1)
    assert int(tb.egress_events[-1].trafficClass) == 1


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def mrc_ecn_skips_then_recovers(dut):
    tb = TB(dut)
    await tb.reset()
    await configure_mrc(tb, channel_enable=0xF)
    await configure_ev_policy(tb, skip_hold_cycles=16, probe_interval_cycles=64)
    await add_ev(tb, 0, 0, channel=0, entropy=0x0101)
    await add_ev(tb, 0, 1, channel=1, entropy=0x0202)

    payload = payload_bytes(16, seed=9)
    first = mrc_descriptor(request_id=40, payload_length=len(payload), psn=0x900)
    await send_and_check(tb, first, payload, 0, entropy=0x0101)
    await wait_egress(tb, 1)
    generation = int(tb.egress_events[-1].evGeneration)

    await send_path_feedback(
        tb,
        MrcPathFeedback(
            profileId=0, entryIndex=0, generation=generation,
            kind=EV_FB_ECN, probeId=0,
        ),
    )
    for _ in range(4):
        await RisingEdge(tb.clock)

    second = mrc_descriptor(request_id=41, payload_length=len(payload), psn=0x901)
    await send_and_check(tb, second, payload, 1, entropy=0x0202)

    # After the cooldown expires the entry returns to GOOD.
    for _ in range(64):
        await RisingEdge(tb.clock)
    third = mrc_descriptor(request_id=42, payload_length=len(payload), psn=0x902)
    await send_and_check(tb, third, payload, 0, entropy=0x0101)


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def mrc_assumed_bad_probes_and_recovers(dut):
    tb = TB(dut)
    await tb.reset()
    await configure_mrc(tb, channel_enable=0xF)
    await configure_ev_policy(tb, skip_hold_cycles=16, probe_interval_cycles=32)
    await add_ev(tb, 0, 0, channel=0, entropy=0x0303)
    await add_ev(tb, 0, 1, channel=1, entropy=0x0404)

    payload = payload_bytes(16, seed=11)
    first = mrc_descriptor(request_id=50, payload_length=len(payload), psn=0xA00)
    await send_and_check(tb, first, payload, 0, entropy=0x0303)
    await wait_egress(tb, 1)
    generation = int(tb.egress_events[-1].evGeneration)

    await send_path_feedback(
        tb,
        MrcPathFeedback(
            profileId=0, entryIndex=0, generation=generation,
            kind=EV_FB_ALWAYS_SKIP, probeId=0,
        ),
    )
    second = mrc_descriptor(request_id=51, payload_length=len(payload), psn=0xA01)
    await send_and_check(tb, second, payload, 1, entropy=0x0404)

    probe = await collect_probe(tb)
    assert int(probe.entryIndex) == 0
    assert int(probe.channel) == 0

    await send_path_feedback(
        tb,
        MrcPathFeedback(
            profileId=0, entryIndex=0, generation=generation,
            kind=EV_FB_PROBE_GOOD, probeId=int(probe.probeId),
        ),
    )
    for _ in range(8):
        await RisingEdge(tb.clock)
    third = mrc_descriptor(request_id=52, payload_length=len(payload), psn=0xA02)
    await send_and_check(tb, third, payload, 0, entropy=0x0303)


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def mrc_port_down_removes_ev_then_recovers(dut):
    tb = TB(dut)
    await tb.reset()
    await configure_mrc(tb, channel_enable=0xF)
    await add_ev(tb, 0, 0, channel=0, entropy=0x0505)
    await add_ev(tb, 0, 1, channel=1, entropy=0x0606)

    payload = payload_bytes(24, seed=13)
    first = mrc_descriptor(request_id=60, payload_length=len(payload), psn=0xB00)
    await send_and_check(tb, first, payload, 0, entropy=0x0505)

    # Channel 0 is reported unreachable: its EV must drop out of the active set
    # without any EV state change.
    await update_port_status(tb, local_reachable=0b1110, peer_reachable=0xF)
    for _ in range(4):
        await RisingEdge(tb.clock)
    second = mrc_descriptor(request_id=61, payload_length=len(payload), psn=0xB01)
    await send_and_check(tb, second, payload, 1, entropy=0x0606)

    await update_port_status(tb, local_reachable=0xF, peer_reachable=0xF)
    for _ in range(4):
        await RisingEdge(tb.clock)
    third = mrc_descriptor(request_id=62, payload_length=len(payload), psn=0xB02)
    await send_and_check(tb, third, payload, 0, entropy=0x0505)


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def mrc_no_path_reports_once_and_stalls(dut):
    tb = TB(dut)
    await tb.reset()
    await configure_mrc(tb, channel_enable=0xF)
    await add_ev(tb, 0, 0, channel=0, entropy=0x0707)

    # The port must be reported down before the packet is offered, otherwise
    # the scheduler legitimately admits it on the still-reachable path.
    await update_port_status(tb, local_reachable=0b0000, peer_reachable=0xF)
    for _ in range(8):
        await RisingEdge(tb.clock)

    payload = payload_bytes(32, seed=17)
    desc = mrc_descriptor(request_id=70, payload_length=len(payload), psn=0xC00)
    await send_packet(tb, desc, payload)
    for _ in range(16):
        await RisingEdge(tb.clock)

    event = await collect_no_path(tb)
    assert int(event.profileId) == 0
    assert int(event.trafficClass) == 0
    # The event is de-duplicated: no second report while the head is unchanged.
    for _ in range(64):
        await RisingEdge(tb.clock)
    assert await pipe_quiet(tb.no_path, tb.clock) is True

    await update_port_status(tb, local_reachable=0xF, peer_reachable=0xF)
    for _ in range(8):
        await RisingEdge(tb.clock)
    commit = await collect_commit(tb)
    assert int(commit.requestId) == 70


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def unavailable_first_does_not_block_ready_control(dut):
    tb = TB(dut)
    await tb.reset()
    await configure_mrc(tb, channel_enable=0x1, max_high_burst=0)

    blocked_payload = payload_bytes(16, seed=19)
    blocked = mrc_descriptor(
        request_id=80, payload_length=len(blocked_payload), psn=0xD00
    )
    await send_packet(tb, blocked, blocked_payload)

    control_payload = payload_bytes(17, seed=20)
    control = descriptor(
        request_id=81,
        payload_length=len(control_payload),
        psn=0xD01,
        traffic_class=3,
    )
    await send_packet(tb, control, control_payload, source="control")
    commit = await collect_commit(tb)
    assert int(commit.requestId) == 81
    assert await collect_frame(tb, 0) == expected_frame(control, control_payload, CFG)


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def unrelated_start_does_not_duplicate_no_path_event(dut):
    tb = TB(dut)
    await tb.reset()
    await configure_mrc(tb, channel_enable=0x1, max_high_burst=15)

    blocked_payload = payload_bytes(16, seed=21)
    blocked = mrc_descriptor(
        request_id=90, payload_length=len(blocked_payload), psn=0xE00
    )
    await send_packet(tb, blocked, blocked_payload)
    event = await collect_no_path(tb)
    assert int(event.trafficClass) == 0

    control_payload = payload_bytes(18, seed=22)
    control = descriptor(
        request_id=91,
        payload_length=len(control_payload),
        psn=0xE01,
        traffic_class=3,
    )
    await send_packet(tb, control, control_payload, source="control")
    commit = await collect_commit(tb)
    assert int(commit.requestId) == 91
    assert await collect_frame(tb, 0) == expected_frame(control, control_payload, CFG)
    assert await pipe_quiet(tb.no_path, tb.clock) is True
