#!/usr/bin/env bash
# Regression test for the §14 source-identity extraction.
#
#  (A) Golden-diff the naming transforms (safe_name / url_slug) against
#      the ORIGINAL bash `sed` pipelines, for a battery of names.
#  (B) Integration: a throwaway git repo exercising reuse (dup sha),
#      new-asset, and drifted-asset paths. No network, no LLM.
# Run from vault root:  scripts/tests/test_source_identity.sh

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SI="$VAULT_ROOT/scripts/source-identity.py"
rc=0
echo "test_source_identity:"

# ── (A) naming transforms vs original bash sed ──────────────────────────────
py_call() {  # $1=func $2=arg
  python3 - "$SI" "$1" "$2" <<'PY'
import importlib.util as u, sys
spec = u.spec_from_file_location("si", sys.argv[1])
m = u.module_from_spec(spec); spec.loader.exec_module(m)
print(getattr(m, sys.argv[2])(sys.argv[3]), end="")
PY
}
bash_safe_name() { printf '%s' "$1" | sed -E 's/[[:space:],()]+/-/g; s/-+/-/g; s/^-//; s/-\././g'; }
bash_url_slug()  { printf '%s' "$1" | sed -E 's|https?://||; s|[^A-Za-z0-9._-]+|-|g' | cut -c1-80; }

naming_ok=1
for n in "Power, Sex, Suicide (Nick Lane).epub" "  leading space.pdf" \
         "a--b__c.txt" "no-change.md" "trailing-.epub" "(parens).pdf"; do
  g="$(bash_safe_name "$n")"; p="$(py_call safe_name "$n")"
  [[ "$g" == "$p" ]] || { echo "  ✗ safe_name mismatch for [$n]: bash=[$g] py=[$p]"; naming_ok=0; rc=1; }
done
for u in "https://example.com/a/b?q=1&x=2" "http://hĕllo.例/path" \
         "https://$(printf 'x%.0s' {1..120}).com/y"; do
  g="$(bash_url_slug "$u")"; p="$(py_call url_slug "$u")"
  [[ "$g" == "$p" ]] || { echo "  ✗ url_slug mismatch for [$u]: bash=[$g] py=[$p]"; naming_ok=0; rc=1; }
done
[[ $naming_ok -eq 1 ]] && echo "  ✓ naming transforms match bash sed (9 cases)"

