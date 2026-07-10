#!/usr/bin/env python3
"""Export the configured local wiki folder into the generated vault snapshot."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

import app_config

ROOT = Path(__file__).resolve().parent.parent


class SyncError(RuntimeError):
    pass


class UnsafePathError(SyncError):
    pass


@dataclass(frozen=True)
class SyncResult:
    source: Path
    vault: Path
    asset_dest: Path
    commit: str
    asset_count: int


def _log_default(message: str) -> None:
    print(message, flush=True)


def _effective_env(root: Path, env: Mapping[str, str] | None) -> dict[str, str]:
    base = dict(os.environ if env is None else env)
    base.update(app_config.local_env_updates(root, base))
    return base


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _resolve_existing_dir(path: Path) -> Path:
    if not path.is_dir():
        raise SyncError(f"sync: wiki folder is empty or missing: {path}")
    return path.resolve()


def _is_empty_dir(path: Path) -> bool:
    try:
        next(path.iterdir())
        return False
    except StopIteration:
        return True


def reject_nested_paths(source: Path, target: Path, label: str) -> None:
    if source == target or source in target.parents or target in source.parents:
        raise UnsafePathError(
            "sync: refusing unsafe path configuration: "
            f"wiki folder ({source}) overlaps {label} ({target}).\n"
            "      Set PW_CONTENT_DIR to the real source vault, not a generated output path."
        )


def _has_git_head(source: Path) -> bool:
    rev_parse = ["git", "-C", str(source), "rev-parse"]
    try:
        is_repo = subprocess.run(
            [*rev_parse, "--git-dir"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    if is_repo.returncode != 0:
        return False
    try:
        has_head = subprocess.run(
            [*rev_parse, "--verify", "HEAD"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return has_head.returncode == 0


def _git_short_head(source: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--short", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return "local"
    if res.returncode != 0:
        return "local"
    return res.stdout.strip() or "local"


def _safe_extract_tar(archive_file, dest: Path) -> None:
    with tarfile.open(fileobj=archive_file, mode="r:*") as archive:
        for member in archive.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise SyncError(f"sync: unsafe archive path: {member.name}")
        archive.extractall(dest)


def _copy_committed_snapshot(source: Path, dest: Path) -> None:
    with tempfile.TemporaryFile() as archive:
        subprocess.run(
            ["git", "-C", str(source), "archive", "HEAD"],
            stdout=archive,
            check=True,
        )
        archive.seek(0)
        _safe_extract_tar(archive, dest)


def _copy_worktree_snapshot(source: Path, dest: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {".git", ".DS_Store"}}

    shutil.copytree(source, dest, dirs_exist_ok=True, ignore=ignore, symlinks=True)


def _copy_asset_dirs(base: Path, asset_dest: Path, prefix: Path) -> int:
    if not base.is_dir():
        return 0
    count = 0
    for source_dir in sorted(p for p in base.rglob("*.assets") if p.is_dir()):
        rel = source_dir.relative_to(base)
        out = asset_dest / prefix / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, out, symlinks=True)
        count += 1
    return count


def _publish_asset_dirs(vault: Path, asset_dest: Path) -> int:
    shutil.rmtree(asset_dest, ignore_errors=True)
    asset_dest.mkdir(parents=True, exist_ok=True)
    return _copy_asset_dirs(vault / "sources", asset_dest, Path()) + _copy_asset_dirs(
        vault / "lang" / "sources",
        asset_dest,
        Path("lang"),
    )


def _write_sync_meta(vault: Path, commit: str, source: Path) -> None:
    payload = {
        "commit": commit,
        "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": str(source),
    }
    (vault / ".sync-meta.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _run_best_effort(
    cmd: list[str],
    env: Mapping[str, str],
    skipped_message: str,
    log: Callable[[str], None],
) -> None:
    res = subprocess.run(cmd, env=dict(env), check=False)
    if res.returncode != 0:
        log(skipped_message)


def _run_post_builders(root: Path, vault: Path, env: Mapping[str, str], log: Callable[[str], None]) -> None:
    builder_env = dict(env)
    builder_env["PW_VAULT"] = str(vault)

    build_blocks = root / "scripts" / "build-blocks.py"
    _run_best_effort([sys.executable, str(build_blocks)], builder_env, "sync: build-blocks skipped", log)


def sync_content(
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    run_post_build: bool = True,
    log: Callable[[str], None] = _log_default,
) -> SyncResult:
    root = root.resolve()
    effective_env = _effective_env(root, env)
    source_candidate = app_config.content_dir(root, effective_env)

    if not source_candidate.is_dir() or _is_empty_dir(source_candidate):
        raise SyncError(
            f"sync: wiki folder is empty or missing: {source_candidate}\n"
            "      Set PW_CONTENT_DIR=/abs/path/to/wiki in backend/.env, "
            "or run python3 scripts/vendor_content.py."
        )

    source = _resolve_existing_dir(source_candidate)
    vault = _resolve_path(root / "vault")
    asset_dest = _resolve_path(root / "public" / "vault-assets")

    reject_nested_paths(source, vault, "vault destination")
    reject_nested_paths(source, asset_dest, "asset destination")

    has_head = _has_git_head(source) and effective_env.get("PW_SYNC_WORKTREE", "0") != "1"

    log(f"sync: exporting wiki folder from {source} -> vault/")
    shutil.rmtree(vault, ignore_errors=True)
    vault.mkdir(parents=True, exist_ok=True)

    if has_head:
        _copy_committed_snapshot(source, vault)
        commit = _git_short_head(source)
    else:
        _copy_worktree_snapshot(source, vault)
        commit = "local"

    asset_count = _publish_asset_dirs(vault, asset_dest)
    if asset_count:
        log(f"sync: published {asset_count} asset folder(s) -> public/vault-assets/")

    _write_sync_meta(vault, commit, source)
    log(f"sync: done (wiki @ {commit})")

    if run_post_build:
        _run_post_builders(root, vault, effective_env, log)

    shutil.rmtree(root / ".astro", ignore_errors=True)
    return SyncResult(source=source, vault=vault, asset_dest=asset_dest, commit=commit, asset_count=asset_count)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: sync_content.py", file=sys.stderr)
        return 2
    try:
        sync_content()
    except SyncError as exc:
        print(exc, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"sync: command failed ({' '.join(exc.cmd)}): exit {exc.returncode}", file=sys.stderr)
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
