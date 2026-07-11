#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""
Generate per-source ARGUMENT MAPS under `wiki/_maps/`.

NOT an index. A reading/argument map (schema §1 `type: map`,
plan/expansion-plan.md §15): it captures the author's *thinking flow* —
claims, questions and hypotheses linked by TYPED, DIRECTED relations —
plus a chapter-by-chapter reading guide. The structure is extracted by
one LLM pass over validated chapter intelligence when a complete cache
is available, with the SOURCE TEXT as an explicit fallback. It is
citation-grounded: every claim node and guide line carries a canonical
`[src:<id>#sec=<encoded-chapter>]`, so a human can verify it against the real
source without delimiter-ambiguous anchors.
The map is a derived view — never itself a source (schema §6).

Run from vault root:
    scripts/generate-mindmap.py                    # all sources in the log
    scripts/generate-mindmap.py --source-id <ULID>
    scripts/generate-mindmap.py --refresh          # re-call the LLM
    scripts/generate-mindmap.py --dry-run

Determinism / cost: the LLM JSON is cached in `.wiki/mindmap-cache/`
keyed by input identity + prompt version. The input identity is either
the source sha256 or a digest of the complete validated chapter-
intelligence set. Render is deterministic from the cache, so re-runs are
idempotent and free; the LLM is called only on first run, on `--refresh`,
or when its selected input changes. LLM command comes from the shared LLM
client. `LLM_CMD` remains available as an advanced stdin/stdout command
override.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

# Reuse the hardened autolinker (longest-match-first, ASCII word-boundary,
# fenced-code / existing-link skipping, domain filter) so reading-guide
# prose links to entity/topic pages with the same false-positive
# protection the rest of the vault relies on.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _util import default_vault_root  # noqa: E402
from autolink import _autolink_body, _build_entries  # noqa: E402
import chapter_intelligence as ci  # noqa: E402
import derived_lib as dl  # noqa: E402
from source_citations import SOURCE_ID_RX, source_citation  # noqa: E402

TOOLING_ROOT = Path(__file__).resolve().parent.parent  # tooling repo (scripts/, schema.md)
VAULT_ROOT = default_vault_root(TOOLING_ROOT)
WIKI_DIR = VAULT_ROOT / "wiki"
MAPS_DIR = WIKI_DIR / "_maps"
SOURCES_DIR = VAULT_ROOT / "sources"
LOG_PATH = VAULT_ROOT / ".wiki" / "log.md"
CACHE_DIR = VAULT_ROOT / ".wiki" / "mindmap-cache"
CHAPTER_INTELLIGENCE_CACHE_DIR = VAULT_ROOT / ".wiki" / "chapter-intelligence-cache"
EXTRACT = TOOLING_ROOT / "scripts" / "extract.py"

PROMPT_VERSION = "v2"
NODE_WRAP = 16   # soft-wrap Mermaid node labels every ~N chars with <br>
SOURCE_CHAR_LIMIT = 120_000
LLM_TIMEOUT_S = 900

NODE_ID_RX = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

KINDS = {"question", "hypothesis", "claim", "evidence", "consequence"}
RELS = {"answers", "supports", "explains", "causes", "leads-to",
        "competes-with", "refines"}
# Mermaid node shape per kind (open/close delimiters around a quoted label).
KIND_SHAPE = {
    "question": ('(["', '"])'),     # stadium
    "hypothesis": ('{{"', '"}}'),   # hexagon
    "claim": ('["', '"]'),          # rectangle
    "evidence": ('[/"', '"/]'),     # parallelogram
    "consequence": ('(("', '"))'),  # circle
}


# ─── shared helpers ──────────────────────────────────────────────────────────


def chapter_order(source_id: str) -> list[str]:
    """Distinct chapter labels for a source, first-appearance order, from
    `.wiki/log.md`. The `pages:` field is staged-files (ignored)."""
    if not LOG_PATH.is_file():
        return []
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return dl.chapter_order_from_lines(lines, source_id)


