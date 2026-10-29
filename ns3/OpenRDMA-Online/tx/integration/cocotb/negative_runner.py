#!/usr/bin/env python3
"""Run each expected-fatal DMA/Phase-A boundary case separately."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CASES = {
    "context_tag_mismatch": "DMA_PHASEA_CONTEXT_TAG_MISMATCH",
    "last_length_mismatch": "DMA_PHASEA_LAST_LENGTH_MISMATCH",
    "missing_last_flag": "DMA_PHASEA_MISSING_LAST_FLAG",
    "chunk_sequence": "DMA_PHASEA_CHUNK_SEQUENCE",
}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures = []
    for case, marker in CASES.items():
        env = dict(os.environ)
        env["DMA_PHASEA_NEGATIVE_CASE"] = case
        env.pop("COCOTB_TEST_FILTER", None)
        completed = subprocess.run(
            [
                sys.executable,
                str(root / "cocotb" / "sim_runner.py"),
                "--target",
                "integration",
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
        sentinel = f"DMA_PHASEA_ASSERTION_DID_NOT_FIRE_{case}"
        if marker not in output or sentinel in output:
            failures.append(case)
            print(f"FAIL {case}: expected marker {marker}")
            print(output)
        else:
            print(f"PASS {case}: {marker}")

    if failures:
        print(f"negative failures: {', '.join(failures)}")
        return 1
    print(f"PASS all {len(CASES)} expected-fatal DMA Phase-A cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
