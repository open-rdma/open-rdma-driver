from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge, with_timeout

from cocotb_bsv import ActionMethod, PipeIn, PipeOut, ValueMethod
from bfm.common import assert_pipe_empty, pipe_get, pipe_put, reset_dut
from model.rx_types import (
    RxCompletion,
    RxDmaCompletion,
    RxDmaPayloadBeat,
    RxDmaWriteRequest,
    RxEcnEvent,
    RxError,
    RxFeedback,
    RxFlagDmaRequest,
    RxFlagDmaResponse,
    RxNetworkConfig,
    RxOrderedCredit,
    RxOrderedFlagIngress,
    RxQpConfig,
    RxReceiveProgress,
)
from model.wire import (
    NetworkConfig,
    OP_WRITE_FIRST,
    OP_WRITE_LAST,
    OP_WRITE_MIDDLE,
    OP_WRITE_ONLY,
    build_frame,
    wire_beats,
)


CFG = NetworkConfig()
RKEY = 0x12345678


class RxTopTB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.configure_epoch = ActionMethod(dut, "configureEpoch", self.clock)
        self.configure_network = ActionMethod(dut, "configureNetwork", self.clock)
        self.configure_qp = [ActionMethod(dut, f"configure{i}", self.clock) for i in range(4)]
        self.start_method = ActionMethod(dut, "start", self.clock)
        self.progress = ValueMethod(dut, "progress0", self.clock)
        self.dma_ack = ValueMethod(dut, "dmaAckPsn0", self.clock)
        self.next_seq = ValueMethod(dut, "nextGlobalSeq", self.clock)
        self.retire_head = ValueMethod(dut, "retireHead", self.clock)
        self.fault = ValueMethod(dut, "faulted", self.clock)
        self.inputs = [PipeIn(dut, f"lane{i}In", self.clock) for i in range(4)]
        self.requests = [PipeOut(dut, f"lane{i}DmaRequest", self.clock) for i in range(4)]
        self.payloads = [PipeOut(dut, f"lane{i}DmaPayload", self.clock) for i in range(4)]
        self.completions = [PipeIn(dut, f"lane{i}DmaCompletion", self.clock) for i in range(4)]
        self.ordered_flags = PipeIn(dut, "orderedFlagIn", self.clock)
        self.flag_requests = PipeOut(dut, "flagDmaRequest", self.clock)
        self.flag_responses = PipeIn(dut, "flagDmaResponse", self.clock)
        self.credits = PipeOut(dut, "orderedCredit", self.clock)
        self.cqes = PipeOut(dut, "completion", self.clock)
        self.feedback = PipeOut(dut, "feedback", self.clock)
        self.ecn = PipeOut(dut, "ecn", self.clock)
        self.tx_feedback = PipeOut(dut, "txFeedback", self.clock)
        self.errors = PipeOut(dut, "errors", self.clock)

    async def initialize(self, epoch: int = 1) -> None:
        await self.configure_epoch(value=epoch)
        network = RxNetworkConfig(
            localMac=CFG.local_mac,
            localIp=CFG.local_ip,
            peerMac=CFG.peer_mac,
            peerIp=CFG.peer_ip,
        )
        qp = RxQpConfig(
            valid=1,
            qpn=7,
            initialPsn=0xFFFFFE,
            pmtu=0,
            rkey=RKEY,
            virtualBase=0x1000,
            physicalBase=0x8000,
            regionBytes=0x10000,
            remoteWrite=1,
        )
        await self.configure_network(cfg=network.pack())
        await self.configure_qp[0](cfg=qp.pack())
        await self.start_method()

    async def send_frame(self, lane: int, frame: bytes) -> None:
        for beat in wire_beats(frame):
            await pipe_put(self.inputs[lane], self.clock, beat)

    async def collect_dma_packet(self, lane: int):
        request = await pipe_get(self.requests[lane], self.clock, RxDmaWriteRequest)
        data = bytearray()
        while len(data) < int(request.bytes):
            beat = await pipe_get(self.payloads[lane], self.clock, RxDmaPayloadBeat)
            assert int(beat.globalSeq) == int(request.globalSeq)
            assert int(beat.epoch) == int(request.epoch)
            valid = int(beat.be).bit_count()
            assert int(beat.be) == (1 << valid) - 1
            data += int(beat.data).to_bytes(32, "little")[:valid]
            if int(beat.eop):
                break
        assert len(data) == int(request.bytes)
        return request, bytes(data)


