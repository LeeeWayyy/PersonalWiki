#!/usr/bin/env python3
"""Backup and restore backend/data/study.db."""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "backend" / "data" / "study.db"
DEFAULT_BACKUP_DIR = ROOT / "backups" / "study-db"


class StudyDbError(RuntimeError):
    pass


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise StudyDbError(f"could not allocate unique backup path under {path.parent}")


def sqlite_backup(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(src)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _safety_copy(db: Path, dst: Path) -> None:
    """Snapshot the current db before a restore overwrites it. Prefer a
    consistent sqlite backup, but fall back to a raw file copy so restoring
    *over* a corrupt/unreadable db (the case restore exists for) is never
    blocked by the safety step."""
    try:
        sqlite_backup(db, dst)
    except sqlite3.Error:
        dst.unlink(missing_ok=True)
        shutil.copy2(db, dst)


def backup(db: Path = DEFAULT_DB, backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path:
    db = db.expanduser().resolve(strict=False)
    if not db.is_file():
        raise StudyDbError(f"study db not found: {db}")
    out = _unique_path(backup_dir.expanduser().resolve(strict=False) / f"study-{_timestamp()}.db")
    sqlite_backup(db, out)
    return out


def restore(src: Path, db: Path = DEFAULT_DB, backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path | None:
    src = src.expanduser().resolve(strict=False)
    db = db.expanduser().resolve(strict=False)
    backup_dir = backup_dir.expanduser().resolve(strict=False)
    if not src.is_file():
        raise StudyDbError(f"backup file not found: {src}")
    db.parent.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    safety: Path | None = None
    if db.is_file():
        try:
            same_file = src.samefile(db)
        except OSError:
            same_file = src == db
        if same_file:
            raise StudyDbError("refusing to restore study db from itself")
        safety = _unique_path(backup_dir / f"pre-restore-{_timestamp()}.db")
        _safety_copy(db, safety)
    for suffix in ("-shm", "-wal"):
        (db.parent / f"{db.name}{suffix}").unlink(missing_ok=True)
    shutil.copy2(src, db)
    return safety


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup or restore backend/data/study.db")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("PW_STUDY_DB", DEFAULT_DB)))
    parser.add_argument("--backup-dir", type=Path, default=Path(os.environ.get("PW_BACKUP_DIR", DEFAULT_BACKUP_DIR)))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup")
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("backup_file", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "backup":
            out = backup(args.db, args.backup_dir)
            print(f"backup written: {out}")
        else:
            safety = restore(args.backup_file, args.db, args.backup_dir)
            if safety:
                print(f"current db copied to: {safety}")
            print(f"restored: {args.db}")
            print("restart the backend if it was running during restore")
    except StudyDbError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
