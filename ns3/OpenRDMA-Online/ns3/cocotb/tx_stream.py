"""TX stage of the online flow: run the RTL and stream its frames to the hub.

This drives the same `mkTestM0Chain` workload as the offline tree's
`tx_export.py` and scores every emitted frame against the same independent
Python models -- the frames, the workload and the checks are identical.  What
changes is where the frames go:

  * before the workload starts, the stage declares its oracle (the connection,
    the frame table and the message table) to the hub over the socket.  That
    declaration is built from the models, never from the RTL, so the hub and
    the RX keep checking the DUT against something the DUT did not produce;
  * while the workload runs, every frame is handed to the hub in an ADVANCE
    window, timestamped with the simulation time at which it left M3.  The hub
    runs the four-path network up to the window's watermark and answers with
    the frames' fates, which this test checks and writes to `online-tx.json`.

The ADVANCE boundary is the online replacement for the offline tx-trace.csv:
the same (frame_id, eof_time_ps, channel, bytes) tuple, carried by a message
instead of a file, and consumed while the TX simulation is still running.
"""

from __future__ import annotations

import json
import os

import cocotb
from cocotb.queue import Queue
from cocotb.task import bridge
from cocotb.triggers import ReadOnly, RisingEdge, Timer, with_timeout
from cocotb.utils import get_sim_time
from cocotb_bsv import PipeOut

from bfm.m3 import collect_commit, configure, pipe_get
from dma_phasea_model import DmaMessageDone, DmaReadRequest, completion_beats
from m0_chain_bfm import (
    M0ChainTB,
    M0Submission,
    configure_mr,
    configure_pte,
    configure_qp,
    configure_slot,
    expect_message,
)
from m0_chain_model import (
    DecodedGdrpSubmission,
    M0RefModel,
    MrEntry,
    PayloadSlot,
    PteEntry,
    QpEntry,
    make_local_lkey,
    make_token,
)
from model.m3 import EthPacket, MacIpConfig, expected_frame, frame_word_bytes

from online_lib import (
    Connection,
    FrameOracleRow,
    MessageOracleRow,
    TraceRow,
    artifact,
    tx_socket_path,
)
from online_protocol import OnlineClient

# ---------------------------------------------------------------------------
# The workload: fixed, no RNG, identical on every run and to the offline tree.
# ---------------------------------------------------------------------------

CFG = MacIpConfig(
    srcMac=0x020000000001,
    dstMac=0x020000000002,
    srcIp=0x0A000001,
    dstIp=0x0A000002,
)

VIRT_BASE = 0x0000_0000_1000_0000
REMOTE_BASE = 0x0000_2000_0000_0000
MEM_BASE = 0x0000_0080_0000_0000
REMOTE_KEY = 0x5566_7788
KEY_PART = 7
MR_INDEX = 0
PAGE_BYTES = 4096
SLOT_BYTES = 0x4000

# QPN -> PMTU code; 0/1/2/4 are the 256/512/1024/4096 byte MTUs.
QP_PMTU = {0: 0, 1: 1, 2: 2, 3: 4}
# QPN -> starting PSN.  QPN 0 wraps its 24-bit PSN across its two messages.
QP_START_PSN = {0: 0xFFFFFE, 1: 0x123400, 2: 0x20, 3: 0x800000}
# slot id -> local offset; each slot also owns a distinct remote base.
SLOTS = {0: 0x0000, 1: 0x4000, 2: 0x8000, 3: 0xC000}

SUBMISSIONS = [
    M0Submission(qpn=0, slot_id=0, length=700, debug_seq=0),
    M0Submission(qpn=1, slot_id=1, length=985, debug_seq=1),
    M0Submission(qpn=2, slot_id=2, length=128, debug_seq=2),
    M0Submission(qpn=0, slot_id=0, length=64, debug_seq=3),
    M0Submission(qpn=3, slot_id=3, length=5000, debug_seq=4),
    M0Submission(qpn=1, slot_id=1, length=484, debug_seq=5),
]

# bfm.m3.pipe_get defaults to a 20 000 cycle budget, which the larger messages
# legitimately exceed; m0_chain_bfm raises it to 8 ms of simulated time and the
# same budget applies here.
_GET_CYCLES = 4_000_000