@cocotb.test()
async def test_start_requires_epoch_network_and_valid_qp(dut):
    tb = RxTopTB(dut)
    await reset_dut(dut)
    assert int(tb.start_method.ready.value) == 0
    await tb.configure_epoch(value=1)
    assert int(tb.start_method.ready.value) == 0
    await tb.configure_network(
        cfg=RxNetworkConfig(
            localMac=CFG.local_mac,
            localIp=CFG.local_ip,
            peerMac=CFG.peer_mac,
            peerIp=CFG.peer_ip,
        ).pack()
    )
    assert int(tb.start_method.ready.value) == 0
    await tb.configure_qp[0](
        cfg=RxQpConfig(
            valid=1,
            qpn=7,
            initialPsn=0,
            pmtu=0,
            rkey=RKEY,
            virtualBase=0x1000,
            physicalBase=0x8000,
            regionBytes=0x1000,
            remoteWrite=1,
        ).pack()
    )
    assert int(tb.start_method.ready.value) == 1
    await tb.start_method()


@cocotb.test()
async def test_duplicate_qpn_prevents_start_until_reconfigured(dut):
    tb = RxTopTB(dut)
    await reset_dut(dut)
    await tb.configure_epoch(value=1)
    await tb.configure_network(
        cfg=RxNetworkConfig(
            localMac=CFG.local_mac, localIp=CFG.local_ip,
            peerMac=CFG.peer_mac, peerIp=CFG.peer_ip,
        ).pack()
    )
    common = dict(valid=1, initialPsn=100, pmtu=0, rkey=RKEY,
                  virtualBase=0x1000, physicalBase=0x8000,
                  regionBytes=0x1000, remoteWrite=1)
    await tb.configure_qp[0](cfg=RxQpConfig(qpn=7, **common).pack())
    await tb.configure_qp[1](cfg=RxQpConfig(qpn=7, **common).pack())
    assert int(tb.start_method.ready.value) == 0
    await tb.configure_qp[1](cfg=RxQpConfig(qpn=8, **common).pack())
    assert int(tb.start_method.ready.value) == 1
    await tb.start_method()


@cocotb.test()
async def test_four_port_reorder_dual_watermark_and_ordered_flag(dut):
    tb = RxTopTB(dut)
    await reset_dut(dut)
    await tb.initialize()
    payload = bytes((index * 17 + 3) & 0xFF for index in range(500))
    frames = {
        0: build_frame(
            opcode=OP_WRITE_FIRST,
            qpn=7,
            psn=0xFFFFFE,
            payload=payload[:228],
            remote_addr=0x2000,
            rkey=RKEY,
            message_bytes=500,
        ),
        1: build_frame(
            opcode=OP_WRITE_MIDDLE,
            qpn=7,
            psn=0xFFFFFF,
            payload=payload[228:472],
            ecn=3,
        ),
        2: build_frame(
            opcode=OP_WRITE_LAST,
            qpn=7,
            psn=0,
            payload=payload[472:],
        ),
    }

    await tb.send_frame(2, frames[2])
    await tb.send_frame(1, frames[1])
    for _ in range(24):
        await RisingEdge(tb.clock)
    assert not await tb.feedback.not_empty()
    for output in tb.requests:
        assert not await output.not_empty()

    workers = [
        cocotb.start_soon(tb.collect_dma_packet(lane)) for lane in range(3)
    ]
    await tb.send_frame(0, frames[0])
    feedback = [await pipe_get(tb.feedback, tb.clock, RxFeedback) for _ in range(3)]
    assert int(feedback[-1].ackPsn) == 0
    progress = RxReceiveProgress.unpack(await tb.progress())
    assert int(progress.ackPsn) == 0
    assert int(progress.receiveBitmap) == 0
    assert int(await tb.dma_ack()) == 0xFFFFFD
    assert int(await tb.retire_head()) == 0

    ecn = await pipe_get(tb.ecn, tb.clock, RxEcnEvent)
    assert (int(ecn.lane), int(ecn.qpn), int(ecn.psn), int(ecn.ecn)) == (
        1,
        7,
        0xFFFFFF,
        3,
    )

    await pipe_put(
        tb.ordered_flags,
        tb.clock,
        RxOrderedFlagIngress(
            qpn=7,
            barrierPsn=0,
            flagAddr=0x2800,
            rkey=RKEY,
            value=0xC001CAFE,
        ),
    )
    for _ in range(12):
        await RisingEdge(tb.clock)
    assert not await tb.flag_requests.not_empty()

    dma_packets = [await worker for worker in workers]
    by_seq = {int(request.globalSeq): (request, data) for request, data in dma_packets}
    assert sorted(by_seq) == [0, 1, 2]
    gpu_memory: dict[int, int] = {}
    for request, data in by_seq.values():
        for offset, value in enumerate(data):
            address = int(request.addr) + offset
            assert address not in gpu_memory, f"duplicate DMA byte at {address:#x}"
            gpu_memory[address] = value
    rebuilt = bytes(gpu_memory[0x9000 + index] for index in range(500))
    assert rebuilt == payload

    for seq in (2, 1):
        request, _ = by_seq[seq]
        lane = int(request.lane)
        await pipe_put(
            tb.completions[lane],
            tb.clock,
            RxDmaCompletion(globalSeq=seq, epoch=1, lane=lane, success=1),
        )
    for _ in range(12):
        await RisingEdge(tb.clock)
    assert int(await tb.retire_head()) == 0
    assert int(await tb.dma_ack()) == 0xFFFFFD
    assert not await tb.flag_requests.not_empty()

    request0, _ = by_seq[0]
    lane0 = int(request0.lane)
    await pipe_put(
        tb.completions[lane0],
        tb.clock,
        RxDmaCompletion(globalSeq=0, epoch=1, lane=lane0, success=1),
    )
    cqe = await pipe_get(tb.cqes, tb.clock, RxCompletion)
    assert (int(cqe.qpn), int(cqe.lastPsn), int(cqe.bytes), int(cqe.success)) == (
        7,
        0,
        500,
        1,
    )
    flag_request = await pipe_get(tb.flag_requests, tb.clock, RxFlagDmaRequest)
    assert (int(flag_request.addr), int(flag_request.value)) == (0x9800, 0xC001CAFE)
    credit = await pipe_get(tb.credits, tb.clock, RxOrderedCredit)
    assert (int(credit.qpn), int(credit.orderedGrantLimit)) == (7, 5)
    await pipe_put(
        tb.flag_responses,
        tb.clock,
        RxFlagDmaResponse(
            orderedSeq=int(flag_request.orderedSeq),
            epoch=int(flag_request.epoch),
            success=1,
        ),
    )
    for _ in range(8):
        await RisingEdge(tb.clock)
    assert int(await tb.retire_head()) == 3
    assert int(await tb.dma_ack()) == 0
    assert int(await tb.fault()) == 0

    # An old duplicate is acknowledged from receive state but never DMA placed again.
    await tb.send_frame(3, frames[0])
    for _ in range(12):
        await RisingEdge(tb.clock)
    assert int(await tb.fault()) == 0
    assert not await tb.requests[3].not_empty()
    await assert_pipe_empty(tb.errors, tb.clock)


