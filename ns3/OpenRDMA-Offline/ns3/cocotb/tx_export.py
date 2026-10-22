"""Stage 1 of the offline flow: run the TX RTL and export its frames.

This drives the real `mkTestM0Chain` (M0 -> M1 -> M2 -> M3) under Verilator
with a fixed workload, scores every emitted frame against the independent
Python models that live in `tx/integration/cocotb`, and writes four CSVs for
the downstream stages:

    tx-trace.csv       frame_id, eof_time_ps, channel, frame_hex
    tx-frames.csv      per-frame oracle (message, packet index, PSN, opcode, ...)
    tx-messages.csv    per-message oracle (address, key, length, payload)
    tx-connection.csv  the L2/L3 identity the DUT was configured with

`eof_time_ps` is the simulation time at which the frame's EOF word was taken
out of the M3 channel buffer.  That is the boundary handed to ns-3:
store-and-forward, not the SOF beat.  It is a simulation timestamp, so the
network stage replays the *inter-frame spacing* rather than the absolute time.

The workload is deliberately awkward in the ways the unit suites care about:
four QPs with four different MTUs, a PSN that wraps mid-message, a 64-byte
single-packet message, and a message large enough to need MIDDLE packets.
"""

from __future__ import annotations

import cocotb
from cocotb.queue import Queue
from cocotb.triggers import RisingEdge, with_timeout
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

from offline_lib import (
    Connection,
    FrameOracleRow,
    MessageOracleRow,
    TraceRow,
    artifact,
    write_connection,
    write_frame_oracle,
    write_message_oracle,
    write_trace,
)

# ---------------------------------------------------------------------------
# The offline workload: fixed, no RNG, identical on every run.
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


async def run_workload(tb: M0ChainTB, model: M0RefModel, observer) -> list:
    """Submit, service DMA, score every frame, and hand each one to `observer`."""
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
async def export_tx_trace(dut):
    """Run the offline workload and write the TX trace plus both oracles."""
    tb = M0ChainTB(dut)
    await tb.reset()

    model = M0RefModel()
    await bring_up(tb, model)

    trace: list[TraceRow] = []
    frame_rows: list[FrameOracleRow] = []

    def observe(row: TraceRow, message_id: int, packet, reference: bytes) -> None:
        trace.append(row)
        descriptor = packet.descriptor
        frame_rows.append(
            FrameOracleRow(
                frame_id=row.frame_id,
                message_id=message_id,
                packet_index=int(descriptor.packetIndex),
                packet_count=int(descriptor.packetCount),
                request_id=int(descriptor.requestId),
                qpn=int(descriptor.destQpn),
                psn=int(descriptor.psn),
                opcode=int(descriptor.opcode),
                payload_offset=int(descriptor.messageOffset),
                payload_length=int(descriptor.payloadLength),
                # The M3 model's bytes, not the RTL's: ns-3 compares the trace
                # against this column, and it must not be a copy of what the
                # RTL happened to emit (the equality assert above is what ties
                # them together).
                expected_frame=reference,
                channel=row.channel,
            )
        )

    expected = await run_workload(tb, model, observe)

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

    trace.sort(key=lambda row: (row.eof_time_ps, row.frame_id))
    frame_rows.sort(key=lambda row: row.frame_id)

    write_trace(artifact("tx-trace.csv"), trace)
    write_frame_oracle(artifact("tx-frames.csv"), frame_rows)
    write_message_oracle(artifact("tx-messages.csv"), message_rows)
    # The L2/L3 identity as declared to the DUT.  Every frame was compared
    # against `expected_frame(..., CFG)` above, so this file cannot disagree
    # with what went on the wire without the TX stage having failed first --
    # which is what makes it usable downstream as an independent expectation.
    write_connection(
        artifact("tx-connection.csv"),
        Connection(
            src_mac=int(CFG.srcMac),
            dst_mac=int(CFG.dstMac),
            src_ip=int(CFG.srcIp),
            dst_ip=int(CFG.dstIp),
        ),
    )

    channels = sorted({row.channel for row in trace})
    dut._log.info(
        "TX export: %d frames, %d messages, channels=%s, span=%d ps",
        len(trace),
        len(message_rows),
        channels,
        trace[-1].eof_time_ps - trace[0].eof_time_ps,
    )
