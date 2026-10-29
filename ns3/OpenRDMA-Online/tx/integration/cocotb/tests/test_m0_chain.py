"""End-to-end M0 -> M3 chain tests on mkTestM0Chain (short suite).

Every test drives real GDRP submissions plus the four M0 table configuration
servers, then scores DmaEngine requests, DmaMessageDone, TxCommit and the
four channel frame streams against the M0 reference model + Phase-A
packetizer/M3 encapsulator expectations. Lengths and PSNs are NEVER read back
from the DUT; the expectation is derived purely from the configuration and the
submission order.

All local offsets are 4 KiB aligned so every DMA burst starts on a 128-byte
window boundary; the unaligned-window case is covered by the mvp tx unit
suite, and the DmaEngine never has to arbitrate two requests over one
completion window in this chain test.
"""

import random

import cocotb

from m0_chain_bfm import (
    M0ChainTB,
    M0Submission,
    configure_mr,
    configure_pte,
    configure_qp,
    configure_slot,
    run_m0_chain_scenario,
)
from m0_chain_model import (
    M0RefModel,
    MrEntry,
    PayloadSlot,
    PteEntry,
    QpEntry,
    make_local_lkey,
)
from model.m3 import MacIpConfig

CFG = MacIpConfig(
    srcMac=0x020000000001,
    dstMac=0x020000000002,
    srcIp=0x0A000001,
    dstIp=0x0A000002,
)

VIRT_BASE = 0x0000_0000_1000_0000
REMOTE_BASE = 0x0000_2000_0000_0000
MEM_BASE = 0x0000_0080_0000_0000
REMOTE_KEY = 0x5566_7788
KEY_PART = 7
MR_INDEX = 0
PAGE = 4096

PMTU_LIMITS = {0: (256, 28), 1: (512, 28), 2: (1024, 28), 3: (2048, 28), 4: (4096, 28)}


async def bring_up(
    tb: M0ChainTB,
    model: M0RefModel,
    *,
    qps: dict[int, int],
    slots: dict[int, tuple[int, int]],
    psn_starts: dict[int, int] | None = None,
) -> None:
    """Configure one 4 KiB-page MR (pgtOffset 0) plus the given QPs and slots."""
    page_bytes = 1 << 12
    span = max(offset + max_len for offset, max_len in slots.values())
    pte_count = (span + page_bytes - 1) // page_bytes
    assert pte_count <= 128
    mr = MrEntry(
        valid=1,
        keyPart=KEY_PART,
        allowLocalRead=1,
        baseVa=VIRT_BASE,
        length=span,
        pgtOffset=0,
        pageShift=12,
    )
    await configure_mr(tb, model, MR_INDEX, mr)
    for index in range(pte_count):
        pte = PteEntry(valid=1, physicalPageBase=MEM_BASE + index * page_bytes)
        await configure_pte(tb, model, index, pte)
    for qpn, pmtu in qps.items():
        initial = (psn_starts or {}).get(qpn, 0)
        qp = QpEntry(valid=1, pmtu=pmtu, initialPsn=initial)
        await configure_qp(tb, model, qpn, qp)
    for slot_id, (offset, max_len) in slots.items():
        assert offset % page_bytes == 0
        slot = PayloadSlot(
            valid=1,
            allowWriteRelaxed=1,
            localLkey=make_local_lkey(mr_index=MR_INDEX, key_part=KEY_PART),
            localOffset=offset,
            remoteBase=REMOTE_BASE + slot_id * 0x1_0000_0000,
            maxLen=max_len,
            remoteKey=REMOTE_KEY,
        )
        await configure_slot(tb, model, slot_id, slot)


def specs(
    qpn: int,
    slot_id: int,
    lengths: list[int],
    *,
    debug_seed: int = 0,
) -> list[M0Submission]:
    return [
        M0Submission(
            qpn=qpn,
            slot_id=slot_id,
            length=length,
            debug_seq=(debug_seed + index) & 0xF,
        )
        for index, length in enumerate(lengths)
    ]


@cocotb.test(timeout_time=3_000_000, timeout_unit="ns")
async def header_aware_psn_boundaries_pmtu256_and_512(dut):
    """Messages straddling the first/continuation packet payload limits.

    RoCEv2 first packets carry BTH+RETH (28 B overhead), continuations BTH
    (12 B). At pmtu 256 the limits are 228/244 bytes; at pmtu 512 they are
    484/500. The M0 PSN grant must match the packetizer's own count exactly
    (the glue cross-checks this in RTL), and every frame on the wire must
    carry the resulting PSNs, RETH address and RKEY.
    """
    tb = M0ChainTB(dut)
    await tb.reset()
    model = M0RefModel()
    await bring_up(
        tb,
        model,
        qps={0: 0, 1: 1},
        slots={0: (0x0, 0x2000), 1: (0x1000, 0x2000)},
        psn_starts={0: 0x100, 1: 0x800000},
    )

    messages = []
    for index, (pmtu256_len, pmtu512_len) in enumerate(
        zip(
            [1, 227, 228, 229, 230, 471, 472, 473, 700],
            [483, 484, 485, 984, 985, 1500],
            strict=False,
        )
    ):
        messages.append(M0Submission(0, 0, pmtu256_len, debug_seq=(2 * index) & 0xF))
        messages.append(M0Submission(1, 1, pmtu512_len, debug_seq=(2 * index + 1) & 0xF))
    messages.append(M0Submission(0, 0, 256, debug_seq=0xF))

    await run_m0_chain_scenario(tb, model, messages, CFG)


