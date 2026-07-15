"""LLM command, assist, and translation route tests."""

import shlex
import subprocess
import sys
import time


def test_llm_command_timeout_kills_process_group(monkeypatch, tmp_path):
    from app import llm

    pidfile = tmp_path / "child.pid"
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "pidfile = sys.argv[1]\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        "open(pidfile, 'w').write(str(child.pid))\n"
        "sys.stdin.read()\n"
        "time.sleep(20)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "LLM_CMD",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {shlex.quote(str(pidfile))}",
    )

    try:
        llm.complete_command("prompt", timeout=0.5)
        assert False, "timeout did not raise"
    except RuntimeError as exc:
        assert "timed out" in str(exc)

    deadline = time.time() + 2
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert pidfile.exists()
    child_pid = pidfile.read_text(encoding="utf-8").strip()

    deadline = time.time() + 2
    while time.time() < deadline:
        ps = subprocess.run(["ps", "-p", child_pid, "-o", "stat="], capture_output=True, text=True)
        if ps.returncode != 0 or "Z" in ps.stdout:
            break
        time.sleep(0.05)
    assert ps.returncode != 0 or "Z" in ps.stdout



def test_assist_modes_and_validation(client, auth):
    for mode in ("explain", "summarize", "define"):
        r = client.post("/assist", json={"text": "线粒体是能量工厂", "mode": mode}, headers=auth)
        assert r.status_code == 200
        j = r.json()
        assert j["mode"] == mode and j.get("configured") is False  # no LLM in tests
    assert client.post("/assist", json={"text": "x", "mode": "bogus"}, headers=auth).status_code == 400
    assert client.post("/assist", json={"text": "", "mode": "explain"}, headers=auth).status_code == 400


def test_assist_cache_stores_mode_in_context(client, auth, monkeypatch, tmp_path):
    script = tmp_path / "llm_stub.py"
    script.write_text("import sys\nsys.stdin.read()\nprint('local assist')\n", encoding="utf-8")
    monkeypatch.setenv("LLM_CMD", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    monkeypatch.setenv("PW_LLM_MODEL", "test-local-model")

    r = client.post(
        "/assist",
        json={"text": "assist cache context test", "mode": "summarize", "lang": "Japanese"},
        headers=auth,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "local assist"
    assert body["mode"] == "summarize"
    assert body["cached"] is False

    cached = client.post(
        "/assist",
        json={"text": "assist cache context test", "mode": "summarize", "lang": "Japanese"},
        headers=auth,
    ).json()
    assert cached["cached"] is True
    assert cached["mode"] == "summarize"

    from app import db
    conn = db.connect()
    row = conn.execute(
        "SELECT context,lang,prompt_version,llm_provider,llm_model FROM translations WHERE translation=?",
        ("local assist",),
    ).fetchone()
    conn.close()
    assert dict(row) == {
        "context": "summarize",
        "lang": "Japanese",
        "prompt_version": "assist:v1",
        "llm_provider": "command",
        "llm_model": "test-local-model",
    }


def test_assist_empty_llm_output_is_not_cached(client, auth, monkeypatch):
    from app.routers import llm as llm_routes

    outputs = ["", "later assist"]
    monkeypatch.setattr(llm_routes.llm_client, "configured", lambda: True)
    monkeypatch.setattr(llm_routes.llm_client, "identity", lambda: {"provider": "test", "model": "empty-cache"})
    monkeypatch.setattr(llm_routes.llm_client, "complete", lambda *_args, **_kwargs: outputs.pop(0))

    first = client.post(
        "/assist",
        json={"text": "empty assist cache poison test", "mode": "define", "lang": "English"},
        headers=auth,
    ).json()
    assert first["result"] == "(no output)"
    assert first["cached"] is False

    second = client.post(
        "/assist",
        json={"text": "empty assist cache poison test", "mode": "define", "lang": "English"},
        headers=auth,
    ).json()
    assert second["result"] == "later assist"
    assert second["cached"] is False

    third = client.post(
        "/assist",
        json={"text": "empty assist cache poison test", "mode": "define", "lang": "English"},
        headers=auth,
    ).json()
    assert third["result"] == "later assist"
    assert third["cached"] is True


def test_translate_graceful_without_llm(client, auth):
    r = client.post("/translate", json={"text": "hello"}, headers=auth)
    assert r.status_code == 200
    assert r.json().get("configured") is False


def test_translate_prefers_local_llm_command(client, auth, monkeypatch, tmp_path):
    script = tmp_path / "llm_stub.py"
    script.write_text("import sys\nsys.stdin.read()\nprint('local translation')\n", encoding="utf-8")
    monkeypatch.setenv("LLM_CMD", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    monkeypatch.setenv("PW_LLM_API_ENABLED", "1")
    monkeypatch.setenv("PW_LLM_API_KEY", "unused-api-key")
    monkeypatch.setenv("PW_LLM_MODEL", "test-local-model")

    r = client.post("/translate", json={"text": "command preference test", "lang": "ja"}, headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["translation"] == "local translation"
    assert body["target_lang"] == "Simplified Chinese"
    assert body["prompt_version"] == "translate:v1"
    assert body["llm_provider"] == "command"
    assert body["llm_model"] == "test-local-model"

    cached = client.post("/translate", json={"text": "command preference test"}, headers=auth).json()
    assert cached["cached"] is True
    assert cached["target_lang"] == "Simplified Chinese"
    assert cached["prompt_version"] == "translate:v1"
    assert cached["llm_provider"] == "command"
    assert cached["llm_model"] == "test-local-model"

    from app import db
    conn = db.connect()
    row = conn.execute(
        "SELECT context,lang,prompt_version,llm_provider,llm_model FROM translations WHERE translation=?",
        ("local translation",),
    ).fetchone()
    conn.close()
    assert dict(row) == {
        "context": "translate",
        "lang": "Simplified Chinese",
        "prompt_version": "translate:v1",
        "llm_provider": "command",
        "llm_model": "test-local-model",
    }


def test_translate_empty_llm_output_is_not_cached(client, auth, monkeypatch):
    from app.routers import llm as llm_routes

    outputs = [None, "later translation"]
    monkeypatch.setattr(llm_routes.llm_client, "configured", lambda: True)
    monkeypatch.setattr(llm_routes.llm_client, "identity", lambda: {"provider": "test", "model": "empty-cache"})
    monkeypatch.setattr(llm_routes.llm_client, "complete", lambda *_args, **_kwargs: outputs.pop(0))

    first = client.post("/translate", json={"text": "empty translate cache poison test"}, headers=auth).json()
    assert first["translation"] == "(no output)"
    assert first["cached"] is False

    second = client.post("/translate", json={"text": "empty translate cache poison test"}, headers=auth).json()
    assert second["translation"] == "later translation"
    assert second["cached"] is False

    third = client.post("/translate", json={"text": "empty translate cache poison test"}, headers=auth).json()
    assert third["translation"] == "later translation"
    assert third["cached"] is True


def test_api_key_requires_explicit_enable(client, auth, monkeypatch):
    monkeypatch.delenv("LLM_CMD", raising=False)
    monkeypatch.delenv("PW_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("PW_LLM_API_ENABLED", raising=False)
    monkeypatch.setenv("PW_LLM_API_KEY", "unused-api-key")

    r = client.post("/translate", json={"text": "api disabled test"}, headers=auth)

    assert r.status_code == 200
    assert r.json().get("configured") is False
