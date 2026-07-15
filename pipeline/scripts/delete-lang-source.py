#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0", "fugashi", "unidic-lite"]
# ///
"""Delete ONE lang reader and commit the removal.

Removes the reader's committed artifacts under lang/:
  - its `_reading/<slug>.html` + `<slug>.reading.json` (matched by source_id, so
    it works for both source-backed slugs and merged-reader ids)
  - its `sources/<asset>` + `<asset>.md` sidecar, when the reader is source-backed
    (a merged reader has neither — only the two _reading pages)

then git-commits the deletion. The gitignored audio blob (.media/) and LLM cache
(.wiki/) are left as-is — not committed, harmless, reclaimed on the next sweep.
"""
import argparse, importlib.util, json, os, subprocess, sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
# Same descend as merge-lang-readers.py: the backend passes the git repo root
# (content/); glp needs the lang vault (content/lang) to resolve its dirs.
_cd = Path(os.environ.get("PW_CONTENT_DIR") or os.environ.get("VAULT_CONTENT_DIR") or ".").resolve()
if (_cd / "lang" / "sources").is_dir():
    os.environ["PW_CONTENT_DIR"] = str(_cd / "lang")
spec = importlib.util.spec_from_file_location("glp", SCRIPTS / "generate-language-pages.py")
glp = importlib.util.module_from_spec(spec); spec.loader.exec_module(glp)
import derived_lib as dl


def reading_files(source_id: str) -> list[Path]:
    """Every _reading/*.{reading.json,html} whose doc.source_id == source_id."""
    out: list[Path] = []
    if not glp.READING_DIR.is_dir():
        return out
    for jf in glp.READING_DIR.glob("*.reading.json"):
        try:
            doc = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("source_id") == source_id:
            out.append(jf)
            html = jf.with_name(jf.name[: -len(".reading.json")] + ".html")
            if html.exists():
                out.append(html)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-id", required=True)
    args = ap.parse_args()

    paths = reading_files(args.source_id)
    sources = dl.find_sources(glp.SOURCES_DIR)
    if args.source_id in sources:                    # source-backed: asset + sidecar
        asset = Path(sources[args.source_id]["asset"])
        for p in (asset, asset.with_name(asset.name + ".md")):
            if p.exists():
                paths.append(p)
    if not paths:
        sys.exit(f"no lang reader found for source_id {args.source_id}")

    repo = subprocess.check_output(
        ["git", "-C", str(glp.VAULT_ROOT), "rev-parse", "--show-toplevel"], text=True).strip()

    def tracked(p: Path) -> bool:
        return subprocess.run(
            ["git", "-C", repo, "ls-files", "--error-unmatch", str(p)],
            capture_output=True).returncode == 0

    # `git rm` stages tracked deletions; an untracked page (never committed) is
    # just unlinked. Committed artifacts drive the commit below.
    for p in paths:
        if tracked(p):
            subprocess.run(["git", "-C", repo, "rm", "-q", "--", str(p)], check=True)
        else:
            p.unlink(missing_ok=True)

    strs = [str(p) for p in paths]
    if subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet", "--", *strs]).returncode == 0:
        print(f"nothing to delete (no committed artifacts for {args.source_id})")
        return 0
    subprocess.run(
        ["git", "-C", repo, "-c", "user.email=merge@personal-wiki.local",
         "-c", "user.name=lang-merge", "commit", "-m",
         f"lang: delete {args.source_id}", "--", *strs], check=True)
    print(f"deleted lang reader {args.source_id} ({len(paths)} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
