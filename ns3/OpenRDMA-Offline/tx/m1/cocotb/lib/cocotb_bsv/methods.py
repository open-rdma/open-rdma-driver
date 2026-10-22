"""Adapters for ports generated from Bluespec methods."""

from __future__ import annotations

from typing import Any

from cocotb.handle import Immediate
from cocotb.triggers import Lock, ReadWrite, RisingEdge

from .timing import enter_read_write


class MethodPortError(AttributeError):
    pass


class MethodReadyError(RuntimeError):
    pass


def _port(dut: Any, name: str, method_name: str) -> Any:
    try:
        return getattr(dut, name)
    except AttributeError as exc:
        raise MethodPortError(
            f"BSV method {method_name!r} requires DUT port {name!r}"
        ) from exc


class _Method:
    def __init__(self, dut: Any, name: str, clk: Any) -> None:
        self.dut = dut
        self.name = name
        self.clk = clk
        self.ready = _port(dut, f"RDY_{name}", name)
        self._lock = Lock()

    def _drive_arguments(self, arguments: dict[str, Any]) -> bool:
        changed = False
        for argument, value in arguments.items():
            signal = _port(self.dut, f"{self.name}_{argument}", self.name)
            try:
                changed = changed or int(signal.value) != int(value)
            except (TypeError, ValueError, OverflowError):
                changed = True
            signal.value = value
        return changed

    def _is_ready(self) -> bool:
        value = self.ready.value
        try:
            ready = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MethodReadyError(
                f"BSV method {self.name!r} has unresolved RDY value {value!s}"
            ) from exc
        if ready not in (0, 1):
            raise MethodReadyError(
                f"BSV method {self.name!r} has non-boolean RDY value {value!s}"
            )
        return bool(ready)

    async def _prepare(self, arguments: dict[str, Any]) -> None:
        await enter_read_write()
        if arguments and self._drive_arguments(arguments):
            await ReadWrite()
        while not self._is_ready():
            await RisingEdge(self.clk)
            await ReadWrite()


class ValueMethod(_Method):
    def __init__(self, dut: Any, name: str, clk: Any) -> None:
        super().__init__(dut, name, clk)
        self.result = _port(dut, name, name)

    async def __call__(self, **arguments: Any) -> Any:
        async with self._lock:
            await self._prepare(arguments)
            return self.result.value


class ActionMethod(_Method):
    def __init__(self, dut: Any, name: str, clk: Any) -> None:
        super().__init__(dut, name, clk)
        self.enable = _port(dut, f"EN_{name}", name)
        # Keep an idle method driven across reset.  With Icarus, a no-delay
        # deposit made before reset may be released back to Z on the next HDL
        # evaluation and poison logic shared by otherwise idle method ENs.
        self.enable.value = 0

    async def __call__(self, **arguments: Any) -> None:
        async with self._lock:
            try:
                await self._prepare(arguments)
                self.enable.value = 1
                await RisingEdge(self.clk)
                await ReadWrite()
                self.enable.set(Immediate(0))
            finally:
                # Cancellation must never leave a method enabled.
                self.enable.set(Immediate(0))


class ActionValueMethod(_Method):
    """BSV ActionValue method: arguments in, a result value out, one per fire."""

    def __init__(self, dut: Any, name: str, clk: Any) -> None:
        super().__init__(dut, name, clk)
        self.result = _port(dut, name, name)
        self.enable = _port(dut, f"EN_{name}", name)
        self.enable.value = 0

    async def __call__(self, **arguments: Any) -> Any:
        async with self._lock:
            try:
                await self._prepare(arguments)
                # BSV samples the result combinationally from the pre-edge
                # state; the combinational output flips once the edge commits
                # the method's register updates, so read it before enabling.
                result = self.result.value
                self.enable.value = 1
                await RisingEdge(self.clk)
                await ReadWrite()
                self.enable.set(Immediate(0))
                return result
            finally:
                # Cancellation must never leave a method enabled.
                self.enable.set(Immediate(0))