# Online pacing.  The window is how much simulation time may pass between two
# ADVANCE barriers; the resolution is one TX clock cycle, and a frame is only
# offered to the hub once its EOF timestamp is that far behind the simulation
# clock, so a window never claims a frame that the RTL has not finished
# handing over yet.
#
# ONLINE_TX_WINDOW_PS overrides the window: it is the one pacing parameter
# worth trading off (fewer, larger windows mean fewer round trips but later
# frames), and the tutorial's second experiment measures that trade.
WINDOW_PS = int(os.environ.get("ONLINE_TX_WINDOW_PS") or 100_000)
CLOCK_PS = 2_000


async def _pipe_get(pipe: PipeOut, clock) -> int:
    return await pipe_get(pipe, clock, cycles=_GET_CYCLES)


async def bring_up(tb: M0ChainTB, model: M0RefModel) -> None:
    """Configure the MR/PTE/QP/slot tables the workload depends on."""
    span = max(offset + SLOT_BYTES for offset in SLOTS.values())
    pte_count = (span + PAGE_BYTES - 1) // PAGE_BYTES

    await configure_mr(
        tb,
        model,
        MR_INDEX,
        MrEntry(
            valid=1,
            keyPart=KEY_PART,
            allowLocalRead=1,
            baseVa=VIRT_BASE,
            length=span,
            pgtOffset=0,
            pageShift=12,
        ),
    )
    for index in range(pte_count):
        await configure_pte(
            tb,
            model,
            index,
            PteEntry(valid=1, physicalPageBase=MEM_BASE + index * PAGE_BYTES),
        )
    for qpn, pmtu in QP_PMTU.items():
        await configure_qp(
            tb,
            model,
            qpn,
            QpEntry(valid=1, pmtu=pmtu, initialPsn=QP_START_PSN[qpn]),
        )
    for slot_id, offset in SLOTS.items():
        await configure_slot(
            tb,
            model,
            slot_id,
            PayloadSlot(
                valid=1,
                allowWriteRelaxed=1,
                localLkey=make_local_lkey(mr_index=MR_INDEX, key_part=KEY_PART),
                localOffset=offset,
                remoteBase=REMOTE_BASE + slot_id * 0x1_0000_0000,
                maxLen=SLOT_BYTES,
                remoteKey=REMOTE_KEY,
            ),
        )


async def collect_frame_timed(tb: M0ChainTB, channel: int) -> tuple[bytes, int]:
    """Drain one frame from a channel buffer; timestamp the EOF dequeue."""
    data = bytearray()
    first_word: EthPacket | None = None
    while True:
        word = EthPacket.unpack(await _pipe_get(tb.channels[channel], tb.clock))
        if first_word is None:
            first_word = word
            assert int(word.sof) == 1, "first word of a frame must have sof set"
        else:
            assert int(word.sof) == 0, "only the first word of a frame has sof set"
        data += frame_word_bytes(word)
        if int(word.eof):
            break
    assert first_word is not None
    return bytes(data), int(get_sim_time("ps"))


