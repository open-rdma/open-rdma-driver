"""RX stage of the online flow: drive the RTL from the network's arrivals.

This drives the same `mkTestRxTop` (packet buffer -> parser -> RQ bitmap -> DMA
placement -> ordered queue) and checks the same things as the offline tree's
`rx_replay.py`.  What changes is where the frames come from and when they are
injected:

  * the connection context and both oracle tables arrive as one DECLARE message
    from the hub, which relayed them from the TX stage.  They are still built
    from the TX's Python models, so the RX keeps checking the DUT against
    something it did not produce;
  * frames arrive one EVENT at a time, while the TX simulation is still
    running.  Each lane injects its frame when the arrival instant comes, so
    the replay is paced by the network instead of by a timestamp read from a
    file before the replay starts.

That second point turns the socket into a pacing source, which raises the
question of what the replay's own clock is doing while it waits.  The answer has
two parts, and the second is easy to get wrong:

  * a socket read goes through `cocotb.task.bridge`, and a bridged blocking call
    holds the scheduler for as long as it runs -- the simulator cannot advance
    while one is outstanding, whatever the clock is doing.  So no arrival is
    ever late because the host was slow, and the free-running clock the third
    experiment switches on measures the same instants (see `FREE_RUNNING_CLOCK`);
  * `GatedClock` below goes further and makes the clock itself a resource the
    replay hands out: it ticks only inside a `with gate.open()` block, so every
    simulated cycle belongs to a named piece of work.  Injection instants come
    out the same either way, because nothing in this replay waits on a bare
    timestamp; the gate is what keeps that a property of the code rather than a
    coincidence.

The test records how far past the arrival instant each frame actually landed
(`max_injection_slack_ps`, split into the part a busy lane explains and the part
nothing does) so that the claim is checkable in the report rather than argued
from the mechanism.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from contextlib import contextmanager

import cocotb
from cocotb.clock import Clock
from cocotb.queue import Queue
from cocotb.task import bridge
from cocotb.triggers import Event, RisingEdge, Timer
from cocotb.utils import get_sim_time

from bfm.common import pipe_get, pipe_put
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

from online_lib import (
    PSN_MASK,
    ArrivalRow,
    FrameOracleRow,
    MessageOracleRow,
    artifact,
    frame_connection,
    psn_distance,
    rx_socket_path,
)
from online_protocol import DELIVERED, OnlineClient

# ---------------------------------------------------------------------------
# Receiver-side choices.  Everything else is declared by the TX stage.
# ---------------------------------------------------------------------------

# GPU memory the RX places into: one disjoint 64 KiB region per QP.
PHYSICAL_BASE = 0x0000_0000_8000_0000
PHYSICAL_STRIDE = 0x0000_0000_0001_0000
REGION_BYTES = 0x0000_0000_0001_0000

# PMTU codes understood by the legacy profile; code -> maximum packet size.
PMTU_BYTES = {0: 256, 1: 512, 2: 1024, 3: 2048, 4: 4096}
PMTU_CODES = sorted(PMTU_BYTES)

EPOCH = 1

CLOCK_HALF_PS = 1_000

# ONLINE_RX_FREE_CLOCK=1 replaces the gated clock with an ordinary free-running
# one, for the tutorial's third experiment.  The replay's injection instants do
# not change: the socket waits stall the scheduler (that is what `bridge` does),
# so the clock has nothing to run for while the stage is blocked.  What the
# switch removes is the rule that every simulated cycle is handed out by the
# replay; the slack accounting is what shows the instants are unaffected.
FREE_RUNNING_CLOCK = os.environ.get("ONLINE_RX_FREE_CLOCK") == "1"

# The gate keeps this test's simulation time bounded (one 2 ns cycle per
# injection, plus the drain), so ten milliseconds is generous.  Without the
# gate the clock runs across the socket waits and simulation time drifts by
# however long the host took; the test's own timeout then has to stand back
# and let the injection-slack assertion report that drift instead of racing it.
RUN_TIMEOUT_NS = 10_000_000 if not FREE_RUNNING_CLOCK else 100_000_000_000


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


class GatedClock:
    """The RX's clock, owned by the replay instead of free-running.

    `open()` marks a stretch in which the clock is allowed to tick (waiting for
    an arrival instant, pushing a frame through a pipe, draining the ordered
    queue); outside those stretches the clock task parks.  Every simulated
    cycle therefore has an owner on the replay side, and the pipeline only
    advances where the replay says it should.

    This is not what keeps a socket wait from costing simulation time -- a
    bridged blocking call stalls the scheduler by itself, which is why the
    third experiment's free-running clock lands on the same instants.  What the
    gate rules out is incidental ticking: any wait on a timestamp that is not a
    named piece of work.
    """

    def __init__(self, dut) -> None:
        self.dut = dut
        self.clock = dut.CLK
        self._holders = 0
        self._opened = Event()

    @contextmanager
    def open(self):
        self._holders += 1
        self._opened.set()
        try:
            yield
        finally:
            self._holders -= 1

    async def run(self) -> None:
        while True:
            if self._holders == 0:
                self._opened.clear()
                await self._opened.wait()
                continue
            self.clock.value = 1
            await Timer(CLOCK_HALF_PS, unit="ps")
            self.clock.value = 0
            await Timer(CLOCK_HALF_PS, unit="ps")




async def reset_dut_gated(tb: RxTopTB, gate: GatedClock, cycles: int = 4) -> None:
    """rx/cocotb/bfm/common.reset_dut without the free-running clock."""
    with gate.open():
        tb.dut.RST_N.value = 0
        for _ in range(cycles):
            await RisingEdge(tb.clock)
        tb.dut.RST_N.value = 1
        for _ in range(cycles):
            await RisingEdge(tb.clock)


# ---------------------------------------------------------------------------
# Deriving the connection context from the declaration
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
            last_psn = (int(message.start_psn) + len(packets) - 1) & PSN_MASK
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
        raise RuntimeError("the RX has four QP slots; the declaration needs more")

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

        # The declaration must describe a gapless PSN stream: messages ordered
        # by distance from the initial PSN, packets in index order, consecutive.
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
        # frame_id -> (arrival instant, instant the frame went in,
        #              instant this lane was last free)
        self.injections: dict[int, tuple[int, int, int | None]] = {}
        # frame_id -> drop reason, for frames the network never delivered
        self.dropped: dict[int, int] = {}
        # every EVENT the hub sent, delivered or not (the hub numbers them all)
        self.events = 0


async def wait_ps(tb: RxTopTB, target_ps: int) -> None:
    """Spin the clock until the simulation reaches `target_ps`."""
    while int(get_sim_time("ps")) < target_ps:
        await RisingEdge(tb.clock)


async def drive_lane(
    tb: RxTopTB, lane: int, queue: Queue, gate: GatedClock, seen: Observation
) -> None:
    """Wait for this lane's arrival instants, and push frames in on time.

    One task per lane, because one lane is one wire: the RTL sees this lane's
    beats one frame after another, so a lane's frames are already serialized
    by the order they come out of its queue.  Nothing here touches another
    lane's timeline -- a task waiting for its own instant stops the clock at
    that instant (the gate only runs while a holder wants it to), so a lane
    that is waiting cannot be dragged past its target by anything another lane
    does.  The one thing that does move the clock past an instant is a real
    push, and that only happens to a frame whose own lane is still busy.

    `busy_until` is when this lane last finished a push; a frame that finds
    the lane busy is injected as soon as it frees, and the check at the end of
    the test allows exactly that much lateness and no more.
    """

    busy_until: int | None = None
    while True:
        row = await queue.get()
        if row is None:
            return
        with gate.open():
            await wait_ps(tb, row.injection_target_ps)
            started = int(get_sim_time("ps"))
            seen.injections[row.frame_id] = (
                row.injection_target_ps,
                started,
                busy_until,
            )
            await tb.send_frame(lane, row.frame)
            busy_until = int(get_sim_time("ps"))


async def read_events(
    client: OnlineClient,
    frames: dict[int, FrameOracleRow],
    connection,
    queues: list[Queue],
    arrivals: list[ArrivalRow],
    clocking: dict,
    seen: Observation,
) -> None:
    """Pump the hub's event stream into the replay, checking as it goes.

    Every frame is checked against the declared oracle before it is allowed
    near the DUT: bytes, the connection it claims, and the channel it arrived
    on.  The hub has already done the same against the same declaration, but
    this is the side that has to live with the result.

    `clocking` holds the replay's time origin.  It is fixed when the first
    arrival shows up, at which point no lane driver has run yet and the clock
    is still parked, so the first frame is injected at the instant the replay
    starts rather than at whatever time the simulation had drifted to.

    Nothing here waits for the clock, so reading ahead is free: a frame is
    handed to its lane as soon as the hub reports it, which is always before
    the instant that frame names.  That is not a convenience -- an arrival
    event only exists once the network has delivered the frame, so the online
    replay cannot know a frame earlier than its arrival instant and has to
    start it there.  An offline replay, holding the whole trace up front,
    could in principle start a frame early so that its last beat lands on the
    arrival instant; here the instant is the earliest start the replay is
    allowed to use, and the lanes live with the difference (see `drive_lane`).

    A frame the network dropped still arrives here as an event, because the
    hub reports every frame it took in.  It is counted and recorded, not
    injected, and the test fails at the end where all the missing frames can
    be named at once.
    """
    while True:
        event = await bridge(client.receive_event)()
        if event is None:
            for queue in queues:
                await queue.put(None)
            return
        if event.sequence != seen.events:
            raise AssertionError(
                f"arrival {event.sequence} out of order "
                f"({seen.events} events received so far)"
            )
        seen.events += 1
        if not event.delivered:
            # Nothing to inject: the network dropped this frame on the way.  The
            # replay records it and keeps the stream and the handshake running,
            # because the hub only writes its report -- the one that says which
            # frames went missing -- once both stages have finished.  The run
            # fails at the end of the test instead, where the missing frames can
            # be named all at once.
            seen.dropped[event.frame_id] = event.reason
            continue
        if event.channel >= 4:
            raise AssertionError(f"frame {event.frame_id} arrived on lane {event.channel}")
        oracle = frames.get(event.frame_id)
        if oracle is None:
            raise AssertionError(f"frame {event.frame_id} was never declared")
        if event.frame != oracle.expected_frame:
            raise AssertionError(
                f"frame {event.frame_id} differs from the declared oracle "
                f"({len(event.frame)} vs {len(oracle.expected_frame)} bytes)"
            )
        actual = frame_connection(event.frame)
        if actual != connection:
            raise AssertionError(
                f"frame {event.frame_id} carries {actual}, "
                f"but the connection was declared as {connection}"
            )
        if arrivals and event.time_ps < arrivals[-1].arrival_time_ps:
            raise AssertionError(
                f"frame {event.frame_id} arrives at {event.time_ps} ps, before "
                f"the previously reported {arrivals[-1].arrival_time_ps} ps"
            )
        if clocking["origin_ps"] is None:
            clocking["origin_ps"] = event.time_ps
            clocking["replay_start_ps"] = int(get_sim_time("ps"))
            cocotb.log.info(
                "RX replay: origin is the first arrival, %d ps of network time "
                "at %d ps of simulation time",
                event.time_ps,
                clocking["replay_start_ps"],
            )
        row = ArrivalRow(
            sequence=event.sequence,
            frame_id=event.frame_id,
            tx_order=event.tx_order,
            channel=event.channel,
            arrival_time_ps=event.time_ps,
            frame=event.frame,
            injection_target_ps=(
                clocking["replay_start_ps"] + event.time_ps - clocking["origin_ps"]
            ),
        )
        arrivals.append(row)
        await queues[row.channel].put(row)


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


@cocotb.test(timeout_time=RUN_TIMEOUT_NS, timeout_unit="ns")
async def stream_rx_from_hub(dut):
    client = OnlineClient(str(rx_socket_path()))
    await bridge(client.connect)()
    # The oracle arrives before any frame does: the hub relays exactly what the
    # TX declared, and nothing the RX sees later can change what it expects.
    declaration = await bridge(client.receive_declaration)()
    frames = {row.frame_id: row for row in declaration.frames}
    messages = declaration.messages
    connection = declaration.connection

    contexts = build_contexts(messages, list(frames.values()))

    tb = RxTopTB(dut)
    gate = GatedClock(dut)
    if FREE_RUNNING_CLOCK:
        # The tutorial's third experiment: run the clock on its own instead of
        # handing out its cycles.  Nothing else changes, and the two numbers
        # measured below plus the slack accounting at the end are what show
        # what the gate did and did not do.
        cocotb.start_soon(Clock(dut.CLK, 2 * CLOCK_HALF_PS, unit="ps").start())
    else:
        cocotb.start_soon(gate.run())
    dut._log.info(
        "RX clock: %s (ONLINE_RX_FREE_CLOCK=%s)",
        "free-running" if FREE_RUNNING_CLOCK else "gated",
        os.environ.get("ONLINE_RX_FREE_CLOCK", "<unset>"),
    )
    await reset_dut_gated(tb, gate)

    # ---- what a socket wait costs, measured rather than argued ----
    #
    # The clock is running here (the reset above was clocked), so waiting for a
    # later instant moves simulation time by exactly that much.  Waiting for the
    # hub through `bridge` moves it not at all: a bridged blocking call holds
    # the scheduler for as long as it runs, and the simulator cannot advance
    # while one is outstanding.  That is why the arrival instants survive the
    # socket, and the gate is a second, independent rule about who may tick the
    # clock -- see the tutorial's third experiment.
    before = int(get_sim_time("ps"))
    with gate.open():
        await wait_ps(tb, before + 10 * CLOCK_HALF_PS)
    clocked = int(get_sim_time("ps"))
    await bridge(time.sleep)(0.05)
    stalled = int(get_sim_time("ps"))
    dut._log.info(
        "RX wait cost: %d ps clocked, %d ps bridged over 0.05 s of host time",
        clocked - before,
        stalled - clocked,
    )
    if not 10 * CLOCK_HALF_PS <= clocked - before <= 12 * CLOCK_HALF_PS:
        raise AssertionError(
            f"a clocked wait of {10 * CLOCK_HALF_PS} ps cost {clocked - before} ps; "
            "waiting on the clock is supposed to land on the next edge"
        )
    if stalled != clocked:
        raise AssertionError(
            f"a bridged call advanced simulation time by {stalled - clocked} ps; "
            "socket waits are supposed to stall the scheduler"
        )
    with gate.open():
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
    arrivals: list[ArrivalRow] = []
    lanes: list[Queue] = [Queue() for _ in range(4)]

    # Everything below runs against a clock that only moves when a driver asks
    # it to, so the replay gets its own timeline, anchored at the first
    # arrival: every frame after it is spaced exactly as the network spaced
    # them.  `clocking` is where the reader fixes that anchor.
    clocking: dict = {"origin_ps": None, "replay_start_ps": 0}

    collectors = [cocotb.start_soon(collect_lane(tb, lane, seen)) for lane in range(4)]
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

    drivers = [
        cocotb.start_soon(drive_lane(tb, lane, lanes[lane], gate, seen))
        for lane in range(4)
    ]
    reader = cocotb.start_soon(
        read_events(client, frames, connection, lanes, arrivals, clocking, seen)
    )

    try:
        # Nothing in this task drives time: each lane driver advances the clock
        # to its own arrival instants and while pushing its beats, and this one
        # only waits for the stream to end.  The hub closes it after the TX
        # stage has finished and the network has been run past the last frame.
        await reader
        for driver in drivers:
            await driver
        if not seen.dropped:
            with gate.open():
                # Let the pipeline finish retiring what it has already accepted.
                await wait_until(
                    tb,
                    lambda: len(seen.cqes) >= len(messages),
                    400_000,
                    f"{len(messages)} CQEs (saw {len(seen.cqes)})",
                )
        # With a dropped frame in the stream the pipeline never finishes -- the
        # hole starves everything behind it -- so the wait above is skipped and
        # the drop check below is the failure that gets reported.
    finally:
        for task in [reader, *drivers, *collectors, *background]:
            task.cancel()

    if client.eos is None:
        raise AssertionError("the hub never reported the end of the stream")
    if seen.events != client.eos.delivered + len(seen.dropped):
        raise AssertionError(
            f"the hub sent {seen.events} events, counting "
            f"{client.eos.delivered} deliveries and {len(seen.dropped)} drops"
        )
    if seen.dropped:
        # A lossy trace cannot be replayed: the RX has no retransmission, so a
        # message with a missing PSN never retires and nothing downstream of
        # the hole can be checked.  Say exactly which frames are missing, then
        # fail -- the hub's own report has the rest of the story.
        raise AssertionError(
            f"the network dropped {len(seen.dropped)} of {len(frames)} frames "
            f"({sorted(seen.dropped)}); the RX has no retransmission and cannot "
            "replay a lossy trace"
        )
    if len(arrivals) != len(frames):
        raise AssertionError(
            f"the hub delivered {len(arrivals)} frames, the TX declared {len(frames)}"
        )
    if client.eos.delivered != len(arrivals):
        raise AssertionError(
            f"the hub counted {client.eos.delivered} deliveries, "
            f"the RX received {len(arrivals)}"
        )
    for index, row in enumerate(arrivals):
        if row.sequence != index or row.frame_id in {r.frame_id for r in arrivals[:index]}:
            raise AssertionError(f"arrival {index} is out of order or repeated")
    # ---- every frame went in at the instant the network reported ----
    #
    # Two things can legitimately stand between the arrival instant and the
    # injection: the clock's own granularity (a target landing between two
    # edges is met at the next edge, up to one 2 ns cycle) and a lane that is
    # still pushing its previous frame.  The second one is real work by the
    # RTL, not drift, so it is measured and reported separately -- what the
    # gated clock has to rule out is delay with nothing behind it.
    slack = max(
        (actual - target for target, actual, _ in seen.injections.values()), default=0
    )
    contention = max(
        (lane_busy - target for target, _, lane_busy in seen.injections.values()
         if lane_busy is not None and lane_busy > target),
        default=0,
    )
    unexplained = max(
        (
            actual - max(target, lane_busy or 0)
            for target, actual, lane_busy in seen.injections.values()
        ),
        default=0,
    )
    dut._log.info(
        "RX replay: injection slack %d ps (lane contention %d, unexplained %d)",
        slack,
        contention,
        unexplained,
    )
    if unexplained > 2 * CLOCK_HALF_PS:
        worst = max(
            seen.injections.items(),
            key=lambda item: item[1][1] - max(item[1][0], item[1][2] or 0),
        )
        frame_id, (target, actual, lane_busy) = worst
        raise AssertionError(
            f"frame {frame_id} went in {actual - max(target, lane_busy or 0)} ps "
            f"after the earliest instant the replay allowed (arrival {target}, "
            f"lane free at {lane_busy}); the replay clock is not being gated"
        )

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
                    f"QPN {key[0]} PSN {key[1]:#x}: payload differs from the "
                    "declared oracle"
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
    lanes_used = sorted({row.channel for row in arrivals})
    if lanes_used != [0, 1, 2, 3]:
        raise AssertionError(f"the replay used lanes {lanes_used}, expected all of 0..3")

    stats = await bridge(client.finish)(len(arrivals))
    if stats.frames_ingested != len(arrivals):
        raise AssertionError(
            f"the hub acknowledges {stats.frames_ingested} frames ingested, "
            f"this stage received {len(arrivals)}"
        )

    report = {
        "stage": "rx-stream",
        "status": "PASS",
        "transport": "unix-socket",
        "socket": str(rx_socket_path()),
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
        "max_injection_slack_ps": slack,
        "lane_contention_ps": contention,
        "unexplained_slack_ps": unexplained,
        "rx_clock": "free-running" if FREE_RUNNING_CLOCK else "gated",
        "clocked_wait_ps": clocked - before,
        "bridged_wait_ps": stalled - clocked,
        "network_final_time_ps": client.eos.final_time_ps,
        "network_reorder_inversions": stats.reorder_inversions,
        "faulted": int(await tb.fault()),
    }
    artifact("online-rx.json").write_text(json.dumps(report, indent=2) + "\n")

    dut._log.info(
        "RX stream: %d frames, %d DMA writes, %d CQEs, retired=%d, reordered=%s, "
        "injection slack=%d ps (lane contention %d, unexplained %d)",
        len(arrivals),
        len(seen.dma),
        len(seen.cqes),
        head,
        report["reordered"],
        slack,
        contention,
        unexplained,
    )
    client.close()


def _arrival_reordered(arrivals: list[ArrivalRow]) -> bool:
    order = [row.tx_order for row in arrivals]
    return order != sorted(order)
