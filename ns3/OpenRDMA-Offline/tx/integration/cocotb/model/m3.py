"""Wire mirrors and byte-exact reference model for the M3 TX scheduler."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from cocotb_bsv import Struct
from model.common import (
    Bit1, Bit2, Bit3, Bit4, Bit8, Bit13, Bit16, Bit18, Bit24, Bit25, Bit32,
    Bit48, Bit64, Bit256,
)
from model.m2 import (
    PMTU_BYTES,
    TRAFFIC_RETRY,
    WIRE_LEGACY_PRIVATE,
    WIRE_MRC_IPV4_ECMP_NO_ICRC,
    PacketDescriptor,
    PacketPayloadBeat,
    opcode_for,
    prefix_mask,
    word_from_bytes,
)


class MacIpConfig(Struct):
    _members_def = OrderedDict(srcMac=Bit48, dstMac=Bit48, srcIp=Bit32, dstIp=Bit32)


class MrcTxCfgReq(Struct):
    _members_def = OrderedDict(cfg=MacIpConfig, channelEnable=Bit4, maxHighBurst=Bit4)


class CfgResp(Struct):
    _members_def = OrderedDict(success=Bit1)


class EthPacket(Struct):
    _members_def = OrderedDict(
        data=Bit256,
        be=Bit32,
        sof=Bit1,
        eof=Bit1,
        requestId=Bit16,
        qpn=Bit24,
        destQpn=Bit24,
        psn=Bit24,
        isFirst=Bit1,
        isLast=Bit1,
        trafficClass=Bit2,
    )


class TxCommit(Struct):
    _members_def = OrderedDict(
        requestId=Bit16,
        qpn=Bit24,
        psn=Bit24,
        packetIndex=Bit18,
        channel=Bit2,
        trafficClass=Bit2,
    )


class MrcEvEntryConfig(Struct):
    _members_def = OrderedDict(
        profileId=Bit2,
        entryIndex=Bit3,
        valid=Bit1,
        adminEnable=Bit1,
        entropy=Bit32,
        channel=Bit2,
    )


class MrcEvPolicyConfig(Struct):
    _members_def = OrderedDict(skipHoldCycles=Bit16, probeIntervalCycles=Bit16)


class MrcPortStatusUpdate(Struct):
    _members_def = OrderedDict(localReachable=Bit4, peerReachable=Bit4)


class MrcSourceProfileConfig(Struct):
    _members_def = OrderedDict(
        mrcMask=Bit4,
        firstProfileId=Bit2,
        retryProfileId=Bit2,
        orderedProfileId=Bit2,
        controlProfileId=Bit2,
    )


class MrcPathFeedback(Struct):
    _members_def = OrderedDict(
        profileId=Bit2,
        entryIndex=Bit3,
        generation=Bit8,
        kind=Bit3,
        probeId=Bit16,
    )


class MrcProbeRequest(Struct):
    _members_def = OrderedDict(
        profileId=Bit2,
        entryIndex=Bit3,
        generation=Bit8,
        entropy=Bit32,
        channel=Bit2,
        probeId=Bit16,
    )


class MrcNoPathEvent(Struct):
    _members_def = OrderedDict(trafficClass=Bit2, profileId=Bit2)


class TxEgressEvent(Struct):
    _members_def = OrderedDict(
        requestId=Bit16,
        qpn=Bit24,
        psn=Bit24,
        packetIndex=Bit18,
        channel=Bit2,
        trafficClass=Bit2,
        evValid=Bit1,
        profileId=Bit2,
        evIndex=Bit3,
        evGeneration=Bit8,
        entropy=Bit32,
    )


EV_FB_ECN = 0
EV_FB_SKIP_ONCE = 1
EV_FB_TRIMMED = 2
EV_FB_ALWAYS_SKIP = 3
EV_FB_TIMEOUT = 4
EV_FB_PROBE_GOOD = 5
EV_FB_PROBE_SKIP = 6
EV_FB_CLEAN = 7


@dataclass(frozen=True)
class Frame:
    data: bytes
    request_id: int
    qpn: int
    dest_qpn: int
    psn: int
    is_first: int
    is_last: int
    traffic_class: int


def ipv4_checksum(header: bytes) -> int:
    total = sum(int.from_bytes(header[i : i + 2], "big") for i in range(0, 20, 2))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def header_bytes(descriptor: PacketDescriptor, cfg: MacIpConfig) -> bytes:
    first_layout = int(descriptor.opcode) in (0x06, 0x09)
    payload_length = int(descriptor.payloadLength)
    ip_length = payload_length + (56 if first_layout else 40)
    udp_length = payload_length + (36 if first_layout else 20)

    ip = (
        b"\x45\x00"
        + ip_length.to_bytes(2, "big")
        + b"\x00\x00\x40\x00\x40\x11\x00\x00"
        + int(cfg.srcIp).to_bytes(4, "big")
        + int(cfg.dstIp).to_bytes(4, "big")
    )
    ip = ip[:10] + ipv4_checksum(ip).to_bytes(2, "big") + ip[12:]
    udp = (
        b"\xc1\xe8\x12\xb7"
        + udp_length.to_bytes(2, "big")
        + b"\x00\x00"
    )
    bth = (
        bytes((int(descriptor.opcode), 0))
        + b"\xff\xff"
        + int(descriptor.destQpn).to_bytes(3, "big")
        + b"\x00"
        + int(descriptor.psn).to_bytes(3, "big")
        + b"\x00"
    )
    mac = (
        int(cfg.dstMac).to_bytes(6, "big")
        + int(cfg.srcMac).to_bytes(6, "big")
        + b"\x08\x00"
    )
    if first_layout:
        reth = (
            int(descriptor.remoteKey).to_bytes(4, "big")
            + int(descriptor.remoteAddr).to_bytes(8, "big")
            + int(descriptor.messageLength).to_bytes(4, "big")
        )
        return mac + ip + udp + bth + reth
    return mac + ip + udp + bth


MRC_UDP_DESTINATION_PORT = 4791


def mrc_header_bytes(
    descriptor: PacketDescriptor, cfg: MacIpConfig, entropy: int
) -> bytes:
    """Byte-exact mirror of computeMrcHeader in MrcHeaders.bsv."""
    payload_length = int(descriptor.payloadLength)
    pad_bytes = (4 - payload_length % 4) % 4
    ip_length = payload_length + pad_bytes + 60
    udp_length = payload_length + pad_bytes + 40

    ip = (
        b"\x45\x00"
        + ip_length.to_bytes(2, "big")
        + b"\x00\x00\x40\x00\x40\x11\x00\x00"
        + int(cfg.srcIp).to_bytes(4, "big")
        + int(cfg.dstIp).to_bytes(4, "big")
    )
    ip = ip[:10] + ipv4_checksum(ip).to_bytes(2, "big") + ip[12:]
    udp = (
        (int(entropy) & 0xFFFF).to_bytes(2, "big")
        + MRC_UDP_DESTINATION_PORT.to_bytes(2, "big")
        + udp_length.to_bytes(2, "big")
        + b"\x00\x00"
    )
    retransmit = 1 if int(descriptor.trafficClass) == TRAFFIC_RETRY else 0
    bth = (
        bytes(
            (
                int(descriptor.opcode),
                (pad_bytes << 4) & 0xFF,
            )
        )
        + b"\xff\xff"
        + b"\x00"
        + int(descriptor.destQpn).to_bytes(3, "big")
        + bytes(
            (
                ((int(descriptor.isLast) & 1) << 7)
                | ((retransmit & 1) << 5),
            )
        )
        + int(descriptor.psn).to_bytes(3, "big")
    )
    # RQMSN is undefined for normal Write and is encoded as zero.
    meth = b"\x00\x00" + int(descriptor.mrcMsn).to_bytes(2, "big")
    reth = (
        int(descriptor.remoteAddr).to_bytes(8, "big")
        + int(descriptor.remoteKey).to_bytes(4, "big")
        + int(descriptor.messageLength).to_bytes(4, "big")
    )
    mac = (
        int(cfg.dstMac).to_bytes(6, "big")
        + int(cfg.srcMac).to_bytes(6, "big")
        + b"\x08\x00"
    )
    return mac + ip + udp + bth + meth + reth


def mrc_pad_bytes(descriptor: PacketDescriptor) -> int:
    return (4 - int(descriptor.payloadLength) % 4) % 4


def validate_packet_descriptor(descriptor: PacketDescriptor) -> None:
    """Reject packet metadata that cannot be emitted by the M2 packetizer."""
    profile = int(descriptor.wireProfile)
    if profile not in (WIRE_LEGACY_PRIVATE, WIRE_MRC_IPV4_ECMP_NO_ICRC):
        raise ValueError(f"unsupported wire profile: {profile}")

    pmtu_code = int(descriptor.pmtu)
    if not 0 <= pmtu_code < len(PMTU_BYTES):
        raise ValueError(f"unsupported PMTU code: {pmtu_code}")
    index = int(descriptor.packetIndex)
    count = int(descriptor.packetCount)
    is_first = int(descriptor.isFirst)
    is_last = int(descriptor.isLast)
    if count <= 0 or not 0 <= index < count:
        raise ValueError("packet index is outside packet count")
    if is_first != int(index == 0) or is_last != int(index + 1 == count):
        raise ValueError("packet boundary flags disagree with packet index")
    if int(descriptor.opcode) != opcode_for(index, count, profile):
        raise ValueError("packet opcode disagrees with packet position")

    pmtu = PMTU_BYTES[pmtu_code]
    limit = pmtu if profile == WIRE_MRC_IPV4_ECMP_NO_ICRC else pmtu - (
        28 if is_first else 12
    )
    payload_length = int(descriptor.payloadLength)
    if not 0 < payload_length <= limit or (not is_last and payload_length != limit):
        raise ValueError("packet payload length disagrees with PMTU and position")
    message_offset = int(descriptor.messageOffset)
    message_length = int(descriptor.messageLength)
    if is_first != int(message_offset == 0):
        raise ValueError("packet first flag disagrees with message offset")
    if message_length <= 0 or message_offset + payload_length > message_length:
        raise ValueError("packet payload lies outside the message range")


def expected_frame(
    descriptor: PacketDescriptor,
    payload: bytes,
    cfg: MacIpConfig,
    entropy: int = 0,
) -> Frame:
    validate_packet_descriptor(descriptor)
    assert len(payload) == int(descriptor.payloadLength)
    if int(descriptor.wireProfile) == WIRE_MRC_IPV4_ECMP_NO_ICRC:
        header = mrc_header_bytes(descriptor, cfg, entropy)
        trailer = b"\x00" * mrc_pad_bytes(descriptor)
    else:
        header = header_bytes(descriptor, cfg)
        trailer = b""
    return Frame(
        data=header + payload + trailer,
        request_id=int(descriptor.requestId),
        qpn=int(descriptor.qpn),
        dest_qpn=int(descriptor.destQpn),
        psn=int(descriptor.psn),
        is_first=int(descriptor.isFirst),
        is_last=int(descriptor.isLast),
        traffic_class=int(descriptor.trafficClass),
    )


def packet_payload_beats(descriptor: PacketDescriptor, payload: bytes) -> list[PacketPayloadBeat]:
    assert len(payload) == int(descriptor.payloadLength)
    result = []
    for offset in range(0, len(payload), 128):
        chunk = payload[offset : offset + 128]
        result.append(
            PacketPayloadBeat(
                requestId=int(descriptor.requestId),
                psn=int(descriptor.psn),
                data=word_from_bytes(chunk),
                keep=prefix_mask(len(chunk)),
                sop=int(offset == 0),
                eop=int(offset + len(chunk) == len(payload)),
            )
        )
    return result


def frame_word_bytes(word: EthPacket) -> bytes:
    be = int(word.be)
    assert be and be == (1 << be.bit_length()) - 1
    return int(word.data).to_bytes(32, "little")[: be.bit_length()]
