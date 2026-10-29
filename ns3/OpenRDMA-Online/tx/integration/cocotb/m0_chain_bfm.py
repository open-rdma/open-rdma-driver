"""BFM and concurrent scoreboard for the end-to-end M0 -> M3 chain.

Drives mkTestM0Chain (mvp TxMvp -> M0MessageAdapter glue ->
mkDmaPhaseAIntegration) from GDRP submissions plus the four M0 table config
servers, services the DMA request tap with a deterministic memory image, and
scores every observable output -- DmaEngine requests, DmaMessageDone,
TxCommit, and the four channel frame streams -- against a specification that
is derived exclusively from the submissions and table configurations
(M0RefModel in m0_chain_model), never from the DUT.

There is intentionally no negative test suite for the m0chain target: the
glue's assertions (M0CHAIN_*) are cross-checks that fire only when M0 drifts
from the Phase-A packetizer contract, which cannot be provoked from the test
boundary by a "valid-looking" input. Invalid inputs are rejected upstream by
M0's own dynamic asserts (unit-tested in the mvp tx suite).
"""

from __future__ import annotations

from dataclasses import dataclass

import cocotb
from cocotb.clock import Clock
from cocotb.queue import Queue
from cocotb.triggers import RisingEdge, with_timeout
from cocotb_bsv import PipeIn, PipeOut

from bfm.m3 import (
    collect_commit, collect_egress, collect_frame, configure, pipe_get,
)
from dma_phasea_model import (
    DmaMessageDone,
    DmaReadRequest,
    completion_beats,
    data_byte,
)
from m0_chain_model import (
    DecodedGdrpSubmission,
    MrCfgReq,
    PgtCfgReq,
    QpCfgReq,
    SlotCfgReq,
    TxCfgResp,
    TxDmaReadRequest,
    make_token,
    packet_count,
)
from model.m2 import MessageContext, WIRE_LEGACY_PRIVATE, packetize
from model.m3 import MacIpConfig, expected_frame

# pipe_get's default budget is 20_000 cycles (~40 us at the 2 ns clock), which
# a single large message legitimately exceeds: the tracker's DmaMessageDone
# for a 2 MiB message fires only after the whole payload has traversed
# compactor -> M2 -> M3 (~100+ us of pipeline latency, message-serial by
# construction).  The per-get cap below (8 ms) stays far under every test's
# own timeout, so a genuine stall still fails fast -- but a slow-but-valid
# message no longer trips it.
_GET_CYCLES = 4_000_000


async def _pipe_get(pipe, clock) -> int:
    return await pipe_get(pipe, clock, cycles=_GET_CYCLES)


@dataclass(frozen=True)
class M0Submission:
    qpn: int
    slot_id: int
    length: int
    debug_seq: int = 0


@dataclass(frozen=True)
class EngineChunkSpec:
    """Expected DmaReadRequest fields for one chunk (1:1 with an M0 burst)."""

    wqe_tag: int
    chunk_seq: int
    physical_addr: int
    length: int
    message_offset: int
    is_first: int
    is_last: int


@dataclass(frozen=True)
class ExpectedMessage:
    context: MessageContext
    payload: bytes
    chunks: tuple[EngineChunkSpec, ...]

    @property
    def packets(self):
        return packetize(self.context, self.payload)


class M0ChainTB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.resetn = dut.RST_N
        self.submission = PipeIn(dut, "submissionPipeIn", self.clock)
        self.slot_cfg_req = PipeIn(dut, "slotCfgReqPipeIn", self.clock)
        self.slot_cfg_resp = PipeOut(dut, "slotCfgRespPipeOut", self.clock)
        self.qp_cfg_req = PipeIn(dut, "qpCfgReqPipeIn", self.clock)
        self.qp_cfg_resp = PipeOut(dut, "qpCfgRespPipeOut", self.clock)
        self.mr_cfg_req = PipeIn(dut, "mrCfgReqPipeIn", self.clock)
        self.mr_cfg_resp = PipeOut(dut, "mrCfgRespPipeOut", self.clock)
        self.pgt_cfg_req = PipeIn(dut, "pgtCfgReqPipeIn", self.clock)
        self.pgt_cfg_resp = PipeOut(dut, "pgtCfgRespPipeOut", self.clock)
        self.dma_request = PipeOut(dut, "dmaReadReqPipeOut", self.clock)
        self.completion = PipeIn(dut, "dmaCompletionPipeIn", self.clock)
        self.channels = [
            PipeOut(dut, f"channel{index}PipeOut", self.clock)
            for index in range(4)
        ]
        self.commits = PipeOut(dut, "txCommitPipeOut", self.clock)
        self.egress = PipeOut(dut, "txEgressPipeOut", self.clock)
        self.done = PipeOut(dut, "dmaMessageDonePipeOut", self.clock)
        self.config = PipeIn(dut, "configPipeIn", self.clock)
        self.config_response = PipeOut(dut, "configRespPipeOut", self.clock)
        # TxEgressEvent backpressures the encoder until it is drained, so the
        # chain keeps a background consumer even when it does not inspect it.

    async def reset(self) -> None:
        cocotb.start_soon(Clock(self.clock, 2, unit="ns").start())
        self.resetn.value = 0
        for _ in range(4):
            await RisingEdge(self.clock)
        self.resetn.value = 1
        for _ in range(4):
            await RisingEdge(self.clock)
        self._drain = cocotb.start_soon(self._drain_egress())

    async def _drain_egress(self) -> None:
        while True:
            await collect_egress(self)


