#!/usr/bin/env python3
"""Run one stage of the offline TX -> ns-3 -> RX flow.

    python3 ns3/cocotb/offline_runner.py tx      # RTL -> results/tx-*.csv
    python3 ns3/cocotb/offline_runner.py ns3     # -> results/rx-trace.csv
    python3 ns3/cocotb/offline_runner.py rx      # -> results/rx-replay.json
    python3 ns3/cocotb/offline_runner.py all

The TX and RX stages each compile their Verilog with `make verilog` in their
own tree, build a Verilator model with cocotb, and run one test module from
this folder.  The ns-3 stage compiles `ns3/offline-txrx.cc` against an
existing ns-3 build and runs it.

Run this with the cocotb 2 interpreter (see COCOTB_PYTHON in the Makefile).

Environment:
    NS3_DIR          ns-3 tree to compile against (default Ns3-lab/ns-3)
    OFFLINE_RESULTS  directory for the CSV/JSON artifacts (default <tree>/results)
    BSC, SIM         forwarded to the module makefiles, like the upstream trees
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

from offline_lib import NS3_ROOT, RX_ROOT, TREE_ROOT, TX_ROOT, artifact, results_dir

COCOTB_ROOT = NS3_ROOT / "cocotb"
BSC_PRIMITIVES = (
    "FIFO1.v",
    "FIFO2.v",
    "SizedFIFO.v",
    "RevertReg.v",
    "RegFile.v",
    "BRAM2.v",
)


def default_ns3_dir() -> Path:
    override = os.environ.get("NS3_DIR")
    if override:
        return Path(override).expanduser().resolve()
    # The ns-3 engine is shared with the lab tree rather than copied: it is
    # over a gigabyte, and the labs keep it configured and built.
    return (TREE_ROOT / "Ns3-lab" / "ns-3").resolve()


def bsc_library() -> Path:
    executable = shutil.which(os.getenv("BSC", "bsc"))
    if executable is None:
        raise SystemExit("configured BSC is not executable")
    library = Path(executable).resolve().parent.parent / "lib" / "Verilog"
    if not library.is_dir():
        raise SystemExit(f"cannot locate the BSC Verilog library: {library}")
    return library


def test_verdict(results_xml: Path) -> tuple[int, int, str]:
    """(tests, failures, first failure) from cocotb's JUnit XML."""
    total = 0
    failed = 0
    first = ""
    for case in ElementTree.parse(results_xml).iter("testcase"):
        total += 1
        for failure in case.iter("failure"):
            failed += 1
            if not first:
                # cocotb writes error_msg="..."; pytest uses a body instead.
                message = (failure.get("error_msg") or failure.get("message") or "").strip()
                lines = [line for line in (failure.text or "").splitlines() if line.strip()]
                kind = (failure.get("error_type") or "").strip()
                first = message or (lines[-1].strip() if lines else "failed")
                if kind and not first.startswith(kind):
                    first = f"{kind}: {first}"
    return total, failed, first


def clear_artifacts(*names: str) -> None:
    """Drop this stage's outputs first.

    Every stage writes its report only after all of its checks pass, so a
    failed run would otherwise leave the previous run's ``"status": "PASS"``
    in place and ``make ns3``/``make rx`` would happily consume it.
    """
    for name in names:
        path = artifact(name)
        if path.is_file():
            path.unlink()


def run_verilog(tree: Path, target: str) -> None:
    """Build the testbench Verilog with the module tree's own makefile."""
    env = dict(os.environ)
    env.setdefault("BSC", "bsc")
    print(f"[build] {tree.name}: make verilog TARGET={target}")
    subprocess.run(
        ["make", "verilog", f"TARGET={target}", f"PYTHON={sys.executable}"],
        cwd=tree,
        env=env,
        check=True,
    )


def build_and_test(
    *,
    tree: Path,
    target: str,
    hdl_toplevel: str,
    test_module: str,
    extra_paths: list[Path],
) -> None:
    from cocotb_tools.runner import get_runner

    verilog_dir = tree / "build" / target / "verilog"
    top_source = verilog_dir / f"{hdl_toplevel}.v"
    if not top_source.is_file():
        raise SystemExit(f"missing generated top-level Verilog: {top_source}")
    generated = sorted(verilog_dir.glob("*.v"))

    library = bsc_library()
    primitives = [
        library / name for name in BSC_PRIMITIVES if (library / name).is_file()
    ]

    simulator = os.getenv("SIM", "verilator")
    build_dir = tree / "build" / target / "cocotb" / simulator
    build_args = ["--Wno-fatal", "--timing"] if simulator == "verilator" else []
    runner = get_runner(simulator)

    # cocotb_tools.runner forwards the parent's sys.path to the simulator
    # process through PYTHONPATH, which is how the test module finds both this
    # folder and the module tree's own python mirrors.
    original_path = sys.path.copy()
    try:
        sys.path[:0] = [str(path) for path in extra_paths]
        print(f"[build] {tree.name}: verilator model for {hdl_toplevel}")
        runner.build(
            sources=[*primitives, *generated],
            hdl_toplevel=hdl_toplevel,
            build_args=build_args,
            build_dir=build_dir,
            always=True,
            timescale=("1ns", "1ps"),
            waves=False,
        )
        print(f"[test]  {tree.name}: {test_module}")
        results_xml = build_dir / f"results-{target}.xml"
        runner.test(
            test_module=test_module,
            hdl_toplevel=hdl_toplevel,
            build_dir=build_dir,
            test_dir=build_dir,
            extra_env=dict(os.environ),
            results_xml=results_xml.name,
            waves=False,
        )
    finally:
        sys.path[:] = original_path

    # cocotb_tools.runner only turns the results file into an exit code when
    # it is itself running under pytest; the simulator process exits 0 even
    # when a test failed, so the harness has to read the verdict itself.
    if not results_xml.is_file():
        raise SystemExit(
            f"{test_module}: no results file at {results_xml}; "
            "the simulator did not report a verdict"
        )
    total, failed, first = test_verdict(results_xml)
    if failed or not total:
        raise SystemExit(
            f"{test_module}: {failed} of {total} cocotb tests failed"
            + (f": {first}" if first else "")
            + f" (see {results_xml})"
        )
    print(f"[test]  {tree.name}: {test_module} PASS ({total}/{total})")


