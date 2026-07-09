#!/usr/bin/env bash
# Self-check for the `--profile lang` ingest path:
# scripts/generate-language-pages.py emits structured `_reading/*.reading.json`
# plus an HTML reader, isolated under content/lang/.
#
# Runs the FULL `ingest.py --profile lang` loop with the deterministic stub LLM
# inside a throwaway git content repo.

set -uo pipefail
PIPELINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="$(cd "$PIPELINE_ROOT/.." && pwd)"
STUB="$PIPELINE_ROOT/scripts/tests/stub-llm.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rc=0
echo "test_language_pages:"
ok() { echo "  ✓ $1"; }
bad() { echo "  ✗ $1"; rc=1; }

BASE="$TMP/content"
mkdir -p "$BASE/.wiki"; : > "$BASE/.wiki/.keep"
cp "$PROJECT_ROOT/content/.gitignore" "$BASE/.gitignore" 2>/dev/null || true
git -C "$BASE" init -q
git -C "$BASE" config user.email t@t; git -C "$BASE" config user.name t
git -C "$BASE" config core.quotepath false
git -C "$BASE" add -A; git -C "$BASE" commit -qm init

ING() { LLM_CMD="$STUB" VAULT_CONTENT_DIR="$BASE" python3 "$PIPELINE_ROOT/ingest.py" --profile lang "$@"; }
LINT() { VAULT_CONTENT_DIR="$BASE" uv run "$PIPELINE_ROOT/scripts/lint.py" --profile lang; }
current_sid() { git -C "$BASE" log -1 --format=%s | sed -nE 's/^lang: ([0-9A-Z]{26}).*/\1/p'; }
latest_reading_json() {
  local rel
  rel="$(git -C "$BASE" show --name-only --format= HEAD | grep -E '^lang/_reading/.*\.reading\.json$' | head -1 || true)"
  [[ -n "$rel" ]] && printf '%s/%s\n' "$BASE" "$rel"
}
latest_reading_html() {
  local rel
  rel="$(git -C "$BASE" show --name-only --format= HEAD | grep -E '^lang/_reading/.*\.html$' | head -1 || true)"
  [[ -n "$rel" ]] && printf '%s/%s\n' "$BASE" "$rel"
}
json_query() {
  python3 - "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
tokens = [
    tok
    for ch in d.get("chapters", [])
    for para in ch.get("paragraphs", [])
    for sent in para.get("sentences", [])
    for tok in sent.get("tokens", [])
]
sentences = [
    sent
    for ch in d.get("chapters", [])
    for para in ch.get("paragraphs", [])
    for sent in para.get("sentences", [])
]
sys.exit(0 if eval(sys.argv[2], {"d": d, "tokens": tokens, "sentences": sentences}) else 1)
PY
}

# Multi-chapter JP fixture. 料理 appears in BOTH chapters (first-touch = ch1).
# 心(ココロ) vs 核心(カクシン) are different lemmas. The ch1 heading carries
# an internal double space to exercise section matching and chapter_key logging.
cat > "$TMP/jp.txt" <<'MD'
## 第1章  序論
これは本です。料理を食べる。心がある。
## 第2章
料理は美味しい。核心を突く。
MD

out="$(ING "$TMP/jp.txt" 2>&1)" || { bad "ingest --profile lang exited non-zero"; echo "$out" | tail -20; }
echo "$out" | grep -q "committed lang pages" && ok "ingest committed lang pages" || bad "no lang commit"

RJ="$(latest_reading_json)"
RH="$(latest_reading_html)"
sid="$(current_sid)"
[[ -f "$RJ" && -f "$RH" ]] \
  && ok "structured reading JSON + HTML written" \
  || bad "missing generated _reading artifacts"

[[ -n "$sid" ]] && json_query "$RJ" "d.get('schema') == 'reading/2' and d.get('source_id') == '$sid' and len(d.get('chapters', [])) == 2" \
  && ok "reading JSON schema/source/chapters are correct" || bad "reading JSON metadata wrong"
json_query "$RJ" "any(t.get('t') == '料理' and t.get('w') == '料理' for t in tokens)" \
  && ok "fugashi tokenized content words (料理 present)" || bad "no tokenized vocab"
json_query "$RJ" "[t.get('new') for t in tokens if t.get('t') == '料理'] == [True, False]" \
  && ok "first-touch: 料理 new only on first occurrence" || bad "first-touch wrong for repeated 料理"