async def _configure(req: PipeIn, resp: PipeOut, packed: int, clock) -> None:
    await req.enq(packed)
    response = TxCfgResp.unpack(await _pipe_get(resp, clock))
    assert int(response.success) == 1


async def configure_slot(tb: M0ChainTB, model, index: int, entry) -> None:
    await _configure(tb.slot_cfg_req, tb.slot_cfg_resp, SlotCfgReq(index=index, entry=entry).pack(), tb.clock)
    model.configure_slot(index, entry)


async def configure_qp(tb: M0ChainTB, model, index: int, entry) -> None:
    await _configure(tb.qp_cfg_req, tb.qp_cfg_resp, QpCfgReq(index=index, entry=entry).pack(), tb.clock)
    model.configure_qp(index, entry)


async def configure_mr(tb: M0ChainTB, model, index: int, entry) -> None:
    await _configure(tb.mr_cfg_req, tb.mr_cfg_resp, MrCfgReq(index=index, entry=entry).pack(), tb.clock)
    model.configure_mr(index, entry)


async def configure_pte(tb: M0ChainTB, model, index: int, entry) -> None:
    await _configure(tb.pgt_cfg_req, tb.pgt_cfg_resp, PgtCfgReq(index=index, entry=entry).pack(), tb.clock)
    model.configure_pte(index, entry)


def spec_context(first: TxDmaReadRequest) -> MessageContext:
    assert int(first.isFirst) == 1
    qpn = int(first.qpn)
    return MessageContext(
        requestId=int(first.requestId),
        qpn=qpn,
        destQpn=qpn,  # M0 is a self-loop WRITE; the adapter forwards qpn.
        slotId=int(first.slotId),
        debugSeq=int(first.debugSeq),
        pmtu=int(first.pmtu),
        startPsn=int(first.startPsn),
        psnCount=int(first.psnCount),
        remoteAddr=int(first.remoteAddr),
        remoteKey=int(first.remoteKey),
        len=int(first.messageLength),
        wireProfile=WIRE_LEGACY_PRIVATE,
        evProfileId=0,
        mrcMsn=0,
        mrcMsnValid=0,
    )


def payload_of(requests: tuple[TxDmaReadRequest, ...]) -> bytes:
    """Deterministic memory image: byte(addr) == data_byte(addr)."""
    return bytes(
        data_byte(address)
        for request in requests
        for address in range(
            int(request.physicalAddr), int(request.physicalAddr) + int(request.len)
        )
    )


def engine_specs(requests: tuple[TxDmaReadRequest, ...]) -> tuple[EngineChunkSpec, ...]:
    offset = 0
    result = []
    for index, request in enumerate(requests):
        result.append(
            EngineChunkSpec(
                wqe_tag=int(request.requestId),
                chunk_seq=int(request.dmaSeq),
                physical_addr=int(request.physicalAddr),
                length=int(request.len),
                message_offset=offset,
                is_first=int(request.isFirst),
                is_last=int(request.isLast),
            )
        )
        offset += int(request.len)
    return tuple(result)


def expect_message(
    model, spec: M0Submission
) -> ExpectedMessage:
    """Ask the M0 reference model for a submission and derive every expectation."""
    token = make_token(slot_id=spec.slot_id, length=spec.length, debug_seq=spec.debug_seq)
    requests = tuple(model.submit(qpn=spec.qpn, token=token))
    assert requests, "reference model produced no requests"
    context = spec_context(requests[0])
    payload = payload_of(requests)
    assert len(payload) == spec.length == int(context.len)
    assert int(context.psnCount) == packet_count(spec.length, int(context.pmtu))
    assert len(requests) == int(requests[-1].dmaSeq) + 1
    assert int(requests[-1].isLast) == 1
    return ExpectedMessage(
        context=context, payload=payload, chunks=engine_specs(requests)
    )


