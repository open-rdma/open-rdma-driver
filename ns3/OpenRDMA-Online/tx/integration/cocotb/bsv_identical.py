#!/usr/bin/env python3
"""Compare two BSV sources ignoring comments and blank lines.

The module folders each own a copy of the shared type packages, and the
preflight check below fails the build when a copy drifts: BSC resolves
packages through -p in order, so a divergent copy would silently change which
definition the integration build sees.

A byte-exact `cmp` is the strictest form of that check, but it also rejects
copies that differ only in comments -- which is exactly what happens when
someone annotates one copy while studying it.  Since comments cannot change
which definition BSC picks, this script strips `//` line comments and
`/* ... */` block comments and compares the remaining tokens.

Exit status is 0 when the code is identical, 1 when it differs, and 2 when a
file cannot be read.

Usage: bsv_identical.py <left.bsv> <right.bsv>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"//[^\n]*")
STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')


def code_only(path: str) -> list[str]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    # Keep string literals opaque: a `//` inside one is not a comment.
    literals: list[str] = []

    def stash(match: re.Match[str]) -> str:
        literals.append(match.group(0))
        return f"\x00{len(literals) - 1}\x00"

    text = STRING_LITERAL.sub(stash, text)
    text = BLOCK_COMMENT.sub("", text)
    text = LINE_COMMENT.sub("", text)

    def restore(token: str) -> str:
        def replace(match: re.Match[str]) -> str:
            return literals[int(match.group(1))]

        return re.sub(r"\x00(\d+)\x00", replace, token)

    return [
        restore(line).rstrip()
        for line in text.splitlines()
        if restore(line).strip()
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    try:
        left = code_only(argv[1])
        right = code_only(argv[2])
    except OSError as error:
        print(f"bsv_identical: {error}", file=sys.stderr)
        return 2
    if left == right:
        return 0

    import difflib

    diff = difflib.unified_diff(left, right, argv[1], argv[2], lineterm="")
    print("\n".join(diff), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
