import random

import cocotb

from bfm.m2 import make_message_context
from dma_phasea_bfm import DmaPhaseATB, IntegrationMessage, run_integration_scenario
from dma_phasea_model import make_chunks
from model.m2 import WIRE_LEGACY_PRIVATE, WIRE_MRC_IPV4_ECMP_NO_ICRC
from model.m3 import MacIpConfig


CFG = MacIpConfig(
    srcMac=0x020000000001,
    dstMac=0x020000000002,
    srcIp=0x0A000001,
    dstIp=0x0A000002,
)


def make_case(
    request_id: int,
    extents: list[tuple[int, int]],
    *,
    pmtu: int,
    start_psn: int,
    qpn: int,
    dest_qpn: int,
    wire_profile: int = WIRE_LEGACY_PRIVATE,
    ev_profile_id: int = 0,
    mrc_msn: int = 0,
) -> IntegrationMessage:
    chunks = make_chunks(request_id, extents)
    length = sum(int(chunk.len) for chunk in chunks)
    context = make_message_context(
        length=length,
        request_id=request_id,
        qpn=qpn,
        dest_qpn=dest_qpn,
        slot_id=request_id,
        debug_seq=request_id & 0xF,
        pmtu=pmtu,
        start_psn=start_psn,
        remote_addr=0x400000000000 + request_id * 0x100000,
        remote_key=0x55000000 | request_id,
        wire_profile=wire_profile,
        ev_profile_id=ev_profile_id,
        mrc_msn=mrc_msn,
    )
    return IntegrationMessage(context=context, chunks=chunks)


@cocotb.test(timeout_time=3_000_000, timeout_unit="ns")
async def noncontiguous_dma_reorder_to_byte_exact_phasea_frames(dut):
    tb = DmaPhaseATB(dut)
    await tb.reset()

    messages = [
        make_case(
            1,
            [(0x100007F, 1)],
            pmtu=0,
            start_psn=0x123400,
            qpn=0x101,
            dest_qpn=0x201,
        ),
        make_case(
            2,
            [
                (0x200003F, 513),
                (0x9000001, 1024),
                (0x3000DFF, 513),
            ],
            pmtu=0,
            start_psn=0xFFFFFE,
            qpn=0x102,
            dest_qpn=0x202,
        ),
        make_case(
            3,
            [
                (0x4000080, 1024),
                (0xA000040, 1024),
                (0x5000001, 1024),
                (0xB000800, 1024),
            ],
            pmtu=2,
            start_psn=0x200000,
            qpn=0x103,
            dest_qpn=0x203,
        ),
    ]
    await run_integration_scenario(tb, messages, CFG)


@cocotb.test(timeout_time=2_000_000, timeout_unit="ns")
async def chunk_may_arrive_before_matching_context(dut):
    tb = DmaPhaseATB(dut)
    await tb.reset()

    message = make_case(
        0x44,
        [(0x7000F80, 128), (0xC000001, 777), (0x6000200, 300)],
        pmtu=1,
        start_psn=0xABCDEF,
        qpn=0x12345,
        dest_qpn=0x54321,
    )
    await run_integration_scenario(
        tb,
        [message],
        CFG,
        channel_enable=0b0101,
        chunk_first=True,
    )


@cocotb.test(timeout_time=2_000_000, timeout_unit="ns")
async def mrc_profile_crosses_dma_m2_and_m3(dut):
    tb = DmaPhaseATB(dut)
    await tb.reset()

    message = make_case(
        0x55,
        [(0x710007F, 377), (0xC100001, 323)],
        pmtu=0,
        start_psn=0xFFFFFE,
        qpn=0x23456,
        dest_qpn=0x65432,
        wire_profile=WIRE_MRC_IPV4_ECMP_NO_ICRC,
        ev_profile_id=2,
        mrc_msn=0x1234,
    )
    await run_integration_scenario(
        tb,
        [message],
        CFG,
        channel_enable=0b0100,
        mrc_entropy=0xABCD,
        mrc_channel=2,
    )


@cocotb.test(timeout_time=2_000_000, timeout_unit="ns")
async def request_id_reuse_waits_for_previous_final_tx_commit(dut):
    tb = DmaPhaseATB(dut)
    await tb.reset()

    messages = [
        make_case(
            0x77,
            [(0xD00007F, 200)],
            pmtu=0,
            start_psn=0x100,
            qpn=0x700,
            dest_qpn=0x800,
        ),
        make_case(
            0x77,
            [(0xE000001, 127), (0xF000080, 300)],
            pmtu=1,
            start_psn=0x200,
            qpn=0x701,
            dest_qpn=0x801,
        ),
    ]
    await run_integration_scenario(tb, messages, CFG)


@cocotb.test(timeout_time=4_000_000, timeout_unit="ns")
async def randomized_noncontiguous_extents_and_all_pmtus(dut):
    tb = DmaPhaseATB(dut)
    await tb.reset()
    rng = random.Random(20260829)

    messages = []
    for request_id in range(0x100, 0x108):
        remaining = rng.randint(1, 6000)
        extents = []
        chunk_index = 0
        while remaining:
            length = min(remaining, rng.randint(1, 1024))
            page = 0x100000000 + request_id * 0x200000 + chunk_index * 0x4000
            lane = rng.randrange(4096 - length + 1)
            extents.append((page + lane, length))
            remaining -= length
            chunk_index += 1
        messages.append(
            make_case(
                request_id,
                extents,
                pmtu=request_id % 5,
                start_psn=(0xFFFFF8 + request_id) & 0xFFFFFF,
                qpn=0x1000 + request_id,
                dest_qpn=0x2000 + request_id,
            )
        )

    await run_integration_scenario(tb, messages, CFG, timeout_ns=3_500_000)
