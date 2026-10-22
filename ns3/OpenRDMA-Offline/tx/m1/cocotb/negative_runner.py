#!/usr/bin/env python3
"""Run each expected-fatal DMA protocol case in an isolated simulator process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CASES = {
    "unknown_tag": "DMA_ASSERT_UNKNOWN_TAG",
    "zero_keep": "DMA_ASSERT_ZERO_KEEP",
    "noncontiguous_keep": "DMA_ASSERT_NONCONTIGUOUS_KEEP",
    "early_last": "DMA_ASSERT_BURST_LAST",
    "late_last": "DMA_ASSERT_BURST_LAST",
    "over_completion": "DMA_ASSERT_OVER_COMPLETION",
    "late_completion": "DMA_ASSERT_UNKNOWN_TAG",
    "duplicate_message_id": "DMA_ASSERT_DUPLICATE_WQE_TAG",
    "duplicate_message_id_stalled_last": "DMA_ASSERT_DUPLICATE_WQE_TAG",
    "address_overflow": "DMA_ASSERT_CHUNK_ADDRESS_OVERFLOW",
    "zero_chunk": "DMA_ASSERT_ZERO_CHUNK",
    "chunk_too_large": "DMA_ASSERT_CHUNK_TOO_LARGE",
    "chunk_crosses_4k": "DMA_ASSERT_CHUNK_CROSSES_4K",
    "missing_first": "DMA_ASSERT_MISSING_CHUNK_FIRST",
    "chunk_sequence": "DMA_ASSERT_CHUNK_SEQUENCE",
    "chunk_tag_changed": "DMA_ASSERT_CHUNK_TAG_CHANGED",
}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for case, marker in CASES.items():
        env = dict(os.environ)
        env["DMA_NEGATIVE_CASE"] = case
        env.pop("COCOTB_TEST_FILTER", None)
        completed = subprocess.run(
            [
                sys.executable,
                str(root / "cocotb" / "sim_runner.py"),
                "--target",
                "dma",
                "--suite",
                "negative",
            ],
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output = completed.stdout
        sentinel = f"DMA_ASSERTION_DID_NOT_FIRE_{case}"
        if marker not in output or sentinel in output:
            failures.append(case)
            print(f"FAIL {case}: expected marker {marker}")
            print(output)
        else:
            print(f"PASS {case}: {marker}")

    if failures:
        print(f"negative failures: {', '.join(failures)}")
        return 1
    print(f"PASS all {len(CASES)} expected-fatal DMA cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
