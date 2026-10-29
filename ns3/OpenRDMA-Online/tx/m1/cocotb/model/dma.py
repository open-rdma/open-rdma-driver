"""DMA Engine wire types, physical-chunk model, and strict scoreboards."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from cocotb_bsv import Struct
from model.common import (
    Bit1,
    Bit16,
    Bit17,
    Bit32,
    Bit64,
    Bit128,
    Bit1024,
    bits,
)

Bit11 = bits("Bit11", 11)

MAX_BURST_BYTES = 1024
WINDOW_BYTES = 128
PHYSICAL_BOUNDARY_BYTES = 4096
MAX_U64 = (1 << 64) - 1


class DmaChunkReq(Struct):
    # 与 WqeDmaRead.DmaReq 保持相同字段顺序和位宽。
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
    _members_def = OrderedDict(
        wqeTag=Bit16,
        length=Bit32,
        chunkCount=Bit17,
    )


@dataclass(frozen=True)
class DmaMessage:
    wqe_tag: int
    chunks: tuple[DmaChunkReq, ...]

    @property
    def length(self) -> int:
        return sum(int(chunk.len) for chunk in self.chunks)


@dataclass(frozen=True)
class BurstSpec:
    wqe_tag: int
    chunk_seq: int
    physical_addr: int
    length: int
    message_offset: int
    is_first: int
    is_last: int


def data_byte(addr: int) -> int:
    """确定性 memory 图案，便于逐 byte 定位地址或顺序错误。"""
    return (addr * 0x9E + 0x47) & 0xFF


def _chunk(wqe_tag: int, seq: int, addr: int, length: int, count: int) -> DmaChunkReq:
    if not 1 <= length <= MAX_BURST_BYTES:
        raise ValueError(f"invalid chunk length: {length}")
    if addr < 0 or addr + length - 1 > MAX_U64:
        raise ValueError("chunk physical address overflows")
    if (addr & (PHYSICAL_BOUNDARY_BYTES - 1)) + length > PHYSICAL_BOUNDARY_BYTES:
        raise ValueError("chunk crosses physical 4KiB boundary")
    return DmaChunkReq(
        physAddr=addr,
        len=length,
        wqeTag=wqe_tag,
        chunkSeq=seq,
        isFirst=int(seq == 0),
        isLast=int(seq + 1 == count),
    )


def make_chunk_message(
    wqe_tag: int,
    chunks: Sequence[tuple[int, int]],
) -> DmaMessage:
    """按给定 physical chunks 构造一条 message，允许相邻 PA 不连续。"""
    if not chunks:
        raise ValueError("message must contain at least one chunk")
    count = len(chunks)
    packed = tuple(
        _chunk(wqe_tag, seq, int(addr), int(length), count)
        for seq, (addr, length) in enumerate(chunks)
    )
    return DmaMessage(wqe_tag=wqe_tag, chunks=packed)


def make_message(
    message_id: int,
    _qpn: int,
    physical_addr: int,
    length: int,
) -> DmaMessage:
    """兼容既有测试调用；reference model 在上游完成 contiguous range chunking。"""
    if length <= 0:
        raise ValueError("message length must be non-zero")
    if physical_addr < 0 or physical_addr + length - 1 > MAX_U64:
        raise ValueError("end-exclusive physical address overflows")

    chunks: list[tuple[int, int]] = []
    addr = physical_addr
    remaining = length
    while remaining:
        chunk_len = min(
            remaining,
            MAX_BURST_BYTES,
            PHYSICAL_BOUNDARY_BYTES - (addr & (PHYSICAL_BOUNDARY_BYTES - 1)),
        )
        chunks.append((addr, chunk_len))
        addr += chunk_len
        remaining -= chunk_len
    return make_chunk_message(message_id, chunks)


def split_message(message: DmaMessage) -> list[BurstSpec]:
    offset = 0
    result: list[BurstSpec] = []
    for chunk in message.chunks:
        result.append(
            BurstSpec(
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


def all_bursts(messages: Iterable[DmaMessage]) -> list[BurstSpec]:
    return [burst for message in messages for burst in split_message(message)]


def completion_beats(
    request: DmaReadRequest,
    byte_at: Callable[[int], int] = data_byte,
) -> list[DmaCompletionBeat]:
    """按 128B memory window 生成一条实际 tagged request 的合法 completion。"""
    addr = int(request.physAddr)
    remaining = int(request.len)
    beats: list[DmaCompletionBeat] = []
    while remaining:
        window_addr = addr & ~(WINDOW_BYTES - 1)
        window_offset = addr - window_addr
        valid = min(WINDOW_BYTES - window_offset, remaining)
        data = 0
        for index in range(valid):
            data |= byte_at(addr + index) << (8 * (window_offset + index))
        keep = ((1 << valid) - 1) << window_offset
        beats.append(
            DmaCompletionBeat(
                tag=int(request.tag),
                windowAddr=window_addr,
                data=data,
                keep=keep,
                burstLast=int(valid == remaining),
            )
        )
        addr += valid
        remaining -= valid
    return beats


def payload_beats_for_burst(
    burst: BurstSpec,
    byte_at: Callable[[int], int] = data_byte,
) -> list[DmaPayloadBeat]:
    request = DmaReadRequest(
        tag=0,
        wqeTag=burst.wqe_tag,
        chunkSeq=burst.chunk_seq,
        physAddr=burst.physical_addr,
        len=burst.length,
        messageOffset=burst.message_offset,
        isFirst=burst.is_first,
        isLast=burst.is_last,
    )
    result: list[DmaPayloadBeat] = []
    consumed = 0
    for completion in completion_beats(request, byte_at):
        valid = int(completion.keep).bit_count()
        offset = burst.message_offset + consumed
        result.append(
            DmaPayloadBeat(
                wqeTag=burst.wqe_tag,
                messageOffset=offset,
                data=int(completion.data),
                keep=int(completion.keep),
                first=int(offset == 0),
                last=int(burst.is_last and int(completion.burstLast)),
            )
        )
        consumed += valid
    return result


def expected_payload(
    messages: Iterable[DmaMessage],
    byte_at: Callable[[int], int] = data_byte,
) -> list[DmaPayloadBeat]:
    return [
        beat
        for message in messages
        for burst in split_message(message)
        for beat in payload_beats_for_burst(burst, byte_at)
    ]


def expected_done(messages: Iterable[DmaMessage]) -> list[DmaMessageDone]:
    return [
        DmaMessageDone(
            wqeTag=message.wqe_tag,
            length=message.length,
            chunkCount=len(message.chunks),
        )
        for message in messages
    ]


class DmaScoreboard:
    @staticmethod
    def compare_request(expected: BurstSpec, actual: DmaReadRequest, index: int) -> None:
        fields = {
            "wqeTag": expected.wqe_tag,
            "chunkSeq": expected.chunk_seq,
            "physAddr": expected.physical_addr,
            "len": expected.length,
            "messageOffset": expected.message_offset,
            "isFirst": expected.is_first,
            "isLast": expected.is_last,
        }
        for name, expected_value in fields.items():
            actual_value = int(getattr(actual, name))
            if actual_value != expected_value:
                raise AssertionError(
                    f"request[{index}].{name}: expected={expected_value:#x} "
                    f"actual={actual_value:#x}"
                )

    @staticmethod
    def compare_struct(expected: Struct, actual: Struct, event: str, index: int) -> None:
        for name in expected._members_def:
            expected_value = int(getattr(expected, name))
            actual_value = int(getattr(actual, name))
            if actual_value != expected_value:
                raise AssertionError(
                    f"{event}[{index}].{name}: expected={expected_value:#x} "
                    f"actual={actual_value:#x}"
                )
