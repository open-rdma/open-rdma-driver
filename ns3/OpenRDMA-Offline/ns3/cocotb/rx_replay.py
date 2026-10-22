"""Stage 3 of the offline flow: replay the arrival trace into the RX RTL.

This drives the real `mkTestRxTop` (packet buffer -> parser -> RQ bitmap ->
DMA placement -> ordered queue) under Verilator.  Frames arrive on the four
lanes in the order ns-3 delivered them, spaced by the arrival timestamps, and
the test checks the things the RX is responsible for:

  * every byte the TX sent lands at the right GPU address, exactly once
    (matched per (QPN, PSN), so reordering and the two intentional address
    overlaps in the workload cannot hide an error);
  * one CQE per message, carrying the right QPN, last PSN and byte count;
  * the ordered queue retires every packet and the watermarks land on the
    last PSN of each stream.

The QP context -- QPN, initial PSN, RKEY, virtual base -- is derived from the
artifacts the TX stage wrote, not from the TX source tree: the two ends of a
connection agree on that context out of band, and the RX has to enforce it
against whatever the network delivers.  The L2/L3 addresses come from
`tx-connection.csv` the same way: the RX is configured with the declared
identity and must reject arrivals that do not match it.

Lane assignment is channel == lane: the four M3 egress channels are the four
network paths and the four receive lanes.
"""

from __future__ import annotations

import json
from collections import defaultdict

import cocotb
from cocotb.queue import Queue
from cocotb.triggers import RisingEdge
from cocotb.utils import get_sim_time

from bfm.common import pipe_get, pipe_put, reset_dut
from cocotb_bsv import ActionMethod, PipeIn, PipeOut, ValueMethod
from model.rx_types import (
    RxCompletion,
    RxDmaCompletion,
    RxDmaPayloadBeat,
    RxDmaWriteRequest,
    RxEcnEvent,
    RxError,
    RxFeedback,
    RxFlagDmaRequest,
    RxNetworkConfig,
    RxOrderedCredit,
    RxQpConfig,
    RxReceiveProgress,
    RxTxFeedback,
    RxWireBeat,
)

from offline_lib import (
    PSN_MASK,
    ArrivalRow,
    FrameOracleRow,
    MessageOracleRow,
    artifact,
    frame_connection,
    psn_distance,
    read_arrivals,
    read_connection,
    read_frame_oracle,
    read_message_oracle,
)

# ---------------------------------------------------------------------------
# Receiver-side choices.  Everything else comes from the trace artifacts.
# ---------------------------------------------------------------------------

# GPU memory the RX places into: one disjoint 64 KiB region per QP.
PHYSICAL_BASE = 0x0000_0000_8000_0000
PHYSICAL_STRIDE = 0x0000_0000_0001_0000
REGION_BYTES = 0x0000_0000_0001_0000

# PMTU codes understood by the legacy profile; code -> maximum packet size.
PMTU_BYTES = {0: 256, 1: 512, 2: 1024, 3: 2048, 4: 4096}
PMTU_CODES = sorted(PMTU_BYTES)

EPOCH = 1


