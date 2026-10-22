from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge

from cocotb_bsv import ActionMethod, PipeIn, PipeOut, ValueMethod
from bfm.common import assert_pipe_empty, pipe_get, pipe_put, reset_dut
from model.rx_types import (
    RxCompletion,
    RxDmaRetire,
    RxError,
    RxFlagDmaRequest,
    RxFlagDmaResponse,
    RxOrderedCredit,
    RxOrderedFlagIngress,
    RxQpConfig,
)


RKEY = 0x12345678


def qp_config(qpn: int, initial_psn: int) -> RxQpConfig:
    return RxQpConfig(
        valid=1,
        qpn=qpn,
        initialPsn=initial_psn,
        pmtu=0,
        rkey=RKEY,
        virtualBase=0x1000,
        physicalBase=0x8000,
        regionBytes=0x10000,
        remoteWrite=1,
    )


def retired(
    seq: int,
    qp: int,
    qpn: int,
    psn: int,
    ack: int,
    *,
    ack_valid: int = 1,
    complete: int = 0,
    message_bytes: int = 1,
) -> RxDmaRetire:
    return RxDmaRetire(
        globalSeq=seq,
        qp=qp,
        qpn=qpn,
        psn=psn,
        success=1,
        ackValid=ack_valid,
        ackPsnAfterMerge=ack,
        messageComplete=complete,
        messageLastPsn=ack,
        messageBytes=message_bytes,
    )


class OrderedTB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.configure_epoch = ActionMethod(dut, "configureEpoch", self.clock)
        self.config = [ActionMethod(dut, f"configure{i}", self.clock) for i in range(4)]
        self.start_method = ActionMethod(dut, "start", self.clock)
        self.dma_ack = [ValueMethod(dut, f"dmaAckPsn{i}", self.clock) for i in range(4)]
        self.fault = ValueMethod(dut, "faulted", self.clock)
        self.retire = PipeIn(dut, "retireIn", self.clock)
        self.flags = PipeIn(dut, "orderedFlagIn", self.clock)
        self.flag_requests = PipeOut(dut, "flagRequestOut", self.clock)
        self.flag_responses = PipeIn(dut, "flagResponseIn", self.clock)
        self.credits = PipeOut(dut, "creditOut", self.clock)
        self.completions = PipeOut(dut, "completionOut", self.clock)
        self.errors = PipeOut(dut, "errorOut", self.clock)

    async def initialize(self, configs: list[RxQpConfig], epoch: int = 1) -> None:
        await self.configure_epoch(value=epoch)
        for index, cfg in enumerate(configs):
            await self.config[index](cfg=cfg.pack())
        await self.start_method()

    async def complete_flag(self, request: RxFlagDmaRequest) -> None:
        await pipe_put(
            self.flag_responses,
            self.clock,
            RxFlagDmaResponse(
                orderedSeq=int(request.orderedSeq),
                epoch=int(request.epoch),
                success=1,
            ),
        )


@cocotb.test()
async def test_flag_waits_for_dma_watermark_and_credit_commits_on_deq(dut):
    tb = OrderedTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100)])
    flag = RxOrderedFlagIngress(
        qpn=7,
        barrierPsn=102,
        flagAddr=0x2000,
        rkey=RKEY,
        value=0xA5A55A5A,
    )
    await pipe_put(tb.flags, tb.clock, flag)
    for _ in range(8):
        await RisingEdge(tb.clock)
    assert not await tb.flag_requests.not_empty()

    await pipe_put(tb.retire, tb.clock, retired(0, 0, 7, 100, 100))
    await pipe_put(tb.retire, tb.clock, retired(1, 0, 7, 101, 101))
    for _ in range(8):
        await RisingEdge(tb.clock)
    assert int(await tb.dma_ack[0]()) == 101
    assert not await tb.flag_requests.not_empty()

    await pipe_put(
        tb.retire,
        tb.clock,
        retired(2, 0, 7, 102, 102, complete=1, message_bytes=500),
    )
    completion = await pipe_get(tb.completions, tb.clock, RxCompletion)
    assert (int(completion.qpn), int(completion.lastPsn), int(completion.bytes)) == (
        7,
        102,
        500,
    )
    while not await tb.flag_requests.not_empty():
        await RisingEdge(tb.clock)
    assert not await tb.credits.not_empty(), "credit appeared before Flag DMA acceptance"
    request = await pipe_get(tb.flag_requests, tb.clock, RxFlagDmaRequest)
    assert int(request.addr) == 0x9000
    assert int(request.value) == 0xA5A55A5A
    assert int(request.barrierPsn) == 102
    credit = await pipe_get(tb.credits, tb.clock, RxOrderedCredit)
    assert (int(credit.qpn), int(credit.orderedGrantLimit)) == (7, 5)
    await tb.complete_flag(request)
    for _ in range(4):
        await RisingEdge(tb.clock)
    assert int(await tb.fault()) == 0
    await assert_pipe_empty(tb.errors, tb.clock)