json_query "$RJ" "any(t.get('t') == '心' for t in tokens) and any(t.get('t') == '核心' for t in tokens)" \
  && ok "distinct lemmas 心 / 核心 both present" || bad "lemma-key dedup merged distinct words"
json_query "$RJ" "any(t.get('t') == '料理' and t.get('rt') == 'りょうり' for t in tokens) and not any(t.get('t') == '料理' and t.get('rt') == 'リョウリ' for t in tokens)" \
  && ok "reading normalized katakana→hiragana (りょうり)" || bad "reading not normalized to hiragana"
json_query "$RJ" "all(s.get('en') for s in sentences) and any(s.get('grammar') for s in sentences)" \
  && ok "sentence translations + anchored grammar emitted" || bad "missing sentence translation or grammar"

grep -qE "\\[src:${sid}#[^][]+\\]" "$RH" \
  && ok "HTML reader renders chapter citations" || bad "no chapter citations in reader HTML"
grep -qE "#第1章 序論  pages:" "$BASE/lang/.wiki/log.md" \
  && ok "space-bearing chapter label round-trips to log (LOG_LINE_RX form)" \
  || { bad "chapter label did not round-trip to log"; grep '#第1章' "$BASE/lang/.wiki/log.md"; }

# ── isolation: nothing under content/sources or content/wiki; child wrote lang/sources ──
[[ ! -d "$BASE/sources" && ! -d "$BASE/wiki" ]] && ok "isolation: no content/sources or content/wiki" \
  || bad "isolation leak: content/sources or content/wiki exists"