class RxTopTB:
    """Bindings for mkTestRxTop, mirroring rx/cocotb's integration test."""

    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self.configure_epoch = ActionMethod(dut, "configureEpoch", self.clock)
        self.configure_network = ActionMethod(dut, "configureNetwork", self.clock)
        self.configure_qp = [
            ActionMethod(dut, f"configure{index}", self.clock) for index in range(4)
        ]
        self.start_method = ActionMethod(dut, "start", self.clock)
        self.progress = ValueMethod(dut, "progress0", self.clock)
        self.dma_ack = ValueMethod(dut, "dmaAckPsn0", self.clock)
        self.retire_head = ValueMethod(dut, "retireHead", self.clock)
        self.fault = ValueMethod(dut, "faulted", self.clock)
        self.inputs = [PipeIn(dut, f"lane{index}In", self.clock) for index in range(4)]
        self.requests = [
            PipeOut(dut, f"lane{index}DmaRequest", self.clock) for index in range(4)
        ]
        self.payloads = [
            PipeOut(dut, f"lane{index}DmaPayload", self.clock) for index in range(4)
        ]
        self.completions = [
            PipeIn(dut, f"lane{index}DmaCompletion", self.clock) for index in range(4)
        ]
        self.ordered_flags = PipeIn(dut, "orderedFlagIn", self.clock)
        self.flag_requests = PipeOut(dut, "flagDmaRequest", self.clock)
        self.flag_responses = PipeIn(dut, "flagDmaResponse", self.clock)
        self.credits = PipeOut(dut, "orderedCredit", self.clock)
        self.cqes = PipeOut(dut, "completion", self.clock)
        self.feedback = PipeOut(dut, "feedback", self.clock)
        self.ecn = PipeOut(dut, "ecn", self.clock)
        self.tx_feedback = PipeOut(dut, "txFeedback", self.clock)
        self.errors = PipeOut(dut, "errors", self.clock)

    async def send_frame(self, lane: int, frame: bytes) -> None:
        for offset in range(0, len(frame), 32):
            chunk = frame[offset : offset + 32]
            await pipe_put(
                self.inputs[lane],
                self.clock,
                RxWireBeat(
                    data=int.from_bytes(chunk.ljust(32, b"\x00"), "little"),
                    be=(1 << len(chunk)) - 1,
                    sof=int(offset == 0),
                    eof=int(offset + len(chunk) == len(frame)),
                ),
            )


# ---------------------------------------------------------------------------
# Deriving the connection context from the artifacts
# ---------------------------------------------------------------------------


class QpContext:
    def __init__(self, qpn: int) -> None:
        self.qpn = qpn
        self.initial_psn = 0
        self.rkey = 0
        self.virtual_base = 0
        self.region_bytes = REGION_BYTES
        self.physical_base = PHYSICAL_BASE + qpn * PHYSICAL_STRIDE
        self.pmtu = 0
        self.messages: list[MessageOracleRow] = []
        self.frames: list[FrameOracleRow] = []
        self.messages_by_id: dict[int, MessageOracleRow] = {}

    def expected_dma(self, frame: FrameOracleRow) -> tuple[int, int, bytes]:
        """(address, byte count, payload) the RX must place for this packet."""
        message = self.messages_by_id[int(frame.message_id)]
        start = int(frame.payload_offset)
        return (
            self.physical_base + start,
            int(frame.payload_length),
            message.payload[start : start + int(frame.payload_length)],
        )

    def expected_cqes(self) -> list[tuple[int, int, int]]:
        """(qpn, last PSN, byte count) for every message on this QP."""
        result = []
        for message in self.messages:
            packets = [
                frame
                for frame in self.frames
                if int(frame.message_id) == int(message.message_id)
            ]
            if not packets:
                raise RuntimeError(f"message {message.message_id} has no packets")
            if len(packets) != int(packets[0].packet_count):
                raise RuntimeError(f"message {message.message_id}: packet count mismatch")
            last_psn = (
                int(message.start_psn) + len(packets) - 1
            ) & PSN_MASK
            result.append((int(message.qpn), last_psn, int(message.message_length)))
        return result

    def last_psn(self) -> int:
        """Furthest PSN of this QP in the wrapping PSN space."""
        return max(
            (int(frame.psn) for frame in self.frames),
            key=lambda psn: psn_distance(self.initial_psn, psn),
        )