def load_chapter_intelligence(
    source_id: str,
    source_sha256: str,
    chapters: list[str],
) -> tuple[dict | None, str]:
    """Load one current, validated artifact for every log chapter.

    Chapter text is intentionally unavailable here. As in
    ``discover_prior_spines``, source spans were checked before the analyzer's
    atomic cache write. This consumer revalidates each artifact and its cache
    manifest without assuming the producer's model is still configured.
    """
    if not chapters:
        return None, "no chapter labels in .wiki/log.md"

    expected_labels = set(chapters)
    artifacts_by_label: dict[str, list[tuple[int, str, dict, dict]]] = {
        label: [] for label in chapters
    }
    # The analyzer's shared cache scanner requires a manifest and checks the
    # artifact/manifest agreement and ordered_sections. This consumer does not
    # assume the producer's model/schema/template is still configured, so it
    # applies no further producer-identity predicate — only the log's labels.
    for section, modified_ns, name, validated, inputs in ci.scan_validated_entries(
        CHAPTER_INTELLIGENCE_CACHE_DIR,
        source_id=source_id,
        source_sha256=source_sha256,
        prompt_version=ci.PROMPT_VERSION,
        ordered_sections=chapters,
    ):
        if section in expected_labels:
            artifacts_by_label[section].append(
                (modified_ns, name, validated, inputs)
            )

    incomplete = [label for label in chapters if not artifacts_by_label[label]]
    if incomplete:
        return None, "incomplete chapter intelligence: " + ", ".join(incomplete)

    selected: list[dict] = []
    spines: list[dict] = []
    for label in chapters:
        coherent = [
            item for item in artifacts_by_label[label]
            if item[3]["analysis_context"]["previous_chapter_spine"] == spines
        ]
        if not coherent:
            return None, f"incoherent chapter intelligence at {label}"
        artifact = max(coherent, key=lambda item: (item[0], item[1]))[2]
        selected.append(artifact)
        spines.append({
            "section_label": label,
            "central_question": artifact["central_question"],
            "chapter_claim": artifact["chapter_claim"],
        })

    return {
        "schema": ci.SCHEMA_VERSION,
        "artifacts": selected,
    }, f"complete chapter intelligence ({len(chapters)} chapters)"


def compact_intelligence_input(bundle: dict) -> dict:
    """Project validated artifacts to the evidence needed by the map model."""
    compact_chapters = []
    for artifact in bundle["artifacts"]:
        compact_chapters.append(
            {
                "label": artifact["section_label"],
                "question": artifact["central_question"],
                "claim": artifact["chapter_claim"],
                "builds_on": artifact["builds_on"],
                "claims": [
                    {key: claim[key] for key in ci.CLAIM_PROJECTION_FIELDS}
                    for claim in artifact["claims"]
                ],
                "relations": artifact["relations"],
                "entities": [
                    {key: entity[key] for key in ci.ENTITY_PROJECTION_FIELDS}
                    for entity in artifact["entities"]
                ],
                "topics": artifact["topics"],
            }
        )
    return {
        "schema": "mindmap-chapter-intelligence-input/1",
        "chapters": compact_chapters,
    }


def intelligence_cache_identity(compact_input: dict) -> str:
    """Hash exactly the compact intelligence object supplied to the map model."""
    # The leading non-hex marker guarantees separation from raw source SHA
    # identities while retaining 44 digest bits in derived_lib's sha12 path.
    return "i" + ci.json_digest(compact_input)


def mermaid_label(s: str) -> str:
    """Escape for inside a Mermaid quoted label. CJK is fine; quotes,
    brackets, pipes, newlines are not (entity-code them)."""
    return (
        s.replace("\n", " ").replace('"', "#quot;")
        .replace("[", "#91;").replace("]", "#93;").replace("|", "#124;")
    )


