"""TX MVP DUT access, reset, configuration, and progress helpers."""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, with_timeout

from cocotb_bsv import PipeIn, PipeOut
from model.tx import (
    CfgResp,
    MrCfgReq,
    MrEntry,
    PayloadSlot,
    PgtCfgReq,
    PteEntry,
    QpCfgReq,
    QpEntry,
    SlotCfgReq,
    TxReferenceModel,
    make_local_lkey,
)


class TB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.resetn = dut.RST_N

        self.slot_cfg_req = PipeIn(dut, "slotCfgReqPipeIn", self.clock)
        self.slot_cfg_resp = PipeOut(dut, "slotCfgRespPipeOut", self.clock)
        self.qp_cfg_req = PipeIn(dut, "qpCfgReqPipeIn", self.clock)
        self.qp_cfg_resp = PipeOut(dut, "qpCfgRespPipeOut", self.clock)
        self.mr_cfg_req = PipeIn(dut, "mrCfgReqPipeIn", self.clock)
        self.mr_cfg_resp = PipeOut(dut, "mrCfgRespPipeOut", self.clock)
        self.pgt_cfg_req = PipeIn(dut, "pgtCfgReqPipeIn", self.clock)
        self.pgt_cfg_resp = PipeOut(dut, "pgtCfgRespPipeOut", self.clock)

        self.submission = PipeIn(dut, "submissionPipeIn", self.clock)
        self.dma = PipeOut(dut, "dmaReadReqPipeOut", self.clock)

    async def reset(self) -> None:
        cocotb.start_soon(Clock(self.clock, 2, unit="ns").start())
        self.resetn.value = 0
        for _ in range(4):
            await RisingEdge(self.clock)
        self.resetn.value = 1
        for _ in range(4):
            await RisingEdge(self.clock)


async def pipe_get(pipe: PipeOut, clock, *, cycles: int = 2000) -> int:
    async def wait_and_get() -> int:
        while not await pipe.not_empty():
            await RisingEdge(clock)
        value = await pipe.first()
        await pipe.deq()
        return value

    return await with_timeout(wait_and_get(), cycles * 2, "ns")


async def configure(req: PipeIn, resp: PipeOut, packed: int, clock) -> None:
    await req.enq(packed)
    response = CfgResp.unpack(await pipe_get(resp, clock))
    assert int(response.success) == 1


async def configure_basic(
    tb: TB,
    model: TxReferenceModel,
    *,
    local_offset: int = 0,
    max_len: int = 8192,
    mr_length: int = 8192,
    page_shift: int = 12,
    initial_psn: int = 0xFFFF00,
    key_part: int = 7,
    mr_index: int = 0,
    pmtu: int = 0,
) -> dict[str, int]:
    local_base = 0x0000_0000_1000_0000
    remote_base = 0x0000_2000_0000_0000
    remote_key = 0x5566_7788
    local_lkey = make_local_lkey(mr_index=mr_index, key_part=key_part)
    first_page = 0x0000_0000_8000_0000
    page_bytes = 1 << page_shift
    page_count = (mr_length + page_bytes - 1) // page_bytes

    slot = PayloadSlot(
        valid=1,
        allowWriteRelaxed=1,
        localLkey=local_lkey,
        localOffset=local_offset,
        remoteBase=remote_base,
        maxLen=max_len,
        remoteKey=remote_key,
    )
    qp = QpEntry(valid=1, pmtu=pmtu, initialPsn=initial_psn)
    mr = MrEntry(
        valid=1,
        keyPart=key_part,
        allowLocalRead=1,
        baseVa=local_base,
        length=mr_length,
        pgtOffset=0,
        pageShift=page_shift,
    )

    await configure(
        tb.slot_cfg_req,
        tb.slot_cfg_resp,
        SlotCfgReq(index=0, entry=slot).pack(),
        tb.clock,
    )
    await configure(
        tb.qp_cfg_req,
        tb.qp_cfg_resp,
        QpCfgReq(index=0, entry=qp).pack(),
        tb.clock,
    )
    await configure(
        tb.mr_cfg_req,
        tb.mr_cfg_resp,
        MrCfgReq(index=mr_index, entry=mr).pack(),
        tb.clock,
    )

    for index in range(page_count):
        pte = PteEntry(
            valid=1,
            physicalPageBase=first_page + index * page_bytes,
        )
        await configure(
            tb.pgt_cfg_req,
            tb.pgt_cfg_resp,
            PgtCfgReq(index=index, entry=pte).pack(),
            tb.clock,
        )
        model.configure_pte(index, pte)

    model.configure_slot(0, slot)
    model.configure_qp(0, qp)
    model.configure_mr(mr_index, mr)

    return {
        "local_base": local_base,
        "remote_base": remote_base,
        "remote_key": remote_key,
        "key_part": key_part,
        "local_lkey": local_lkey,
        "first_page": first_page,
        "second_page": first_page + page_bytes,
    }
