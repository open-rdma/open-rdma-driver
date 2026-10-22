"""Randomized M3 regression over both wire profiles.

As with M2, `make test-long` used to re-run the short module. This suite drives
a fixed-seed mix of legacy and MRC packets while feedback and port-status
updates keep changing which EV entries are eligible, and checks that every
packet still comes out, that MRC frames carry a configured entropy in the UDP
source port, and that no scheduler stalls behind a blocked channel.
"""

import random

import cocotb
from cocotb.triggers import RisingEdge

from bfm.m2 import payload_bytes
from bfm.m3 import (
    TB, collect_commit, collect_frame, configure, configure_ev_entry,
    configure_ev_policy, configure_source_route, send_packet,
    send_path_feedback, update_port_status, wait_egress,
)
from model.m2 import (
    OP_MRC_WRITE_ONLY,
    OP_WRITE_ONLY,
    WIRE_LEGACY_PRIVATE,
    WIRE_MRC_IPV4_ECMP_NO_ICRC,
)
from model.m3 import (
    EV_FB_ALWAYS_SKIP, EV_FB_ECN, EV_FB_PROBE_GOOD, MrcEvEntryConfig,
    MrcPathFeedback, expected_frame,
)
from tests.m3.test_scheduler import CFG, descriptor

MRC = WIRE_MRC_IPV4_ECMP_NO_ICRC
ENTROPIES = (0xA000, 0xA001, 0xA002, 0xA003)
MRC_UDP_DESTINATION_PORT = 4791


def udp_source_port(frame: bytes) -> int:
    """UDP source port of an Ethernet/IPv4/UDP frame."""
    assert frame[12:14] == b"\x08\x00", frame[:16].hex()
    return int.from_bytes(frame[34:36], "big")


@cocotb.test(timeout_time=3_000_000, timeout_unit="ns")
async def randomized_mrc_paths_and_channel_masks(dut):
    tb = TB(dut)
    await tb.reset()
    rng = random.Random(0x1234_ABCD)
    await configure(tb, CFG, channel_enable=0xF)
    await configure_source_route(tb, mrc_mask=0b0001)
    await configure_ev_policy(tb, skip_hold_cycles=32, probe_interval_cycles=64)
    for index, entropy in enumerate(ENTROPIES):
        await configure_ev_entry(
            tb,
            MrcEvEntryConfig(
                profileId=0,
                entryIndex=index,
                valid=1,
                adminEnable=1,
                entropy=entropy,
                channel=index,
            ),
        )
    for _ in range(4):
        await RisingEdge(tb.clock)

    outstanding = 0
    for index in range(48):
        mrc = rng.random() < 0.6
        length = rng.choice([1, 3, 64, 128, 129, 257, 700])
        payload = payload_bytes(length, seed=index)
        # Only the `first` source is configured for the MRC profile; the
        # legacy packets ride the `ordered` source so both profiles can be
        # exercised without tripping the profile/route agreement assertion.
        desc = descriptor(
            request_id=2000 + index,
            payload_length=length,
            psn=0x2000 + index,
            opcode=OP_MRC_WRITE_ONLY if mrc else OP_WRITE_ONLY,
            traffic_class=0 if mrc else 2,
            wire_profile=MRC if mrc else WIRE_LEGACY_PRIVATE,
            mrc_msn_valid=1 if mrc else 0,
            mrc_msn=index,
        )
        await send_packet(tb, desc, payload, source="first" if mrc else "ordered")
        commit = await collect_commit(tb)
        channel = int(commit.channel)
        frame = await collect_frame(tb, channel)
        if mrc:
            expected = None
            for entropy in ENTROPIES:
                candidate = expected_frame(desc, payload, CFG, entropy=entropy)
                if candidate.data == frame.data:
                    expected = candidate
                    break
            assert expected is not None, frame.data.hex()
            assert udp_source_port(frame.data) in ENTROPIES
            assert frame.data[36:38] != b"\x00\x00"
            assert int.from_bytes(frame.data[36:38], "big") == (
                MRC_UDP_DESTINATION_PORT
            )
        else:
            assert frame.data == expected_frame(desc, payload, CFG).data
        outstanding += 1

        # Occasionally perturb the path state between packets: ECN-driven SKIP,
        # hard failure, and port reachability all change which EV is eligible,
        # but must never make the scheduler drop or stall a packet.
        roll = rng.random()
        if roll < 0.12 and outstanding:
            generation = _latest_generation(tb)
            await send_path_feedback(
                tb,
                MrcPathFeedback(
                    profileId=0,
                    entryIndex=rng.randrange(len(ENTROPIES)),
                    generation=generation,
                    kind=rng.choice([EV_FB_ECN, EV_FB_ALWAYS_SKIP, EV_FB_PROBE_GOOD]),
                    probeId=0,
                ),
            )
        elif roll < 0.2:
            mask = rng.choice([0b1110, 0b1101, 0b1011, 0b0111, 0b1111])
            await update_port_status(tb, local_reachable=mask, peer_reachable=0xF)

    assert outstanding == 48
    await wait_egress(tb, 1)


def _latest_generation(tb: TB) -> int:
    # The entry generator starts at 1 after the first configuration, and stale
    # feedback is ignored by design, so either value is safe to send.
    return 1 if not tb.egress_events else int(tb.egress_events[-1].evGeneration)
