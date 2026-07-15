"""Shared media identity + head resolution (expansion-plan §8.0).

ONE resolver, imported by `media-identity.py` and future card/frame paths. The
contract is **die-loud**: a tracked media sidecar that can't be
classified, or a `supersedes:` pointer outside the identity set, is a hard error
— never silently skipped (no-silent-drift).

This module is *imported*, not run, so it carries no PEP-723 block; it uses
`yaml`, which the importing uv-script's venv provides.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path

import yaml

# Re-export the zero-dep shared helpers so the media front-door scripts can keep
# importing them from here (their single import); the canonical defs live in _util.py.
from _util import (  # noqa: F401  (re-exported for consumers)
    _ULID_ALPHABET, die, git_tracked, hhmmss, iso_now, new_ulid, progress,
    sha256_of, today,
)

_ULID = r"[0-9A-Z]{26}"
# The bare-ULID branch needs an end-of-token guard: without it a corrupt
# `01AAA…<27+ chars>` would match its first 26 chars as a "valid" ULID and
# silently resolve a wrong target (no-silent-drift). The `[[…]]` branch is
# delimiter-bounded already.
_SUPERSEDES_RX = re.compile(rf"\[\[({_ULID})\]\]|(?<![0-9A-Z])({_ULID})(?![0-9A-Z])")

# Sidecar globs, by canonical-artifact kind. `pathlib.Path.glob` does NOT
# brace-expand, so these are collected as separate explicit patterns (§8.0).
# `*.cards.md.md` (image_note, §8.2) is included now that image_note is implemented
# and always writes `media.identity_basis` (image_post_id|image_bundle) — so
# identity_key() classifies every card sidecar. (A hand-made card sidecar lacking
# identity_basis would die-loud here, which is the intended no-silent-drift behavior.)
_SIDECAR_GLOBS = ("*.transcript.md.md", "*.cards.md.md")


class ResolverError(Exception):
    """Raised on any die-loud condition; callers map it to their own die()."""


def parse_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # tracked-but-missing-on-disk sidecar (e.g. deleted without staging): return {} so
        # resolve_head raises a clean ResolverError (caller → die()), not a raw traceback.
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}


def identity_key(fm: dict) -> tuple[str, tuple]:
    """Return ``(identity_basis, key-tuple)`` for a media sidecar's frontmatter.

    Platform-dispatched (schema §7.2). A `platform: youtube` sidecar with no
    `identity_basis` is read as `youtube_video_id` (legacy backcompat so the
    strict resolver never bricks committed Phase-2 sources). A missing/unknown
    platform or basis, or a basis missing its required fields, is a ResolverError.
    """
    # MAINTENANCE: this basis enum is load-bearing — resolve_head parses EVERY
    # tracked media sidecar, so an unrecognized basis dies for ALL ingests, not
    # just the target's. Extend the enum (and a legacy-compat rule if older
    # sidecars predate the field) ATOMICALLY when adding a platform.
    media = fm.get("media")
    if not isinstance(media, dict):
        raise ResolverError("sidecar has no `media:` block")
    platform = media.get("platform")
    basis = media.get("identity_basis")
    if not basis and platform == "youtube":
        basis = "youtube_video_id"  # legacy compat

    def need(*fields: str) -> tuple:
        vals = tuple(media.get(f) for f in fields)
        if any(v in (None, "") for v in vals):
            raise ResolverError(
                f"identity_basis={basis!r} requires media.{'/'.join(fields)} (got {vals!r})"
            )
        return vals

    if basis == "youtube_video_id":
        return (basis, need("video_id"))
    if basis == "feed_guid":
        return (basis, need("feed_url", "episode_guid"))
    if basis == "feed_enclosure":
        return (basis, need("feed_url", "enclosure_url"))
    if basis == "feed_title_published":
        feed_url, published = need("feed_url", "published")
        title = fm.get("title")  # top-level, not under media: — consistent with every source type
        if not title:
            raise ResolverError("identity_basis=feed_title_published requires a title")
        return (basis, (feed_url, title, published))
    if basis == "image_post_id":
        return (basis, need("platform", "post_id"))
    if basis == "image_bundle":
        return (basis, need("platform", "image_bundle_sha256"))
    raise ResolverError(f"unrecognized/missing media.identity_basis (platform={platform!r})")


def _tracked_under(sources: Path) -> set[str]:
    """One `git ls-files` for all of sources/ (avoids an O(n) git subprocess per
    sidecar). Returns repo-relative paths (relative to the repo root = cwd for our
    callers)."""
    r = subprocess.run(["git", "ls-files", "-z", "--", str(sources)],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        return set()
    return {p for p in r.stdout.decode("utf-8", "replace").split("\0") if p}


def assets_dir_tracked_files(root: Path, assets_rel: str) -> list[str]:
    """git-TRACKED files under an `<asset>.assets/` dir (root-relative paths, recursive).
    Used to decide a source still has committed assets even if every YAML frame-signal was
    hand-edited away — so the drift guard can't be disabled by deleting frontmatter alone.
    cwd-independent (`git -C root`)."""
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", assets_rel],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        return []
    return [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]


def find_media_sidecars(sources: Path) -> list[Path]:
    """All TRACKED media sidecars, enumerated DIRECTLY from `git ls-files` (NOT a filesystem
    glob ∩ tracked). Enumerating the tracked set means a tracked-but-deleted-from-worktree
    sidecar is STILL returned — parse_frontmatter then hits OSError → {} → resolve_head
    raises a loud ResolverError, instead of the resolver silently behaving as though no
    prior source exists (the no-silent-drift hole a filesystem glob would leave: the glob
    never yields a missing file). Scoped to DIRECT children of sources/ with a sidecar
    suffix (matching the old non-recursive globs). Returns repo/cwd-relative paths."""
    src_rel = os.path.relpath(str(sources))
    suffixes = tuple(g.lstrip("*") for g in _SIDECAR_GLOBS)  # ".transcript.md.md", ".cards.md.md"
    found = [Path(rel) for rel in _tracked_under(sources)
             if rel.endswith(suffixes) and os.path.dirname(rel) == src_rel]
    return sorted(found)


def resolve_head(sources: Path, target: tuple[str, tuple]) -> tuple[Path, dict] | None:
    """Resolve the unique head of the supersession chain whose identity == ``target``.

    Returns ``(sidecar_path, frontmatter)`` or ``None`` when no sidecar matches.
    Dies (ResolverError) on: an unparseable/unclassifiable tracked sidecar, a
    sidecar missing `source_id`, a `supersedes:` pointer outside the identity set,
    or anything other than exactly one head. Compares **like-basis to like-basis**
    (the full ``(basis, key)`` tuple), so a podcast sidecar is never judged by
    YouTube fields.

    `media.identity_aliases[]` (schema §7.2) is a RESERVED field for a future
    basis-upgrade reconciliation command and is NOT honored here yet: matching a
    candidate by an alias is incompatible with the "supersedes points within the
    matched set" rule for *cross-basis* chains (the superseded weaker-basis source
    wouldn't be a candidate under the stronger target), so it's deferred until the
    reconciliation flow is designed. Today a basis upgrade is caught by the
    cross-basis duplicate guard in the podcast front door (die, never silent dup).
    """
    candidates: dict[str, tuple[Path, dict]] = {}  # source_id -> (path, fm)
    for sc in find_media_sidecars(sources):
        fm = parse_frontmatter(sc)
        if not fm:
            raise ResolverError(f"tracked media sidecar failed to parse / is empty: {sc}")
        try:
            key = identity_key(fm)
        except ResolverError as exc:
            raise ResolverError(f"{sc}: {exc}") from exc
        if key != target:
            continue
        sid = fm.get("source_id")
        if not sid:
            raise ResolverError(f"tracked media sidecar lacks source_id: {sc}")
        if sid in candidates:
            # two matching sidecars share an immutable source_id — collapsing them in this
            # dict would silently reuse whichever sorts last; die instead (no-silent-drift).
            raise ResolverError(f"duplicate source_id {sid} across {candidates[sid][0]} and {sc}")
        candidates[sid] = (sc, fm)
    if not candidates:
        return None
    superseded: set[str] = set()
    for sid, (sc, fm) in candidates.items():
        sup = fm.get("supersedes")
        if not sup:
            continue
        m = _SUPERSEDES_RX.search(str(sup))
        if not m:
            raise ResolverError(f"malformed supersedes in {sc}: {sup!r}")
        prior = m.group(1) or m.group(2)
        if prior not in candidates:
            raise ResolverError(
                f"supersedes points outside the identity set ({sc} -> {prior})"
            )
        superseded.add(prior)
    heads = [c for sid, c in candidates.items() if sid not in superseded]
    if len(heads) != 1:
        raise ResolverError(
            f"ambiguous supersession chain for {target!r}: {len(heads)} heads"
        )
    return heads[0]


def superseded_ids(sources: Path) -> set[str]:
    """The set of committed source_ids that some OTHER tracked media sidecar supersedes
    (i.e. NOT chain heads). Shared so cross-basis dedup scans can skip superseded sources
    — once a supersede flow (--reocr/--retranscribe/add-frames) commits a new source over
    an old one, the OLD one must drop out of dedup or it would falsely look like a same-
    bundle duplicate of the new head. Uses the same _SUPERSEDES_RX as resolve_head."""
    # map every classifiable tracked sidecar: source_id -> (identity_key, supersedes)
    by_id: dict[str, tuple] = {}
    for sc in find_media_sidecars(sources):
        fm = parse_frontmatter(sc)
        sid = fm.get("source_id")
        if not sid:
            continue
        try:
            key = identity_key(fm)
        except ResolverError:
            continue  # unclassifiable — can't validly participate in a supersession chain
        by_id[sid] = (key, fm.get("supersedes"))
    out: set[str] = set()
    for key, sup in by_id.values():
        if not sup:
            continue
        m = _SUPERSEDES_RX.search(str(sup))
        if not m:
            continue
        prior = m.group(1) or m.group(2)
        # Honor ONLY same-identity supersession (the resolve_head invariant): a corrupt or
        # cross-identity `supersedes:` pointer must NOT silently hide `prior` from dedup.
        if prior in by_id and by_id[prior][0] == key:
            out.add(prior)
    return out


_CARD_HEADING_LINE_RX = re.compile(r"^## card \d+\s*$")


def render_image_note_md(items: list[dict]) -> str:
    """Canonical image_note `.cards.md` text — the SINGLE renderer, shared by ingest
    (which WRITES it from the OCR cards) and the completeness check (which RE-DERIVES it
    from the committed `.cards.json` rows and compares). Each item needs `index` and its
    OCR text under `ocr_text` (ingest cards) or `text` (audit rows). A body line that
    mimics a `## card N` heading is indented one space so untrusted OCR can't inject a
    phantom heading. Keeping one renderer is what makes the text/order binding checkable
    without the two copies drifting."""
    out: list[str] = []
    for c in sorted(items, key=lambda c: c["index"]):
        out.append(f"## card {c['index'] + 1}")
        body = (c.get("ocr_text") if c.get("ocr_text") is not None else c.get("text", "")) or ""
        body = body.strip("\n")
        if body:
            out.append("\n".join(
                (" " + ln) if _CARD_HEADING_LINE_RX.match(ln) else ln
                for ln in body.split("\n")))
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def evidence_completeness_notes(sidecar: Path, fm: dict, root: Path) -> list[str]:
    """Shared no-silent-drift COMPLETENESS check over media.evidence_artifacts[] + the
    audit json — used by BOTH lint (weekly) and ingest reuse (front door) so the two
    cannot diverge. Returns a list of problem strings (empty = ok). It does NOT re-hash
    the artifact FILES (each caller already re-hashes every listed entry); it enforces
    that the LISTING is complete and self-consistent:
      - the required audit + bundle roles are present;
      - the audit (cards_json/frames_json) + bundle `from:` paths are the ones DERIVED
        from THIS sidecar's name — a miswired sidecar can't point evidence at another
        source's audit and have us validate the wrong files;
      - the dedup scalar (image_note) is present and agrees with the committed bundle;
      - every audited image row is bound — by path AND by sha256 — to an image evidence
        entry (so .cards.json/frames.json can't claim a different hash/order than the
        committed images);
      - image_note rows carry heading_anchor == card-{index+1};
      - the committed image_note `.cards.md` equals the render of its `.cards.json` rows
        (the order ↔ heading ↔ text binding).
    `sidecar` is the sidecar path (<asset>.md.md); `root` is the directory the sidecar
    paths are relative to (VAULT_ROOT for lint, the cwd for ingest)."""
    notes: list[str] = []
    media = fm.get("media") or {}
    arts = media.get("evidence_artifacts") or []
    if not isinstance(arts, list):
        return ["media.evidence_artifacts is not a list"]
    by_role: dict = {}
    ev_paths: set = set()
    ev_sha: dict = {}
    for a in arts:
        if isinstance(a, dict):
            by_role.setdefault(a.get("role"), []).append(a)
            if a.get("path"):
                ev_paths.add(a["path"])
                if a.get("role") in ("card_image", "frame_image") and a.get("sha256"):
                    ev_sha[a["path"]] = a["sha256"]
    is_img = fm.get("origin_type") == "image_note"
    audit_role, img_role, bundle_role = (
        ("cards_json", "card_image", "image_bundle") if is_img
        else ("frames_json", "frame_image", "frame_bundle"))
    # Expected audit + canonical paths DERIVED FROM THIS SIDECAR'S NAME, so a miswired
    # `path:`/`from:` that points evidence at ANOTHER source under sources/ fails instead
    # of validating unrelated files.
    scname = Path(sidecar).name
    if is_img:
        stem = scname[:-len(".cards.md.md")] if scname.endswith(".cards.md.md") else scname[:-3]
        expected_audit = f"sources/{stem}.cards.json"
        expected_canonical = f"sources/{stem}.cards.md"
    else:
        stem = scname[:-len(".transcript.md.md")] if scname.endswith(".transcript.md.md") else scname[:-3]
        expected_audit = f"sources/{stem}.transcript.md.assets/frames.json"
        expected_canonical = None
    # frames sources also commit the transcript JSON as evidence — require it so the
    # transcript.json drift guard can't be removed alongside the scalar (no-silent-drift).
    required_roles = [audit_role, bundle_role] + ([] if is_img else ["transcript_json"])
    for needed in required_roles:
        if needed not in by_role:
            notes.append(f"evidence_artifacts missing required '{needed}' role")
    for entry in by_role.get(audit_role, []):
        if entry.get("path") != expected_audit:
            notes.append(f"'{audit_role}' evidence path {entry.get('path')!r} is not this sidecar's {expected_audit!r}")
    for entry in by_role.get(bundle_role, []):
        if entry.get("from") != expected_audit:
            notes.append(f"'{bundle_role}' bundle from {entry.get('from')!r} is not this sidecar's {expected_audit!r}")
    if not is_img:
        # bind the transcript_json role to THIS sidecar's sibling .transcript.json — else a
        # hand-edit could repoint it at another file and lint would hash the wrong JSON.
        expected_tj = f"sources/{stem}.transcript.json"
        for entry in by_role.get("transcript_json", []):
            if entry.get("path") != expected_tj:
                notes.append(f"'transcript_json' evidence path {entry.get('path')!r} is not this sidecar's {expected_tj!r}")
    scalar = media.get("image_bundle_sha256")
    if is_img and not scalar:
        notes.append("image_note sidecar missing media.image_bundle_sha256 (required)")
    elif scalar and by_role.get(bundle_role) and scalar != by_role[bundle_role][0].get("sha256"):
        notes.append(f"media.image_bundle_sha256 disagrees with the '{bundle_role}' evidence bundle")
    ajson = root / expected_audit
    if ajson.is_file():
        try:
            rows = json.loads(ajson.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = None
        if not isinstance(rows, list):
            notes.append(f"audit json {expected_audit} is not a list")
        else:
            str_fields = ("image_sha256", "image_path") + (("heading_anchor", "text") if is_img else ())
            malformed = False
            indices: list[int] = []
            for i, r in enumerate(rows):
                if (not isinstance(r, dict) or not isinstance(r.get("index"), int)
                        or isinstance(r.get("index"), bool)
                        or any(not isinstance(r.get(f), str) for f in str_fields)):
                    notes.append(f"audit row {i} missing/malformed index|{'|'.join(str_fields)}")
                    malformed = True
                    continue
                indices.append(r["index"])
                ip_unsafe = ".." in r["image_path"].split("/") or not r["image_path"].startswith("sources/")
                if not ip_unsafe:
                    # also symlink-resolve (parity with lint's _under_sources / ingest's
                    # _evidence_path_ok) so a committed symlink can't escape sources/
                    try:
                        (root / r["image_path"]).resolve().relative_to((root / "sources").resolve())
                    except (ValueError, OSError):
                        ip_unsafe = True
                if ip_unsafe:
                    notes.append(f"audit row {i} image_path is unsafe: {r['image_path']!r}")
                    malformed = True
                    continue
                if not is_img and (
                        isinstance(r.get("frame_id"), bool) or not isinstance(r.get("frame_id"), int)
                        or isinstance(r.get("timecode"), bool) or not isinstance(r.get("timecode"), (int, float))
                        or not math.isfinite(r["timecode"]) or not isinstance(r.get("filename"), str)):
                    # a frame row must pin frame_id (int) + timecode (number) + filename, so
                    # #frame-N still proves WHICH frame/time it names (not just an ordinal).
                    notes.append(f"frame audit row {i} missing/malformed frame_id|timecode|filename")
                    malformed = True
                    continue
                if is_img and r["heading_anchor"] != f"card-{r['index'] + 1}":
                    notes.append(f"audit row {i} heading_anchor {r['heading_anchor']!r} != card-{r['index'] + 1}")
                if r["image_path"] not in ev_paths:
                    notes.append(f"committed image {r['image_path']} has no '{img_role}' evidence entry")
                elif ev_sha.get(r["image_path"]) != r["image_sha256"]:
                    notes.append(f"audit image_sha256 for {r['image_path']} disagrees with its '{img_role}' evidence entry")
            if not malformed:
                # index shape + count invariants (parity with the ingest gate): image_note
                # rows are 0-based contiguous; frame rows are unique 1-based ordinals; the
                # media.card_count/frame_count scalar matches the committed row count.
                expected_seq = list(range(len(rows))) if is_img else list(range(1, len(rows) + 1))
                if sorted(indices) != expected_seq:
                    notes.append(f"audit indices are not the expected "
                                 f"{'0-based' if is_img else 'unique 1-based'} sequence: {sorted(indices)}")
                if not is_img:  # frame_id is the audit/sort key — it must be unique per frame
                    fids = [r["frame_id"] for r in rows]
                    if len(set(fids)) != len(fids):
                        notes.append(f"frame audit has duplicate frame_id(s): {sorted(fids)}")
                # the count scalar is REQUIRED (schema + frame capability key) — deleting it
                # must fail, not silently pass. Must be an int equal to the committed row count.
                count = media.get("card_count") if is_img else media.get("frame_count")
                if isinstance(count, bool) or not isinstance(count, int) or count != len(rows):
                    notes.append(f"media.{'card' if is_img else 'frame'}_count {count!r} "
                                 f"is not an int equal to the audit row count {len(rows)}")
            if is_img and not malformed and expected_canonical:
                # the committed canonical .cards.md must equal the render of its rows
                canonical = root / expected_canonical
                if canonical.is_file() and canonical.read_text(encoding="utf-8") != render_image_note_md(rows):
                    notes.append("committed .cards.md disagrees with the render of .cards.json rows "
                                 "(order/heading/text binding broken)")
    # No EXTRA committed files in the asset dir: every file there must be an evidenced image
    # (ev_paths) or the audit json itself (frames.json lives in the dir). An unlisted
    # committed asset would otherwise drift completely unguarded.
    assets_rel = f"sources/{stem}.cards.md.assets" if is_img else f"sources/{stem}.transcript.md.assets"
    allowed = ev_paths | {expected_audit}
    # Enumerate git-TRACKED files under the asset dir (not the filesystem): every committed
    # file there must be an evidenced image or the audit json. Using git (`-C root` so it's
    # cwd-independent) is recursive AND only sees COMMITTED files — so a nested file or a
    # tracked dotfile is caught, while untracked working-tree junk (.DS_Store) is ignored.
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", assets_rel],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode == 0:
        for rel in r.stdout.decode("utf-8", "replace").split("\0"):
            if rel and rel not in allowed:
                notes.append(f"unlisted committed asset {rel} (absent from evidence_artifacts/audit "
                             f"— it would drift unguarded)")
    return notes