@cocotb.test(timeout_time=3_000_000, timeout_unit="ns")
async def physical_page_boundaries_and_multi_chunk_messages(dut):
    """Messages spanning physical pages and 4 KiB boundary splits.

    Lengths 4095/4096/4097 force a page-crossing burst pattern; larger
    lengths exercise the per-message request stream through the
    synchronizer and DmaEngine's out-of-order completion reassembly.
    """
    tb = M0ChainTB(dut)
    await tb.reset()
    model = M0RefModel()
    await bring_up(
        tb,
        model,
        qps={0: 2, 1: 3},
        slots={0: (0x0, 0x8000), 1: (0x1000, 0x8000)},
        psn_starts={0: 0x1000, 1: 0xABCDEF},
    )

    messages = specs(0, 0, [1024, 3071, 4095, 4096, 4097, 4196, 8192, 0x7FFF])
    messages += specs(
        1, 1, [1, 1500, 2020, 2021, 3000, 4000], debug_seed=3
    )
    await run_m0_chain_scenario(tb, model, messages, CFG)


@cocotb.test(timeout_time=4_000_000, timeout_unit="ns")
async def interleaved_multi_qp_independent_psn_with_wrap(dut):
    """Four QPs with independent PSN state, interleaved submissions.

    qpn0's initial PSN is near the 24-bit wrap so the granted window crosses
    0xFFFFFF; the frame BTH PSN must still match the packetizer expectation.
    """
    tb = M0ChainTB(dut)
    await tb.reset()
    model = M0RefModel()
    await bring_up(
        tb,
        model,
        qps={0: 0, 1: 1, 2: 2, 3: 4},
        slots={
            0: (0x0, 0x4000),
            1: (0x1000, 0x4000),
            2: (0x2000, 0x4000),
            3: (0x3000, 0x4000),
        },
        psn_starts={0: 0xFFFFFE, 1: 0x123400, 2: 0x0, 3: 0x7FFFFF},
    )

    messages = []
    base = {0: 500, 1: 484, 2: 996, 3: 4068}  # per-qpn message lengths
    order = [0, 3, 1, 2, 0, 1, 2, 3, 0, 2, 1, 3]
    for index, qpn in enumerate(order):
        length = base[qpn] + (index % 3)
        messages.append(
            M0Submission(qpn, qpn, length, debug_seq=(7 * index) & 0xF)
        )
    # qpn0 wrap: 500 bytes at pmtu 256 = 3 PSNs starting at 0xFFFFFE.
    messages.append(M0Submission(0, 0, 500, debug_seq=0))

    await run_m0_chain_scenario(tb, model, messages, CFG)


async def _channel_mask_case(dut, mask: int) -> None:
    tb = M0ChainTB(dut)
    await tb.reset()
    model = M0RefModel()
    await bring_up(
        tb,
        model,
        qps={0: 0},
        slots={0: (0x0, 0x4000)},
        psn_starts={0: 0x555555},
    )
    messages = specs(0, 0, [100, 1100, 2000, 300, 1500, 2500], debug_seed=1)
    await run_m0_chain_scenario(tb, model, messages, CFG, channel_enable=mask)


@cocotb.test(timeout_time=2_000_000, timeout_unit="ns")
async def channel_enable_mask_0101(dut):
    await _channel_mask_case(dut, 0b0101)


@cocotb.test(timeout_time=2_000_000, timeout_unit="ns")
async def channel_enable_mask_1010(dut):
    await _channel_mask_case(dut, 0b1010)


@cocotb.test(timeout_time=5_000_000, timeout_unit="ns")
async def randomized_fixed_seed_m0_to_m3(dut):
    """Deterministic pseudo-random submissions across four QPs/slots."""
    tb = M0ChainTB(dut)
    await tb.reset()
    model = M0RefModel()
    await bring_up(
        tb,
        model,
        qps={0: 0, 1: 1, 2: 2, 3: 3},
        slots={
            0: (0x0, 0x8000),
            1: (0x1000, 0x8000),
            2: (0x2000, 0x8000),
            3: (0x3000, 0x8000),
        },
        psn_starts={0: 0x100, 1: 0x123400, 2: 0xFFFF00, 3: 0x800000},
    )
    rng = random.Random(20260903)
    messages = []
    for index in range(24):
        qpn = rng.randrange(4)
        pmtu = {0: 0, 1: 1, 2: 2, 3: 3}[qpn]
        first_limit = {0: 228, 1: 484, 2: 996, 3: 2020}[pmtu]
        continuation_limit = {0: 244, 1: 500, 2: 1012, 3: 2036}[pmtu]
        if rng.random() < 0.4:
            length = rng.choice(
                [
                    first_limit - 1,
                    first_limit,
                    first_limit + 1,
                    first_limit + continuation_limit,
                    first_limit + continuation_limit + 1,
                ]
            )
        else:
            length = rng.randint(1, 0x8000)
        messages.append(
            M0Submission(qpn, qpn, length, debug_seq=(3 * index) & 0xF)
        )
    await run_m0_chain_scenario(tb, model, messages, CFG)
