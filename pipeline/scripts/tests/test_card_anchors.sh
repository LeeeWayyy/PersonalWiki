#!/usr/bin/env bash
# §8.2 card-anchor validator (lint --gate=card-anchors): every [src:<id>#card-N]
# must cite an image_note source and resolve to a real `## card N` heading in its
# committed .cards.md, 1 ≤ N ≤ card_count; a card anchor on a non-image_note source
# is a capability error. Run from the tooling root.

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LINT="$VAULT_ROOT/scripts/lint.py"
rc=0
echo "test_card_anchors:"

C="$(mktemp -d)/content"; mkdir -p "$C/sources" "$C/wiki/entities" "$C/.wiki"
trap 'rm -rf "$(dirname "$C")"' EXIT
git -C "$C" init -q; git -C "$C" config user.email t@t; git -C "$C" config user.name t
printf '# taxonomy\n' > "$C/wiki/_taxonomy.md"

IMG="$(printf 'I%.0s' {1..26})"   # 26-char [A-Z0-9] id
VID="$(printf 'V%.0s' {1..26})"
PAGE="$(printf 'P%.0s' {1..26})"
sha() { shasum -a 256 "$1" | awk '{print $1}'; }

# an image_note source: canonical .cards.md (2 cards) + sidecar (card_count: 2)
printf '## card 1\nhello\n\n## card 2\nworld\n' > "$C/sources/note.cards.md"
MDSHA="$(sha "$C/sources/note.cards.md")"
cat > "$C/sources/note.cards.md.md" <<YAML
---
source_id: ${IMG}
type: source
sha256: ${MDSHA}
origin_type: image_note
media:
  platform: rednote
  post_id: p1
  identity_basis: image_post_id
  card_count: 2
---
# x
YAML
# a transcript source (NON-image_note) to test the capability error
printf '# t\n\n<x>\n[0:00-0:05] hi\n' > "$C/sources/v.transcript.md"
TSHA="$(sha "$C/sources/v.transcript.md")"
cat > "$C/sources/v.transcript.md.md" <<YAML
---
source_id: ${VID}
type: source
sha256: ${TSHA}
origin_type: video
media: {platform: youtube, video_id: vidAAAAAAAA1}
---
# x
YAML

mkpage() { cat > "$C/wiki/entities/p.md" <<MD
---
type: Entity
page_id: ${PAGE}
sources: [${IMG}]
---
# P
<!-- llm-zone -->
$1
<!-- /llm-zone -->
MD
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm c >/dev/null; }

gate() { VAULT_CONTENT_DIR="$C" "$LINT" --gate=card-anchors 2>&1; }
ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }

mkpage 'A claim [src:'"${IMG}"'#card-1] and another [src:'"${IMG}"'#card-2].'
gate >/dev/null 2>&1 && ok "valid card-1/card-2 → pass" || { bad "valid cards failed"; gate; }

mkpage 'Out of range [src:'"${IMG}"'#card-9].'
gate >/dev/null 2>&1 && bad "card-9 (>count) should fail" || ok "card N out of range → fail"

mkpage 'Card anchor on a transcript [src:'"${VID}"'#card-1].'
gate >/dev/null 2>&1 && bad "card anchor on non-image_note should fail" || ok "card anchor on non-image_note → capability error"

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
