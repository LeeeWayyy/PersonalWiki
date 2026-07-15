"""Shared helpers for derived-view generators (mindmap, mocs, language pages).

Dependency-bearing (needs PyYAML for frontmatter), so it lives here rather
than in the stdlib-only `_util.py`. These are the genuinely reusable pieces a
derived-view generator needs: frontmatter parsing, source/log discovery, the
LLM call + JSON extraction, an LLM-result cache, atomic writes, and the
human-zone preservation contract.

All functions are PARAMETERIZED — they take the cache dir, prompt version,
char limit, etc. as arguments instead of closing over module globals — so a
caller picks its own cache namespace (e.g. `lang/.wiki/lang-cache/`) and never
inherits another generator's cache or prompt version.

Language pages, mindmaps, and MOCs consume these helpers directly; JS reader
code remains a separate runtime boundary.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

import llm_client
from _util import LOG_LINE_RX, chapter_order_from_lines

# Re-export the shared `.wiki/log.md` parser for derived-view scripts that
# already import it from this module.


def parse_frontmatter(text: str) -> dict | None:
    """Return parsed YAML frontmatter dict, or None if absent / malformed."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    try:
        fm = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def find_sources(sources_dir: Path) -> dict[str, dict]:
    """source_id → {title, asset, sha256} for every sidecar in `sources_dir`."""
    out: dict[str, dict] = {}
    if not sources_dir.is_dir():
        return out
    for sc in sorted(sources_dir.glob("*.md")):
        if sc.name == "README.md":
            continue
        fm = parse_frontmatter(sc.read_text(encoding="utf-8", errors="replace"))
        if not fm or not isinstance(fm.get("source_id"), str):
            continue
        asset = sc.with_suffix("")  # strip the .md sidecar suffix
        out[fm["source_id"]] = {
            "title": str(fm.get("title") or asset.name),
            "asset": asset,
            "sha256": str(fm.get("sha256") or ""),
        }
    return out


def clean_title(title: str) -> str:
    """Strip the ingest date prefix and asset extension for display."""
    s = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", title.strip())
    s = re.sub(r"\.(epub|mobi|azw3?|pdf|html?|txt|md|transcript\.json)$", "", s,
               flags=re.IGNORECASE)
    return s.strip() or title


def source_slug(title: str, source_id: str) -> str:
    """Display-title → slug, collapsing whitespace/commas only (matches the
    mindmap slug). NOT filesystem-safe on its own — wrap with fs_safe_slug."""
    s = re.sub(r"[\s,]+", "-", clean_title(title)).strip("-")
    return s or source_id


_FS_RESERVED_RX = re.compile(r'[/\\:#?*"<>|\s\x00-\x1f]+')


def fs_safe_slug(s: str, fallback: str = "untitled") -> str:
    """Make `s` safe as a single path component: strip FS-reserved chars and
    leading dots/dashes, bound length to 80, guarantee non-empty. Used for
    lang page filenames (source slug and chapter slug)."""
    out = _FS_RESERVED_RX.sub("-", s).strip().strip(". -")
    out = re.sub(r"-{2,}", "-", out)
    if len(out) > 80:
        out = out[:80].strip("-")
    return out or fallback


def render_human_zone(existing_text: str | None, zone_label: str) -> str:
    """Extract the verbatim human-zone from an existing file, else emit a
    default placeholder. `zone_label` names the tool-owned zone (e.g.
    "study-zone") so the placeholder's 'do not edit' line is accurate."""
    default = (
        "<!-- human-zone -->\n"
        "_Optional human commentary; preserved verbatim across regenerations._\n"
        f"_Do not edit {zone_label} or frontmatter — they're tool-owned._\n"
        "<!-- /human-zone -->\n"
    )
    if not existing_text:
        return default
    m = re.search(
        r"<!-- human-zone -->.*?<!-- /human-zone -->\n?", existing_text, re.DOTALL
    )
    if not m:
        return default
    return m.group(0) if m.group(0).endswith("\n") else m.group(0) + "\n"


def existing_last_generated(existing_text: str | None) -> str | None:
    """Pull `last_generated:` out of an existing page's frontmatter (str or
    datetime.date), so a no-op re-run keeps the prior date and stable bytes."""
    if not existing_text:
        return None
    fm = parse_frontmatter(existing_text)
    if not fm:
        return None
    val = fm.get("last_generated")
    if isinstance(val, str):
        return val
    if isinstance(val, datetime.date):
        return val.isoformat()
    return None


def extract_source_text(extract_py: Path, asset: Path, limit: int,
                        section: str | None = None) -> str:
    """Run extract.py on `asset` (via uv), optionally filtered to one section.
    `section` must be a bare regex string (anchored by the caller, e.g.
    `^第1章$`); it is passed as a single argv element, NOT shell-quoted."""
    argv = ["uv", "run", str(extract_py), str(asset), "--limit", str(limit)]
    if section is not None:
        argv += ["--section", section]
    res = subprocess.run(argv, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"extract.py failed for {asset.name}: {res.stderr[-500:]}")
    return res.stdout


