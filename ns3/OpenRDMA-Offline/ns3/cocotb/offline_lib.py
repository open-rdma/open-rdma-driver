"""Shared helpers for the offline TX -> ns-3 -> RX flow.

The flow is three processes that only talk through CSV artifacts:

    tx/  (Verilator + cocotb)   writes  results/tx-trace.csv
                                        results/tx-frames.csv
                                        results/tx-messages.csv
                                        results/tx-connection.csv
    ns3/ (offline-txrx)         reads   results/tx-*.csv
                                writes  results/rx-trace.csv
                                        results/ns3-offline.json
    rx/  (Verilator + cocotb)   reads   results/rx-trace.csv
                                        results/tx-frames.csv
                                        results/tx-messages.csv
                                        results/tx-connection.csv
                                writes  results/rx-replay.json

Everything is deterministic: the TX workload is fixed, the ns-3 network is a
deterministic four-path point-to-point topology, and the RX replay is driven by
the arrival trace rather than by wall-clock time.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Tree layout
# --------------------------------------------------------------------------

NS3_ROOT = Path(__file__).resolve().parents[1]
TREE_ROOT = NS3_ROOT.parent
TX_ROOT = TREE_ROOT / "tx"
RX_ROOT = TREE_ROOT / "rx"


def results_dir() -> Path:
    """Where the three stages exchange CSVs.

    OFFLINE_RESULTS overrides it; the default is <tree>/results, shared by
    every stage so a run is reproducible from one directory listing.
    """
    override = os.environ.get("OFFLINE_RESULTS")
    path = Path(override) if override else TREE_ROOT / "results"
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact(name: str) -> Path:
    path = results_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# CSV records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceRow:
    """One transmitted frame, as exported by the TX run."""

    frame_id: int
    eof_time_ps: int
    channel: int
    frame: bytes


@dataclass(frozen=True)
class ArrivalRow:
    """One delivered frame, as exported by the ns-3 run."""

    sequence: int
    frame_id: int
    tx_order: int
    channel: int
    arrival_time_ps: int
    frame: bytes


@dataclass(frozen=True)
class FrameOracleRow:
    frame_id: int
    message_id: int
    packet_index: int
    packet_count: int
    request_id: int
    qpn: int
    psn: int
    opcode: int
    payload_offset: int
    payload_length: int
    expected_frame: bytes
    # The egress channel the TX scheduler picked.  The TX test asserts the
    # commit stream spread over all four enabled channels; carrying the value
    # here lets ns-3 check that the trace it injects still says the same thing,
    # so a trace where the channels were flattened cannot pass unnoticed.
    channel: int


@dataclass(frozen=True)
class Connection:
    """The L2/L3 identity of the connection, as the TX declares it.

    This is the transmitter's view: `src_*` is the TX, `dst_*` is the RX.  The
    RX stage configures itself from this file rather than from the frames it is
    about to receive, so its address filter is a real check instead of a
    restatement of its input.
    """

    src_mac: int
    dst_mac: int
    src_ip: int
    dst_ip: int

    def as_rx_view(self) -> tuple[int, int, int, int]:
        """(localMac, localIp, peerMac, peerIp) for RxNetworkConfig."""
        return self.dst_mac, self.dst_ip, self.src_mac, self.src_ip


@dataclass(frozen=True)
class MessageOracleRow:
    message_id: int
    request_id: int
    qpn: int
    start_psn: int
    remote_addr: int
    remote_key: int
    message_length: int
    payload: bytes


def _read_rows(path: Path, header: tuple[str, ...]) -> list[list[str]]:
    with path.open(newline="") as stream:
        reader = csv.reader(stream)
        rows = [row for row in reader if row and not row[0].startswith("#")]
    if not rows:
        raise RuntimeError(f"{path}: no CSV rows")
    if tuple(rows[0]) != header:
        raise RuntimeError(f"{path}: unexpected header {rows[0]!r}, want {list(header)!r}")
    body = rows[1:]
    for index, row in enumerate(body, start=2):
        if len(row) != len(header):
            raise RuntimeError(f"{path}:{index}: expected {len(header)} fields, got {len(row)}")
    # Keyed on the header tuples themselves rather than on hand-copied strings:
    # a literal key silently stops matching the moment a column is added to the
    # header above it.
    dispatch = {
        TRACE_HEADER: _parse_trace,
        ARRIVAL_HEADER: _parse_arrival,
        FRAME_ORACLE_HEADER: _parse_frame_oracle,
        MESSAGE_ORACLE_HEADER: _parse_message_oracle,
        CONNECTION_HEADER: _parse_connection,
    }
    parser = dispatch.get(tuple(header))
    if parser is None:
        raise RuntimeError(f"{path}: no parser for header {header!r}")
    return [parser(row) for row in body]


def _parse_trace(row: list[str]) -> TraceRow:
    return TraceRow(
        frame_id=int(row[0]),
        eof_time_ps=int(row[1]),
        channel=int(row[2]),
        frame=bytes.fromhex(row[3]),
    )


def _parse_arrival(row: list[str]) -> ArrivalRow:
    return ArrivalRow(
        sequence=int(row[0]),
        frame_id=int(row[1]),
        tx_order=int(row[2]),
        channel=int(row[3]),
        arrival_time_ps=int(row[4]),
        frame=bytes.fromhex(row[5]),
    )


def _parse_frame_oracle(row: list[str]) -> FrameOracleRow:
    return FrameOracleRow(
        frame_id=int(row[0]),
        message_id=int(row[1]),
        packet_index=int(row[2]),
        packet_count=int(row[3]),
        request_id=int(row[4]),
        qpn=int(row[5]),
        psn=int(row[6]),
        opcode=int(row[7]),
        payload_offset=int(row[8]),
        payload_length=int(row[9]),
        expected_frame=bytes.fromhex(row[10]),
        channel=int(row[11]),
    )


def _parse_connection(row: list[str]) -> Connection:
    return Connection(
        src_mac=int(row[0], 16),
        src_ip=int(row[1], 16),
        dst_mac=int(row[2], 16),
        dst_ip=int(row[3], 16),
    )


def _parse_message_oracle(row: list[str]) -> MessageOracleRow:
    return MessageOracleRow(
        message_id=int(row[0]),
        request_id=int(row[1]),
        qpn=int(row[2]),
        start_psn=int(row[3]),
        remote_addr=int(row[4]),
        remote_key=int(row[5]),
        message_length=int(row[6]),
        payload=bytes.fromhex(row[7]),
    )


TRACE_HEADER = ("frame_id", "eof_time_ps", "channel", "frame_hex")
ARRIVAL_HEADER = ("sequence", "frame_id", "tx_order", "channel", "arrival_time_ps", "frame_hex")
FRAME_ORACLE_HEADER = (
    "frame_id",
    "message_id",
    "packet_index",
    "packet_count",
    "request_id",
    "qpn",
    "psn",
    "opcode",
    "payload_offset",
    "payload_length",
    "expected_frame_hex",
    "channel",
)
CONNECTION_HEADER = ("src_mac", "src_ip", "dst_mac", "dst_ip")
MESSAGE_ORACLE_HEADER = (
    "message_id",
    "request_id",
    "qpn",
    "start_psn",
    "remote_addr",
    "remote_key",
    "message_length",
    "expected_payload_hex",
)


def read_trace(path: Path) -> list[TraceRow]:
    return _read_rows(path, TRACE_HEADER)


def read_arrivals(path: Path) -> list[ArrivalRow]:
    return _read_rows(path, ARRIVAL_HEADER)


def read_frame_oracle(path: Path) -> list[FrameOracleRow]:
    return _read_rows(path, FRAME_ORACLE_HEADER)


def read_message_oracle(path: Path) -> list[MessageOracleRow]:
    return _read_rows(path, MESSAGE_ORACLE_HEADER)


def read_connection(path: Path) -> Connection:
    """The single row of tx-connection.csv."""
    rows = _read_rows(path, CONNECTION_HEADER)
    if len(rows) != 1:
        raise RuntimeError(f"{path}: expected one connection row, got {len(rows)}")
    return rows[0]


def _write_csv(path: Path, header: tuple[str, ...], rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_trace(path: Path, rows: list[TraceRow]) -> None:
    _write_csv(
        path,
        TRACE_HEADER,
        [(row.frame_id, row.eof_time_ps, row.channel, row.frame.hex()) for row in rows],
    )


def write_frame_oracle(path: Path, rows: list[FrameOracleRow]) -> None:
    _write_csv(
        path,
        FRAME_ORACLE_HEADER,
        [
            (
                row.frame_id,
                row.message_id,
                row.packet_index,
                row.packet_count,
                row.request_id,
                row.qpn,
                row.psn,
                row.opcode,
                row.payload_offset,
                row.payload_length,
                row.expected_frame.hex(),
                row.channel,
            )
            for row in rows
        ],
    )


def write_connection(path: Path, connection: Connection) -> None:
    _write_csv(
        path,
        CONNECTION_HEADER,
        [
            (
                f"{connection.src_mac:012x}",
                f"{connection.src_ip:08x}",
                f"{connection.dst_mac:012x}",
                f"{connection.dst_ip:08x}",
            )
        ],
    )


def write_message_oracle(path: Path, rows: list[MessageOracleRow]) -> None:
    _write_csv(
        path,
        MESSAGE_ORACLE_HEADER,
        [
            (
                row.message_id,
                row.request_id,
                row.qpn,
                row.start_psn,
                row.remote_addr,
                row.remote_key,
                row.message_length,
                row.payload.hex(),
            )
            for row in rows
        ],
    )


# --------------------------------------------------------------------------
# Frame field access (little helpers shared by the TX and RX stages)
# --------------------------------------------------------------------------

PSN_MASK = 0xFFFFFF

MAC_DST_OFFSET = 0
MAC_SRC_OFFSET = 6
IP_SRC_OFFSET = 26
IP_DST_OFFSET = 30


def frame_connection(frame: bytes) -> Connection:
    """The addresses a frame carries, in the same orientation as `Connection`.

    Compare this against the declared connection instead of using it to derive
    one: an expectation built from the frames under test cannot fail.
    """
    if len(frame) < 42:
        raise RuntimeError("frame is too short to carry a network identity")
    return Connection(
        src_mac=int.from_bytes(frame[MAC_SRC_OFFSET : MAC_SRC_OFFSET + 6], "big"),
        dst_mac=int.from_bytes(frame[MAC_DST_OFFSET : MAC_DST_OFFSET + 6], "big"),
        src_ip=int.from_bytes(frame[IP_SRC_OFFSET : IP_SRC_OFFSET + 4], "big"),
        dst_ip=int.from_bytes(frame[IP_DST_OFFSET : IP_DST_OFFSET + 4], "big"),
    )


def psn_distance(start: int, current: int) -> int:
    return (current - start) & PSN_MASK
