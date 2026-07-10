export interface VaultPage {
  rel: string;
  slug: string;
  kind: string;
  title: string;
  href: string;
  data: Record<string, unknown>;
  body: string;
  aliases: string[];
  tags: string[];
  sources: string[];
  last_ingested: string | null;
}

export interface SourceMeta {
  id: string;
  title: string;
  origin_type: string;
  origin_ref: string | null;
  href: string;
  supersedes: string | string[] | null;
}

export interface ReadingToken {
  t: string;
  rt?: string;
  w?: string;
  m?: string;
  n?: string;
  pos?: string;
  key?: string;
  new?: boolean;
}

export interface ReadingSentence {
  jp: string;
  en: string;
  tokens: ReadingToken[];
  grammar?: Array<Record<string, string>>;
}

export interface ReadingParagraph {
  sentences: ReadingSentence[];
}

export interface ReadingChapter {
  chapter?: string;
  paragraphs?: ReadingParagraph[];
  sentences?: ReadingSentence[];
  grammar?: Array<Record<string, string>>;
}

export interface ReadingDoc {
  schema: string;
  source_id: string;
  title: string;
  lang: string;
  target_lang?: string;
  prompt_version?: string;
  chapters: ReadingChapter[];
}

export interface LangEntry {
  id: string;
  slug: string;
  title: string;
  vocab: Array<Record<string, string>>;
  grammar: Array<Record<string, string>>;
  word_count: number | null;
  token_count: number | null;
  grammar_count: number | null;
  chapter_count: number;
  chapters: Array<{ heading: string; text: string }>;
  reading: ReadingDoc | null;
}

export const cleanTitle: (t: string | null | undefined) => string;
export const enOf: (p: { aliases?: string[]; title?: string }) => string;
export function loadVault(): {
  pages: VaultPage[];
  sources: SourceMeta[];
  aliasMap: Map<string, string>;
  sourceMap: Map<string, SourceMeta>;
  backlinks: Map<string, Set<string>>;
  forward: Map<string, Set<string>>;
};
export function loadLang(): LangEntry[];
export function readingStats(reading: ReadingDoc | null | undefined): {
  word_count: number | null;
  token_count: number | null;
  grammar_count: number | null;
  chapter_count: number;
};

export interface SourceBlock {
  id: string;
  type: string;
  section_id: string;
  section: string;
  order: number;
  text: string;
  prev: string;
  next: string;
  src?: string;
  page?: number;
}
export interface BlocksDoc {
  source_id: string;
  title: string;
  lang: string;
  blocks: SourceBlock[];
}
export function blocksForSource(sourceId: string): BlocksDoc | null;
export function sourceHasBlocks(sourceId: string): boolean;
export function chaptersForSource(sourceId: string): Array<{
  id: string;
  index: number;
  title: string;
  section: string;
  section_id: string;
  first_block_id: string;
  block_count: number;
  blocks: SourceBlock[];
}>;
export function sourceReaderHref(sourceId: string, anchor?: string): string;

export interface Citation {
  wiki_href: string;
  wiki_title: string;
  anchor: string;
  excerpt: string;
  kind: string;
}
export function citationsForSource(sourceId: string): Citation[];

export function resolveWikilink(target: string): string | null;
export function resolveSource(id: string): SourceMeta | null;
export function pageNeighbors(href: string): Array<{ href: string; title: string; dir: 'in' | 'out' | 'both' }>;
