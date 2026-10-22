"""Shared bit-vector types and arithmetic limits for MVP reference models."""

from cocotb_bsv import Bits


def bits(name: str, width: int) -> type[Bits]:
    return type(name, (Bits,), {"_width": width})


Bit1 = bits("Bit1", 1)
Bit2 = bits("Bit2", 2)
Bit3 = bits("Bit3", 3)
Bit4 = bits("Bit4", 4)
Bit6 = bits("Bit6", 6)
Bit7 = bits("Bit7", 7)
Bit8 = bits("Bit8", 8)
Bit9 = bits("Bit9", 9)
Bit13 = bits("Bit13", 13)
Bit16 = bits("Bit16", 16)
Bit17 = bits("Bit17", 17)
Bit18 = bits("Bit18", 18)
Bit19 = bits("Bit19", 19)
Bit24 = bits("Bit24", 24)
Bit25 = bits("Bit25", 25)
Bit32 = bits("Bit32", 32)
Bit48 = bits("Bit48", 48)
Bit64 = bits("Bit64", 64)
Bit128 = bits("Bit128", 128)
Bit256 = bits("Bit256", 256)
Bit1024 = bits("Bit1024", 1024)

MAX_U64 = (1 << 64) - 1
