#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""
Media front door for the LLM-wiki (Phase 2, expansion-plan §7) — the media
analogue of source-identity.py, kept SEPARATE so the frozen source-identity
oracle stays untouched.

Turns a YouTube URL into a committed, citable transcript by delegating ASR to
the external `transcript-remote` service (expansion-plan §7.1). The vault never
holds media bytes; the committed `<slug>.transcript.md` (timestamped, speaker-
labeled) is the canonical cited artifact (top-level sha256), with a committed
`<slug>.transcript.json` audit/render-source artifact (media.transcript_json_sha256).

Pipeline (§7.2), transaction-shaped — nothing touches sources/ until the payload
validates; artifacts are temp-staged then moved (sidecar LAST):
  derive video_id + canonical URL (local) → dedup on media.video_id BEFORE the
  network (head-of-chain; ambiguous ⇒ die; reuse ⇒ re-hash both artifacts +
  short-circuit) → transcript-remote -f json → timing HARD gate + coverage gate
  → render md + json + truncated prompt copy (temp) → hash → atomic move → emit.

stdout: shell-safe KEY=VALUE lines for ingest.py to eval — the source-identity
        contract (SOURCE_ID SHA256 ADDED ORIGIN_TYPE ORIGIN_REF DEST
        DEST_BASENAME SIDECAR EXISTING_SIDECAR) PLUS media extras
        (TEXT_FILE AUDIT_JSON). EXISTING_SIDECAR is set on the reuse path.
stderr: progress/errors prefixed `ingest:`.
exit 1: any die condition.

Run with cwd = the content repo (ingest.py sets it; honors $VAULT_CONTENT_DIR).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path


import media_resolver  # shared head-resolver (§8.0); sibling module, same venv
from media_resolver import (  # shared script utilities (single source of truth)
    die, git_tracked, hhmmss, iso_now, new_ulid, parse_frontmatter, progress,
    sha256_of, today,
)

SOURCES = Path("sources")  # cwd-relative, like source-identity.py
RENDER_FORMAT_VERSION = 1
_VID_RX = (
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|v/)([A-Za-z0-9_-]{11})"),
)
# Coverage gate: min transcript chars per second of *speech* (Σ seg spans).
_COVERAGE_MIN = {"en": 3.0, "zh": 1.5}
_COVERAGE_DEFAULT = 3.0  # strictest, for unlisted languages


def yaml_squote(v: str) -> str:
    """Escape a string for a single-quoted YAML scalar. Folds CR/LF/control chars to a
    space FIRST: a raw newline or control char inside a single-quoted scalar breaks the
    frontmatter (or silently re-folds on re-parse), so every untrusted service/user
    string routed through y()/yopt() is sanitized at this one chokepoint. Clean ASCII
    text is byte-unchanged (the hardened YouTube sidecar stays byte-identical)."""
    v = "".join(" " if (c == "\n" or ord(c) < 0x20) else c for c in v.replace("\r", ""))
    return v.replace("'", "''")


def y(v) -> str:
    """Single-quoted YAML scalar for a sidecar line."""
    return f"'{yaml_squote(str(v))}'"


def _yopt(v, *, quote_bools: bool = False) -> str:
    """Render a possibly-missing value as a YAML scalar.

    Most sidecars keep YAML bools as real bools. Image-note OCR params deliberately
    quote bools for historical compatibility; keep that as an explicit flag.
    """
    if v is None:
        return "null"
    if isinstance(v, bool) and not quote_bools:
        return "true" if v else "false"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return y(v)


def yopt(v) -> str:
    """Render a possibly-missing value as a YAML scalar (null/bool/num/str)."""
    return _yopt(v)


def emit(**vars_: str) -> None:
    for k, v in vars_.items():
        sys.stdout.write(f"{k}={shlex.quote(v)}\n")


# ── identity ────────────────────────────────────────────────────────────────

def extract_video_id(url: str) -> str | None:
    for rx in _VID_RX:
        m = rx.search(url)
        if m:
            return m.group(1)
    return None


# ── transcript rendering ──────────────────────────────────────────────────────

def render_markdown(doc: dict, title: str, canonical_url: str) -> str:
    """Deterministic timestamped + speaker-labeled markdown: one line per
    segment `[H:MM:SS-H:MM:SS] SPEAKER_xx: text` (shared rounding: floor start /
    ceil end)."""
    lines = [f"# {title}", "", f"<{canonical_url}>", ""]
    for seg in doc["segments"]:
        start = hhmmss(seg["start"])  # floor
        end = hhmmss(math.ceil(seg["end"]))
        spk = f"{seg['speaker']}: " if seg.get("speaker") else ""
        text = (seg.get("text") or "").strip()
        lines.append(f"[{start}-{end}] {spk}{text}")
    return "\n".join(lines) + "\n"


def render_prompt_copy(doc: dict, limit: int) -> str:
    """Deterministic long-media sampling (§7.7), by MEDIA-TIMELINE start:
    (a) every segment with start < 300; (b) for each k>=1, segments with
    k*1200 <= start < k*1200+120. Gap markers between non-adjacent selections;
    drop whole trailing windows to fit `limit` codepoints."""
    segs = sorted(doc["segments"], key=lambda s: s["start"])

    def window_of(start: float) -> int:
        if start < 300:
            return 0
        k = int(start // 1200)
        return k if (k * 1200) <= start < (k * 1200 + 120) else -1

    # group selected segments by window index, in order
    windows: list[tuple[int, list[dict]]] = []
    for s in segs:
        w = window_of(s["start"])
        if w < 0:
            continue
        if not windows or windows[-1][0] != w:
            windows.append((w, []))
        windows[-1][1].append(s)

    def assemble(ws: list[tuple[int, list[dict]]]) -> str:
        out: list[str] = []
        prev_end: float | None = None
        for _, segs_w in ws:
            for s in segs_w:
                if prev_end is not None and s["start"] - prev_end > 2:
                    out.append(f"[…gap {hhmmss(prev_end)}–{hhmmss(s['start'])}…]")
                spk = f"{s['speaker']}: " if s.get("speaker") else ""
                out.append(f"{spk}{(s.get('text') or '').strip()}")
                prev_end = s["end"]
        return "\n".join(out) + "\n"

    while windows:
        text = assemble(windows)
        if len(text) <= limit:
            return text
        windows.pop()  # drop whole trailing window
    return ""


# ── main ──────────────────────────────────────────────────────────────────────

def _safe_str(v):
    """Sanitize an untrusted service/user string field before it enters an identity
    key or a YAML sidecar: drop CR, fold LF/other control chars to a space, strip.
    A raw newline in a single-quoted YAML scalar folds to a space on re-parse, so the
    in-memory key and the resolver-re-read key would diverge → a silent duplicate."""
    if not isinstance(v, str):
        return v
    return "".join(" " if (c == "\n" or ord(c) < 0x20) else c for c in v.replace("\r", "")).strip()


def _safe_scalar_token(v) -> str:
    """A conservative token for an untrusted value embedded RAW (unquoted) into a YAML
    block scalar — `asr_engine: whisperx@<v>` and the byte-identical youtube
    `language: <v>` line. _safe_str folds newlines but keeps a `:` (which would split the
    scalar into a phantom mapping and corrupt the frontmatter), so for these few unquoted
    sinks keep only [A-Za-z0-9._+-] and drop the rest. Byte-identical for a normal version
    ('3.1.1') or language ('en', 'zh-Hant')."""
    return re.sub(r"[^A-Za-z0-9._+-]", "", v) if isinstance(v, str) else ""


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _canon_feed_url(url: str) -> str:
    """Canonicalize a feed URL for identity (schema §7.1): lower-case scheme+host,
    strip a leading `www.`, drop a default port (80/443), drop the fragment, and
    collapse a trailing slash. Path and query are left byte-as-is — NOT %-decoded:
    decoding reserved delimiters (`%3F`→`?`, `%23`→`#`, `%2F`→`/`) could move bytes
    across URL components or merge distinct paths. Identity only needs this to be a
    DETERMINISTIC function applied consistently, which it is. (Userinfo credentials
    are dropped — they don't belong in committed YAML.)"""
    from urllib.parse import urlsplit, urlunsplit
    clean = _safe_str(url) or ""
    try:
        sp = urlsplit(clean)
    except ValueError:
        return clean
    if not sp.scheme:
        return clean
    scheme = sp.scheme.lower()
    host = (sp.hostname or "")
    if host.startswith("www."):
        host = host[4:]
    port = sp.port
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = host + (f":{port}" if port else "")
    path = sp.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, sp.query, ""))


def _build_media_sidecar(*, source_id, sha, added, origin_type, origin_ref, title,
                         media_lines: list[str], supersedes=None) -> str:
    lines = [
        "---",
        f"source_id: {source_id}",
        "type: source",
        f"sha256: {sha}",
        f"added: {added}",
        f"origin_type: {origin_type}",
        f"origin_ref: {y(origin_ref)}",
        (f"supersedes: '[[{supersedes}]]'" if supersedes else "supersedes: null"),
        f"title: {y(title)}",
        "media:",
        *media_lines,
        "---",
        "",
        f"# {title}",
        "",
        "Auto-generated media sidecar. Do not hand-edit.",
        "",
    ]
    return "\n".join(lines)


def _asr_engine(meta: dict) -> str:
    wx_ver = _safe_scalar_token(meta.get("whisperx_version"))  # untrusted → token (emitted raw)
    return f"whisperx@{wx_ver}" if wx_ver else "whisperx"


def _transcript_server(meta: dict) -> str | None:
    server_ver = meta.get("server_version")
    return f"transcript@{server_ver}" if server_ver else None


def _storage_guard_lines() -> list[str]:
    return [
        "  source_sha256: null",
        "  source_bytes_stored: false",
        "  upstream_drift_guard: none",
    ]


def _asr_recipe_lines(*, speech, lang, meta, transcript_tool, language_quoted=True) -> list[str]:
    return [
        f"  speech_duration_s: {int(round(speech))}",
        f"  duration_s: {yopt(meta.get('duration_s'))}",
        "  transcript_kind: asr",
        f"  transcript_tool: {transcript_tool}",
        f"  transcript_server: {yopt(_transcript_server(meta))}",
        f"  asr_engine: {_asr_engine(meta)}",
        f"  asr_model: {y(meta.get('model', ''))}",
        f"  device: {y(meta.get('device') or '')}",
        f"  compute_type: {y(meta.get('compute_type', ''))}",
        f"  diarize_requested: {str(bool(meta.get('diarized'))).lower()}",
        f"  language: {y(lang) if language_quoted else lang}",
    ]


def _media_resolver_own_orphan(identity, origin_ref):
    def check(ofm: dict, _dest_json: str) -> bool:
        try:
            same_identity = media_resolver.identity_key(ofm) == identity
        except media_resolver.ResolverError:
            same_identity = False
        return ofm.get("origin_ref") == origin_ref and same_identity
    return check


def _git_tracked_under(path: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--", f"{path.rstrip('/')}/"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    return bool(result.stdout.strip())


def _evidence_file_hashes(fm: dict) -> dict[str, str]:
    artifacts = (fm.get("media") or {}).get("evidence_artifacts") or []
    if not isinstance(artifacts, list):
        return {}
    return {
        item["path"]: item["sha256"]
        for item in artifacts
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("sha256"), str)
        and "bundle_recipe" not in item
    }


