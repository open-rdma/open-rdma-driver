"""Client side of the hub protocol (see `ns3/online-cosim.cc`).

Both simulation stages speak this to the network hub over a Unix domain
socket.  The framing and the barrier (ADVANCE -> run -> RESULT) are the ones
`Ns3-lab/Lab7-OnlineSocketCosim` established; the online flow adds the messages
that make the RX a participant instead of a spectator:

    HELLO / HELLO_ACK     capabilities, both links
    DECLARE / DECLARE_ACK the TX's oracle: connection, frame table, message table
    ADVANCE / RESULT      the TX's virtual clock barrier, per window
    EVENT                 one delivery, pushed to the RX as it happens
    EOS                   no more events; carries the hub's final counters
    FINISH / FINISH_ACK   end of stream, both links

Message bodies are packed big-endian with `struct`; `online-cosim.cc` writes
and reads the same layouts by hand.  Every length is explicit, so a truncated
or oversized message is an error rather than a short read.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

from online_lib import Connection, FrameOracleRow, MessageOracleRow

MAGIC = 0x4E533743  # "NS7C"
VERSION = 1

HELLO = 1
HELLO_ACK = 2
ADVANCE = 3
RESULT = 4
FINISH = 5
FINISH_ACK = 6
ERROR = 7
DECLARE = 8
DECLARE_ACK = 9
EVENT = 10
EOS = 11

# NetworkEvent::Kind in four-path-roce.h.
DELIVERED = 1
DROPPED = 2

MAX_MESSAGE_BYTES = 16 * 1024 * 1024

HELLO_ACK_FORMAT = ">IHHQII4Q"
ADVANCE_PREFIX_FORMAT = ">QI"
ADVANCE_FRAME_FORMAT = ">QQB3xI"
EVENT_FORMAT = ">QQIBBHQI"
FINISH_ACK_FORMAT = ">QQQQQQQQIQ"
EOS_FORMAT = ">QQQQQ"


@dataclass(frozen=True)
class Capabilities:
    """What HELLO_ACK advertises about the network the hub built."""

    flags: int
    channels: int
    time_unit: int  # 1 == picoseconds
    link_bps: int
    max_frame_bytes: int
    max_batch_frames: int
    delays_ps: tuple[int, int, int, int]

    def drain_guard_ps(self, frames: list[bytes]) -> int:
        """How far past the last injection the client must advance to FINISH.

        This mirrors `FourPathRoceNetwork::DrainGuardPs`: the longest path
        delay plus the longest serialization time, so a frame injected last
        still meets its fate before the barrier stops.  It is computed from
        what the hub advertised rather than from a constant, because a hub
        configured with slower links needs a longer drain.
        """
        if not frames:
            return max(self.delays_ps) + 1_000
        serialized = [
            ((len(frame) + 2) * 8 * 1_000_000_000_000 + self.link_bps - 1)
            // self.link_bps
            for frame in frames
        ]
        return max(self.delays_ps) + max(serialized) + 1_000


@dataclass(frozen=True)
class DeliveryEvent:
    """One terminal event for one frame, as the hub reports it."""

    sequence: int
    frame_id: int
    tx_order: int
    kind: int
    channel: int
    reason: int
    time_ps: int
    frame: bytes

    @property
    def delivered(self) -> bool:
        return self.kind == DELIVERED


@dataclass(frozen=True)
class EndOfStream:
    """The hub's counters at the moment the RX's event stream ends."""

    submitted: int
    delivered: int
    dropped: int
    last_sequence: int
    final_time_ps: int


@dataclass(frozen=True)
class FinishStats:
    submitted: int
    delivered: int
    dropped: int
    last_sequence: int
    final_time_ps: int
    reorder_inversions: int
    reorder_max_depth: int
    messages_complete: int
    channels_used: int
    frames_ingested: int

    @property
    def channel_set(self) -> set[int]:
        return {index for index in range(4) if self.channels_used & (1 << index)}


@dataclass(frozen=True)
class Declaration:
    """The TX's oracle, relayed by the hub to the RX."""

    connection: Connection
    frames: list[FrameOracleRow]
    messages: list[MessageOracleRow]


