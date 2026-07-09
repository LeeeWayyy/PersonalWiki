#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""
One-shot backfill: assign `tags:` to existing wiki pages that don't
have them yet. Reads page digest (path, page_id, H1, aliases, body
snippet), bundles N pages per LLM call, parses JSON output, surgically
inserts/replaces `tags:` line via line-edit (NOT PyYAML round-trip).

Run from vault root:
    scripts/backfill-tags.py --dry-run             # default: print proposals
    scripts/backfill-tags.py --apply               # write high+medium-conf
    scripts/backfill-tags.py --apply --include-low # apply low-conf too
    scripts/backfill-tags.py --page-id <ULID>      # process single page
    scripts/backfill-tags.py --batch-size 5        # smaller batches

Confidence handling on `--apply`:
  high   — applied without prompt
  medium — applied, flagged in stdout summary
  low    — skipped (use --include-low to apply); re-run with
           --page-id <ULID> and full body context for low-confidence
           cases

Frontmatter writing:
  - If `tags:` already exists → replace via _upsert_line (block-style
    continuation-aware, matches sync-frontmatter.py:102 contract).
  - Else → insert immediately after `aliases:` block via
    _insert_after_aliases helper (handles flow style, block style,
    EOF-after-aliases edge case; preserves trailing blank/comment
    separators that belong to the next key).
  - Never round-trip through PyYAML (preserves human formatting).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import llm_client
import yaml

from _util import default_vault_root, split_frontmatter

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
WIKI_DIR = VAULT_ROOT / "wiki"
TAXONOMY_PATH = WIKI_DIR / "_taxonomy.md"

H1_RX = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
LLM_OPEN = "<!-- llm-zone -->"
LLM_CLOSE = "<!-- /llm-zone -->"
CITATION_RX = re.compile(r"\[src:[A-Z0-9]{26}(?:#[^\]]*)?\]")

DIGEST_BODY_CHARS = 600
LOW_CONF_BODY_CAP = 8000  # for low-confidence single-page retries

def parse_fm(text: str) -> dict:
    s = split_frontmatter(text)
    if not s:
        return {}
    try:
        out = yaml.safe_load(s[1])
    except yaml.YAMLError:
        return {}
    return out if isinstance(out, dict) else {}


def _upsert_line(fm_body: str, key: str, new_value: str) -> tuple[str, bool]:
    """Replace the `key:` entry with a single `key: <new_value>` line.
    Handles both flow-style (`key: foo`) and block-style:
        key:
          - a
          - b
    by skipping any continuation lines (indented or blank/comment) that
    belong to the entry. Returns (new_body, found) — second value is
    True when the key was located and rewritten (regardless of whether
    the rewrite changed bytes), False when the key was absent.

    NOTE: this is a near-twin of `_upsert_line` in
    `scripts/sync-frontmatter.py`. Same mechanics (continuation skip,
    first-line replacement), but a DIFFERENT second-return-value
    contract:
      - backfill-tags.py (here): returns `found`. Caller uses it to
        decide between replace-in-place vs. insert-after-aliases.
      - sync-frontmatter.py: returns `changed` (i.e. `new_body !=
        fm_body`). Caller uses it as a "is there a write to commit?"
        signal that gates last_ingested bumping and the file write.
    Keep mechanics in sync; do NOT merge the second return-value
    semantics — each caller relies on its own.

    NOTE: not multi-line-flow-aware. The plan-enforced contract is
    single-line flow only; the lint tag-gate rejects multi-line flow
    before it ever reaches this helper."""
    new_line = f"{key}: {new_value}"
    lines = fm_body.split("\n")
    out: list[str] = []
    i = 0
    found = False
    key_rx = re.compile(rf"^{re.escape(key)}\s*:\s*(.*)$")
    while i < len(lines):
        line = lines[i]
        m = key_rx.match(line) if not found else None
        if m:
            value = m.group(1)
            val_no_comment = re.sub(r"\s+#.*$", "", value).rstrip()
            out.append(new_line)
            found = True
            i += 1
            if val_no_comment.strip():
                continue  # single-line entry
            # Block style: skip continuation.
            while i < len(lines):
                cont = lines[i]
                if (cont == ""
                        or re.match(r"^\s*#", cont)
                        or re.match(r"^[ \t]+\S", cont)
                        or re.match(r"^-(\s|$)", cont)):
                    i += 1
                    continue
                break
            continue
        out.append(line)
        i += 1
    if found:
        new_body = "\n".join(out)
        return new_body, True
    # Not present — caller decides where to insert.
    return fm_body, False


