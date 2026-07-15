#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""
Parse a unified diff and emit path information.

Sole owner of diff-header parsing logic for the LLM-wiki. Used by
both ingest.sh's scope-check and the auto-retry path. Unifying the
parser eliminates the duplication that historically caused parser
bugs to recur (rounds 3-5 of the design review).

Usage:
    scripts/diff-paths.py <diff-file> [--mode=scope|--mode=modify-targets]

Modes:
  --mode=scope            (default) Validate every path-bearing line:
                          forbidden prefix, .md extension, no `..`
                          traversal, malformed-header detection,
                          unified-without-git-header rejection. Prints
                          any bad paths; exits 1 if any are bad, 0
                          otherwise.

  --mode=modify-targets   Print modify-target paths only (one per
                          line, sorted, deduped). Skips:
                            - new-file diffs (Mode A; handled via
                              git-apply stderr parsing instead).
                            - pure-rename blocks without content
                              changes (no `@@` hunk).
                          For rename-with-edits emits BOTH source
                          and destination paths (source for git-apply
                          error matching, destination for
                          CANDIDATES_FILE matching). Exits 0 on clean
                          parse, 2 on file-not-found.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PATH_RX = re.compile(r"^wiki/(entities|topics)/[^/].*\.md$")
TAXONOMY_PATH = "wiki/_taxonomy.md"
QUOTED_HEADER_RX = re.compile(r'^diff --git "a/([^"]+)" "b/([^"]+)"$')
MODE_DECL_RX = re.compile(r"^(?:old mode|new mode|new file mode|deleted file mode) ([0-9]{6})$")
INDEX_MODE_RX = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+ ([0-9]{6})$")
REGULAR_GIT_MODES = {"100644", "100755"}


def parse_header(line: str) -> tuple[str, str] | None:
    """Return (old_path, new_path) from a `diff --git` header line.
    Handles three forms:
      - bare:    diff --git a/<old> b/<new>
      - quoted:  diff --git "a/<old>" "b/<new>"  (core.quotepath form)
      - bare-with-spaces: diff --git a/some name.md b/some name.md
    For the bare form we split on the rightmost ` b/` — works as long
    as the old path doesn't itself end with ` b/...`, which is exotic
    enough to ignore (and would be malformed for our path schema).
    Returns None for malformed headers."""
    m = QUOTED_HEADER_RX.match(line.rstrip("\n"))
    if m:
        return m.group(1), m.group(2)
    rest = line[len("diff --git "):].rstrip("\n")
    if not rest.startswith("a/"):
        return None
    sep = " b/"
    idx = rest.rfind(sep)
    if idx < 0:
        return None
    return rest[2:idx], rest[idx + len(sep):]


_MALFORMED_PREFIX = object()  # sentinel: line is `---`/`+++` but prefix isn't a/, b/, or /dev/null


def extract_unified_path(line: str):
    """Extract path from a unified-diff path header (`--- a/X` or
    `+++ b/Y`). Handles ALL of:
        --- a/X            +++ b/Y
        --- "a/X"          +++ "b/Y"     (git core.quotepath form)
        --- /dev/null      (new/deleted file marker — not a real path)
    Tolerates optional `\\t<timestamp>` tail (GNU diff format).

    Returns:
        None                     for non-path-bearing lines (not `---`/`+++`)
                                 OR for `/dev/null`.
        _MALFORMED_PREFIX        for `---`/`+++` lines whose prefix is
                                 neither `a/`/`b/` nor `/dev/null`.
                                 The caller should treat this as a
                                 hard rejection — `git apply -p1`
                                 would strip the unknown prefix and
                                 silently apply the patch to a
                                 path scope-check never saw.
        <str>                    the extracted path (after `a/` or `b/`)."""
    if not (line.startswith("--- ") or line.startswith("+++ ")):
        return None
    rest = line[4:].rstrip("\n")
    if "\t" in rest:
        rest = rest.split("\t", 1)[0]
    rest = rest.strip()
    if rest == "/dev/null":
        return None
    if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
        rest = rest[1:-1]
    if rest.startswith("a/") or rest.startswith("b/"):
        return rest[2:]
    return _MALFORMED_PREFIX