def mermaid_node_label(s: str, width: int = NODE_WRAP) -> str:
    """Soft-wrap a node label to ~`width` chars/line with <br>, packing
    by unit so ASCII tokens (ATP, DNA) never split. CJK chars are
    single units. Then escape — <br> survives (none of its chars are in
    the escape set)."""
    if len(s) <= width:
        return mermaid_label(s)
    units = re.findall(r"[A-Za-z0-9]+|\S|\s", s)
    lines: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for u in units:
        if cur and cur_len + len(u) > width:
            lines.append("".join(cur))
            cur, cur_len = [u], len(u)
        else:
            cur.append(u)
            cur_len += len(u)
    if cur:
        lines.append("".join(cur))
    return mermaid_label("<br>".join(lines))


_LINK_ENTRIES: list | None = None


def autolink_prose(s: str) -> str:
    """Link known entity/topic aliases in prose via the shared autolinker
    (self_domains=[] → domain filter falls through, linking all matches —
    right for a single book spanning domains). No-op if the index is
    absent."""
    global _LINK_ENTRIES
    if _LINK_ENTRIES is None:
        try:
            idx = json.loads((WIKI_DIR / ".alias-index.json").read_text(encoding="utf-8"))
            _LINK_ENTRIES = _build_entries(idx)
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            _LINK_ENTRIES = []
    return _autolink_body(s, _LINK_ENTRIES, None, []) if _LINK_ENTRIES else s


# ─── LLM extraction ──────────────────────────────────────────────────────────


def _build_prompt(
    evidence: str,
    chapters: list[str],
    title: str,
    *,
    evidence_instruction: str,
    evidence_heading: str,
    evidence_rules: str = "",
) -> str:
    chap_list = (
        ", ".join(chapters)
        if chapters
        else "(use the 第N章 / Chapter-N headings in the text)"
    )
    return f"""You map the ARGUMENT of a book — its author's reasoning flow — for a
reading aid. {evidence_instruction} and output ONE JSON object (no prose,
no markdown fences) with exactly this shape:

{{
  "central_question": "the one question the whole book sets out to answer (in the book's language)",
  "thesis": "the book's central answer/claim, 1-2 sentences (book's language)",
  "chapters": [
    {{"label": "<one of: {chap_list}>",
      "question": "the question THIS chapter answers",
      "claim": "the chapter's key claim/move",
      "builds_on": "how it follows from earlier chapters, or null for the first"}}
  ],
  "nodes": [
    {{"id": "n1",
      "kind": "question|hypothesis|claim|evidence|consequence",
      "label": "a SHORT SENTENCE stating the actual claim/question — informative, not a 4-char fragment; ~12-30 Chinese chars / ~6-15 words, the book's language",
      "chapter": "<one of the chapter labels above>"}}
  ],
  "edges": [
    {{"from": "n1", "to": "n2",
      "rel": "answers|supports|explains|causes|leads-to|competes-with|refines"}}
  ]
}}

Rules:
- Capture the THINKING FLOW, not a topic index: nodes are claims,
  questions, hypotheses, evidence, or consequences — NOT a list of
  entities/terms. Aim for 18-30 nodes and a comparable number of edges.
- Node labels are SHORT SENTENCES that state the actual point (e.g.
  "真核细胞可能只演化出现过一次" — not "真核只一次"). Name the key
  entities/terms inside the label so the reasoning is legible.
- Edges must be MEANINGFUL and DIRECTED (cause/support/answer/compete),
  forming a connected reasoning structure — not "these co-occur".
- Every node's `chapter` MUST be one of: {chap_list}.
- `id`s are short ascii tokens (n1, n2, …), unique; edges reference them.
- Labels in the book's own language ({title!r} is in Chinese → Chinese labels).
{evidence_rules}- Output ONLY the JSON object.

{evidence_heading}:
{evidence}
"""


def build_prompt(text: str, chapters: list[str], title: str) -> str:
    return _build_prompt(
        text,
        chapters,
        title,
        evidence_instruction="Read the SOURCE TEXT",
        evidence_heading="SOURCE TEXT",
    )