def _verify_owned_orphan_paths(*, sidecar: str, fm: dict, dest_md: str,
                               dest_json: str, dest_assets: str | None) -> None:
    """Prove every pre-existing untracked payload belongs to this sidecar.

    A matching identity alone is insufficient: a user file can share the
    deterministic slug. Existing bytes must also match hashes recorded by the
    coherent sidecar, and asset directories may contain no unlisted files.
    """
    expected_md = fm.get("sha256")
    if Path(dest_md).exists():
        if not Path(dest_md).is_file() or not isinstance(expected_md, str):
            die(f"untracked canonical artifact is not verifiably owned: {dest_md}")
        if sha256_of(dest_md) != expected_md:
            die(f"untracked canonical artifact does not match its sidecar: {dest_md}")

    evidence = _evidence_file_hashes(fm)
    media = fm.get("media") or {}
    expected_json = evidence.get(dest_json) or media.get("transcript_json_sha256")
    if Path(dest_json).exists():
        if not Path(dest_json).is_file() or not isinstance(expected_json, str):
            die(f"untracked audit artifact is not verifiably owned: {dest_json}")
        if sha256_of(dest_json) != expected_json:
            die(f"untracked audit artifact does not match its sidecar: {dest_json}")

    if not dest_assets or not Path(dest_assets).exists():
        return
    assets = Path(dest_assets)
    if not assets.is_dir() or assets.is_symlink():
        die(f"untracked assets path is not a verifiable directory: {dest_assets}")
    expected_under = {
        path: digest for path, digest in evidence.items()
        if Path(path).is_relative_to(assets)
    }
    if not expected_under:
        die(f"untracked assets dir has no ownership evidence in {sidecar}: {dest_assets}")
    actual: set[str] = set()
    for path in assets.rglob("*"):
        if path.is_symlink():
            die(f"untracked assets dir contains a symlink; refusing to replace: {path}")
        if not path.is_file():
            continue
        rel = path.as_posix()
        actual.add(rel)
        expected = expected_under.get(rel)
        if not expected or sha256_of(path) != expected:
            die(f"untracked asset is unlisted or does not match its sidecar: {rel}")
    if actual != set(expected_under):
        missing = sorted(set(expected_under) - actual)
        die(f"untracked assets dir is incomplete for its sidecar: {', '.join(missing)}")