def build_contexts(
    messages: list[MessageOracleRow], frames: list[FrameOracleRow]
) -> dict[int, QpContext]:
    by_qpn: dict[int, QpContext] = {}
    for message in messages:
        qpn = int(message.qpn)
        context = by_qpn.setdefault(qpn, QpContext(qpn))
        context.messages.append(message)
    for frame in frames:
        context = by_qpn.get(int(frame.qpn))
        if context is None:
            raise RuntimeError(
                f"frame {frame.frame_id} references unknown QPN {frame.qpn}"
            )
        context.frames.append(frame)

    if len(by_qpn) > 4:
        raise RuntimeError("the RX has four QP slots; the trace needs more")

    for context in by_qpn.values():
        # RKEY and virtual base are connection-wide: every message on a QP must
        # agree, because the RX is configured with one base per QP.
        keys = {int(message.remote_key) for message in context.messages}
        bases = {int(message.remote_addr) for message in context.messages}
        if len(keys) != 1 or len(bases) != 1:
            raise RuntimeError(
                f"QPN {context.qpn}: messages disagree on RKEY {keys} or base {bases}"
            )
        context.rkey = keys.pop()
        context.virtual_base = bases.pop()
        context.messages_by_id = {
            int(message.message_id): message for message in context.messages
        }

        # Initial PSN: the start PSN whose forward distance to every other
        # start is smallest.  A receiver is configured with the PSN the sender
        # begins at, and every other message must sit ahead of it.
        starts = [int(message.start_psn) for message in context.messages]
        context.initial_psn = min(
            starts,
            key=lambda candidate: max(psn_distance(candidate, s) for s in starts),
        )

        # The trace must describe a gapless PSN stream: messages ordered by
        # distance from the initial PSN, packets in index order, consecutive.
        ordered = sorted(
            context.frames,
            key=lambda frame: (
                psn_distance(context.initial_psn, int(frame.psn)),
                int(frame.packet_index),
            ),
        )
        for position, frame in enumerate(ordered):
            actual = psn_distance(context.initial_psn, int(frame.psn))
            if actual != position:
                raise RuntimeError(
                    f"QPN {context.qpn}: PSN stream has a hole or duplicate at "
                    f"position {position} (frame {frame.frame_id}, PSN {frame.psn:#x})"
                )
            message = context.messages_by_id[int(frame.message_id)]
            if int(frame.psn) != (
                int(message.start_psn) + int(frame.packet_index)
            ) & PSN_MASK:
                raise RuntimeError(
                    f"QPN {context.qpn}: frame {frame.frame_id} is not packet "
                    f"{frame.packet_index} of message {frame.message_id}"
                )
            end = int(message.remote_addr) + int(frame.payload_offset) + int(
                frame.payload_length
            )
            if end > int(message.remote_addr) + REGION_BYTES:
                raise RuntimeError(
                    f"QPN {context.qpn}: frame {frame.frame_id} escapes the region"
                )

        # PMTU is informational for the RX (RxQpConfig.pmtu is stored but never
        # read): report the smallest MTU that could have carried these packets.
        widest = max(int(frame.payload_length) for frame in context.frames)
        context.pmtu = next(code for code in PMTU_CODES if PMTU_BYTES[code] >= widest)
        context.frames.sort(key=lambda frame: int(frame.frame_id))

    return by_qpn


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


class Observation:
    def __init__(self) -> None:
        self.dma: dict[tuple[int, int], tuple[int, int, bytes]] = {}
        self.dma_duplicates: list[tuple[int, int]] = []
        self.cqes: list[RxCompletion] = []
        self.feedback: list[RxFeedback] = []
        self.credits: list[RxOrderedCredit] = []
        self.errors: list[RxError] = []
        self.ecn: list[RxEcnEvent] = []
        self.tx_feedback: list[RxTxFeedback] = []
        self.unexpected_flags: list[RxFlagDmaRequest] = []
        self.completion_queue: Queue = Queue()


async def wait_ps(tb: RxTopTB, target_ps: int) -> None:
    """Spin the clock until the simulation reaches `target_ps`."""
    while int(get_sim_time("ps")) < target_ps:
        await RisingEdge(tb.clock)


