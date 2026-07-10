#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""
Fetch / canonicalize a source's identity. Extracted verbatim from
ingest.sh §1 (§14 refactor) — BEHAVIOR-PRESERVING.

Per schema §8: one asset (by sha256) = one source_id. If the input
matches an existing *tracked* sidecar's sha256, reuse that source_id and
asset; otherwise assign a fresh ULID, copy the asset in, and write the
sidecar. Run from the vault root (paths are cwd-relative, like the bash).

Usage:  source-identity.py <path-or-url>

stdout: shell-safe `KEY=VALUE` assignments (shlex-quoted) for the caller
        to `eval`: SOURCE_ID SHA256 ADDED ORIGIN_TYPE ORIGIN_REF DEST
        DEST_BASENAME SIDECAR EXISTING_SIDECAR (empty when newly created).
stderr: progress / error messages (prefixed `ingest:`), matching the bash.
exit 1: on any `die` condition (fetch failure, drift, collision, …).

Verified by scripts/tests/test_source_identity.sh (temp-git fixture +
golden-diff of the naming transforms against the original bash `sed`).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

SOURCES = Path("sources")  # cwd-relative, exactly like ingest.sh `sources/*.md`
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
URL_FETCH_MAX_TIME_S = os.environ.get("PW_SOURCE_FETCH_MAX_TIME_S", "120")
URL_FETCH_MAX_BYTES = os.environ.get("PW_SOURCE_FETCH_MAX_BYTES", str(100 * 1024 * 1024))


def die(msg: str) -> None:
    print(f"ingest: {msg}", file=sys.stderr)
    raise SystemExit(1)


def progress(msg: str) -> None:
    print(f"ingest: {msg}", file=sys.stderr)


def sha256_of(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def new_ulid() -> str:
    ts = int(time.time() * 1000)
    n = (ts << 80) | secrets.randbits(80)
    s = ""
    for _ in range(26):
        s = _ULID_ALPHABET[n & 0x1F] + s
        n >>= 5
    return s


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def git_tracked(path: str) -> bool:
    return subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def _field2(text: str, key: str) -> str:
    """awk '/^<key>/{print $2}' — second whitespace field of the first
    line starting with `key`. Empty string if none."""
    for line in text.splitlines():
        if line.startswith(key):
            parts = line.split()
            return parts[1] if len(parts) > 1 else ""
    return ""


def url_slug(url: str) -> str:
    # bash: sed -E 's|https?://||; s|[^A-Za-z0-9._-]+|-|g' | cut -c1-80
    s = re.sub(r"https?://", "", url, count=1)
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s)
    return s[:80]


def safe_name(name: str) -> str:
    # bash: sed -E 's/[[:space:],()]+/-/g; s/-+/-/g; s/^-//; s/-\././g'
    s = re.sub(r"[\s,()]+", "-", name)
    s = re.sub(r"-+", "-", s)
    s = re.sub(r"^-", "", s)
    s = re.sub(r"-\.", ".", s)
    return s


def yaml_squote(v: str) -> str:
    # single-quoted YAML scalar: double any embedded single quote.
    return v.replace("'", "''")


