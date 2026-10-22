"""Shared Cocotb 2 helpers for RX wrappers."""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, with_timeout


async def reset_dut(dut, cycles: int = 4) -> None:
    cocotb.start_soon(Clock(dut.CLK, 2, unit="ns").start())
    dut.RST_N.value = 0
    for _ in range(cycles):
        await RisingEdge(dut.CLK)
    dut.RST_N.value = 1
    for _ in range(cycles):
        await RisingEdge(dut.CLK)


async def pipe_put(pipe, clock, value, cycles: int = 20_000) -> None:
    async def put_one() -> None:
        while not await pipe.not_full():
            await RisingEdge(clock)
        await pipe.enq(value.pack() if hasattr(value, "pack") else int(value))

    await with_timeout(put_one(), cycles * 2, "ns")


async def pipe_get(pipe, clock, struct_type=None, cycles: int = 20_000):
    async def get_one():
        while not await pipe.not_empty():
            await RisingEdge(clock)
        raw = await pipe.first()
        await pipe.deq()
        return struct_type.unpack(raw) if struct_type is not None else raw

    return await with_timeout(get_one(), cycles * 2, "ns")


async def assert_pipe_empty(pipe, clock, cycles: int = 8) -> None:
    for _ in range(cycles):
        await RisingEdge(clock)
        assert not await pipe.not_empty(), "unexpected trailing output"