def stage_tx() -> None:
    clear_artifacts("tx-trace.csv", "tx-frames.csv", "tx-messages.csv", "tx-connection.csv")
    run_verilog(TX_ROOT / "integration", "m0chain")
    build_and_test(
        tree=TX_ROOT / "integration",
        target="m0chain",
        hdl_toplevel="mkTestM0Chain",
        test_module="tx_export",
        extra_paths=[
            COCOTB_ROOT,
            TX_ROOT / "integration" / "cocotb",
            TX_ROOT / "integration" / "cocotb" / "lib",
        ],
    )


def stage_rx() -> None:
    clear_artifacts("rx-replay.json")
    run_verilog(RX_ROOT, "rx_top")
    build_and_test(
        tree=RX_ROOT,
        target="rx_top",
        hdl_toplevel="mkTestRxTop",
        test_module="rx_replay",
        extra_paths=[
            COCOTB_ROOT,
            RX_ROOT / "cocotb",
            RX_ROOT / "cocotb" / "lib",
        ],
    )


def stage_ns3(extra_args: list[str]) -> None:
    clear_artifacts("rx-trace.csv", "ns3-offline.json")
    for stale in results_dir().glob("ns3-offline*.pcap"):
        stale.unlink()
    ns3_dir = default_ns3_dir()
    include_dir = ns3_dir / "build" / "include"
    lib_dir = ns3_dir / "build" / "lib"
    if not include_dir.is_dir() or not lib_dir.is_dir():
        raise SystemExit(
            f"{ns3_dir} is not a configured ns-3 build (no build/include or build/lib)"
        )

    # Discover the version and profile the tree was built with, so this keeps
    # working across ns-3 releases instead of hard-coding "ns3.48-...-debug".
    cores = sorted(lib_dir.glob("libns3.*-core-*.so"))
    if not cores:
        raise SystemExit(f"no libns3 core library under {lib_dir}")
    prefix = cores[0].name.split("-")[0]  # e.g. "libns3.48"
    version = prefix.removeprefix("lib")
    libraries = [
        str(path)
        for path in sorted(lib_dir.glob(f"{prefix}-*.so"))
        if "-test-" not in path.name
    ]
    if not libraries:
        raise SystemExit(f"no linkable libns3 libraries under {lib_dir}")

    build_dir = NS3_ROOT / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    binary = build_dir / f"{version}-offline-txrx"

    compile_command = [
        os.getenv("CXX", "c++"),
        "-g",
        "-O2",
        "-std=c++23",
        "-Wall",
        "-Wpedantic",
        "-DNS3_ASSERT_ENABLE",
        "-DNS3_BUILD_PROFILE_DEBUG",
        "-DNS3_LOG_ENABLE",
        "-D__LINUX__",
        f"-DPROJECT_SOURCE_PATH=\"{ns3_dir}\"",
        f"-I{include_dir}",
        f"-I{NS3_ROOT}",
        str(NS3_ROOT / "offline-txrx.cc"),
        "-o",
        str(binary),
        f"-L{lib_dir}",
        f"-Wl,-rpath,{lib_dir}",
        "-Wl,--no-as-needed",
        *libraries,
        "-Wl,--as-needed",
    ]
    print(f"[build] ns-3: {binary.name} against {ns3_dir}")
    subprocess.run(compile_command, check=True)

    results = results_dir()
    run_command = [
        str(binary),
        f"--txTrace={results / 'tx-trace.csv'}",
        f"--frameOracle={results / 'tx-frames.csv'}",
        f"--messageOracle={results / 'tx-messages.csv'}",
        f"--connection={results / 'tx-connection.csv'}",
        f"--rxTrace={results / 'rx-trace.csv'}",
        f"--output={results / 'ns3-offline.json'}",
        f"--pcapPrefix={results / 'ns3-offline'}",
        *extra_args,
    ]
    print(f"[run]   ns-3: {' '.join(run_command)}")
    subprocess.run(run_command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "stage", choices=("tx", "ns3", "rx", "all"), help="which stage to run"
    )
    parser.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="extra arguments forwarded to the ns-3 program",
    )
    args = parser.parse_args()

    if args.stage in ("tx", "all"):
        stage_tx()
    if args.stage in ("ns3", "all"):
        stage_ns3(args.extra)
    if args.stage in ("rx", "all"):
        stage_rx()


if __name__ == "__main__":
    main()