def pack_declaration(
    connection: Connection,
    frames: list[FrameOracleRow],
    messages: list[MessageOracleRow],
) -> bytes:
    body = bytearray(
        struct.pack(
            ">QIQIII",
            connection.src_mac,
            connection.src_ip,
            connection.dst_mac,
            connection.dst_ip,
            len(frames),
            len(messages),
        )
    )
    for row in frames:
        body += struct.pack(
            ">QIHHIII",
            row.frame_id,
            row.message_id,
            row.packet_index,
            row.packet_count,
            row.request_id,
            row.qpn,
            row.psn,
        )
        body += struct.pack(
            ">B3xIII",
            row.opcode,
            row.payload_offset,
            row.payload_length,
            len(row.expected_frame),
        )
        body += row.expected_frame
    for row in messages:
        body += struct.pack(
            ">IIIIQIII",
            row.message_id,
            row.request_id,
            row.qpn,
            row.start_psn,
            row.remote_addr,
            row.remote_key,
            row.message_length,
            len(row.payload),
        )
        body += row.payload
    return bytes(body)


def unpack_declaration(body: bytes) -> Declaration:
    offset = 0

    def take(fmt: str) -> tuple:
        nonlocal offset
        size = struct.calcsize(fmt)
        if offset + size > len(body):
            raise RuntimeError("truncated DECLARE")
        values = struct.unpack_from(fmt, body, offset)
        offset += size
        return values

    def take_bytes(size: int) -> bytes:
        nonlocal offset
        if offset + size > len(body):
            raise RuntimeError("truncated DECLARE payload")
        chunk = body[offset : offset + size]
        offset += size
        return chunk

    src_mac, src_ip, dst_mac, dst_ip, frame_count, message_count = take(">QIQIII")
    connection = Connection(src_mac=src_mac, dst_mac=dst_mac, src_ip=src_ip, dst_ip=dst_ip)
    frames = []
    for _ in range(frame_count):
        (
            frame_id,
            message_id,
            packet_index,
            packet_count,
            request_id,
            qpn,
            psn,
        ) = take(">QIHHIII")
        opcode, payload_offset, payload_length, frame_length = take(">B3xIII")
        frames.append(
            FrameOracleRow(
                frame_id=frame_id,
                message_id=message_id,
                packet_index=packet_index,
                packet_count=packet_count,
                request_id=request_id,
                qpn=qpn,
                psn=psn,
                opcode=opcode,
                payload_offset=payload_offset,
                payload_length=payload_length,
                expected_frame=take_bytes(frame_length),
            )
        )
    messages = []
    for _ in range(message_count):
        (
            message_id,
            request_id,
            qpn,
            start_psn,
            remote_addr,
            remote_key,
            message_length,
            payload_length,
        ) = take(">IIIIQIII")
        messages.append(
            MessageOracleRow(
                message_id=message_id,
                request_id=request_id,
                qpn=qpn,
                start_psn=start_psn,
                remote_addr=remote_addr,
                remote_key=remote_key,
                message_length=message_length,
                payload=take_bytes(payload_length),
            )
        )
    if offset != len(body):
        raise RuntimeError("trailing bytes in DECLARE")
    return Declaration(connection=connection, frames=frames, messages=messages)


def _unpack_event(body: bytes) -> DeliveryEvent:
    if len(body) < struct.calcsize(EVENT_FORMAT):
        raise RuntimeError("truncated event")
    (
        sequence,
        frame_id,
        tx_order,
        kind,
        channel,
        reason,
        time_ps,
        frame_length,
    ) = struct.unpack_from(EVENT_FORMAT, body, 0)
    offset = struct.calcsize(EVENT_FORMAT)
    if offset + frame_length != len(body):
        raise RuntimeError("event payload length does not match its header")
    return DeliveryEvent(
        sequence=sequence,
        frame_id=frame_id,
        tx_order=tx_order,
        kind=kind,
        channel=channel,
        reason=reason,
        time_ps=time_ps,
        frame=body[offset:],
    )