def list_sections(extract_py: Path, asset: Path) -> list[str]:
    """Section (`## ` heading) titles for an asset, in document order. Empty
    list means no headings → caller treats the asset as one whole unit."""
    res = subprocess.run(
        ["uv", "run", str(extract_py), str(asset), "--list-sections"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"extract.py --list-sections failed for {asset.name}: {res.stderr[-500:]}")
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


def call_llm(prompt: str, timeout_s: int) -> str:
    """Run the shared LLM client."""
    try:
        out = llm_client.complete(prompt, timeout=timeout_s)
    except Exception as exc:
        raise RuntimeError(f"LLM failed: {exc}") from exc
    if out is None:
        raise RuntimeError("LLM failed: no local or API provider is configured")
    return out


def extract_json(raw: str) -> dict:
    """Pull a JSON object out of an LLM response that may carry fences or
    preamble. Tries a fenced block, then a balanced-brace scan from the
    first `{`."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    start = raw.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            c = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(raw[start:i + 1])
                    break
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("no parseable JSON object in LLM output")


def _execution_cache_key() -> str:
    payload = json.dumps(
        llm_client.execution_identity(), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def cache_path(cache_dir: Path, prompt_version: str, source_id: str, sha: str,
               suffix: str) -> Path:
    """Cache filename keyed by source, prompt, and producing LLM identity."""
    return cache_dir / (
        f"{source_id}.{(sha or '0')[:12]}.{suffix}.{prompt_version}."
        f"{_execution_cache_key()}.json"
    )


def load_cache(cache_dir: Path, prompt_version: str, source_id: str, sha: str,
               suffix: str) -> dict | None:
    p = cache_path(cache_dir, prompt_version, source_id, sha, suffix)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_cache(cache_dir: Path, prompt_version: str, source_id: str, sha: str,
               data: dict, suffix: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path(cache_dir, prompt_version, source_id, sha, suffix).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def flat_cache_path(cache_dir: Path, prompt_version: str, source_id: str,
                    sha: str) -> Path:
    """Cache filename for single-result derived views.

    This preserves the historical mindmap cache shape:
    `<source_id>.<sha12>.<prompt_version>.json`.
    """
    return cache_dir / (
        f"{source_id}.{(sha or '0')[:12]}.{prompt_version}."
        f"{_execution_cache_key()}.json"
    )


def load_flat_cache(cache_dir: Path, prompt_version: str, source_id: str,
                    sha: str) -> dict | None:
    p = flat_cache_path(cache_dir, prompt_version, source_id, sha)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_flat_cache(cache_dir: Path, prompt_version: str, source_id: str,
                    sha: str, data: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    flat_cache_path(cache_dir, prompt_version, source_id, sha).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def atomic_write(path: Path, content: str, dry_run: bool) -> bool:
    """Write `content` to `path` via temp+rename. Skip if unchanged. Returns
    True if a write happened (or would, under dry_run)."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return True


def demo() -> None:
    """Self-check: the pure, dependency-light helpers. Run: python3 derived_lib.py"""
    assert parse_frontmatter("---\ntype: map\n---\nbody") == {"type": "map"}
    assert parse_frontmatter("no frontmatter") is None

    lines = [
        "2026-06-01 01ABC00000000000000000000A#第1章 はじめに  pages: x",
        "2026-06-01 01ABC00000000000000000000A#第2章  pages: y",
        "2026-06-01 01ABC00000000000000000000A#第1章 はじめに  pages: z",  # dup
        "2026-06-01 01ZZZ0000000000000000000ZZ#別  pages: w",            # other src
        "2026-06-01 01ABC00000000000000000000A#Front pages: a history  pages: q",
        "garbage line",
    ]
    assert chapter_order_from_lines(lines, "01ABC00000000000000000000A") == [
        "第1章 はじめに", "第2章", "Front pages: a history",
    ]
    # label-less line is dropped
    assert chapter_order_from_lines(
        ["2026 01ABC00000000000000000000A  pages: x"], "01ABC00000000000000000000A"
    ) == []

    assert clean_title("2026-06-01-Foo, Bar.epub") == "Foo, Bar"
    assert source_slug("2026-06-01-Foo, Bar.epub", "X") == "Foo-Bar"
    assert fs_safe_slug("第1章: a/b\\c#d") == "第1章-a-b-c-d"
    assert fs_safe_slug("...") == "untitled"
    assert fs_safe_slug("", fallback="01") == "01"

    assert extract_json('prefix {"a": 1} suffix') == {"a": 1}
    assert extract_json('```json\n{"b": 2}\n```') == {"b": 2}

    path = flat_cache_path(Path(".cache"), "v1", "S", "abcdef1234567890")
    assert path.name.startswith("S.abcdef123456.v1.") and path.suffix == ".json"

    z = render_human_zone(None, "study-zone")
    assert "study-zone" in z and z.startswith("<!-- human-zone -->")
    kept = "<!-- human-zone -->\nmy notes\n<!-- /human-zone -->\n"
    assert render_human_zone("junk\n" + kept + "more", "study-zone") == kept

    assert existing_last_generated("---\nlast_generated: 2026-06-01\n---\n") == "2026-06-01"
    assert existing_last_generated("no fm") is None

    print("derived_lib demo: OK")


if __name__ == "__main__":
    demo()
