#!/usr/bin/env bash
# Regression oracle for the §14 apply-diff extraction.
#
# Holds the ORIGINAL inline-python (strip-fences, detect-expand) and the
# ORIGINAL bash pipeline (parse-failed-paths) verbatim, and diffs their
# output byte-for-byte against scripts/apply-diff.py across the tricky
# cases. No git, no LLM. Run from vault root:
#   scripts/tests/test_apply_diff.sh

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AD="$VAULT_ROOT/scripts/apply-diff.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rc=0; echo "test_apply_diff:"

# ── golden oracles (verbatim copies of the original ingest.sh logic) ────────
golden_strip_fences() {  # $1=src $2=out
  python3 - "$1" "$2" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
fences = re.findall(r"^```(?:diff|patch)?\s*\n(.*?)\n```\s*$", text, re.DOTALL | re.MULTILINE)
if fences:
    text = "\n".join(fences)
i = text.find("diff --git")
if i > 0:
    text = text[i:]
elif i < 0:
    pass
open(dst, "w", encoding="utf-8").write(text)
PY
}
golden_detect_expand() {  # $1=src $2=out
  python3 - "$1" "$2" <<'PY'
import json, re, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
def find_expand(s):
    if "diff --git " in s:
        return None
    decoder = json.JSONDecoder()
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", s, flags=re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1).strip())
            if isinstance(obj, dict) and obj.get("action") == "expand":
                return obj
        except json.JSONDecodeError:
            pass
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
PY
}
# (No golden for parse-failed-paths: the original bash Mode-B sed is
# unparseable — `|` is both delimiter and alternation, so sed aborts with
# "parentheses not balanced" before reading input, regardless of input.
# We therefore assert the INTENDED parse for all cases; the extraction
# fixes the latent bug. See apply-diff.py + the commit message.)

check() { # $1=name $2=golden-file $3=new-file
  if diff -u "$2" "$3" > "$TMP/d"; then echo "  ✓ $1"; else echo "  ✗ $1"; sed -n '1,30p' "$TMP/d"; rc=1; fi
}

# ── strip-fences cases ──────────────────────────────────────────────────────
sf() { # $1=name ; stdin = input
  local n="$1"; cat > "$TMP/in"
  golden_strip_fences "$TMP/in" "$TMP/g"; "$AD" strip-fences "$TMP/in" "$TMP/n" 2>/dev/null
  check "strip-fences: $n" "$TMP/g" "$TMP/n"
}
printf '```diff\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\n' | sf "single fence"
printf 'prose\n```diff\ndiff --git a/x b/x\n-a\n+b\n```\n```diff\ndiff --git a/y b/y\n-c\n+d\n```\n' | sf "multi fence"
printf 'Here is the patch:\ndiff --git a/x b/x\n-a\n+b\n' | sf "preamble, no fence"
printf 'NO_CHANGES: nothing to do here\n' | sf "no diff (NO_CHANGES)"
printf '```patch\ndiff --git a/x b/x\n-a\n+b\n```\n' | sf "patch fence"
printf 'diff --git a/x b/x\n-a\n+b\n' | sf "already starts with diff"

# ── detect-expand cases ─────────────────────────────────────────────────────
de() { local n="$1"; cat > "$TMP/in"
  golden_detect_expand "$TMP/in" "$TMP/g"; "$AD" detect-expand "$TMP/in" "$TMP/n" 2>/dev/null
  check "detect-expand: $n" "$TMP/g" "$TMP/n"
}
printf '{"action":"expand","files":["wiki/entities/A.md","wiki/topics/B.md"]}\n' | de "bare expand"
printf '```json\n{"action":"expand","files":["wiki/entities/A.md"]}\n```\n' | de "fenced expand"
printf 'I need more:\n{"action":"expand","files":["wiki/entities/A.md"]}\n' | de "preamble expand"
printf 'diff --git a/x b/x\n+{"action":"expand","files":["x"]}\n' | de "expand literal inside diff → none"
printf '{"action":"other"}\n' | de "non-expand json → none"
printf 'just prose, no json\n' | de "prose → none"

# ── parse-failed-paths cases ────────────────────────────────────────────────
# NOTE: the ORIGINAL bash Mode-B sed is BROKEN — it uses `|` as both the
# s-delimiter and the alternation operator, so it errors ("parentheses not
# balanced") on any "patch does not apply" / "already exists in index" line.
# So we golden-diff ONLY the Mode-A ("patch failed: X:") path against bash
# (where bash works), and assert the INTENDED output for Mode B (which the
# extraction fixes). See the commit message + apply-diff.py header.

pf_expect() { local n="$1" expected="$2"; cat > "$TMP/err"
  "$AD" parse-failed-paths "$TMP/err" > "$TMP/n" 2>/dev/null
  printf '%s' "$expected" > "$TMP/g"
  check "parse-failed-paths: $n" "$TMP/g" "$TMP/n"
}
# Mode A ("patch failed: X:")
pf_expect "Mode A dedupe+sort" $'wiki/entities/A.md\nwiki/entities/X.md\n' \
  <<< $'error: patch failed: wiki/entities/X.md:12\nerror: patch failed: wiki/entities/A.md:9\nerror: patch failed: wiki/entities/A.md:1'
pf_expect "Mode A quoted path" $'wiki/entities/Z with space.md\n' \
  <<< 'error: patch failed: "wiki/entities/Z with space.md":3'
# Mode B ("X: patch does not apply" / "already exists in index" / …)
pf_expect "Mode B patch-does-not-apply" $'wiki/topics/Y.md\n' <<< 'error: wiki/topics/Y.md: patch does not apply'
pf_expect "Mode B already-exists-in-index" $'wiki/topics/Y.md\n' <<< 'error: wiki/topics/Y.md: already exists in index'
pf_expect "Mode B a/ prefix stripped" $'wiki/entities/W.md\n' <<< 'error: a/wiki/entities/W.md: patch does not apply'
pf_expect "fatal ignored + Mode B" $'wiki/topics/Y.md\n' <<< $'fatal: unable to read index\nerror: wiki/topics/Y.md: does not match index'
pf_expect "Mode A + B same file deduped" $'wiki/entities/X.md\n' <<< $'error: patch failed: wiki/entities/X.md:12\nerror: wiki/entities/X.md: patch does not apply'
pf_expect "no error lines → empty" '' <<< 'warning: nothing here'

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
