#!/usr/bin/env python3
"""Generate a small byte-exact trace matching OpenRDMA-flow M3."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

SRC_MAC = bytes.fromhex("020000000001")
DST_MAC = bytes.fromhex("020000000002")
SRC_IP = bytes((10, 0, 0, 1))
DST_IP = bytes((10, 0, 0, 2))
RKEY = 0x55667788
REMOTE_ADDR = 0x0000200000000000


def checksum(header: bytes) -> int:
    total = sum(int.from_bytes(header[i : i + 2], "big") for i in range(0, len(header), 2))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def payload(qpn: int, start: int, length: int) -> bytes:
    return bytes((qpn * 29 + offset * 7) & 0xFF for offset in range(start, start + length))


def build_frame(
    *, qpn: int, psn: int, opcode: int, data: bytes, message_length: int, remote_addr: int
) -> bytes:
    first_layout = opcode in (0x06, 0x09)
    bth = (
        bytes((opcode, 0x00, 0xFF, 0xFF))
        + qpn.to_bytes(3, "big")
        + b"\x00"
        + psn.to_bytes(3, "big")
        + b"\x00"
    )
    reth = b""
    if first_layout:
        # This order mirrors the current OpenRDMA M3 byte model exactly.
        reth = RKEY.to_bytes(4, "big") + remote_addr.to_bytes(8, "big") + message_length.to_bytes(4, "big")

    udp_payload = bth + reth + data
    udp_length = 8 + len(udp_payload)
    udp = b"\xC1\xE8\x12\xB7" + udp_length.to_bytes(2, "big") + b"\x00\x00"

    ip_length = 20 + udp_length
    ip = (
        b"\x45\x00"
        + ip_length.to_bytes(2, "big")
        + b"\x00\x00\x40\x00\x40\x11\x00\x00"
        + SRC_IP
        + DST_IP
    )
    ip = ip[:10] + checksum(ip).to_bytes(2, "big") + ip[12:]
    ethernet = DST_MAC + SRC_MAC + b"\x08\x00"
    return ethernet + ip + udp + udp_payload


def rows() -> list[tuple[int, int, bytes]]:
    long_message = payload(2, 0, 700)
    short_message = payload(1, 0, 64)
    return [
        # The slow path carries FIRST; continuations arrive before it.
        (0, 3, build_frame(qpn=2, psn=0xFFFFFE, opcode=0x06, data=long_message[:228],
                           message_length=700, remote_addr=REMOTE_ADDR)),
        (100, 0, build_frame(qpn=2, psn=0xFFFFFF, opcode=0x07, data=long_message[228:472],
                             message_length=700, remote_addr=REMOTE_ADDR + 228)),
        (200, 1, build_frame(qpn=2, psn=0x000000, opcode=0x08, data=long_message[472:],
                             message_length=700, remote_addr=REMOTE_ADDR + 472)),
        (300, 2, build_frame(qpn=1, psn=0x000100, opcode=0x09, data=short_message,
                             message_length=64, remote_addr=REMOTE_ADDR + 0x1000)),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "test-data/m3-trace.csv")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("time_ns", "channel", "frame_hex"))
        for time_ns, channel, frame in rows():
            writer.writerow((time_ns, channel, frame.hex()))


if __name__ == "__main__":
    main()