ls "$BASE"/lang/sources/*.md >/dev/null 2>&1 && ok "child source-identity wrote lang/sources/" || bad "no lang sidecar"
offtree="$(git -C "$BASE" show --name-only --format= HEAD | grep -v '^lang/' | grep -v '^$' || true)"
[[ -z "$offtree" ]] && ok "commit touches only lang/ paths (no lang/lang/, no leak)" || { bad "commit touched non-lang paths"; echo "$offtree"; }

# ── lang lint runs (not vacuous): clean passes, a seeded bad citation fails ──
LINT >/dev/null 2>&1 && ok "lang lint passes on a clean tree" || bad "lang lint failed on clean tree"
printf '\n[src:01ZZZZZZZZZZZZZZZZZZZZZZZZZ]\n' >> "$RH"
LINT >/dev/null 2>&1 && bad "lang lint vacuously passed a bad citation" || ok "lang lint catches an orphan citation (non-vacuous)"
git -C "$BASE" checkout -- "${RH#"$BASE"/}" 2>/dev/null

# ── forbidden flags error BEFORE any source write ──
before="$(find "$BASE/lang/sources" -maxdepth 1 -type f | wc -l | tr -d ' ')"
ING --kind video "$TMP/jp.txt" >/dev/null 2>&1 && bad "--kind should be rejected under lang" || ok "--kind rejected under --profile lang"
after="$(find "$BASE/lang/sources" -maxdepth 1 -type f | wc -l | tr -d ' ')"
[[ "$before" == "$after" ]] && ok "rejected run left lang/sources/ untouched" || bad "forbidden-flag run dirtied lang/sources/"
ING --section '^X$' "$TMP/jp.txt" >/dev/null 2>&1 && bad "--section should be rejected under lang" || ok "--section rejected under --profile lang"
ING --limit 5000 "$TMP/jp.txt" >/dev/null 2>&1 && bad "--limit should be rejected under lang" || ok "--limit rejected under --profile lang"
ING --title T "$TMP/jp.txt" >/dev/null 2>&1 && bad "--title should be rejected under lang" || ok "--title rejected under --profile lang"
ING --feed-url http://x "$TMP/jp.txt" >/dev/null 2>&1 && bad "--feed-url should be rejected under lang" || ok "--feed-url rejected under --profile lang"
ING --platform podcast "$TMP/jp.txt" >/dev/null 2>&1 && bad "--platform should be rejected under lang" || ok "explicit --platform rejected under --profile lang"

VAULT_CONTENT_DIR="$BASE/lang" python3 "$PIPELINE_ROOT/ingest.py" --profile lang "$TMP/jp.txt" >/dev/null 2>&1 \
  && bad "double-nest guard missing" || ok "env-contract guard rejects a lang-subtree VAULT_CONTENT_DIR"

# ── idempotency: a CLEAN re-run (no edits) makes no new commit ──
commits_before="$(git -C "$BASE" rev-list --count HEAD)"
ING "$TMP/jp.txt" >/dev/null 2>&1
commits_after="$(git -C "$BASE" rev-list --count HEAD)"
[[ "$commits_before" == "$commits_after" ]] && ok "idempotent re-run made no new commit" || bad "clean re-run created a commit"

# ── cache-collision regression: two chapters whose fs-safe slugs collapse
#    (甲/X and 甲:X → 甲-X) must keep separate cache entries by ordinal. ──
cat > "$TMP/coll.txt" <<'MD'
## 甲/X
机がある。
## 甲:X
窓がある。
MD
ING "$TMP/coll.txt" >/dev/null 2>&1
RJ_COLL="$(latest_reading_json)"
json_query "$RJ_COLL" "len(d.get('chapters', [])) == 2 and any(t.get('t') == '窓' for t in d['chapters'][1]['paragraphs'][0]['sentences'][0]['tokens']) and not any(t.get('t') == '机' for t in d['chapters'][1]['paragraphs'][0]['sentences'][0]['tokens'])" \
  && ok "collapsing-slug chapters keep separate caches (ch2 has 窓, not ch1's 机)" \
  || bad "cache-key collision: ch2 reading wrong (expected 窓, not 机)"

# ── structural-char regression: heading chars that break citation/log syntax
#    must be stripped consistently and remain log-idempotent. ──
cat > "$TMP/sc.txt" <<'MD'
## 第1章 [導入: pages]
机がある。
MD
ING "$TMP/sc.txt" >/dev/null 2>&1
sid_sc="$(current_sid)"
RH_SC="$(latest_reading_html)"
grep -qE "\\[src:${sid_sc}#[^][]+\\]" "$RH_SC" \
  && ok "bracket/colon heading → well-formed citation anchor" \
  || { bad "structural chars leaked into citation anchor"; grep -o '\[src:[^]]*\]*' "$RH_SC" | head -2; }
sc_log_before="$(grep -c '#第1章 導入 pages ' "$BASE/lang/.wiki/log.md" 2>/dev/null || echo 0)"
ING "$TMP/sc.txt" >/dev/null 2>&1
sc_log_after="$(grep -c '#第1章 導入 pages ' "$BASE/lang/.wiki/log.md" 2>/dev/null || echo 0)"
[[ "$sc_log_before" == "1" && "$sc_log_after" == "1" ]] \
  && ok "structural-char chapter is log-idempotent (1 line, no dup on re-run)" \
  || bad "log idempotency broke for structural-char heading (before=$sc_log_before after=$sc_log_after)"

# ── empty-chapter citation + duplicate-heading warning ──
cat > "$TMP/edge.txt" <<'MD'
## 第1章
机がある。
## まとめ
。
## まとめ
。
MD
edge_out="$(ING "$TMP/edge.txt" 2>&1)"
echo "$edge_out" | grep -q "share chapter key" && ok "duplicate heading warns (まとめ merged)" || bad "no duplicate-heading warning"
sid_edge="$(current_sid)"
RJ_EDGE="$(latest_reading_json)"
RH_EDGE="$(latest_reading_html)"
json_query "$RJ_EDGE" "len(d.get('chapters', [])) == 2" \
  && ok "duplicate まとめ merged → 2 chapters total, not 3" || bad "duplicate heading not merged"
grep -qE "\\[src:${sid_edge}#まとめ\\]" "$RH_EDGE" \
  && ok "empty-vocab/grammar chapter still carries a chapter citation" || bad "empty chapter has no [src:] anchor"
grep -qE "\\[src:${sid_edge}\\]" "$RH_EDGE" \
  && ok "reader HTML carries a source-level [src:] citation" || bad "reader HTML missing source citation"

# ── differing-raw-text collision must MERGE (not drop): two headings that
#    canonicalize to the same key but have different raw text. ──
cat > "$TMP/mrg.txt" <<'MD'
## 語彙 A:B
机がある。
## 語彙 A B
窓がある。
MD
ING "$TMP/mrg.txt" >/dev/null 2>&1
RJ_MRG="$(latest_reading_json)"
json_query "$RJ_MRG" "any(t.get('t') == '机' for t in tokens) and any(t.get('t') == '窓' for t in tokens)" \
  && ok "collapsing-key headings merge (both 机 and 窓 extracted, no drop)" \
  || bad "differing-raw-text collision DROPPED a section (expected both 机 and 窓)"

[[ $rc -eq 0 ]] && echo "  ALL PASS" || echo "  FAIL"
exit $rc