def _insert_after_aliases(fm_body: str, new_line: str) -> tuple[str, bool]:
    r"""Insert `new_line` immediately after the `aliases:` value (and
    its block continuation, if any). Falls back to appending at end of
    fm_body if `aliases:` not found.

    Algorithm (per plan §7 "_insert_after_key" worked examples):
      1. Locate `^aliases:` line.
      2. End-of-value detection:
         - flow with `[` and `]` on same line → end = anchor index.
         - flow with `[` no `]` → consume until `]`.
         - no `[` → consume block-style continuations matching either
           `^\s+-` or `^\s+\S`.
      3. Insert at end-of-value + 1. Do NOT skip blank/comment lines
         that follow — those belong to the NEXT key (YAML convention).
    """
    lines = fm_body.split("\n")
    aliases_rx = re.compile(r"^aliases\s*:\s*(.*)$")
    anchor = -1
    for i, line in enumerate(lines):
        if aliases_rx.match(line):
            anchor = i
            break
    if anchor < 0:
        # No anchor; append at end.
        sep = "" if fm_body.endswith("\n") else "\n"
        return fm_body + sep + new_line, True

    # End-of-value detection.
    m = aliases_rx.match(lines[anchor])
    value = m.group(1) if m else ""
    val_no_comment = re.sub(r"\s+#.*$", "", value).rstrip()
    end = anchor
    if "[" in val_no_comment:
        if "]" in val_no_comment:
            end = anchor  # single-line flow
        else:
            # Multi-line flow — pathological; consume until ].
            j = anchor + 1
            while j < len(lines):
                if "]" in lines[j]:
                    end = j
                    break
                j += 1
            else:
                end = len(lines) - 1
    elif val_no_comment:
        # Scalar on same line.
        end = anchor
    else:
        # Block style: walk forward through continuations.
        j = anchor + 1
        while j < len(lines):
            cont = lines[j]
            if re.match(r"^\s+-", cont) or re.match(r"^\s+\S", cont):
                end = j
                j += 1
                continue
            break

    # Insert at end+1.
    out = lines[: end + 1] + [new_line] + lines[end + 1 :]
    new_body = "\n".join(out)
    return new_body, True


def write_tags(text: str, tags: list[str]) -> tuple[str, bool]:
    """Apply tag list to a page text. Returns (new_text, changed).
    Replaces an existing `tags:` line, OR inserts after `aliases:`."""
    s = split_frontmatter(text)
    if not s:
        raise ValueError("page has no frontmatter")
    _before, fm_body, after = s
    # `_upsert_line` builds the full `key: value` line internally, so
    # pass the bare flow-list value here (NOT the full `tags: [...]`
    # line); otherwise the result would be `tags: tags: [...]`.
    # `_insert_after_aliases` takes a full line.
    flow_value = f"[{', '.join(tags)}]"
    new_line = f"tags: {flow_value}"
    new_fm, found = _upsert_line(fm_body, "tags", flow_value)
    if not found:
        new_fm, _ = _insert_after_aliases(fm_body, new_line)
    new_text = f"---\n{new_fm}\n---\n{after}"
    return new_text, new_text != text


# ─── digest builder ─────────────────────────────────────────────────────────


def llm_zone_body(text: str) -> str:
    a = text.find(LLM_OPEN)
    b = text.find(LLM_CLOSE)
    if a < 0 or b < 0 or b <= a:
        return ""
    body = text[a + len(LLM_OPEN) : b]
    # Strip citations to make the snippet more topic-dense.
    return CITATION_RX.sub("", body).strip()


