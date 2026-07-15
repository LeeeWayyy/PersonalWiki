#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
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
import argparse
import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from _util import default_vault_root, split_frontmatter  # noqa: E402

TOOLING_ROOT = SCRIPTS.parent
INGEST = TOOLING_ROOT / "ingest.py"
LINT = SCRIPTS / "lint.py"
SOURCE_ID_RX = re.compile(r"[A-Za-z0-9_-]{1,64}")
SIDECAR_SOURCE_RX = re.compile(
    r"^source_id:\s*['\"]?([A-Za-z0-9_-]{1,64})['\"]?\s*(?:#.*)?$", re.MULTILINE,
)


def lang_root() -> Path:
    root = default_vault_root(TOOLING_ROOT)
    return root if root.name == "lang" else root / "lang"


@contextlib.contextmanager
def ingest_lock(vault: Path):
    lock = vault / ".wiki" / "ingest.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def preflight(repo: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(INGEST), "--preflight", "--profile", "lang"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PW_CONTENT_DIR": str(repo), "VAULT_CONTENT_DIR": str(repo)},
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "lang preflight failed").strip())
    try:
        report = json.loads(result.stdout)
        ok, message = report["ok"], report["message"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"lang preflight returned invalid JSON: {exc}") from exc
    if not ok:
        offending = ", ".join(report.get("offending") or [])
        raise RuntimeError(f"{message}{': ' + offending if offending else ''}")


def reading_documents(vault: Path) -> list[tuple[Path, dict]]:
    docs: list[tuple[Path, dict]] = []
    reading_dir = vault / "_reading"
    if not reading_dir.is_dir():
        return docs
    for path in sorted(reading_dir.glob("*.reading.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot inspect {path.name}: {exc}") from exc
        if not isinstance(doc, dict):
            raise RuntimeError(f"cannot inspect {path.name}: expected a JSON object")
        docs.append((path, doc))
    return docs


def reading_files(source_id: str, docs: list[tuple[Path, dict]]) -> list[Path]:
    """Every _reading/*.{reading.json,html} whose doc.source_id == source_id."""
    out: list[Path] = []
    for jf, doc in docs:
        if doc.get("source_id") == source_id:
            out.append(jf)
            html = jf.with_name(jf.name[: -len(".reading.json")] + ".html")
            if html.exists():
                out.append(html)
    return out


def source_files(vault: Path, source_id: str) -> list[Path]:
    out: list[Path] = []
    for sidecar in sorted((vault / "sources").glob("*.md")):
        try:
            parts = split_frontmatter(sidecar.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        match = SIDECAR_SOURCE_RX.search(parts[1]) if parts else None
        if match and match.group(1) == source_id:
            asset = sidecar.with_suffix("")
            if asset.exists():
                out.append(asset)
            out.append(sidecar)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-id", required=True)
    args = ap.parse_args()
    if not SOURCE_ID_RX.fullmatch(args.source_id):
        sys.exit("source_id must contain only letters, numbers, '_' or '-' (max 64)")

    vault = lang_root().resolve()
    with ingest_lock(vault):
        repo = Path(subprocess.check_output(
            ["git", "-C", str(vault), "rev-parse", "--show-toplevel"], text=True,
            timeout=30,
        ).strip()).resolve()
        preflight(repo)
        docs = reading_documents(vault)
        dependents = [
            str(doc.get("source_id") or path.name.removesuffix(".reading.json"))
            for path, doc in docs
            if args.source_id in (doc.get("merged_from") or [])
        ]
        if dependents:
            sys.exit(
                f"source_id {args.source_id} is used by merged reader(s) "
                f"{', '.join(dependents)}; delete them first"
            )
        paths = list(dict.fromkeys([
            *reading_files(args.source_id, docs), *source_files(vault, args.source_id),
        ]))
        if not paths:
            sys.exit(f"no lang reader found for source_id {args.source_id}")

        rels = [p.relative_to(repo).as_posix() for p in paths]
        untracked = [
            rel for rel in rels
            if subprocess.run(
                ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", rel],
                capture_output=True, timeout=30,
            ).returncode != 0
        ]
        if untracked:
            sys.exit(f"refusing to delete uncommitted artifact(s): {', '.join(untracked)}")

        def terminate(signum, _frame):
            raise SystemExit(128 + signum)

        old_sigterm = signal.signal(signal.SIGTERM, terminate)
        try:
            try:
                subprocess.run(
                    ["git", "-C", str(repo), "rm", "-q", "--", *rels],
                    check=True, capture_output=True, text=True, timeout=30,
                )
                subprocess.run(
                    [str(LINT), "--profile", "lang"],
                    check=True, capture_output=True, text=True, timeout=120,
                    cwd=vault,
                    env={**os.environ, "PW_CONTENT_DIR": str(repo), "VAULT_CONTENT_DIR": str(repo)},
                )
                subprocess.run(
                    ["git", "-C", str(repo), "-c", "user.email=merge@personal-wiki.local",
                     "-c", "user.name=lang-merge", "commit", "-m",
                     f"lang: delete {args.source_id}", "--", *rels],
                    check=True, capture_output=True, text=True, timeout=30,
                )
            except BaseException as exc:
                detail = getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or str(exc)
                try:
                    status = subprocess.run(
                        ["git", "-C", str(repo), "-c", "core.quotepath=false", "status",
                         "--porcelain", "--untracked-files=all", "--", *rels],
                        check=True, capture_output=True, text=True, timeout=30,
                    )
                    if status.stdout.strip():
                        subprocess.run(
                            ["git", "-C", str(repo), "restore", "--source=HEAD", "--staged", "--worktree",
                             "--", *rels],
                            check=True, capture_output=True, text=True, timeout=30,
                        )
                except BaseException as rollback_exc:
                    rollback_detail = (
                        getattr(rollback_exc, "stderr", None)
                        or getattr(rollback_exc, "stdout", None)
                        or str(rollback_exc)
                    )
                    raise RuntimeError(
                        f"{str(detail).strip()}; rollback failed: {rollback_detail}"
                    ) from exc
                if isinstance(exc, (OSError, subprocess.SubprocessError)):
                    raise RuntimeError(str(detail).strip() or "git delete failed") from exc
                raise
        finally:
            signal.signal(signal.SIGTERM, old_sigterm)

    print(f"deleted lang reader {args.source_id} ({len(paths)} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