def emit(**vars_: str) -> None:
    for k, v in vars_.items():
        sys.stdout.write(f"{k}={shlex.quote(v)}\n")


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1]:
        die("usage: source-identity.py <path-or-url>")
    inp = sys.argv[1]

    # This script is cwd-relative (SOURCES, dedup via git, dest writes). Honor
    # $VAULT_CONTENT_DIR like the other helpers so it operates on the content
    # repo regardless of caller cwd. ingest already runs us with cwd=content
    # (so this is a no-op there); it also makes standalone use consistent.
    # Resolve a relative FILE input against the ORIGINAL cwd BEFORE chdir, so
    # the chdir can't strand it (URLs and absolute paths pass through).
    vcd = os.environ.get("VAULT_CONTENT_DIR")
    if vcd:
        if not re.match(r"^https?://", inp) and not os.path.isabs(inp):
            inp = os.path.abspath(inp)
        os.chdir(vcd)

    origin_type = "file"
    origin_ref = inp
    tmp_fetch: str | None = None

    try:
        if re.match(r"^https?://", inp):
            origin_type = "url"
            fd, tmp_fetch = tempfile.mkstemp(prefix="ingest-")
            os.close(fd)
            progress(f"fetching {inp}")
            # -f: non-zero on HTTP errors, so 404/500 bodies aren't stored.
            if subprocess.run([
                "curl", "-fsSL",
                "--proto", "=http,https",
                "--max-time", URL_FETCH_MAX_TIME_S,
                "--max-filesize", URL_FETCH_MAX_BYTES,
                inp, "-o", tmp_fetch,
            ]).returncode != 0:
                die(f"fetch failed for {inp}")
            sha256 = sha256_of(tmp_fetch)
            fetched = tmp_fetch
        else:
            if not Path(inp).is_file():
                die(f"not a file: {inp}")
            sha256 = sha256_of(inp)
            fetched = inp

        # Dedup over TRACKED sidecars only (untracked orphans from an
        # aborted run must not match). sorted() == shell glob order.
        existing_sidecar = ""
        if SOURCES.is_dir():
            for sc in sorted(SOURCES.glob("*.md")):
                if sc.name == "README.md":
                    continue
                if not git_tracked(str(sc)):
                    continue
                if _field2(sc.read_text(encoding="utf-8", errors="replace"), "sha256:") == sha256:
                    existing_sidecar = str(sc)
                    break

        added = iso_now()

        if existing_sidecar:
            if origin_type == "url" and tmp_fetch:
                os.remove(tmp_fetch)
                tmp_fetch = None
            sidecar = existing_sidecar
            dest = sidecar[:-3] if sidecar.endswith(".md") else sidecar  # ${SIDECAR%.md}
            sc_text = Path(sidecar).read_text(encoding="utf-8", errors="replace")
            source_id = _field2(sc_text, "source_id:")
            dest_basename = os.path.basename(dest)
            # Re-hash the stored asset; refuse if it drifted from the sidecar.
            stored = sha256_of(dest)
            expected = _field2(sc_text, "sha256:")
            if stored != expected:
                die(f"stored asset {dest} has drifted from its sidecar sha "
                    f"(got {stored[:12]}…, expected {expected[:12]}…). Restore the "
                    f"file or supersede the sidecar before re-ingesting.")
            progress(f"reusing existing source_id={source_id} (sha match)")
            progress(f"asset={dest}")
            progress(f"sidecar={sidecar}")
        else:
            if origin_type == "url":
                dest_basename = f"{today()}-{url_slug(inp)}.html"
                dest = f"sources/{dest_basename}"
                if Path(dest).exists():
                    if git_tracked(dest):
                        die(f"destination exists and is tracked: {dest} "
                            f"(different content with the same date+slug)")
                    die(f"destination exists but is UNTRACKED: {dest} (likely an "
                        f"aborted prior run; remove {dest} and {dest}.md if present, "
                        f"then re-run)")
                os.replace(fetched, dest)  # mv
                tmp_fetch = None
            else:
                dest_basename = f"{today()}-{safe_name(os.path.basename(inp))}"
                dest = f"sources/{dest_basename}"
                if Path(dest).exists():
                    if git_tracked(dest):
                        die(f"destination exists and is tracked: {dest} "
                            f"(different content with the same date+slug)")
                    die(f"destination exists but is UNTRACKED: {dest} (likely an "
                        f"aborted prior run; remove {dest} and {dest}.md if present, "
                        f"then re-run)")
                import shutil
                shutil.copyfile(inp, dest)

            source_id = new_ulid()
            sidecar = f"{dest}.md"
            Path(sidecar).write_text(
                "---\n"
                f"source_id: {source_id}\n"
                "type: source\n"
                f"sha256: {sha256}\n"
                f"added: {added}\n"
                f"origin_type: {origin_type}\n"
                f"origin_ref: '{yaml_squote(origin_ref)}'\n"
                "supersedes: null\n"
                f"title: '{yaml_squote(dest_basename)}'\n"
                "---\n\n"
                f"# {dest_basename}\n\n"
                "Auto-generated sidecar. Do not hand-edit.\n",
                encoding="utf-8",
            )
            progress(f"new source_id={source_id}")
            progress(f"sidecar={sidecar}")
            existing_sidecar = ""
    except SystemExit:
        if tmp_fetch and os.path.exists(tmp_fetch):
            os.remove(tmp_fetch)
        raise

    emit(
        SOURCE_ID=source_id, SHA256=sha256, ADDED=added,
        ORIGIN_TYPE=origin_type, ORIGIN_REF=origin_ref,
        DEST=dest, DEST_BASENAME=dest_basename, SIDECAR=sidecar,
        EXISTING_SIDECAR=existing_sidecar,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