@cocotb.test()
async def test_malformed_ftile_beat_reports_error_and_resynchronizes(dut):
    tb = RxTopTB(dut)
    await reset_dut(dut)
    await tb.initialize()
    from model.rx_types import RxWireBeat

    await pipe_put(
        tb.inputs[3], tb.clock, RxWireBeat(data=0x55, be=0x5, sof=1, eof=1)
    )
    error = await pipe_get(tb.errors, tb.clock, RxError)
    assert int(error.code) == 0

    frame = build_frame(
        opcode=OP_WRITE_FIRST,
        qpn=7,
        psn=0xFFFFFE,
        payload=b"x" * 228,
        remote_addr=0x2000,
        rkey=RKEY,
        message_bytes=229,
    )
    last = build_frame(
        opcode=OP_WRITE_LAST,
        qpn=7,
        psn=0xFFFFFF,
        payload=b"y",
    )
    worker3 = cocotb.start_soon(tb.collect_dma_packet(3))
    await tb.send_frame(3, frame)
    request, data = await worker3
    worker2 = cocotb.start_soon(tb.collect_dma_packet(2))
    await tb.send_frame(2, last)
    await worker2
    assert data == b"x" * 228
    assert int(request.globalSeq) == 0


@cocotb.test()
async def test_global_fault_blocks_ordered_flag_ingress_and_publication(dut):
    tb = RxTopTB(dut)
    await reset_dut(dut)
    await tb.initialize()
    bad = build_frame(
        opcode=OP_WRITE_ONLY,
        qpn=7,
        psn=0xFFFFFE,
        payload=b"bad",
        remote_addr=0x2000,
        rkey=0xDEADBEEF,
        message_bytes=3,
    )
    await tb.send_frame(0, bad)
    error = await pipe_get(tb.errors, tb.clock, RxError)
    assert int(error.code) == 5
    assert int(await tb.fault()) == 1
    for _ in range(8):
        await RisingEdge(tb.clock)
    assert not await tb.ordered_flags.not_full()
    assert not await tb.flag_requests.not_empty()
