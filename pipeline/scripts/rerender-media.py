#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""
`--rerender` for media sources (expansion-plan §7.5) — re-render a committed
transcript's markdown from its (byte-unchanged) `.transcript.json` after a
`render_format_version` bump, WITHOUT re-hitting the ASR service.

The JSON is unchanged, so segment text + times — and every cited time range —
are identical; only the markdown rendering differs. So this is a pure provenance
migration: a NEW source_id `supersedes:` the old, and every live
`[src:<old>#mm:ss]` citation is repointed to the new id (via
scripts/rewrite-citations.py) so nothing is orphaned. Transactional: stage all
artifacts + rewrites, then commit together; abort with nothing moved on failure.

Usage:  rerender-media.py <video_id-or-watch-url> [--force]
  (no-op + exit 0 if the source is already at the current render_format_version,
   unless --force.) Run with cwd = the content repo (honors $VAULT_CONTENT_DIR).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

import media_resolver  # shared head-resolver (§8.0); sibling module, same venv
from media_resolver import (  # shared script utilities (single source of truth)
    die, hhmmss, iso_now, new_ulid, progress, sha256_of,
)

RENDER_FORMAT_VERSION = 1  # keep in sync with media-identity.py
SCRIPTS = Path(__file__).resolve().parent


def render_markdown(doc: dict, title: str, canonical_url: str) -> str:
    """Identical render contract to media-identity.py (floor start / ceil end)."""
    lines = [f"# {title}", "", f"<{canonical_url}>", ""]
    for seg in doc["segments"]:
        start = hhmmss(seg["start"])
        end = hhmmss(math.ceil(seg["end"]))
        spk = f"{seg['speaker']}: " if seg.get("speaker") else ""
        lines.append(f"[{start}-{end}] {spk}{(seg.get('text') or '').strip()}")
    return "\n".join(lines) + "\n"


def find_head(video_id: str) -> tuple[Path, dict]:
    """The non-superseded sidecar for video_id, via the shared die-loud resolver
    (§8.0) — same rule as media-identity, no longer a divergent silent-skip copy.
    Unlike media-identity's resolve_head, rerender requires a head to exist, so a
    missing source is itself a die()."""
    try:
        head = media_resolver.resolve_head(Path("sources"), ("youtube_video_id", (video_id,)))
    except media_resolver.ResolverError as exc:
        die(str(exc))
    if head is None:
        die(f"no committed media source for video_id={video_id}")
    return head


def main() -> int:
    ap = argparse.ArgumentParser(prog="rerender-media.py")
    ap.add_argument("ref", help="video_id or watch URL")
    ap.add_argument("--force", action="store_true", help="rerender even if already current")
    args = ap.parse_args()
    if os.environ.get("VAULT_CONTENT_DIR"):
        os.chdir(os.environ["VAULT_CONTENT_DIR"])

    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})|youtu\.be/([A-Za-z0-9_-]{11})", args.ref)
    video_id = (m.group(1) or m.group(2)) if m else (args.ref if re.fullmatch(r"[A-Za-z0-9_-]{11}", args.ref) else None)
    if not video_id:
        die(f"could not derive video_id from {args.ref!r}")

    sidecar, fm = find_head(video_id)
    media = fm.get("media") or {}
    old_id = fm["source_id"]
    cur = media.get("render_format_version")
    if cur == RENDER_FORMAT_VERSION and not args.force:
        progress(f"source {old_id} already at render_format_version={cur} — nothing to do")
        return 0

    old_md = Path(str(sidecar)[:-3])              # <slug>.transcript.md
    old_json = old_md.with_suffix(".json")         # <slug>.transcript.json
    if not old_json.is_file():
        die(f"committed transcript JSON missing: {old_json}")
    if media.get("transcript_json_sha256") and sha256_of(old_json) != media["transcript_json_sha256"]:
        die(f"{old_json} drifted from its sidecar hash — refusing to rerender")
    doc = json.loads(old_json.read_text(encoding="utf-8"))

    new_id = new_ulid()
    canonical = media.get("canonical_url") or f"https://www.youtube.com/watch?v={video_id}"
    title = fm.get("title") or canonical
    base = old_md.name[: -len(".transcript.md")]
    new_base = f"{base}.r{RENDER_FORMAT_VERSION}"
    new_md = Path("sources", f"{new_base}.transcript.md")
    new_json = Path("sources", f"{new_base}.transcript.json")
    new_sidecar = Path(f"{new_md}.md")
    for t in (new_md, new_json, new_sidecar):
        if t.exists():
            die(f"rerender target already exists: {t}")

    # stage artifacts in temp
    stage = Path(__import__("tempfile").mkdtemp(prefix="rerender-"))
    md_tmp = stage / new_md.name
    json_tmp = stage / new_json.name
    md_tmp.write_text(render_markdown(doc, title, canonical), encoding="utf-8")
    shutil.copyfile(old_json, json_tmp)
    new_sha = sha256_of(md_tmp)
    new_json_sha = sha256_of(json_tmp)

    # new sidecar: carry the old media: block forward, updating the changed fields
    new_fm = dict(fm)
    new_fm["source_id"] = new_id
    new_fm["sha256"] = new_sha
    new_fm["added"] = iso_now()
    new_fm["supersedes"] = f"[[{old_id}]]"
    nm = dict(media)
    nm["transcript_json_sha256"] = new_json_sha
    nm["render_format_version"] = RENDER_FORMAT_VERSION
    nm["transcribed"] = iso_now()
    new_fm["media"] = nm
    sidecar_text = ("---\n" + yaml.safe_dump(new_fm, sort_keys=False, allow_unicode=True)
                    + "---\n\n" + f"# {title}\n\nAuto-generated media sidecar. Do not hand-edit.\n")

    # commit-stage: move artifacts in (sidecar LAST), then rewrite citations
    shutil.move(str(md_tmp), str(new_md))
    shutil.move(str(json_tmp), str(new_json))
    new_sidecar.write_text(sidecar_text, encoding="utf-8")
    progress(f"rerendered {old_id} → {new_id} (render_format_version={RENDER_FORMAT_VERSION})")

    rc = subprocess.run([f"{SCRIPTS}/rewrite-citations.py", old_id, new_id]).returncode
    if rc != 0:
        die("citation rewrite failed (artifacts staged but not committed — inspect + git checkout)")

    Path(".wiki").mkdir(exist_ok=True)
    with open(".wiki/log.md", "a", encoding="utf-8") as f:
        f.write(f"{iso_now()}  {new_id}  pages: (rerender supersedes {old_id})\n")
    subprocess.run(["git", "-c", "core.quotepath=false", "add",
                    str(new_md), str(new_json), str(new_sidecar),
                    "wiki", ".wiki/log.md"], check=False)
    subprocess.run(["git", "-c", "core.quotepath=false", "commit", "-m",
                    f"rerender: {new_id} supersedes {old_id} (render_format_version={RENDER_FORMAT_VERSION})"],
                   check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
