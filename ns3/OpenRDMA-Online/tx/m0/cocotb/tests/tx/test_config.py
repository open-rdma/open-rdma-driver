import cocotb
from cocotb.triggers import RisingEdge

from bfm.tx import TB, configure, pipe_get
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
    make_local_lkey,
)


@cocotb.test()
async def action_enables_constructed_before_reset_remain_idle(dut):
    # TB constructs every method adapter before reset.  Icarus must keep each
    # unused top-level EN driven low instead of releasing it back to Z.
    tb = TB(dut)
    await tb.reset()

    enable_names = (
        "EN_slotCfgReqPipeIn_enq",
        "EN_slotCfgRespPipeOut_deq",
        "EN_qpCfgReqPipeIn_enq",
        "EN_qpCfgRespPipeOut_deq",
        "EN_mrCfgReqPipeIn_enq",
        "EN_mrCfgRespPipeOut_deq",
        "EN_pgtCfgReqPipeIn_enq",
        "EN_pgtCfgRespPipeOut_deq",
        "EN_submissionPipeIn_enq",
        "EN_dmaReadReqPipeOut_deq",
    )
    for name in enable_names:
        assert int(getattr(dut, name).value) == 0, f"{name} is not idle"


@cocotb.test()
async def configuration_responses_are_committed_and_backpressured(dut):
    tb = TB(dut)
    await tb.reset()

    slot = PayloadSlot(
        valid=1,
        allowWriteRelaxed=1,
        localLkey=make_local_lkey(mr_index=0, key_part=1),
        localOffset=0,
        remoteBase=0x2000,
        maxLen=4096,
        remoteKey=0x55,
    )
    await tb.slot_cfg_req.enq(SlotCfgReq(index=0, entry=slot).pack())
    while not await tb.slot_cfg_resp.not_empty():
        await RisingEdge(tb.clock)

    held = await tb.slot_cfg_resp.first()
    for _ in range(6):
        assert not await tb.qp_cfg_req.not_full()
        assert await tb.slot_cfg_resp.first() == held
        await RisingEdge(tb.clock)

    response = CfgResp.unpack(await pipe_get(tb.slot_cfg_resp, tb.clock))
    assert int(response.success) == 1

    await configure(
        tb.qp_cfg_req,
        tb.qp_cfg_resp,
        QpCfgReq(index=0, entry=QpEntry(valid=1, pmtu=0, initialPsn=3)).pack(),
        tb.clock,
    )
    await configure(
        tb.mr_cfg_req,
        tb.mr_cfg_resp,
        MrCfgReq(
            index=0,
            entry=MrEntry(
                valid=1,
                keyPart=1,
                allowLocalRead=1,
                baseVa=0x1000,
                length=4096,
                pgtOffset=0,
                pageShift=12,
            ),
        ).pack(),
        tb.clock,
    )
    await configure(
        tb.pgt_cfg_req,
        tb.pgt_cfg_resp,
        PgtCfgReq(
            index=0, entry=PteEntry(valid=1, physicalPageBase=0x8000)
        ).pack(),
        tb.clock,
    )

    # There is no CONFIG/RUN lifecycle gate in the MVP.  Once the previous
    # response is consumed, every configuration service and the data input are
    # available again; the testbench owns the configure-before-submit order.
    assert await tb.slot_cfg_req.not_full()
    assert await tb.qp_cfg_req.not_full()
    assert await tb.mr_cfg_req.not_full()
    assert await tb.pgt_cfg_req.not_full()
    assert await tb.submission.not_full()
