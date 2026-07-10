"""Shared, zero-dependency helpers for the vault's scripts.

Stdlib-only (no pyyaml, no third-party) so even the dependency-free scripts
(add-page-id.py, rewrite-citations.py) can import these without inheriting a
yaml dep. Sidecar/frontmatter parsing — which needs pyyaml — lives in
media_resolver.py instead, which re-exports these for the media front-door
scripts. The frozen source-identity.py oracle keeps its own copies by design.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# `.wiki/log.md` line parser. Ingest writes two spaces before the `pages:`
# field; requiring that delimiter keeps labels like "Front pages: a history"
# intact while still allowing spaces inside chapter labels.
LOG_LINE_RX = re.compile(r"^\S+\s+([0-9A-Z]{26})(?:#(.*))?\s{2,}pages:")


def _log_prefix() -> str:
    run_id = os.environ.get("PW_RUN_ID", "").strip()
    return f"ingest[{run_id}]" if run_id else "ingest"


def die(msg: str) -> None:
    print(f"{_log_prefix()}: {msg}", file=sys.stderr)
    raise SystemExit(1)


def default_vault_root(tooling_root: str | Path) -> Path:
    """Resolve the content repo for both current and legacy layouts."""
    override = os.environ.get("PW_CONTENT_DIR") or os.environ.get("VAULT_CONTENT_DIR")
    if override:
        return Path(override).expanduser().resolve()

    root = Path(tooling_root).resolve()
    candidates = (root.parent / "content", root / "content")
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def progress(msg: str) -> None:
    print(f"{_log_prefix()}: {msg}", file=sys.stderr)


def sha256_of(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_log_line(line: str) -> tuple[str, str | None] | None:
    """Return (source_id, optional chapter label) for one ingest log line."""
    m = LOG_LINE_RX.match(line)
    if not m:
        return None
    label = m.group(2)
    return m.group(1), label.strip() if label is not None else None


def chapter_order_from_lines(lines: list[str], source_id: str) -> list[str]:
    """Distinct chapter labels for a source in first-appearance order.

    Label-less lines are dropped, so callers that need to detect whole-source
    completion should use parse_log_line directly.
    """
    order: list[str] = []
    seen: set[str] = set()
    for line in lines:
        parsed = parse_log_line(line)
        if not parsed or parsed[0] != source_id:
            continue
        label = (parsed[1] or "").strip()
        if label and label not in seen:
            seen.add(label)
            order.append(label)
    return order


def new_ulid() -> str:
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    s = ""
    for _ in range(26):
        s = _ULID_ALPHABET[n & 0x1F] + s
        n >>= 5
    return s


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def split_frontmatter(text: str) -> tuple[str, str, str] | None:
    """Return (prefix, frontmatter_body, body) for YAML frontmatter, if present."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    after = text[end + 4:]
    if after.startswith("\n"):
        after = after[1:]
    return "", text[4:end], after


def git_tracked(path: str) -> bool:
    return subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def hhmmss(seconds: float) -> str:
    """floor for a range start / used by the renderer + validator (shared)."""
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
