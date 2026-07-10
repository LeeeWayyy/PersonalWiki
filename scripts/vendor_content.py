#!/usr/bin/env python3
"""Create or import a local wiki folder into the gitignored fallback ./content."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Seeded into a freshly-initialized content repo so regenerable caches never
# show as untracked. Without this, ingest leaves wiki/.alias-index.json (etc.)
# untracked under wiki/, and the backend preflight — which refuses any untracked
# wiki/ path — blocks every subsequent ingest. Mirrors the reference vault's
# .gitignore (regenerable caches + OS cruft + Obsidian workspace state).
DEFAULT_CONTENT_GITIGNORE = """\
# Regenerable alias index (rebuilt by scripts/alias-index.py build)
wiki/.alias-index.json

# LLM-derived caches (regenerable)
.wiki/mindmap-cache/
lang/.wiki/lang-cache/

# Downloaded/derived media blobs (large; re-fetchable from sources)
sources/.media/

# Obsidian local workspace state (keep vault structure, drop machine state)
.obsidian/workspace*
.obsidian/cache
.obsidian/app.json
.obsidian/appearance.json
.obsidian/core-plugins*
.obsidian/community-plugins*
.obsidian/graph.json
.obsidian/hotkeys.json

# OS cruft
.DS_Store
Thumbs.db
*.swp
*.swo
"""


class VendorContentError(RuntimeError):
    pass


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def reject_overlap(source: Path, dest: Path) -> None:
    source = _resolve(source)
    dest = _resolve(dest)
    if source == dest or source in dest.parents or dest in source.parents:
        raise VendorContentError(f"refusing unsafe import: source ({source}) overlaps destination ({dest})")


def _copy_tree(source: Path, dest: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {".git", ".DS_Store"}}

    shutil.copytree(source, dest, dirs_exist_ok=True, ignore=ignore, symlinks=True)


def _git_available() -> bool:
    return shutil.which("git") is not None


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def _display_dest(dest: Path, root: Path) -> str:
    if dest == root / "content":
        return "content/"
    return str(dest)


def init_git_snapshot(
    dest: Path,
    *,
    user_email: str,
    user_name: str,
    commit_message: str,
    allow_empty: bool = False,
) -> tuple[bool, str]:
    if not _git_available():
        return False, "git is not installed"
    # Seed the ignore rules BEFORE `git add -A` so regenerable caches (e.g.
    # wiki/.alias-index.json) are never tracked and never trip preflight later.
    # Guard on existence so we never clobber a vault's own .gitignore.
    gitignore = dest / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(DEFAULT_CONTENT_GITIGNORE, encoding="utf-8")
    for cmd in (["git", "-C", str(dest), "init", "-q"], ["git", "-C", str(dest), "add", "-A"]):
        res = _run(cmd)
        if res.returncode != 0:
            return False, (res.stderr or res.stdout or "").strip()
    has_staged = _run(["git", "-C", str(dest), "diff", "--cached", "--quiet"]).returncode != 0
    if not has_staged and allow_empty:
        return True, "empty"
    res = _run([
        "git",
        "-C",
        str(dest),
        "-c",
        f"user.email={user_email}",
        "-c",
        f"user.name={user_name}",
        "commit",
        "-qm",
        commit_message,
    ])
    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "").strip()
    return True, "committed"


def init_empty_content(
    dest: Path,
    *,
    user_email: str = "ingest@personal-wiki.local",
    user_name: str = "ingest",
) -> tuple[bool, str]:
    """Create a fresh, empty-but-valid content vault when none exists.

    Unlike import_content (which copies an existing source vault), this makes an
    empty one from nothing: the directory + a seeded .gitignore + a baseline git
    commit, so ingest can commit into it. The wiki structure itself
    (wiki/_taxonomy.md, entities/, topics/, …) is created by ingest.py's
    ensure_wiki_scaffold on the first run — no need to duplicate it here. Refuses
    a dest that already has files, so it can't clobber an existing vault.
    Returns (ok, message)."""
    dest = _resolve(dest)
    if dest.exists() and any(dest.iterdir()):
        return False, f"{dest} already exists and is not empty; not overwriting"
    dest.mkdir(parents=True, exist_ok=True)
    ok, detail = init_git_snapshot(
        dest,
        user_email=user_email,
        user_name=user_name,
        commit_message="init: empty wiki vault",
        allow_empty=True,
    )
    if not ok:
        return False, detail or "git init failed"
    return True, f"created an empty wiki vault at {dest}"


def _init_git(dest: Path) -> bool:
    ok, _detail = init_git_snapshot(
        dest,
        user_email="vendor@personal-wiki.local",
        user_name="vendor",
        commit_message="vendor: initial content snapshot",
    )
    return ok


def _build_alias_index(root: Path, dest: Path) -> bool:
    alias_index = root / "pipeline" / "scripts" / "alias-index.py"
    if not alias_index.is_file():
        return False
    env = dict(os.environ)
    env["VAULT_CONTENT_DIR"] = str(dest)
    if shutil.which("uv"):
        cmd = ["uv", "run", str(alias_index), "build"]
    else:
        cmd = [sys.executable, str(alias_index), "build"]
    return subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def import_content(source: Path, dest: Path = ROOT / "content", root: Path = ROOT) -> list[str]:
    source = _resolve(source)
    dest = _resolve(dest)
    root = root.resolve()
    display_dest = _display_dest(dest, root)
    messages: list[str] = []

    if (dest / ".git").exists():
        return [f"{display_dest} is already a git repo; to refresh: git -C {dest} pull"]
    if dest.is_dir() and any(dest.iterdir()):
        return [f"{display_dest} is already populated. Remove it first to re-vendor: rm -rf {dest}"]
    if not source.is_dir():
        raise VendorContentError(
            f"source not found: {source}\n"
            "Pass the path to your wiki content, for example:\n"
            "  python3 scripts/vendor_content.py /path/to/existing/wiki-content"
        )

    reject_overlap(source, dest)
    if (source / ".git").exists():
        if not _git_available():
            raise VendorContentError("git is required to clone a git source")
        messages.append(f"cloning {source} -> {display_dest} (git repo; ingest can commit into it)")
        res = _run(["git", "clone", str(source), str(dest)])
        if res.returncode != 0:
            raise VendorContentError((res.stderr or res.stdout).strip() or "git clone failed")
    else:
        messages.append(f"copying {source} -> {display_dest} (files only)")
        dest.mkdir(parents=True, exist_ok=True)
        _copy_tree(source, dest)
        if _init_git(dest):
            messages.append(f"initialized {display_dest} git with an initial commit")

    if _build_alias_index(root, dest):
        messages.append("built wiki/.alias-index.json")
    else:
        messages.append("alias-index build skipped")
    messages.append(f"{display_dest} populated. Run: npm run build")
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create ./content or import an existing wiki folder into it")
    parser.add_argument("source", nargs="?", type=Path, default=None)
    parser.add_argument("--dest", type=Path, default=ROOT / "content")
    args = parser.parse_args(argv)
    try:
        env_source = os.environ.get("PW_CONTENT_SOURCE")
        source = args.source or (Path(env_source) if env_source else None)
        if source is None:
            ok, message = init_empty_content(args.dest)
            if not ok:
                raise VendorContentError(message)
            print(message)
            return 0
        for message in import_content(source, args.dest):
            print(message)
    except VendorContentError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
