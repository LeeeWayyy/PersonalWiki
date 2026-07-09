#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Auto-link known aliases in wiki page llm-zone bodies.

Reads `wiki/.alias-index.json` and scans each given page's
`<!-- llm-zone -->` block for unlinked occurrences of any known alias.
Inserts a `[[wikilink]]` on every match (not just first mention) so the
graph stays dense for Obsidian's backlink/graph view.

Matching rules:
- **Longest-match-first.** Aliases are sorted by length DESC. When the
  vault registers both `ATP合成酶` and `ATP` (or any compound + a
  substring), the longer match wins.
- **ASCII boundary check** (only for aliases containing ASCII alnum):
  the alias is rejected if the character immediately before or after
  it is itself ASCII alnum or `_`. This protects `ATP` from matching
  inside the ASCII identifier `ATPase`. CJK characters are NOT treated
  as ASCII boundaries, so `ATP` still matches in `ATP的浓度` (the
  particle `的` is a non-ASCII letter).
- **Pure-CJK aliases skip the boundary check.** Chinese has no
  whitespace boundaries; particles like `的`, `在`, `与` are alnum in
  Unicode but always sit next to content words. Requiring a boundary
  would block `[[线粒体]]` from matching in any natural sentence. The
  trade-off: a pure-CJK alias *will* link inside a longer compound if
  no longer alias is registered (e.g. `线粒体某词` → `[[线粒体]]某词`).
  The mitigation is to register the longer compound as its own alias
  on the relevant page; longest-match-first then picks the right one.
- **Case-insensitive matching for ASCII-bearing aliases.** `Mitochondria`
  catches both `Mitochondria` and `mitochondria` in prose; the wikilink
  preserves the surface form (`[[线粒体|mitochondria]]`).
- **Minimum length 2.** Single-character aliases are too noisy.

Skipped regions (no matching attempted inside):
- Frontmatter (only the llm-zone body is touched)
- Existing wikilinks `[[…]]`
- Citations and other bracketed structures `[src:ID]`, `[^footnote]`
- Standard markdown links `[text](url)` (whole bracket+url span)
- Fenced code blocks ``` ``` … ``` ```
- Inline code `` `…` ``

