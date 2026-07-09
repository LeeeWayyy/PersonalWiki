#!/usr/bin/env bash
# Unit test for lint.py's media timestamp-anchor validator (expansion-plan §7.4):
# a [src:<id>#H:MM:SS-H:MM:SS] citation must resolve to real transcript segments
# in the source's committed .transcript.json. Builds a tiny isolated content
# tree (media sidecar + the real-shaped JSON fixture + a wiki page) and drives
# check_timestamp_anchors directly. Run from the tooling root.

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXT="$VAULT_ROOT/scripts/tests/fixtures/transcript-sample.json"
SID="01TESTMEDIASRC0000000000AB"
rc=0
echo "test_timestamp_anchors:"

# fixture transcript segments span ≈10.1s → 21.8s (the real video's first 6 segs)
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/wiki/entities" "$T/sources"
cp "$FIXT" "$T/sources/vid.transcript.json"
JSHA="$(shasum -a 256 "$T/sources/vid.transcript.json" | awk '{print $1}')"
printf -- '---\nsource_id: %s\ntype: source\nsha256: x\norigin_type: video\nmedia:\n  video_id: ba-HMvDn_vU\n  transcript_json_sha256: %s\n---\n' "$SID" "$JSHA" > "$T/sources/vid.transcript.md.md"
printf '# t\n' > "$T/sources/vid.transcript.md"

page() {  # $1=suffix-char  $2=anchor
  printf -- '---\ntype: Entity\npage_id: 01PAGE000000000000000000%s\n---\n# P\n<!-- llm-zone -->\n> claim [src:%s#%s].\n<!-- /llm-zone -->\n' \
    "$1" "$SID" "$2" > "$T/wiki/entities/p.md"
}
verdict() {  # prints "OK" or "FAIL"
  VAULT_CONTENT_DIR="$T" python3 -c "
import importlib.util
s=importlib.util.spec_from_file_location('lint','$VAULT_ROOT/scripts/lint.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
ok,_=m.check_timestamp_anchors(); print('OK' if ok else 'FAIL')"
}
expect() {  # $1=label  $2=anchor  $3=OK|FAIL
  page A "$2"; got="$(verdict)"
  if [[ "$got" == "$3" ]]; then echo "  ✓ $1 ($2 → $got)"; else echo "  ✗ $1: $2 expected $3 got $got"; rc=1; fi
}

expect "in-range, contiguous coverage" "00:10-00:18" OK
expect "past end of transcript"        "00:10-09:00" FAIL
expect "before any speech"             "00:00-00:05" FAIL
expect "zero-length range"             "00:12-00:12" FAIL
expect "exceeds 10-min cap"            "00:10-11:00" FAIL

# a time anchor citing a NON-media source (no .transcript.json) must fail
printf -- '---\nsource_id: 01DOCSOURCE00000000000000A\ntype: source\nsha256: y\norigin_type: file\n---\n' > "$T/sources/doc.epub.md"
printf 'x\n' > "$T/sources/doc.epub"
printf -- '---\ntype: Entity\npage_id: 01PAGE000000000000000000Z\n---\n# P\n<!-- llm-zone -->\n> c [src:01DOCSOURCE00000000000000A#00:01-00:05].\n<!-- /llm-zone -->\n' > "$T/wiki/entities/p.md"
got="$(verdict)"
[[ "$got" == "FAIL" ]] && echo "  ✓ time anchor on non-media source rejected" || { echo "  ✗ non-media anchor not rejected ($got)"; rc=1; }

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
