"""BFM and concurrent scoreboard for DMA -> Phase-A M2/M3 integration."""

from __future__ import annotations

from dataclasses import dataclass

import cocotb
from cocotb.clock import Clock
from cocotb.queue import Queue
from cocotb.triggers import RisingEdge, with_timeout
from cocotb_bsv import PipeIn, PipeOut

from bfm.m3 import (
    collect_commit,
    collect_egress,
    collect_frame,
    configure,
    configure_ev_entry,
    configure_source_route,
    pipe_get,
)
from dma_phasea_model import (
    DmaChunkReq,
    DmaCompletionBeat,
    DmaMessageDone,
    DmaReadRequest,
    ChunkSpec,
    chunk_specs,
    completion_beats,
    logical_payload,
)
from model.m2 import (
    WIRE_MRC_IPV4_ECMP_NO_ICRC,
    MessageContext,
    Packet,
    packetize,
)
from model.m3 import Frame, MacIpConfig, MrcEvEntryConfig, expected_frame


@dataclass(frozen=True)
class IntegrationMessage:
    context: MessageContext
    chunks: tuple[DmaChunkReq, ...]

    @property
    def payload(self) -> bytes:
        return logical_payload(self.chunks)

    @property
    def packets(self) -> list[Packet]:
        return packetize(self.context, self.payload)


class DmaPhaseATB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.resetn = dut.RST_N
        self.context = PipeIn(dut, "contextPipeIn", self.clock)
        self.chunk = PipeIn(dut, "chunkPipeIn", self.clock)
        self.dma_request = PipeOut(dut, "dmaReadReqPipeOut", self.clock)
        self.completion = PipeIn(dut, "dmaCompletionPipeIn", self.clock)
        self.channels = [
            PipeOut(dut, f"channel{index}PipeOut", self.clock)
            for index in range(4)
        ]
        self.commits = PipeOut(dut, "txCommitPipeOut", self.clock)
        self.egress = PipeOut(dut, "txEgressPipeOut", self.clock)
        self.done = PipeOut(dut, "dmaMessageDonePipeOut", self.clock)
        self.source_route_config = PipeIn(
            dut, "sourceRouteConfigPipeIn", self.clock
        )
        self.ev_entry_config = PipeIn(dut, "evEntryConfigPipeIn", self.clock)
        self.ev_policy_config = PipeIn(dut, "evPolicyConfigPipeIn", self.clock)
        self.port_status = PipeIn(dut, "portStatusPipeIn", self.clock)
        self.path_feedback = PipeIn(dut, "pathFeedbackPipeIn", self.clock)
        self.probes = PipeOut(dut, "probePipeOut", self.clock)
        self.no_path = PipeOut(dut, "noPathPipeOut", self.clock)
        self.config = PipeIn(dut, "configPipeIn", self.clock)
        self.config_response = PipeOut(dut, "configRespPipeOut", self.clock)

    async def reset(self) -> None:
        cocotb.start_soon(Clock(self.clock, 2, unit="ns").start())
        self.resetn.value = 0
        for _ in range(4):
            await RisingEdge(self.clock)
        self.resetn.value = 1
        for _ in range(4):
            await RisingEdge(self.clock)
        # TxEgressEvent backpressures the encoder until it is drained.
        cocotb.start_soon(self._drain_egress())

    async def _drain_egress(self) -> None:
        while True:
            await collect_egress(self)

    async def put_context(self, context: MessageContext) -> None:
        await self.context.enq(context.pack())

    async def put_chunk(self, chunk: DmaChunkReq) -> None:
        await self.chunk.enq(chunk.pack())

    async def get_request(self) -> DmaReadRequest:
        return DmaReadRequest.unpack(await pipe_get(self.dma_request, self.clock))

    async def put_completion(self, completion: DmaCompletionBeat) -> None:
        await self.completion.enq(completion.pack())

    async def get_done(self) -> DmaMessageDone:
        return DmaMessageDone.unpack(await pipe_get(self.done, self.clock))


def _compare_request(expected: ChunkSpec, actual: DmaReadRequest, index: int) -> None:
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


def _compare_frame(expected: Frame, actual: Frame, index: int) -> None:
    assert actual == expected, (
        f"frame[{index}] mismatch\n"
        f"expected={expected!r}\nactual={actual!r}"
    )


async def run_integration_scenario(
    tb: DmaPhaseATB,
    messages: list[IntegrationMessage],
    cfg: MacIpConfig,
    *,
    channel_enable: int = 0xF,
    chunk_first: bool = False,
    mrc_entropy: int | None = None,
    mrc_channel: int = 0,
    timeout_ns: int = 2_000_000,
) -> None:
    await configure(tb, cfg, channel_enable=channel_enable)

    mrc_profiles = {
        int(message.context.evProfileId)
        for message in messages
        if int(message.context.wireProfile) == WIRE_MRC_IPV4_ECMP_NO_ICRC
    }
    if mrc_profiles:
        if any(
            int(message.context.wireProfile) != WIRE_MRC_IPV4_ECMP_NO_ICRC
            for message in messages
        ):
            raise ValueError(
                "one integration run cannot mix wire profiles on the first source"
            )
        if mrc_entropy is None:
            raise ValueError("MRC integration messages require an EV entropy")
        if len(mrc_profiles) != 1:
            raise ValueError("the integration helper supports one MRC EV profile per run")
        if not channel_enable & (1 << mrc_channel):
            raise ValueError("the configured MRC channel is disabled")
        profile = next(iter(mrc_profiles))
        await configure_source_route(tb, mrc_mask=0b0001, first_profile=profile)
        await configure_ev_entry(
            tb,
            MrcEvEntryConfig(
                profileId=profile,
                entryIndex=0,
                valid=1,
                adminEnable=1,
                entropy=mrc_entropy,
                channel=mrc_channel,
            ),
        )
        for _ in range(4):
            await RisingEdge(tb.clock)

    expected_chunk_groups = [chunk_specs(message.chunks) for message in messages]
    expected_packets = [packet for message in messages for packet in message.packets]
    expected_frames = [
        expected_frame(
            packet.descriptor,
            packet.payload,
            cfg,
            entropy=(
                int(mrc_entropy)
                if int(packet.descriptor.wireProfile)
                == WIRE_MRC_IPV4_ECMP_NO_ICRC
                else 0
            ),
        )
        for packet in expected_packets
    ]

    async def produce() -> None:
        for message in messages:
            if chunk_first:
                await tb.put_chunk(message.chunks[0])
                await RisingEdge(tb.clock)
                await tb.put_context(message.context)
                for chunk in message.chunks[1:]:
                    await tb.put_chunk(chunk)
            else:
                await tb.put_context(message.context)
                for chunk in message.chunks:
                    await tb.put_chunk(chunk)

    async def service_dma() -> None:
        request_index = 0
        for group in expected_chunk_groups:
            group_index = 0
            while group_index < len(group):
                batch_count = min(8, len(group) - group_index)
                batch = []
                for local_index in range(batch_count):
                    actual = await tb.get_request()
                    _compare_request(
                        group[group_index + local_index],
                        actual,
                        request_index + local_index,
                    )
                    batch.append(actual)
                group_index += batch_count
                request_index += batch_count
                # 每批完全反序返回，证明 cross-tag completion reorder。
                for request in reversed(batch):
                    for beat in completion_beats(request):
                        await tb.put_completion(beat)

    async def collect_dones() -> None:
        for index, message in enumerate(messages):
            actual = await tb.get_done()
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