def _compare_request(expected: EngineChunkSpec, actual: DmaReadRequest, index: int) -> None:
    values = {
        "wqeTag": expected.wqe_tag,
        "chunkSeq": expected.chunk_seq,
        "physAddr": expected.physical_addr,
        "len": expected.length,
        "messageOffset": expected.message_offset,
        "isFirst": expected.is_first,
        "isLast": expected.is_last,
    }
    for field, expected_value in values.items():
        actual_value = int(getattr(actual, field))
        assert actual_value == expected_value, (
            f"request[{index}].{field}: expected={expected_value:#x} "
            f"actual={actual_value:#x}"
        )


def _compare_frame(expected, actual: object, index: int) -> None:
    assert actual == expected, (
        f"frame[{index}] mismatch\n"
        f"expected={expected!r}\nactual={actual!r}"
    )


async def run_m0_chain_scenario(
    tb: M0ChainTB,
    model,
    specs: list[M0Submission],
    cfg: MacIpConfig,
    *,
    channel_enable: int = 0xF,
    timeout_ns: int = 3_000_000,
) -> None:
    await configure(tb, cfg, channel_enable=channel_enable)

    expected = [expect_message(model, spec) for spec in specs]
    expected_packets = [packet for message in expected for packet in message.packets]
    expected_frames = [
        expected_frame(packet.descriptor, packet.payload, cfg)
        for packet in expected_packets
    ]

    async def produce() -> None:
        for spec in specs:
            token = make_token(
                slot_id=spec.slot_id, length=spec.length, debug_seq=spec.debug_seq
            )
            submission = DecodedGdrpSubmission(qpn=spec.qpn, token=token)
            await tb.submission.enq(submission.pack())

    async def service_dma() -> None:
        request_index = 0
        for message in expected:
            chunks = message.chunks
            group_index = 0
            while group_index < len(chunks):
                batch_count = min(8, len(chunks) - group_index)
                batch = []
                for local_index in range(batch_count):
                    actual = DmaReadRequest.unpack(
                        await _pipe_get(tb.dma_request, tb.clock)
                    )
                    _compare_request(
                        chunks[group_index + local_index],
                        actual,
                        request_index + local_index,
                    )
                    batch.append(actual)
                group_index += batch_count
                request_index += batch_count
                # Return each batch fully reversed to prove cross-tag
                # completion reorder survives the whole chain.
                for request in reversed(batch):
                    for beat in completion_beats(request):
                        await tb.completion.enq(beat.pack())

    async def collect_dones() -> None:
        for index, message in enumerate(expected):
            actual = DmaMessageDone.unpack(await _pipe_get(tb.done, tb.clock))
            assert int(actual.wqeTag) == int(message.context.requestId), index
            assert int(actual.length) == len(message.payload), index
            assert int(actual.chunkCount) == len(message.chunks), index

    async def collect_tx() -> None:
        queues = [Queue() for _ in range(4)]

        async def channel_worker(channel: int) -> None:
            while True:
                item = await queues[channel].get()
                if item is None:
                    return
                index, expected = item
                actual = await collect_frame(tb, channel)
                _compare_frame(expected, actual, index)

        workers = [cocotb.start_soon(channel_worker(channel)) for channel in range(4)]
        for index, packet in enumerate(expected_packets):
            commit = await collect_commit(tb)
            descriptor = packet.descriptor
            assert int(commit.requestId) == int(descriptor.requestId), index
            assert int(commit.qpn) == int(descriptor.qpn), index
            assert int(commit.psn) == int(descriptor.psn), index
            assert int(commit.packetIndex) == int(descriptor.packetIndex), index
            assert int(commit.trafficClass) == int(descriptor.trafficClass), index
            channel = int(commit.channel)
            assert channel_enable & (1 << channel), index
            await queues[channel].put((index, expected_frames[index]))

        for queue in queues:
            await queue.put(None)
        for worker in workers:
            await worker

    tasks = [
        cocotb.start_soon(produce()),
        cocotb.start_soon(service_dma()),
        cocotb.start_soon(collect_dones()),
        cocotb.start_soon(collect_tx()),
    ]

    async def wait_all() -> None:
        for task in tasks:
            await task

    await with_timeout(wait_all(), timeout_ns, "ns")

    for _ in range(16):
        await RisingEdge(tb.clock)
        assert not await tb.dma_request.not_empty(), "trailing DMA request"
        assert not await tb.done.not_empty(), "trailing DMA done"
        assert not await tb.commits.not_empty(), "trailing TxCommit"
        for channel in tb.channels:
            assert not await channel.not_empty(), "trailing EthPacket"
