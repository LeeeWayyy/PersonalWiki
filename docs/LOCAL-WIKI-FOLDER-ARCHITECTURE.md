# Local Wiki Folder Architecture

Goal: one app shell can be shared or deployed, while each user keeps a different
wiki folder on their own device.

## Core Decision

The wiki folder is the source of truth. The application code, generated build
artifacts, search index, extracted reader blocks, and private study database are
all separate from it.

```text
local wiki folder (PW_CONTENT_DIR)
  wiki/              # entity/topic/map/index Markdown
  sources/           # source assets + sidecars
  lang/              # language-learning source artifacts
  .wiki/             # pipeline logs/cache/index inputs

generated app cache
  vault/             # read-only snapshot used by Astro
  public/vault-assets/
  dist/
  public/pagefind/

runtime personal state
  backend/data/*.db  # annotations, study queue, FSRS state
```

In the current repo, `PW_CONTENT_DIR` points at the local wiki folder. If it is
unset, the fallback is `./content`, which is gitignored and can be populated with
`scripts/vendor_content.py`.

## Current Implementation

Today this is still a build-time Astro site:

```text
PW_CONTENT_DIR ──► scripts/sync_content.py ──► vault/
      │                                      └──► vault/.blocks/
      │
      └──► FastAPI backend writes/ingests/promotes into the same folder

vault/ ──► Astro build ──► dist/ + Pagefind
```

This works well for a local private deployment. It does not yet mean a public
hosted static site can read an arbitrary folder from a user's device. Browsers
block that unless the user grants folder access through browser APIs, or a local
companion/desktop app provides the filesystem bridge.

The path away from shell-script runtime glue is tracked separately in
[`SHELL-TO-LOCAL-APP-MIGRATION.md`](./SHELL-TO-LOCAL-APP-MIGRATION.md).

## Boundary Rules

- Application code must not treat repo-local `./content` as special. It is only
  the default local wiki folder for development.
- Build code reads from generated `./vault`, not directly from `PW_CONTENT_DIR`.
- Mutating operations write only through the backend or pipeline, and use the same
  `PW_CONTENT_DIR` as the build.
- Generated artifacts stay outside the source wiki folder unless they are part of
  the vault schema. `vault/.blocks` and `public/vault-assets` are disposable.
- Git is not required to read, build, or study the wiki — sync falls back to a
  plain worktree copy. It is only needed for ingest/promote, which use commits as
  the recovery boundary; if the folder is not a repo, ingest auto-initializes one
  (opt out with PW_INGEST_NO_AUTO_GIT=1).
- `backend/data/*.db` is personal runtime state, not wiki content. Back it up
  separately or design an explicit export/import path.

## Future Hosted App Shape

For the later deployed app, keep the UI as an app shell and swap the content
adapter:

```text
Hosted app shell
  ├── Browser folder adapter
  │     File System Access API; user picks a local folder explicitly
  ├── Local companion adapter
  │     Hosted app talks to http://127.0.0.1:<port>; companion owns filesystem
  └── Desktop adapter
        Tauri/Electron app embeds the same UI and owns filesystem access
```

The UI should depend on a content-source contract, not on Node `fs` directly:

- list pages/sources
- read page/source/asset by stable relative path
- write page or staged artifact when mutation is allowed
- provide derived indexes: aliases, backlinks, citations, source metadata
- trigger or observe rebuild/indexing work

The current `scripts/sync_content.py` plus `src/lib/vault.mjs` pair is the
build-time version of that contract. A future browser or companion adapter can
implement the same concepts at runtime.

## Recommended Phases

1. Keep the current local private build, but always configure the wiki via
   `PW_CONTENT_DIR` and keep generated `vault/` disposable.
2. Extract the content-source concepts from `src/lib/vault.mjs` into smaller
   modules so the UI stops caring whether content came from a build snapshot or a
   runtime adapter.
3. Add a local companion service only when the hosted app needs reliable
   cross-browser folder access and writes.
4. Consider Tauri when installable desktop reliability matters more than a
   zero-install browser workflow.
