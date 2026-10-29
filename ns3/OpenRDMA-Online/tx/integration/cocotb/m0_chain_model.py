"""M0 (TxMvp) side wire mirrors and reference model for the m0->m3 chain.

The mvp cocotb package (mvp/cocotb) is unreachable from this directory: every
repo cocotb tree is self-contained (its own model/ and bfm/ package roots), so
the authoritative mirror in mvp/cocotb/model/tx.py is re-cast here. Keep the
two in sync. Field order of every Struct is the BSV declaration order of the
mirrored typedef (TxMvpTypes.bsv), exactly as the mvp suite uses it.

Submission reference model: mirrors TxMvp's Slot/MR/QP/PGT join pipeline and
the physical DMA burst splitter (mvp/src/TxMvp.bsv, TxTables.bsv), including
the header-aware PSN grant (txPacketCount == phasea packetCountForMessage).
"""

from __future__ import annotations

from collections import OrderedDict

from cocotb_bsv import Struct
from model.common import (
    Bit1,
    Bit2,
    Bit3,
    Bit4,
    Bit6,
    Bit7,
    Bit13,
    Bit16,
    Bit17,
    Bit18,
    Bit19,
    Bit24,
    Bit25,
    Bit32,
    Bit64,
    MAX_U64,
)


class PayloadSlot(Struct):
    _members_def = OrderedDict(
        valid=Bit1,
        allowWriteRelaxed=Bit1,
        localLkey=Bit32,
        localOffset=Bit64,
        remoteBase=Bit64,
        maxLen=Bit25,
        remoteKey=Bit32,
    )


class QpEntry(Struct):
    _members_def = OrderedDict(valid=Bit1, pmtu=Bit3, initialPsn=Bit24)


class MrEntry(Struct):
    _members_def = OrderedDict(
        valid=Bit1,
        keyPart=Bit19,
        allowLocalRead=Bit1,
        baseVa=Bit64,
        length=Bit64,
        pgtOffset=Bit7,
        pageShift=Bit6,
    )


class PteEntry(Struct):
    _members_def = OrderedDict(valid=Bit1, physicalPageBase=Bit64)


class SlotCfgReq(Struct):
    _members_def = OrderedDict(index=Bit4, entry=PayloadSlot)


class QpCfgReq(Struct):
    _members_def = OrderedDict(index=Bit2, entry=QpEntry)


class MrCfgReq(Struct):
    _members_def = OrderedDict(index=Bit13, entry=MrEntry)


class PgtCfgReq(Struct):
    _members_def = OrderedDict(index=Bit7, entry=PteEntry)


class TxCfgResp(Struct):
    _members_def = OrderedDict(success=Bit1)


class DecodedGdrpSubmission(Struct):
    _members_def = OrderedDict(qpn=Bit24, token=Bit64)


class TxDmaReadRequest(Struct):
    _members_def = OrderedDict(
        requestId=Bit16,
        qpn=Bit24,
        slotId=Bit18,
        debugSeq=Bit4,
        pmtu=Bit3,
        startPsn=Bit24,
        psnCount=Bit17,
        messageLength=Bit25,
        dmaSeq=Bit16,
        physicalAddr=Bit64,
        remoteAddr=Bit64,
        len=Bit25,
        remoteKey=Bit32,
        isFirst=Bit1,
        isLast=Bit1,
    )


PMTU_BYTES = {0: 256, 1: 512, 2: 1024, 3: 2048, 4: 4096}
PAGE_BYTES = {12: 4096, 21: 2 * 1024 * 1024}
MAX_DMA_BURST = 1024
PHYSICAL_BOUNDARY = 4096
MAX_MESSAGE = 16 * 1024 * 1024

# Token layout mirrors mvp model/tx.py make_token: flags[7:0], length[39:8]
# (0 means 16 MiB), slotId[57:40], opcode[59:58], debugSeq[63:60].


def make_local_lkey(*, mr_index: int, key_part: int) -> int:
    assert 0 <= mr_index < (1 << 13)
    assert 0 <= key_part < (1 << 19)
    return (key_part << 13) | mr_index


def make_token(*, slot_id: int, length: int, debug_seq: int = 0) -> int:
    assert 1 <= length <= MAX_MESSAGE
    length_field = 0 if length == MAX_MESSAGE else length
    return (
        0
        | ((length_field & 0xFFFFFFFF) << 8)
        | ((slot_id & 0x3FFFF) << 40)
        | ((debug_seq & 0xF) << 60)
    )


def packet_count(length: int, pmtu: int) -> int:
    """Mirror of BSV txPacketCount / phasea packetCountForMessage."""
    pmtu_bytes = PMTU_BYTES[pmtu]
    first_limit = pmtu_bytes - 28
    continuation_limit = pmtu_bytes - 12
    if length <= first_limit:
        return 1
    return 1 + (length - first_limit + continuation_limit - 1) // continuation_limit