class OnlineClient:
    """One end of a hub link (the TX stage uses one, the RX stage the other).

    Every method blocks; the cocotb stages call them through `cocotb.task.bridge`
    so the scheduler keeps running while they wait.
    """

    def __init__(self, path: str, timeout: float | None = 900.0) -> None:
        self._path = path
        self._timeout = timeout
        self._socket: socket.socket | None = None
        self._request_id = 0
        self.current_ps = 0
        self.capabilities: Capabilities | None = None
        self.eos: EndOfStream | None = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> Capabilities:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        sock.connect(self._path)
        self._socket = sock
        self._send(HELLO, struct.pack(">I", 0))
        message_type, request_id, body = self._receive()
        if message_type != HELLO_ACK or request_id != 0:
            raise RuntimeError("invalid HELLO_ACK")
        if len(body) != struct.calcsize(HELLO_ACK_FORMAT):
            raise RuntimeError("invalid HELLO_ACK length")
        flags, channels, unit, link_bps, max_frame, max_batch, *delays = struct.unpack(
            HELLO_ACK_FORMAT, body
        )
        if flags != 0 or unit != 1:
            raise RuntimeError(f"unsupported hub capabilities (flags={flags}, unit={unit})")
        if channels != 4:
            raise RuntimeError(f"the four-path topology has four channels, hub says {channels}")
        if max_frame < 4096 or max_batch < 16 or link_bps <= 0:
            raise RuntimeError("implausible hub capabilities")
        self.capabilities = Capabilities(
            flags=flags,
            channels=channels,
            time_unit=unit,
            link_bps=link_bps,
            max_frame_bytes=max_frame,
            max_batch_frames=max_batch,
            delays_ps=tuple(delays),
        )
        self._request_id = 1
        return self.capabilities

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    # -- TX side -----------------------------------------------------------

    def declare(
        self,
        connection: Connection,
        frames: list[FrameOracleRow],
        messages: list[MessageOracleRow],
    ) -> None:
        """Hand the hub the oracle it will check every injection against."""
        body = pack_declaration(connection, frames, messages)
        request_id = self._request_id
        self._request_id += 1
        self._send(DECLARE, body, request_id)
        message_type, response_id, _ = self._receive()
        if message_type != DECLARE_ACK or response_id != request_id:
            raise RuntimeError("invalid DECLARE_ACK")

    def advance(self, target_ps: int, frames) -> list[DeliveryEvent]:
        """Run the network up to `target_ps`, injecting `frames` on the way."""
        if target_ps <= self.current_ps:
            raise ValueError("target_ps must advance time")
        ordered = sorted(frames, key=lambda item: (item.eof_time_ps, item.frame_id))
        body = bytearray(struct.pack(ADVANCE_PREFIX_FORMAT, target_ps, len(ordered)))
        previous = None
        for item in ordered:
            order = (item.eof_time_ps, item.frame_id)
            if previous is not None and order <= previous:
                raise ValueError(f"frame batch is not strictly ordered at {order}")
            if not self.current_ps <= item.eof_time_ps <= target_ps:
                raise ValueError(
                    f"frame {item.frame_id} at {item.eof_time_ps} ps is outside "
                    f"the advance window ({self.current_ps}, {target_ps}]"
                )
            if not 0 <= item.channel < 4:
                raise ValueError(f"frame {item.frame_id} has channel {item.channel}")
            body += struct.pack(
                ADVANCE_FRAME_FORMAT,
                item.frame_id,
                item.eof_time_ps,
                item.channel,
                len(item.frame),
            )
            body += item.frame
            previous = order

        request_id = self._request_id
        self._request_id += 1
        self._send(ADVANCE, bytes(body), request_id)
        message_type, response_id, response = self._receive()
        if message_type != RESULT or response_id != request_id:
            raise RuntimeError("invalid ADVANCE response")

        if len(response) < struct.calcsize(ADVANCE_PREFIX_FORMAT):
            raise RuntimeError("truncated RESULT")
        watermark, count = struct.unpack_from(ADVANCE_PREFIX_FORMAT, response, 0)
        offset = struct.calcsize(ADVANCE_PREFIX_FORMAT)
        events = []
        for _ in range(count):
            size = struct.calcsize(EVENT_FORMAT)
            if offset + size > len(response):
                raise RuntimeError("truncated RESULT event")
            frame_length = struct.unpack_from(">I", response, offset + size - 4)[0]
            if offset + size + frame_length > len(response):
                raise RuntimeError("truncated RESULT frame")
            events.append(_unpack_event(response[offset : offset + size + frame_length]))
            offset += size + frame_length
        if offset != len(response):
            raise RuntimeError("trailing RESULT bytes")
        if watermark != target_ps:
            raise RuntimeError(
                f"hub stopped at {watermark} ps instead of the requested {target_ps} ps"
            )
        self.current_ps = watermark
        return events

    # -- RX side -----------------------------------------------------------

    def receive_declaration(self) -> Declaration:
        """Wait for the oracle relay, then acknowledge it."""
        message_type, _, body = self._receive()
        if message_type != DECLARE:
            raise RuntimeError(f"expected DECLARE, hub sent message type {message_type}")
        declaration = unpack_declaration(body)
        self._send(DECLARE_ACK, b"", self._request_id)
        self._request_id += 1
        return declaration

    def receive_event(self) -> DeliveryEvent | None:
        """The next arrival, or None once the hub has sent EOS.

        The end-of-stream counters are left in `self.eos`; the RX counts what
        it actually ingested and sends that back in FINISH, so the two sides
        have to agree.
        """
        while True:
            message_type, _, body = self._receive()
            if message_type == EVENT:
                return _unpack_event(body)
            if message_type == EOS:
                if len(body) != struct.calcsize(EOS_FORMAT):
                    raise RuntimeError("invalid EOS length")
                self.eos = EndOfStream(*struct.unpack(EOS_FORMAT, body))
                return None
            raise RuntimeError(f"unexpected message type {message_type} on the RX link")

    # -- both sides --------------------------------------------------------

    def finish(self, frames_ingested: int | None = None) -> FinishStats:
        # The two stages say different things when they stop: the TX has
        # nothing left to report (the hub already holds every frame it
        # submitted), while the RX states how many frames it actually took in,
        # which the hub checks against its own delivery count.  The empty body
        # is the TX's, so `None` -- not 0 -- is what "no count" means here.
        body = b"" if frames_ingested is None else struct.pack(">Q", frames_ingested)
        request_id = self._request_id
        self._request_id += 1
        self._send(FINISH, body, request_id)
        message_type, response_id, response = self._receive()
        if message_type != FINISH_ACK or response_id != request_id:
            raise RuntimeError("invalid FINISH response")
        if len(response) != struct.calcsize(FINISH_ACK_FORMAT):
            raise RuntimeError("invalid FINISH_ACK length")
        return FinishStats(*struct.unpack(FINISH_ACK_FORMAT, response))

    # -- framing -----------------------------------------------------------

    def _send(self, message_type: int, body: bytes, request_id: int | None = None) -> None:
        if self._socket is None:
            raise RuntimeError("socket is not connected")
        if request_id is None:
            request_id = self._request_id
        payload = struct.pack(">IHHI", MAGIC, VERSION, message_type, request_id) + body
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("protocol message is too large")
        self._socket.sendall(struct.pack(">I", len(payload)) + payload)

    def _receive(self) -> tuple[int, int, bytes]:
        length = struct.unpack(">I", self._read_exact(4))[0]
        if not 12 <= length <= MAX_MESSAGE_BYTES:
            raise RuntimeError(f"invalid protocol message length {length}")
        payload = self._read_exact(length)
        magic, version, message_type, request_id = struct.unpack_from(">IHHI", payload)
        if magic != MAGIC or version != VERSION:
            raise RuntimeError("invalid protocol header")
        body = payload[12:]
        if message_type == ERROR:
            if len(body) < 8:
                raise RuntimeError("malformed hub error")
            code, text_length = struct.unpack_from(">II", body)
            text = body[8 : 8 + text_length].decode("ascii", errors="replace")
            raise RuntimeError(f"hub error {code}: {text}")
        return message_type, request_id, body

    def _read_exact(self, size: int) -> bytes:
        if self._socket is None:
            raise RuntimeError("socket is not connected")
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self._socket.recv(size - len(chunks))
            if not chunk:
                raise RuntimeError("hub closed the socket before the message completed")
            chunks += chunk
        return bytes(chunks)
