"""Fast M3 scheduler and encapsulator smoke tests."""

import cocotb
from cocotb.triggers import RisingEdge

from bfm.m2 import payload_bytes
from bfm.m3 import TB, collect_commit, collect_frame, configure, send_packet
from model.m2 import (
    OP_WRITE_MIDDLE,
    OP_WRITE_ONLY,
    WIRE_LEGACY_PRIVATE,
    PacketDescriptor,
    TRAFFIC_FIRST,
)
from model.m3 import MacIpConfig, expected_frame


CFG = MacIpConfig(
    srcMac=0x020304050607,
    dstMac=0x0A0B0C0D0E0F,
    srcIp=0xC0A80101,
    dstIp=0xC0A80102,
)


def descriptor(
    *,
    request_id: int,
    payload_length: int,
    psn: int,
    pmtu: int = 4,
    traffic_class: int = 0,
    opcode: int = OP_WRITE_ONLY,
    packet_index: int = 0,
    packet_count: int = 1,
    message_offset: int = 0,
    message_length: int | None = None,
    is_first: int = 1,
    is_last: int = 1,
    wire_profile: int = WIRE_LEGACY_PRIVATE,
    ev_profile_id: int = 0,
    mrc_msn: int = 0,
    mrc_msn_valid: int = 0,
) -> PacketDescriptor:
    return PacketDescriptor(
        requestId=request_id,
        qpn=0x010203,
        destQpn=0x0A0B0C,
        slotId=request_id,
        debugSeq=request_id & 0xF,
        pmtu=pmtu,
        psn=psn,
        packetIndex=packet_index,
        packetCount=packet_count,
        opcode=opcode,
        remoteAddr=0x1122334455667788 + message_offset,
        remoteKey=0x99AABBCC,
        messageOffset=message_offset,
        payloadLength=payload_length,
        messageLength=payload_length if message_length is None else message_length,
        isFirst=is_first,
        isLast=is_last,
        trafficClass=traffic_class,
        wireProfile=wire_profile,
        evProfileId=ev_profile_id,
        mrcMsn=mrc_msn,
        mrcMsnValid=mrc_msn_valid,
    )


def assert_frame(expected, actual):
    assert actual == expected


@cocotb.test(timeout_time=100_000, timeout_unit="ns")
async def encapsulates_single_packet_byte_exact(dut):
    tb = TB(dut)
    await tb.reset()
    await configure(tb, CFG, channel_enable=0x1)
    payload = payload_bytes(259, seed=1)
    desc = descriptor(request_id=1, payload_length=len(payload), psn=0xFFFFFE)
    await send_packet(tb, desc, payload)
    commit = await collect_commit(tb)
    assert int(commit.requestId) == 1
    assert int(commit.channel) == 0
    assert_frame(expected_frame(desc, payload, CFG), await collect_frame(tb, 0))


@cocotb.test(timeout_time=100_000, timeout_unit="ns")
async def assigns_round_robin_and_commits_once(dut):
    tb = TB(dut)
    await tb.reset()
    await configure(tb, CFG)

    packets = []
    for index in range(5):
        payload = payload_bytes(3 + index, seed=100 + index)
        desc = descriptor(request_id=10 + index, payload_length=len(payload), psn=50 + index)
        packets.append((desc, payload))
        await send_packet(tb, desc, payload)

    commits = [await collect_commit(tb) for _ in packets]
    assert [int(commit.channel) for commit in commits] == [0, 1, 2, 3, 0]
    assert [int(commit.requestId) for commit in commits] == [10, 11, 12, 13, 14]

    expected_by_channel = [[packets[0], packets[4]], [packets[1]], [packets[2]], [packets[3]]]
    for channel, expected_packets in enumerate(expected_by_channel):
        for desc, payload in expected_packets:
            assert_frame(expected_frame(desc, payload, CFG), await collect_frame(tb, channel))


@cocotb.test(timeout_time=100_000, timeout_unit="ns")
async def arbitrates_only_at_packet_boundaries(dut):
    tb = TB(dut)
    await tb.reset()
    await configure(tb, CFG, channel_enable=0)

    staged = {}
    for source, traffic_class, request_id in (
        ("first", 0, 20),
        ("retry", 1, 21),
        ("ordered", 2, 22),
        ("control", 3, 23),
    ):
        payload = payload_bytes(129 + traffic_class, seed=request_id)
        desc = descriptor(
            request_id=request_id,
            payload_length=len(payload),
            psn=100 + traffic_class,
            traffic_class=traffic_class,
        )
        staged[source] = (desc, payload)
        await send_packet(tb, desc, payload, source=source)

    await configure(tb, CFG, channel_enable=0xF)
    commits = [await collect_commit(tb) for _ in range(4)]
    assert [int(commit.requestId) for commit in commits] == [23, 22, 21, 20]
    assert [int(commit.channel) for commit in commits] == [0, 1, 2, 3]

    for channel, source in enumerate(("control", "ordered", "retry", "first")):
        desc, payload = staged[source]
        assert_frame(expected_frame(desc, payload, CFG), await collect_frame(tb, channel))


@cocotb.test(timeout_time=150_000, timeout_unit="ns")
async def skips_disabled_channels_in_round_robin(dut):
    tb = TB(dut)
    await tb.reset()
    await configure(tb, CFG, channel_enable=0b1010)

    packets = []
    for index in range(4):
        payload = payload_bytes(5 + index, seed=700 + index)
        desc = descriptor(
            request_id=700 + index, payload_length=len(payload), psn=0x700 + index
        )
        packets.append((desc, payload))
        await send_packet(tb, desc, payload)

    commits = [await collect_commit(tb) for _ in packets]
    assert [int(commit.channel) for commit in commits] == [1, 3, 1, 3]
    expected_by_channel = {1: [packets[0], packets[2]], 3: [packets[1], packets[3]]}
    for channel, expected_packets in expected_by_channel.items():
        for desc, payload in expected_packets:
            assert_frame(expected_frame(desc, payload, CFG), await collect_frame(tb, channel))


