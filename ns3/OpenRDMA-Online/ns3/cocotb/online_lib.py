"""Shared types for the online TX <-> ns-3 <-> RX co-simulation.

The online flow runs the same three participants as `OpenRDMA-Offline`, but as
three live processes wired together with Unix domain sockets instead of CSV
files:

    tx/ (Verilator + cocotb) --tx.sock--> ns3/online-cosim --rx.sock--> rx/ (Verilator + cocotb)

Nothing is exchanged through the filesystem between the stages.  The TX run
declares its oracle to the network hub over the wire, the hub validates every
frame as it is injected and forwards each arrival as it happens, and the RX
replays the arrivals against its own virtual clock while the TX is still
running.  The only files a run leaves behind are the three reports.

What that buys over the offline tree:

  * the RX lane injects a frame at the arrival instant the network reports,
    instead of at a timestamp read out of a file before the replay starts;
  * the oracle is still produced by the TX stage from the independent Python
    models, so the RX is still checked against something it did not produce --
    the same property the offline CSVs had, carried by a message rather than
    by a column.

The row types below are the offline ones minus `channel` in `FrameOracleRow`:
offline the TX wrote the egress channel it *had* committed into the oracle
column, and the ns-3 stage compared its input trace against it, which caught a
trace whose channels had been flattened on the way to disk.  Online there is no
trace file in between: the channel travels in the same ADVANCE message as the
frame, so the hub checks that all four channels carry traffic but cannot
cross-check the value against a second source.  See `ns3/README.md`.
"""

from __future__ import annotations

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
    """Where the three stages write their reports.

    ONLINE_RESULTS overrides it; the default is <tree>/results, so one
    directory listing still describes a whole run.
    """
    override = os.environ.get("ONLINE_RESULTS")
    path = Path(override) if override else TREE_ROOT / "results"
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact(name: str) -> Path:
    path = results_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def sockets_dir() -> Path:
    """Where the hub's two listening sockets live.

    A Unix socket path is capped at 108 bytes by the kernel, so this stays
    under the results directory rather than under a build tree whose depth
    depends on how the runner was invoked.
    """
    path = results_dir() / "sockets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _socket_path(variable: str, name: str) -> Path:
    override = os.environ.get(variable)
    return Path(override) if override else sockets_dir() / name


def tx_socket_path() -> Path:
    """The TX stage's end of the hub (ONLINE_TX_SOCKET overrides)."""
    return _socket_path("ONLINE_TX_SOCKET", "tx.sock")


def rx_socket_path() -> Path:
    """The RX stage's end of the hub (ONLINE_RX_SOCKET overrides)."""
    return _socket_path("ONLINE_RX_SOCKET", "rx.sock")


# --------------------------------------------------------------------------
# Records carried over the wire
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceRow:
    """One transmitted frame: what the TX sends inside an ADVANCE.

    `eof_time_ps` is the simulation time at which the frame's EOF word left the
    M3 channel buffer -- the same boundary the offline flow hands to ns-3.
    """

    frame_id: int
    eof_time_ps: int
    channel: int
    frame: bytes


@dataclass(frozen=True)
class ArrivalRow:
    """One delivered frame, as the hub reports it to the RX.

    `arrival_time_ps` is the network's own time base.  `injection_target_ps` is
    the same instant mapped onto the RX simulation's time base, which starts
    when the first arrival shows up: the replay is spaced exactly like the
    network spaced it, but it does not pretend to start at network time zero.
    """

    sequence: int
    frame_id: int
    tx_order: int
    channel: int
    arrival_time_ps: int
    frame: bytes
    injection_target_ps: int


@dataclass(frozen=True)
class FrameOracleRow:
    """The TX's declaration of one frame, produced by the M3 model."""

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


@dataclass(frozen=True)
class Connection:
    """The L2/L3 identity of the connection, as the TX declares it.

    This is the transmitter's view: `src_*` is the TX, `dst_*` is the RX.  The
    RX configures itself from the declaration rather than from the frames it
    is about to receive, so its address filter is a real check instead of a
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


# --------------------------------------------------------------------------
# Frame field access (shared by the TX and RX stages)
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