# ── (B) integration in a throwaway git repo ─────────────────────────────────
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
(
  cd "$TMP"
  git init -q; git config user.email t@t; git config user.name t
  mkdir sources
  printf 'hello world\n' > sources/2026-01-01-orig.txt
  ORIG_SHA="$(shasum -a 256 sources/2026-01-01-orig.txt | awk '{print $1}')"
  cat > sources/2026-01-01-orig.txt.md <<EOF
---
source_id: 01ORIGSOURCEID00000000000A
type: source
sha256: ${ORIG_SHA}
added: 2026-01-01T00:00:00Z
origin_type: file
origin_ref: 'orig.txt'
supersedes: null
title: '2026-01-01-orig.txt'
---

# 2026-01-01-orig.txt

Auto-generated sidecar. Do not hand-edit.
EOF
  git add -A; git commit -qm init

  pass() { echo "  ✓ $1"; }
  fail() { echo "  ✗ $1"; rc=1; }

  # B1: reuse — input with identical bytes → same source_id, EXISTING_SIDECAR set, no new sidecar
  printf 'hello world\n' > /tmp/si_dup.txt
  before="$(ls sources | wc -l)"
  out="$("$SI" /tmp/si_dup.txt 2>/dev/null)"; eval "$out"
  after="$(ls sources | wc -l)"
  if [[ "$SOURCE_ID" == "01ORIGSOURCEID00000000000A" && -n "$EXISTING_SIDECAR" && "$before" == "$after" ]]; then
    pass "reuse: dup sha → existing source_id, no new files"
  else
    fail "reuse path (SOURCE_ID=$SOURCE_ID EXISTING=$EXISTING_SIDECAR files $before→$after)"
  fi

  # B2: new — novel bytes → fresh ULID, EXISTING_SIDECAR empty, asset+sidecar created
  printf 'a totally different source\n' > /tmp/si_new.txt
  out="$("$SI" /tmp/si_new.txt 2>/dev/null)"; eval "$out"
  if [[ -z "$EXISTING_SIDECAR" && "$SOURCE_ID" != "01ORIGSOURCEID00000000000A" \
        && -f "$DEST" && -f "$SIDECAR" && "$DEST" == sources/*-si_new.txt ]]; then
    pass "new: novel sha → fresh source_id, asset+sidecar written"
  else
    fail "new path (EXISTING=$EXISTING_SIDECAR DEST=$DEST SIDECAR=$SIDECAR)"
  fi

  # B3: drift — tracked asset tampered on disk; input matches the sidecar's
  # recorded sha → dedup hits the sidecar, drift check must die.
  printf 'hello world\n' > /tmp/si_dup2.txt          # matches the RECORDED orig sha
  printf 'TAMPERED\n'    > sources/2026-01-01-orig.txt  # but on-disk asset now differs
  if "$SI" /tmp/si_dup2.txt >/dev/null 2>"$TMP/err"; then
    fail "drift: expected non-zero exit"
  elif grep -q 'drifted' "$TMP/err"; then
    pass "drift: tampered tracked asset → die 'drifted'"
  else
    fail "drift: wrong error: $(cat "$TMP/err")"
  fi

  # B4: URL fetches are bounded. Fake curl writes the requested -o file and
  # records argv; source-identity should pass both timeout and filesize caps.
  mkdir -p "$TMP/bin"
  cat > "$TMP/bin/curl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$TMP/curl.args"
out=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-o" ]]; then out="$2"; shift 2; continue; fi
  shift
done
printf 'url body\n' > "$out"
SH
  chmod +x "$TMP/bin/curl"
  if TMP="$TMP" PATH="$TMP/bin:$PATH" "$SI" "https://example.com/a/b?q=1" >/dev/null 2>"$TMP/url.err" \
     && grep -q -- "--max-time" "$TMP/curl.args" \
     && grep -q -- "--max-filesize" "$TMP/curl.args" \
     && grep -q -- "--proto =http,https" "$TMP/curl.args"; then
    pass "url fetch: curl bounded and restricted to http(s)"
  else
    fail "url fetch bounds missing: args=$(cat "$TMP/curl.args" 2>/dev/null) err=$(cat "$TMP/url.err" 2>/dev/null)"
  fi

  # B5: transactional mode reports every future path before publication and
  # waits for the parent acknowledgement. Killing it before PUBLISH must leave
  # the vault untouched.
  printf 'handshake cancellation source\n' > /tmp/si_handshake_cancel.txt
  if SI="$SI" python3 - /tmp/si_handshake_cancel.txt <<'PY'
import os, shlex, subprocess, sys
p = subprocess.Popen(
    [os.environ["SI"], "--reserve-handshake", sys.argv[1]],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
values = {}
for line in p.stdout:
    key, sep, raw = line.partition("=")
    if sep:
        parts = shlex.split(raw)
        values[key] = parts[0] if parts else ""
    if key == "IDENTITY_READY":
        break
assert values["IDENTITY_READY"] == "new", values
assert not os.path.exists(values["DEST"]), values
assert not os.path.exists(values["SIDECAR"]), values
p.terminate()
p.wait(timeout=5)
assert not os.path.exists(values["DEST"]), values
assert not os.path.exists(values["SIDECAR"]), values
PY
  then pass "reservation handshake: cancel before PUBLISH leaves no vault artifacts"
  else fail "reservation handshake cancellation safety"; fi

  # B6: after the parent has read and registered the paths, PUBLISH creates the
  # asset and sidecar and exits with the same machine-readable contract.
  printf 'handshake publication source\n' > /tmp/si_handshake_publish.txt
  if SI="$SI" python3 - /tmp/si_handshake_publish.txt <<'PY'
import os, shlex, subprocess, sys
p = subprocess.Popen(
    [os.environ["SI"], "--reserve-handshake", sys.argv[1]],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
values = {}
for line in p.stdout:
    key, sep, raw = line.partition("=")
    if sep:
        parts = shlex.split(raw)
        values[key] = parts[0] if parts else ""
    if key == "IDENTITY_READY":
        break
assert values["IDENTITY_READY"] == "new", values
assert not os.path.exists(values["DEST"]), values
p.stdin.write("PUBLISH\n"); p.stdin.flush()
_out, err = p.communicate(timeout=5)
assert p.returncode == 0, err
assert os.path.isfile(values["DEST"]), values
assert os.path.isfile(values["SIDECAR"]), values
PY
  then pass "reservation handshake: publication starts only after parent acknowledgement"
  else fail "reservation handshake publication"; fi

  # B7: the sidecar path is independently protected. Previously a user-created
  # untracked sidecar was overwritten whenever the sibling asset was absent.
  printf 'collision source\n' > /tmp/si_sidecar_collision.txt
  COLLISION_SC="sources/$(date -u +%F)-si_sidecar_collision.txt.md"
  printf 'user-owned sidecar\n' > "$COLLISION_SC"
  if "$SI" /tmp/si_sidecar_collision.txt >/dev/null 2>"$TMP/sidecar.err"; then
    fail "pre-existing untracked sidecar should be refused"
  elif [[ "$(cat "$COLLISION_SC")" == "user-owned sidecar" ]] \
       && grep -q 'refusing to overwrite pre-existing data' "$TMP/sidecar.err"; then
    pass "pre-existing untracked sidecar is refused without modification"
  else
    fail "pre-existing sidecar was changed or wrong error: $(cat "$TMP/sidecar.err")"
  fi
  rm -f /tmp/si_dup.txt /tmp/si_new.txt /tmp/si_dup2.txt
  rm -f /tmp/si_handshake_cancel.txt /tmp/si_handshake_publish.txt /tmp/si_sidecar_collision.txt
  exit $rc
) || rc=1

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
