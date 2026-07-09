#!/usr/bin/env bash
# Test the media --rerender migration (expansion-plan §7.5): re-render a
# committed transcript from its (unchanged) .transcript.json after a
# render_format_version bump → new source_id supersedes the old + every live
# [src:<old>#mm:ss] citation is repointed to the new id (no ASR, no LLM).
# Run from the tooling root.

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXT="$VAULT_ROOT/scripts/tests/fixtures/transcript-sample.json"
OLD=01OLDMEDIASRC000000000000A     # 26-char ULID
VID=ba-HMvDn_vU
rc=0
echo "test_rerender_media:"

C="$(mktemp -d)/content"; mkdir -p "$C/sources" "$C/wiki/entities" "$C/wiki/_maps" "$C/.wiki"
trap 'rm -rf "$(dirname "$C")"' EXIT
git -C "$C" init -q; git -C "$C" config user.email t@t; git -C "$C" config user.name t

SLUG="2026-06-01-youtube-${VID}"
cp "$FIXT" "$C/sources/${SLUG}.transcript.json"
JSHA="$(shasum -a 256 "$C/sources/${SLUG}.transcript.json" | awk '{print $1}')"
printf '# Old render\n\n<https://www.youtube.com/watch?v=%s>\n' "$VID" > "$C/sources/${SLUG}.transcript.md"
SHA="$(shasum -a 256 "$C/sources/${SLUG}.transcript.md" | awk '{print $1}')"
# sidecar at render_format_version: 0 (older than the code's 1 → triggers rerender)
cat > "$C/sources/${SLUG}.transcript.md.md" <<YAML
---
source_id: ${OLD}
type: source
sha256: ${SHA}
added: 2026-06-01T00:00:00Z
origin_type: video
origin_ref: 'https://www.youtube.com/watch?v=${VID}'
supersedes: null
title: 'Old render'
media:
  platform: youtube
  video_id: ${VID}
  canonical_url: 'https://www.youtube.com/watch?v=${VID}'
  transcript_json_sha256: ${JSHA}
  render_format_version: 0
---

# Old render
YAML
# a wiki page citing the old source with a time anchor + frontmatter sources:
cat > "$C/wiki/entities/p.md" <<MD
---
type: Entity
page_id: 01PAGE0000000000000000000A
sources: [${OLD}]
---
# P
<!-- llm-zone -->
> A claim about the talk [src:${OLD}#00:10-00:18].
<!-- /llm-zone -->
MD
git -C "$C" add -A; git -C "$C" commit -qm baseline

ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }
HEAD_BEFORE="$(git -C "$C" rev-parse HEAD)"
VAULT_CONTENT_DIR="$C" python3 "$VAULT_ROOT/scripts/rerender-media.py" "$VID" > /dev/null 2>&1 \
  || { echo "  ✗ rerender exited non-zero"; exit 1; }

[[ "$(git -C "$C" rev-parse HEAD)" != "$HEAD_BEFORE" ]] && ok "a commit was created" || bad "no commit"
NEW="$(git -C "$C" log -1 --format=%s | sed -nE 's/^rerender: ([0-9A-Z]{26}) supersedes.*/\1/p')"
[[ -n "$NEW" && "$NEW" != "$OLD" ]] && ok "new source_id ($NEW) supersedes $OLD" || bad "no fresh source_id"
NEWSC="$C/sources/${SLUG}.r1.transcript.md.md"
[[ -f "$NEWSC" ]] && ok "new artifact set written (.r1)" || bad "no .r1 artifacts"
grep -qF "supersedes: '[[${OLD}]]'" "$NEWSC" 2>/dev/null || grep -qF "supersedes: \"[[${OLD}]]\"" "$NEWSC" 2>/dev/null || grep -qF "[[${OLD}]]" "$NEWSC" && ok "sidecar supersedes the old id" || bad "supersedes not set"
grep -qE 'render_format_version: 1' "$NEWSC" && ok "render_format_version bumped to 1" || bad "version not bumped"
# old artifacts immutable — still present
[[ -f "$C/sources/${SLUG}.transcript.md.md" ]] && ok "old source preserved (immutable)" || bad "old source removed"
# the citation was repointed to the new id, anchor preserved
grep -qF "[src:${NEW}#00:10-00:18]" "$C/wiki/entities/p.md" && ok "citation repointed to new id (anchor preserved)" || bad "citation not rewired"
grep -qF "$OLD" "$C/wiki/entities/p.md" && bad "old id still cited on the page" || ok "no dangling old-id citation"
grep -qF "sources: [${NEW}]" "$C/wiki/entities/p.md" && ok "frontmatter sources: migrated" || bad "frontmatter not migrated"

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