async def run_workload(tb: M0ChainTB, model: M0RefModel, observer, on_oracle=None) -> list:
    """Submit, service DMA, score every frame, and hand each one to `observer`.

    `on_oracle`, when given, is awaited with the finished expectation tables
    before the first submission is queued: it is the hook the online flow uses
    to publish the oracle, and it has to sit here rather than in the caller
    because asking the reference model for a submission is what advances it --
    a second pass over the same model would describe the *next* six messages.
    """
    await configure(tb, CFG, channel_enable=0xF)

    expected = [expect_message(model, spec) for spec in SUBMISSIONS]
    packets = [
        (message_id, packet)
        for message_id, message in enumerate(expected)
        for packet in message.packets
    ]
    expected_frames = [
        expected_frame(packet.descriptor, packet.payload, CFG) for _, packet in packets
    ]
    if on_oracle is not None:
        await on_oracle(expected, packets, expected_frames)

    async def produce() -> None:
        for spec in SUBMISSIONS:
            token = make_token(
                slot_id=spec.slot_id, length=spec.length, debug_seq=spec.debug_seq
            )
            await tb.submission.enq(
                DecodedGdrpSubmission(qpn=spec.qpn, token=token).pack()
            )

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
                    chunk = chunks[group_index + local_index]
                    assert int(actual.physAddr) == chunk.physical_addr, request_index
                    assert int(actual.len) == chunk.length, request_index
                    batch.append(actual)
                group_index += batch_count
                request_index += batch_count
                # Reverse each batch, exactly like the m0chain suite: cross-tag
                # completion reorder must not perturb the frame stream.
                for request in reversed(batch):
                    for beat in completion_beats(request):
                        await tb.completion.enq(beat.pack())

    async def collect_dones() -> None:
        for index, message in enumerate(expected):
            actual = DmaMessageDone.unpack(await _pipe_get(tb.done, tb.clock))
            assert int(actual.wqeTag) == int(message.context.requestId), index
            assert int(actual.length) == len(message.payload), index
            assert int(actual.chunkCount) == len(message.chunks), index

    channels_used: set[int] = set()

    async def collect_tx() -> None:
        queues: list[Queue] = [Queue() for _ in range(4)]

        async def channel_worker(channel: int) -> None:
            while True:
                item = await queues[channel].get()
                if item is None:
                    return
                index, message_id, packet, want = item
                frame, eof_time_ps = await collect_frame_timed(tb, channel)
                assert frame == want.data, (
                    f"frame[{index}] differs from the M3 model\n"
                    f"expected={want.data.hex()}\nactual={frame.hex()}"
                )
                observer(
                    TraceRow(
                        frame_id=index,
                        eof_time_ps=eof_time_ps,
                        channel=channel,
                        frame=frame,
                    ),
                    message_id,
                    packet,
                    want.data,
                )

        workers = [cocotb.start_soon(channel_worker(channel)) for channel in range(4)]
        for index, (message_id, packet) in enumerate(packets):
            commit = await collect_commit(tb)
            descriptor = packet.descriptor
            assert int(commit.requestId) == int(descriptor.requestId), index
            assert int(commit.qpn) == int(descriptor.qpn), index
            assert int(commit.psn) == int(descriptor.psn), index
            assert int(commit.packetIndex) == int(descriptor.packetIndex), index
            channel = int(commit.channel)
            assert channel < 4, f"commit[{index}] chose channel {channel}"
            channels_used.add(channel)
            await queues[channel].put((index, message_id, packet, expected_frames[index]))

        for queue in queues:
            await queue.put(None)
        for worker in workers:
            await worker

    async def wait_all() -> None:
        for task in (
            cocotb.start_soon(produce()),
            cocotb.start_soon(service_dma()),
            cocotb.start_soon(collect_dones()),
            cocotb.start_soon(collect_tx()),
        ):
            await task

    await with_timeout(wait_all(), 3_000_000, "ns")

    for _ in range(16):
        await RisingEdge(tb.clock)
        assert not await tb.dma_request.not_empty(), "trailing DMA request"
        assert not await tb.done.not_empty(), "trailing DMA done"
        assert not await tb.commits.not_empty(), "trailing TxCommit"
        for channel in tb.channels:
            assert not await channel.not_empty(), "trailing EthPacket"

    assert packets, "the workload produced no packets"
    # The four-channel scheduler is the whole point of the four-path network
    # downstream: if the RTL stopped spreading traffic over all four enabled
    # channels, every lane-level result after this point would be vacuous.
    assert channels_used == {0, 1, 2, 3}, (
        f"the scheduler used channels {sorted(channels_used)}, expected all of "
        "0..3 (four-path experiment would degenerate)"
    )
    return expected


