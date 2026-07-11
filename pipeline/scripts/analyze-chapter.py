#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Analyze one extracted source section into chapter-intelligence JSON.

Integration example:

    analyze-chapter.py \
      --text-file /tmp/chapter.txt \
      --source-id 01K00000000000000000000000 \
      --source-sha256 <sha256> \
      --section-label "Chapter 2" \
      --chapter-outline-json '["Chapter 1", "Chapter 2"]' \
      --cache-dir content/.wiki/chapter-intelligence-cache \
      --output /tmp/chapter-intelligence.json

The validated artifact is written atomically to --output. Stdout is reserved
for one completion-status line; provider progress remains on stderr through the
shared llm_client.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import chapter_intelligence as ci


def _chapter_outline(raw: str | None) -> list[str]:
    # Per-item length and duplicate checks live in chapter_intelligence
    # (discover_prior_spines / analysis_context); their ValueError reaches the
    # same handler in main().
    if raw is None:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--chapter-outline-json is not valid JSON: {exc.msg}") from exc
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ValueError("--chapter-outline-json must be a JSON array of strings")
    return value


def _timeout_from_env() -> int:
    raw = os.environ.get("PW_ANALYZE_TIMEOUT_S", str(ci.DEFAULT_TIMEOUT_S))
    try:
        timeout = int(raw)
    except ValueError as exc:
        raise ValueError("PW_ANALYZE_TIMEOUT_S must be a positive integer") from exc
    if timeout <= 0:
        raise ValueError("PW_ANALYZE_TIMEOUT_S must be a positive integer")
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a validated chapter-intelligence/1 artifact."
    )
    parser.add_argument(
        "--text-file",
        required=True,
        help="UTF-8 extracted section text, or - to read stdin.",
    )
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument(
        "--section-label",
        required=True,
        help="Exact section label; pass an empty string for a whole-source run.",
    )
    parser.add_argument(
        "--model",
        help="Analyzer model override (also honored via PW_ANALYZE_MODEL).",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Required path for the validated JSON artifact copy.",
    )
    parser.add_argument(
        "--cache-dir",
        help="Optional chapter-intelligence cache root.",
    )
    parser.add_argument(
        "--chapter-outline-json",
        help="Optional literal JSON array of ordered section-label strings.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.text_file == "-":
            text = sys.stdin.read()
        else:
            text = Path(args.text_file).read_text(encoding="utf-8")
        outline = _chapter_outline(args.chapter_outline_json)
        output = Path(args.output)
        if str(output) == "-":
            raise ValueError("--output must be a file path; stdout is status-only")

        identity, _ = ci.resolve_model_identity(args.model)
        prior_spines: list[dict] = []
        if args.cache_dir and outline:
            prior_spines = ci.discover_prior_spines(
                args.cache_dir,
                chapter_outline=outline,
                current_section_label=args.section_label,
                source_id=args.source_id,
                source_sha256=args.source_sha256,
                prompt_version=ci.PROMPT_VERSION,
                model_identity=identity,
                schema_ingest_sha256=ci.selected_schema_digest(),
                prompt_template_sha256=ci.prompt_template_identity(),
            )

        artifact = ci.analyze_chapter(
            text,
            source_id=args.source_id,
            source_sha256=args.source_sha256,
            section_label=args.section_label,
            ordered_sections=outline,
            prior_chapters=prior_spines,
            model=args.model,
            model_identity=identity,
            cache_dir=args.cache_dir,
            timeout_s=_timeout_from_env(),
        )
        ci.atomic_write_json(output, artifact)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"analyze-chapter: {exc}", file=sys.stderr)
        return 1

    print(
        "analyze-chapter: wrote "
        f"{output} ({len(artifact['claims'])} claims, "
        f"{len(artifact['entities'])} entities, "
        f"{len(artifact['topics'])} topics, "
        f"{len(prior_spines)} prior spines)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