async def drive_lane(
    tb: RxTopTB, lane: int, rows: list[ArrivalRow], replay_start_ps: int, origin_ps: int
) -> None:
    for row in rows:
        await wait_ps(tb, replay_start_ps + row.arrival_time_ps - origin_ps)
        await tb.send_frame(lane, row.frame)


async def collect_lane(tb: RxTopTB, lane: int, seen: Observation) -> None:
    while True:
        request = await pipe_get(tb.requests[lane], tb.clock, RxDmaWriteRequest)
        data = bytearray()
        while len(data) < int(request.bytes):
            beat = await pipe_get(tb.payloads[lane], tb.clock, RxDmaPayloadBeat)
            if int(beat.globalSeq) != int(request.globalSeq):
                raise RuntimeError(f"lane {lane}: payload beat for the wrong packet")
            if int(beat.epoch) != EPOCH:
                raise RuntimeError(f"lane {lane}: payload beat for the wrong epoch")
            valid = int(beat.be).bit_count()
            if int(beat.be) != (1 << valid) - 1:
                raise RuntimeError(f"lane {lane}: sparse byte enables in a payload beat")
            data += int(beat.data).to_bytes(32, "little")[:valid]
            if int(beat.eop):
                break
        if len(data) != int(request.bytes):
            raise RuntimeError(
                f"lane {lane}: {len(data)} payload bytes for a "
                f"{request.bytes} byte write"
            )
        key = (int(request.qpn), int(request.psn))
        if key in seen.dma:
            seen.dma_duplicates.append(key)
        else:
            seen.dma[key] = (int(request.addr), int(request.bytes), bytes(data))
        await seen.completion_queue.put(
            (lane, int(request.globalSeq), int(request.epoch))
        )


async def feed_completions(tb: RxTopTB, seen: Observation) -> None:
    """Acknowledge every DMA write, like a real DMA engine would."""
    while True:
        lane, global_seq, epoch = await seen.completion_queue.get()
        await pipe_put(
            tb.completions[lane],
            tb.clock,
            RxDmaCompletion(globalSeq=global_seq, epoch=epoch, lane=lane, success=1),
        )


async def collect_pipe(pipe, clock, struct_type, sink: list) -> None:
    while True:
        sink.append(await pipe_get(pipe, clock, struct_type))


async def wait_until(tb: RxTopTB, predicate, cycles: int, what: str) -> None:
    for _ in range(cycles):
        if predicate():
            return
        await RisingEdge(tb.clock)
    raise AssertionError(f"timed out waiting for {what}")


