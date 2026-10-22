"""End-to-end M0 -> M3 chain, long suite (mkTestM0Chain).

One MR backed by two 2 MiB pages (pageShift 21, PTE indices 0..1); four QPs /
slots, a deterministic pseudo-random mix of ordinary messages plus one full
2 MiB special message (pmtu 2) that spans 2048 DMA chunks and ~2000 frames on
a single wire.  Mirrors the constraints of the short suite: every local
offset is a multiple of 4 KiB, messages are strictly serial per QP, and the
reference expectations come only from the model.
"""

import os
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

PAGE_SHIFT = 21
PAGE = 1 << PAGE_SHIFT
VIRT_BASE = 0x0000_0000_1000_0000  # 2 MiB aligned
REMOTE_BASE = 0x0000_2000_0000_0000
MEM_BASE = 0x0000_0080_0000_0000  # 2 MiB aligned
REMOTE_KEY = 0xAABB_CCDD
KEY_PART = 9
MR_INDEX = 0

# pmtu -> (first-packet limit, continuation limit)
LIMITS = {0: (228, 244), 1: (484, 500), 2: (996, 1012), 3: (2020, 2036), 4: (4068, 4084)}
QPN_PMTU = {0: 0, 1: 1, 2: 2, 3: 4}
SPECIAL_LENGTH = 2 * 1024 * 1024  # exactly 2 MiB == one huge page payload


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


async def _bring_up(tb: M0ChainTB, model: M0RefModel) -> None:
    slots = {
        0: (0x0_0000, 0x1_0000),    # qpn0, pmtu 256
        1: (0x1_0000 * 16, 0x1_0000),  # 0x100000, qpn1, pmtu 512
        2: (0x200000, 0x200000),    # qpn2, pmtu 1024; hosts the 2 MiB message
        3: (0x300000, 0x1_0000),    # qpn3, pmtu 4096
    }
    span = 0x400000  # max(offset + max_len) == 4 MiB, within two 2 MiB pages
    pte_count = 2
    mr = MrEntry(
        valid=1,
        keyPart=KEY_PART,
        allowLocalRead=1,
        baseVa=VIRT_BASE,
        length=span,
        pgtOffset=0,
        pageShift=PAGE_SHIFT,
    )
    await configure_mr(tb, model, MR_INDEX, mr)
    for index in range(pte_count):
        pte = PteEntry(valid=1, physicalPageBase=MEM_BASE + index * PAGE)
        await configure_pte(tb, model, index, pte)
    for qpn in sorted(QPN_PMTU):
        qp = QpEntry(valid=1, pmtu=QPN_PMTU[qpn], initialPsn=0x0)
        await configure_qp(tb, model, qpn, qp)
    for slot_id, (offset, max_len) in slots.items():
        assert offset % 4096 == 0
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


def _random_messages(rng: random.Random, count: int) -> list[M0Submission]:
    """Ordinary messages across qpn 0/1/2/3, capped at 64 KiB each."""
    messages = []
    for index in range(count):
        qpn = rng.choice([0, 0, 0, 1, 1, 1, 2, 3])
        if qpn == 2:
            # Slot 2's window also hosts the 2 MiB special message; keep its
            # random traffic small so offsets + length stay inside page 1.
            length = rng.randint(1, 0x1_0000)
        else:
            if rng.random() < 0.4:
                first, continuation = LIMITS[QPN_PMTU[qpn]]
                length = rng.choice(
                    [first, first + 1, first + continuation, first + continuation + 1]
                )
            else:
                length = rng.randint(1, 0x1_0000)
        messages.append(M0Submission(qpn, qpn, length, debug_seq=(3 * index) & 0xF))
    return messages


@cocotb.test(timeout_time=60_000_000, timeout_unit="ns")
async def huge_page_mix_and_2mib_special_message(dut):
    """Huge pages, big transfer and the full frame stream end to end."""
    tb = M0ChainTB(dut)
    await tb.reset()
    model = M0RefModel()
    await _bring_up(tb, model)

    seed = _int_env("M0CHAIN_SEED", 20260903)
    count = _int_env("M0CHAIN_MESSAGE_COUNT", 48)
    rng = random.Random(seed)
    messages = _random_messages(rng, count)

    special = M0Submission(2, 2, SPECIAL_LENGTH, debug_seq=0xB)
    messages.insert(count // 3, special)

    if "M0CHAIN_SPECIAL_LENGTH" in os.environ:
        # Bisection knob: shrink the special message without touching slot 2's
        # maxLen so a single oversized-message run is easy to build.
        special = M0Submission(
            2, 2, int(os.environ["M0CHAIN_SPECIAL_LENGTH"], 0), debug_seq=0xB
        )
        messages = [special]

    await run_m0_chain_scenario(
        tb, model, messages, CFG, timeout_ns=60_000_000
    )