def ok(p: str) -> bool:
    """Allow content pages and the exact taxonomy path, with no traversal."""
    if p != TAXONOMY_PATH and not PATH_RX.match(p):
        return False
    if any(seg == ".." for seg in p.split("/")):
        return False
    return True


def cmd_scope(diff_path: Path) -> int:
    """Validate every path-bearing line AND operation kind. Prints
    bad paths and forbidden operations; exits 1 if any, 0 otherwise.

    Operation scope: the LLM's prompt only allows create/modify
    operations. Destructive Git operations (delete, rename, copy)
    are rejected here as defense-in-depth — relying solely on the
    prompt would let a malformed LLM response delete or rename
    pages silently.

    Path scope (defense in depth): a unified patch without
    `diff --git` headers (just `--- a/X` / `+++ b/X`) is still
    applicable by `git apply`, so we also reject that whole shape
    as malformed/non-git-format — even if every individual path
    passes ok()."""
    bad: list[str] = []
    header_count = 0
    plus_path_count = 0

    with open(diff_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("diff --git "):
                header_count += 1
                parsed = parse_header(line)
                if parsed is None:
                    bad.append(line.rstrip("\n") + "  (malformed diff header)")
                    continue
                for p in parsed:
                    if not ok(p):
                        bad.append(p)
                continue
            if line.startswith("--- ") or line.startswith("+++ "):
                # Reject delete diffs that don't carry an explicit
                # `deleted file mode` line. `git apply` recognizes
                # `+++ /dev/null` as a delete instruction even without
                # `deleted file mode`, so without this check an LLM
                # could remove an in-scope wiki page just by emitting
                # `+++ /dev/null` against an `--- a/X` header.
                if line.startswith("+++ ") and line[4:].rstrip("\n").split("\t", 1)[0].strip() == "/dev/null":
                    bad.append(line.rstrip("\n") + "  (forbidden operation: delete via +++ /dev/null)")
                    continue
                p = extract_unified_path(line)
                if p is None:
                    continue
                if p is _MALFORMED_PREFIX:
                    # Header has an unrecognized prefix (not a/, b/, or
                    # /dev/null). `git apply -p1` would silently strip
                    # the unknown prefix and apply the patch to a path
                    # the scope check never saw. Reject the whole patch.
                    bad.append(line.rstrip("\n") + "  (unrecognized unified-header prefix; expected a/ or b/)")
                    continue
                plus_path_count += 1
                if not ok(p):
                    bad.append(p + "  (from unified-diff path header)")
                continue
            # Reject destructive operations. The prompt only allows
            # create/modify; a stray destructive op from a
            # malformed LLM response would otherwise pass scope and
            # then be applied by `git apply --index`.
            mode_line = line.rstrip("\n")
            mode_match = MODE_DECL_RX.match(mode_line) or INDEX_MODE_RX.match(mode_line)
            if mode_match and mode_match.group(1) not in REGULAR_GIT_MODES:
                bad.append(
                    line.rstrip("\n")
                    + "  (forbidden operation: symlink or non-regular Git mode)"
                )
                continue
            if line.startswith("deleted file mode"):
                bad.append("  (forbidden operation: deleted file mode)")
            elif line.startswith("rename from "):
                bad.append("  (forbidden operation: " + line.rstrip("\n") + ")")
            elif line.startswith("rename to "):
                bad.append("  (forbidden operation: " + line.rstrip("\n") + ")")
            elif line.startswith("copy from "):
                bad.append("  (forbidden operation: " + line.rstrip("\n") + ")")
            elif line.startswith("copy to "):
                bad.append("  (forbidden operation: " + line.rstrip("\n") + ")")

    if header_count == 0 and plus_path_count > 0:
        bad.append(
            "(patch has unified-diff path headers but no `diff --git`"
            " headers — refused as malformed/non-git-format)"
        )

    for b in bad:
        print(b)
    return 1 if bad else 0


def cmd_modify_targets(diff_path: Path) -> int:
    """Walk diff blocks, emit modify-target paths.
    For each `diff --git` block:
      - If `new file mode` is present → skip (Mode A).
      - If `rename from`/`rename to` is present AND no `@@` hunk →
        skip (pure rename, no content edit).
      - Else → emit both old_path and new_path (de-duped,
        scope-filtered). Source side is what git apply reports on
        failure; destination side is what's in CANDIDATES_FILE.
    For unified-only patches (no `diff --git` header) → emit paths
    from `--- a/X` / `+++ b/Y` headers only when at least one `@@`
    hunk exists; drop /dev/null."""
    text = open(diff_path, encoding="utf-8", errors="replace").read()
    out: set[str] = set()

    # Split on `diff --git ` block boundary. blocks[0] is the
    # preamble (anything before the first `diff --git`); subsequent
    # entries are block bodies.
    blocks = re.split(r"(?m)^diff --git ", text)
    preamble = blocks[0]
    block_bodies = blocks[1:]

    if not block_bodies:
        # Unified-only patch (no `diff --git` header). A unified
        # patch can describe many files in sequence:
        #   --- a/X      +++ b/X      @@ ...
        #   --- a/Y      +++ b/Y      @@ ...
        # Emit each (old, new) pair when its `@@` hunk arrives,
        # rather than overwriting old_p/new_p in a loop and only
        # yielding the last pair.
        old_p = new_p = None
        for line in preamble.splitlines():
            if line.startswith("--- "):
                p = extract_unified_path(line)
                if isinstance(p, str):
                    old_p = p
            elif line.startswith("+++ "):
                p = extract_unified_path(line)
                if isinstance(p, str):
                    new_p = p
            elif line.startswith("@@") and (old_p or new_p):
                # Hunk seen — flush accumulated path pair.
                for p in (old_p, new_p):
                    if p and ok(p):
                        out.add(p)
                # Reset so we don't re-emit if more hunks appear
                # for the same file before the next `--- /+++ ` pair.
                old_p = new_p = None

    for body in block_bodies:
        first_line, rest = (body.split("\n", 1) + [""])[:2]
        parsed = parse_header("diff --git " + first_line)
        if parsed is None:
            continue
        old_h, new_h = parsed
        is_new_file = False
        is_rename = False
        has_hunk = False
        old_unified = new_unified = None
        for line in rest.splitlines():
            if line.startswith("new file mode"):
                is_new_file = True
            elif line.startswith("rename from ") or line.startswith("rename to "):
                is_rename = True
            elif line.startswith("@@"):
                has_hunk = True
            elif line.startswith("--- "):
                p = extract_unified_path(line)
                if isinstance(p, str):
                    old_unified = p
            elif line.startswith("+++ "):
                p = extract_unified_path(line)
                if isinstance(p, str):
                    new_unified = p
        if is_new_file:
            continue  # Mode A; handled via stderr parsing.
        if is_rename and not has_hunk:
            continue  # pure rename, no content edit.
        for p in (old_h, new_h, old_unified, new_unified):
            if p and ok(p):
                out.add(p)

    for p in sorted(out):
        print(p)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("diff_file")
    ap.add_argument(
        "--mode",
        choices=["scope", "modify-targets"],
        default="scope",
        help="scope (default): validate paths; "
             "modify-targets: emit modify-target paths for retry expansion",
    )
    args = ap.parse_args()

    p = Path(args.diff_file)
    if not p.is_file():
        print(f"diff-paths: not a file: {args.diff_file}", file=sys.stderr)
        return 2

    if args.mode == "scope":
        return cmd_scope(p)
    return cmd_modify_targets(p)


if __name__ == "__main__":
    raise SystemExit(main())
