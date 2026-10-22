#!/usr/bin/env python3
"""将 BSC 生成层次打包成可移动的 Quartus RTL bundle。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


REQUIRED_RTL = {
    "mkDmaEngine.v",
    "mkDmaChunkNormalizer.v",
    "mkOrderedDmaReadScheduler.v",
    "mkDmaCompletionReorder.v",
    "mkDmaMessageTransferTracker.v",
    "FIFO1.v",
    "FIFO2.v",
    "SizedFIFO.v",
}


def executable_path(command: str) -> Path:
    candidate = Path(command).expanduser()
    if candidate.parent != Path(".") or candidate.is_absolute():
        if candidate.is_file():
            return candidate.resolve()
        raise RuntimeError(f"executable does not exist: {candidate}")

    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeError(f"executable is not on PATH: {command}")
    return Path(resolved).resolve()


def collect_sources(
    bsc: Path,
    collector: Path,
    bdir: Path,
    vdir: Path,
    top: str,
) -> list[Path]:
    bluetcl = bsc.with_name("bluetcl")
    if not bluetcl.is_file():
        resolved = shutil.which("bluetcl")
        if resolved is None:
            raise RuntimeError(f"cannot find bluetcl next to {bsc} or on PATH")
        bluetcl = Path(resolved).resolve()

    bluespec_dir = bsc.parent.parent / "lib"
    if not bluespec_dir.is_dir():
        configured = os.environ.get("BLUESPECDIR")
        if configured is None or not Path(configured).is_dir():
            raise RuntimeError(f"cannot locate the BSC library directory for {bsc}")
        bluespec_dir = Path(configured).resolve()

    env = os.environ.copy()
    env["BLUESPECDIR"] = str(bluespec_dir)
    command = [
        str(bluetcl),
        str(collector),
        "-q",
        "-all",
        "-bdir",
        str(bdir),
        "-vdir",
        str(vdir),
        top,
        top,
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Bluetcl dependency collection failed:\n{detail}")

    sources: list[Path] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("<unable to locate file"):
            raise RuntimeError(line)
        path = Path(line).expanduser()
        if path.suffix.lower() != ".v":
            raise RuntimeError(f"unsupported RTL dependency returned by Bluetcl: {line}")
        if not path.is_file():
            raise RuntimeError(f"RTL dependency does not exist: {line}")
        sources.append(path.resolve())

    if not sources:
        raise RuntimeError("Bluetcl returned an empty RTL dependency set")
    return sources


def validate_sources(sources: list[Path]) -> dict[str, Path]:
    by_name: dict[str, Path] = {}
    for source in sources:
        previous = by_name.get(source.name)
        if previous is not None and previous != source:
            raise RuntimeError(
                f"duplicate RTL basename {source.name}: {previous} and {source}"
            )
        by_name[source.name] = source

    test_modules = sorted(name for name in by_name if name.startswith("mkTest"))
    if test_modules:
        raise RuntimeError(
            "testbench modules are not allowed in the Quartus bundle: "
            + ", ".join(test_modules)
        )

    missing = sorted(REQUIRED_RTL - by_name.keys())
    if missing:
        raise RuntimeError(
            "required mkDmaEngine dependencies are missing: " + ", ".join(missing)
        )
    return by_name


def write_filelist(root: Path, names: list[str]) -> None:
    content = "".join(f"rtl/{name}\n" for name in names)
    (root / "dma_engine.f").write_text(content, encoding="ascii")


def write_qip(root: Path, names: list[str]) -> None:
    lines = [
        "# 由 OpenRDMA-flow/m1 自动生成；路径相对于此 QIP 文件。",
        "# 宿主工程负责设置 TOP_LEVEL_ENTITY、device、pin 与 SDC 约束。",
    ]
    for name in names:
        lines.extend(
            [
                "set_global_assignment -name VERILOG_FILE \\",
                f'    [file join $::quartus(qip_path) "rtl" "{name}"]',
            ]
        )
    lines.append("")
    (root / "dma_engine.qip").write_text("\n".join(lines), encoding="utf-8")


def publish_bundle(output: Path, sources: dict[str, Path]) -> None:
    output = output.resolve()
    staging = output.parent / f".{output.name}.tmp"

    # 导出目标是生成物；先移除旧发布物，避免失败后误用上一版 RTL。
    for path in (output, staging):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    rtl_dir = staging / "rtl"
    rtl_dir.mkdir(parents=True)
    names = sorted(sources)
    for name in names:
        shutil.copy2(sources[name], rtl_dir / name)

    write_filelist(staging, names)
    write_qip(staging, names)
    staging.replace(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bsc", default="bsc", help="BSC executable")
    parser.add_argument("--collector", required=True, type=Path)
    parser.add_argument("--bdir", required=True, type=Path)
    parser.add_argument("--vdir", required=True, type=Path)
    parser.add_argument("--top", default="mkDmaEngine")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        bsc = executable_path(args.bsc)
        collector = args.collector.resolve()
        if not collector.is_file():
            raise RuntimeError(f"collector does not exist: {collector}")
        sources = collect_sources(
            bsc=bsc,
            collector=collector,
            bdir=args.bdir.resolve(),
            vdir=args.vdir.resolve(),
            top=args.top,
        )
        validated = validate_sources(sources)
        publish_bundle(args.output, validated)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(
        f"Exported {len(validated)} RTL files for {args.top} to "
        f"{args.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
