"""Pytest fixtures for the backend API tests.

Environment (auth token, a temp SQLite dir, a temp content git repo for promote)
is configured BEFORE the app is imported, since app.main / app.db / app.ingest_runner
read these at import time.
"""
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

TOKEN = "test-token-123"
_TMP = Path(tempfile.mkdtemp(prefix="pw-api-test-"))
_CONTENT = _TMP / "content"

# Configure the environment the app reads at import time.
os.environ["PW_AUTH_TOKEN"] = TOKEN
os.environ["PW_DATA_DIR"] = str(_TMP / "data")
os.environ["PW_CONTENT_DIR"] = str(_CONTENT)
os.environ.pop("LLM_CMD", None)          # ensure /assist + /translate degrade gracefully
os.environ.pop("PW_LLM_PROVIDER", None)
os.environ.pop("PW_CODEX_BIN", None)
os.environ.pop("PW_LLM_API_KEY", None)


def _init_content_repo() -> None:
    (_CONTENT / "wiki" / "entities").mkdir(parents=True, exist_ok=True)
    page = _CONTENT / "wiki" / "entities" / "ATP.md"
    page.write_text(
        "---\ntitle: ATP\n---\n# ATP\n\n<!-- llm-zone -->\nSynthesis.\n<!-- /llm-zone -->\n\n"
        "<!-- human-zone -->\n<!-- /human-zone -->\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(_CONTENT), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(_CONTENT), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(_CONTENT), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(_CONTENT), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(_CONTENT), "commit", "-q", "-m", "init"], check=True)


_init_content_repo()
_BASE_COMMIT = subprocess.check_output(
    ["git", "-C", str(_CONTENT), "rev-parse", "HEAD"], text=True
).strip()


@pytest.fixture(autouse=True)
def reset_content_repo():
    """Keep per-domain test files independent of mutations to the shared temp repo."""
    subprocess.run(
        ["git", "-C", str(_CONTENT), "reset", "--hard", _BASE_COMMIT],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(_CONTENT), "clean", "-fd"],
        check=True,
        stdout=subprocess.DEVNULL,
    )


@pytest.fixture(scope="session")
def token() -> str:
    return TOKEN


@pytest.fixture(scope="session")
def content_dir() -> Path:
    return _CONTENT


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


@pytest.fixture()
def auth(token):
    return {"X-Auth-Token": token}
