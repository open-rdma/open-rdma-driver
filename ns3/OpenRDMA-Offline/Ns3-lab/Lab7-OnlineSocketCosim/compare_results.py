#!/usr/bin/env python3
"""Require offline Lab 6 and online Lab 7 to produce the same network result."""

from __future__ import annotations

import json
from pathlib import Path
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    lab6 = json.loads((root / "results/lab6.json").read_text())
    lab7 = json.loads((root / "results/lab7.json").read_text())
    for key in ("timestamp_boundary", "tx_frames", "rx_frames", "messages", "tx", "events"):
        if lab6[key] != lab7[key]:
            raise RuntimeError(f"Lab 6/Lab 7 mismatch in {key}")
    print("Lab 6/Lab 7 equivalence PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, ValueError, RuntimeError) as error:
        print(f"comparison ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
