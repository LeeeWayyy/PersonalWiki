#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""Verify post-apply wiki quality against chapter-intelligence JSON."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from ingest_quality import Issue, PageInput, evaluate_quality


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intelligence", required=True, help="chapter-intelligence JSON path")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--section-label", required=True)
    parser.add_argument(
        "--modified",
        action="extend",
        nargs="+",
        metavar="WIKI_PATH",
        help="one or more modified wiki paths; the option may be repeated",
    )
    parser.add_argument(
        "--existing",
        action="extend",
        nargs="+",
        metavar="WIKI_PATH",
        help="unchanged candidate pages eligible for deterministic already-covered checks",
    )
    return parser


def _git_root() -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _baseline_text(path: Path, git_root: Path | None) -> str | None:
    if git_root is None:
        return None
    try:
        relative = path.resolve().relative_to(git_root).as_posix()
    except (OSError, ValueError):
        return None
    result = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        text=True,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def _read_inputs(modified_paths: list[str], existing_paths: list[str]) -> tuple[
    list[PageInput], list[Issue], list[Issue]
]:
    git_root = _git_root()
    pages: list[PageInput] = []
    errors: list[Issue] = []
    warnings: list[Issue] = []
    seen: set[str] = set()
    for disposition, raw_path in [
        *(("modified", path) for path in modified_paths),
        *(("existing", path) for path in existing_paths),
    ]:
        display = str(Path(raw_path))
        if display in seen:
            warnings.append(
                Issue("modified.duplicate", "duplicate --modified path ignored", display)
            )
            continue
        seen.add(display)
        path = Path(raw_path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(
                Issue("modified.unreadable", f"cannot read modified page: {exc}", display)
            )
            continue
        pages.append(
            PageInput(
                path=display,
                text=text,
                baseline_text=(
                    _baseline_text(path, git_root) if disposition == "modified" else text
                ),
                disposition=disposition,
            )
        )
    return pages, errors, warnings


def _load_intelligence(path: str) -> tuple[object, list[Issue]]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), []
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [
            Issue(
                "intelligence.unreadable",
                f"cannot load intelligence JSON: {exc}",
                str(Path(path)),
            )
        ]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    modified = args.modified or []
    existing = args.existing or []
    artifact, intelligence_errors = _load_intelligence(args.intelligence)
    pages, page_errors, warnings = _read_inputs(modified, existing)
    try:
        receipt = evaluate_quality(
            artifact,
            source_id=args.source_id,
            section_label=args.section_label,
            pages=pages,
            initial_errors=(*intelligence_errors, *page_errors),
            initial_warnings=warnings,
        )
    except Exception as exc:  # fail closed and preserve the JSON receipt contract
        receipt = {
            "schema": "ingest-quality-receipt/1",
            "ok": False,
            "source_id": args.source_id,
            "section_label": args.section_label,
            "errors": [
                {
                    "code": "gate.internal",
                    "message": f"quality gate failed closed: {type(exc).__name__}: {exc}",
                }
            ],
            "warnings": [issue.as_dict() for issue in warnings],
        }
    receipt["intelligence"] = str(Path(args.intelligence))
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
