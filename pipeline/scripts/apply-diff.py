#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""
Diff/JSON parsing helpers for ingest.sh's apply path (§14 refactor).

ingest.sh keeps the orchestration (git apply, the LLM retry loop, commits)
— this module owns only the three bash-is-bad-at-it PARSING bits that were
inline (two of them duplicated). BEHAVIOR-PRESERVING: the first two
sub-commands are the verbatim inline-python; the third is the bash
grep|sed|while pipeline translated. Verified by
scripts/tests/test_apply_diff.sh.

Sub-commands:
  strip-fences <raw> <out>
      Remove ```diff/```patch fences and trim to the first `diff --git`.
  detect-expand <raw> <out>
      If the response is a `{"action":"expand","files":[…]}` JSON (bare or
      fenced, possibly after preamble) and NOT a diff, write its file paths
      to <out>, one per line; otherwise truncate <out> to empty.
  parse-failed-paths <apply-err>
      From `git apply` stderr, print the normalized (de-quoted, a//b/
      stripped), sorted-unique set of failing paths.
"""

from __future__ import annotations

import json
import re
import sys


def strip_fences(src: str, dst: str) -> int:
    # ── verbatim from ingest.sh inline python (lines 614-640 / 781-797) ──
    text = open(src, encoding="utf-8").read()
    # Remove ```diff / ``` fences and any prose outside them.
    # Some models fence each modified file in its own ```diff block. Capture
    # ALL fenced blocks, not just the first — `re.search` would silently drop
    # every subsequent file.
    # Anchor BOTH fence ends to line-start so a triple-backtick INSIDE the
    # diff body (e.g. a markdown edit that adds a ```code``` block)
    # does not prematurely close the outer fence. Diff lines start with
    # `+`, `-`, ` `, `@`, or `\`, so an inner ``` is never at column 0;
    # the outer fence's closer always is. MULTILINE for `^`/`$`.
    fences = re.findall(r"^```(?:diff|patch)?\s*\n(.*?)\n```\s*$", text, re.DOTALL | re.MULTILINE)
    if fences:
        text = "\n".join(fences)
    # If no fence, trim to the first "diff --git" onward.
    i = text.find("diff --git")
    if i > 0:                       # i == 0 → already starts with diff; nothing to do
        text = text[i:]
    elif i < 0:
        # No `diff --git` marker found — leave text intact (probably a
        # NO_CHANGES response or expand JSON). The downstream NO_CHANGES
        # detector and scope check will surface the actual issue.
        pass
    open(dst, "w", encoding="utf-8").write(text)
    return 0


def detect_expand(src: str, dst: str) -> int:
    # ── verbatim from ingest.sh inline python (lines 665-709) ──
    text = open(src, encoding="utf-8").read()

    def find_expand(s: str) -> dict | None:
        # If the response contains a `diff --git` header anywhere, treat it
        # as a diff. Otherwise a literal `{"action":"expand",…}` embedded in
        # the diff body (e.g. as added markdown content) could false-trigger
        # an expansion round and burn an extra LLM call.
        if "diff --git " in s:
            return None
        decoder = json.JSONDecoder()
        # Try fenced JSON first.
        fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", s, flags=re.DOTALL)
        if fence:
            try:
                obj = json.loads(fence.group(1).strip())
                if isinstance(obj, dict) and obj.get("action") == "expand":
                    return obj
            except json.JSONDecodeError:
                pass
        # Otherwise scan for the first JSON object via raw_decode (which
        # respects string semantics, so `{` or `}` inside a JSON string
        # value doesn't break parsing).
        for i in range(len(s)):
            if s[i] != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(s[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("action") == "expand":
                return obj
        return None

    obj = find_expand(text)
    if obj:
        files = [f for f in obj.get("files", []) if isinstance(f, str)]
        with open(dst, "w", encoding="utf-8") as f:
            for p in files:
                f.write(p + "\n")
    else:
        open(dst, "w").close()
    return 0


# Translation of ingest.sh's bash failing-path pipeline (lines 709-716):
#   grep -E "^error: " | sed -nE 's|^error: patch failed: ([^:]+):.*|\1|p;
#     s|^error: ([^:]+): (patch does not apply|already exists in index|
#       already exists in working directory|does not exist in index|
#       does not match index).*|\1|p' | (strip quotes + a//b/) | sort -u
_PAT_PATCH_FAILED = re.compile(r"^error: patch failed: ([^:]+):")
_PAT_OTHER = re.compile(
    r"^error: ([^:]+): (?:patch does not apply|already exists in index|"
    r"already exists in working directory|does not exist in index|"
    r"does not match index)"
)


def parse_failed_paths(apply_err: str) -> int:
    out: set[str] = set()
    with open(apply_err, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            # sed precedence: pattern-1 first; if it matches, pattern-2
            # cannot (pattern space is now just the path). Mutually exclusive.
            m = _PAT_PATCH_FAILED.match(line)
            if not m:
                m = _PAT_OTHER.match(line)
            if not m:
                continue
            p = m.group(1)
            # strip one surrounding double-quote each side, then a/ or b/.
            if p.endswith('"'):
                p = p[:-1]
            if p.startswith('"'):
                p = p[1:]
            if re.match(r"^[ab]/", p):
                p = p[2:]
            out.add(p)
    for p in sorted(out):
        sys.stdout.write(p + "\n")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: apply-diff.py {strip-fences|detect-expand|parse-failed-paths} …", file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    if cmd == "strip-fences":
        return strip_fences(sys.argv[2], sys.argv[3])
    if cmd == "detect-expand":
        return detect_expand(sys.argv[2], sys.argv[3])
    if cmd == "parse-failed-paths":
        return parse_failed_paths(sys.argv[2])
    print(f"apply-diff: unknown sub-command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
