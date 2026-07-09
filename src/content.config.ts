import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';

// Heterogeneous vault frontmatter → no strict schema; we read fields defensively.
// generateId preserves the raw path (case + unicode). Astro's default id
// generation lowercases ASCII, which would build /wiki/entities/atp while every
// link (from vault.mjs) points at /wiki/entities/ATP — broken on case-sensitive
// hosts. Keeping the exact stem keeps page paths and links in lockstep.
const keepId = ({ entry }: { entry: string }) => entry.replace(/\.md$/, '');
const wiki = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './vault/wiki', generateId: keepId }),
});

export const collections = { wiki };
