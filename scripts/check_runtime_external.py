#!/usr/bin/env python3
"""Static privacy scan for runtime browser assets."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"https?://|cdn")
ALLOW = (
    re.compile(r"http://localhost(:[0-9]+)?"),
    re.compile(r"http://127\.0\.0\.1(:[0-9]+)?"),
    re.compile(r"https://youtube\.com/(\.\.\.|\u2026)"),
    re.compile(r'placeholder="https://youtube\.com/(\.\.\.|\u2026)'),
)


def _iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for base in (root / "src", root / "public"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            try:
                path.relative_to(root / "public" / "vault-assets")
                continue
            except ValueError:
                pass
            out.append(path)
    return out


def scan_runtime_external(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    unexpected: list[str] = []
    for path in _iter_files(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            if not PATTERN.search(line):
                continue
            if any(pattern.search(line) for pattern in ALLOW):
                continue
            unexpected.append(f"{path.relative_to(root)}:{lineno}:{line}")
    return unexpected


def main() -> int:
    unexpected = scan_runtime_external()
    if unexpected:
        print("Unexpected runtime external URL(s):", file=sys.stderr)
        print("\n".join(unexpected), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
