#!/usr/bin/env python3
"""Cocotb launcher for the M2 (PacketAssembly) unit suite.

Self-contained: every python mirror lives under this folder's cocotb/ tree.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from cocotb_tools.runner import get_runner

ROOT = Path(__file__).resolve().parents[1]
COCOTB_ROOT = ROOT / "cocotb"

MODULES_SHORT = ("tests.m2.test_packetizer",)
MODULES_LONG = ("tests.m2.test_packetizer_long",)


def bsc_verilog_library() -> Path:
    executable = shutil.which(os.getenv("BSC", "bsc"))
    if executable is None:
        raise RuntimeError("configured BSC is not executable")
    library = Path(executable).resolve().parent.parent / "lib" / "Verilog"
    if not library.is_dir():
        raise RuntimeError(f"cannot locate BSC Verilog library: {library}")
    return library


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("short", "long"), default="short")
    args = parser.parse_args()
    modules = MODULES_LONG if args.suite == "long" else MODULES_SHORT

    simulator = os.getenv("SIM", "verilator")
    library = bsc_verilog_library()
    primitive_names = (
        "FIFO1.v",
        "FIFO2.v",
        "SizedFIFO.v",
        "RevertReg.v",
        "RegFile.v",
        "BRAM2.v",
    )
    primitives = [library / name for name in primitive_names if (library / name).is_file()]
    verilog_dir = ROOT / "build" / "verilog"
    sources = [*primitives, *sorted(verilog_dir.glob("*.v"))]
    top_source = verilog_dir / "mkTestM2.v"
    if not top_source.is_file():
        raise RuntimeError(f"missing generated top-level Verilog: {top_source}")

    build_dir = ROOT / "build" / "cocotb" / simulator
    build_args = ["--Wno-fatal", "--timing"] if simulator == "verilator" else []
    runner = get_runner(simulator)

    original_path = sys.path.copy()
    try:
        sys.path[:0] = [str(COCOTB_ROOT), str(COCOTB_ROOT / "lib")]
        runner.build(
            sources=sources,
            hdl_toplevel="mkTestM2",
            build_args=build_args,
            build_dir=build_dir,
            always=True,
            timescale=("1ns", "1ps"),
            waves=False,
        )
        runner.test(
            test_module=modules,
            hdl_toplevel="mkTestM2",
            build_dir=build_dir,
            test_dir=build_dir,
            extra_env=dict(os.environ),
            results_xml=f"results-{args.suite}.xml",
            waves=False,
        )
    finally:
        sys.path[:] = original_path


if __name__ == "__main__":
    main()
