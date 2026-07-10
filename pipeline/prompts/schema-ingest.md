# Schema Rule Blocks for Wiki Ingest

This file is split into named rule blocks. `build-prompt.py` selects the
blocks relevant to the current operation. If two selected blocks overlap,
follow the stricter rule.

## Page Selection And Coverage

- Integrate the source into a graph, not a chapter summary. Create or
  update enough pages to cover the source's central entities,
  mechanisms, hypotheses, processes, named organisms, named
  enzymes/proteins, named people, named books, and recurring technical
  concepts.
- Do not treat `SOURCE_KEY_TERMS` as a quota. It is a recall checklist.
  If `SOURCE_TEXT` contains additional important concepts not listed
  there, still create or update pages for them.
- Prefer a dedicated `Entity` page for a singular reusable node: a
  biological structure, molecule, enzyme, species/group, mechanism,
  named theory, named person, named method, or named dataset.
- Prefer a `Topic` page for a synthesis question or theme spanning
  multiple entities or sources: origin stories, tradeoffs, disputes,
  causal chains, and comparative frameworks.
- Do not create pages for chapter titles, section titles, generic
  phrases, one-off examples, or source-local rhetorical labels unless
  they are also real reusable concepts.

## Page Types

- `Entity` lives in `wiki/entities/`: a person, project, concept,
  molecule, organism, mechanism, method, named theory, event, or other
  singular node worth linking to later.
- `Topic` lives in `wiki/topics/`: a synthesis page spanning multiple
  entities, mechanisms, disputes, or sources.
- `index`, `map`, `reading`, and source sidecars are tool-owned or
  derived artifacts. The ingest LLM must not edit them.

## Frontmatter

Content pages under `wiki/entities/` and `wiki/topics/` use YAML
frontmatter:

```yaml
---
type: Entity | Topic
aliases: [...]
tags: [...]
---
```

- Tooling owns `page_id`, `sources`, and `last_ingested`. Do not emit
  `page_id`; it is injected after apply. `sources` and `last_ingested`
  are overwritten from citations.
- `aliases:` must include every important surface form seen in the
  source plus cross-language names when known. Do not invent unknown
  translations.
- `tags:` must use only tags present in `TAXONOMY`.

## Tags

- Every entity/topic page carries 2-4 tags drawn only from `TAXONOMY`.
- Emit `tags:` in single-line flow style: `tags: [concept,
  biology/cell]`.
- Pick exactly one Form tag and at least one Domain tag.
- Optional secondary tags may be Domain or Reserved tags.
- Preserve existing page tags unless the new source meaningfully
  changes the page's form or primary domain.
- Use `taxonomy-gap` only alongside the closest available Domain tag
  when the source clearly needs a missing Domain. It does not replace
  the required Domain tag.

## Zones

- Body prose may only be edited inside `<!-- llm-zone -->` and
  `<!-- /llm-zone -->`.
- Never edit `human-zone`.
- Every new or modified page must include the closing
  `<!-- /llm-zone -->` marker.
- New single-source pages may use one simple callout:

```markdown
<!-- llm-zone -->
> [!AI] LLM Synthesis
>
> One idea, developed in prose, with a citation [src:<id>#<label>].
<!-- /llm-zone -->
```

## Citations

- Every claim in `llm-zone` ends with one or more `[src:<id>]`
  citations.
- If `SECTION_LABEL` is present, every citation to this run's source
  must include it as an anchor: `[src:<id>#<SECTION_LABEL>]`.
- Multiple citations are allowed: `[src:a,src:b]`.
- Never cite a wiki page as a source. `[src:...]` resolves only to
  source sidecar ids listed in `ALL_SOURCE_IDS` or this run's
  `SOURCE_META`.
- The citation carries provenance. Do not repeat provenance in prose
  unless attribution is semantically necessary.
- Weak evidence stays weak. If the source hedges, hedge in prose:
  `可能`, `也许`, `据认为`, `有人主张`, `仍有争议`, `may`,
  `might`, `likely`, `is proposed to`, `arguably`, etc.

## Voice And Attribution

Entity pages are encyclopedia-style. Write declarative claims about
the entity itself, not about how a chapter or book phrases it.

Forbidden on `type: Entity` pages:

- English: `the chapter`, `the section`, `the source`, `the text`,
  `the book`, `the author`, `according to`, `it is argued that`.
- Chinese: `本章`, `本节`, `本书`, `该章`, `该节`, `这一章`,
  `第N章`, `文中`, `书中`, `文献`, `作者认为`, `作者指出`,
  `书中认为`, `书中指出`, `书中提出`, `按照本章`.

Bad entity prose:

