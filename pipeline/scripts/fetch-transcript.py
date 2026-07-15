#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Turn a YouTube/media URL or a local audio/video file into a timed lang reading
page: transcript via the transcript server (script_generation), audio blob for
in-page playback, then a normal `ingest.py --profile lang` run.

Usage:
    scripts/fetch-transcript.py <url-or-media-file> [--language ja]
                                [--no-diarize] [--name SLUG] [--out DIR]
                                [--no-ingest]

Steps:
  1. Grab the playback audio: yt-dlp -x (URLs) or the file itself / an
     ffmpeg -vn remux (local video). Also yields the display name for URLs.
  2. `transcript-remote <input> -f json` (submit → poll → JSON with segment +
     word timestamps). $TRANSCRIPT_SERVER/$TRANSCRIPT_TOKEN are its config;
     $TRANSCRIPT_REMOTE_CMD overrides the CLI (mirrors media-identity.py).
  3. Record the audio extension in the JSON's meta.audio_ext (the lang
     generator renders the <audio> bar from it) and write
     <out>/<name>.transcript.json.
  4. Run `ingest.py --profile lang` on that JSON (extract → LLM → timed page).
  5. Copy the audio to content/lang/sources/.media/<asset-stem>.<ext>
     (gitignored blob the committed page references relatively).

--no-ingest stops after step 3 — hand the JSON to ingest.py yourself later.
The <out> copies (default: cwd) are yours to delete once ingested.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _util import default_vault_root, sha256_of  # noqa: E402

TOOLING_ROOT = Path(__file__).resolve().parent.parent
LANG_ROOT = default_vault_root(TOOLING_ROOT) / "lang"
AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wav", ".flac"}


def load_backend_env() -> None:
    """Fall back to backend/.env for TRANSCRIPT_SERVER/TRANSCRIPT_TOKEN so a
    manual CLI run works without exporting anything. The backend loads that
    file itself; this only fills vars not already in the environment."""
    env_file = TOOLING_ROOT.parent / "backend" / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(TRANSCRIPT_[A-Z_]+)=(.*)$", line.strip())
        if m and m.group(2):
            os.environ.setdefault(m.group(1), m.group(2).strip("'\""))


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"fetch-transcript: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"fetch-transcript: $ {' '.join(argv)}", file=sys.stderr)
    return subprocess.run(argv, **kw)


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s))


def url_slug(u: str) -> str:
    """Filesystem-safe name from a URL, for when no video title is available
    (audio download skipped/failed). Mirrors the old source-identity naming,
    e.g. www.youtube.com-watch-v-cwzzkDwrPv4."""
    from urllib.parse import urlparse
    p = urlparse(u)
    raw = p.netloc + p.path + (f"-{p.query}" if p.query else "")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-") or "transcript"


def fetch_audio(inp: str, out_dir: Path, name: str | None) -> Path | None:
    """Local playback copy of the source's audio in out_dir, or None if it
    can't be produced. The transcript itself comes from the server (URL sent
    directly), so this audio is ONLY for the reading page's <audio> player —
    a failure downgrades to a playerless page, it does not sink the job.

    URL → yt-dlp downloads the audio-only stream directly (no `-x`/ffmpeg
    transcode; itag 140 m4a plays natively in the browser) and names it after
    the video title. Local audio file → copied as-is. Local video → ffmpeg
    audio remux if ffmpeg is present, else None."""
    if is_url(inp):
        tmpl = f"{name}.%(ext)s" if name else "%(title)s.%(ext)s"
        argv = ["yt-dlp", "-f", "bestaudio[ext=m4a]/bestaudio", "--no-simulate",
                "--no-update", "--print", "after_move:filepath",
                "-o", str(out_dir / tmpl)]
        if shutil.which("node"):  # yt-dlp needs a JS runtime for YouTube now
            argv += ["--js-runtimes", "node"]
        r = run(argv + [inp], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"fetch-transcript: yt-dlp could not fetch playback audio "
                  f"(reading page will have no player):\n{r.stderr.strip()[-600:]}",
                  file=sys.stderr)
            return None
        path = Path(r.stdout.strip().splitlines()[-1])
        return path if path.is_file() else None
    src = Path(inp).expanduser().resolve()
    if not src.is_file():
        die(f"not a file: {src}")
    stem = name or src.stem
    if src.suffix.lower() in AUDIO_EXTS:
        dest = out_dir / f"{stem}{src.suffix.lower()}"
        if src != dest:
            shutil.copyfile(src, dest)
        return dest
    if not shutil.which("ffmpeg"):
        print("fetch-transcript: ffmpeg not found — reading page will have no "
              "player (install ffmpeg to extract audio from video files).",
              file=sys.stderr)
        return None
    dest = out_dir / f"{stem}.m4a"  # video container → audio-only remux
    r = run(["ffmpeg", "-y", "-i", str(src), "-vn", "-c:a", "aac", str(dest)],
            capture_output=True, text=True)
    if r.returncode != 0:
        print(f"fetch-transcript: ffmpeg failed (no player):\n{r.stderr.strip()[-600:]}",
              file=sys.stderr)
        return None
    return dest


