"""Independent byte-exact model of the current OpenRDMA-flow M3 frame."""

from __future__ import annotations

from dataclasses import dataclass

from model.rx_types import RxBufferedFrame, RxPayloadTag, RxWireBeat

OP_WRITE_FIRST = 0x06
OP_WRITE_MIDDLE = 0x07
OP_WRITE_LAST = 0x08
OP_WRITE_ONLY = 0x09
OP_ACK = 0x11


@dataclass(frozen=True)
class NetworkConfig:
    local_mac: int = 0x112233445566
    peer_mac: int = 0x66778899AABB
    local_ip: int = 0x0A000002
    peer_ip: int = 0x0A000001


def _be(value: int, width: int) -> bytes:
    return int(value).to_bytes(width, "big")


def ipv4_checksum(header: bytes) -> int:
    assert len(header) == 20
    total = sum(int.from_bytes(header[i : i + 2], "big") for i in range(0, 20, 2))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def build_frame(
    *,
    opcode: int,
    qpn: int,
    psn: int,
    payload: bytes,
    config: NetworkConfig = NetworkConfig(),
    ecn: int = 0,
    remote_addr: int = 0,
    rkey: int = 0,
    message_bytes: int = 0,
) -> bytes:
    first = opcode in (OP_WRITE_FIRST, OP_WRITE_ONLY)
    transport_extra = 16 if first else 0
    ip_length = 20 + 8 + 12 + transport_extra + len(payload)
    udp_length = 8 + 12 + transport_extra + len(payload)

    ethernet = _be(config.local_mac, 6) + _be(config.peer_mac, 6) + b"\x08\x00"
    ip = bytearray(20)
    ip[0] = 0x45
    ip[1] = ecn & 0x3
    ip[2:4] = _be(ip_length, 2)
    ip[4:6] = b"\x00\x00"
    ip[6:8] = b"\x40\x00"
    ip[8] = 64
    ip[9] = 17
    ip[10:12] = b"\x00\x00"
    ip[12:16] = _be(config.peer_ip, 4)
    ip[16:20] = _be(config.local_ip, 4)
    ip[10:12] = _be(ipv4_checksum(bytes(ip)), 2)
    udp = b"\xc1\xe8\x12\xb7" + _be(udp_length, 2) + b"\x00\x00"
    bth = bytes([opcode, 0]) + b"\xff\xff" + _be(qpn, 3) + b"\x00" + _be(psn, 3) + b"\x00"
    reth = b""
    if first:
        reth = _be(rkey, 4) + _be(remote_addr, 8) + _be(message_bytes, 4)
    return ethernet + bytes(ip) + udp + bth + reth + payload


def build_ack_frame(
    *, qpn: int, psn: int, aeth: int, config: NetworkConfig = NetworkConfig(), ecn: int = 0
) -> bytes:
    return build_frame(
        opcode=OP_ACK,
        qpn=qpn,
        psn=psn,
        payload=_be(aeth, 4),
        config=config,
        ecn=ecn,
    )


def buffered_frame(frame: bytes, tag: RxPayloadTag) -> RxBufferedFrame:
    padded = frame + bytes(max(0, 96 - len(frame)))
    words = [int.from_bytes(padded[i : i + 32], "little") for i in (0, 32, 64)]
    return RxBufferedFrame(
        tag=tag,
        header0=words[0],
        header1=words[1],
        header2=words[2],
        bytes=len(frame),
        beats=(len(frame) + 31) // 32,
    )


def wire_beats(frame: bytes) -> list[RxWireBeat]:
    result: list[RxWireBeat] = []
    for offset in range(0, len(frame), 32):
        chunk = frame[offset : offset + 32]
        result.append(
            RxWireBeat(
                data=int.from_bytes(chunk.ljust(32, b"\x00"), "little"),
                be=(1 << len(chunk)) - 1,
                sof=int(offset == 0),
                eof=int(offset + len(chunk) == len(frame)),
            )
        )
    return result