def build_digest(path: Path, body_cap: int = DIGEST_BODY_CHARS) -> dict:
    text = path.read_text(encoding="utf-8")
    fm = parse_fm(text)
    h1_m = H1_RX.search(text)
    h1 = h1_m.group(1).strip() if h1_m else path.stem
    body = llm_zone_body(text)
    if len(body) > body_cap:
        body = body[: body_cap // 2] + "\n[…truncated…]\n" + body[-body_cap // 2 :]
    aliases = fm.get("aliases") if isinstance(fm, dict) else None
    if isinstance(aliases, str):
        aliases = [aliases]
    elif not isinstance(aliases, list):
        aliases = []
    return {
        "path": str(path.relative_to(VAULT_ROOT)),
        "page_id": fm.get("page_id") if isinstance(fm, dict) else None,
        "type": fm.get("type") if isinstance(fm, dict) else None,
        "h1": h1,
        "aliases": aliases,
        "body": body,
    }


# ─── LLM call ───────────────────────────────────────────────────────────────


PROMPT_TEMPLATE = """\
You are tagging existing wiki pages with controlled-vocabulary tags.

The TAXONOMY below defines the valid tags. You MUST pick from it; do
not invent tags.

Cardinality rule for every page:
- Exactly 1 Form tag (from the `## Form` section).
- Exactly 1 primary Domain tag (from the `## Domain` section).
- Optionally 0–2 secondary tags (Domain or Reserved).
- Total 2–4 tags.

Output format: a single JSON object on its own (no prose, no fences),
mapping each input page's relative path to a tag assignment object:

{{
  "wiki/entities/example.md": {{
    "tags": ["concept", "biology/cell"],
    "confidence": "high",
    "rationale": "Single-sentence justification, ≤ 100 chars."
  }},
  ...
}}

`confidence` must be one of `high`, `medium`, `low`:
- `high`: clear single-domain page; tagging is unambiguous.
- `medium`: defensible but a reviewer should spot-check.
- `low`: ambiguous, may span multiple domains, or page content is too
  thin to confidently tag — flag for human review.

==== TAXONOMY ====

{taxonomy}

==== PAGES TO TAG ({n} pages) ====

{pages}

Emit the JSON object now. Nothing else."""


def render_page_block(d: dict) -> str:
    aliases = ", ".join(d["aliases"]) if d["aliases"] else "<none>"
    return (
        f"### {d['path']}\n"
        f"- type: {d['type']}\n"
        f"- page_id: {d['page_id']}\n"
        f"- H1: {d['h1']}\n"
        f"- aliases: {aliases}\n"
        f"- body excerpt:\n```\n{d['body']}\n```"
    )


def call_llm(prompt: str) -> str:
    """Send prompt through the shared LLM client."""
    try:
        out = llm_client.complete(prompt, timeout=180)
    except Exception as e:
        sys.exit(f"backfill-tags: LLM call failed: {e}")
    if out is None:
        sys.exit("backfill-tags: LLM call failed: no local or API provider is configured")
    return out


def parse_llm_output(out: str) -> dict:
    """Pull the JSON object out of the LLM's response. Tolerate
    leading/trailing whitespace or stray markdown fences."""
    out = out.strip()
    # Strip ```json ... ``` fences if present.
    if out.startswith("```"):
        out = re.sub(r"^```(?:json)?\s*", "", out)
        out = re.sub(r"\s*```$", "", out)
    # Find first `{` and last `}`.
    a = out.find("{")
    b = out.rfind("}")
    if a < 0 or b < 0 or b <= a:
        sys.exit(f"backfill-tags: no JSON object in LLM output:\n{out[:500]}")
    try:
        return json.loads(out[a : b + 1])
    except json.JSONDecodeError as e:
        sys.exit(f"backfill-tags: invalid JSON: {e}\n{out[:500]}")


# ─── main ───────────────────────────────────────────────────────────────────


def needs_tags(path: Path) -> bool:
    fm = parse_fm(path.read_text(encoding="utf-8"))
    if not isinstance(fm, dict):
        return False
    if "tags" not in fm:
        return True
    val = fm.get("tags")
    if not isinstance(val, list) or not val:
        return True
    return False


def all_pages() -> list[Path]:
    out: list[Path] = []
    for sub in ("entities", "topics"):
        d = WIKI_DIR / sub
        if d.is_dir():
            out.extend(sorted(d.rglob("*.md")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="default: print proposals, do not write")
    ap.add_argument("--apply", dest="dry_run", action="store_false",
                    help="write proposals (high+medium confidence by default)")
    ap.add_argument("--include-low", action="store_true",
                    help="when --apply, also write low-confidence proposals")
    ap.add_argument(
        "--page-id",
        help=(
            "process a single page identified by page_id (ULID); "
            "uses larger body cap. Used for low-confidence retries."
        ),
    )
    ap.add_argument("--batch-size", type=int, default=10,
                    help="pages per LLM call (default: 10)")
    args = ap.parse_args()

    if not TAXONOMY_PATH.is_file():
        sys.exit(f"backfill-tags: taxonomy missing: {TAXONOMY_PATH.relative_to(VAULT_ROOT)}")
    taxonomy_text = TAXONOMY_PATH.read_text(encoding="utf-8")

    if args.page_id:
        target = None
        for p in all_pages():
            fm = parse_fm(p.read_text(encoding="utf-8"))
            if isinstance(fm, dict) and fm.get("page_id") == args.page_id:
                target = p
                break
        if target is None:
            sys.exit(f"backfill-tags: page_id not found: {args.page_id}")
        pages_to_process = [target]
        body_cap = LOW_CONF_BODY_CAP
    else:
        pages_to_process = [p for p in all_pages() if needs_tags(p)]
        body_cap = DIGEST_BODY_CHARS

    if not pages_to_process:
        print("backfill-tags: nothing to do — all pages already tagged.")
        return 0

    print(f"backfill-tags: {len(pages_to_process)} page(s) need tagging")

    # Build digests.
    digests = [build_digest(p, body_cap=body_cap) for p in pages_to_process]

    # Batch and call LLM.
    results: dict[str, dict] = {}
    batch_size = max(1, args.batch_size if not args.page_id else 1)
    for batch_idx, start in enumerate(range(0, len(digests), batch_size), 1):
        batch = digests[start : start + batch_size]
        pages_blob = "\n\n".join(render_page_block(d) for d in batch)
        prompt = PROMPT_TEMPLATE.format(
            taxonomy=taxonomy_text, n=len(batch), pages=pages_blob
        )
        n_batches = (len(digests) + batch_size - 1) // batch_size
        print(f"  batch {batch_idx}/{n_batches} ({len(batch)} pages)... ", end="", flush=True)
        out = call_llm(prompt)
        parsed = parse_llm_output(out)
        results.update(parsed)
        print("ok")

    # Print proposals.
    print()
    bucket: dict[str, list[tuple[str, dict]]] = {"high": [], "medium": [], "low": []}
    missing: list[str] = []
    for d in digests:
        rel = d["path"]
        if rel not in results:
            missing.append(rel)
            continue
        r = results[rel]
        conf = (r.get("confidence") or "low").lower()
        if conf not in bucket:
            conf = "low"
        bucket[conf].append((rel, r))

    for conf in ("high", "medium", "low"):
        if not bucket[conf]:
            continue
        print(f"=== {conf.upper()} confidence ({len(bucket[conf])} page(s)) ===")
        for rel, r in bucket[conf]:
            tags = r.get("tags") or []
            rat = r.get("rationale") or ""
            print(f"  {rel}")
            print(f"    tags: {tags}")
            print(f"    rationale: {rat}")
        print()

    if missing:
        print(f"⚠ {len(missing)} page(s) missing from LLM response:")
        for m in missing:
            print(f"  {m}")
        print()

    if args.dry_run:
        print("backfill-tags: --dry-run, no files written. Re-run with --apply to write.")
        return 0

    # Apply phase.
    to_apply: list[tuple[str, list[str]]] = []
    for conf in ("high", "medium"):
        for rel, r in bucket[conf]:
            tags = r.get("tags")
            if isinstance(tags, list) and 2 <= len(tags) <= 4:
                to_apply.append((rel, tags))
    if args.include_low:
        for rel, r in bucket["low"]:
            tags = r.get("tags")
            if isinstance(tags, list) and 2 <= len(tags) <= 4:
                to_apply.append((rel, tags))
    elif bucket["low"]:
        print(f"Skipping {len(bucket['low'])} low-confidence page(s).")
        print("  Re-run with --apply --include-low, OR run individually:")
        print("    scripts/backfill-tags.py --apply --page-id <ULID>")
        print()

    written = 0
    for rel, tags in to_apply:
        path = VAULT_ROOT / rel
        text = path.read_text(encoding="utf-8")
        new_text, changed = write_tags(text, tags)
        if changed:
            path.write_text(new_text, encoding="utf-8")
            written += 1
            print(f"  wrote {rel}")
        else:
            print(f"  unchanged {rel}")
    print()
    print(f"backfill-tags: wrote {written} of {len(to_apply)} page(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