@cocotb.test(timeout_time=10_000_000, timeout_unit="ns")
async def replay_rx_trace(dut):
    arrivals = read_arrivals(artifact("rx-trace.csv"))
    frames = {row.frame_id: row for row in read_frame_oracle(artifact("tx-frames.csv"))}
    messages = read_message_oracle(artifact("tx-messages.csv"))
    # The connection is declared, not sniffed: the RX is configured from what
    # the TX says the link is, and then has to match every frame against it.
    # Deriving local/peer addresses from the arrivals would make this check a
    # tautology -- whatever arrived would define what was expected.
    connection = read_connection(artifact("tx-connection.csv"))

    if len(arrivals) != len(frames):
        raise AssertionError(
            f"arrival trace has {len(arrivals)} frames, oracle has {len(frames)}"
        )
    # The bytes that reach the RX must be exactly the bytes the TX emitted:
    # ns-3 checked this against the trace, and the trace is checked here
    # against the model that scored the RTL.
    for row in arrivals:
        expected = frames[row.frame_id].expected_frame
        if row.frame != expected:
            raise AssertionError(
                f"frame {row.frame_id} changed between TX and RX "
                f"({len(row.frame)} vs {len(expected)} bytes)"
            )
        actual = frame_connection(row.frame)
        if actual != connection:
            raise AssertionError(
                f"frame {row.frame_id} carries {actual}, "
                f"but the connection was declared as {connection}"
            )

    contexts = build_contexts(messages, list(frames.values()))

    tb = RxTopTB(dut)
    await reset_dut(dut)
    await tb.configure_epoch(value=EPOCH)
    local_mac, local_ip, peer_mac, peer_ip = connection.as_rx_view()
    await tb.configure_network(
        cfg=RxNetworkConfig(
            localMac=local_mac,
            localIp=local_ip,
            peerMac=peer_mac,
            peerIp=peer_ip,
        ).pack()
    )
    slot_zero_qpn = sorted(contexts)[0]
    for index, qpn in enumerate(sorted(contexts)):
        context = contexts[qpn]
        await tb.configure_qp[index](
            cfg=RxQpConfig(
                valid=1,
                qpn=qpn,
                initialPsn=context.initial_psn,
                pmtu=context.pmtu,
                rkey=context.rkey,
                virtualBase=context.virtual_base,
                physicalBase=context.physical_base,
                regionBytes=context.region_bytes,
                remoteWrite=1,
            ).pack()
        )
    await tb.start_method()

    seen = Observation()
    origin_ps = min(row.arrival_time_ps for row in arrivals)
    per_lane: dict[int, list[ArrivalRow]] = defaultdict(list)
    for row in arrivals:
        per_lane[row.channel].append(row)
    for rows in per_lane.values():
        rows.sort(key=lambda row: row.arrival_time_ps)

    replay_start_ps = int(get_sim_time("ps"))
    senders = [
        cocotb.start_soon(
            drive_lane(tb, lane, rows, replay_start_ps, origin_ps)
        )
        for lane, rows in sorted(per_lane.items())
    ]
    collectors = [
        cocotb.start_soon(collect_lane(tb, lane, seen)) for lane in range(4)
    ]
    background = [
        cocotb.start_soon(feed_completions(tb, seen)),
        cocotb.start_soon(collect_pipe(tb.cqes, tb.clock, RxCompletion, seen.cqes)),
        cocotb.start_soon(
            collect_pipe(tb.feedback, tb.clock, RxFeedback, seen.feedback)
        ),
        cocotb.start_soon(
            collect_pipe(tb.credits, tb.clock, RxOrderedCredit, seen.credits)
        ),
        cocotb.start_soon(collect_pipe(tb.errors, tb.clock, RxError, seen.errors)),
        cocotb.start_soon(collect_pipe(tb.ecn, tb.clock, RxEcnEvent, seen.ecn)),
        cocotb.start_soon(
            collect_pipe(tb.tx_feedback, tb.clock, RxTxFeedback, seen.tx_feedback)
        ),
        cocotb.start_soon(
            collect_pipe(
                tb.flag_requests, tb.clock, RxFlagDmaRequest, seen.unexpected_flags
            )
        ),
    ]

    try:
        for sender in senders:
            await sender
        await wait_until(
            tb,
            lambda: len(seen.cqes) >= len(messages),
            400_000,
            f"{len(messages)} CQEs (saw {len(seen.cqes)})",
        )
    finally:
        for task in senders + collectors + background:
            task.cancel()

    if int(await tb.fault()) != 0:
        raise AssertionError("the RX raised a global fault")
    if seen.errors:
        raise AssertionError(
            "the RX reported protocol errors: "
            + ", ".join(f"code={int(e.code)} qpn={int(e.qpn)}" for e in seen.errors)
        )
    if seen.unexpected_flags:
        raise AssertionError("the RX issued an unsolicited flag DMA request")
    if seen.dma_duplicates:
        raise AssertionError(f"duplicate DMA writes for {seen.dma_duplicates}")

    # ---- every packet was placed at the right address with the right bytes ----
    expected_keys: set[tuple[int, int]] = set()
    for context in contexts.values():
        for frame in context.frames:
            key = (context.qpn, int(frame.psn))
            expected_keys.add(key)
            if key not in seen.dma:
                raise AssertionError(f"no DMA write for QPN {key[0]} PSN {key[1]:#x}")
            address, count, payload = context.expected_dma(frame)
            actual_address, actual_count, actual_payload = seen.dma[key]
            if actual_address != address:
                raise AssertionError(
                    f"QPN {key[0]} PSN {key[1]:#x}: wrote {actual_address:#x}, "
                    f"expected {address:#x}"
                )
            if actual_count != count:
                raise AssertionError(
                    f"QPN {key[0]} PSN {key[1]:#x}: wrote {actual_count} bytes, "
                    f"expected {count}"
                )
            if actual_payload != payload:
                raise AssertionError(
                    f"QPN {key[0]} PSN {key[1]:#x}: payload differs from the TX oracle"
                )
    extra = set(seen.dma) - expected_keys
    if extra:
        raise AssertionError(f"DMA writes for packets the TX never sent: {extra}")

    # ---- one CQE per message ----
    expected_cqes = sorted(
        item for context in contexts.values() for item in context.expected_cqes()
    )
    actual_cqes = sorted(
        (int(cqe.qpn), int(cqe.lastPsn), int(cqe.bytes)) for cqe in seen.cqes
    )
    if actual_cqes != expected_cqes:
        raise AssertionError(f"CQEs {actual_cqes} do not match {expected_cqes}")
    for cqe in seen.cqes:
        if int(cqe.success) != 1:
            raise AssertionError("a CQE reports failure")

    # ---- the ordered queue drained and the watermarks landed ----
    head = int(await tb.retire_head())
    if head != len(arrivals):
        raise AssertionError(
            f"ordered queue retired {head} entries, expected {len(arrivals)}"
        )
    slot_zero = contexts[slot_zero_qpn]
    dma_ack = int(await tb.dma_ack())
    if dma_ack != slot_zero.last_psn():
        raise AssertionError(
            f"dmaAckPsn0 is {dma_ack:#x}, expected {slot_zero.last_psn():#x}"
        )
    progress = RxReceiveProgress.unpack(await tb.progress())
    if int(progress.ackPsn) != slot_zero.last_psn():
        raise AssertionError(
            f"progress0.ackPsn is {int(progress.ackPsn):#x}, "
            f"expected {slot_zero.last_psn():#x}"
        )
    if int(progress.oooCount) != 0:
        raise AssertionError("the RX still holds out-of-order packets")

    # All four lanes have to have carried traffic: the interesting part of this
    # replay is that reordered frames arriving on four independent lanes still
    # land in the right place, and that is vacuous if everything came in on one.
    # (ns-3 already checked this about its own trace; this is the RX side of it.)
    lanes_used = sorted(per_lane)
    if lanes_used != [0, 1, 2, 3]:
        raise AssertionError(f"the replay used lanes {lanes_used}, expected all of 0..3")

    report = {
        "stage": "rx-replay",
        "status": "PASS",
        "lanes_used": lanes_used,
        "qpns": sorted(contexts),
        "frames_replayed": len(arrivals),
        "dma_writes": len(seen.dma),
        "cqes": len(seen.cqes),
        "feedback_events": len(seen.feedback),
        "credits": len(seen.credits),
        "retired": head,
        "dma_ack_psn0": dma_ack,
        "reordered": _arrival_reordered(arrivals),
        "faulted": int(await tb.fault()),
    }
    artifact("rx-replay.json").write_text(json.dumps(report, indent=2) + "\n")

    dut._log.info(
        "RX replay: %d frames, %d DMA writes, %d CQEs, retired=%d, reordered=%s",
        len(arrivals),
        len(seen.dma),
        len(seen.cqes),
        head,
        report["reordered"],
    )


def _arrival_reordered(arrivals: list[ArrivalRow]) -> bool:
    order = [row.tx_order for row in arrivals]
    return order != sorted(order)
