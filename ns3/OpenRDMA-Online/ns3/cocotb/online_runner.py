#!/usr/bin/env python3
"""Run the online TX <-> ns-3 <-> RX co-simulation.

    python3 ns3/cocotb/online_runner.py all    # the whole flow
    python3 ns3/cocotb/online_runner.py hub    # only the network hub
    python3 ns3/cocotb/online_runner.py tx     # only the TX stage (needs a hub)
    python3 ns3/cocotb/online_runner.py rx     # only the RX stage (needs a hub)
    python3 ns3/cocotb/online_runner.py tx --role   # the same, as a child process

`all` compiles the hub, starts it, and then runs the two RTL stages side by
side, each in its own process, each building its own Verilator model first and
connecting to the hub when its test starts.  The stages are started together
and talk to each other only through the hub, so the RX replays arrivals while
the TX is still producing them.

`tx` and `rx` on their own expect a hub that is already listening -- which is
how a single stage is debugged without rebuilding the other one.

Run this with the cocotb 2 interpreter (see COCOTB_PYTHON in the Makefile).

Environment:
    NS3_DIR            ns-3 tree to compile against (default ../Ns3-lab/ns-3)
    ONLINE_RESULTS     directory for the three reports (default <tree>/results)
    ONLINE_TX_SOCKET   override the hub's TX socket path
    ONLINE_RX_SOCKET   override the hub's RX socket path
    BSC, SIM           forwarded to the module makefiles, like the upstream trees
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from xml.etree import ElementTree

from online_lib import (
    NS3_ROOT,
    RX_ROOT,
    TREE_ROOT,
    TX_ROOT,
    artifact,
    results_dir,
    rx_socket_path,
    sockets_dir,
    tx_socket_path,
)

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
    # The ns-3 engine is shared with the offline tree rather than copied: it is
    # over a gigabyte, it is already configured and built, and both flows run
    # against the same library set.  Point NS3_DIR at another checkout to use a
    # different one.
    return (TREE_ROOT.parent / "OpenRDMA-Offline" / "Ns3-lab" / "ns-3").resolve()


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
    in place.
    """
    for name in names:
        path = artifact(name)
        if path.is_file():
            path.unlink()


def run_verilog(tree: Path, target: str) -> None:
    """Build the testbench Verilog with the module tree's own makefile."""
    env = dict(os.environ)
    env.setdefault("BSC", "bsc")
    print(f"[build] {tree.name}: make verilog TARGET={target}", flush=True)
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
        print(f"[build] {tree.name}: verilator model for {hdl_toplevel}", flush=True)
        runner.build(
            sources=[*primitives, *generated],
            hdl_toplevel=hdl_toplevel,
            build_args=build_args,
            build_dir=build_dir,
            always=True,
            timescale=("1ns", "1ps"),
            waves=False,
        )
        print(f"[test]  {tree.name}: {test_module}", flush=True)
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
    print(f"[test]  {tree.name}: {test_module} PASS ({total}/{total})", flush=True)


# ---------------------------------------------------------------------------
# The hub
# ---------------------------------------------------------------------------


def ns3_build() -> tuple[Path, Path, list[str]]:
    """(include directory, library directory, libraries) of a configured ns-3 build."""
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
    libraries = [
        str(path)
        for path in sorted(lib_dir.glob(f"{prefix}-*.so"))
        if "-test-" not in path.name
    ]
    if not libraries:
        raise SystemExit(f"no linkable libns3 libraries under {lib_dir}")
    return include_dir, lib_dir, libraries


def build_hub() -> Path:
    include_dir, lib_dir, libraries = ns3_build()
    build_dir = NS3_ROOT / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    binary = build_dir / "online-cosim"

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
        f"-DPROJECT_SOURCE_PATH=\"{default_ns3_dir()}\"",
        f"-I{include_dir}",
        f"-I{NS3_ROOT}",
        str(NS3_ROOT / "online-cosim.cc"),
        "-o",
        str(binary),
        f"-L{lib_dir}",
        f"-Wl,-rpath,{lib_dir}",
        "-Wl,--no-as-needed",
        *libraries,
        "-Wl,--as-needed",
    ]
    print(f"[build] ns-3: {binary.name} against {default_ns3_dir()}", flush=True)
    subprocess.run(compile_command, check=True)
    return binary