def build_intelligence_prompt(compact_input: dict, chapters: list[str], title: str) -> str:
    compact = ci.canonical_json(compact_input)
    rules = """- CHAPTER INTELLIGENCE is already source-grounded and validated. Use its
  chapter question/claim/builds_on spine, claims, and directed relations as
  the evidence for the argument flow; do not invent claims beyond it.
- Claim ids are local to each chapter. Preserve chapter provenance when
  turning them into globally unique map nodes.
- Entities and topics provide context for precise claim labels. They are not
  themselves argument nodes.
"""
    return _build_prompt(
        compact,
        chapters,
        title,
        evidence_instruction="Read the VALIDATED CHAPTER INTELLIGENCE",
        evidence_heading="VALIDATED CHAPTER INTELLIGENCE",
        evidence_rules=rules,
    )


def validate(obj: dict, chapters: list[str]) -> dict:
    """Coerce/validate the LLM JSON. Drop nodes with bad chapter/kind and
    edges referencing dropped/unknown nodes or bad rels. Returns a clean
    structure; raises if it's hopeless (no nodes)."""
    chap_set = set(chapters)
    nodes_in = obj.get("nodes") or []
    nodes: dict[str, dict] = {}
    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "").strip()
        kind = str(n.get("kind") or "").strip()
        label = str(n.get("label") or "").strip()
        chap = str(n.get("chapter") or "").strip()
        if not (NODE_ID_RX.match(nid) and label and kind in KINDS):
            continue
        if chap_set and chap not in chap_set:
            continue  # ungrounded chapter → drop
        if nid in nodes:
            continue
        nodes[nid] = {"id": nid, "kind": kind, "label": label, "chapter": chap}
    edges = []
    seen_e: set[tuple[str, str, str]] = set()
    for e in obj.get("edges") or []:
        if not isinstance(e, dict):
            continue
        a, b, rel = str(e.get("from") or ""), str(e.get("to") or ""), str(e.get("rel") or "")
        if a in nodes and b in nodes and a != b and rel in RELS:
            key = (a, b, rel)
            if key not in seen_e:
                seen_e.add(key)
                edges.append({"from": a, "to": b, "rel": rel})
    chapters_out = []
    for c in obj.get("chapters") or []:
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or "").strip()
        if chap_set and label not in chap_set:
            continue
        chapters_out.append({
            "label": label,
            "question": str(c.get("question") or "").strip(),
            "claim": str(c.get("claim") or "").strip(),
            "builds_on": (str(c.get("builds_on")).strip()
                          if c.get("builds_on") not in (None, "", "null") else ""),
        })
    if not nodes:
        raise ValueError("LLM produced no valid nodes")
    return {
        "central_question": str(obj.get("central_question") or "").strip(),
        "thesis": str(obj.get("thesis") or "").strip(),
        "chapters": chapters_out,
        "nodes": list(nodes.values()),
        "edges": edges,
    }


# ─── render ──────────────────────────────────────────────────────────────────


