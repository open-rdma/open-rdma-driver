"""Thin adapters for the conventional BSV Pipe method set."""

from __future__ import annotations

from typing import Any

from .methods import ActionMethod, ValueMethod


class PipeIn:
    def __init__(self, dut: Any, name: str, clk: Any) -> None:
        self.not_full_method = ValueMethod(dut, f"{name}_notFull", clk)
        self.enq_method = ActionMethod(dut, f"{name}_enq", clk)

    async def not_full(self) -> bool:
        return bool(int(await self.not_full_method()))

    async def enq(self, data: Any) -> None:
        await self.enq_method(value=int(data))


class PipeOut:
    def __init__(self, dut: Any, name: str, clk: Any) -> None:
        self.not_empty_method = ValueMethod(dut, f"{name}_notEmpty", clk)
        self.first_method = ValueMethod(dut, f"{name}_first", clk)
        self.deq_method = ActionMethod(dut, f"{name}_deq", clk)

    async def not_empty(self) -> bool:
        return bool(int(await self.not_empty_method()))

    async def first(self) -> int:
        return int(await self.first_method())

    async def deq(self) -> None:
        await self.deq_method()