@cocotb.test()
async def test_per_qp_fifo_order_and_cross_qp_round_robin(dut):
    tb = OrderedTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100), qp_config(8, 200)])
    flags = [
        RxOrderedFlagIngress(
            qpn=7, barrierPsn=99, flagAddr=0x2000, rkey=RKEY, value=0
        ),
        RxOrderedFlagIngress(
            qpn=7, barrierPsn=99, flagAddr=0x2004, rkey=RKEY, value=1
        ),
        RxOrderedFlagIngress(
            qpn=8, barrierPsn=199, flagAddr=0x3000, rkey=RKEY, value=2
        ),
    ]
    for flag in flags:
        await pipe_put(tb.flags, tb.clock, flag)

    requests = []
    credits = []
    for _ in range(3):
        request = await pipe_get(tb.flag_requests, tb.clock, RxFlagDmaRequest)
        requests.append(request)
        credits.append(await pipe_get(tb.credits, tb.clock, RxOrderedCredit))
        await tb.complete_flag(request)
    assert [int(request.qpn) for request in requests] == [7, 8, 7]
    assert [int(request.value) for request in requests] == [0, 2, 1]
    assert [int(request.orderedSeq) for request in requests] == [0, 2, 1]
    assert [int(credit.orderedGrantLimit) for credit in credits] == [5, 5, 6]


@cocotb.test()
async def test_psn_wrap_barrier(dut):
    tb = OrderedTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(9, 0xFFFFFE)])
    await pipe_put(
        tb.flags,
        tb.clock,
        RxOrderedFlagIngress(
            qpn=9, barrierPsn=0, flagAddr=0x2400, rkey=RKEY, value=0x55
        ),
    )
    for seq, psn in enumerate((0xFFFFFE, 0xFFFFFF)):
        await pipe_put(tb.retire, tb.clock, retired(seq, 0, 9, psn, psn))
    for _ in range(8):
        await RisingEdge(tb.clock)
    assert not await tb.flag_requests.not_empty()
    await pipe_put(tb.retire, tb.clock, retired(2, 0, 9, 0, 0))
    request = await pipe_get(tb.flag_requests, tb.clock, RxFlagDmaRequest)
    assert int(request.barrierPsn) == 0


@cocotb.test()
async def test_queue_overflow_is_fail_stop(dut):
    tb = OrderedTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100)])
    for index in range(5):
        await pipe_put(
            tb.flags,
            tb.clock,
            RxOrderedFlagIngress(
                qpn=7,
                barrierPsn=150,
                flagAddr=0x2000 + index * 4,
                rkey=RKEY,
                value=index,
            ),
        )
    error = await pipe_get(tb.errors, tb.clock, RxError)
    assert int(error.code) == 11
    assert int(await tb.fault()) == 1


@cocotb.test()
async def test_selected_flag_counts_toward_four_entry_capacity(dut):
    tb = OrderedTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100)])
    for index in range(5):
        await pipe_put(
            tb.flags,
            tb.clock,
            RxOrderedFlagIngress(
                qpn=7,
                barrierPsn=99,
                flagAddr=0x2400 + index * 4,
                rkey=RKEY,
                value=index,
            ),
        )
        if index == 0:
            while not await tb.flag_requests.not_empty():
                await RisingEdge(tb.clock)
    error = await pipe_get(tb.errors, tb.clock, RxError)
    assert int(error.code) == 11
    assert int(await tb.fault()) == 1


@cocotb.test()
async def test_bad_retire_is_fail_stop(dut):
    tb = OrderedTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100)])
    await pipe_put(tb.retire, tb.clock, retired(1, 0, 7, 100, 100))
    error = await pipe_get(tb.errors, tb.clock, RxError)
    assert int(error.code) == 13
    assert int(await tb.fault()) == 1


@cocotb.test()
async def test_backward_or_ambiguous_barrier_is_rejected(dut):
    tb = OrderedTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100)])
    await pipe_put(
        tb.flags,
        tb.clock,
        RxOrderedFlagIngress(
            qpn=7, barrierPsn=110, flagAddr=0x2000, rkey=RKEY, value=1
        ),
    )
    await pipe_put(
        tb.flags,
        tb.clock,
        RxOrderedFlagIngress(
            qpn=7, barrierPsn=105, flagAddr=0x2004, rkey=RKEY, value=2
        ),
    )
    error = await pipe_get(tb.errors, tb.clock, RxError)
    assert int(error.code) == 12
    assert int(await tb.fault()) == 1


@cocotb.test()
async def test_delayed_already_satisfied_barrier_is_accepted(dut):
    tb = OrderedTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100)])
    await pipe_put(tb.retire, tb.clock, retired(0, 0, 7, 200, 200))
    await pipe_put(
        tb.flags,
        tb.clock,
        RxOrderedFlagIngress(
            qpn=7,
            barrierPsn=100,
            flagAddr=0x2000,
            rkey=RKEY,
            value=1,
        ),
    )
    request = await pipe_get(tb.flag_requests, tb.clock, RxFlagDmaRequest)
    assert int(request.barrierPsn) == 100
    await assert_pipe_empty(tb.errors, tb.clock)


@cocotb.test()
async def test_stale_flag_response_epoch_reports_original_identity(dut):
    tb = OrderedTB(dut)
    await reset_dut(dut)
    await tb.initialize([qp_config(7, 100)], epoch=2)
    await pipe_put(
        tb.flags,
        tb.clock,
        RxOrderedFlagIngress(
            qpn=7, barrierPsn=99, flagAddr=0x2000, rkey=RKEY, value=1
        ),
    )
    request = await pipe_get(tb.flag_requests, tb.clock, RxFlagDmaRequest)
    assert int(request.epoch) == 2
    await pipe_put(
        tb.flag_responses,
        tb.clock,
        RxFlagDmaResponse(
            orderedSeq=int(request.orderedSeq), epoch=1, success=1
        ),
    )
    error = await pipe_get(tb.errors, tb.clock, RxError)
    assert (int(error.qpn), int(error.psn), int(error.code)) == (7, 99, 15)
    assert int(await tb.fault()) == 1