@cocotb.test(timeout_time=150_000, timeout_unit="ns")
async def keeps_bound_packet_on_channel_after_reconfiguration(dut):
    tb = TB(dut)
    await tb.reset()
    await configure(tb, CFG, channel_enable=0b0001)

    payload0 = payload_bytes(300, seed=800)
    desc0 = descriptor(request_id=800, payload_length=len(payload0), psn=0x800)
    await send_packet(tb, desc0, payload0)
    commit0 = await collect_commit(tb)
    assert int(commit0.channel) == 0

    await configure(tb, CFG, channel_enable=0b0010)
    payload1 = payload_bytes(301, seed=801)
    desc1 = descriptor(request_id=801, payload_length=len(payload1), psn=0x801)
    await send_packet(tb, desc1, payload1)
    commit1 = await collect_commit(tb)
    assert int(commit1.channel) == 1

    assert_frame(expected_frame(desc0, payload0, CFG), await collect_frame(tb, 0))
    assert_frame(expected_frame(desc1, payload1, CFG), await collect_frame(tb, 1))


@cocotb.test(timeout_time=200_000, timeout_unit="ns")
async def limits_a_stalled_channel_to_two_packets(dut):
    tb = TB(dut)
    await tb.reset()
    await configure(tb, CFG, channel_enable=0b0001)

    packets = []
    for index, length in enumerate((129, 257, 33)):
        payload = payload_bytes(length, seed=900 + index)
        desc = descriptor(
            request_id=900 + index, payload_length=length, psn=0x900 + index
        )
        packets.append((desc, payload))
        await send_packet(tb, desc, payload)

    first_commits = [await collect_commit(tb) for _ in range(2)]
    assert [int(commit.requestId) for commit in first_commits] == [900, 901]
    for _ in range(20):
        await RisingEdge(tb.clock)
        assert not await tb.commits.not_empty(), "third packet exceeded the two-slot limit"

    assert_frame(
        expected_frame(*packets[0], CFG),
        await collect_frame(tb, 0),
    )
    third_commit = await collect_commit(tb)
    assert int(third_commit.requestId) == 902
    assert int(third_commit.channel) == 0
    for desc, payload in packets[1:]:
        assert_frame(expected_frame(desc, payload, CFG), await collect_frame(tb, 0))


@cocotb.test(timeout_time=250_000, timeout_unit="ns")
async def preserves_payload_at_gearbox_tail_boundaries(dut):
    tb = TB(dut)
    await tb.reset()
    await configure(tb, CFG, channel_enable=0b0001)

    for index, length in enumerate((1, 10, 11, 26, 27, 31, 32, 33, 127, 128, 129)):
        payload = payload_bytes(length, seed=1000 + length)
        desc = descriptor(
            request_id=1000 + index,
            payload_length=length,
            psn=0x1000 + index,
        )
        await send_packet(tb, desc, payload)
        commit = await collect_commit(tb)
        assert int(commit.channel) == 0
        assert_frame(expected_frame(desc, payload, CFG), await collect_frame(tb, 0))


@cocotb.test(timeout_time=250_000, timeout_unit="ns")
async def encapsulates_maximum_continuation_payload(dut):
    tb = TB(dut)
    await tb.reset()
    await configure(tb, CFG, channel_enable=0b0100)

    payload = payload_bytes(4084, seed=0x4084)
    desc = descriptor(
        request_id=1100,
        payload_length=len(payload),
        psn=0xABCDE0,
        opcode=OP_WRITE_MIDDLE,
        packet_index=1,
        packet_count=3,
        message_offset=4068,
        message_length=9000,
        is_first=0,
        is_last=0,
    )
    await send_packet(tb, desc, payload)
    commit = await collect_commit(tb)
    assert int(commit.channel) == 2
    assert_frame(expected_frame(desc, payload, CFG), await collect_frame(tb, 2))


@cocotb.test(timeout_time=150_000, timeout_unit="ns")
async def guarantees_first_service_with_high_priority_backlog(dut):
    tb = TB(dut)
    await tb.reset()
    await configure(tb, CFG, channel_enable=0, max_high_burst=1)

    first_payload = payload_bytes(5, seed=1200)
    first_desc = descriptor(request_id=1200, payload_length=5, psn=0x1200)
    await send_packet(tb, first_desc, first_payload, source="first")

    controls = []
    for index in range(2):
        payload = payload_bytes(6 + index, seed=1210 + index)
        desc = descriptor(
            request_id=1210 + index,
            payload_length=len(payload),
            psn=0x1210 + index,
            traffic_class=3,
        )
        controls.append((desc, payload))
        await send_packet(tb, desc, payload, source="control")

    await configure(tb, CFG, channel_enable=0xF, max_high_burst=1)
    commits = [await collect_commit(tb) for _ in range(3)]
    assert [int(commit.requestId) for commit in commits] == [1210, 1200, 1211]
    assert [int(commit.channel) for commit in commits] == [0, 1, 2]

    expected = [controls[0], (first_desc, first_payload), controls[1]]
    for channel, (desc, payload) in enumerate(expected):
        assert_frame(expected_frame(desc, payload, CFG), await collect_frame(tb, channel))
