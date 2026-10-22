"""Cocotb 2.x phase helpers used by the local method adapters."""

from cocotb._gpi_triggers import current_gpi_trigger
from cocotb.triggers import NextTimeStep, ReadOnly, ReadWrite


def in_read_write() -> bool:
    return isinstance(current_gpi_trigger(), ReadWrite)


async def enter_read_write() -> None:
    trigger = current_gpi_trigger()
    if isinstance(trigger, ReadOnly):
        await NextTimeStep()
        trigger = current_gpi_trigger()
    if not isinstance(trigger, ReadWrite):
        await ReadWrite()