Skipped matches:
- Self-references (a page's own alias).
- Aliases whose normalized form is ambiguous (>1 page in the index) —
  the alias index already fail-closes on these; the autolinker mirrors
  that policy rather than guessing.

Output form: if the matched alias equals the target's filename stem,
emit `[[alias]]`. Otherwise emit `[[stem|alias]]` so the displayed
text matches what the prose said while the link resolves to the
canonical file.

Usage:
    scripts/autolink.py <path>...
    scripts/autolink.py --all
    scripts/autolink.py --dry-run <path>...
    scripts/autolink.py --check        # exit 1 if any page has unlinked aliases
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _util import default_vault_root

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
WIKI_DIR = VAULT_ROOT / "wiki"
INDEX_PATH = WIKI_DIR / ".alias-index.json"

# Capture llm-zone open/body/close so we only rewrite inside.
ZONE_RX = re.compile(
    r"(<!-- llm-zone -->)(.*?)(<!-- /llm-zone -->)", re.DOTALL
)


def all_wiki_pages() -> list[Path]:
    """Inclusion list — entities/ + topics/ only.

    `wiki/_index/` (MOC files) is intentionally excluded: MOCs have
    deterministic, generator-emitted wikilinks; running autolink on
    the member list would double-link or rewrite the canonical
    `[[stem|H1]]` form. Do NOT add `_index/` here."""
    pages: list[Path] = []
    for sub in ("entities", "topics"):
        d = WIKI_DIR / sub
        if d.is_dir():
            pages.extend(sorted(d.rglob("*.md")))
    return pages


def load_index() -> dict:
    if not INDEX_PATH.exists():
        sys.exit("autolink: alias index missing — run scripts/alias-index.py build")
    idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    # Domain-aware autolink (added in alias-index v2) requires per-page
    # `domains` to be present. A v1 index would have no `domains` field
    # for any page → every match would look "no Domain" → fallback rule
    # would re-introduce the cross-domain false-positives this fix
    # exists to prevent. Refuse to load and force a rebuild.
    version = idx.get("version", 1)
    if version < 2:
        sys.exit(
            f"autolink: alias index is v{version}, but this script requires v2+ "
            "(domain-aware filter). Rebuild with: scripts/alias-index.py build"
        )
    return idx


_HAS_ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")


def _is_ascii_word_char(c: str) -> bool:
    """True if `c` is an ASCII letter, digit, or underscore — i.e. would
    extend an ASCII word like `ATPase`. This deliberately treats CJK
    characters as NOT word chars, because in Chinese prose an ASCII
    alias is typically followed by a particle (`的`, `的浓度`) or a
    compound modifier; the ASCII-only definition lets the alias still
    match in those normal sentences.

    Trade-off: `ATP合成酶` with only `ATP` registered will match `ATP`
    here (since `合` is non-ASCII, not a boundary). The user's preferred
    behavior is to register `ATP合成酶` as its own alias so longest-
    match-first wins. Leaving the alias gap means the partial link is
    chosen over no link, which is the lesser of two evils."""
    if not c:
        return False
    return c.isascii() and (c.isalnum() or c == "_")


def _alias_needs_boundary_check(alias: str) -> bool:
    """An alias needs ASCII-style word-boundary checking only if it contains
    an ASCII letter or digit. Protects mixed-script aliases like `ATP`
    from matching inside ASCII identifiers like `ATPase`. Pure-CJK
    aliases skip the check — see the note in `_is_ascii_word_char`."""
    return bool(_HAS_ASCII_ALNUM.search(alias))


def _autolink_body(
    body: str,
    entries: list[tuple[str, str, str, list[str]]],
    self_pid: str | None,
    self_domains: list[str],
) -> str:
    """Walk `body` left-to-right, skipping no-link regions, and replace
    longest-matching aliases with wikilinks. `entries` is a list of
    (alias, target_pid, target_stem, target_domains) sorted by alias
    length DESC.

    Domain filter (alias-index v2): if both source and target have ≥1
    Domain tag and the sets are disjoint, the link is skipped. This
    prevents cross-domain bare-token false positives (e.g., the
    economic entity `分工` getting autolinked into biology prose that
    happened to use the same Chinese word). When either side has no
    Domain (rare after the tagging migration completed), fall back to
    the unfiltered behavior — the alternative would silently break
    legitimate links during migration windows."""
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        # Fenced code block: a line beginning (after optional Obsidian
        # `>` callout prefix(es)) with a run of >=3 backticks OR >=3
        # tildes opens a fence; the next line beginning (after the same
        # optional prefix) with a run of the SAME character of length
        # >= opening run closes it. The whole vault's llm-zone is
        # wrapped in a `> [!AI]` callout, so fences typically appear as
        # `> ```` / `> ~~~` rather than bare.
        is_line_start = i == 0 or body[i - 1] == "\n"
        if is_line_start:
            # Consume the line's quote prefix (`>` chars + whitespace),
            # counting `>` chars to record the callout depth. A close
            # at a different depth (e.g. `> > ``` ` nested inside a
            # depth-1 callout) is content, not a real closer.
            scan = i
            open_quote_depth = 0
            while scan < n and (body[scan] == ">" or body[scan] == " " or body[scan] == "\t"):
                if body[scan] == ">":
                    open_quote_depth += 1
                scan += 1
            if scan < n and (body[scan] == "`" or body[scan] == "~"):
                fence_char = body[scan]
                run = scan
                while run < n and body[run] == fence_char:
                    run += 1
                fence_len = run - scan
                if fence_len >= 3:
                    # Find the closing fence: must have the SAME quote
                    # depth, followed by a same-char run of length >=
                    # fence_len, followed ONLY by whitespace until EOL.
                    # CommonMark forbids info-strings on close; we
                    # enforce that, plus the depth-match safeguard so a
                    # `> > ``` ` nested-quote line cannot prematurely
                    # close a depth-1 fence.
                    search = run
                    end = n
                    while search < n:
                        ln_start = body.find("\n", search)
                        if ln_start == -1:
                            break
                        line_begin = ln_start + 1
                        k = line_begin
                        close_quote_depth = 0
                        while k < n and (body[k] == ">" or body[k] == " " or body[k] == "\t"):
                            if body[k] == ">":
                                close_quote_depth += 1
                            k += 1
                        if close_quote_depth != open_quote_depth:
                            search = ln_start + 1
                            continue
                        f_start = k
                        while k < n and body[k] == fence_char:
                            k += 1
                        if k - f_start >= fence_len:
                            # Ensure only whitespace remains until EOL.
                            tail = k
                            while tail < n and (body[tail] == " " or body[tail] == "\t"):
                                tail += 1
                            if tail >= n or body[tail] == "\n":
                                end = k
                                break
                        search = ln_start + 1
                    out.append(body[i:end])
                    i = end
                    continue
        # Inline code: a run of N backticks (`, ``, ```, …) opens a span
        # that closes at the next run of EXACTLY N backticks. Markdown
        # uses N>1 to embed a literal backtick (`` ``ATP`` `` displays
        # `ATP` with a backtick visible). Skip the whole span so aliases
        # inside code examples are not rewritten.
        if body[i] == "`":
            run_end = i
            while run_end < n and body[run_end] == "`":
                run_end += 1
            run = body[i:run_end]
            close = body.find(run, run_end)
            # Be careful: `find(run)` could match a longer-run boundary;
            # require the matched closing run to NOT be immediately
            # followed by another backtick (otherwise we found an N-run
            # inside an (N+1)-run, which doesn't actually close us).
            while close != -1 and close + len(run) < n and body[close + len(run)] == "`":
                close = body.find(run, close + len(run))
            end = n if close == -1 else close + len(run)
            out.append(body[i:end])
            i = end
            continue
        # Wikilink `[[…]]`.
        if body.startswith("[[", i):
            end = body.find("]]", i + 2)
            end = n if end == -1 else end + 2
            out.append(body[i:end])
            i = end
            continue
        # Any other bracketed structure: citations `[src:ID#anchor]`,
        # standard markdown links `[text](url)`, footnote refs `[^id]`.
        # Skip the bracket span so we don't corrupt citation IDs or
        # mangle markdown link text. We do NOT enter and substitute
        # within these — too many ways to break the surrounding syntax.
        if body[i] == "[":
            end = body.find("]", i + 1)
            if end == -1:
                # Unclosed bracket — treat as literal character.
                out.append(body[i])
                i += 1
                continue
            # If immediately followed by `(`, it's a markdown link
            # `[text](url)` — also skip the URL portion. URLs commonly
            # contain `)` (Wikipedia: `…_(disambiguator)/…`), so balance
            # parens rather than taking the first one.
            if end + 1 < n and body[end + 1] == "(":
                depth = 1
                p = end + 2
                while p < n and depth > 0:
                    if body[p] == "(":
                        depth += 1
                    elif body[p] == ")":
                        depth -= 1
                    p += 1
                if depth == 0:
                    end = p - 1  # position of the matching `)`
                # else: unmatched paren — leave `end` at the original `]`
            end += 1
            out.append(body[i:end])
            i = end
            continue

        # Try aliases longest-first. For ASCII-bearing aliases we match
        # case-insensitively so `Mitochondria` (alias) catches both
        # `Mitochondria` and `mitochondria` in prose. The DISPLAY text
        # in the wikilink uses what was actually written.
        matched: tuple[str, str, str, str] | None = None  # (alias, pid, stem, surface)
        for alias, target_pid, target_stem, target_domains in entries:
            la = len(alias)
            surface = body[i:i + la]
            if len(surface) != la:
                continue
            ascii_alias = _alias_needs_boundary_check(alias)
            if ascii_alias:
                if surface.casefold() != alias.casefold():
                    continue
            else:
                if surface != alias:
                    continue
            if ascii_alias:
                # ASCII-only word-boundary check on either side.
                before = body[i - 1] if i > 0 else ""
                after = body[i + la] if i + la < n else ""
                if _is_ascii_word_char(before) or _is_ascii_word_char(after):
                    continue
            # Don't link a page to itself.
            if self_pid and target_pid == self_pid:
                continue
            # Domain-overlap filter. Both pages have domains AND no
            # overlap → skip (cross-domain false positive). One side
            # missing domains → fall through and link (migration
            # safety; lint enforces ≥1 Domain via cardinality so this
            # branch is rare in practice).
            if self_domains and target_domains and not (set(self_domains) & set(target_domains)):
                continue
            matched = (alias, target_pid, target_stem, surface)
            break

        if matched is None:
            out.append(body[i])
            i += 1
            continue

        alias, _, target_stem, surface = matched
        # Use what the prose wrote (`surface`) as the display form, so
        # case is preserved (`mitochondria` vs `Mitochondria`).
        if surface == target_stem:
            out.append(f"[[{surface}]]")
        else:
            out.append(f"[[{target_stem}|{surface}]]")
        i += len(alias)

    return "".join(out)


def _build_entries(idx: dict) -> list[tuple[str, str, str, list[str]]]:
    """Return (alias, target_pid, target_stem, target_domains) sorted
    longest-first. Skips ambiguous aliases (>1 page) and aliases shorter
    than 2 chars.

    `target_domains` is the per-page Domain list from the v2 alias
    index, used by the domain-overlap filter in `_autolink_body`."""
    aliases = idx.get("aliases", {})
    pages = idx.get("pages", {})
    # Build pid -> set of normalized aliases that resolve uniquely
    unambiguous_norm: set[str] = {
        norm for norm, pids in aliases.items() if len(pids) == 1
    }
    out: list[tuple[str, str, str, list[str]]] = []
    for pid, info in pages.items():
        target_stem = Path(info["path"]).stem
        target_domains = info.get("domains") or []
        for alias in info.get("aliases", []):
            if not isinstance(alias, str) or len(alias) < 2:
                continue
            # Use the same normalization the index does (NFKC + casefold +
            # whitespace squeeze). We don't have unicodedata here, but
            # since we only check whether the alias's *normalized* form
            # is unambiguous, simulate via index lookup: any alias whose
            # canonical form maps to >1 pid is ambiguous.
            # (alias-index.py stores the post-norm key; we re-look up.)
            import unicodedata
            key = unicodedata.normalize("NFKC", alias).casefold()
            key = re.sub(r"\s+", " ", key).strip()
            if key not in unambiguous_norm:
                continue
            out.append((alias, pid, target_stem, list(target_domains)))
    out.sort(key=lambda e: (-len(e[0]), e[0]))
    return out


def autolink_page(
    path: Path,
    idx: dict,
    dry_run: bool = False,
    entries: list[tuple[str, str, str, list[str]]] | None = None,
) -> tuple[bool, int]:
    """Returns (changed, n_links_added).

    Pass `entries` (from `_build_entries(idx)`) when invoking in a loop;
    rebuilding it per page is O(N log N) on alias count and dominates the
    runtime of an `--all` sweep at scale."""
    text = path.read_text(encoding="utf-8")
    rel = str(path.relative_to(VAULT_ROOT))
    self_pid = next(
        (pid for pid, info in idx.get("pages", {}).items() if info.get("path") == rel),
        None,
    )
    self_domains: list[str] = []
    if self_pid is not None:
        self_domains = list(idx.get("pages", {}).get(self_pid, {}).get("domains") or [])
    if entries is None:
        entries = _build_entries(idx)

    added = 0

    def replace_zone(m: re.Match) -> str:
        nonlocal added
        zone_open, body, zone_close = m.group(1), m.group(2), m.group(3)
        before_count = body.count("[[")
        new_body = _autolink_body(body, entries, self_pid, self_domains)
        after_count = new_body.count("[[")
        added += after_count - before_count
        return zone_open + new_body + zone_close

    new_text = ZONE_RX.sub(replace_zone, text)
    if new_text == text:
        return False, 0
    if not dry_run:
        path.write_text(new_text, encoding="utf-8")
    return True, added


def cmd_run(args: argparse.Namespace) -> int:
    idx = load_index()
    targets = all_wiki_pages() if args.all else [Path(p).resolve() for p in args.paths]
    targets = [p for p in targets if p.is_file()]
    if not targets:
        print("autolink: nothing to do", file=sys.stderr)
        return 0

    entries = _build_entries(idx)  # build once, reuse across all pages
    total_changed = 0
    total_added = 0
    for path in targets:
        changed, added = autolink_page(
            path, idx, dry_run=args.dry_run, entries=entries
        )
        if changed:
            total_changed += 1
            total_added += added
            mark = "would update" if args.dry_run else "updated"
            print(f"  {mark} {path.relative_to(VAULT_ROOT)}: +{added} link(s)")
    if total_changed == 0:
        print("autolink: no pages needed updates")
    else:
        verb = "would have added" if args.dry_run else "added"
        print(f"autolink: {verb} {total_added} link(s) across {total_changed} page(s)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Exit 1 if any page has unlinked aliases. Used by lint."""
    idx = load_index()
    entries = _build_entries(idx)  # build once
    bad: list[tuple[Path, int]] = []
    for path in all_wiki_pages():
        _, added = autolink_page(path, idx, dry_run=True, entries=entries)
        if added:
            bad.append((path, added))
    if not bad:
        print(f"  ✓ all pages link every entity mention")
        return 0
    print(f"  ⚠ {len(bad)} page(s) with unlinked aliases (would auto-link):")
    for p, n in bad:
        print(f"    {p.relative_to(VAULT_ROOT)}  (+{n})")
    print("    → fix: scripts/autolink.py --all")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="wiki pages to process")
    ap.add_argument("--all", action="store_true", help="process every wiki page")
    ap.add_argument("--dry-run", action="store_true", help="show what would change")
    ap.add_argument("--check", action="store_true",
                    help="report missing links and exit 1 (for lint)")
    args = ap.parse_args()

    if args.check:
        return cmd_check(args)
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