def hub_command(binary: Path, extra_args: list[str]) -> list[str]:
    return [
        str(binary),
        f"--txSocket={tx_socket_path()}",
        f"--rxSocket={rx_socket_path()}",
        f"--output={results_dir() / 'online-ns3.json'}",
        f"--pcapPrefix={results_dir() / 'online-ns3'}",
        # The message trace is an artifact like the reports: it is what lets a
        # reader line a run up against the protocol table in the manual.
        f"--wireTrace={results_dir() / 'online-ns3.wire'}",
        *extra_args,
    ]


def start_hub(binary: Path, extra_args: list[str]) -> subprocess.Popen:
    """Start the hub and wait until both of its sockets are listening."""
    command = hub_command(binary, extra_args)
    print(f"[run]   hub: {' '.join(command)}", flush=True)
    hub = subprocess.Popen(
        command, stdout=subprocess.PIPE, text=True, bufsize=1
    )
    assert hub.stdout is not None
    for line in hub.stdout:
        print(line, end="", flush=True)
        if line.startswith("READY "):
            break
    else:
        hub.wait()
        raise SystemExit(f"the hub exited with {hub.returncode} before it was listening")

    # Keep showing what the hub has to say (its summary, or why a check
    # failed) without holding up the stages.
    def relay() -> None:
        for line in hub.stdout:  # type: ignore[union-attr]
            print(line, end="", flush=True)

    threading.Thread(target=relay, daemon=True).start()
    return hub


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def stage_tx() -> None:
    clear_artifacts("online-tx.json")
    run_verilog(TX_ROOT / "integration", "m0chain")
    build_and_test(
        tree=TX_ROOT / "integration",
        target="m0chain",
        hdl_toplevel="mkTestM0Chain",
        test_module="tx_stream",
        extra_paths=[
            COCOTB_ROOT,
            TX_ROOT / "integration" / "cocotb",
            TX_ROOT / "integration" / "cocotb" / "lib",
        ],
    )


def stage_rx() -> None:
    clear_artifacts("online-rx.json")
    run_verilog(RX_ROOT, "rx_top")
    build_and_test(
        tree=RX_ROOT,
        target="rx_top",
        hdl_toplevel="mkTestRxTop",
        test_module="rx_stream",
        extra_paths=[
            COCOTB_ROOT,
            RX_ROOT / "cocotb",
            RX_ROOT / "cocotb" / "lib",
        ],
    )


def stage_all(extra_args: list[str]) -> None:
    clear_artifacts("online-ns3.json", "online-tx.json", "online-rx.json")
    for stale in results_dir().glob("online-ns3*.pcap"):
        stale.unlink()
    (results_dir() / "online-ns3.wire").unlink(missing_ok=True)
    for stale in sockets_dir().glob("*.sock"):
        stale.unlink()
    binary = build_hub()
    hub = start_hub(binary, extra_args)

    # Both stages build their own model and then connect, so they are started
    # together: whichever finishes building first waits on the hub, and the
    # network runs as soon as the TX declares its oracle.
    failures: list[str] = []
    children = []
    try:
        for role in ("tx", "rx"):
            children.append(
                (
                    role,
                    subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), role, "--role"]
                    ),
                )
            )
        failures = [
            f"the {role.upper()} stage exited with {status}"
            for role, child in children
            if (status := child.wait()) != 0
        ]
    finally:
        for _, child in children:
            if child.poll() is None:
                child.kill()
        try:
            hub.wait(timeout=60)
        except subprocess.TimeoutExpired:
            hub.kill()
            failures.append("the hub did not exit after both stages finished")

    if failures:
        raise SystemExit("; ".join(failures) + f" (see {results_dir()})")
    if hub.returncode != 0:
        raise SystemExit(f"the hub exited with {hub.returncode} (see {results_dir()})")
    print(f"[done]  three reports in {results_dir()}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "stage", choices=("all", "hub", "tx", "rx"), help="which part to run"
    )
    parser.add_argument(
        "--role",
        action="store_true",
        help="internal: this process is one of `all`'s two stages, not the driver",
    )
    parser.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="extra arguments forwarded to the hub",
    )
    args = parser.parse_args()

    if args.stage == "tx":
        stage_tx()
    elif args.stage == "rx":
        stage_rx()
    elif args.stage == "all":
        stage_all(args.extra)
    else:
        clear_artifacts("online-ns3.json")
        hub = start_hub(build_hub(), args.extra)
        hub.wait()


if __name__ == "__main__":
    main()
