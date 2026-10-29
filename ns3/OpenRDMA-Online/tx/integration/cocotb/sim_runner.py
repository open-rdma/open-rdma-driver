#!/usr/bin/env python3
"""Cocotb launcher for the integration suites.

DmaPhaseAIntegration end-to-end (mkTestDmaPhaseAIntegration) and the M0 chain
(mkTestM0Chain: TxMvp -> M0MessageAdapter -> DmaPhaseAIntegration, GDRP
submission through four Ethernet frame channels).  Self-contained: every
python mirror lives under this folder's cocotb/ tree.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cocotb_tools.runner import get_runner


@dataclass(frozen=True)
class TargetConfig:
    hdl_toplevel: str
    short_modules: tuple[str, ...]
    long_modules: tuple[str, ...]
    long_env: dict[str, str] = field(default_factory=dict)
    negative_modules: tuple[str, ...] = ()


TARGETS = {
    "integration": TargetConfig(
        hdl_toplevel="mkTestDmaPhaseAIntegration",
        short_modules=("tests.test_end_to_end",),
        long_modules=("tests.test_long",),
        negative_modules=("tests.test_protocol_assertions",),
    ),
    "m0chain": TargetConfig(
        hdl_toplevel="mkTestM0Chain",
        short_modules=("tests.test_m0_chain",),
        long_modules=("tests.test_m0_chain_long",),
    ),
}

ROOT = Path(__file__).resolve().parents[1]
COCOTB_ROOT = ROOT / "cocotb"


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
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--suite", choices=("short", "long", "negative"), default="short")
    args = parser.parse_args()

    target = TARGETS[args.target]
    if args.suite == "short":
        modules = target.short_modules
    elif args.suite == "long":
        modules = target.long_modules
    else:
        if not target.negative_modules:
            raise RuntimeError(f"negative suite is not implemented for target {args.target}")
        modules = target.negative_modules

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
    verilog_dir = ROOT / "build" / args.target / "verilog"
    top_source = verilog_dir / f"{target.hdl_toplevel}.v"
    generated = sorted(verilog_dir.glob("*.v"))
    if not top_source.is_file():
        raise RuntimeError(f"missing generated top-level Verilog: {top_source}")

    build_leaf = simulator
    if args.suite == "negative":
        build_leaf = (
            f"{simulator}-negative-"
            f"{os.environ.get('DMA_PHASEA_NEGATIVE_CASE', 'unset')}"
        )
    build_dir = ROOT / "build" / args.target / "cocotb" / build_leaf
    build_args = ["--Wno-fatal", "--timing"] if simulator == "verilator" else []
    runner = get_runner(simulator)
    extra_env = dict(os.environ)
    extra_env["DMA_PHASEA_SUITE"] = args.suite
    if args.suite == "long":
        for name, value in target.long_env.items():
            extra_env.setdefault(name, value)

    original_path = sys.path.copy()
    try:
        sys.path[:0] = [str(COCOTB_ROOT), str(COCOTB_ROOT / "lib")]
        runner.build(
            sources=[*primitives, *generated],
            hdl_toplevel=target.hdl_toplevel,
            build_args=build_args,
            build_dir=build_dir,
            always=True,
            timescale=("1ns", "1ps"),
            waves=False,
        )
        case = os.environ.get("DMA_PHASEA_NEGATIVE_CASE", "all")
        results_xml = (
            f"results-{args.target}-{args.suite}-{case}.xml"
            if args.suite == "negative"
            else f"results-{args.target}-{args.suite}.xml"
        )
        runner.test(
            test_module=modules,
            hdl_toplevel=target.hdl_toplevel,
            build_dir=build_dir,
            test_dir=build_dir,
            extra_env=extra_env,
            results_xml=results_xml,
            waves=False,
        )
    finally:
        sys.path[:] = original_path


if __name__ == "__main__":
    main()
