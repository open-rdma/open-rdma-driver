"""Cocotb BFM for the M3 four-channel scheduler."""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, with_timeout

from cocotb_bsv import PipeIn, PipeOut
from model.m2 import PacketDescriptor
from model.m3 import (
    CfgResp, EthPacket, Frame, MacIpConfig, MrcEvEntryConfig, MrcEvPolicyConfig,
    MrcNoPathEvent, MrcPathFeedback, MrcPortStatusUpdate, MrcProbeRequest,
    MrcSourceProfileConfig, MrcTxCfgReq, TxCommit, TxEgressEvent,
    frame_word_bytes, packet_payload_beats,
)


class TB:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.resetn = dut.RST_N
        self.descriptors = {
            name: PipeIn(dut, f"{name}DescriptorPipeIn", self.clock)
            for name in ("first", "retry", "ordered", "control")
        }
        self.payloads = {
            name: PipeIn(dut, f"{name}PayloadPipeIn", self.clock)
            for name in ("first", "retry", "ordered", "control")
        }
        self.channels = [
            PipeOut(dut, f"channel{index}PipeOut", self.clock)
            for index in range(4)
        ]
        self.commits = PipeOut(dut, "txCommitPipeOut", self.clock)
        self.egress = PipeOut(dut, "txEgressPipeOut", self.clock)
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
        # TxEgressEvent is an observability stream that backpressures the
        # encoder until it is drained, so every test drains it in the
        # background and inspects the collected list when it cares.
        self.egress_events: list[TxEgressEvent] = []

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
            self.egress_events.append(await collect_egress(self))


async def pipe_get(pipe: PipeOut, clock, cycles: int = 20_000) -> int:
    async def get_one() -> int:
        value = await pipe.first()
        await pipe.deq()
        return value

    return await with_timeout(get_one(), cycles * 2, "ns")


async def configure(
    tb: TB, cfg: MacIpConfig, channel_enable: int = 0xF, max_high_burst: int = 4
) -> None:
    request = MrcTxCfgReq(
        cfg=cfg, channelEnable=channel_enable, maxHighBurst=max_high_burst
    )
    await tb.config.enq(request.pack())
    response = CfgResp.unpack(await pipe_get(tb.config_response, tb.clock))
    assert int(response.success) == 1


async def send_packet(
    tb: TB, descriptor: PacketDescriptor, payload: bytes, source: str = "first"
) -> None:
    descriptor_pipe = tb.descriptors[source]
    payload_pipe = tb.payloads[source]
    await descriptor_pipe.enq(descriptor.pack())
    for beat in packet_payload_beats(descriptor, payload):
        await payload_pipe.enq(beat.pack())


async def collect_frame(tb: TB, channel: int) -> Frame:
    data = bytearray()
    first_word = None
    while True:
        word = EthPacket.unpack(await pipe_get(tb.channels[channel], tb.clock))
        if first_word is None:
            first_word = word
            assert int(word.sof) == 1
        else:
            assert int(word.sof) == 0
        data += frame_word_bytes(word)
        if int(word.eof):
            break
    assert first_word is not None
    return Frame(
        data=bytes(data),
        request_id=int(first_word.requestId),
        qpn=int(first_word.qpn),
        dest_qpn=int(first_word.destQpn),
        psn=int(first_word.psn),
        is_first=int(first_word.isFirst),
        is_last=int(first_word.isLast),
        traffic_class=int(first_word.trafficClass),
    )


async def collect_commit(tb: TB) -> TxCommit:
    return TxCommit.unpack(await pipe_get(tb.commits, tb.clock))


async def configure_source_route(
    tb: TB,
    mrc_mask: int = 0,
    first_profile: int = 0,
    retry_profile: int = 0,
    ordered_profile: int = 0,
    control_profile: int = 0,
) -> None:
    await tb.source_route_config.enq(
        MrcSourceProfileConfig(
            mrcMask=mrc_mask,
            firstProfileId=first_profile,
            retryProfileId=retry_profile,
            orderedProfileId=ordered_profile,
            controlProfileId=control_profile,
        ).pack()
    )


async def configure_ev_entry(tb: TB, config: MrcEvEntryConfig) -> None:
    await tb.ev_entry_config.enq(config.pack())


async def configure_ev_policy(
    tb: TB, skip_hold_cycles: int, probe_interval_cycles: int
) -> None:
    await tb.ev_policy_config.enq(
        MrcEvPolicyConfig(
            skipHoldCycles=skip_hold_cycles,
            probeIntervalCycles=probe_interval_cycles,
        ).pack()
    )


async def update_port_status(
    tb: TB, local_reachable: int, peer_reachable: int
) -> None:
    await tb.port_status.enq(
        MrcPortStatusUpdate(
            localReachable=local_reachable, peerReachable=peer_reachable
        ).pack()
    )


async def send_path_feedback(tb: TB, feedback: MrcPathFeedback) -> None:
    await tb.path_feedback.enq(feedback.pack())


async def collect_probe(tb: TB) -> MrcProbeRequest:
    return MrcProbeRequest.unpack(await pipe_get(tb.probes, tb.clock))


async def collect_no_path(tb: TB) -> MrcNoPathEvent:
    return MrcNoPathEvent.unpack(await pipe_get(tb.no_path, tb.clock))


async def collect_egress(tb: TB) -> TxEgressEvent:
    return TxEgressEvent.unpack(await pipe_get(tb.egress, tb.clock))


async def wait_egress(tb: TB, count: int, cycles: int = 20_000) -> None:
    """Wait until the background drain has collected at least `count` events."""
    for _ in range(cycles):
        if len(tb.egress_events) >= count:
            return
        await RisingEdge(tb.clock)
    raise AssertionError(
        f"only {len(tb.egress_events)} TxEgressEvent in {cycles} cycles"
    )


async def pipe_quiet(pipe: PipeOut, clock, cycles: int = 64) -> bool:
    """Return True if the pipe stays empty for the whole window."""
    for _ in range(cycles):
        if await pipe.not_empty():
            return False
        await RisingEdge(clock)
    return True
