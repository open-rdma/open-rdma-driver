"""Flat DMA/Phase-A integration wire mirrors and helper models."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from cocotb_bsv import Struct
from model.common import Bit1, Bit16, Bit17, Bit32, Bit64, Bit128, Bit1024, bits

Bit11 = bits("DmaBit11", 11)


class DmaChunkReq(Struct):
    _members_def = OrderedDict(
        physAddr=Bit64,
        len=Bit32,
        wqeTag=Bit16,
        chunkSeq=Bit16,
        isFirst=Bit1,
        isLast=Bit1,
    )


class DmaReadRequest(Struct):
    _members_def = OrderedDict(
        tag=Bit16,
        wqeTag=Bit16,
        chunkSeq=Bit16,
        physAddr=Bit64,
        len=Bit11,
        messageOffset=Bit32,
        isFirst=Bit1,
        isLast=Bit1,
    )


class DmaCompletionBeat(Struct):
    _members_def = OrderedDict(
        tag=Bit16,
        windowAddr=Bit64,
        data=Bit1024,
        keep=Bit128,
        burstLast=Bit1,
    )


class DmaPayloadBeat(Struct):
    _members_def = OrderedDict(
        wqeTag=Bit16,
        messageOffset=Bit32,
        data=Bit1024,
        keep=Bit128,
        first=Bit1,
        last=Bit1,
    )


class DmaMessageDone(Struct):
    _members_def = OrderedDict(wqeTag=Bit16, length=Bit32, chunkCount=Bit17)


@dataclass(frozen=True)
class ChunkSpec:
    wqe_tag: int
    chunk_seq: int
    physical_addr: int
    length: int
    message_offset: int
    is_first: int
    is_last: int


def data_byte(address: int) -> int:
    return (address * 0x9E + 0x47) & 0xFF


def make_chunks(wqe_tag: int, extents: list[tuple[int, int]]) -> tuple[DmaChunkReq, ...]:
    if not extents:
        raise ValueError("message requires at least one extent")
    result = []
    for index, (address, length) in enumerate(extents):
        if not 1 <= length <= 1024:
            raise ValueError(f"invalid chunk length {length}")
        if (address & 0xFFF) + length > 4096:
            raise ValueError("chunk crosses physical 4KiB")
        result.append(
            DmaChunkReq(
                physAddr=address,
                len=length,
                wqeTag=wqe_tag,
                chunkSeq=index,
                isFirst=int(index == 0),
                isLast=int(index + 1 == len(extents)),
            )
        )
    return tuple(result)


def chunk_specs(chunks: tuple[DmaChunkReq, ...]) -> list[ChunkSpec]:
    offset = 0
    result = []
    for chunk in chunks:
        result.append(
            ChunkSpec(
                wqe_tag=int(chunk.wqeTag),
                chunk_seq=int(chunk.chunkSeq),
                physical_addr=int(chunk.physAddr),
                length=int(chunk.len),
                message_offset=offset,
                is_first=int(chunk.isFirst),
                is_last=int(chunk.isLast),
            )
        )
        offset += int(chunk.len)
    return result


def logical_payload(chunks: tuple[DmaChunkReq, ...]) -> bytes:
    return bytes(
        data_byte(address)
        for chunk in chunks
        for address in range(int(chunk.physAddr), int(chunk.physAddr) + int(chunk.len))
    )


def completion_beats(request: DmaReadRequest) -> list[DmaCompletionBeat]:
    address = int(request.physAddr)
    remaining = int(request.len)
    result = []
    while remaining:
        window = address & ~0x7F
        lane = address - window
        valid = min(128 - lane, remaining)
        data = 0
        for index in range(valid):
            data |= data_byte(address + index) << (8 * (lane + index))
        result.append(
            DmaCompletionBeat(
                tag=int(request.tag),
                windowAddr=window,
                data=data,
                keep=((1 << valid) - 1) << lane,
                burstLast=int(valid == remaining),
            )
        )
        address += valid
        remaining -= valid
    return result


def raw_payload_beats(
    wqe_tag: int,
    payload: bytes,
    fragments: list[tuple[int, int]],
) -> list[DmaPayloadBeat]:
    """把逻辑 payload 按 (lane_offset, byte_count) 构造成 raw DMA beats。"""
    if sum(count for _, count in fragments) != len(payload):
        raise ValueError("fragment lengths do not cover payload")
    result = []
    consumed = 0
    for index, (lane, count) in enumerate(fragments):
        if not 0 <= lane < 128 or not 1 <= count <= 128 - lane:
            raise ValueError("invalid raw fragment")
        part = payload[consumed : consumed + count]
        result.append(
            DmaPayloadBeat(
                wqeTag=wqe_tag,
                messageOffset=consumed,
                data=int.from_bytes(part, "little") << (8 * lane),
                keep=((1 << count) - 1) << lane,
                first=int(index == 0),
                last=int(index + 1 == len(fragments)),
            )
        )
        consumed += count
    return result
