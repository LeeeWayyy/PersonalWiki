#!/usr/bin/env bash
# End-to-end test for the Phase 3 podcast (audio_extraction) front door (§8.1):
# `media-identity.py --platform podcast --feed-url <f> --episode-guid <g>` →
# (stub) extract-remote → validate/render → atomic move + podcast sidecar.
# Exercises: happy path, dedup-after-resolve reuse, guid-reconciliation hard
# error, bare-feed rejection, feed_url canonicalization. Run from the tooling root.

set -euo pipefail
VAULT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STUB="$VAULT_ROOT/scripts/tests/stub-extract-remote"   # via $EXTRACT_REMOTE_CMD
MI="$VAULT_ROOT/scripts/media-identity.py"
rc=0
FEED="https://www.Example.com/Feed.xml/#frag"   # www + trailing slash + fragment → canonicalized
GUID="ep-0001-guid"
echo "test_podcast_e2e:"

C="$(mktemp -d)/content"; mkdir -p "$C/sources"
trap 'rm -rf "$(dirname "$C")"' EXIT
git -C "$C" init -q; git -C "$C" config user.email t@t; git -C "$C" config user.name t

ok() { echo "  ✓ $1"; }; bad() { echo "  ✗ $1"; rc=1; }
run() { env VAULT_CONTENT_DIR="$C" EXTRACT_REMOTE_CMD="$STUB" "$@"; }

# ── 1. bare feed (no selector) → die ──
if run "$MI" --platform podcast --feed-url "$FEED" >/dev/null 2>&1; then
  bad "bare feed (no selector) should die"
else ok "bare feed without a selector → die"; fi

# ── 2. happy path → new podcast source ──
OUT="$(run "$MI" --platform podcast --feed-url "$FEED" --episode-guid "$GUID" 2>/tmp/pod.err)" \
  || { echo "  ✗ podcast ingest exited non-zero:"; sed 's/^/    | /' /tmp/pod.err; exit 1; }
SC="$(ls "$C"/sources/*.transcript.md.md 2>/dev/null | head -1)"
[[ -n "$SC" ]] && ok "podcast sidecar written" || { bad "no sidecar"; echo "$OUT"; }
grep -q '^  platform: podcast' "$SC" && ok "platform: podcast" || bad "platform not podcast"
grep -q "^  identity_basis: feed_guid" "$SC" && ok "identity_basis: feed_guid" || bad "no identity_basis"
grep -qF "  feed_url: 'https://example.com/Feed.xml'" "$SC" && ok "feed_url canonicalized (www/slash/frag stripped)" || { bad "feed_url not canonicalized"; grep feed_url "$SC"; }
grep -qF "  episode_guid: '$GUID'" "$SC" && ok "episode_guid recorded" || bad "no episode_guid"
grep -q "^  transcript_json_sha256: " "$SC" && ok "transcript_json_sha256 guard present" || bad "no transcript_json_sha256"
grep -q "evidence_artifacts" "$SC" && bad "evidence_artifacts present (should be deferred to §8.4)" || ok "no evidence_artifacts yet (deferred to §8.4 lint gen)"
grep -q "origin_type: audio" "$SC" && ok "origin_type: audio" || bad "wrong origin_type"
# emit contract
echo "$OUT" | grep -q "^SOURCE_ID=" && echo "$OUT" | grep -q "AUDIT_JSON=" && ok "emit contract (SOURCE_ID + AUDIT_JSON)" || { bad "emit contract missing"; echo "$OUT"; }
# the committed .transcript.json is the envelope (carries segments + meta)
MD="${SC%.md}"; JSON="${MD%.transcript.md}.transcript.json"
[[ -f "$JSON" ]] && grep -q '"kind": "audio_extraction"' "$JSON" && ok ".transcript.json = the envelope" || bad "audit json not the envelope"
# the markdown carries timestamped segment lines
grep -qE '^\[00:00-00:05\] SPEAKER_00:' "$MD" && ok "rendered timestamped transcript md" || { bad "md not timestamped"; head -8 "$MD"; }

# ── 3. dedup: commit, re-run same identity → reuse ──
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm c1
OUT2="$(run "$MI" --platform podcast --feed-url "$FEED" --episode-guid "$GUID" 2>/dev/null)"
echo "$OUT2" | grep -q "EXISTING_SIDECAR=.*transcript.md.md" && ok "re-ingest same identity → reuse (EXISTING_SIDECAR set)" || { bad "did not dedup"; echo "$OUT2"; }
[[ "$(ls "$C"/sources/*.transcript.md.md | wc -l | tr -d ' ')" == "1" ]] && ok "no duplicate source minted" || bad "duplicate source minted"

# ── 3b. pre-network dedup: with the guid known, a committed episode reuses WITHOUT
#        calling the service — prove it by making the stub FAIL; reuse must succeed. ──
OUT3="$(run env STUB_EXTRACT_FAIL=1 "$MI" --platform podcast --feed-url "$FEED" --episode-guid "$GUID" 2>/dev/null)" \
  && echo "$OUT3" | grep -q "EXISTING_SIDECAR=.*transcript.md.md" \
  && ok "pre-network dedup reuses without invoking extract-remote" || bad "pre-network dedup did not short-circuit the service"

# ── 4. guid reconciliation: a FRESH (uncommitted) guid the service resolves to a
#       DIFFERENT one → die (pre-call dedup misses, so the service is invoked). ──
if run env STUB_EXTRACT_GUID="other-guid-9999" "$MI" --platform podcast --feed-url "$FEED" --episode-guid "fresh-guid-xyz" >/dev/null 2>&1; then
  bad "guid mismatch should die"