def _stage_media_artifacts(*, slug, md_path, json_path, sidecar, sidecar_text,
                           md_suffix, json_suffix, origin_ref, identity=None,
                           assets_path=None, assets_suffix=None,
                           own_orphan_check=None) -> None:
    """Move staged media artifacts into sources/, writing sidecar last.

    The own-orphan rule is shared across media families. Most use resolver
    identity; the legacy YouTube transcript path keeps its stricter JSON-hash
    orphan check by passing a custom callback.
    """
    dest_md = f"sources/{slug}{md_suffix}"
    dest_json = f"sources/{slug}{json_suffix}"
    dest_assets = f"sources/{slug}{assets_suffix}" if assets_suffix else None
    targets = (dest_md, dest_json, sidecar)
    for t in targets:
        if Path(t).is_symlink():
            die(f"target is a symlink (refusing to replace or follow it): {t}")
        if Path(t).exists() and git_tracked(t):
            die(f"target already tracked (refusing to overwrite): {t}")
    if dest_assets and Path(dest_assets).exists() and _git_tracked_under(dest_assets):
        die(f"assets dir has git-tracked files (refusing to clobber): {dest_assets}")
    existing_payload = any(Path(t).exists() for t in (dest_md, dest_json))
    existing_payload = existing_payload or bool(dest_assets and Path(dest_assets).exists())
    if existing_payload and not Path(sidecar).is_file():
        die("pre-existing untracked media artifacts have no coherent ownership sidecar; "
            f"refusing to overwrite: {dest_md}")
    if Path(sidecar).exists():
        if not Path(sidecar).is_file():
            die(f"untracked sidecar path is not a file: {sidecar}")
        ofm = parse_frontmatter(Path(sidecar))
        check = own_orphan_check or _media_resolver_own_orphan(identity, origin_ref)
        if not check(ofm, dest_json):
            die(f"untracked sidecar exists and is not a recognizable own-orphan: {sidecar}")
        if (ofm.get("type") != "source"
                or not re.fullmatch(r"[0-9A-Z]{26}", str(ofm.get("source_id", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(ofm.get("sha256", "")))):
            die(f"untracked sidecar lacks a coherent source identity/hash: {sidecar}")
        _verify_owned_orphan_paths(
            sidecar=sidecar, fm=ofm, dest_md=dest_md,
            dest_json=dest_json, dest_assets=dest_assets,
        )
    Path("sources").mkdir(exist_ok=True)
    shutil.move(md_path, dest_md)
    shutil.move(json_path, dest_json)
    if dest_assets:
        if Path(dest_assets).exists():
            shutil.rmtree(dest_assets)  # untracked own-orphan partial (guarded above) — replace
        shutil.move(assets_path, dest_assets)
    Path(sidecar).write_text(sidecar_text, encoding="utf-8")  # sidecar LAST = commit marker


def _transcript_audit_path(dest: str) -> str:
    return dest[: -len(".transcript.md")] + ".transcript.json"


def _cards_audit_path(dest: str) -> str:
    return dest[: -len(".cards.md")] + ".cards.json"


def _verify_reuse_hashes(checks: tuple[tuple[str, str | None, str], ...], *, drift_detail=False) -> None:
    for path, expected, label in checks:
        if not Path(path).is_file():
            die(f"committed {label} missing for reuse: {path}")
        if not expected:
            die(f"sidecar is missing the {label} hash — cannot verify no-drift: {path}")
        got = sha256_of(path)
        if got != expected:
            if drift_detail:
                die(f"{label} drifted from sidecar ({got[:12]}… vs {expected[:12]}…): {path}")
            die(f"{label} drifted from sidecar: {path}")


def _reuse_committed_source(head, *, origin_ref, origin_type, progress_message, text_tmp_prefix,
                            canonical_label, limit, audit_path_fn, prompt_text_fn,
                            needs_transcript_audit=True, audit_optional=False,
                            verify_evidence=False, evidence_condition=None,
                            drift_detail=False) -> int:
    sidecar, fm = head
    dest = str(sidecar)[:-3]
    audit = audit_path_fn(dest)
    media = fm.get("media") or {}
    checks: list[tuple[str, str | None, str]] = [(dest, fm.get("sha256"), canonical_label)]
    if needs_transcript_audit:
        checks.append((audit, media.get("transcript_json_sha256"), "transcript.json"))
    _verify_reuse_hashes(tuple(checks), drift_detail=drift_detail)
    if verify_evidence or (evidence_condition and evidence_condition(dest, media)):
        _verify_evidence_or_die(fm, sidecar)
    progress(progress_message.format(source_id=fm.get("source_id")))
    text_file = tempfile.mkstemp(prefix=text_tmp_prefix)[1]
    Path(text_file).write_text(prompt_text_fn(dest, audit, limit), encoding="utf-8")
    emit(
        SOURCE_ID=fm["source_id"], SHA256=fm.get("sha256", ""), ADDED=iso_now(),
        ORIGIN_TYPE=origin_type, ORIGIN_REF=origin_ref, DEST=dest,
        DEST_BASENAME=os.path.basename(dest), SIDECAR=str(sidecar),
        EXISTING_SIDECAR=str(sidecar), TEXT_FILE=text_file,
        AUDIT_JSON=(audit if (not audit_optional or Path(audit).is_file()) else ""),
    )
    return 0


def _build_podcast_sidecar(*, source_id, sha, added, origin_ref, title, canon_feed,
                           guid, enclosure, published, basis, resolution_source,
                           speech, lang, meta, json_sha, supersedes=None) -> str:
    """The §7.2 podcast sidecar (platform: podcast) — RSS identity + ASR recipe +
    evidence_artifacts[] for the committed .transcript.json audit artifact."""
    media_lines = [
        "  platform: podcast",
        f"  feed_url: {y(canon_feed)}",
        f"  episode_guid: {yopt(guid)}",
        f"  enclosure_url: {yopt(enclosure)}",
        f"  published: {yopt(published)}",
        f"  identity_basis: {basis}",
        f"  resolution_source: {yopt(resolution_source)}",
        *_storage_guard_lines(),
        *_asr_recipe_lines(speech=speech, lang=lang, meta=meta, transcript_tool="extract-remote"),
        f"  transcript_job_id: {yopt(meta.get('job_id'))}",
        # transcript_json_sha256 is the active drift guard (what lint reads today);
        # media.evidence_artifacts[] (schema §7.2) lands with the §8.4 lint
        # generalization — NOT carried here yet, to avoid two competing hash fields.
        f"  transcript_json_sha256: {json_sha}",
        f"  render_format_version: {RENDER_FORMAT_VERSION}",
        f"  transcribed: {added}",
    ]
    return _build_media_sidecar(
        source_id=source_id, sha=sha, added=added, origin_type="audio",
        origin_ref=origin_ref, title=title, media_lines=media_lines,
        supersedes=supersedes,
    )


def _stage_into_sources_podcast(slug, md_path, json_path, sidecar, sidecar_text,
                                identity, origin_ref) -> None:
    """Podcast analogue of _stage_into_sources: md → json → sidecar(LAST), refusing
    to clobber a TRACKED file; an untracked sidecar must be a recognizable
    own-orphan (same origin_ref + same generalized identity). `identity` is the full
    ``(basis, key)`` pair returned by media_resolver.identity_key (NOT the bare key).
    A sidecar-less md/json partial (no sidecar) is an incomplete own-orphan from a
    prior interrupted run → safe to replace."""
    _stage_media_artifacts(
        slug=slug, md_path=md_path, json_path=json_path, sidecar=sidecar,
        sidecar_text=sidecar_text, md_suffix=".transcript.md",
        json_suffix=".transcript.json", identity=identity, origin_ref=origin_ref,
    )


def _resolve_head_or_die(target):
    try:
        return media_resolver.resolve_head(SOURCES, target)
    except media_resolver.ResolverError as exc:
        die(str(exc))


def _image_notes_with_bundle(platform: str, bundle_sha: str) -> list[tuple]:
    """Every committed image_note sidecar whose `media.image_bundle_sha256` matches (same
    platform). The resolver keys identity on ONE basis, so a bundle ingest can't see a
    prior image_post_id source and vice versa — but the bundle sha is a deterministic
    function of the images, so scanning by it catches the SAME images under EITHER
    identity_basis. Returns (sidecar, fm) pairs."""
    out: list[tuple] = []
    superseded = media_resolver.superseded_ids(SOURCES)  # skip non-head (re-OCR'd) sources
    for sc in media_resolver.find_media_sidecars(SOURCES):
        fm = parse_frontmatter(sc)
        media = fm.get("media") or {}
        if fm.get("origin_type") != "image_note" or media.get("platform") != platform:
            continue
        if fm.get("source_id") in superseded:
            continue  # a re-OCR'd predecessor — its bundle belongs to its successor head
        # Match on the RECOMPUTED bundle (deterministic from the candidate's committed
        # .cards.json images) — NOT the mutable scalar, which a stale/corrupt edit could
        # set away from the truth to hide a duplicate. An unreadable committed audit is a
        # HARD ERROR (not a scalar fallback): we can't prove these images aren't already
        # committed there, and minting a duplicate is worse than aborting until it's fixed.
        name = str(sc)
        audit = (name[: -len(".cards.md.md")] + ".cards.json") if name.endswith(".cards.md.md") else None
        if not audit or not Path(audit).is_file():
            die(f"committed image_note {sc} has no readable .cards.json — cannot safely "
                f"dedup; repair it before re-ingesting.")
        # The recomputed bundle is only trustworthy if the candidate's .cards.json itself
        # hasn't DRIFTED from its committed cards_json evidence hash — a drifted audit would
        # recompute a wrong bundle, hide this existing source from the scan, and let a
        # duplicate be minted. Verify it (die-loud on drift; repair before re-ingesting).
        cards_json_sha = next((a.get("sha256") for a in (media.get("evidence_artifacts") or [])
                               if isinstance(a, dict) and a.get("role") == "cards_json"), None)
        if not cards_json_sha:
            die(f"committed image_note {sc} has no cards_json evidence hash — cannot safely dedup.")
        if sha256_of(audit) != cards_json_sha:
            die(f"committed image_note {sc} has a drifted .cards.json — repair it before re-ingesting.")
        try:
            rows = json.loads(Path(audit).read_text(encoding="utf-8"))
            recomputed = _image_bundle_sha256(rows) if isinstance(rows, list) else None
        except (OSError, ValueError, KeyError, TypeError):
            recomputed = None
        if recomputed is None:
            die(f"committed image_note {sc} has an unreadable/invalid .cards.json — cannot "
                f"safely dedup; repair it before re-ingesting.")
        if recomputed == bundle_sha:
            out.append((sc, fm))
    return out


def _podcast_reuse(head, origin_ref, basis, limit) -> int:
    """Reuse an existing committed podcast head (re-hash both artifacts for
    no-silent-drift, regenerate the prompt copy, emit the reuse contract)."""
    return _reuse_committed_source(
        head, origin_ref=origin_ref, origin_type="audio",
        progress_message=f"reusing existing source_id={{source_id}} ({basis} match)",
        text_tmp_prefix="ingest-podcast-", canonical_label="transcript.md",
        limit=limit, audit_path_fn=_transcript_audit_path,
        prompt_text_fn=lambda _dest, audit, cap: render_prompt_copy(_load_json(audit), cap),
    )


def _podcast_main(args) -> int:
    """Podcast (audio_extraction) front door (§8.1). A feed-scoped identity needs a
    feed URL, so --feed-url (or a positional feed URL) is required. When the GUID is
    known we dedup BEFORE the network (skip a wasted ASR job); otherwise we invoke
    `extract-remote` (the service resolves the feed + ASRs the episode) and dedup on
    the service-resolved identity afterwards — the vault stays vault-agnostic (no RSS
    parsing here)."""
    feed_url = args.feed_url or (
        args.source if args.source.startswith(("http://", "https://")) else "")
    if not feed_url:
        die("podcast needs --feed-url (or a positional feed URL): a feed-scoped identity requires it")
    if not (args.episode_guid or args.episode_url
            or (args.episode_title and args.episode_published)):
        die("a bare podcast feed URL is not one episode — pass --episode-guid / --episode-url "
            "/ (--episode-title + --episode-published)")
    canon_feed = _canon_feed_url(feed_url)
    origin_ref = args.source or feed_url

    # 0. pre-network dedup when the GUID is known — the feed_guid key is fully
    #    deterministic locally, so we needn't pay for an ASR job to discover a dup.
    #    SKIP this optimization on --retranscribe (we WANT a fresh ASR to supersede with).
    supersedes_id = None
    if args.episode_guid and not args.retranscribe:
        head = _resolve_head_or_die(("feed_guid", (canon_feed, args.episode_guid)))
        if head:
            return _podcast_reuse(head, origin_ref, "feed_guid", args.limit)

    # 1. extract via the service ($EXTRACT_REMOTE_CMD overrides; mirrors $TRANSCRIPT_REMOTE_CMD)
    out_dir = tempfile.mkdtemp(prefix="ingest-podcast-")
    try:
        client = shlex.split(os.environ.get("EXTRACT_REMOTE_CMD", "extract-remote"))
        cmd = [*client, "--kind", "audio_extraction", "--out-dir", out_dir]
        for flag, val in (("--feed-url", feed_url), ("--episode-guid", args.episode_guid),
                          ("--episode-url", args.episode_url), ("--episode-title", args.episode_title),
                          ("--episode-published", args.episode_published),
                          ("--enclosure-url", args.enclosure_url)):
            if val:
                cmd += [flag, val]
        progress(f"resolving + transcribing podcast via {client[0]} …")
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else str(r.returncode)
            die(f"extract-remote failed: {tail}")
        results = sorted(Path(out_dir).glob("*/result.json"))
        if len(results) != 1:
            die(f"expected exactly one result.json under {out_dir}, found {len(results)}")
        result_path = _confine_result(out_dir, results[0])  # confined once, reused below
        doc = _load_json(str(result_path))
        if doc.get("kind") != "audio_extraction":
            die(f"unexpected extraction kind {doc.get('kind')!r} (expected audio_extraction)")
        meta = doc.get("meta") or {}

        # 2. validate — untrusted result.json: meta is a dict, segments are dicts
        #    with finite, ordered start/end; then the coverage gate.
        if not isinstance(meta, dict):
            die("result.json meta is not an object")
        segs = doc.get("segments") or []
        if not isinstance(segs, list) or not segs:
            die("podcast transcript has no segments")
        prev_end = None
        for s in segs:
            if not isinstance(s, dict):
                die("transcript has a non-object segment")
            txt = s.get("text")
            if txt is not None and not isinstance(txt, str):
                die("transcript segment text is not a string — refusing")
            st, en = s.get("start"), s.get("end")
            # exclude bool (a subclass of int) and non-finite (NaN/inf — NaN would
            # otherwise slip the coverage gate, since every comparison with NaN is False)
            if (isinstance(st, bool) or isinstance(en, bool)
                    or not isinstance(st, (int, float)) or not isinstance(en, (int, float))
                    or not math.isfinite(st) or not math.isfinite(en)):
                die("transcript has segments without finite numeric start/end — not timestamp-citable")
            if en < st:
                die(f"transcript has an inverted segment ({en} < {st}) — refusing")
            # globally ordered AND non-overlapping (overlap would over-count speech →
            # the coverage gate is safe-direction, but it's still malformed ASR output)
            if prev_end is not None and st < prev_end:
                die(f"transcript segments overlap or are out of order ({st} < prev end {prev_end}) — "
                    f"rendered timecodes would not be monotonic")
            prev_end = en
        speech = sum(max(0.0, s["end"] - s["start"]) for s in segs)
        chars = sum(len((s.get("text") or "")) for s in segs)
        _lang_raw = doc.get("language")  # untrusted: a non-str would be unhashable in the gate lookup
        lang = (_safe_str(_lang_raw) if isinstance(_lang_raw, str) else "") or "en"
        floor = _COVERAGE_MIN.get(lang, _COVERAGE_DEFAULT)
        cps = chars / speech if speech > 0 else 0.0
        if cps < floor and not args.force:
            die(f"coverage gate: {cps:.1f} chars/s of speech < {floor} for lang={lang} (pass --force)")

        # 3. identity (§8.1): reconcile guid AND feed_url hard, pick the basis
        svc_guid = meta.get("episode_guid")
        if args.episode_guid and svc_guid and args.episode_guid != svc_guid:
            die(f"episode_guid mismatch: requested {args.episode_guid} but the service resolved "
                f"{svc_guid} — refusing to cite a different episode than transcribed")
        svc_feed = meta.get("feed_url")
        if svc_feed and _canon_feed_url(svc_feed) != canon_feed:
            die(f"feed_url mismatch: requested {canon_feed} but the service resolved "
                f"{_canon_feed_url(svc_feed)} — refusing to key under a different feed")
        guid = _safe_str(svc_guid or args.episode_guid) or None
        enclosure = _safe_str(meta.get("enclosure_url") or args.enclosure_url) or None
        published = _safe_str(meta.get("published") or args.episode_published) or None
        # the identity-bearing episode title — SERVICE-resolved value wins (consistent
        # with guid/enclosure/published), reconciled hard against the requested one;
        # used BOTH in the feed_title_published key AND as the sidecar's top-level
        # title, so the resolver (which reads top-level `title`) re-derives the key.
        svc_title = _safe_str(meta.get("episode_title"))
        req_title = _safe_str(args.episode_title)
        if req_title and svc_title and req_title != svc_title:
            die(f"episode_title mismatch: requested {req_title!r} but the service resolved "
                f"{svc_title!r} — refusing to key under a different title")
        episode_title = svc_title or req_title or None
        if guid:
            basis, key = "feed_guid", (canon_feed, guid)
        elif enclosure:
            basis, key = "feed_enclosure", (canon_feed, enclosure)
        elif episode_title and published:
            basis, key = "feed_title_published", (canon_feed, episode_title, published)
        else:
            die("could not establish a podcast identity (no guid / enclosure / title+published)")
        progress(f"{len(segs)} segments, {speech:.0f}s speech, {cps:.1f} chars/s "
                 f"(lang={lang}); identity_basis={basis}")

        # feed_title_published is title-keyed → the sidecar title MUST equal the key
        # title (don't let a --title override split the identity); other bases are free.
        if basis == "feed_title_published":
            title = episode_title
        else:
            title = _safe_str(args.title) or episode_title or guid or canon_feed
        canonical_url = _safe_str(args.episode_url) or enclosure or canon_feed

        # 4. dedup AFTER resolution — primary basis + a CROSS-BASIS collision guard.
        #    The resolver doesn't reconcile cross-basis (aliases reserved/§8.4), so if
        #    this episode already exists under a different basis (a basis upgrade), we
        #    DIE rather than silently mint a duplicate (no-silent-drift).
        #    BEST-EFFORT: we can only probe a weaker basis when the service returns its
        #    field(s); per the service contract it returns enclosure_url + episode_title
        #    + published for every episode, so the probe is complete in practice. If a
        #    future service omits them, a cross-basis dup could slip — covered when the
        #    aliases/reconciliation flow lands (§8.4).
        applicable = [(basis, key)]
        for b, k in (("feed_guid", (canon_feed, guid) if guid else None),
                     ("feed_enclosure", (canon_feed, enclosure) if enclosure else None),
                     ("feed_title_published",
                      (canon_feed, episode_title, published) if (episode_title and published) else None)):
            if k is not None and (b, k) not in applicable:
                applicable.append((b, k))
        primary_head = None
        for b, k in applicable:
            h = _resolve_head_or_die((b, k))
            if not h:
                continue
            if b == basis:
                primary_head = h
            else:
                die(f"episode already ingested under identity_basis={b} as "
                    f"{h[1].get('source_id')}, but the service now resolves basis={basis}. "
                    f"Reconcile manually (remove the old source to re-ingest) — automatic "
                    f"basis-upgrade reconciliation is not yet implemented.")
        if primary_head:
            if not args.retranscribe:
                return _podcast_reuse(primary_head, origin_ref, basis, args.limit)
            # --retranscribe: supersede this head with the freshly-ASR'd source (mint below).
            supersedes_id = primary_head[1].get("source_id")
            if not supersedes_id:
                die("existing podcast head lacks a source_id — cannot supersede on --retranscribe")
        elif args.retranscribe:
            die("--retranscribe passed but no prior committed source exists for this episode — "
                "nothing to supersede; drop --retranscribe to ingest fresh.")

        # 5. render + commit (result.json IS the audit artifact: it carries segments + meta)
        source_id = new_ulid()
        ident_repr = guid or enclosure or f"{episode_title}|{published}"
        slug = f"{today()}-podcast-" + hashlib.sha1(
            f"{canon_feed}|{ident_repr}".encode()).hexdigest()[:11]
        if supersedes_id:
            slug = f"{slug}-r{source_id[-6:].lower()}"  # disambiguate from the superseded predecessor
        added = iso_now()
        stage = tempfile.mkdtemp(prefix="ingest-podcast-stage-")
        try:
            md_path = os.path.join(stage, f"{slug}.transcript.md")
            json_path = os.path.join(stage, f"{slug}.transcript.json")
            Path(md_path).write_text(render_markdown(doc, title, canonical_url), encoding="utf-8")
            shutil.copyfile(str(result_path), json_path)  # the envelope = the audit/render source
            text_file = tempfile.mkstemp(prefix="ingest-podcast-")[1]
            Path(text_file).write_text(render_prompt_copy(doc, args.limit), encoding="utf-8")
            sha = sha256_of(md_path)
            json_sha = sha256_of(json_path)
            dest = f"sources/{slug}.transcript.md"
            sidecar = f"{dest}.md"
            sidecar_text = _build_podcast_sidecar(
                source_id=source_id, sha=sha, added=added, origin_ref=origin_ref, title=title,
                canon_feed=canon_feed, guid=guid, enclosure=enclosure, published=published,
                basis=basis, resolution_source=meta.get("resolution_source"), speech=speech,
                lang=lang, meta=meta, json_sha=json_sha, supersedes=supersedes_id,
            )
            _stage_into_sources_podcast(slug, md_path, json_path, sidecar, sidecar_text,
                                        (basis, key), origin_ref)
        finally:
            shutil.rmtree(stage, ignore_errors=True)  # md/json moved out on success; cleans partials
        progress(f"new source_id={source_id}  {dest}"
                 + (f" (supersedes {supersedes_id})" if supersedes_id else ""))
        emit(
            SOURCE_ID=source_id, SHA256=sha, ADDED=added, ORIGIN_TYPE="audio",
            ORIGIN_REF=origin_ref, DEST=dest, DEST_BASENAME=f"{slug}.transcript.md",
            SIDECAR=sidecar, EXISTING_SIDECAR="", TEXT_FILE=text_file,
            AUDIT_JSON=f"sources/{slug}.transcript.json", SUPERSEDES=(supersedes_id or ""),
        )
        return 0
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)  # the extract-remote bundle dir


def _safe_job_ref(job_dir: Path, ref) -> Path:
    """Resolve an untrusted service-supplied `image_ref` against the extraction job dir,
    refusing an absolute path or any `..` escape. A malformed/compromised result.json
    must not make us read (and copy into sources/*.assets/) a file outside job_dir."""
    if not isinstance(ref, str) or not ref:
        die(f"image_ref is missing or not a string: {ref!r}")
    resolved = (job_dir / ref).resolve()
    try:
        resolved.relative_to(job_dir.resolve())
    except ValueError:
        die(f"image_ref escapes the job directory: {ref!r}")
    return resolved


def _confine_result(out_dir: str, result_path: Path) -> Path:
    """Confine the extractor's result.json under the out_dir WE created. `glob("*/result.json")`
    follows symlinked dirs, so a malicious/compromised extract-remote could drop
    `out_dir/link -> /outside` with `link/result.json` and pull the whole untrusted-input
    boundary (job_dir, image_ref resolution) outside out_dir. Require the resolved result
    to stay under out_dir; die on a symlink escape. Returns the resolved path."""
    res = result_path.resolve()
    try:
        res.relative_to(Path(out_dir).resolve())
    except ValueError:
        die(f"result.json escapes the extraction out-dir (symlink?): {result_path}")
    return res


_SAFE_BASENAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")


def _safe_image_basename(ref: str) -> str:
    """The committed asset basename derived from an untrusted image_ref. The basename is
    written verbatim into a YAML flow-map `path:` scalar AND used as a filename, so a `:`,
    `,`, `{`, `}`, newline, or leading dot/dash would corrupt the sidecar (unparsable or
    silently misparsed) or the staging dir. Reject anything outside a conservative set."""
    base = os.path.basename(ref)
    if not _SAFE_BASENAME_RX.match(base):
        die(f"image_ref basename is unsafe for a committed asset / YAML path: {base!r}")
    return base


def _evidence_path_ok(path) -> bool:
    """An evidence/audit path must be lexically under sources/ (no `..`) AND resolve —
    following symlinks — to a location still under sources/. The lexical check alone lets
    a committed symlink (`sources/link.jpg -> /outside`) make reuse fingerprint a file
    outside the vault, defeating no-silent-drift. (cwd-relative, like the rest of this
    module — ingest runs from the vault root.)"""
    if not path or ".." in str(path).split("/") or not str(path).startswith("sources/"):
        return False
    try:
        Path(path).resolve().relative_to(Path("sources").resolve())
    except (ValueError, OSError):
        return False
    return True


def _recompute_evidence_bundle(art: dict) -> str | None:
    """Reuse-time recompute of an `image_sha256_index_join` bundle from its committed
    `.cards.json`/`frames.json` (`from:`). Mirrors lint's _recompute_bundle (and the
    same sources/ + no-`..` + no-symlink-escape path constraint). None = unverifiable
    (caller dies)."""
    if art.get("bundle_recipe") != "image_sha256_index_join":
        return None
    src = art.get("from")
    if not _evidence_path_ok(src):
        return None
    if not Path(src).is_file():
        return None
    try:
        rows = json.loads(Path(src).read_text(encoding="utf-8"))  # NOT _load_json (it die()s on bad JSON)
    except (OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    try:
        return _image_bundle_sha256(rows)
    except (KeyError, TypeError):
        return None


def _verify_evidence_or_die(fm: dict, sidecar) -> None:
    """No-silent-drift guard for the reuse path: re-hash EVERY media.evidence_artifacts[]
    entry (file → sha256_of; image_sha256_index_join bundle → recompute from its source
    json) and die on any drift/missing/unsafe entry. The reuse contract must not be
    emitted over a tampered .cards.json/frames.json/image/bundle. Mirrors lint's
    _check_evidence_artifacts, but fails loud at ingest time."""
    arts = (fm.get("media") or {}).get("evidence_artifacts") or []
    if not isinstance(arts, list):
        die(f"media.evidence_artifacts is not a list in {sidecar}")
    if not arts:
        # image_note + frames reuse both REQUIRE evidence_artifacts[] — an absent/empty
        # block must not let reuse emit a clean contract over unverifiable evidence.
        die(f"media.evidence_artifacts[] is empty/absent — cannot verify no-drift on reuse: {sidecar}")
    for art in arts:
        if not isinstance(art, dict) or "sha256" not in art:
            die(f"malformed evidence_artifacts entry in {sidecar}: {art!r}")
        want = art["sha256"]
        role = art.get("role", "?")
        if "bundle_recipe" in art:
            got = _recompute_evidence_bundle(art)
            if got is None:
                die(f"evidence bundle '{role}' is unverifiable for reuse "
                    f"(recipe={art.get('bundle_recipe')!r}): {sidecar}")
        else:
            path = art.get("path")
            if not _evidence_path_ok(path):
                die(f"evidence artifact '{role}' has an unsafe path {path!r} in {sidecar}")
            if not Path(path).is_file():
                die(f"evidence artifact '{role}' missing for reuse: {path}")
            got = sha256_of(path)
        if got != want:
            die(f"evidence artifact '{role}' drifted from sidecar for reuse: {sidecar}")
    # Re-hashing the LISTED entries isn't enough — also run the SHARED completeness check
    # (the same one lint runs) so a removed image entry, a tampered dedup scalar, a
    # mis-anchored audit row, or a .cards.md/.cards.json disagreement can't reuse cleanly.
    for note in media_resolver.evidence_completeness_notes(Path(sidecar), fm, Path(".")):
        die(f"reuse: {note}: {sidecar}")


def _image_bundle_sha256(cards: list[dict]) -> str:
    """schema §7.1: sha256 over the index-ordered per-image `image_sha256` values,
    `\\n`-joined (UTF-8, LF, no trailing newline). Mirrors lint's
    `image_sha256_index_join` recipe so the committed bundle re-verifies."""
    ordered = sorted(cards, key=lambda c: c["index"])
    return hashlib.sha256(
        "\n".join(str(c["image_sha256"]) for c in ordered).encode("utf-8")).hexdigest()


def _render_image_note_md(cards_sorted: list[dict]) -> str:
    """Canonical .cards.md, DETERMINISTICALLY from the audited cards (the same `ocr_text`
    that feeds .cards.json) — NOT the service's free-form `text`. Delegates to the ONE
    shared renderer in media_resolver so lint/reuse can re-derive and compare it (the
    order/heading/text binding) without a second copy drifting."""
    return media_resolver.render_image_note_md(cards_sorted)


def _build_image_note_sidecar(*, source_id, sha, added, origin_ref, title, platform,
                              post_id, basis, card_count, bundle_sha, meta, slug,
                              cards_json_sha, card_evidence, supersedes=None) -> str:
    """The §7.1/§7.2 image_note sidecar (platform: rednote|unknown) + OCR recipe +
    evidence_artifacts[] (the .cards.json, every committed card image, and the
    derived image_bundle)."""
    ocr_params = meta.get("ocr_params")
    if isinstance(ocr_params, (dict, list)):
        ocr_params = json.dumps(ocr_params, ensure_ascii=False, sort_keys=True)
    def qopt(value):
        return _yopt(value, quote_bools=True)
    media_lines = [
        f"  platform: {platform}",
        f"  post_id: {qopt(post_id)}",
        f"  identity_basis: {basis}",
        f"  card_count: {card_count}",
        f"  image_bundle_sha256: {bundle_sha}",
        f"  ocr_tool: {qopt(meta.get('ocr_engine'))}",
        f"  ocr_model: {qopt(meta.get('ocr_model'))}",
        f"  ocr_params: {qopt(ocr_params)}",
        f"  ocr_device: {qopt(meta.get('ocr_device'))}",
        f"  ocred: {added}",
        f"  ocr_job_id: {qopt(meta.get('job_id'))}",
        "  evidence_artifacts:",
        f"    - {{role: cards_json, path: sources/{slug}.cards.json, sha256: {cards_json_sha}}}",
    ]
    for path, isha in card_evidence:
        media_lines.append(f"    - {{role: card_image, path: {path}, sha256: {isha}}}")
    media_lines += [
        f"    - {{role: image_bundle, sha256: {bundle_sha}, "
        f"bundle_recipe: image_sha256_index_join, from: sources/{slug}.cards.json}}",
        f"  render_format_version: {RENDER_FORMAT_VERSION}",
    ]
    return _build_media_sidecar(
        source_id=source_id, sha=sha, added=added, origin_type="image_note",
        origin_ref=origin_ref, title=title, media_lines=media_lines,
        supersedes=supersedes,
    )


def _image_note_reuse(head, origin_ref, basis, limit) -> int:
    """Reuse a committed image_note head (re-hash the canonical .cards.md, regenerate
    the prompt copy from it, emit the reuse contract)."""
    return _reuse_committed_source(
        head, origin_ref=origin_ref, origin_type="image_note",
        progress_message=f"reusing existing source_id={{source_id}} ({basis} match)",
        text_tmp_prefix="ingest-imagenote-", canonical_label=".cards.md",
        needs_transcript_audit=False, audit_optional=True, verify_evidence=True,
        limit=limit, audit_path_fn=_cards_audit_path,
        prompt_text_fn=lambda dest, _audit, cap: Path(dest).read_text(encoding="utf-8")[:cap],
    )


def _image_note_main(args) -> int:
    """image_note (RedNote etc.) front door (§8.2). Invokes `extract-remote --kind
    image_note` (OCR + the card-image bundle), commits the candidate `.cards.md`
    (canonical), a `.cards.json` audit artifact, and the card images under
    `<slug>.cards.md.assets/` (which ingest.py git-adds). Manual-export first."""
    source = args.source
    if not source:
        die("image_note needs a source: a manual-export bundle (zip/tar) path or a post URL")

    out_dir = tempfile.mkdtemp(prefix="ingest-imagenote-")
    try:
        client = shlex.split(os.environ.get("EXTRACT_REMOTE_CMD", "extract-remote"))
        cmd = [*client, "--kind", "image_note", "--out-dir", out_dir, source]
        progress(f"OCR'ing image_note via {client[0]} …")
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else str(r.returncode)
            die(f"extract-remote failed: {tail}")
        results = sorted(Path(out_dir).glob("*/result.json"))
        if len(results) != 1:
            die(f"expected exactly one result.json under {out_dir}, found {len(results)}")
        result_path = _confine_result(out_dir, results[0])  # confined once, reused below
        job_dir = result_path.parent
        doc = _load_json(str(result_path))
        if doc.get("kind") != "image_note":
            die(f"unexpected extraction kind {doc.get('kind')!r} (expected image_note)")
        meta = doc.get("meta") or {}
        if not isinstance(meta, dict):
            die("result.json meta is not an object")

        # validate cards + the candidate text + each card's committed image asset
        cards = doc.get("cards")
        if not isinstance(cards, list) or not cards:
            die("image_note result has no cards")
        seen_idx, seen_base = set(), set()
        for c in cards:
            if not isinstance(c, dict):
                die("image_note has a non-object card")
            for f in ("index", "ocr_text", "image_ref", "image_sha256"):
                if f not in c:
                    die(f"image_note card missing required field {f!r}")
            if not isinstance(c["ocr_text"], str):
                die("image_note card ocr_text is not a string")
            if not isinstance(c["image_sha256"], str):
                die(f"image_note card image_sha256 is not a string: {type(c['image_sha256']).__name__}")
            if not isinstance(c["index"], int) or isinstance(c["index"], bool):
                die("image_note card index is not an int")
            if c["index"] in seen_idx:
                die(f"image_note has a duplicate card index {c['index']}")
            seen_idx.add(c["index"])
            img = _safe_job_ref(job_dir, c["image_ref"])  # reject abs/`..` escape
            if not img.is_file():
                die(f"card image_ref not in the bundle: {c['image_ref']}")
            base = _safe_image_basename(c["image_ref"])  # reject YAML/path-unsafe basenames
            if base == "cards.json":  # reserved: don't shadow the audit file's name in the asset tree
                die("card image basename collides with the reserved audit file cards.json")
            if base in seen_base:
                die(f"duplicate card image basename {base!r} (refs would collide in staging)")
            seen_base.add(base)
            if sha256_of(str(img)) != c["image_sha256"]:
                die(f"card image_sha256 mismatch for {c['image_ref']} (corrupt bundle)")
        # indices must be the 0-based contiguous sequence (schema §7.1): headings/anchors
        # render from index+1, so a gap like [10,11] would commit `## card 11` with
        # card_count=2 — citable anchors card-1/card-2 could never resolve.
        if sorted(seen_idx) != list(range(len(cards))):
            die(f"image_note card indices must be 0-based contiguous (0..{len(cards) - 1}); "
                f"got {sorted(seen_idx)}")
        card_count = meta.get("card_count")
        if card_count is None:
            card_count = len(cards)
        if isinstance(card_count, bool) or not isinstance(card_count, int):
            die(f"meta.card_count is not an int: {card_count!r}")  # reject bool (True==1)
        if card_count != len(cards):
            die(f"meta.card_count {card_count} != number of cards {len(cards)}")
        # canonical .cards.md is DERIVED from the audited cards (below), not doc["text"].
        progress(f"{len(cards)} cards OCR'd")

        # identity (§8.2): known post = (platform, post_id); manual export =
        # (platform, image_bundle_sha256). platform is rednote or recognized `unknown`.
        # RECONCILE the CLI selector against the (untrusted) service metadata — if BOTH are
        # given and they DIFFER, die loud (mirrors the podcast/video reconciliation) rather
        # than committing one post's OCR under another's identity. NFC-normalize first so
        # equivalent-but-differently-composed forms reconcile instead of falsely conflicting.
        svc_post = meta.get("post_id")
        if svc_post is not None and not isinstance(svc_post, str):
            die(f"service post_id must be a string token, got {type(svc_post).__name__}")
        cli_post = unicodedata.normalize("NFC", _safe_str(args.post_id)) if args.post_id else None
        svc_post = unicodedata.normalize("NFC", _safe_str(svc_post)) if svc_post else None
        cli_post = cli_post or None
        svc_post = svc_post or None
        if cli_post and svc_post and cli_post != svc_post:
            die(f"post_id mismatch: CLI --post-id={cli_post!r} but service resolved {svc_post!r}")
        post_id = cli_post or svc_post
        # platform: an explicit CLI --platform is a deliberate OVERRIDE of the service's
        # guess (e.g. a manual export the owner labels `unknown`), so CLI wins (no die).
        platform = args.platform if args.platform in ("rednote", "unknown") else None
        platform = platform or (_safe_str(meta.get("platform")) or "unknown")
        if platform not in ("rednote", "unknown"):
            platform = "unknown"
        bundle_sha = _image_bundle_sha256(cards)
        if post_id:
            basis, key = "image_post_id", (platform, post_id)
        else:
            basis, key = "image_bundle", (platform, bundle_sha)
        title = _safe_str(args.title) or post_id or f"{platform} image note"
        origin_ref = source

        # dedup
        head = _resolve_head_or_die((basis, key))
        # Cross-basis dedup (BOTH directions): the resolver keys on ONE basis, so a bundle
        # ingest can't see a prior post_id head and a post_id ingest can't see a prior
        # bundle head. Scan committed image_note sidecars by the deterministic bundle sha
        # to catch the same images under EITHER identity_basis, and die loud rather than
        # mint a silent duplicate (re-OCR supersede is deferred, so no same-bundle chains
        # exist yet — any other-id match is a true cross-basis duplicate).
        head_id = head[1].get("source_id") if head else None
        for _sc, _fm in _image_notes_with_bundle(platform, bundle_sha):
            if _fm.get("source_id") != head_id:
                die(f"these images are already committed as {_fm.get('source_id')} "
                    f"(identity_basis={(_fm.get('media') or {}).get('identity_basis')}) — "
                    f"reconcile by hand (remove one) before re-ingesting.")
        if head:
            committed_bundle = (head[1].get("media") or {}).get("image_bundle_sha256")
            if basis == "image_post_id" and not args.reocr:
                # the scalar is part of the sidecar contract + drives this guard — a
                # post_id head missing it can't prove the bundle is unchanged → die.
                if not committed_bundle:
                    die(f"committed post {post_id} sidecar is missing media.image_bundle_sha256 "
                        f"— cannot verify the bundle is unchanged; remove it to re-ingest.")
                if committed_bundle != bundle_sha:
                    die(f"image bundle changed for post {post_id} (committed "
                        f"{committed_bundle[:12]}… vs {bundle_sha[:12]}…) — re-run with --reocr to supersede")
            if not args.reocr:
                return _image_note_reuse(head, origin_ref, basis, args.limit)
            # --reocr: re-OCR → mint a NEW source that SUPERSEDES the head. The wiki then
            # re-synthesizes from the fresh OCR via the normal LLM pipeline, and ingest.py
            # migrates the old source's live citations (rewrite-citations old→new) after the
            # commit. The old source stays in sources/ (immutable); the resolver picks the
            # new head; superseded_ids() drops the old from future dedup scans.
            supersedes_id = head[1].get("source_id")
            if not supersedes_id:
                die("existing image_note head lacks a source_id — cannot supersede on --reocr")
        else:
            if args.reocr:
                die(f"--reocr passed but no prior committed source exists for "
                    f"({'post_id=' + post_id if post_id else 'this image bundle'}, "
                    f"platform={platform}) — nothing to supersede; drop --reocr to ingest fresh.")
            supersedes_id = None

        # new source (fresh, or — on --reocr — superseding `supersedes_id`)
        source_id = new_ulid()
        # a post_id that is all-non-alphanumeric strips to "" → fall back to the bundle sha
        # (mirrors the no-post_id branch) so the slug never ends in a dangling dash.
        ident = (re.sub(r"[^A-Za-z0-9]+", "-", post_id).strip("-")[:40] if post_id else "") or bundle_sha[:11]
        slug = f"{today()}-{platform}-{ident}"
        if supersedes_id:
            # disambiguate from the (possibly same-day) predecessor's slug; the fresh ULID
            # tail is unique so the new artifacts never collide with the superseded ones.
            slug = f"{slug}-r{source_id[-6:].lower()}"
        added = iso_now()
        dest = f"sources/{slug}.cards.md"
        sidecar = f"{dest}.md"
        assets_rel = f"sources/{slug}.cards.md.assets"

        stage = tempfile.mkdtemp(prefix="ingest-imagenote-stage-")
        try:
            md_tmp = os.path.join(stage, f"{slug}.cards.md")
            json_tmp = os.path.join(stage, f"{slug}.cards.json")
            assets_tmp = os.path.join(stage, f"{slug}.cards.md.assets")
            os.makedirs(assets_tmp, exist_ok=True)
            cards_sorted = sorted(cards, key=lambda c: c["index"])
            canonical = _render_image_note_md(cards_sorted)  # derived from audited cards
            Path(md_tmp).write_text(canonical, encoding="utf-8")
            # commit the card images + build the ordered .cards.json audit artifact
            cards_json, card_evidence = [], []
            for c in cards_sorted:
                base = _safe_image_basename(c["image_ref"])
                dst = os.path.join(assets_tmp, base)
                shutil.copyfile(str(_safe_job_ref(job_dir, c["image_ref"])), dst)
                isha = sha256_of(dst)  # re-hash the COPY, not the service-claimed value
                if isha != c["image_sha256"]:
                    die(f"card image hash changed during staging (copy corruption?): {base!r}")
                committed = f"{assets_rel}/{base}"
                cards_json.append({
                    "index": c["index"], "heading_anchor": f"card-{c['index'] + 1}",
                    "text": c["ocr_text"], "image_sha256": isha,
                    "image_path": committed,
                    **({"confidence": c["confidence"]} if c.get("confidence") is not None else {}),
                })
                card_evidence.append((committed, isha))
            Path(json_tmp).write_text(json.dumps(cards_json, ensure_ascii=False, indent=2), encoding="utf-8")
            sha = sha256_of(md_tmp)
            cards_json_sha = sha256_of(json_tmp)
            text_file = tempfile.mkstemp(prefix="ingest-imagenote-")[1]
            Path(text_file).write_text(canonical[: args.limit], encoding="utf-8")
            sidecar_text = _build_image_note_sidecar(
                source_id=source_id, sha=sha, added=added, origin_ref=origin_ref, title=title,
                platform=platform, post_id=post_id, basis=basis, card_count=card_count,
                bundle_sha=bundle_sha, meta=meta, slug=slug, cards_json_sha=cards_json_sha,
                card_evidence=card_evidence, supersedes=supersedes_id)
            _stage_into_sources_image_note(slug, md_tmp, json_tmp, assets_tmp, sidecar,
                                           sidecar_text, (basis, key), origin_ref)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        progress(f"new source_id={source_id}  {dest}"
                 + (f" (supersedes {supersedes_id})" if supersedes_id else ""))
        emit(
            SOURCE_ID=source_id, SHA256=sha, ADDED=added, ORIGIN_TYPE="image_note",
            ORIGIN_REF=origin_ref, DEST=dest, DEST_BASENAME=f"{slug}.cards.md",
            SIDECAR=sidecar, EXISTING_SIDECAR="", TEXT_FILE=text_file,
            AUDIT_JSON=f"sources/{slug}.cards.json", SUPERSEDES=(supersedes_id or ""),
        )
        return 0
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _stage_into_sources_image_note(slug, md_tmp, json_tmp, assets_tmp, sidecar,
                                   sidecar_text, identity, origin_ref) -> None:
    """Atomic move into sources/ for image_note: .cards.md → .cards.json → assets/ →
    sidecar(LAST). Refuses to clobber a tracked file; an untracked sidecar must be a
    recognizable own-orphan (same origin_ref + same generalized identity)."""
    _stage_media_artifacts(
        slug=slug, md_path=md_tmp, json_path=json_tmp, assets_path=assets_tmp,
        sidecar=sidecar, sidecar_text=sidecar_text, md_suffix=".cards.md",
        json_suffix=".cards.json", assets_suffix=".cards.md.assets",
        identity=identity, origin_ref=origin_ref,
    )


def _head_has_frames(sidecar) -> bool:
    """True if a committed head is a frames source. Decided by sidecar SEMANTICS
    (origin_type: video + media.frame_count) AND the physical frames.json — consistent
    with lint's _frame_ordinals. Without the semantic guard, a stray frames.json left by
    an aborted run beside a transcript-only head would route to the reuse path and die
    with a confusing 'cannot verify no-drift' instead of the correct transcript-only
    'adding frames not yet implemented' message."""
    fm = parse_frontmatter(Path(sidecar))
    if fm.get("origin_type") != "video" or (fm.get("media") or {}).get("frame_count") is None:
        return False
    md = str(sidecar)[:-3]  # <slug>.transcript.md.md → <slug>.transcript.md
    return Path(f"{md}.assets", "frames.json").is_file()


def _build_video_frames_sidecar(*, source_id, sha, added, origin_ref, title, video_id,
                                canonical_url, speech, lang, meta, json_sha, slug,
                                frames_json_sha, frame_evidence, bundle_sha, frame_count,
                                supersedes=None, transcript_tool="extract-remote") -> str:
    """A frames source sidecar (§8.3): the transcript recipe (like the YouTube path)
    PLUS the frame policy + dual evidence (transcript_json + frames_json + per-frame
    images + the derived frame_bundle)."""
    fp = meta.get("frame_policy")
    if isinstance(fp, (dict, list)):
        fp = json.dumps(fp, ensure_ascii=False, sort_keys=True)
    assets_rel = f"sources/{slug}.transcript.md.assets"
    media_lines = [
        "  platform: youtube",
        f"  video_id: {video_id}",
        f"  canonical_url: {y(canonical_url)}",
        f"  service_source: {y(meta.get('source', canonical_url))}",
        *_storage_guard_lines(),
        *_asr_recipe_lines(speech=speech, lang=lang, meta=meta, transcript_tool=transcript_tool),
        f"  selected_audio_format: {yopt(meta.get('selected_audio_format'))}",
        f"  selected_video_format: {yopt(meta.get('selected_video_format'))}",
        f"  ffmpeg_version: {yopt(meta.get('ffmpeg_version'))}",
        f"  frame_policy: {yopt(fp)}",
        f"  frame_count: {frame_count}",
        f"  transcript_job_id: {yopt(meta.get('job_id'))}",
        f"  transcript_json_sha256: {json_sha}",
        "  evidence_artifacts:",
        f"    - {{role: transcript_json, path: sources/{slug}.transcript.json, sha256: {json_sha}}}",
        f"    - {{role: frames_json, path: {assets_rel}/frames.json, sha256: {frames_json_sha}}}",
    ]
    for path, isha in frame_evidence:
        media_lines.append(f"    - {{role: frame_image, path: {path}, sha256: {isha}}}")
    media_lines += [
        f"    - {{role: frame_bundle, sha256: {bundle_sha}, "
        f"bundle_recipe: image_sha256_index_join, from: {assets_rel}/frames.json}}",
        f"  render_format_version: {RENDER_FORMAT_VERSION}",
        f"  transcribed: {added}",
    ]
    return _build_media_sidecar(
        source_id=source_id, sha=sha, added=added, origin_type="video",
        origin_ref=origin_ref, title=title, media_lines=media_lines,
        supersedes=supersedes,
    )


def _stage_into_sources_frames(slug, md_tmp, json_tmp, assets_tmp, sidecar,
                               sidecar_text, identity, origin_ref) -> None:
    """Atomic move into sources/ for a frames source: .transcript.md → .transcript.json
    → assets/ (frames.json + frame images) → sidecar(LAST). Same guards as the other
    media stagers."""
    _stage_media_artifacts(
        slug=slug, md_path=md_tmp, json_path=json_tmp, assets_path=assets_tmp,
        sidecar=sidecar, sidecar_text=sidecar_text, md_suffix=".transcript.md",
        json_suffix=".transcript.json", assets_suffix=".transcript.md.assets",
        identity=identity, origin_ref=origin_ref,
    )


def _video_frames_reuse(head, origin_ref, limit) -> int:
    """Idempotent reuse of an existing frames source (re-hash the canonical
    .transcript.md, emit the reuse contract)."""
    return _reuse_committed_source(
        head, origin_ref=origin_ref, origin_type="video",
        progress_message="reusing existing frames source_id={source_id} (video_id match)",
        text_tmp_prefix="ingest-frames-", canonical_label=".transcript.md",
        audit_optional=True, verify_evidence=True, limit=limit,
        audit_path_fn=_transcript_audit_path,
        prompt_text_fn=lambda _dest, audit, cap: render_prompt_copy(_load_json(audit), cap),
    )


def _carry_forward_transcript(head):
    """Add-frames-to-an-existing-transcript (§8.3 supersede): verify head A's committed
    transcript artifacts against its sidecar (no-drift) and return the VERBATIM bytes +
    parsed doc. The new frames source B carries A's `.transcript.md`/`.transcript.json`
    BYTE-FOR-BYTE so every existing transcript timecode citation stays valid after the
    A→B migration — only the frame bundle is new. Dies loud if A drifted or is unfit."""
    sidecar, fm = head
    md = str(sidecar)[:-3]                                  # <slug>.transcript.md
    audit = md[: -len(".transcript.md")] + ".transcript.json"
    # Physical-asset net FIRST (independent of the YAML signals _head_has_frames trusts): a
    # genuine transcript-only head has NO committed frame bundle. If a frames source had its
    # frame_count/evidence_artifacts hand-edited away but left the tracked `.assets/` behind,
    # _head_has_frames() mis-routes it here and we'd supersede without ever re-hashing those
    # committed assets. Refuse loud — deleting frontmatter alone must not disable the guard.
    md_assets_rel = f"{md}.assets"
    if media_resolver.assets_dir_tracked_files(Path("."), md_assets_rel):
        die(f"head {fm.get('source_id')} presents as transcript-only but has committed frame "
            f"assets under {md_assets_rel} (hand-edited frames source?); refusing to carry it "
            f"forward — restore its frame_count/evidence_artifacts, or remove the source.")
    md_sha = fm.get("sha256")
    json_sha = (fm.get("media") or {}).get("transcript_json_sha256")
    for path, expected, label in (
        (md, md_sha, "transcript.md"),
        (audit, json_sha, "transcript.json"),
    ):
        if not Path(path).is_file():
            die(f"committed {label} missing for frame carry-forward: {path}")
        if not expected:
            die(f"sidecar missing the {label} hash — cannot verify no-drift: {path}")
        if sha256_of(path) != expected:
            die(f"{label} drifted from sidecar — refusing to carry it forward: {path}")
    # A transcript-only head carries no frame/audio bundle; if it somehow lists
    # evidence_artifacts, re-hash them loud before trusting any carry-forward.
    if (fm.get("media") or {}).get("evidence_artifacts") is not None:
        _verify_evidence_or_die(fm, sidecar)
    # Byte-identity proves the bytes are A's, NOT that they are well-formed. A
    # hand-repaired source whose sidecar hash was updated to match would otherwise let a
    # malformed transcript through to render_prompt_copy / speech-sum. Re-validate the
    # segment shape (finite, ordered) so an unfit A dies loud here, as the docstring says.
    doc = _load_json(audit)
    if not isinstance(doc.get("meta"), (dict, type(None))):
        # parity with the fresh path's meta guard — a non-dict meta would otherwise blow up
        # in dict(a_doc["meta"]) downstream with a raw TypeError instead of a clean die.
        die(f"carry-forward transcript.json meta is not an object: {audit}")
    segs = doc.get("segments")
    if not isinstance(segs, list) or not segs:
        die(f"carry-forward transcript has no segments — unfit to add frames to: {audit}")
    prev_end = None
    for s in segs:
        if not isinstance(s, dict):
            die(f"carry-forward transcript has a non-object segment: {audit}")
        st, en = s.get("start"), s.get("end")
        if (isinstance(st, bool) or isinstance(en, bool)
                or not isinstance(st, (int, float)) or not isinstance(en, (int, float))
                or not math.isfinite(st) or not math.isfinite(en)):
            die(f"carry-forward transcript has non-finite segment start/end: {audit}")
        if en < st or (prev_end is not None and st < prev_end):
            die(f"carry-forward transcript segments are inverted/out of order: {audit}")
        prev_end = en
    # A's actual transcript tool is authoritative for B (B's transcript IS A's); default to
    # transcript-remote only if A predates the field. It is emitted RAW into B's sidecar, so a
    # hand-edited non-token value (e.g. one with a `:`) must NOT be silently sanitized into a
    # different string — that would be a quiet provenance mutation. Require it already be a
    # safe token; die loud otherwise (no inject, no silent rewrite).
    transcript_tool = (fm.get("media") or {}).get("transcript_tool") or "transcript-remote"
    if not isinstance(transcript_tool, str) or _safe_scalar_token(transcript_tool) != transcript_tool:
        die(f"predecessor transcript_tool {transcript_tool!r} is not a safe scalar token — "
            f"refusing to carry it forward (would rewrite provenance): {sidecar}")
    return {"md_bytes": Path(md).read_bytes(), "json_bytes": Path(audit).read_bytes(),
            "doc": doc, "md_sha": md_sha, "json_sha": json_sha,
            "title": fm.get("title"), "transcript_tool": transcript_tool}


def _video_frames_main(args) -> int:
    """Video + frames front door (§8.3). FRESH video (no existing head): one new source
    carrying the transcript + the frame bundle. An existing transcript-only head +
    --frames: ADD FRAMES via supersede — mint a new source that carries A's transcript
    forward BYTE-EXACT and grafts on the freshly-extracted frame bundle (ingest.py then
    migrates A's live citations A→B). An existing frames head reuses (idempotent)."""
    video_id = extract_video_id(args.source)
    if not video_id:
        die(f"no 11-char YouTube video_id in {args.source!r} (pass an explicit watch URL)")
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    head = _resolve_head_or_die(("youtube_video_id", (video_id,)))
    supersedes_id = None
    carry = None  # set when adding frames to an existing transcript-only head (byte-exact)
    if head:
        if args.retranscribe:
            # --retranscribe means re-ASR (new transcript). Combined with --frames that's a
            # re-transcribe-AND-reframe supersede whose fresh transcript could invalidate the
            # old source's timecode anchors — a distinct, riskier operation. Still deferred;
            # die loud rather than mint a duplicate head.
            die(f"video frames --retranscribe (re-transcribe + reframe supersede) is not yet "
                f"implemented; the existing source is {head[1].get('source_id')} — remove "
                f"it to re-ingest with --frames, or drop --retranscribe to add frames to the "
                f"existing transcript.")
        if _head_has_frames(head[0]):
            return _video_frames_reuse(head, args.source, args.limit)
        # transcript-only head + --frames → ADD FRAMES via supersede. Carry A's transcript
        # forward byte-exact (every existing transcript citation stays valid); graft on the
        # freshly-extracted frames. A stays immutable; ingest.py migrates A's citations A→B.
        supersedes_id = head[1].get("source_id")
        if not supersedes_id:
            die("existing transcript head lacks a source_id — cannot supersede to add frames")
        carry = _carry_forward_transcript(head)

    out_dir = tempfile.mkdtemp(prefix="ingest-frames-")
    try:
        client = shlex.split(os.environ.get("EXTRACT_REMOTE_CMD", "extract-remote"))
        cmd = [*client, "--kind", "video", "--frames", "--out-dir", out_dir]
        if args.cadence:
            cmd += ["--cadence", args.cadence]
        cmd.append(canonical_url)
        progress(f"transcribing + extracting frames via {client[0]} …")
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else str(r.returncode)
            die(f"extract-remote failed: {tail}")
        results = sorted(Path(out_dir).glob("*/result.json"))
        if len(results) != 1:
            die(f"expected exactly one result.json under {out_dir}, found {len(results)}")
        result_path = _confine_result(out_dir, results[0])  # confined once, reused below
        job_dir = result_path.parent
        doc = _load_json(str(result_path))
        if doc.get("kind") != "video":
            die(f"unexpected extraction kind {doc.get('kind')!r} (expected video)")
        meta = doc.get("meta") or {}
        if not isinstance(meta, dict):
            die("result.json meta is not an object")
        svc_vid = meta.get("video_id")
        if svc_vid and svc_vid != video_id:
            die(f"video_id mismatch: local={video_id} but service resolved {svc_vid}")

        if carry is None:
            # FRESH path: validate the run's transcript (finite, ordered) + coverage and
            # let it drive the sidecar's transcript provenance.
            segs = doc.get("segments") or []
            if not isinstance(segs, list) or not segs:
                die("video transcript has no segments")
            prev_end = None
            for s in segs:
                if not isinstance(s, dict):
                    die("transcript has a non-object segment")
                st, en = s.get("start"), s.get("end")
                if (isinstance(st, bool) or isinstance(en, bool)
                        or not isinstance(st, (int, float)) or not isinstance(en, (int, float))
                        or not math.isfinite(st) or not math.isfinite(en)):
                    die("transcript has segments without finite numeric start/end")
                if en < st:
                    die(f"transcript has an inverted segment ({en} < {st})")
                if prev_end is not None and st < prev_end:
                    die(f"transcript segments overlap or are out of order ({st} < {prev_end})")
                prev_end = en
            speech = sum(max(0.0, s["end"] - s["start"]) for s in segs)
            chars = sum(len((s.get("text") or "")) for s in segs)
            _lang_raw = doc.get("language")  # untrusted: a non-str would be unhashable in the gate lookup
            lang = (_safe_str(_lang_raw) if isinstance(_lang_raw, str) else "") or "en"
            floor = _COVERAGE_MIN.get(lang, _COVERAGE_DEFAULT)
            cps = chars / speech if speech > 0 else 0.0
            if cps < floor and not args.force:
                die(f"coverage gate: {cps:.1f} chars/s < {floor} for lang={lang} (pass --force)")
            src_meta = meta  # frame-run meta drives every sidecar field
        else:
            # CARRY-FORWARD: the transcript IS A's (validated + coverage-gated at A's ingest,
            # re-hashed byte-exact and re-shape-checked in _carry_forward_transcript). Derive
            # transcript provenance from A's transcript.json meta — the SAME meta A's sidecar
            # was built from, so every transcript field reproduces A's recorded value. The
            # frame run is discarded except for the frame bundle; speech/lang reflect A.
            a_doc = carry["doc"]
            speech = sum(max(0.0, s["end"] - s["start"]) for s in a_doc["segments"])
            _alang = a_doc.get("language")
            lang = (_safe_str(_alang) if isinstance(_alang, str) else "") or "en"
            src_meta = dict(a_doc.get("meta") or {})
            # The frame/video-extraction fields describe B's NEW frame bundle, never A's
            # transcript run — set them UNCONDITIONALLY from the frame run (None if absent)
            # so A's audio-side ffmpeg_version can never leak into a field about the frames.
            for k in ("frame_policy", "selected_video_format", "ffmpeg_version"):
                src_meta[k] = meta.get(k)

        # validate frames
        frames = doc.get("frames")
        if not isinstance(frames, list) or not frames:
            die("video --frames returned no frames")
        seen_fbase, seen_fid = set(), set()
        for f in frames:
            if not isinstance(f, dict):
                die("a frame is not an object")
            for k in ("frame_id", "timecode", "image_ref"):
                if k not in f:
                    die(f"frame missing required field {k!r}")
            tc = f["timecode"]
            if isinstance(tc, bool) or not isinstance(tc, (int, float)) or not math.isfinite(tc):
                die("frame timecode is not a finite number")
            fid = f["frame_id"]
            if isinstance(fid, bool) or not isinstance(fid, int):
                die(f"frame_id must be an int ordinal (frames are sorted by it): {type(fid).__name__}")
            if fid in seen_fid:  # frame_id is the audit/sort key — duplicates would cite the same frame twice
                die(f"duplicate frame_id {fid}")
            seen_fid.add(fid)
            if "ocr_text" in f and not isinstance(f["ocr_text"], str):  # untrusted → committed verbatim
                die(f"frame ocr_text is not a string: {type(f['ocr_text']).__name__}")
            img = _safe_job_ref(job_dir, f["image_ref"])  # reject abs/`..` escape
            if not img.is_file():
                die(f"frame image_ref not in the bundle: {f['image_ref']}")
            fbase = _safe_image_basename(f["image_ref"])  # reject YAML/path-unsafe basenames
            if fbase == "frames.json":  # reserved: the audit file shares this assets dir
                die("frame image basename collides with the reserved audit file frames.json")
            if fbase in seen_fbase:
                die(f"duplicate frame image basename {fbase!r} (refs would collide in staging)")
            seen_fbase.add(fbase)
        progress(f"{speech:.0f}s speech; {len(frames)} frames"
                 + (f" (adding to transcript {supersedes_id})" if carry else ""))

        source_id = new_ulid()
        slug = f"{today()}-youtube-{video_id}"
        if supersedes_id:
            # disambiguate from the (possibly same-day) predecessor's slug; the fresh ULID
            # tail is unique so the new artifacts never collide with the superseded ones.
            slug = f"{slug}-r{source_id[-6:].lower()}"
        added = iso_now()
        title = (_safe_str(args.title)
                 or (_safe_str(carry["title"]) if carry else "")
                 or _safe_str(meta.get("title")) or canonical_url)
        origin_ref = args.source or canonical_url
        dest = f"sources/{slug}.transcript.md"
        sidecar = f"{dest}.md"

        stage = tempfile.mkdtemp(prefix="ingest-frames-stage-")
        try:
            md_tmp = os.path.join(stage, f"{slug}.transcript.md")
            json_tmp = os.path.join(stage, f"{slug}.transcript.json")
            assets_tmp = os.path.join(stage, f"{slug}.transcript.md.assets")
            os.makedirs(assets_tmp, exist_ok=True)
            if carry is None:
                Path(md_tmp).write_text(render_markdown(doc, title, canonical_url), encoding="utf-8")
                shutil.copyfile(str(result_path), json_tmp)  # envelope = transcript audit (segments+meta)
            else:
                # carry A's transcript artifacts forward BYTE-FOR-BYTE (re-hashed below)
                Path(md_tmp).write_bytes(carry["md_bytes"])
                Path(json_tmp).write_bytes(carry["json_bytes"])
            # frames.json (1-based ordinal index) + committed frame images + per-frame evidence
            fjson, frame_evidence = [], []
            for i, f in enumerate(sorted(frames, key=lambda f: f["frame_id"]), start=1):
                base = _safe_image_basename(f["image_ref"])
                shutil.copyfile(str(_safe_job_ref(job_dir, f["image_ref"])), os.path.join(assets_tmp, base))
                # DELIBERATELY ignore any service-claimed frame hash: the committed hash is
                # computed from the actual copied bytes, so frames.json/evidence always reflect
                # what was committed (no untrusted-claim trust; provenance binds to bytes).
                isha = sha256_of(os.path.join(assets_tmp, base))
                committed = f"sources/{slug}.transcript.md.assets/{base}"
                fjson.append({"index": i, "frame_id": f["frame_id"], "timecode": f["timecode"],
                              "filename": base, "ocr_text": f.get("ocr_text", ""),
                              "image_sha256": isha, "image_path": committed})
                frame_evidence.append((committed, isha))
            Path(assets_tmp, "frames.json").write_text(
                json.dumps(fjson, ensure_ascii=False, indent=2), encoding="utf-8")
            bundle_sha = _image_bundle_sha256(fjson)
            frames_json_sha = sha256_of(os.path.join(assets_tmp, "frames.json"))
            text_file = tempfile.mkstemp(prefix="ingest-frames-")[1]
            Path(text_file).write_text(
                render_prompt_copy(carry["doc"] if carry else doc, args.limit), encoding="utf-8")
            sha = sha256_of(md_tmp)
            json_sha = sha256_of(json_tmp)
            if carry is not None and (sha != carry["md_sha"] or json_sha != carry["json_sha"]):
                # belt-and-suspenders: the authoritative no-drift gate already ran in
                # _carry_forward_transcript (it re-hashed A's committed files vs the sidecar).
                # This re-checks that the verbatim copy reproduced those bytes — it can only
                # fire on an OS-level write fault or a future refactor that mutates the bytes.
                die("carry-forward transcript did not reproduce the predecessor's hashes — "
                    "refusing to supersede with a drifted transcript")
            sidecar_text = _build_video_frames_sidecar(
                source_id=source_id, sha=sha, added=added, origin_ref=origin_ref, title=title,
                video_id=video_id, canonical_url=canonical_url, speech=speech, lang=lang,
                meta=src_meta, json_sha=json_sha, slug=slug, frames_json_sha=frames_json_sha,
                frame_evidence=frame_evidence, bundle_sha=bundle_sha, frame_count=len(fjson),
                supersedes=supersedes_id,
                transcript_tool=(carry["transcript_tool"] if carry else "extract-remote"))
            _stage_into_sources_frames(slug, md_tmp, json_tmp, assets_tmp, sidecar,
                                       sidecar_text, ("youtube_video_id", (video_id,)), origin_ref)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        progress(f"new frames source_id={source_id}  {dest}"
                 + (f" (supersedes {supersedes_id})" if supersedes_id else ""))
        emit(
            SOURCE_ID=source_id, SHA256=sha, ADDED=added, ORIGIN_TYPE="video",
            ORIGIN_REF=origin_ref, DEST=dest, DEST_BASENAME=f"{slug}.transcript.md",
            SIDECAR=sidecar, EXISTING_SIDECAR="", TEXT_FILE=text_file,
            AUDIT_JSON=f"sources/{slug}.transcript.json", SUPERSEDES=(supersedes_id or ""),
        )
        return 0
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(prog="media-identity.py")
    ap.add_argument("source", nargs="?", default="",
                    help="media URL (YouTube); for --platform podcast a feed/episode URL or empty")
    ap.add_argument("--kind", choices=["video", "audio", "image_note"], default="video")
    ap.add_argument("--platform", choices=["youtube", "podcast", "rednote", "unknown"],
                    default="youtube",
                    help="explicit platform discriminator (§8.1) — never URL-sniffed")
    ap.add_argument("--title", default="")
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--retranscribe", action="store_true")
    ap.add_argument("--force", action="store_true")
    # podcast (audio_extraction) selectors — forwarded to extract-remote
    ap.add_argument("--feed-url", default="")
    ap.add_argument("--episode-guid", default="")
    ap.add_argument("--episode-url", default="")
    ap.add_argument("--episode-title", default="")
    ap.add_argument("--episode-published", default="")
    ap.add_argument("--enclosure-url", default="")
    # image_note (§8.2) selectors
    ap.add_argument("--post-id", default="")
    ap.add_argument("--reocr", action="store_true",
                    help="image_note: re-OCR an existing known post → new source + supersede")
    # video frames (§8.3)
    ap.add_argument("--frames", action="store_true",
                    help="video: also extract keyframes (→ extract-remote --kind video --frames)")
    ap.add_argument("--cadence", default="", help="video frames: fixed cadence in seconds")
    args = ap.parse_args()

    # honor $VAULT_CONTENT_DIR up front (all platforms run with cwd=content)
    vcd = os.environ.get("VAULT_CONTENT_DIR")
    if vcd:
        os.chdir(vcd)

    # §8.2: image_note routes before the YouTube path (no video_id).
    if args.kind == "image_note":
        return _image_note_main(args)
    # §8.3: video + frames → the frames front door (extract-remote, not transcript-
    # remote). The no-frames youtube path below stays byte-identical.
    if args.kind == "video" and args.frames:
        return _video_frames_main(args)
    # §8.1: route podcast BEFORE the YouTube video_id extraction (which would die
    # on a feed URL). The platform is an explicit flag, never sniffed from the URL.
    if args.platform == "podcast":
        return _podcast_main(args)

    # 2. local identity
    video_id = extract_video_id(args.source)
    if not video_id:
        die(f"no 11-char YouTube video_id in {args.source!r} (pass an explicit watch URL)")
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    # 3. dedup BEFORE the network
    head = _resolve_head_or_die(("youtube_video_id", (video_id,)))
    if head and not args.retranscribe:
        # A frames source is reachable by this plain-video path (same youtube_video_id
        # basis); it carries frame images + frames.json + bundle in evidence_artifacts[]
        # that the two scalar re-hashes above don't cover. Gate on the UNION of every
        # frames signal — physical frames.json OR media.frame_count OR a non-empty
        # evidence_artifacts[] — so deleting any ONE of them (a hand-edit) can't skip the
        # check. With any present, _verify_evidence_or_die runs and dies loud on a
        # missing/empty/tampered block. A genuine plain transcript head has none → skipped.
        # `evidence_artifacts is not None` (not truthiness): a hand-edited `[]` must still
        # route in so _verify_evidence_or_die dies on the empty list, not skip silently.
        return _reuse_committed_source(
            head, origin_ref=args.source, origin_type=args.kind,
            progress_message="reusing existing source_id={source_id} (video_id match)",
            text_tmp_prefix="ingest-media-", canonical_label="transcript.md",
            limit=args.limit, audit_path_fn=_transcript_audit_path, drift_detail=True,
            evidence_condition=lambda dest, media: (
                Path(f"{dest}.assets", "frames.json").is_file()
                or media.get("frame_count") is not None
                or media.get("evidence_artifacts") is not None
            ),
            prompt_text_fn=lambda _dest, audit, cap: render_prompt_copy(_load_json(audit), cap),
        )

    # --retranscribe: re-ASR → mint a NEW source that SUPERSEDES the existing head (same
    # video_id). The wiki re-synthesizes from the fresh transcript via the LLM pipeline, and
    # ingest.py migrates the old source's live citations (rewrite-citations old→new). The old
    # source stays immutable; the resolver picks the new head. The no-retranscribe path above
    # is untouched → byte-identical.
    if args.retranscribe and not head:
        die(f"--retranscribe passed but no prior committed source exists for video {video_id} "
            f"— nothing to supersede; drop --retranscribe to ingest fresh.")
    # A plain --retranscribe (no --frames) on a FRAMES head would mint a transcript-only
    # successor and SILENTLY DROP the committed frame bundle (the resolver would then pick a
    # transcript-only head). Refuse — re-extract with --frames, or remove the source.
    if head and _head_has_frames(head[0]):
        die(f"video {video_id} is a FRAMES source ({head[1].get('source_id')}); plain "
            f"--retranscribe would supersede it with a transcript-only source and drop the "
            f"frame bundle. Re-run with --frames to re-extract frames, or remove the source.")
    supersedes_id = head[1].get("source_id") if head else None
    if head and not supersedes_id:
        die("existing head lacks a source_id — cannot supersede on --retranscribe")

    # 4. call the remote ASR service ($TRANSCRIPT_REMOTE_CMD overrides the CLI —
    #    mirrors $LLM_CMD; lets tests point at a stub and users at a custom client)
    tmp_json = tempfile.mkstemp(prefix="ingest-media-", suffix=".json")[1]
    client = shlex.split(os.environ.get("TRANSCRIPT_REMOTE_CMD", "transcript-remote"))
    progress(f"transcribing {canonical_url} via {client[0]} …")
    r = subprocess.run(
        [*client, canonical_url, "-f", "json", "-o", tmp_json],
        stderr=subprocess.PIPE, text=True,
    )
    if r.returncode != 0:
        die(f"transcript-remote failed: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.returncode}")
    doc = _load_json(tmp_json)

    # video_id reconciliation (§7.1): if the service reports the resolved id and
    # it differs from the one we derived locally, we'd be citing a different
    # video than we transcribed — hard error.
    server_vid = (doc.get("meta") or {}).get("video_id")
    if server_vid and server_vid != video_id:
        die(f"video_id mismatch: local={video_id} but service resolved {server_vid} "
            f"(redirect or wrong URL) — refusing to cite a different video than transcribed")

    # 5. validate — timing HARD gate, then coverage gate
    segs = doc.get("segments") or []
    if not segs:
        die("transcript has no segments")
    prev_end = None
    for s in segs:
        st, en = (s.get("start"), s.get("end")) if isinstance(s, dict) else (None, None)
        if (isinstance(st, bool) or isinstance(en, bool)
                or not isinstance(st, (int, float)) or not isinstance(en, (int, float))
                or not math.isfinite(st) or not math.isfinite(en)):
            die("transcript has segments without finite numeric start/end — "
                "not timestamp-citable (cannot --force past this)")
        # ordering guards (parity with the podcast/frames paths): render_markdown emits
        # in input order without sorting, so an inverted/out-of-order segment would commit
        # non-monotonic timecodes that can't be citation-pinned. Valid (ordered)
        # transcripts are unaffected → committed output stays byte-identical.
        if en < st:
            die(f"transcript has an inverted segment ({en} < {st})")
        if prev_end is not None and st < prev_end:
            die(f"transcript segments overlap or are out of order ({st} < {prev_end})")
        prev_end = en
    speech = sum(max(0.0, s["end"] - s["start"]) for s in segs)
    chars = sum(len((s.get("text") or "")) for s in segs)
    lang_raw = doc.get("language")
    # youtube emits `language: <lang>` RAW (unquoted, byte-identical path) — token-sanitize
    # so a colon can't split the scalar; a normal lang ('en', 'zh-Hant') is unchanged.
    lang = _safe_scalar_token(lang_raw) or "en"
    floor = _COVERAGE_MIN.get(lang, _COVERAGE_DEFAULT)
    cps = chars / speech if speech > 0 else 0.0
    if cps < floor and not args.force:
        die(f"coverage gate: {cps:.1f} chars/s of speech < {floor} for lang={lang} "
            f"(likely truncated/garbled; pass --force to accept density)")
    progress(f"{len(segs)} segments, {speech:.0f}s speech, {cps:.1f} chars/s (lang={lang})")

    # identity + slug
    source_id = new_ulid()
    title = _safe_str(args.title or (doc.get("meta") or {}).get("title") or "") or canonical_url
    slug_title = re.sub(r"[^A-Za-z0-9]+", "-", args.title).strip("-")[:40] if args.title else ""
    slug = f"{today()}-youtube-{video_id}" + (f"-{slug_title}" if slug_title else "")
    if supersedes_id:
        # disambiguate from the (possibly same-day) predecessor's slug; the fresh ULID tail
        # is unique so the new artifacts never collide with the superseded ones.
        slug = f"{slug}-r{source_id[-6:].lower()}"
    added = iso_now()

    # 6. render in a temp dir
    stage = tempfile.mkdtemp(prefix="ingest-media-stage-")
    md_path = os.path.join(stage, f"{slug}.transcript.md")
    json_path = os.path.join(stage, f"{slug}.transcript.json")
    Path(md_path).write_text(render_markdown(doc, title, canonical_url), encoding="utf-8")
    # commit the JSON verbatim (the bytes we fetched) as the audit/render source
    shutil.copyfile(tmp_json, json_path)
    text_file = tempfile.mkstemp(prefix="ingest-media-")[1]
    Path(text_file).write_text(render_prompt_copy(doc, args.limit), encoding="utf-8")

    # 7. hashes
    sha = sha256_of(md_path)
    json_sha = sha256_of(json_path)

    # sidecar (v1 field set; ⧗ fields null until the service emits them)
    meta = doc.get("meta") or {}
    dest = f"sources/{slug}.transcript.md"
    sidecar = f"{dest}.md"
    sidecar_text = _build_sidecar(
        source_id=source_id, sha=sha, added=added, origin_type=args.kind,
        origin_ref=args.source, title=title, video_id=video_id,
        canonical_url=canonical_url, speech=speech, meta=meta, lang=lang,
        json_sha=json_sha, supersedes=supersedes_id,
    )

    # 8. atomic move into sources/ (sidecar LAST), own-orphan rule
    _stage_into_sources(slug, md_path, json_path, sidecar, sidecar_text,
                        video_id, args.source)

    progress(f"new source_id={source_id}  {dest}"
             + (f" (supersedes {supersedes_id})" if supersedes_id else ""))
    emit(
        SOURCE_ID=source_id, SHA256=sha, ADDED=added, ORIGIN_TYPE=args.kind,
        ORIGIN_REF=args.source, DEST=dest, DEST_BASENAME=f"{slug}.transcript.md",
        SIDECAR=sidecar, EXISTING_SIDECAR="", TEXT_FILE=text_file,
        AUDIT_JSON=f"sources/{slug}.transcript.json", SUPERSEDES=(supersedes_id or ""),
    )
    return 0


def _load_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"could not parse transcript JSON {path}: {exc}")
        raise  # unreachable


def _build_sidecar(*, source_id, sha, added, origin_type, origin_ref, title,
                   video_id, canonical_url, speech, meta, lang, json_sha, supersedes=None) -> str:
    ytdlp_ver = meta.get("yt_dlp_version")
    downloader = f"yt-dlp@{ytdlp_ver}" if ytdlp_ver else (meta.get("downloader") or "")
    media_lines = [
        "  platform: youtube",
        f"  video_id: {video_id}",
        f"  canonical_url: {y(canonical_url)}",
        f"  service_source: {y(meta.get('source', canonical_url))}",
        f"  resolved_url: {yopt(meta.get('resolved_url'))}",
        *_storage_guard_lines(),
        f"  speech_duration_s: {int(round(speech))}",
        f"  duration_s: {yopt(meta.get('duration_s'))}",
        f"  channel: {yopt(meta.get('channel'))}",
        f"  uploader: {yopt(meta.get('uploader'))}",
        f"  upload_date: {yopt(meta.get('upload_date'))}",
        "  transcript_kind: asr",
        "  transcript_tool: transcript-remote",
        f"  transcript_server: {yopt(_transcript_server(meta))}",
        f"  asr_engine: {_asr_engine(meta)}",
        f"  asr_model: {y(meta.get('model', ''))}",
        f"  device: {y(meta.get('device', ''))}",
        f"  compute_type: {y(meta.get('compute_type', ''))}",
        f"  align_requested: {yopt(meta.get('align_requested'))}",
        f"  align_succeeded: {yopt(meta.get('align_succeeded'))}",
        f"  diarize_requested: {str(bool(meta.get('diarized'))).lower()}",
        f"  diarize_succeeded: {yopt(meta.get('diarize_succeeded'))}",
        f"  language: {lang}",
        f"  downloader: {yopt(downloader or None)}",
        f"  yt_dlp_version: {yopt(ytdlp_ver)}",
        f"  selected_format: {yopt(meta.get('selected_format'))}",
        f"  ffmpeg_version: {yopt(meta.get('ffmpeg_version'))}",
        f"  pyannote_version: {yopt(meta.get('pyannote_version'))}",
        f"  transcript_job_id: {yopt(meta.get('job_id'))}",
        f"  transcript_json_sha256: {json_sha}",
        f"  render_format_version: {RENDER_FORMAT_VERSION}",
        f"  transcribed: {added}",
    ]
    return _build_media_sidecar(
        source_id=source_id, sha=sha, added=added, origin_type=origin_type,
        origin_ref=origin_ref, title=title, media_lines=media_lines,
        supersedes=supersedes,
    )


def _stage_into_sources(slug, md_path, json_path, sidecar, sidecar_text,
                        video_id, origin_ref) -> None:
    """Move temp artifacts into sources/ in order md → json → sidecar(LAST).
    Refuse to clobber a tracked or non-own-orphan file (§7.2 step 8)."""
    def own_orphan(ofm: dict, dest_json: str) -> bool:
        omedia = ofm.get("media") or {}
        return (
            ofm.get("origin_ref") == origin_ref
            and omedia.get("video_id") == video_id
            and omedia.get("transcript_json_sha256")
            == (sha256_of(dest_json) if Path(dest_json).is_file() else None)
        )

    _stage_media_artifacts(
        slug=slug, md_path=md_path, json_path=json_path, sidecar=sidecar,
        sidecar_text=sidecar_text, md_suffix=".transcript.md",
        json_suffix=".transcript.json", origin_ref=origin_ref,
        own_orphan_check=own_orphan,
    )


if __name__ == "__main__":
    raise SystemExit(main())
