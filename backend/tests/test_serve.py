import re
from pathlib import Path

from app import serve


def _write_backend(root: Path, env_text: str = "") -> None:
    backend = root / "backend"
    backend.mkdir(parents=True)
    (backend / ".env.example").write_text(
        "PW_AUTH_TOKEN=\nPW_CONTENT_DIR=content\n",
        encoding="utf-8",
    )
    if env_text:
        (backend / ".env").write_text(env_text, encoding="utf-8")


def _write_content(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "marker.md").write_text("# marker\n", encoding="utf-8")


def test_configure_environment_loads_env_before_backend_import(tmp_path):
    _write_backend(
        tmp_path,
        "PW_AUTH_TOKEN=backend-token\nPW_CONTENT_DIR=backend-content\nPW_PORT=9876\n",
    )
    _write_content(tmp_path / "backend-content")
    env: dict[str, str] = {}

    config = serve.configure_environment(tmp_path, env)

    assert env["PW_AUTH_TOKEN"] == "backend-token"
    assert env["PW_CONTENT_DIR"] == str((tmp_path / "backend-content").resolve())
    assert env["PW_LLM_CMD_BASE_DIR"] == str((tmp_path / "backend").resolve())
    assert config.content_dir == (tmp_path / "backend-content").resolve()
    assert config.port == 9876
    assert config.host == "127.0.0.1"


def test_configure_environment_preserves_explicit_env(tmp_path):
    _write_backend(
        tmp_path,
        "PW_AUTH_TOKEN=backend-token\nPW_CONTENT_DIR=backend-content\nPW_PORT=9876\n",
    )
    explicit_content = tmp_path / "explicit-content"
    _write_content(explicit_content)
    env = {
        "PW_AUTH_TOKEN": "explicit-token",
        "PW_CONTENT_DIR": str(explicit_content),
        "PW_PORT": "9999",
        "PW_HOST": "127.0.0.2",
        "PW_LLM_CMD_BASE_DIR": "/tmp/llm-base",
    }

    config = serve.configure_environment(tmp_path, env)

    assert env["PW_AUTH_TOKEN"] == "explicit-token"
    assert env["PW_CONTENT_DIR"] == str(explicit_content.resolve())
    assert env["PW_LLM_CMD_BASE_DIR"] == "/tmp/llm-base"
    assert config.port == 9999
    assert config.host == "127.0.0.2"


def test_configure_environment_bootstraps_backend_env_token(tmp_path):
    _write_backend(tmp_path)
    env: dict[str, str] = {}

    config = serve.configure_environment(tmp_path, env)

    token = env["PW_AUTH_TOKEN"]
    assert token
    assert (tmp_path / "content" / ".git").is_dir()
    assert config.content_dir == (tmp_path / "content").resolve()
    assert re.search(rf"^PW_AUTH_TOKEN={re.escape(token)}$", (tmp_path / "backend" / ".env").read_text(), re.M)
    assert "created backend/.env" in config.messages
    assert "generated backend auth token in backend/.env" in config.messages
    assert any("created an empty wiki vault" in msg for msg in config.messages)


def test_configure_environment_rejects_missing_content(tmp_path):
    _write_backend(tmp_path, "PW_AUTH_TOKEN=token\nPW_CONTENT_DIR=missing\n")

    try:
        serve.configure_environment(tmp_path, {})
    except serve.ServeConfigError as exc:
        assert "wiki folder not found" in str(exc)
    else:
        raise AssertionError("missing content should fail backend startup")


def test_exec_uvicorn_forces_one_worker_after_user_arguments(monkeypatch, tmp_path):
    config = serve.RuntimeConfig(
        backend_dir=tmp_path,
        content_dir=tmp_path / "content",
        host="127.0.0.1",
        port=8787,
    )
    called = {}
    monkeypatch.setattr(serve.os, "chdir", lambda path: called.setdefault("cwd", path))
    monkeypatch.setattr(
        serve.os,
        "execvpe",
        lambda executable, argv, environ: called.update(
            executable=executable, argv=argv, environ=environ
        ),
    )

    serve.exec_uvicorn(config, ["--log-level", "debug"])

    assert called["cwd"] == tmp_path
    assert called["argv"][-4:] == ["--log-level", "debug", "--workers", "1"]