else ok "service-resolved guid ≠ requested → die"; fi

# ── 5. --retranscribe on an existing head → SUPERSEDE: mint a new source + emit
#       SUPERSEDES=old, the new (staged) sidecar carries supersedes: '[[old]]'. ──
OLD_SID="$(grep -h '^source_id:' "$SC" | awk '{print $2}')"
OUT5="$(run "$MI" --platform podcast --feed-url "$FEED" --episode-guid "$GUID" --retranscribe 2>/tmp/pod-rt.err)" \
  || { bad "--retranscribe supersede exited non-zero"; sed 's/^/    | /' /tmp/pod-rt.err | tail -5; }
NEW_SID="$(echo "$OUT5" | sed -nE 's/^SOURCE_ID=(.*)/\1/p' | tr -d "'\"")"
[[ -n "$NEW_SID" && "$NEW_SID" != "$OLD_SID" ]] && ok "--retranscribe mints a NEW source (≠ old)" || bad "--retranscribe did not mint a new source"
echo "$OUT5" | grep -qF "SUPERSEDES=$OLD_SID" && ok "emit SUPERSEDES=old (ingest migrates citations)" || { bad "no SUPERSEDES emit"; echo "$OUT5"; }
NEWSC="$(echo "$OUT5" | sed -nE 's/^SIDECAR=(.*)/\1/p' | tr -d "'\"")"
[[ -n "$NEWSC" ]] && grep -qF "supersedes: '[[${OLD_SID}]]'" "$C/$NEWSC" && ok "new podcast sidecar supersedes old" || { bad "new sidecar missing supersedes"; grep -n supersedes "$C/$NEWSC" 2>/dev/null; }
# the old source is untouched (still tracked) — supersede is mint-new, never mutate-old
git -C "$C" checkout -- sources/ 2>/dev/null; git -C "$C" clean -fdq sources/ 2>/dev/null  # drop the staged (uncommitted) supersede artifacts

# ── 6. enclosure-only (no feed URL) → die (a feed-scoped identity needs a feed) ──
if run "$MI" --platform podcast --enclosure-url "https://cdn.example.com/x.mp3" >/dev/null 2>&1; then
  bad "enclosure-only without --feed-url should die"
else ok "enclosure-only without --feed-url → die"; fi

# ── 7. segment hard gates: out-of-order and NaN must die ──
if run env STUB_EXTRACT_SEGMENTS=ooo "$MI" --platform podcast --feed-url "https://feed2.example.com/f.xml" --episode-guid g-ooo >/dev/null 2>&1; then
  bad "out-of-order segments should die"; else ok "out-of-order segments → die"; fi
if run env STUB_EXTRACT_SEGMENTS=nan "$MI" --platform podcast --feed-url "https://feed2.example.com/f.xml" --episode-guid g-nan >/dev/null 2>&1; then
  bad "NaN segment start should die"; else ok "NaN start → die (would bypass coverage gate)"; fi

# ── 8. cross-basis collision guard: ingest under feed_enclosure (no guid), then a
#       later resolve with a guid (same feed+enclosure) must DIE, not mint a dup. ──
F2="https://feed3.example.com/f.xml"
run "$MI" --platform podcast --feed-url "$F2" --episode-url "https://ep.example.com/1" >/dev/null 2>/tmp/p8.err \
  && grep -q '^  identity_basis: feed_enclosure' "$(ls -t "$C"/sources/*.transcript.md.md | head -1)" \
  && ok "no-guid episode → feed_enclosure source" || { bad "feed_enclosure ingest failed"; cat /tmp/p8.err; }
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm c8
if run env STUB_EXTRACT_GUID="now-has-guid" "$MI" --platform podcast --feed-url "$F2" --episode-guid "now-has-guid" >/dev/null 2>&1; then
  bad "basis upgrade (enclosure→guid) should die (cross-basis collision)"
else ok "basis upgrade for a known episode → die (no silent dup)"; fi

# ── 9. feed_title_published: title-keyed identity is stable across re-ingest ──
F3="https://feed4.example.com/f.xml"
run env STUB_EXTRACT_NO_ENCLOSURE=1 "$MI" --platform podcast --feed-url "$F3" --episode-title "Ep Title" --episode-published "2026-05-01" >/dev/null 2>/tmp/p9.err \
  && grep -q '^  identity_basis: feed_title_published' "$(ls -t "$C"/sources/*.transcript.md.md | head -1)" \
  && ok "no-guid/no-enclosure → feed_title_published source" || { bad "feed_title_published ingest failed"; cat /tmp/p9.err; }
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm c9
OUT9="$(run env STUB_EXTRACT_NO_ENCLOSURE=1 "$MI" --platform podcast --feed-url "$F3" --episode-title "Ep Title" --episode-published "2026-05-01" 2>/dev/null)"
echo "$OUT9" | grep -q "EXISTING_SIDECAR=.*transcript.md.md" && ok "feed_title_published re-ingest → reuse (title identity stable)" || { bad "feed_title_published did not dedup"; echo "$OUT9"; }

# ── 10. episode_title reconciliation: a user --episode-title differing from the
#        service-resolved title must DIE (service title wins the identity). ──
F5="https://feed5.example.com/f.xml"
if run env STUB_EXTRACT_NO_ENCLOSURE=1 STUB_EXTRACT_TITLE="Service Official Title" \
     "$MI" --platform podcast --feed-url "$F5" --episode-title "User Label" --episode-published "2026-05-01" >/dev/null 2>&1; then
  bad "episode_title mismatch (user≠service) should die"
else ok "episode_title mismatch (user≠service) → die (service title wins identity)"; fi

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