def content_hash(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def render_map(source_id: str, title: str, chapters: list[str], data: dict,
               chash: str, last_generated: str, existing: str | None) -> str:
    nodes = data["nodes"]
    edges = data["edges"]
    by_chapter: dict[str, list[dict]] = {}
    for n in nodes:
        by_chapter.setdefault(n["chapter"] or "(unscoped)", []).append(n)
    ordered = [c for c in chapters if c in by_chapter] + \
              [c for c in by_chapter if c not in chapters]

    def cite(chap: str) -> str:
        return source_citation(source_id, chap if chap and chap in chapters else "")

    # ---- Mermaid argument flow ----
    mer = ["```mermaid", "flowchart TD"]
    for chapter_index, chap in enumerate(ordered):
        mer.append(f'  subgraph c{chapter_index}["{mermaid_label(chap)}"]')
        for n in by_chapter[chap]:
            o, c = KIND_SHAPE.get(n["kind"], ('["', '"]'))
            mer.append(f'    {n["id"]}{o}{mermaid_node_label(n["label"])}{c}')
        mer.append("  end")
    for e in edges:
        mer.append(f'  {e["from"]} -->|{e["rel"]}| {e["to"]}')
    mer.append("```")

    # ---- Reading guide spine ----
    guide = ["## Reading guide", ""]
    chap_meta = {c["label"]: c for c in data["chapters"]}
    for chap in chapters:
        m = chap_meta.get(chap)
        if not m:
            continue
        guide.append(f"### {chap}")
        if m["question"]:
            guide.append(f"- **Q** {autolink_prose(m['question'])}")
        if m["claim"]:
            guide.append(f"- **▸** {autolink_prose(m['claim'])}")
        if m["builds_on"]:
            guide.append(f"- **↳** {autolink_prose(m['builds_on'])}")
        guide.append(f"  {cite(chap)}")
        guide.append("")

    # ---- Claims index (per-node provenance) ----
    legend = ["## Claims", ""]
    for chap in ordered:
        legend.append(f"### {chap}")
        for n in by_chapter[chap]:
            legend.append(f"- _{n['kind']}_ — {n['label']} {cite(chap)}")
        legend.append("")

    central = ["## Central argument", ""]
    if data["central_question"]:
        central.append(f"**Question.** {autolink_prose(data['central_question'])} {cite('')}")
        central.append("")
    if data["thesis"]:
        central.append(f"**Thesis.** {autolink_prose(data['thesis'])} {cite('')}")
        central.append("")

    fm = (
        "---\n"
        "type: map\n"
        f"source_id: {source_id}\n"
        f"title: '{title.replace(chr(39), chr(39) * 2)}'\n"
        f"node_count: {len(nodes)}\n"
        f"edge_count: {len(edges)}\n"
        f"chapter_count: {len(ordered)}\n"
        f"last_generated: {last_generated}\n"
        f"content_hash: {chash}\n"
        "---\n\n"
    )
    h1 = f"# 🧭 {title}\n\n"
    human = dl.render_human_zone(existing, "map-zone")
    body = (
        "<!-- map-zone -->\n"
        + "\n".join(central) + "\n"
        + "## Argument flow\n\n" + "\n".join(mer) + "\n\n"
        + "\n".join(guide) + "\n"
        + "\n".join(legend)
        + "<!-- /map-zone -->\n"
    )
    return fm + h1 + human + "\n" + body


def generate_one(source_id: str, meta: dict, refresh: bool, dry_run: bool) -> tuple[bool, str]:
    if not SOURCE_ID_RX.fullmatch(source_id):
        raise ValueError(f"invalid source_id {source_id!r}")
    chapters = chapter_order(source_id)
    sha = meta["sha256"]
    intelligence, intelligence_status = load_chapter_intelligence(
        source_id, sha, chapters
    )
    if intelligence is not None:
        compact_intelligence = compact_intelligence_input(intelligence)
        input_identity = intelligence_cache_identity(compact_intelligence)
        input_path = intelligence_status
    else:
        compact_intelligence = None
        input_identity = sha
        input_path = f"raw-source fallback ({intelligence_status})"

    data = (
        None
        if refresh
        else dl.load_flat_cache(CACHE_DIR, PROMPT_VERSION, source_id, input_identity)
    )
    used_llm = False
    if data is None:
        if dry_run:
            return False, (
                f"  ⟳ {source_id}: needs LLM extraction via {input_path} "
                "(no map cache) — skipped under --dry-run"
            )
        if intelligence is not None:
            prompt = build_intelligence_prompt(
                compact_intelligence, chapters, meta["title"]
            )
        else:
            text = dl.extract_source_text(EXTRACT, meta["asset"], SOURCE_CHAR_LIMIT)
            prompt = build_prompt(text, chapters, meta["title"])
        raw = dl.call_llm(prompt, LLM_TIMEOUT_S)
        data = validate(dl.extract_json(raw), chapters)
        dl.save_flat_cache(CACHE_DIR, PROMPT_VERSION, source_id, input_identity, data)
        used_llm = True

    chash = content_hash(data)
    base_slug = dl.fs_safe_slug(
        dl.source_slug(meta["title"], source_id), fallback=source_id
    )
    suffix = f"-{source_id}.md"
    byte_budget = 255 - len(suffix.encode("utf-8"))
    base_slug = base_slug.encode("utf-8")[:byte_budget].decode(
        "utf-8", errors="ignore"
    ).rstrip(". -") or "map"
    path = MAPS_DIR / f"{base_slug}{suffix}"
    if path.is_file():
        current_meta = dl.parse_frontmatter(
            path.read_text(encoding="utf-8", errors="replace")
        ) or {}
        if current_meta.get("source_id") != source_id:
            raise ValueError(f"canonical map path is owned by another source: {path.name}")
    # ponytail: one bounded scan per source; build an index only if map counts
    # make batch generation measurably slow.
    owned_paths = []
    if MAPS_DIR.is_dir():
        for candidate in sorted(MAPS_DIR.rglob("*.md")):
            if candidate == path:
                continue
            candidate_text = candidate.read_text(encoding="utf-8", errors="replace")
            if (dl.parse_frontmatter(candidate_text) or {}).get("source_id") == source_id:
                owned_paths.append(candidate)
    if len(owned_paths) > 1:
        raise ValueError(
            f"multiple existing maps claim source_id {source_id}: "
            + ", ".join(path.name for path in owned_paths)
        )
    legacy_path = owned_paths[0] if owned_paths else None
    existing_path = path if path.exists() else legacy_path
    existing = existing_path.read_text(encoding="utf-8") if existing_path else None
    today = date.today().isoformat()
    prior = dl.existing_last_generated(existing)
    display = dl.clean_title(meta["title"])
    preserved = render_map(source_id, display, chapters, data, chash, prior or today, existing)
    tag = f" [{input_path}{', LLM' if used_llm else ''}]"
    if existing_path == path and existing == preserved:
        if legacy_path:
            if not dry_run:
                legacy_path.unlink()
            return True, f"  {'would remove' if dry_run else 'removed'} legacy {legacy_path.relative_to(VAULT_ROOT)}{tag}"
        return False, f"  = {path.relative_to(VAULT_ROOT)} unchanged ({len(data['nodes'])} nodes){tag}"
    content = render_map(source_id, display, chapters, data, chash, today, existing)
    wrote = dl.atomic_write(path, content, dry_run)
    if wrote and not dry_run and legacy_path:
        legacy_path.unlink()
    verb = "would write" if dry_run else "wrote"
    return wrote, f"  {verb} {path.relative_to(VAULT_ROOT)} ({len(data['nodes'])} nodes, {len(data['edges'])} edges){tag}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--source-id", help="only this source_id (default: all in the log)")
    ap.add_argument("--refresh", action="store_true", help="re-call the LLM, ignore cache")
    ap.add_argument("--dry-run", action="store_true", help="render from cache only; never call LLM or write")
    args = ap.parse_args()

    if not WIKI_DIR.is_dir():
        print("generate-mindmap: run from vault root", file=sys.stderr)
        return 2

    sources = dl.find_sources(SOURCES_DIR)
    if args.source_id:
        if args.source_id not in sources:
            print(f"generate-mindmap: no sidecar for {args.source_id}", file=sys.stderr)
            return 2
        targets = [args.source_id]
    else:
        targets = []
        if LOG_PATH.is_file():
            for line in LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
                m = dl.LOG_LINE_RX.match(line)
                if m and m.group(1) not in targets and m.group(1) in sources:
                    targets.append(m.group(1))

    written = 0
    failures = 0
    for sid in targets:
        try:
            wrote, msg = generate_one(sid, sources[sid], args.refresh, args.dry_run)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the batch
            wrote, msg = False, f"  ✗ {sid}: {exc}"
            failures += 1
        written += 1 if wrote else 0
        print(msg)

    print()
    label = "would change" if args.dry_run else "written/updated"
    print(f"generate-mindmap: {written} map(s) {label} of {len(targets)} source(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