def _value(struct: Struct, field: str) -> int:
    return int(getattr(struct, field))


class M0RefModel:
    """Reference behavior for valid M0 submissions; invalid input is a test bug."""

    def __init__(self) -> None:
        self.slots: dict[int, PayloadSlot] = {}
        self.qps: dict[int, QpEntry] = {}
        self.mrs: dict[int, MrEntry] = {}
        self.ptes: dict[int, PteEntry] = {}
        self.next_psn: dict[int, int] = {}
        self.next_request_id = 0

    def configure_slot(self, index: int, entry: PayloadSlot) -> None:
        assert 0 <= index < 16
        self.slots[index] = entry

    def configure_qp(self, index: int, entry: QpEntry) -> None:
        assert 0 <= index < 4
        self.qps[index] = entry
        self.next_psn[index] = _value(entry, "initialPsn")

    def configure_mr(self, index: int, entry: MrEntry) -> None:
        assert 0 <= index < 16
        self.mrs[index] = entry

    def configure_pte(self, index: int, entry: PteEntry) -> None:
        assert 0 <= index < 128
        self.ptes[index] = entry

    def submit(self, qpn: int, token: int) -> list[TxDmaReadRequest]:
        length_field = (token >> 8) & 0xFFFFFFFF
        slot_id = (token >> 40) & 0x3FFFF
        debug_seq = (token >> 60) & 0xF
        length = MAX_MESSAGE if length_field == 0 else length_field

        assert qpn < 4
        assert token & 0xFF == 0
        assert (token >> 58) & 0x3 == 0
        assert slot_id < 16
        slot = self.slots[slot_id]
        assert _value(slot, "valid") == 1
        assert _value(slot, "allowWriteRelaxed") == 1
        assert 0 < length <= MAX_MESSAGE
        assert length <= _value(slot, "maxLen")

        local_offset = _value(slot, "localOffset")
        remote_base = _value(slot, "remoteBase")
        assert local_offset + length - 1 <= MAX_U64
        assert remote_base + length - 1 <= MAX_U64

        local_lkey = _value(slot, "localLkey")
        mr_index = local_lkey & 0x1FFF
        key_part = local_lkey >> 13
        assert mr_index < 16
        mr = self.mrs[mr_index]
        assert _value(mr, "valid") == 1
        assert _value(mr, "keyPart") == key_part
        assert _value(mr, "allowLocalRead") == 1
        assert local_offset + length <= _value(mr, "length")
        local_va = _value(mr, "baseVa") + local_offset
        assert local_va <= MAX_U64
        page_shift = _value(mr, "pageShift")
        assert page_shift in PAGE_BYTES

        qp = self.qps[qpn]
        assert _value(qp, "valid") == 1
        pmtu = _value(qp, "pmtu")
        assert pmtu in PMTU_BYTES

        psn_count = packet_count(length, pmtu)
        start_psn = self.next_psn[qpn]
        self.next_psn[qpn] = (start_psn + psn_count) & 0xFFFFFF
        request_id = self.next_request_id
        self.next_request_id = (self.next_request_id + 1) & 0xFFFF

        outputs: list[TxDmaReadRequest] = []
        message_completed = 0
        page_bytes = PAGE_BYTES[page_shift]
        while message_completed < length:
            current_va = local_va + message_completed
            page_offset = current_va & (page_bytes - 1)
            page_len = min(length - message_completed, page_bytes - page_offset)
            page_index = (current_va >> page_shift) - (
                _value(mr, "baseVa") >> page_shift
            )
            pte_index = _value(mr, "pgtOffset") + page_index
            assert pte_index < 128
            pte = self.ptes[pte_index]
            assert _value(pte, "valid") == 1
            physical_page = _value(pte, "physicalPageBase")
            assert physical_page & (page_bytes - 1) == 0
            page_physical = physical_page + page_offset
            assert page_physical <= MAX_U64

            page_completed = 0
            while page_completed < page_len:
                physical_addr = page_physical + page_completed
                boundary_len = PHYSICAL_BOUNDARY - (
                    physical_addr & (PHYSICAL_BOUNDARY - 1)
                )
                burst_len = min(
                    page_len - page_completed, MAX_DMA_BURST, boundary_len
                )
                completed = message_completed + page_completed
                outputs.append(
                    TxDmaReadRequest(
                        requestId=request_id,
                        qpn=qpn,
                        slotId=slot_id,
                        debugSeq=debug_seq,
                        pmtu=pmtu,
                        startPsn=start_psn,
                        psnCount=psn_count,
                        messageLength=length,
                        dmaSeq=len(outputs),
                        physicalAddr=physical_addr,
                        remoteAddr=remote_base + completed,
                        len=burst_len,
                        remoteKey=_value(slot, "remoteKey"),
                        isFirst=int(completed == 0),
                        isLast=int(completed + burst_len == length),
                    )
                )
                page_completed += burst_len
            message_completed += page_len
        return outputs