@cocotb.test(timeout_time=3_000_000, timeout_unit="ns")
async def stream_tx_to_hub(dut):
    """Run the TX workload and stream its frames into the network hub."""
    tb = M0ChainTB(dut)
    await tb.reset()

    model = M0RefModel()
    await bring_up(tb, model)

    # The L2/L3 identity as declared to the DUT.  Every frame is compared
    # against `expected_frame(..., CFG)` inside run_workload, so this cannot
    # disagree with what goes on the wire without the TX stage having failed
    # first -- which is what makes it usable as an independent expectation
    # downstream.
    connection = Connection(
        src_mac=int(CFG.srcMac),
        dst_mac=int(CFG.dstMac),
        src_ip=int(CFG.srcIp),
        dst_ip=int(CFG.dstIp),
    )

    # Socket calls run through `cocotb.task.bridge`.  A bridged blocking call
    # holds the scheduler while it runs, so the simulation does not advance
    # during a round trip: the window loop's own `Timer` waits are what move
    # simulation time, and the watermark is taken from the time they reach.
    client = OnlineClient(str(tx_socket_path()))
    capabilities = await bridge(client.connect)()
    dut._log.info(
        "TX stream: hub link up (link=%d bps, delays=%s ps)",
        capabilities.link_bps,
        list(capabilities.delays_ps),
    )

    trace: list[TraceRow] = []
    declared: list = []

    def observe(row: TraceRow, message_id: int, packet, reference: bytes) -> None:
        # `reference` is the model's frame; run_workload has already asserted
        # the RTL emitted exactly those bytes.
        trace.append(row)

    async def declare_oracle(expected, packets, expected_frames) -> None:
        """Publish the oracle the moment the models have produced it.

        Frame ids are packet indices: the workload submits messages in order
        and walks each message's packets in order, so the n-th frame the RTL
        emits is the n-th packet of the flattened list, and the hub can hold
        this table while the frames arrive one by one.
        """
        frame_rows = []
        for frame_id, (message_id, packet) in enumerate(packets):
            descriptor = packet.descriptor
            frame_rows.append(
                FrameOracleRow(
                    frame_id=frame_id,
                    message_id=message_id,
                    packet_index=int(descriptor.packetIndex),
                    packet_count=int(descriptor.packetCount),
                    request_id=int(descriptor.requestId),
                    qpn=int(descriptor.destQpn),
                    psn=int(descriptor.psn),
                    opcode=int(descriptor.opcode),
                    payload_offset=int(descriptor.messageOffset),
                    payload_length=int(descriptor.payloadLength),
                    # The M3 model's bytes, not the RTL's: the hub compares
                    # every injected frame against this column, and it must not
                    # be a copy of what the RTL happened to emit (the equality
                    # assert in run_workload is what ties the two together).
                    expected_frame=expected_frames[frame_id].data,
                )
            )
        message_rows = []
        for message_id, message in enumerate(expected):
            context = message.context
            message_rows.append(
                MessageOracleRow(
                    message_id=message_id,
                    request_id=int(context.requestId),
                    qpn=int(context.destQpn),
                    start_psn=int(context.startPsn),
                    remote_addr=int(context.remoteAddr),
                    remote_key=int(context.remoteKey),
                    message_length=int(context.len),
                    payload=message.payload,
                )
            )
        # Recorded only once the hub has acknowledged it: the window loop below
        # waits on `declared`, and the two socket calls must not be in flight
        # at the same time -- they share one link, and the hub checks the
        # request IDs are contiguous.
        await bridge(client.declare)(connection, frame_rows, message_rows)
        declared.append((frame_rows, message_rows))

    events = []
    windows = 0
    sent = 0
    scenario = cocotb.start_soon(run_workload(tb, model, observe, declare_oracle))
    while not scenario.done():
        await Timer(WINDOW_PS, unit="ps")
        if not declared:
            # The oracle is still on its way; the hub refuses an ADVANCE that
            # arrives before the table it would be checked against.
            continue
        await ReadOnly()
        watermark = max(0, int(get_sim_time("ps")) - CLOCK_PS)
        ready = [row for row in trace[sent:] if row.eof_time_ps <= watermark]
        sent += len(ready)
        assert sent == len(trace) or trace[sent].eof_time_ps > watermark, (
            "frames are not offered to the hub in transmission order"
        )
        target = max(watermark, ready[-1].eof_time_ps + 1) if ready else watermark
        target = max(target, client.current_ps + 1)
        events += await bridge(client.advance)(target, ready)
        windows += 1
    # Let the workload's own assertions surface (trailing queue checks, the
    # four-channel check, or the model mismatch that killed a channel worker).
    await scenario

    # Flush whatever the last window did not claim, then run the network past
    # the last frame by the drain guard so every frame meets its fate before
    # the FINISH barrier: the hub refuses to close while a frame is in flight.
    remaining = trace[sent:]
    assert remaining == sorted(remaining, key=lambda row: (row.eof_time_ps, row.frame_id))
    drain_target = client.current_ps + 1
    if remaining:
        drain_target = max(drain_target, remaining[-1].eof_time_ps + 1)
    drain_target += capabilities.drain_guard_ps([row.frame for row in trace])
    events += await bridge(client.advance)(drain_target, remaining)
    sent = len(trace)

    # ---- what the hub says happened to every frame -------------------------
    frame_rows, message_rows = declared[0]
    oracle = {row.frame_id: row for row in frame_rows}
    if len(trace) != len(frame_rows):
        raise AssertionError(
            f"the RTL emitted {len(trace)} frames, the model declares {len(frame_rows)}"
        )
    by_id = {row.frame_id: row for row in trace}
    seen: set[int] = set()
    dropped: dict[int, int] = {}
    for index, event in enumerate(events):
        if event.sequence != index:
            raise AssertionError(
                f"event {index} carries sequence {event.sequence}; the hub's "
                "delivery sequence must be contiguous"
            )
        if not event.delivered:
            # A drop is only legal when the hub was started in lossy mode, and
            # the hub is the one holding that flag: it fails the run itself
            # when a lossless run drops a frame.  Recording it here instead of
            # raising keeps the handshake alive, so the hub still writes the
            # report that says which frames went missing.
            dropped[event.frame_id] = event.reason
            continue
        if event.frame_id in seen:
            raise AssertionError(f"frame {event.frame_id} was delivered twice")
        seen.add(event.frame_id)
        row = by_id[event.frame_id]
        if event.channel != row.channel:
            raise AssertionError(
                f"frame {event.frame_id} left on channel {row.channel} and "
                f"arrived on channel {event.channel}"
            )
        if event.time_ps < row.eof_time_ps:
            raise AssertionError(
                f"frame {event.frame_id} arrived at {event.time_ps} ps, "
                f"before it was transmitted at {row.eof_time_ps} ps"
            )
        if event.frame != oracle[event.frame_id].expected_frame:
            raise AssertionError(
                f"frame {event.frame_id} changed in the network "
                f"({len(event.frame)} bytes back, "
                f"{len(oracle[event.frame_id].expected_frame)} bytes declared)"
            )
    if seen != set(oracle) - set(dropped):
        raise AssertionError(f"frames {sorted(set(oracle) - seen)} never came back")

    stats = await bridge(client.finish)()
    if stats.submitted != len(trace):
        raise AssertionError(
            f"the hub submitted {stats.submitted} of {len(trace)} frames"
        )
    if stats.delivered != len(seen) or stats.dropped != len(dropped):
        raise AssertionError(
            f"the hub counted {stats.delivered} deliveries and {stats.dropped} "
            f"drops, this stage saw {len(seen)} and {len(dropped)}"
        )
    if stats.channel_set != {0, 1, 2, 3}:
        raise AssertionError(
            f"the network carried channels {sorted(stats.channel_set)}, "
            "expected all of 0..3"
        )
    if not dropped and stats.messages_complete != len(message_rows):
        raise AssertionError(
            f"{stats.messages_complete} of {len(message_rows)} messages reassembled"
        )

    report = {
        "stage": "tx-stream",
        "status": "PASS (lossy)" if dropped else "PASS",
        "transport": "unix-socket",
        "socket": str(tx_socket_path()),
        "frames": len(trace),
        "messages": len(message_rows),
        "delivered": len(seen),
        "dropped": len(dropped),
        "messages_complete": stats.messages_complete,
        "channels": sorted({row.channel for row in trace}),
        "window_ps": WINDOW_PS,
        "advances": windows + 1,
        "tx_span_ps": trace[-1].eof_time_ps - trace[0].eof_time_ps,
        "final_time_ps": stats.final_time_ps,
        "reorder_inversions": stats.reorder_inversions,
        "reorder_max_depth": stats.reorder_max_depth,
    }
    artifact("online-tx.json").write_text(json.dumps(report, indent=2) + "\n")

    dut._log.info(
        "TX stream: %d frames, %d messages, channels=%s, %d events checked%s",
        len(trace),
        len(message_rows),
        report["channels"],
        len(events),
        f", dropped={sorted(dropped)}" if dropped else "",
    )
    client.close()
