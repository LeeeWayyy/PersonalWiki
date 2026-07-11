#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""
Weekly no-drift lint for the LLM-wiki.

Run from vault root:
    scripts/lint.py

Checks (per plan/llm-wiki-implementation.md §4 "No-drift loop"):
  1. Source drift — every sources/*.md sidecar's sha256 matches its asset.
  2. Open conflicts — ==CONFLICT: ...== markers anywhere in wiki/.
  3. Citation orphans — every [src:<id>] resolves to a known source_id.
  4. Unused sources — every sidecar source_id is cited by ≥1 wiki page.
  5. Zone markers — every wiki page has both llm-zone open + close.
  6. Frontmatter sync — every page's `sources:` list matches the set of
     [src:<id>] citations actually in its body (fix with
     scripts/sync-frontmatter.py).

Then prints 2–3 random wiki pages for human spot-checking.

Exits 0 if all checks pass, non-zero and prints a summary if not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
import unicodedata
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ — for media_resolver
from _util import default_vault_root  # noqa: E402
import media_resolver  # noqa: E402  (shared evidence-completeness check, §8.0)
from media_resolver import parse_frontmatter, sha256_of  # noqa: E402  (single source of truth)
from source_citations import (  # noqa: E402
    BRACKETED_CITATION_RX,
    iter_source_citations,
)

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
SOURCES_DIR = VAULT_ROOT / "sources"
WIKI_DIR = VAULT_ROOT / "wiki"
TAXONOMY_PATH = WIKI_DIR / "_taxonomy.md"
INDEX_DIR = WIKI_DIR / "_index"
MAPS_DIR = WIKI_DIR / "_maps"

# Tags must match this regex: lowercase ASCII letters/digits, with `/` and `-`
# as the only separators. No leading/trailing/adjacent separators. No `_`
# (the slug rule depends on `_` being absent so `/`→`__` is collision-free).
TAG_RX = re.compile(r"^[a-z0-9]+([-/][a-z0-9]+)*$")
# Strict taxonomy-bullet pattern. The tag must be wrapped in backticks.
TAXONOMY_BULLET_RX = re.compile(r"^- `([^`]+)`")
# Loose "looks like a bullet" detector for the loud-fail-on-malformed rule.
LOOSE_BULLET_RX = re.compile(r"^- ")

# Citation extraction is two-step: first find `[...]` segments that
# contain `src:`, then pull `src:<ULID>` out of each. This tolerates any
# legal interior whitespace (e.g. `[src:a, src:b]`) and any anchor form
# (`#§1`, `#第一章`), while preventing matches in headings/prose like
# `### From src:01K…` (where `src:` is not inside brackets).
_FENCED_CODE_RX = re.compile(r"```.*?```", re.DOTALL)


def extract_citations(text: str) -> list[str]:
    """Return the list of source_ids referenced in body citations.
    Order is body order; duplicates allowed. Citations inside fenced code
    blocks are excluded — those are examples, not real provenance."""
    text = _FENCED_CODE_RX.sub("", text)
    # Keep malformed ids too: the orphan gate must reject them rather than
    # silently treating a page with bad citations as uncited.
    return [citation.source_id for citation in iter_source_citations(text)]


def extract_citation_keys(text: str) -> list[str]:
    """Return source ids with optional anchors (`<id>` or `<id>#<label>`).
    Used when section/chapter granularity matters."""
    text = _FENCED_CODE_RX.sub("", text)
    out: list[str] = []
    for citation in iter_source_citations(text):
        suffix = f"#{citation.raw_anchor}" if citation.raw_anchor else ""
        out.append(citation.source_id + suffix)
    return out


# Time-range media anchors: `src:<ULID>#H:MM:SS-H:MM:SS` (hours optional).
TIME_ANCHOR_RX = re.compile(
    # trailing (?![\w:-]) end-boundary: `#0:00-0:05x` must not prefix-match as `0:00-0:05`
    # (chapter/section labels like `#§1`/`#第一章` never match this regex, so are unaffected).
    r"src:([A-Z0-9]{26})#(\d{1,2}:\d{2}(?::\d{2})?)-(\d{1,2}:\d{2}(?::\d{2})?)(?![\w:-])"
)


def _ts_to_seconds(ts: str) -> int:
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def _load_segments(json_path: Path) -> list[tuple[float, float]] | None:
    """(start,end) spans from a committed .transcript.json; None if unreadable OR not a
    transcript-shaped json. The path-stem collision `<slug>.cards.md`→`<slug>.cards.json`
    means an image_note's audit (a LIST of cards) can be handed here; it must yield None
    (→ clean 'non-timestamped source' capability error), never crash on `.get`."""
    try:
        d = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("segments"), list):
        return None
    out: list[tuple[float, float]] = []
    for s in d["segments"]:
        if not isinstance(s, dict):
            continue
        st, en = s.get("start"), s.get("end")
        # reject bool (float(True)==1.0) and non-finite (NaN/inf) bounds — parity with the
        # ingest paths, else an anchor could "resolve" against malformed committed timing.
        if (isinstance(st, bool) or isinstance(en, bool)
                or not isinstance(st, (int, float)) or not isinstance(en, (int, float))
                or not math.isfinite(st) or not math.isfinite(en)):
            continue
        out.append((float(st), float(en)))
    return out


PAGE_ID_RX = re.compile(r"^page_id:\s*([A-Z0-9]{26})\s*$", re.MULTILINE)
CONFLICT_RX = re.compile(r"==CONFLICT:[^=]+==")
# Wikilink: `[[Page]]`, `[[Page#anchor]]`, `[[Page|alt]]`, `[[Page#anchor|alt]]`.
# Capture group 1 is the page reference (everything before # or |).
WIKILINK_RX = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
ALIAS_INDEX_PATH = VAULT_ROOT / "wiki" / ".alias-index.json"
LLM_OPEN = "<!-- llm-zone -->"
LLM_CLOSE = "<!-- /llm-zone -->"


def normalize_alias(s: str) -> str:
    """Match alias-index.py normalization: NFKC + casefold + whitespace squeeze."""
    s = unicodedata.normalize("NFKC", s).casefold()
    return re.sub(r"\s+", " ", s).strip()


# ─── helpers ────────────────────────────────────────────────────────────────


def all_wiki_pages() -> list[Path]:
    pages: list[Path] = []
    for sub in ("entities", "topics"):
        d = WIKI_DIR / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    return pages


def lang_pages() -> list[Path]:
    """Tool-owned language pages under the `--profile lang` subtree.
    Includes current `_reading/*.html` renders plus legacy
    `_study/_vocab/_grammar` markdown artifacts. Kept SEPARATE from the wiki
    helpers so no wiki constant leaks into the lang lint path."""
    pages: list[Path] = []
    for sub in ("_study", "_vocab", "_grammar"):
        d = VAULT_ROOT / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    reading = VAULT_ROOT / "_reading"
    if reading.is_dir():
        pages.extend(sorted(reading.rglob("*.html")))
    return pages


def generated_pages() -> list[Path]:
    """Tool-owned generated pages: MOCs (`_index/`) + mind maps (`_maps/`).
    Kept SEPARATE from `all_wiki_pages()` so content-page checks (§2
    frontmatter, tags, citations, llm-zone structure) never fire on generated
    pages — they're exempt by schema §1. Only the cross-cutting checks
    that legitimately apply to generated content (open conflicts,
    wikilink resolution) opt in via this helper."""
    pages: list[Path] = []
    for d in (INDEX_DIR, MAPS_DIR):
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    return pages


# Unambiguous sidecar-name suffixes: `<asset-ext>.md` for every extension extract.py
# dispatches (scripts/extract.py:dispatch) plus the media `.md.md` double-suffix. Keep in
# sync with extract.py's accepted extensions.
_SIDECAR_SUFFIXES = (".md.md", ".epub.md", ".pdf.md", ".html.md", ".htm.md",
                     ".xhtml.md", ".txt.md", ".markdown.md", ".rst.md")


def all_sidecars() -> list[Path]:
    """Every committed source sidecar (`<asset>.md` with `type: source` frontmatter).

    A sidecar is `<asset>.md`. Identify it by EITHER its sibling asset existing
    (`p.with_suffix("").exists()` — the normal case, which also catches a sidecar whose
    YAML has gone MALFORMED since the asset itself is untouched) OR `type: source`
    frontmatter (which catches a sidecar whose asset was DELETED — an orphan). This
    avoids three traps from earlier attempts:
      - asset-existence alone dropped an asset-orphaned sidecar (no-silent-drift hole);
      - `type: source` alone dropped a malformed (unparsable) sidecar;
      - leading-`---` alone misclassified a canonical Markdown source artifact
        (`sources/<slug>.md` with frontmatter, whose real sidecar is `<slug>.md.md`) as a
        sidecar, then bricked lint on a missing `<slug>` asset.
    Canonical `.md` assets (`*.transcript.md`, `*.cards.md`, a hand-written `foo.md`) have
    neither a `<name-minus-.md>` sibling nor `type: source`, so they're excluded. The
    `.md.md` double-suffix is an UNAMBIGUOUS sidecar marker (only a sidecar of a `.md`
    asset — every media sidecar, `*.transcript.md.md`/`*.cards.md.md` — ends that way;
    canonical assets and markdown sources end in a single `.md`), so it's always included.
    That closes the simultaneously-orphaned-AND-malformed blind spot for media sidecars
    (a `.epub.md`-style doc sidecar in that double-failure state is the only residual).
    check_source_drift reports any included orphan/malformed sidecar loudly (missing
    asset / no sha256)."""
    out: list[Path] = []
    for p in sorted(SOURCES_DIR.glob("*.md")):
        if p.name == "README.md":
            continue
        # _SIDECAR_SUFFIXES are unambiguous sidecar markers (`<asset-ext>.md` for every
        # extension extract.py ingests + `.md.md`) — a canonical `.transcript.md`/`.cards.md`
        # asset or a markdown SOURCE `foo.md` never ends that way, but an orphaned AND
        # malformed sidecar still does, so it stays visible to drift. Sibling-exists /
        # type:source are the fallbacks for the normal (well-formed) cases.
        if (p.name.endswith(_SIDECAR_SUFFIXES)
                or p.with_suffix("").exists()
                or parse_frontmatter(p).get("type") == "source"):
            out.append(p)
    return out


# ─── checks ─────────────────────────────────────────────────────────────────


def _under_sources(path) -> bool:
    """An evidence/audit path is acceptable only if it is lexically under sources/ (no
    `..`) AND resolves — following symlinks — to a location still under sources/. The
    lexical check alone lets a committed symlink (`sources/link.jpg -> /outside`) make
    drift/reuse fingerprint a file outside the vault, defeating the no-silent-drift
    contract."""
    if not path or ".." in str(path).split("/") or not str(path).startswith("sources/"):
        return False
    try:
        (VAULT_ROOT / path).resolve().relative_to((VAULT_ROOT / "sources").resolve())
    except (ValueError, OSError):
        return False
    return True


def _recompute_bundle(recipe: str, args: dict) -> str | None:
    """Recompute a derived bundle hash from committed files (schema §7.2). Returns
    the hex digest, or None if the recipe/inputs are unusable (caller reports it).

    Recipes:
      `image_sha256_index_join` — sha256 over the index-ordered per-image
      `image_sha256` values from a committed `.cards.json` (`from:` path),
      `\n`-joined (UTF-8, LF, no trailing newline). This is the image_note
      `image_bundle_sha256` (and the frames frame-bundle, same shape).
    """
    if recipe != "image_sha256_index_join":
        return None
    src = args.get("from")
    # Same constraint as file artifacts (under sources/, no `..`, no symlink escape):
    # a hand-edited `from:` must not make lint fingerprint a file outside sources/.
    if not _under_sources(src):
        return None
    p = VAULT_ROOT / src
    if not p.is_file():
        return None
    try:
        cards = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cards, list):
        return None
    try:
        ordered = sorted(cards, key=lambda c: c["index"])
        joined = "\n".join(str(c["image_sha256"]) for c in ordered)
    except (KeyError, TypeError):
        return None
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _requires_evidence(sidecar: Path, fm: dict) -> bool:
    """True for sidecars whose drift guard IS media.evidence_artifacts[]. image_note
    always qualifies; a frames source is identified by its PHYSICAL committed
    `<slug>.transcript.md.assets/frames.json` — NOT a mutable frontmatter scalar — so
    deleting media.frame_count can't silently demote it and disable the guard. Podcast /
    plain-youtube carry none (scalar hash guards) and are exempt.

    INTENTIONAL asymmetry with _frame_ordinals (which requires origin_type: video AND
    frame_count): that one is STRICT to avoid validating #frame-N against a stray
    frames.json (false-positive capability); this one is BROAD (physical file only) to
    avoid letting a YAML edit escape the drift guard (false-negative). Do NOT "align"
    them by adding the semantic guard here — it would reopen the silent-drift hole."""
    if fm.get("origin_type") == "image_note":
        return True
    md = sidecar.with_suffix("")  # <slug>.transcript.md.md → <slug>.transcript.md
    # A frames source is TRANSCRIPT-shaped (`<slug>.transcript.md`) with a frame bundle under
    # `<slug>.transcript.md.assets/`. Detect it by PHYSICAL signals on THAT dir — NOT the
    # mutable origin_type/frame_count YAML (tamper-resistant: editing origin_type away from
    # `video` can't demote it). Document sources (`.epub`/`.pdf` → `.epub.assets/` etc) are
    # not `.transcript.md`, so their figure dirs are correctly excluded; a plain youtube
    # transcript / podcast has no `.transcript.md.assets/` and stays scalar-guarded → False.
    if not str(md).endswith(".transcript.md"):
        return False
    media = fm.get("media") or {}
    assets = Path(str(md) + ".assets")
    if (assets / "frames.json").is_file():
        return True
    if media.get("frame_count") is not None or bool(media.get("evidence_artifacts")):
        return True
    # tamper net: frame images still committed under .transcript.md.assets/ even after every
    # YAML signal AND frames.json were hand-deleted → still require evidence (fail loud).
    return bool(media_resolver.assets_dir_tracked_files(VAULT_ROOT, str(assets.relative_to(VAULT_ROOT))))


def _check_evidence_artifacts(sidecar: Path, fm: dict) -> tuple[bool, list[str]]:
    """Re-hash every committed evidence artifact named in media.evidence_artifacts[]
    (§7.2). File entries (`path` + `sha256`) are re-hashed directly; `bundle_recipe`
    entries are recomputed via _recompute_bundle. Paths must be repo-relative, under
    sources/, with no `..`."""
    ok = True
    notes: list[str] = []
    arts = (fm.get("media") or {}).get("evidence_artifacts") or []
    rel = sidecar.relative_to(VAULT_ROOT)
    if not isinstance(arts, list):
        return False, [f"  ✗ {rel}: media.evidence_artifacts is not a list"]
    if not arts and _requires_evidence(sidecar, fm):
        # For image_note + frames-video sidecars, evidence_artifacts[] IS the drift
        # guard — an absent/empty list must fail loud, never coerce to a vacuous pass.
        return False, [f"  ✗ {rel}: {fm.get('origin_type')} sidecar has no "
                       f"media.evidence_artifacts[] (required — single source of drift truth)"]
    for art in arts:
        if not isinstance(art, dict) or "sha256" not in art:
            notes.append(f"  ✗ {rel}: malformed evidence_artifacts entry: {art!r}")
            ok = False
            continue
        role = art.get("role", "?")
        if "bundle_recipe" in art:
            got = _recompute_bundle(art["bundle_recipe"], art)
            if got is None:
                notes.append(f"  ✗ {rel}: evidence bundle '{role}' is unverifiable "
                             f"(recipe={art['bundle_recipe']!r}, inputs missing)")
                ok = False
            elif got != art["sha256"]:
                notes.append(f"  ✗ {rel}: evidence bundle '{role}' drifted "
                             f"(sidecar={art['sha256'][:12]}…  actual={got[:12]}…)")
                ok = False
            continue
        path = art.get("path")
        if not _under_sources(path):
            notes.append(f"  ✗ {rel}: evidence artifact '{role}' has an unsafe/invalid path: {path!r}")
            ok = False
            continue
        ap = VAULT_ROOT / path
        if not ap.is_file():
            notes.append(f"  ✗ {rel}: evidence artifact '{role}' missing: {path}")
            ok = False
        else:
            actual = sha256_of(ap)
            if actual != art["sha256"]:
                notes.append(f"  ✗ {path} drifted: sidecar={art['sha256'][:12]}…  actual={actual[:12]}…")
                ok = False
    # Completeness: re-hashing only the LISTED entries isn't enough (a removed image entry,
    # a tampered dedup scalar, a mis-anchored audit row, or a .cards.md/.cards.json
    # disagreement would pass). Defer to the SHARED check so lint and ingest reuse can't
    # diverge (media_resolver.evidence_completeness_notes).
    if _requires_evidence(sidecar, fm):
        for note in media_resolver.evidence_completeness_notes(sidecar, fm, VAULT_ROOT):
            notes.append(f"  ✗ {rel}: {note}")
            ok = False
    return ok, notes


def check_source_drift(profile: str = "wiki") -> tuple[bool, list[str]]:
    """Every sidecar's sha256 must match its asset. Under profile="lang" the
    media-transcript audit block + evidence_artifacts are skipped (lang v1 has
    no media; a plain document named `*.transcript.md` must not be forced to
    carry a `.transcript.json`)."""
    notes: list[str] = []
    ok = True
    sidecars = all_sidecars()
    for sidecar in sidecars:
        asset = sidecar.with_suffix("")
        fm = parse_frontmatter(sidecar)
        if not asset.is_file():
            notes.append(f"  ✗ {sidecar.relative_to(VAULT_ROOT)}: canonical asset missing "
                         f"({asset.name}) — a committed source artifact was deleted")
            ok = False
            continue
        expected = fm.get("sha256")
        if not expected:
            notes.append(f"  ✗ {sidecar.relative_to(VAULT_ROOT)}: no sha256 in frontmatter")
            ok = False
            continue
        actual = sha256_of(asset)
        if actual != expected:
            sid = fm.get("source_id", "<no id>")
            notes.append(
                f"  ✗ {asset.relative_to(VAULT_ROOT)} drifted: sidecar={expected[:12]}…  actual={actual[:12]}…"
            )
            notes.append(
                f"    → create a new sidecar with a fresh source_id and "
                f"`supersedes: [[{sid}]]`, then re-ingest under the new id."
            )
            ok = False
        # Media (§7.1): guard the committed `.transcript.json` audit artifact — transcript
        # sources ONLY (image_note's `.cards.json` is guarded via evidence_artifacts). It's
        # not a `*.md` asset, so the generic check never sees it. It must be covered by
        # EITHER the transcript_json_sha256 scalar OR a transcript_json evidence entry;
        # losing BOTH (a hand-edit) must fail loud, never silently accept JSON drift.
        if profile == "wiki" and str(asset).endswith(".transcript.md"):
            # A physical frames.json beside a transcript source means it IS a frames source;
            # it MUST declare origin_type: video. Otherwise a hand-edit of origin_type would
            # silently demote its #frame-N capability (_frame_ordinals is origin_type-gated by
            # design, to avoid validating against a stray frames.json) — fail loud here instead.
            if (Path(str(asset) + ".assets") / "frames.json").is_file() and fm.get("origin_type") != "video":
                notes.append(f"  ✗ {asset.relative_to(VAULT_ROOT)}: has a committed frames.json but "
                             f"origin_type={fm.get('origin_type')!r} (a frames source must be origin_type: video)")
                ok = False
            # EVERY transcript source commits a `.transcript.json` audit artifact (schema
            # §7.2). It must EXIST and be hash-guarded by the scalar OR a transcript_json
            # evidence entry — deleting the file AND its guard (a hand-edit) must fail loud,
            # never silently drop a committed audit artifact from the drift contract.
            json_asset = asset.with_suffix(".json")  # <slug>.transcript.md → .transcript.json
            json_sha = (fm.get("media") or {}).get("transcript_json_sha256")
            ev = (fm.get("media") or {}).get("evidence_artifacts")
            # the transcript_json entry only COVERS this source if its path is THIS sidecar's
            # sibling — else (audio/plain transcript don't run completeness path-binding) a
            # podcast could point it at another file and let its real .transcript.json drift.
            expected_tj = str(json_asset.relative_to(VAULT_ROOT))
            ev_covers = isinstance(ev, list) and any(
                isinstance(a, dict) and a.get("role") == "transcript_json"
                and a.get("path") == expected_tj for a in ev)
            if not json_asset.is_file():
                notes.append(f"  ✗ {asset.relative_to(VAULT_ROOT)}: committed transcript.json missing "
                             f"(every transcript source must have its audit artifact)")
                ok = False
            elif not json_sha and not ev_covers:
                notes.append(f"  ✗ {json_asset.relative_to(VAULT_ROOT)}: committed transcript.json is "
                             f"not hash-guarded (no transcript_json_sha256 scalar or evidence entry)")
                ok = False
            elif json_sha:
                json_actual = sha256_of(json_asset)
                if json_actual != json_sha:
                    notes.append(
                        f"  ✗ {json_asset.relative_to(VAULT_ROOT)} drifted: "
                        f"sidecar={json_sha[:12]}…  actual={json_actual[:12]}…"
                    )
                    ok = False
        # Media §7.2: re-hash every committed NON-canonical evidence artifact named
        # in `media.evidence_artifacts[]` (the generalized single-source-of-truth
        # drift guard for image_note/frames: .cards.json / _manifest.md / frames.json,
        # plus derived bundle hashes via `bundle_recipe`). Each entry is
        # {role, path?, sha256, bundle_recipe?}. Paths are repo-relative, under
        # sources/, no `..`. A bundle_recipe entry is verified by its recipe handler.
        if profile == "wiki":
            e_ok, e_notes = _check_evidence_artifacts(sidecar, fm)
            ok = ok and e_ok
            notes.extend(e_notes)
    if ok:
        notes.append(f"  ✓ all {len(sidecars)} source(s) match their sha256")
    return ok, notes


def check_timestamp_anchors() -> tuple[bool, list[str]]:
    """§7.4: every `[src:<id>#H:MM:SS-H:MM:SS]` time-range citation must resolve
    to real transcript segments in the source's committed `.transcript.json`:
    a < b, ≤ 10-min cap, and covered by segments (≤ 2 s gaps, ±2 s bounds). A
    time anchor on a non-media (no `.transcript.json`) source is itself an error.
    No-op for document-only vaults (no time anchors → checked == 0)."""
    MAX_CAP, GAP_TOL, BOUND_TOL = 600, 2.0, 2.0
    notes: list[str] = []
    ok = True
    sources = collect_source_ids()
    seg_cache: dict[str, list[tuple[float, float]] | None] = {}
    checked = 0
    for page in all_wiki_pages():
        text = _FENCED_CODE_RX.sub("", page.read_text(encoding="utf-8", errors="replace"))
        cites = "\n".join(BRACKETED_CITATION_RX.findall(text))  # only bracketed [src:…] spans
        for m in TIME_ANCHOR_RX.finditer(cites):
            sid, a_s, b_s = m.group(1), m.group(2), m.group(3)
            a, b = _ts_to_seconds(a_s), _ts_to_seconds(b_s)
            checked += 1
            loc = f"{page.relative_to(VAULT_ROOT)}: [src:{sid[:8]}…#{a_s}-{b_s}]"
            if a >= b:
                notes.append(f"  ✗ {loc}: zero/negative range")
                ok = False
                continue
            if b - a > MAX_CAP:
                notes.append(f"  ✗ {loc}: range exceeds {MAX_CAP // 60}-min cap")
                ok = False
                continue
            if sid not in sources:
                continue  # unknown source_id — the citation-orphan check owns that
            if sid not in seg_cache:
                jpath = sources[sid].with_suffix("").with_suffix(".json")  # …transcript.md.md → …transcript.json
                seg_cache[sid] = _load_segments(jpath) if jpath.is_file() else None
            segs = seg_cache[sid]
            if segs is None:
                notes.append(f"  ✗ {loc}: time anchor on a non-timestamped source")
                ok = False
                continue
            overlapping = sorted(s for s in segs if s[1] >= a - BOUND_TOL and s[0] <= b + BOUND_TOL)
            cursor = float(a)
            for s0, s1 in overlapping:
                if s0 > cursor + GAP_TOL:
                    break
                cursor = max(cursor, s1)
            if cursor < b - BOUND_TOL:
                notes.append(f"  ✗ {loc}: range not covered by speech (gap > {GAP_TOL}s)")
                ok = False
    if ok:
        notes.append(f"  ✓ all {checked} time-range citation(s) resolve to transcript segments")
    return ok, notes


CARD_ANCHOR_RX = re.compile(r"src:([A-Z0-9]{26})#card-(\d+)(?![\w-])")  # end-boundary: no `#card-2extra`
_CARD_HEADING_RX = re.compile(r"^## card (\d+)\s*$", re.MULTILINE)


def _image_note_cards(sidecar: Path) -> tuple[int, set[int]] | None:
    """(card_count, {heading Ns}) for an image_note source, or None if the sidecar
    is not an image_note (so a card anchor on it is a capability error)."""
    fm = parse_frontmatter(sidecar)
    if fm.get("origin_type") != "image_note":
        return None
    md = sidecar.with_suffix("")  # <slug>.cards.md.md → <slug>.cards.md
    if not md.is_file():
        return None
    headings = {int(x) for x in _CARD_HEADING_RX.findall(md.read_text(encoding="utf-8", errors="replace"))}
    cc = (fm.get("media") or {}).get("card_count")
    # a hand-edited non-int card_count must not crash `1 <= n <= cc`; coerce to 0 → every
    # anchor falls out of range and fails loud (the completeness gate flags the bad scalar).
    cc = cc if isinstance(cc, int) and not isinstance(cc, bool) else 0
    return (cc, headings)


def check_card_anchors() -> tuple[bool, list[str]]:
    """§8.2: every `[src:<id>#card-N]` must cite an image_note source and resolve to
    a real `## card N` heading in its committed `.cards.md`, with 1 ≤ N ≤ card_count.
    A card anchor on a non-image_note source is a capability error. No-op for vaults
    with no card anchors."""
    notes: list[str] = []
    ok = True
    sources = collect_source_ids()
    cache: dict[str, tuple[int, set[int]] | None] = {}
    checked = 0
    for page in all_wiki_pages():
        text = _FENCED_CODE_RX.sub("", page.read_text(encoding="utf-8", errors="replace"))
        cites = "\n".join(BRACKETED_CITATION_RX.findall(text))  # only bracketed [src:…] spans
        for m in CARD_ANCHOR_RX.finditer(cites):
            sid, n = m.group(1), int(m.group(2))
            checked += 1
            loc = f"{page.relative_to(VAULT_ROOT)}: [src:{sid[:8]}…#card-{n}]"
            if sid not in sources:
                continue  # unknown id — the citation-orphan check owns that
            if sid not in cache:
                cache[sid] = _image_note_cards(sources[sid])
            info = cache[sid]
            if info is None:
                notes.append(f"  ✗ {loc}: card anchor on a non-image_note source")
                ok = False
                continue
            card_count, headings = info
            if not (1 <= n <= card_count):
                notes.append(f"  ✗ {loc}: card N out of range (1..{card_count})")
                ok = False
            elif n not in headings:
                notes.append(f"  ✗ {loc}: no `## card {n}` heading in the committed .cards.md")
                ok = False
    if ok:
        notes.append(f"  ✓ all {checked} card citation(s) resolve to real cards")
    return ok, notes


FRAME_ANCHOR_RX = re.compile(r"src:([A-Z0-9]{26})#frame-(\d+)(?![\w-])")  # end-boundary: no `#frame-1oops`


def _frame_ordinals(sidecar: Path) -> set[int] | None:
    """{1-based frame ordinals} for a frames source, or None if the source carries no
    frames (so a frame anchor on it is a capability error). Frame-capability is decided
    by sidecar SEMANTICS (origin_type: video + media.frame_count), NOT merely by a
    sibling frames.json existing — a stray/stale frames.json beside the wrong source
    must not make `#frame-N` validate against a source never committed with frames."""
    fm = parse_frontmatter(sidecar)
    if fm.get("origin_type") != "video" or (fm.get("media") or {}).get("frame_count") is None:
        return None
    md = sidecar.with_suffix("")  # <slug>.transcript.md.md → <slug>.transcript.md
    fj = Path(str(md) + ".assets") / "frames.json"
    if not fj.is_file():
        return None
    try:
        frames = json.loads(fj.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(frames, list):
        return None
    return {f["index"] for f in frames
            if isinstance(f, dict) and isinstance(f.get("index"), int) and not isinstance(f["index"], bool)}


def check_frame_anchors() -> tuple[bool, list[str]]:
    """§8.3: every `[src:<id>#frame-N]` must cite a frames source and resolve to a
    pinned ordinal in its committed `frames.json`. A frame anchor on a source with no
    frames is a capability error. `#frame-N` is disjoint from `TIME_ANCHOR_RX` (which
    needs a `H:MM:SS-…` range). No-op for vaults with no frame anchors."""
    notes: list[str] = []
    ok = True
    sources = collect_source_ids()
    cache: dict[str, set[int] | None] = {}
    checked = 0
    for page in all_wiki_pages():
        text = _FENCED_CODE_RX.sub("", page.read_text(encoding="utf-8", errors="replace"))
        cites = "\n".join(BRACKETED_CITATION_RX.findall(text))  # only bracketed [src:…] spans
        for m in FRAME_ANCHOR_RX.finditer(cites):
            sid, n = m.group(1), int(m.group(2))
            checked += 1
            loc = f"{page.relative_to(VAULT_ROOT)}: [src:{sid[:8]}…#frame-{n}]"
            if sid not in sources:
                continue  # unknown id — the citation-orphan check owns that
            if sid not in cache:
                cache[sid] = _frame_ordinals(sources[sid])
            ordinals = cache[sid]
            if ordinals is None:
                notes.append(f"  ✗ {loc}: frame anchor on a source with no frames")
                ok = False
            elif n not in ordinals:
                notes.append(f"  ✗ {loc}: no frame {n} in the committed frames.json")
                ok = False
    if ok:
        notes.append(f"  ✓ all {checked} frame citation(s) resolve to real frames")
    return ok, notes


def check_open_conflicts_on_pages(pages: list[Path]) -> tuple[bool, list[str]]:
    """No unresolved ==CONFLICT:== markers in the given pages (their
    human-zones are hand-edited, so conflict markers there must fail)."""
    notes: list[str] = []
    hits: list[tuple[Path, int, str]] = []
    for page in pages:
        for i, line in enumerate(page.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in CONFLICT_RX.finditer(line):
                hits.append((page, i, m.group(0)))
    if not hits:
        notes.append("  ✓ none")
        return True, notes
    notes.append(f"  ✗ {len(hits)} open conflict(s):")
    for page, i, txt in hits:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}:{i}  {txt}")
    return False, notes


def check_open_conflicts() -> tuple[bool, list[str]]:
    """No unresolved ==CONFLICT:== markers anywhere in wiki/ — entities/topics
    (via all_wiki_pages()) AND generated pages (`_index/` MOCs + `_maps/` mind
    maps). Generated dirs are is_dir()-guarded inside their helpers."""
    return check_open_conflicts_on_pages(list(all_wiki_pages()) + generated_pages())


def collect_citations_for(pages: list[Path]) -> dict[Path, set[str]]:
    """page → set of source_ids it cites, over the given page set."""
    out: dict[Path, set[str]] = {}
    for page in pages:
        ids = set(extract_citations(page.read_text(encoding="utf-8", errors="replace")))
        if ids:
            out[page] = ids
    return out


def collect_citations() -> dict[Path, set[str]]:
    """page → set of source_ids it cites (wiki entities/topics)."""
    return collect_citations_for(all_wiki_pages())


def collect_source_ids() -> dict[str, Path]:
    """source_id → sidecar path. (Duplicate ids are caught separately by
    check_duplicate_source_ids; this map is last-write-wins, used after that gate.)"""
    out: dict[str, Path] = {}
    for sidecar in all_sidecars():
        fm = parse_frontmatter(sidecar)
        sid = fm.get("source_id")
        if sid:
            out[sid] = sidecar
    return out


def check_duplicate_source_ids() -> tuple[bool, list[str]]:
    """No two committed source sidecars may share a source_id (immutable identity).
    Only reachable via a hand-edit or a ULID-generator regression, but a duplicate would
    make citations/anchors resolve ambiguously (collect_source_ids is last-write-wins) —
    fail loud, symmetric with check_page_id_present for wiki pages."""
    seen: dict[str, Path] = {}
    dups: list[tuple[str, Path, Path]] = []
    for sidecar in all_sidecars():
        sid = parse_frontmatter(sidecar).get("source_id")
        if not sid:
            continue
        if sid in seen:
            dups.append((sid, seen[sid], sidecar))
        else:
            seen[sid] = sidecar
    if not dups:
        return True, ["  ✓ no duplicate source_id"]
    notes = [f"  ✗ {len(dups)} duplicate source_id(s):"]
    for sid, a, b in dups:
        notes.append(f"    {sid}: {a.relative_to(VAULT_ROOT)} ↔ {b.relative_to(VAULT_ROOT)}")
    return False, notes


def check_citation_orphans(
    cites: dict[Path, set[str]], sources: dict[str, Path]
) -> tuple[bool, list[str]]:
    notes: list[str] = []
    orphans: list[tuple[Path, str]] = []
    total = 0
    for page, ids in cites.items():
        for sid in ids:
            total += 1
            if sid not in sources:
                orphans.append((page, sid))
    if not orphans:
        notes.append(f"  ✓ all {total} citation(s) resolve to a known source_id")
        return True, notes
    notes.append(f"  ✗ {len(orphans)} orphan citation(s):")
    for page, sid in orphans:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}  cites unknown src:{sid}")
    return False, notes


def check_unused_sources(
    cites: dict[Path, set[str]], sources: dict[str, Path]
) -> tuple[bool, list[str]]:
    notes: list[str] = []
    cited: set[str] = set()
    for ids in cites.values():
        cited.update(ids)
    unused = [sid for sid in sources if sid not in cited]
    if not unused:
        notes.append(f"  ✓ all {len(sources)} source(s) are cited by ≥1 wiki page")
        return True, notes
    notes.append(f"  ⚠ {len(unused)} source(s) not cited anywhere (not necessarily wrong):")
    for sid in unused:
        notes.append(f"    src:{sid}  sidecar: {sources[sid].relative_to(VAULT_ROOT)}")
    return True, notes  # warning only, does not fail the lint


def check_zone_markers() -> tuple[bool, list[str]]:
    notes: list[str] = []
    pages = all_wiki_pages()
    bad: list[tuple[Path, str]] = []
    for page in pages:
        text = page.read_text(encoding="utf-8", errors="replace")
        if LLM_OPEN not in text:
            bad.append((page, "missing <!-- llm-zone -->"))
        if LLM_CLOSE not in text:
            bad.append((page, "missing <!-- /llm-zone -->"))
    if not bad:
        notes.append(f"  ✓ all {len(pages)} wiki pages have complete zone markers")
        return True, notes
    notes.append(f"  ✗ {len(bad)} zone-marker issue(s):")
    for page, msg in bad:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}  {msg}")
    return False, notes


def check_page_id_present() -> tuple[bool, list[str]]:
    """Every wiki page must have a `page_id: <ULID>` line in frontmatter.
    Tools (rewire, alias index) reference pages by it; without it,
    renames lose identity. Fix with `scripts/add-page-id.py --all`."""
    notes: list[str] = []
    bad: list[Path] = []
    seen: dict[str, Path] = {}
    dupes: list[tuple[Path, Path, str]] = []
    pages = all_wiki_pages()
    for page in pages:
        text = page.read_text(encoding="utf-8", errors="replace")
        m = PAGE_ID_RX.search(text)
        if not m:
            bad.append(page)
            continue
        pid = m.group(1)
        if pid in seen:
            dupes.append((seen[pid], page, pid))
        else:
            seen[pid] = page
    if not bad and not dupes:
        notes.append(f"  ✓ all {len(pages)} pages have unique page_id")
        return True, notes
    if bad:
        notes.append(f"  ✗ {len(bad)} page(s) missing page_id:")
        for p in bad:
            notes.append(f"    {p.relative_to(VAULT_ROOT)}")
        notes.append("    → fix: scripts/add-page-id.py --all")
    if dupes:
        notes.append(f"  ✗ {len(dupes)} duplicate page_id collision(s):")
        for a, b, pid in dupes:
            notes.append(f"    {pid}: {a.relative_to(VAULT_ROOT)} ↔ {b.relative_to(VAULT_ROOT)}")
    return False, notes


def _load_alias_index() -> dict | None:
    if not ALIAS_INDEX_PATH.exists():
        return None
    try:
        return json.loads(ALIAS_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def check_wikilinks() -> tuple[bool, list[str]]:
    """Every `[[Page]]` resolves to exactly one page via the alias index.
    Zero matches = orphan (error). >1 = ambiguous (error). Self-links
    (page references itself) are tolerated."""
    notes: list[str] = []
    idx = _load_alias_index()
    if idx is None:
        notes.append("  ⚠ alias index missing — run `scripts/alias-index.py build`")
        return True, notes  # warn-only when index is absent

    aliases = idx.get("aliases", {})
    pages_by_id = idx.get("pages", {})
    page_id_by_path: dict[str, str] = {
        info["path"]: pid for pid, info in pages_by_id.items() if "path" in info
    }

    orphans: list[tuple[Path, str]] = []
    ambiguous: list[tuple[Path, str, list[str]]] = []
    total = 0
    # Content pages + generated maps/MOCs: the map outline emits real
    # [[wikilinks]] that must resolve. Mermaid blocks are fenced code and
    # stripped below, so only the outline links are checked.
    for page in all_wiki_pages() + generated_pages():
        text = page.read_text(encoding="utf-8", errors="replace")
        # Skip wikilinks inside fenced code blocks — they're examples, not links.
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        rel = str(page.relative_to(VAULT_ROOT))
        self_pid = page_id_by_path.get(rel)
        for m in WIKILINK_RX.finditer(text):
            if m.start() > 0 and text[m.start() - 1] == "!":
                continue
            ref = m.group(1).strip()
            if not ref:
                continue
            total += 1
            key = normalize_alias(ref)
            pids = aliases.get(key, [])
            # Self-links: tolerate (page referencing its own alias).
            pids = [p for p in pids if p != self_pid] if self_pid else pids
            # Re-add self_pid only if it was the *only* match — that's a
            # self-link and we tolerate it; we just don't double-count.
            if not pids and self_pid and self_pid in aliases.get(key, []):
                continue
            if not pids:
                orphans.append((page, ref))
            elif len(pids) > 1:
                paths = [pages_by_id[p]["path"] for p in pids if p in pages_by_id]
                ambiguous.append((page, ref, paths))

    if not orphans and not ambiguous:
        notes.append(f"  ✓ all {total} wikilink(s) resolve uniquely")
        return True, notes
    if orphans:
        notes.append(f"  ✗ {len(orphans)} orphan wikilink(s):")
        for page, ref in orphans:
            notes.append(f"    {page.relative_to(VAULT_ROOT)}  [[{ref}]]")
    if ambiguous:
        notes.append(f"  ✗ {len(ambiguous)} ambiguous wikilink(s):")
        for page, ref, paths in ambiguous:
            notes.append(f"    {page.relative_to(VAULT_ROOT)}  [[{ref}]] → {paths}")
    return False, notes


def check_no_source_metadata_headings() -> tuple[bool, list[str]]:
    """Source provenance belongs in paragraph citations, not visible headings."""
    notes: list[str] = []
    bad: list[tuple[Path, str]] = []
    zone_rx = re.compile(
        re.escape(LLM_OPEN) + r"(.*?)" + re.escape(LLM_CLOSE), re.DOTALL
    )
    persrc_rx = re.compile(r"^>\s*###\s+(From\s+src:[^\n]+)\s*$", re.MULTILINE)
    for page in all_wiki_pages():
        text = page.read_text(encoding="utf-8", errors="replace")
        m = zone_rx.search(text)
        if not m:
            continue
        body = m.group(1)
        for hm in persrc_rx.finditer(body):
            bad.append((page, hm.group(1).strip()))
    if not bad:
        notes.append("  ✓ no visible source-metadata headings")
        return True, notes
    notes.append(
        f"  ✗ {len(bad)} source-metadata heading(s) should be paragraph citations instead:"
    )
    for page, heading in bad:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}  ### {heading}")
    notes.append("    → fix: scripts/format-llm-zone.py --all")
    return False, notes


def check_entity_size(threshold: int = 400) -> tuple[bool, list[str]]:
    """Warn (not fail) when an entity exceeds `threshold` lines.
    Large entities are usually candidates for promotion to a Topic with
    sub-entity splits — see scripts/promote.py."""
    notes: list[str] = []
    big: list[tuple[Path, int]] = []
    entities_dir = WIKI_DIR / "entities"
    if not entities_dir.is_dir():
        notes.append("  ✓ no entities/ directory")
        return True, notes
    for page in sorted(entities_dir.rglob("*.md")):
        n = sum(1 for _ in page.read_text(encoding="utf-8", errors="replace").splitlines())
        if n > threshold:
            big.append((page, n))
    if not big:
        notes.append(f"  ✓ all entities ≤ {threshold} lines")
        return True, notes
    notes.append(f"  ⚠ {len(big)} entity/entities exceed {threshold} lines (consider promote.py):")
    for page, n in big:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}  ({n} lines)")
    return True, notes  # warn-only


def check_h1_present() -> tuple[bool, list[str]]:
    """Every wiki page must have a `# <title>` H1 matching its filename stem
    (per schema §10). Codex occasionally drops it; catch it early."""
    notes: list[str] = []
    bad: list[tuple[Path, str]] = []
    h1_rx = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
    for page in all_wiki_pages():
        text = page.read_text(encoding="utf-8", errors="replace")
        m = h1_rx.search(text)
        if not m:
            bad.append((page, "missing '# <title>' heading"))
            continue
        if m.group(1).strip() != page.stem:
            bad.append((page, f"H1 is '{m.group(1).strip()}' but filename stem is '{page.stem}'"))
    if not bad:
        notes.append(f"  ✓ all {len(all_wiki_pages())} pages have H1 matching filename")
        return True, notes
    notes.append(f"  ✗ {len(bad)} page(s) with H1 issues:")
    for page, msg in bad:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}  {msg}")
    return False, notes


def parse_taxonomy(path: Path) -> tuple[dict[str, set[str]] | None, list[str]]:
    r"""Parse `wiki/_taxonomy.md` into {section: set_of_tags}.
    Returns (None, [errors]) on parse failure (caller should treat as
    fail-closed empty allowlist).
    Sections recognized: Domain, Form, Reserved. Any other H2 is ignored.
    Bullet pattern is strict: ``^- `<tag>` ``. A line under a recognized
    H2 that starts with ``- `` but doesn't match the strict pattern is a
    loud failure (catches forgotten backticks)."""
    errors: list[str] = []
    if not path.is_file():
        errors.append(f"taxonomy file missing: {path.relative_to(VAULT_ROOT)}")
        return None, errors

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    sections: dict[str, list[str]] = {"Domain": [], "Form": [], "Reserved": []}
    current: str | None = None
    in_fence = False

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        # Track fenced code blocks; bullets inside are not tags.
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Skip blockquote prefix lines.
        if line.startswith(">"):
            continue
        # H2 detection.
        if line.startswith("## "):
            heading = line[3:].strip()
            current = heading if heading in sections else None
            continue
        if current is None:
            continue
        # Bullet processing inside a recognized section.
        m = TAXONOMY_BULLET_RX.match(line)
        if m:
            sections[current].append(m.group(1))
            continue
        if LOOSE_BULLET_RX.match(line):
            errors.append(
                f"malformed taxonomy bullet at {path.name}:{lineno}: {line!r} "
                f"(under ## {current}; expected `- \\`<tag>\\``)"
            )

    parsed: dict[str, set[str]] = {}
    for section, items in sections.items():
        if not items:
            errors.append(f"taxonomy section ## {section} is empty (allowlist would be empty)")
            continue
        # Reject duplicates within a section.
        seen: set[str] = set()
        for tag in items:
            if tag in seen:
                errors.append(f"duplicate tag in ## {section}: {tag!r}")
            seen.add(tag)
        parsed[section] = seen

    if errors:
        return None, errors

    # Reject cross-section overlap.
    domain = parsed.get("Domain", set())
    form = parsed.get("Form", set())
    reserved = parsed.get("Reserved", set())
    overlap_df = domain & form
    overlap_dr = domain & reserved
    overlap_fr = form & reserved
    for label, overlap in (
        ("Domain∩Form", overlap_df),
        ("Domain∩Reserved", overlap_dr),
        ("Form∩Reserved", overlap_fr),
    ):
        if overlap:
            errors.append(f"taxonomy cross-section overlap {label}: {sorted(overlap)}")

    if errors:
        return None, errors
    return parsed, []


def _has_tags_field(text: str, parsed_fm: dict) -> bool:
    """Detect whether `tags:` is present, via raw text OR parsed YAML.
    Both detectors fire — either finding "present" triggers validation."""
    # Raw-text scan, whitespace-tolerant for `tags  :` etc.
    fm_end = text.find("\n---", 3)
    fm_text = text[3:fm_end] if fm_end != -1 else ""
    raw_present = bool(re.search(r"^tags\s*:", fm_text, re.MULTILINE))
    yaml_present = isinstance(parsed_fm, dict) and "tags" in parsed_fm
    return raw_present or yaml_present


def _tags_style_ok(text: str) -> tuple[bool, str]:
    """Raw-text style check for `tags:`. If flow style (opens with `[`),
    require `]` on the same line. Block style and scalar pass."""
    fm_end = text.find("\n---", 3)
    fm_text = text[3:fm_end] if fm_end != -1 else ""
    for line in fm_text.splitlines():
        m = re.match(r"^tags\s*:\s*(.*)$", line)
        if not m:
            continue
        value = m.group(1)
        # Strip inline comment from the value.
        val = re.sub(r"\s+#.*$", "", value).rstrip()
        if val.startswith("["):
            if "]" not in val:
                return False, "multi-line flow not allowed for `tags:` (open `[` without `]` on same line)"
        return True, ""
    return True, ""  # no tags line found in raw text


def check_tags() -> tuple[bool, list[str]]:
    """Validate `tags:` frontmatter against `wiki/_taxonomy.md`.
    Silent-pass on absent tags (until check_required_tags is enabled
    in step 5 of the migration). When tags are present, enforce:
    style (no multi-line flow), syntax, depth ≤ 2, no duplicates,
    membership in taxonomy, and cardinality (1 Form + 1 primary
    Domain + 0–2 secondary, total 2–4)."""
    notes: list[str] = []
    parsed, parse_errors = parse_taxonomy(TAXONOMY_PATH)
    if parse_errors or parsed is None:
        notes.append("  ✗ taxonomy parse failed — every page with tags will fail-closed:")
        for e in parse_errors:
            notes.append(f"    {e}")
        return False, notes

    domain_tags = parsed["Domain"]
    form_tags = parsed["Form"]
    reserved_tags = parsed["Reserved"]
    allowed = domain_tags | form_tags | reserved_tags

    pages = all_wiki_pages()
    failures: list[str] = []
    warnings: list[str] = []
    checked = 0
    for page in pages:
        text = page.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(page)
        if not _has_tags_field(text, fm):
            continue
        rel = page.relative_to(VAULT_ROOT)

        # YAML parse must have succeeded if `tags:` is in raw text.
        # parse_frontmatter swallows YAMLError → returns {}. If raw
        # has tags but parsed dict doesn't, frontmatter is malformed.
        fm_end = text.find("\n---", 3)
        fm_text = text[3:fm_end] if fm_end != -1 else ""
        raw_has_tags = bool(re.search(r"^tags\s*:", fm_text, re.MULTILINE))
        yaml_has_tags = isinstance(fm, dict) and "tags" in fm
        if raw_has_tags and not yaml_has_tags:
            failures.append(f"    {rel}: frontmatter malformed near `tags:`")
            continue

        # Style check: reject multi-line flow.
        ok_style, msg = _tags_style_ok(text)
        if not ok_style:
            failures.append(f"    {rel}: {msg}")
            continue

        # Field-presence triage on parsed value.
        value = fm.get("tags") if yaml_has_tags else None
        if value is None:
            failures.append(f"    {rel}: tags must be a list, got None")
            continue
        if isinstance(value, str):
            warnings.append(f"    {rel}: tags is a scalar string, coercing to [{value!r}]")
            value = [value]
        if not isinstance(value, list):
            failures.append(f"    {rel}: tags must be a list, got {type(value).__name__}")
            continue

        tags_list: list[str] = value
        # Tag syntax + depth.
        bad_syntax = [t for t in tags_list if not (isinstance(t, str) and TAG_RX.match(t))]
        if bad_syntax:
            failures.append(f"    {rel}: invalid tag syntax: {bad_syntax}")
            continue
        bad_depth = [t for t in tags_list if t.count("/") > 1]
        if bad_depth:
            failures.append(f"    {rel}: tag depth > 2: {bad_depth}")
            continue
        # No duplicates.
        if len(tags_list) != len(set(tags_list)):
            failures.append(f"    {rel}: duplicate tags: {tags_list}")
            continue
        # Membership.
        unknown = [t for t in tags_list if t not in allowed]
        if unknown:
            failures.append(f"    {rel}: tags not in taxonomy: {unknown}")
            continue
        # Cardinality. 1 Form + 1 primary Domain + 0–2 secondary,
        # total 2–4. The "primary Domain" is the first Domain tag in
        # list order; secondary slots accept Domain or Reserved tags.
        forms_in = [t for t in tags_list if t in form_tags]
        domains_in = [t for t in tags_list if t in domain_tags]
        if len(forms_in) != 1:
            failures.append(f"    {rel}: must have exactly 1 Form tag, got {forms_in}")
            continue
        if len(domains_in) < 1:
            failures.append(f"    {rel}: must have at least 1 Domain tag (the primary), got none")
            continue
        if not (2 <= len(tags_list) <= 4):
            failures.append(f"    {rel}: total tag count {len(tags_list)} not in 2–4")
            continue
        checked += 1

    for w in warnings:
        notes.append(f"  ⚠ {w.strip()}")
    if not failures:
        if checked:
            notes.append(f"  ✓ {checked} page(s) with tags pass all rules; rest pass silently")
        else:
            notes.append("  ✓ no pages have tags yet (silent-pass; check_required_tags enabled in step 5)")
        return True, notes
    notes.append(f"  ✗ {len(failures)} page(s) failed tag validation:")
    notes.extend(failures)
    return False, notes


def check_required_tags() -> tuple[bool, list[str]]:
    """13th check: every entity/topic page MUST have a non-empty
    `tags:` list. check_tags() above handles syntax/membership/cardinality
    validation; this check just ensures
    presence."""
    notes: list[str] = []
    bad: list[Path] = []
    for page in all_wiki_pages():
        fm = parse_frontmatter(page)
        if not isinstance(fm, dict):
            bad.append(page)
            continue
        tags = fm.get("tags")
        if not isinstance(tags, list) or not tags:
            bad.append(page)
    if not bad:
        notes.append(f"  ✓ all {len(all_wiki_pages())} pages have tags:")
        return True, notes
    notes.append(f"  ✗ {len(bad)} page(s) missing tags:")
    for p in bad:
        notes.append(f"    {p.relative_to(VAULT_ROOT)}")
    notes.append("    → fix: add tags: [...] to frontmatter")
    return False, notes


def check_frontmatter_sync(cites: dict[Path, set[str]]) -> tuple[bool, list[str]]:
    """Every page's frontmatter `sources:` must equal the unique set of
    [src:<id>] citations actually appearing in its body.

    Fixable by running `scripts/sync-frontmatter.py <page>`.
    """
    notes: list[str] = []
    bad: list[tuple[Path, set[str], set[str]]] = []
    for page in all_wiki_pages():
        fm = parse_frontmatter(page)
        raw = fm.get("sources") or []
        # Coerce a scalar `sources: 01ABC...` (degenerate frontmatter from
        # the LLM or hand-edit) into a single-element set rather than a
        # set of characters — the latter produces a confusing diagnostic
        # that masks the real "this should be a list" issue.
        if isinstance(raw, str):
            raw = [raw]
        elif not isinstance(raw, list):
            raw = []
        declared = set(raw)
        actual = cites.get(page, set())
        if declared != actual:
            bad.append((page, declared, actual))
    if not bad:
        notes.append("  ✓ frontmatter sources: matches body citations on all pages")
        return True, notes
    notes.append(f"  ✗ {len(bad)} page(s) with frontmatter drift:")
    for page, declared, actual in bad:
        missing = actual - declared
        extra = declared - actual
        parts = []
        if missing:
            parts.append(f"missing {sorted(missing)}")
        if extra:
            parts.append(f"extra {sorted(extra)}")
        notes.append(f"    {page.relative_to(VAULT_ROOT)}  {'; '.join(parts)}")
    notes.append("    → fix: scripts/sync-frontmatter.py --all")
    return False, notes


# ─── Image embed checks (#14–#20) ─────────────────────────────────────────
#
# Scope: every check below operates on the LLM-zone of every wiki page.
# Human-zone is user-owned and exempt. See plan/image-ingest-plan.md §7.

IMAGE_EXT_RX = re.compile(r"\.(?:png|jpe?g|gif|webp|svg)$", re.IGNORECASE)
EMBED_RX = re.compile(r"!\[\[([^\]]+)\]\]")
MARKDOWN_IMG_RX = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
# Inline code: 1+ backticks ... matching count of backticks. Triple-tilde
# fences mirror the existing _FENCED_CODE_RX (which only matches triple-
# backtick).
_FENCE_TILDE_RX = re.compile(r"~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RX = re.compile(r"(`+)(?:[^`]|(?!\1)`)*?\1")


def _strip_code(text: str) -> str:
    """Remove fenced + inline code regions before scanning for embeds."""
    text = _FENCED_CODE_RX.sub("", text)
    text = _FENCE_TILDE_RX.sub("", text)
    text = _INLINE_CODE_RX.sub("", text)
    return text


def _llm_zone(text: str) -> str:
    """Extract the body inside <!-- llm-zone --> ... <!-- /llm-zone --> .
    Returns empty string if the page has no llm-zone."""
    i = text.find(LLM_OPEN)
    if i < 0:
        return ""
    j = text.find(LLM_CLOSE, i)
    if j < 0:
        return ""
    return text[i + len(LLM_OPEN):j]


def parse_obsidian_embed(target: str) -> tuple[str, bool]:
    """Parse the inside of `![[...]]`. Returns (path, is_image_embed).

    Strips `#fragment` and `|alias`. is_image_embed is True iff the
    path ends in a recognized image extension. Page transcludes (no
    extension) report False and are handled by check #20.
    """
    raw = target.strip()
    # Cut at first | or # (whichever comes first).
    cut = len(raw)
    for sep in ("|", "#"):
        idx = raw.find(sep)
        if idx >= 0 and idx < cut:
            cut = idx
    path = raw[:cut].strip()
    is_image = bool(IMAGE_EXT_RX.search(path))
    return path, is_image


def _iter_embeds(text: str) -> list[tuple[str, bool]]:
    """Yield (path, is_image) for every Obsidian embed in `text` (after
    stripping code regions)."""
    body = _strip_code(text)
    out: list[tuple[str, bool]] = []
    for m in EMBED_RX.finditer(body):
        out.append(parse_obsidian_embed(m.group(1)))
    return out


def _read_assets_manifest_decorative_set(assets_dir: Path) -> set[str]:
    """Return the set of filenames in this asset dir flagged decorative."""
    manifest = assets_dir / "_manifest.md"
    if not manifest.is_file():
        return set()
    text = manifest.read_text(encoding="utf-8", errors="replace")
    deco: set[str] = set()
    cur_file: str | None = None
    for line in text.splitlines():
        m = re.match(r"^\s+- file:\s*(\S+)\s*$", line)
        if m:
            cur_file = m.group(1)
            continue
        if cur_file and re.match(r"^\s+decorative:\s*true\s*$", line):
            deco.add(cur_file)
    return deco


def check_image_embed_paths() -> tuple[bool, list[str]]:
    """#14 — every image-embed in any page's llm-zone resolves to a real
    file under sources/<asset>.assets/."""
    notes: list[str] = []
    bad: list[tuple[Path, str, str]] = []
    for page in all_wiki_pages():
        zone = _llm_zone(page.read_text(encoding="utf-8", errors="replace"))
        if not zone:
            continue
        for embed_path, is_image in _iter_embeds(zone):
            if not is_image:
                continue
            # Image embeds MUST point under sources/<…>.assets/.
            # Strip a leading "sources/" if present; treat relative as
            # vault-root-relative.
            path = embed_path.lstrip("/")
            if ".." in path.split("/"):
                bad.append((page, embed_path, "path traversal"))
                continue
            if "/.assets/" in path or "/.assets" in path or path.startswith(".assets"):
                # malformed pattern
                bad.append((page, embed_path, "malformed assets path"))
                continue
            if not path.startswith("sources/") or ".assets/" not in path:
                bad.append((page, embed_path, "image embed must point to sources/<asset>.assets/<file>"))
                continue
            target = VAULT_ROOT / path
            if not target.is_file():
                bad.append((page, embed_path, "file not found"))
    if not bad:
        notes.append("  ✓ image embeds resolve (or none present)")
        return True, notes
    notes.append(f"  ✗ {len(bad)} broken image embed(s):")
    for page, embed, reason in bad[:20]:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}: ![[{embed}]] — {reason}")
    return False, notes


def check_image_embed_location() -> tuple[bool, list[str]]:
    """#15 — image asset dirs must belong to a source cited by the page."""
    notes: list[str] = []
    bad: list[tuple[Path, str]] = []
    for page in all_wiki_pages():
        text = page.read_text(encoding="utf-8", errors="replace")
        zone = _llm_zone(text)
        if not zone:
            continue
        # Determine the page's declared sources.
        fm = parse_frontmatter(page)
        page_sources = fm.get("sources") or []
        if isinstance(page_sources, str):
            page_sources = [page_sources]
        elif not isinstance(page_sources, list):
            page_sources = []
        page_sources = [s for s in page_sources if isinstance(s, str)]
        expected_stems = {
            stem for src in set(page_sources)
            for stem in [_source_stem_for_id(src)]
            if stem
        }
        for embed_path, is_image in _iter_embeds(zone):
            if not is_image:
                continue
            # Extract "<source-stem>" from sources/<source-stem>.assets/...
            m = re.match(r"sources/(.+?)\.assets/", embed_path.lstrip("/"))
            if not m:
                continue   # already caught by #14
            asset_source_stem = m.group(1)
            if expected_stems and asset_source_stem not in expected_stems:
                bad.append((page,
                            f"embed asset dir is {asset_source_stem!r}, "
                            f"but page sources map to {sorted(expected_stems)}: "
                            f"{embed_path}"))
            # Zero-source pages are surfaced by citation/frontmatter checks.
    if not bad:
        notes.append("  ✓ image embeds belong to cited page sources")
        return True, notes
    notes.append(f"  ✗ {len(bad)} embed(s) in wrong location / cross-source:")
    for page, msg in bad[:20]:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}: {msg}")
    return False, notes


def _source_stem_for_id(source_id: str) -> str | None:
    """Map a source_id to the sidecar-file stem (without `.md`).

    The `<asset>.assets/` directory is named after the source asset
    file's basename (e.g. `2026-04-29-foo.epub.assets`); the asset
    itself is `2026-04-29-foo.epub`; the sidecar is `2026-04-29-foo.epub.md`.
    Stem = `2026-04-29-foo.epub` (the file name without the trailing `.md`).
    """
    for sidecar in all_sidecars():
        try:
            text = sidecar.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(rf"^source_id:\s*['\"]?{re.escape(source_id)}['\"]?\s*$",
                     text, re.MULTILINE):
            return sidecar.with_suffix("").name
    return None


def check_image_embed_decorative() -> tuple[bool, list[str]]:
    """#16 — embeds must not target a decorative image."""
    notes: list[str] = []
    bad: list[tuple[Path, str]] = []
    deco_cache: dict[Path, set[str]] = {}
    for page in all_wiki_pages():
        zone = _llm_zone(page.read_text(encoding="utf-8", errors="replace"))
        if not zone:
            continue
        for embed_path, is_image in _iter_embeds(zone):
            if not is_image:
                continue
            path = embed_path.lstrip("/")
            target = VAULT_ROOT / path
            if not target.is_file():
                continue   # caught by #14
            assets_dir = target.parent
            if assets_dir not in deco_cache:
                deco_cache[assets_dir] = _read_assets_manifest_decorative_set(assets_dir)
            if target.name in deco_cache[assets_dir]:
                bad.append((page, embed_path))
    if not bad:
        notes.append("  ✓ no decorative images embedded")
        return True, notes
    notes.append(f"  ✗ {len(bad)} embed(s) target decorative images:")
    for page, embed in bad[:20]:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}: ![[{embed}]]")
    return False, notes


def _load_orphan_allowlist() -> set[str]:
    """Intentional-orphan allowlist: VAULT_ROOT-relative asset paths kept on purpose
    (e.g. a source's extracted figures surfaced only in the Source Reader, not
    embedded in the wiki synthesis). One path per line; `#` comments + blanks
    ignored. Lives at sources/.orphan-assets-allow so it travels with the content."""
    f = SOURCES_DIR / ".orphan-assets-allow"
    allow: set[str] = set()
    if f.exists():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                allow.add(line)
    return allow


def check_orphan_assets() -> tuple[bool, list[str]]:
    """#17 (warn-only) — files in sources/<asset>.assets/ not referenced
    by any wiki-page embed. Paths in sources/.orphan-assets-allow are treated
    as intentional and excluded from the warning."""
    notes: list[str] = []
    # Collect all files under any *.assets/ dir.
    asset_files: set[Path] = set()
    for assets_dir in SOURCES_DIR.glob("*.assets"):
        if not assets_dir.is_dir():
            continue
        for f in assets_dir.iterdir():
            if f.is_file() and f.name != "_manifest.md":
                asset_files.add(f.resolve())
    if not asset_files:
        notes.append("  ✓ no asset files (no work to do)")
        return True, notes
    # Collect all embed targets.
    embedded: set[Path] = set()
    for page in all_wiki_pages():
        zone = _llm_zone(page.read_text(encoding="utf-8", errors="replace"))
        if not zone:
            continue
        for embed_path, is_image in _iter_embeds(zone):
            if not is_image:
                continue
            path = embed_path.lstrip("/")
            target = (VAULT_ROOT / path).resolve()
            embedded.add(target)
    orphans = sorted(asset_files - embedded)
    allow = _load_orphan_allowlist()
    orphan_rels = [str(f.relative_to(VAULT_ROOT)) for f in orphans]
    flagged = [r for r in orphan_rels if r not in allow]
    allowed_n = len(orphan_rels) - len(flagged)
    stale = sorted(a for a in allow if a not in orphan_rels)
    if not flagged:
        msg = f"  ✓ all {len(asset_files)} asset file(s) are embedded"
        if allowed_n:
            msg += f" or allowlisted ({allowed_n} intentional orphan(s))"
        notes.append(msg)
    else:
        # Warn-only: don't fail the build.
        suffix = f"; {allowed_n} allowlisted" if allowed_n else ""
        notes.append(f"  ⚠ {len(flagged)} orphan asset file(s) (warn only{suffix}):")
        for r in flagged[:15]:
            notes.append(f"    {r}")
        if len(flagged) > 15:
            notes.append(f"    … and {len(flagged) - 15} more")
    if stale:
        notes.append(f"  ⚠ {len(stale)} stale allowlist entr{'y' if len(stale) == 1 else 'ies'} "
                     f"(no longer orphaned — prune from sources/.orphan-assets-allow): "
                     + ", ".join(stale[:5]) + (" …" if len(stale) > 5 else ""))
    return True, notes


def check_no_external_image_urls() -> tuple[bool, list[str]]:
    """#18 — no `![alt](http…)` or `![[http…]]` inside llm-zone."""
    notes: list[str] = []
    bad: list[tuple[Path, str]] = []
    for page in all_wiki_pages():
        zone = _llm_zone(page.read_text(encoding="utf-8", errors="replace"))
        if not zone:
            continue
        body = _strip_code(zone)
        for m in MARKDOWN_IMG_RX.finditer(body):
            if m.group(1).strip().startswith(("http://", "https://")):
                bad.append((page, f"![](...{m.group(1)[:50]}...)"))
        for m in EMBED_RX.finditer(body):
            t = m.group(1).strip()
            if t.startswith(("http://", "https://")):
                bad.append((page, f"![[{t[:60]}]]"))
    if not bad:
        notes.append("  ✓ no external image URLs in llm-zone")
        return True, notes
    notes.append(f"  ✗ {len(bad)} external image URL(s) in llm-zone:")
    for page, msg in bad[:20]:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}: {msg}")
    return False, notes


def check_no_markdown_image_syntax() -> tuple[bool, list[str]]:
    """#19 — markdown image syntax `![alt](path)` is forbidden in
    llm-zone. All LLM-emitted images must use Obsidian transclude
    syntax `![[...]]`. Bypassing this rule would slip past #14-#17."""
    notes: list[str] = []
    bad: list[tuple[Path, str]] = []
    for page in all_wiki_pages():
        zone = _llm_zone(page.read_text(encoding="utf-8", errors="replace"))
        if not zone:
            continue
        body = _strip_code(zone)
        for m in MARKDOWN_IMG_RX.finditer(body):
            bad.append((page, f"![](...{m.group(1)[:60]}...)"))
    if not bad:
        notes.append("  ✓ no markdown image syntax in llm-zone")
        return True, notes
    notes.append(f"  ✗ {len(bad)} markdown image syntax usage(s) in llm-zone:")
    for page, msg in bad[:20]:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}: {msg}")
    notes.append("    → fix: convert to Obsidian transclude: ![[sources/<…>.assets/<file>]]")
    return False, notes


def check_no_llm_page_transcludes() -> tuple[bool, list[str]]:
    """#20 — page transcludes `![[Some Other Page]]` (no image extension)
    inside llm-zone are forbidden. Wiki pages link to other pages via
    plain `[[wikilinks]]`, not transcludes."""
    notes: list[str] = []
    bad: list[tuple[Path, str]] = []
    for page in all_wiki_pages():
        zone = _llm_zone(page.read_text(encoding="utf-8", errors="replace"))
        if not zone:
            continue
        for embed_path, is_image in _iter_embeds(zone):
            if is_image:
                continue
            bad.append((page, embed_path))
    if not bad:
        notes.append("  ✓ no LLM page transcludes in llm-zone")
        return True, notes
    notes.append(f"  ✗ {len(bad)} page transclude(s) in llm-zone:")
    for page, msg in bad[:20]:
        notes.append(f"    {page.relative_to(VAULT_ROOT)}: ![[{msg}]]")
    notes.append("    → fix: convert to a wikilink [[Page]] (no leading !)")
    return False, notes


def spot_check_picks(n: int = 3) -> list[Path]:
    pages = all_wiki_pages()
    if not pages:
        return []
    rng = random.Random()
    return rng.sample(pages, min(n, len(pages)))


# ─── runner ─────────────────────────────────────────────────────────────────


def run_check(idx: int, total: int, label: str, result: tuple[bool, list[str]]) -> bool:
    ok, notes = result
    marker = "✓" if ok else "✗"
    print(f"[{idx}/{total}] {marker} {label}")
    for n in notes:
        print(n)
    print()
    return ok


def run_check_set(
    title: str,
    checks: list[tuple[str, tuple[bool, list[str]]]],
    *,
    success_message: str = "All checks passed.",
    failure_message: str = "One or more checks FAILED. Review above and fix before next ingest.",
) -> int:
    print(f"{title}\n")
    all_ok = True
    for i, (label, result) in enumerate(checks, 1):
        if not run_check(i, len(checks), label, result):
            all_ok = False
    if all_ok:
        print(success_message)
        return 0
    print(failure_message)
    return 1


def run_lang_lint() -> int:
    """`--profile lang` lint: a small, self-contained gate over `content/lang/`
    (no wiki preconditions). Runs source drift (lang, no media audit),
    duplicate source-ids, open conflicts on lang pages, and citation orphans
    via a lang citation collector. No wikilink/tag/page-id/frontmatter checks —
    lang pages use `[src:]` citations, not `[[wikilinks]]`, and have no alias
    index."""
    print("=== LLM-wiki lint (profile=lang) ===\n")
    sources = collect_source_ids()  # SOURCES_DIR is VAULT_ROOT/sources → lang/sources
    cites = collect_citations_for(lang_pages())
    checks: list[tuple[str, tuple[bool, list[str]]]] = [
        ("source drift (sha256)", check_source_drift(profile="lang")),
        ("no duplicate source_id", check_duplicate_source_ids()),
        ("open conflicts", check_open_conflicts_on_pages(lang_pages())),
        ("citation orphans", check_citation_orphans(cites, sources)),
    ]
    all_ok = True
    for i, (label, result) in enumerate(checks, 1):
        if not run_check(i, len(checks), label, result):
            all_ok = False
    if all_ok:
        print("All lang checks passed.")
        return 0
    print("Lang lint FAILED.")
    return 1


def wiki_gate_specs(
    cites: dict[Path, set[str]],
    sources: dict[str, Path],
) -> dict[str, tuple[str, list[tuple[str, tuple[bool, list[str]]]], str, str]]:
    generic_fail = "One or more checks FAILED. Review above and fix before next ingest."
    return {
        "images": (
            "=== LLM-wiki lint (gate=images) ===",
            [
                ("image embed paths resolve", check_image_embed_paths()),
                ("image embed location", check_image_embed_location()),
                ("decorative not embedded", check_image_embed_decorative()),
                ("orphan assets (warn-only)", check_orphan_assets()),
                ("no external image URLs in llm-zone", check_no_external_image_urls()),
                ("no markdown image syntax in llm-zone", check_no_markdown_image_syntax()),
                ("no LLM page transcludes in llm-zone", check_no_llm_page_transcludes()),
            ],
            "All image checks passed.",
            "Image gate FAILED.",
        ),
        "page-id": (
            "=== LLM-wiki lint (gate=page-id) ===",
            [("page_id present + unique", check_page_id_present())],
            "page_id gate passed.",
            "page_id gate FAILED.",
        ),
        "media-anchors": (
            "=== LLM-wiki lint (gate=media-anchors) ===",
            [
                ("no duplicate source_id", check_duplicate_source_ids()),
                ("timestamp anchors resolve", check_timestamp_anchors()),
                ("card anchors resolve", check_card_anchors()),
                ("frame anchors resolve", check_frame_anchors()),
                ("citation orphans", check_citation_orphans(cites, sources)),
            ],
            "All checks passed.",
            generic_fail,
        ),
        "card-anchors": (
            "=== LLM-wiki lint (gate=card-anchors) ===",
            [
                ("no duplicate source_id", check_duplicate_source_ids()),
                ("card anchors resolve", check_card_anchors()),
                ("citation orphans", check_citation_orphans(cites, sources)),
            ],
            "All checks passed.",
            generic_fail,
        ),
        "frame-anchors": (
            "=== LLM-wiki lint (gate=frame-anchors) ===",
            [
                ("no duplicate source_id", check_duplicate_source_ids()),
                ("timestamp anchors resolve", check_timestamp_anchors()),
                ("frame anchors resolve", check_frame_anchors()),
                ("citation orphans", check_citation_orphans(cites, sources)),
            ],
            "All checks passed.",
            generic_fail,
        ),
        "tags": (
            "=== LLM-wiki lint (gate=tags) ===",
            [
                ("tags valid (when present)", check_tags()),
                ("tags required on every page", check_required_tags()),
            ],
            "All checks passed.",
            generic_fail,
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Weekly no-drift lint for the LLM-wiki.",
    )
    ap.add_argument(
        "--profile", choices=["wiki", "lang"], default="wiki",
        help="lint the wiki (default) or the isolated content/lang/ language subtree.",
    )
    ap.add_argument(
        "--gate",
        choices=["tags", "images", "page-id", "card-anchors",
                 "frame-anchors", "media-anchors"],
        default=None,
        help=(
            "Run only a subset of checks (used as an ingest-time gate). "
            "`tags` runs the tag-validity + tag-required checks. `images` "
            "runs the image-embed checks #14-#20. `page-id` runs the "
            "page_id present+unique check (ingest pre-commit gate). "
            "Default (no flag) runs every check."
        ),
    )
    args = ap.parse_args()

    # Profile-aware root guard (parsed AFTER argparse so --profile is known):
    # lang has no wiki/ directory, so it must NOT require WIKI_DIR.
    if args.profile == "lang":
        # Self-resolve to the lang subtree so a standalone `lint.py --profile
        # lang` is correct without the caller exporting VAULT_CONTENT_DIR.
        # Mirrors ingest's resolve_vault_root: if VAULT_ROOT already ends in
        # `lang` (ingest exports content/lang to its children), use it as-is;
        # else append `lang` to the base content root.
        global VAULT_ROOT, SOURCES_DIR
        if VAULT_ROOT.name != "lang":
            VAULT_ROOT = VAULT_ROOT / "lang"
            SOURCES_DIR = VAULT_ROOT / "sources"
        if not VAULT_ROOT.is_dir():
            print(f"lint: no lang subtree at {VAULT_ROOT}", file=sys.stderr)
            return 2
        return run_lang_lint()
    if not VAULT_ROOT.is_dir():
        print("lint: run from vault root", file=sys.stderr)
        return 2
    if not WIKI_DIR.is_dir():
        print("lint: run from vault root", file=sys.stderr)
        return 2

    # Pre-compute stuff used by multiple checks (wiki path).
    cites = collect_citations()
    sources = collect_source_ids()

    if args.gate:
        title, checks, success_message, failure_message = wiki_gate_specs(cites, sources)[args.gate]
        return run_check_set(
            title,
            checks,
            success_message=success_message,
            failure_message=failure_message,
        )

    print("=== LLM-wiki lint report ===\n")
    checks = [
        ("source drift (sha256)", check_source_drift()),
        ("no duplicate source_id", check_duplicate_source_ids()),
        ("timestamp anchors resolve", check_timestamp_anchors()),
        ("card anchors resolve", check_card_anchors()),
        ("frame anchors resolve", check_frame_anchors()),
        ("open conflicts", check_open_conflicts()),
        ("citation orphans", check_citation_orphans(cites, sources)),
        ("unused sources", check_unused_sources(cites, sources)),
        ("zone markers", check_zone_markers()),
        ("page_id present + unique", check_page_id_present()),
        ("wikilinks resolve", check_wikilinks()),
        ("no source-metadata headings", check_no_source_metadata_headings()),
        ("entity size (warn-only)", check_entity_size()),
        ("H1 matches filename", check_h1_present()),
        ("frontmatter sources: matches body", check_frontmatter_sync(cites)),
        ("tags valid (when present)", check_tags()),
        ("tags required on every page", check_required_tags()),
        ("image embed paths resolve", check_image_embed_paths()),
        ("image embed location", check_image_embed_location()),
        ("decorative not embedded", check_image_embed_decorative()),
        ("orphan assets (warn-only)", check_orphan_assets()),
        ("no external image URLs in llm-zone", check_no_external_image_urls()),
        ("no markdown image syntax in llm-zone", check_no_markdown_image_syntax()),
        ("no LLM page transcludes in llm-zone", check_no_llm_page_transcludes()),
    ]

    all_ok = True
    for i, (label, result) in enumerate(checks, 1):
        if not run_check(i, len(checks), label, result):
            all_ok = False

    if args.gate is None:
        picks = spot_check_picks()
        if picks:
            print("Random spot-check — open these and verify claims against their sources:")
            for p in picks:
                print(f"  {p.relative_to(VAULT_ROOT)}")
            print()

    if all_ok:
        print("All checks passed.")
        return 0
    print("One or more checks FAILED. Review above and fix before next ingest.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
