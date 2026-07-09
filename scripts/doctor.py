#!/usr/bin/env python3
"""Local readiness checks for development, ingest, and private serving."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import app_config
import app_start
import check_runtime_external

ROOT = Path(__file__).resolve().parent.parent


class Reporter:
    def __init__(self) -> None:
        self.failed = False

    def ok(self, message: str) -> None:
        print(f"[ok] {message}")

    def warn(self, message: str) -> None:
        print(f"[warn] {message}")

    def fail(self, message: str) -> None:
        self.failed = True
        print(f"[fail] {message}")


def _run(cmd: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


def _node_version_ok(version: str) -> bool:
    try:
        return app_start.node_version_ok(app_start.parse_node_version(version))
    except app_start.AppStartError:
        return False


def _python_bin() -> str:
    return os.environ.get("PYTHON") or shutil.which("python3") or shutil.which("python") or ""


def _load_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(app_config.local_env_updates(root, env))
    return env


def run_doctor(root: Path = ROOT) -> int:
    root = root.resolve()
    reporter = Reporter()
    env = _load_env(root)

    node = shutil.which("node")
    if node:
        version = _run([node, "--version"], root).stdout.strip()
        if _node_version_ok(version):
            reporter.ok(f"Node {version}")
        else:
            reporter.fail(f"Node 22.12+ is required; found {version or 'unknown'}")
    else:
        reporter.fail("node is not installed")

    npm = shutil.which("npm")
    if npm:
        version = _run([npm, "--version"], root).stdout.strip()
        reporter.ok(f"npm {version}")
    else:
        reporter.fail("npm is not installed")

    py = _python_bin()
    if py:
        version = _run([py, "--version"], root).stdout.strip()
        if subprocess.run(
            [py, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"],
            check=False,
        ).returncode == 0:
            reporter.ok(version)
        else:
            reporter.fail(f"Python 3.11+ is required; found {version}")
        if subprocess.run([py, "-c", "import fastapi, pytest"], check=False).returncode == 0:
            reporter.ok("backend Python dependencies import")
        else:
            reporter.fail("backend Python dependencies are missing; run: pip install -r backend/requirements-dev.txt")
    else:
        reporter.fail("python3/python is not installed")

    if (root / "backend" / ".env").is_file():
        reporter.ok("backend/.env exists")
    else:
        reporter.fail("backend/.env is missing; run python3 scripts/app_start.py or python -m app.serve from backend/")

    if env.get("PW_AUTH_TOKEN"):
        reporter.ok("PW_AUTH_TOKEN is configured")
    else:
        reporter.fail("PW_AUTH_TOKEN is empty; run python3 scripts/app_start.py or python -m app.serve from backend/")

    content_dir = app_config.content_dir(root, env).expanduser().resolve(strict=False)
    if content_dir.is_dir() and any(content_dir.iterdir()):
        reporter.ok(f"wiki folder exists: {content_dir}")
        if _run(["git", "-C", str(content_dir), "rev-parse", "--git-dir"], root).returncode == 0:
            status = _run(["git", "-C", str(content_dir), "status", "--porcelain"], root)
            if status.returncode != 0:
                detail = (status.stderr or status.stdout).strip()
                reporter.fail(f"content git status failed: {detail}")
            elif status.stdout.strip():
                reporter.fail("content git tree is dirty; ingest preflight will block until committed/stashed")
                for line in status.stdout.splitlines():
                    print(f"       {line}")
            else:
                reporter.ok("content git tree is clean")
        else:
            reporter.warn("wiki folder is not a git repo; ingest cannot commit into it")
    else:
        reporter.fail(f"wiki folder is empty or missing: {content_dir}")

    unexpected = check_runtime_external.scan_runtime_external(root)
    if unexpected:
        reporter.fail("runtime external URL scan failed; run npm run check:external for details")
    else:
        reporter.ok("runtime external URL scan passed")

    if (root / "backend" / "data" / "study.db").is_file():
        reporter.ok("study database exists")
    else:
        reporter.warn("study database does not exist yet; backend will create it on first use")

    if reporter.failed:
        print("doctor: one or more checks failed", file=sys.stderr)
        return 1
    print("doctor: all checks passed")
    return 0


def main() -> int:
    return run_doctor()


if __name__ == "__main__":
    raise SystemExit(main())
