"""Wire mirrors and reference model for the channel-neutral M2 packetizer."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from cocotb_bsv import Struct
from model.common import (
    Bit1,
    Bit2,
    Bit3,
    Bit4,
    Bit8,
    Bit13,
    Bit16,
    Bit18,
    Bit24,
    Bit25,
    Bit32,
    Bit64,
    Bit128,
    Bit1024,
)


class MessageContext(Struct):
    _members_def = OrderedDict(
        requestId=Bit16,
        qpn=Bit24,
        destQpn=Bit24,
        slotId=Bit18,
        debugSeq=Bit4,
        pmtu=Bit3,
        startPsn=Bit24,
        psnCount=Bit18,
        remoteAddr=Bit64,
        remoteKey=Bit32,
        len=Bit25,
        wireProfile=Bit2,
        evProfileId=Bit2,
        mrcMsn=Bit16,
        mrcMsnValid=Bit1,
    )


class MessagePayloadBeat(Struct):
    _members_def = OrderedDict(
        requestId=Bit16,
        messageOffset=Bit25,
        data=Bit1024,
        keep=Bit128,
        messageSop=Bit1,
        messageEop=Bit1,
    )


class PacketDescriptor(Struct):
    _members_def = OrderedDict(
        requestId=Bit16,
        qpn=Bit24,
        destQpn=Bit24,
        slotId=Bit18,
        debugSeq=Bit4,
        pmtu=Bit3,
        psn=Bit24,
        packetIndex=Bit18,
        packetCount=Bit18,
        opcode=Bit8,
        remoteAddr=Bit64,
        remoteKey=Bit32,
        messageOffset=Bit25,
        payloadLength=Bit13,
        messageLength=Bit25,
        isFirst=Bit1,
        isLast=Bit1,
        trafficClass=Bit2,
        wireProfile=Bit2,
        evProfileId=Bit2,
        mrcMsn=Bit16,
        mrcMsnValid=Bit1,
    )


class PacketPayloadBeat(Struct):
    _members_def = OrderedDict(
        requestId=Bit16,
        psn=Bit24,
        data=Bit1024,
        keep=Bit128,
        sop=Bit1,
        eop=Bit1,
    )


class AlignedBeat(Struct):
    _members_def = OrderedDict(data=Bit1024, payloadTaken=Bit8)


PMTU_BYTES = (256, 512, 1024, 2048, 4096)
WIRE_LEGACY_PRIVATE = 0
WIRE_MRC_IPV4_ECMP_NO_ICRC = 1
OP_WRITE_FIRST = 0x06
OP_WRITE_MIDDLE = 0x07
OP_WRITE_LAST = 0x08
OP_WRITE_ONLY = 0x09
OP_MRC_WRITE_FIRST = 0xC6
OP_MRC_WRITE_MIDDLE = 0xC7
OP_MRC_WRITE_LAST = 0xC8
OP_MRC_WRITE_ONLY = 0xCA
TRAFFIC_FIRST = 0
TRAFFIC_RETRY = 1
TRAFFIC_ORDERED = 2
TRAFFIC_CONTROL = 3


def prefix_mask(size: int) -> int:
    return (1 << size) - 1 if size else 0


def word_from_bytes(data: bytes) -> int:
    return int.from_bytes(data, "little")


def bytes_from_word(data: int, keep: int, width: int = 128) -> bytes:
    raw = data.to_bytes(width, "little")
    return bytes(raw[index] for index in range(width) if keep & (1 << index))


def packet_lengths(
    length: int, pmtu_code: int, wire_profile: int = WIRE_LEGACY_PRIVATE
) -> list[int]:
    pmtu = PMTU_BYTES[pmtu_code]
    if length <= 0:
        raise ValueError("zero-length messages are unsupported")
    if wire_profile == WIRE_MRC_IPV4_ECMP_NO_ICRC:
        return [min(pmtu, length - offset) for offset in range(0, length, pmtu)]
    if wire_profile != WIRE_LEGACY_PRIVATE:
        raise ValueError(f"unsupported wire profile: {wire_profile}")

    first_limit = pmtu - 28
    continuation_limit = pmtu - 12
    result = [min(length, first_limit)]
    remaining = length - result[0]
    while remaining:
        size = min(remaining, continuation_limit)
        result.append(size)
        remaining -= size
    return result


def opcode_for(
    index: int, count: int, wire_profile: int = WIRE_LEGACY_PRIVATE
) -> int:
    if wire_profile == WIRE_MRC_IPV4_ECMP_NO_ICRC:
        if count == 1:
            return OP_MRC_WRITE_ONLY
        if index == 0:
            return OP_MRC_WRITE_FIRST
        if index == count - 1:
            return OP_MRC_WRITE_LAST
        return OP_MRC_WRITE_MIDDLE
    if wire_profile != WIRE_LEGACY_PRIVATE:
        raise ValueError(f"unsupported wire profile: {wire_profile}")
    if count == 1:
        return OP_WRITE_ONLY
    if index == 0:
        return OP_WRITE_FIRST
    if index == count - 1:
        return OP_WRITE_LAST
    return OP_WRITE_MIDDLE


@dataclass(frozen=True)
class Packet:
    descriptor: PacketDescriptor
    payload: bytes


def packetize(ctx: MessageContext, payload: bytes) -> list[Packet]:
    if len(payload) != int(ctx.len):
        raise ValueError("context length does not match payload")
    if int(ctx.remoteAddr) + len(payload) > 1 << 64:
        raise ValueError("remote address range overflow")
    wire_profile = int(ctx.wireProfile)
    if wire_profile == WIRE_MRC_IPV4_ECMP_NO_ICRC and not int(ctx.mrcMsnValid):
        raise ValueError("MRC profile requires a valid MSN")
    lengths = packet_lengths(len(payload), int(ctx.pmtu), wire_profile)
    packets: list[Packet] = []
    offset = 0
    for index, size in enumerate(lengths):
        remote_addr = int(ctx.remoteAddr) + offset
        if remote_addr >= 1 << 64:
            raise ValueError("remote address overflow")
        descriptor = PacketDescriptor(
            requestId=int(ctx.requestId),
            qpn=int(ctx.qpn),
            destQpn=int(ctx.destQpn),
            slotId=int(ctx.slotId),
            debugSeq=int(ctx.debugSeq),
            pmtu=int(ctx.pmtu),
            psn=(int(ctx.startPsn) + index) & 0xFFFFFF,
            packetIndex=index,
            packetCount=len(lengths),
            opcode=opcode_for(index, len(lengths), wire_profile),
            remoteAddr=remote_addr,
            remoteKey=int(ctx.remoteKey),
            messageOffset=offset,
            payloadLength=size,
            messageLength=len(payload),
            isFirst=int(index == 0),
            isLast=int(index == len(lengths) - 1),
            trafficClass=TRAFFIC_FIRST,
            wireProfile=wire_profile,
            evProfileId=int(ctx.evProfileId),
            mrcMsn=int(ctx.mrcMsn),
            mrcMsnValid=int(ctx.mrcMsnValid),
        )
        packets.append(Packet(descriptor, payload[offset : offset + size]))
        offset += size
    return packets


def message_beats(payload: bytes, request_id: int) -> list[MessagePayloadBeat]:
    beats = []
    for offset in range(0, len(payload), 128):
        chunk = payload[offset : offset + 128]
        beats.append(
            MessagePayloadBeat(
                requestId=request_id,
                messageOffset=offset,
                data=word_from_bytes(chunk),
                keep=prefix_mask(len(chunk)),
                messageSop=int(offset == 0),
                messageEop=int(offset + len(chunk) == len(payload)),
            )
        )
    return beats
