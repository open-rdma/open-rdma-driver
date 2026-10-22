from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge, with_timeout

from cocotb_bsv import ActionMethod, PipeIn, PipeOut
from bfm.common import assert_pipe_empty, pipe_get, pipe_put, reset_dut
from model.rx_types import (
    CfgResp,
    EthPacket,
    MacIpConfig,
    MessageContext,
    MessagePayloadBeat,
    MrcTxCfgReq,
    RxCompletion,
    RxDmaCompletion,
    RxDmaPayloadBeat,
    RxDmaWriteRequest,
    RxError,
    RxFeedback,
    RxNetworkConfig,
    RxQpConfig,
    RxWireBeat,
)


RKEY = 0x12345678


class LoopbackTB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.tx_context = PipeIn(dut, "txContextIn", self.clock)
        self.tx_payload = PipeIn(dut, "txPayloadIn", self.clock)
        self.tx_config = PipeIn(dut, "txConfigIn", self.clock)
        self.tx_config_out = PipeOut(dut, "txConfigOut", self.clock)
        self.tx_channels = [PipeOut(dut, f"txChannel{i}", self.clock) for i in range(4)]
        self.tx_commit = PipeOut(dut, "txCommit", self.clock)
        self.configure_epoch = ActionMethod(dut, "configureEpoch", self.clock)
        self.configure_network = ActionMethod(dut, "configureNetwork", self.clock)
        self.configure_qp = ActionMethod(dut, "configureQp0", self.clock)
        self.start_rx = ActionMethod(dut, "startRx", self.clock)
        self.rx_lanes = [PipeIn(dut, f"rxLane{i}", self.clock) for i in range(4)]
        self.requests = [PipeOut(dut, f"dmaRequest{i}", self.clock) for i in range(4)]
        self.payloads = [PipeOut(dut, f"dmaPayload{i}", self.clock) for i in range(4)]
        self.completions = [PipeIn(dut, f"dmaCompletion{i}", self.clock) for i in range(4)]
        self.feedback = PipeOut(dut, "feedback", self.clock)
        self.cqe = PipeOut(dut, "completion", self.clock)
        self.errors = PipeOut(dut, "errors", self.clock)

    async def initialize(self) -> None:
        await self.configure_epoch(value=1)
        tx_cfg = MrcTxCfgReq(
            cfg=MacIpConfig(
                srcMac=0x66778899AABB,
                dstMac=0x112233445566,
                srcIp=0x0A000001,
                dstIp=0x0A000002,
            ),
            channelEnable=0xF,
            maxHighBurst=4,
        )
        await pipe_put(self.tx_config, self.clock, tx_cfg)
        response = await pipe_get(self.tx_config_out, self.clock, CfgResp)
        assert int(response.success) == 1
        await self.configure_network(
            cfg=RxNetworkConfig(
                localMac=0x112233445566,
                localIp=0x0A000002,
                peerMac=0x66778899AABB,
                peerIp=0x0A000001,
            ).pack()
        )
        await self.configure_qp(
            cfg=RxQpConfig(
                valid=1,
                qpn=7,
                initialPsn=0xFFFFFE,
                pmtu=0,
                rkey=RKEY,
                virtualBase=0x1000,
                physicalBase=0x8000,
                regionBytes=0x10000,
                remoteWrite=1,
            ).pack()
        )
        await self.start_rx()

    async def capture_frame(self, channel: int) -> tuple[int, bytes]:
        data = bytearray()
        psn = None
        while True:
            beat = await pipe_get(self.tx_channels[channel], self.clock, EthPacket)
            valid = int(beat.be).bit_count()
            assert int(beat.be) == (1 << valid) - 1
            if psn is None:
                assert int(beat.sof) == 1
                psn = int(beat.psn)
            data += int(beat.data).to_bytes(32, "little")[:valid]
            if int(beat.eof):
                return psn, bytes(data)

    async def send_frame(self, lane: int, frame: bytes) -> None:
        for offset in range(0, len(frame), 32):
            chunk = frame[offset : offset + 32]
            await pipe_put(
                self.rx_lanes[lane],
                self.clock,
                RxWireBeat(
                    data=int.from_bytes(chunk.ljust(32, b"\x00"), "little"),
                    be=(1 << len(chunk)) - 1,
                    sof=int(offset == 0),
                    eof=int(offset + len(chunk) == len(frame)),
                ),
            )

    async def collect_dma(self, lane: int):
        request = await pipe_get(self.requests[lane], self.clock, RxDmaWriteRequest)
        data = bytearray()
        while len(data) < int(request.bytes):
            beat = await pipe_get(self.payloads[lane], self.clock, RxDmaPayloadBeat)
            assert int(beat.epoch) == int(request.epoch)
            valid = int(beat.be).bit_count()
            data += int(beat.data).to_bytes(32, "little")[:valid]
        return request, bytes(data)


