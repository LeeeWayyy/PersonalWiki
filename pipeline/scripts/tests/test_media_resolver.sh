#!/usr/bin/env bash
# Unit test for the shared head-resolver (expansion-plan §8.0): die-loud
# semantics, youtube_video_id backcompat, platform-dispatched identity, and the
# specific divergence it fixes (supersedes-outside-set must DIE, not silent-skip).
# Run from the tooling root.

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
rc=0
echo "test_media_resolver:"

C="$(mktemp -d)/content"; mkdir -p "$C/sources"
trap 'rm -rf "$(dirname "$C")"' EXIT
git -C "$C" init -q; git -C "$C" config user.email t@t; git -C "$C" config user.name t

# helper: write a tracked transcript sidecar with given source_id, video_id, supersedes
mk() { # $1=id $2=video_id $3=supersedes-or-empty [$4=extra-yaml]
  local slug="src-$1"
  printf '# x\n' > "$C/sources/$slug.transcript.md"
  cat > "$C/sources/$slug.transcript.md.md" <<YAML
---
source_id: $1
type: source
origin_type: video
supersedes: ${3:-null}
media:
  platform: youtube
  video_id: $2
${4:-}
---
# x
YAML
}

DRIVER="$VAULT_ROOT/scripts/tests/_resolver_driver.py"
cat > "$DRIVER" <<'PY'
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/
import media_resolver as mr
op, vid = sys.argv[1], sys.argv[2]
src = Path("sources")
try:
    head = mr.resolve_head(src, ("youtube_video_id", (vid,)))
    print("HEAD", head[1]["source_id"] if head else "NONE")
except mr.ResolverError as e:
    print("DIE", str(e)[:60])
PY
run() { ( cd "$C" && uv run --quiet "$DRIVER" resolve "$1" 2>&1 ); }

ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }

# 1. legacy youtube sidecar (no identity_basis) → resolves via backcompat
mk 01AAAAAAAAAAAAAAAAAAAAAAAA vidAAAAAAAA1 ""
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm c1
[[ "$(run vidAAAAAAAA1)" == "HEAD 01AAAAAAAAAAAAAAAAAAAAAAAA" ]] && ok "youtube backcompat (no identity_basis) resolves" || bad "backcompat: $(run vidAAAAAAAA1)"

# 2. a clean supersedes chain → the new id is the unique head
mk 01BBBBBBBBBBBBBBBBBBBBBBBB vidAAAAAAAA1 "'[[01AAAAAAAAAAAAAAAAAAAAAAAA]]'"
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm c2
[[ "$(run vidAAAAAAAA1)" == "HEAD 01BBBBBBBBBBBBBBBBBBBBBBBB" ]] && ok "clean chain → unique head" || bad "chain: $(run vidAAAAAAAA1)"

# 3. no candidate → NONE (media-identity's novel-source path)
[[ "$(run vidZZZZZZZZ9)" == "HEAD NONE" ]] && ok "no candidate → NONE" || bad "novel: $(run vidZZZZZZZZ9)"

# 4. supersedes pointing OUTSIDE the identity set → DIE (the fixed silent-skip)
mk 01CCCCCCCCCCCCCCCCCCCCCCCC vidCCCCCCCC3 "'[[01DEADDEADDEADDEADDEADDEAD]]'"
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm c3
[[ "$(run vidCCCCCCCC3)" == DIE* ]] && ok "supersedes-outside-set → DIE (not silent-skip)" || bad "outside-set: $(run vidCCCCCCCC3)"
git -C "$C" rm -q "sources/src-01CCCCCCCCCCCCCCCCCCCCCCCC.transcript.md"* >/dev/null; git -C "$C" commit -qm c3b >/dev/null

# 5. unrecognized platform / no identity_basis → DIE (unparseable)
printf '# y\n' > "$C/sources/bad.transcript.md"
cat > "$C/sources/bad.transcript.md.md" <<'YAML'
---
source_id: 01EEEEEEEEEEEEEEEEEEEEEEEE
type: source
media:
  platform: weirdplatform
---
# y
YAML
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm c4
[[ "$(run vidAAAAAAAA1)" == DIE* ]] && ok "unknown platform → DIE (die-loud)" || bad "unknown-platform: $(run vidAAAAAAAA1)"
git -C "$C" rm -q "sources/bad.transcript.md"* >/dev/null; git -C "$C" commit -qm c4b >/dev/null

# 6. two heads for one identity (no supersedes link) → DIE (ambiguous)
mk 01FFFFFFFFFFFFFFFFFFFFFFFF vidFFFFFFFF6 ""
mk 01GGGGGGGGGGGGGGGGGGGGGGGG vidFFFFFFFF6 ""
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm c5
[[ "$(run vidFFFFFFFF6)" == DIE* ]] && ok "two heads → DIE (ambiguous)" || bad "ambiguous: $(run vidFFFFFFFF6)"

rm -f "$DRIVER"
[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
