#!/usr/bin/env python3
"""Build and run the ns-3 server with the OpenRDMA Verilator client."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def wait_ready(process: subprocess.Popen[str], timeout: float) -> list[str]:
    assert process.stdout is not None
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            remainder = process.stdout.read()
            if remainder:
                lines.extend(remainder.splitlines())
            raise RuntimeError(
                f"Lab 7 server exited before READY (status {process.returncode})\n"
                + "\n".join(lines)
            )
        readable, _, _ = select.select([process.stdout], [], [], 0.2)
        if not readable:
            continue
        line = process.stdout.readline()
        if not line:
            continue
        print(line, end="")
        lines.append(line.rstrip())
        if line.startswith("READY "):
            return lines
    raise TimeoutError("timed out waiting for Lab 7 server")


def remove_stale_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"refusing to remove non-socket path: {path}")
    path.unlink()


def main() -> int:
    lab_dir = Path(__file__).resolve().parent
    root = lab_dir.parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--openrdma",
        type=Path,
        default=Path(os.environ.get("OPENRDMA_DIR", root.parent / "tx")),
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(os.environ.get("COCOTB_PYTHON", "/home/l/miniconda3/envs/cocotb2/bin/python3")),
    )
    parser.add_argument("--ready-timeout", type=float, default=60.0)
    parser.add_argument("--cosim-timeout", type=float, default=300.0)
    args = parser.parse_args()

    openrdma = args.openrdma.resolve()
    integration = openrdma / "integration"
    ns3 = root / "ns-3" / "ns3"
    if not ns3.is_file():
        raise RuntimeError("ns-3 is not built; run make build first")
    if not (integration / "Makefile").is_file():
        raise RuntimeError(f"OpenRDMA integration tree not found: {integration}")
    if not args.python.is_file():
        raise RuntimeError(f"cocotb Python not found: {args.python}")

    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    socket_path = results / "lab7.sock"
    output_path = results / "lab7.json"
    remove_stale_socket(socket_path)

    build_env = os.environ.copy()
    build_env.update({"PYTHON": str(args.python), "SIM": "verilator"})
    subprocess.run(
        ["make", "verilog", "TARGET=m0chain", f"PYTHON={args.python}"],
        cwd=integration,
        env=build_env,
        check=True,
    )

    server = subprocess.Popen(
        [str(ns3), "run", "--no-build", f"lab7 --socket={socket_path}"],
        cwd=root / "ns-3",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    try:
        wait_ready(server, args.ready_timeout)
        cosim_env = build_env.copy()
        cosim_env.update(
            {
                "LAB7_SOCKET": str(socket_path),
                "LAB7_OUTPUT": str(output_path),
                "PYTHONUNBUFFERED": "1",
            }
        )
        completed = subprocess.run(
            [str(args.python), "cocotb/sim_runner.py", "--target", "m0chain", "--suite", "lab7"],
            cwd=integration,
            env=cosim_env,
            timeout=args.cosim_timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Verilator/cocotb exited with status {completed.returncode}")

        try:
            tail, _ = server.communicate(timeout=30)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Lab 7 server did not exit after FINISH") from error
        if tail:
            print(tail, end="")
        if server.returncode != 0:
            raise RuntimeError(f"Lab 7 server exited with status {server.returncode}")
        if not output_path.is_file():
            raise RuntimeError("Lab 7 did not write its result JSON")
        print(f"Lab 7 PASS: {output_path}")
        return 0
    finally:
        stop_process(server)
        if socket_path.exists() and stat.S_ISSOCK(socket_path.lstat().st_mode):
            socket_path.unlink()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.SubprocessError, TimeoutError) as error:
        print(f"Lab 7 orchestration ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