def fetch_transcript(inp: str, json_path: Path, language: str | None,
                     diarize: bool) -> None:
    client = shlex.split(os.environ.get("TRANSCRIPT_REMOTE_CMD", "transcript-remote"))
    argv = client + [inp, "-f", "json", "-o", str(json_path)]
    if language:
        argv += ["--language", language]
    if not diarize:
        argv += ["--no-diarize"]
    r = run(argv)
    if r.returncode != 0 or not json_path.is_file():
        die("transcript-remote failed (is the server up? $TRANSCRIPT_SERVER)")


def registered_asset_for(json_path: Path) -> Path:
    """The lang source asset ingest registered for this JSON, found by sha —
    the same identity source-identity.py deduplicates on."""
    sha = sha256_of(json_path)
    sources = LANG_ROOT / "sources"
    for sidecar in sorted(sources.glob("*.md")):
        text = sidecar.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"(?m)^sha256:\s*['\"]?([0-9a-f]{64})", text)
        if m and m.group(1) == sha:
            return sidecar.with_suffix("")
    die(f"no lang source sidecar carries sha {sha[:12]}… — did ingest succeed?")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("input", help="http(s) URL or local audio/video file")
    ap.add_argument("--language", default="ja", help="ASR language hint (default ja)")
    ap.add_argument("--no-diarize", dest="diarize", action="store_false", default=True)
    ap.add_argument("--name", default="", help="override the derived source name")
    ap.add_argument("--out", default=".", help="where the JSON + audio copies land (default cwd)")
    ap.add_argument("--no-ingest", action="store_true",
                    help="stop after writing the transcript JSON")
    args = ap.parse_args()

    load_backend_env()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    audio = fetch_audio(args.input, out_dir, args.name or None)
    if args.name:
        name = args.name
    elif audio is not None:
        name = audio.stem  # human-readable video title from yt-dlp
    elif is_url(args.input):
        name = url_slug(args.input)  # no audio → fall back to a URL slug
    else:
        name = Path(args.input).stem
    json_path = out_dir / f"{name}.transcript.json"

    fetch_transcript(args.input, json_path, args.language, args.diarize)

    # Record the playback extension so the generator renders the <audio> bar
    # on the very first ingest (before the blob is copied into .media/). Only
    # when audio was actually fetched — otherwise the page has no player.
    import json
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    doc.setdefault("meta", {})["source"] = doc.get("meta", {}).get("source") or args.input
    if audio is not None:
        doc["meta"]["audio_ext"] = audio.suffix.lstrip(".")
    json_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    print(f"fetch-transcript: wrote {json_path}", file=sys.stderr)

    if args.no_ingest:
        print(f"fetch-transcript: next: {TOOLING_ROOT}/ingest.py --profile lang "
              f"{shlex.quote(str(json_path))}", file=sys.stderr)
        return 0

    r = run(["uv", "run", str(TOOLING_ROOT / "ingest.py"), "--profile", "lang",
             str(json_path)])
    if r.returncode != 0:
        die(f"ingest failed; fix and re-run: ingest.py --profile lang "
            f"{shlex.quote(str(json_path))}")

    if audio is None:
        print("fetch-transcript: done — reading page committed (no audio player; "
              "see the yt-dlp/ffmpeg note above to enable synced playback).",
              file=sys.stderr)
        return 0

    asset = registered_asset_for(json_path)
    media_dir = LANG_ROOT / "sources" / ".media"
    media_dir.mkdir(parents=True, exist_ok=True)
    stem = asset.name[: -len(".transcript.json")]
    blob = media_dir / f"{stem}{audio.suffix.lower()}"
    shutil.copyfile(audio, blob)
    print(f"fetch-transcript: audio → {blob}", file=sys.stderr)
    print(f"fetch-transcript: done — reading page committed for {asset.name}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