@cocotb.test()
async def test_actual_m2_m3_frames_reorder_through_rx_to_gpu_model(dut):
    tb = LoopbackTB(dut)
    await reset_dut(dut)
    await tb.initialize()
    payload = bytes((index * 19 + 7) & 0xFF for index in range(500))
    context = MessageContext(
        requestId=1,
        qpn=99,
        destQpn=7,
        slotId=0,
        debugSeq=0,
        pmtu=0,
        startPsn=0xFFFFFE,
        psnCount=3,
        remoteAddr=0x2000,
        remoteKey=RKEY,
        len=len(payload),
    )
    capture_tasks = [cocotb.start_soon(tb.capture_frame(i)) for i in range(3)]
    await pipe_put(tb.tx_context, tb.clock, context)
    for offset in range(0, len(payload), 128):
        chunk = payload[offset : offset + 128]
        await pipe_put(
            tb.tx_payload,
            tb.clock,
            MessagePayloadBeat(
                requestId=1,
                messageOffset=offset,
                data=int.from_bytes(chunk.ljust(128, b"\x00"), "little"),
                keep=(1 << len(chunk)) - 1,
                messageSop=int(offset == 0),
                messageEop=int(offset + len(chunk) == len(payload)),
            ),
        )
    captured = [await task for task in capture_tasks]
    assert [psn for psn, _ in captured] == [0xFFFFFE, 0xFFFFFF, 0]
    assert [len(frame) for _, frame in captured] == [298, 298, 82]
    for _ in range(3):
        await pipe_get(tb.tx_commit, tb.clock)

    # Reinject only actual wire fields, in reverse PSN order and on new lanes.
    dma_tasks = [cocotb.start_soon(tb.collect_dma(lane)) for lane in range(3)]
    await tb.send_frame(2, captured[2][1])
    await tb.send_frame(1, captured[1][1])
    await tb.send_frame(0, captured[0][1])
    feedback = [await pipe_get(tb.feedback, tb.clock, RxFeedback) for _ in range(3)]
    assert int(feedback[-1].ackPsn) == 0
    dma_packets = [await task for task in dma_tasks]
    dma_packets.sort(key=lambda item: int(item[0].addr))
    rebuilt = b"".join(data for _, data in dma_packets)
    assert rebuilt == payload
    assert [int(request.addr) for request, _ in dma_packets] == [
        0x9000,
        0x9000 + 228,
        0x9000 + 472,
    ]

    by_seq = {int(request.globalSeq): request for request, _ in dma_packets}
    for seq in sorted(by_seq, reverse=True):
        request = by_seq[seq]
        await pipe_put(
            tb.completions[int(request.lane)],
            tb.clock,
            RxDmaCompletion(
                globalSeq=seq,
                epoch=int(request.epoch),
                lane=int(request.lane),
                success=1,
            ),
        )
    cqe = await pipe_get(tb.cqe, tb.clock, RxCompletion)
    assert (int(cqe.qpn), int(cqe.lastPsn), int(cqe.bytes)) == (7, 0, 500)
    await assert_pipe_empty(tb.errors, tb.clock)
