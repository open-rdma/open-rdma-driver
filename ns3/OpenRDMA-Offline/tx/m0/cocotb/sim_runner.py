#!/usr/bin/env python3
"""Cocotb launcher for the M0 (TxMvp) unit suite.

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

SHORT_MODULES = (
    "tests.tx.test_config",
    "tests.tx.test_tx",
    "tests.tx.test_backpressure",
    "tests.tx.test_randomized",
)
LONG_MODULES = ("tests.tx.test_randomized",)
LONG_ENV = {"TX_MVP_REQUEST_COUNT": "4096"}


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

    simulator = os.getenv("SIM", "verilator")
    modules = LONG_MODULES if args.suite == "long" else SHORT_MODULES
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
    top_source = verilog_dir / "mkTestTxMvp.v"
    if not top_source.is_file():
        raise RuntimeError(f"missing generated top-level Verilog: {top_source}")

    build_dir = ROOT / "build" / "cocotb" / simulator
    build_args = ["--Wno-fatal", "--timing"] if simulator == "verilator" else []
    runner = get_runner(simulator)
    extra_env = dict(os.environ)
    if args.suite == "long":
        for name, value in LONG_ENV.items():
            extra_env.setdefault(name, value)

    original_path = sys.path.copy()
    try:
        sys.path[:0] = [str(COCOTB_ROOT), str(COCOTB_ROOT / "lib")]
        runner.build(
            sources=sources,
            hdl_toplevel="mkTestTxMvp",
            build_args=build_args,
            build_dir=build_dir,
            always=True,
            timescale=("1ns", "1ps"),
            waves=False,
        )
        runner.test(
            test_module=modules,
            hdl_toplevel="mkTestTxMvp",
            build_dir=build_dir,
            test_dir=build_dir,
            extra_env=extra_env,
            results_xml=f"results-{args.suite}.xml",
            waves=False,
        )
    finally:
        sys.path[:] = original_path


if __name__ == "__main__":
    main()
