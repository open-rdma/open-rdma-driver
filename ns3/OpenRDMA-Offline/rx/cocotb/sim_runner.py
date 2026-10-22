#!/usr/bin/env python3
"""Target-aware Cocotb launcher for the RX MVP."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from cocotb_tools.runner import get_runner


@dataclass(frozen=True)
class TargetConfig:
    hdl_toplevel: str
    short_modules: tuple[str, ...]
    long_modules: tuple[str, ...]


TARGETS = {
    "m0_packet_buffer": TargetConfig(
        "mkTestRxPacketBuffer",
        ("tests.m0_packet_buffer.test_packet_buffer",),
        ("tests.m0_packet_buffer.test_packet_buffer",),
    ),
    "m1_packet_parser": TargetConfig(
        "mkTestRxPacketParser",
        ("tests.m1_packet_parser.test_packet_parser",),
        ("tests.m1_packet_parser.test_packet_parser",),
    ),
    "m2_rq_bitmap": TargetConfig(
        "mkTestRxRqBitmap",
        ("tests.m2_rq_bitmap.test_rq_bitmap",),
        ("tests.m2_rq_bitmap.test_rq_bitmap",),
    ),
    "m3_dma_placement": TargetConfig(
        "mkTestRxDmaPlacement",
        ("tests.m3_dma_placement.test_dma_placement",),
        ("tests.m3_dma_placement.test_dma_placement",),
    ),
    "m4_ordered_queue": TargetConfig(
        "mkTestRxOrderedQueue",
        ("tests.m4_ordered_queue.test_ordered_queue",),
        ("tests.m4_ordered_queue.test_ordered_queue",),
    ),
    "rx_top": TargetConfig(
        "mkTestRxTop",
        ("tests.integration.test_rx_top",),
        ("tests.integration.test_rx_top",),
    ),
    "tx_rx_loopback": TargetConfig(
        "mkTestTxRxLoopback",
        ("tests.integration.test_tx_rx_loopback",),
        ("tests.integration.test_tx_rx_loopback",),
    ),
}

RX_ROOT = Path(__file__).resolve().parents[1]
COCOTB_ROOT = RX_ROOT / "cocotb"
# Upstream keeps cocotb_bsv one level above this folder, shared with the mvp
# unit trees.  This tree is self-contained: rx/cocotb/lib carries its own copy.
MVP_COCOTB_LIB = COCOTB_ROOT / "lib"


def bsc_verilog_library() -> Path:
    configured = os.getenv("BSC", "bsc")
    executable = shutil.which(configured)
    if executable is None:
        raise RuntimeError(f"configured BSC is not executable: {configured}")
    library = Path(executable).resolve().parent.parent / "lib" / "Verilog"
    if not library.is_dir():
        raise RuntimeError(f"cannot locate BSC Verilog library: {library}")
    return library


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--suite", choices=("short", "long"), default="short")
    args = parser.parse_args()

    target = TARGETS[args.target]
    modules = target.short_modules if args.suite == "short" else target.long_modules
    simulator = os.getenv("SIM", "verilator")
    verilog_dir = RX_ROOT / "build" / args.target / "verilog"
    generated = sorted(verilog_dir.glob("*.v"))
    top_source = verilog_dir / f"{target.hdl_toplevel}.v"
    if not top_source.is_file():
        raise RuntimeError(f"missing generated top-level Verilog: {top_source}")

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
    build_dir = RX_ROOT / "build" / args.target / "cocotb" / simulator
    build_args = ["--Wno-fatal", "--timing"] if simulator == "verilator" else []
    extra_env = dict(os.environ)
    extra_env["RX_MVP_SUITE"] = args.suite
    runner = get_runner(simulator)

    original_path = sys.path.copy()
    try:
        sys.path[:0] = [str(COCOTB_ROOT), str(MVP_COCOTB_LIB)]
        runner.build(
            sources=[*primitives, *generated],
            hdl_toplevel=target.hdl_toplevel,
            build_args=build_args,
            build_dir=build_dir,
            always=True,
            timescale=("1ns", "1ps"),
            waves=False,
        )
        runner.test(
            test_module=modules,
            hdl_toplevel=target.hdl_toplevel,
            build_dir=build_dir,
            test_dir=build_dir,
            extra_env=extra_env,
            results_xml=f"results-{args.target}-{args.suite}.xml",
            waves=False,
        )
    finally:
        sys.path[:] = original_path


if __name__ == "__main__":
    main()