```markdown
> 本章把线粒体获得定义为复杂生命出现的前提 [src:x#第一章].
> 书中指出线粒体自由基泄漏决定衰老速度 [src:x#第七章].
```

Good entity prose:

```markdown
> 线粒体获得可能是复杂生命出现的前提 [src:x#第一章].
> 线粒体自由基泄漏比例可能影响衰老速度 [src:x#第七章].
```

Topic pages discuss how sources frame a subject, but source-narrating
prose is reserved for real attribution or comparison.

- Default to declarative topic prose.
- Do not use a chapter number as the grammatical subject. The citation
  already carries the chapter.
- When attribution matters, name the author, book, theory, or
  hypothesis, not only the chapter.
- Distinguish authorial claims from relayed textbook knowledge. If the
  source merely reports established facts, write the fact
  declaratively and cite the source as provenance.

## Language And Naming

- Source language wins at page creation. A Chinese source creates
  Chinese H1/prose/filenames; an English source creates English
  H1/prose/filenames.
- Existing pages keep their existing page language. Translate the new
  source evidence into that page language.
- Chinese filenames are the bare Chinese term with no spaces.
  English filenames are kebab-case ASCII.
- Page `# H1` matches the filename stem.
- Wikilinks use the target page's native title. Use `[[stem|alias]]`
  when the surface form differs from the target title.
- First mention may include a parenthetical other-language form only
  when the source language differs from the page language.

## Prose Shape

- One idea per paragraph, usually 2-4 sentences. Lead with the
  headline claim, then add mechanism, evidence, or context.
- Do not stack unrelated claims into one dense sentence. Do not split
  one mechanism across multiple tiny paragraphs.
- Use subheadings only when a page has at least three thematic groups.
- Wiki pages are summaries, not evidence. Use wikilinks for graph
  navigation, not as citations.

## Candidate Pages

- Before creating a new entity/topic page, check `CANDIDATE_PAGES`
  carefully, including each candidate's `aliases:` frontmatter.
- A page may already exist under a different surface form. Update that
  existing page instead of creating a duplicate.
- Candidate recall is bounded. If the source clearly warrants a page
  that is absent from candidates, create it; alias-uniqueness lint will
  catch duplicates outside the candidate window.
- When prose mentions an existing entity from `CANDIDATE_PAGES` or any
  declared alias, wikilink the mention. Use `[[stem|alias]]` when the
  visible form differs from the target title.

## Candidate Digests And Expansion

- Candidate pages may be shown as digests. If a digest contains a
  `<!-- digest: ... elided -->` marker and you intend to modify that
  page, emit an expand request first.
- Do not generate hunk context from a truncated digest. It will fail
  `git apply`.
- You may create new pages without expansion.
- Expansion is allowed at most once per ingest pass.

## Expanded Candidate Editing

- Some candidates are shown in full because you requested expansion.
  You may modify those pages directly.
- Candidates still shown only as truncated digests must remain
  unchanged.
- Preserve human-owned text outside `llm-zone`.

## Multi-Source Synthesis

- When a page has two or more distinct sources, use a rolling
  `### Synthesis` section followed by append-only
  `### From src:<id>#<label>` evidence sections.
- Rewrite `### Synthesis` on each ingest so it integrates all sources
  currently on the page.
- Edit an existing matching `### From ...` section in place; never
  duplicate the same source+anchor heading.
- Citations inside `### Synthesis` may combine sources. Citations
  inside an evidence section cite only that source.
- `### From src:<id>#<label>` is a section label, not a citation. Do
  not put bracketed `[src:...]` syntax in the heading.

## Candidate Updates And Conflicts

- Preserve existing claims unless the new source provides a clear
  correction.
- If the source contradicts an existing claim, keep both and insert an
  inline highlight near the affected line:
  `==CONFLICT: <new_source_id> claims X; existing from <old_source_id> says Y.==`
- Preserve existing page tags unless the new source changes the page's
  scope.

## Images

- Embed only images listed in `IMAGES`.
- Use Obsidian transclude syntax: `![[sources/<asset>.assets/<file>]]`.
  Markdown image syntax is forbidden in `llm-zone`.
- Put image embeds in the evidence section that cites the matching
  source. Never place source-specific images in `### Synthesis`.
- Never invent or modify captions. Use the caption text from `IMAGES`.
- If prose references a figure absent from `IMAGES`, cite the prose
  but omit the embed.

## Patch Retry

- This pass exists because the previous diff did not apply cleanly.
- Prefer smaller, conservative hunks with exact context from the shown
  candidate content.
- Do not broaden the edit scope to compensate for patch failure.
- Emit a raw unified diff only; no explanations.
