"""Route tests for the personal-wiki backend.

Covers the health probe, fail-closed private-route auth, annotation CRUD,
image-region round-trip, promote-to-human-zone (into the temp content git repo),
study/job read auth, and the AI-assist / translate graceful-degradation paths
(no LLM configured).
"""
import asyncio
import datetime as dt
import json
import os
import signal
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["auth"] is True
    assert "content" in body  # regression: /health used to reference a removed attr


def test_db_connection_pragmas_and_indexes():
    from app import db

    conn = db.connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        indexes = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_items_state_due" in indexes
        assert "idx_reviews_item" in indexes
    finally:
        conn.close()


def test_db_schema_initializes_once_per_database(monkeypatch, tmp_path):
    from app import db

    test_db = tmp_path / "study.db"
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", test_db)

    conn = db.connect()
    conn.close()

    monkeypatch.setattr(
        db, "_MIGRATIONS", ["ALTER TABLE missing_table ADD COLUMN bad TEXT"]
    )

    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        conn.close()


def test_db_migrates_legacy_reviews_table_to_foreign_key(monkeypatch, tmp_path):
    from app import db

    legacy_db = tmp_path / "study.db"
    raw = sqlite3.connect(legacy_db)
    raw.executescript(
        """
        CREATE TABLE items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL DEFAULT 'word',
          norm_key TEXT NOT NULL,
          lemma TEXT NOT NULL,
          reading TEXT,
          pos TEXT,
          gloss TEXT,
          example TEXT,
          source_id TEXT,
          anchor TEXT,
          status TEXT NOT NULL DEFAULT 'new',
          stability REAL NOT NULL DEFAULT 0,
          difficulty REAL NOT NULL DEFAULT 0,
          state INTEGER NOT NULL DEFAULT 0,
          reps INTEGER NOT NULL DEFAULT 0,
          lapses INTEGER NOT NULL DEFAULT 0,
          due TEXT,
          last_review TEXT,
          created TEXT NOT NULL
        );
        CREATE TABLE reviews (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          item_id INTEGER NOT NULL,
          grade INTEGER NOT NULL,
          reviewed TEXT NOT NULL,
          interval INTEGER
        );
        INSERT INTO items(id,kind,norm_key,lemma,created,due)
          VALUES(1,'word','word:legacy','legacy','2026-01-01T00:00:00Z','2026-01-02');
        INSERT INTO reviews(id,item_id,grade,reviewed,interval)
          VALUES(1,1,3,'2026-01-02T00:00:00Z',1),
                (2,99,3,'2026-01-02T00:00:00Z',1);
        """
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", legacy_db)

    conn = db.connect()
    try:
        assert conn.execute("PRAGMA foreign_key_list(reviews)").fetchone() is not None
        rows = conn.execute("SELECT item_id FROM reviews ORDER BY id").fetchall()
        assert [r[0] for r in rows] == [1]
        conn.execute("DELETE FROM items WHERE id=1")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    finally:
        conn.close()


def test_db_recovers_stranded_reviews_legacy(monkeypatch, tmp_path):
    from app import db

    legacy_db = tmp_path / "study.db"
    raw = sqlite3.connect(legacy_db)
    raw.executescript(
        """
        CREATE TABLE items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL DEFAULT 'word',
          norm_key TEXT NOT NULL,
          lemma TEXT NOT NULL,
          reading TEXT,
          pos TEXT,
          gloss TEXT,
          example TEXT,
          source_id TEXT,
          anchor TEXT,
          status TEXT NOT NULL DEFAULT 'new',
          stability REAL NOT NULL DEFAULT 0,
          difficulty REAL NOT NULL DEFAULT 0,
          state INTEGER NOT NULL DEFAULT 0,
          reps INTEGER NOT NULL DEFAULT 0,
          lapses INTEGER NOT NULL DEFAULT 0,
          due TEXT,
          last_review TEXT,
          created TEXT NOT NULL
        );
        CREATE TABLE reviews_legacy (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          item_id INTEGER NOT NULL,
          grade INTEGER NOT NULL,
          reviewed TEXT NOT NULL,
          interval INTEGER
        );
        INSERT INTO items(id,kind,norm_key,lemma,created,due)
          VALUES(1,'word','word:stranded','stranded','2026-01-01T00:00:00Z','2026-01-02');
        INSERT INTO reviews_legacy(id,item_id,grade,reviewed,interval)
          VALUES(1,1,3,'2026-01-02T00:00:00Z',1),
                (2,99,3,'2026-01-02T00:00:00Z',1);
        """
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", legacy_db)

    conn = db.connect()
    try:
        assert conn.execute("PRAGMA foreign_key_list(reviews)").fetchone() is not None
        rows = conn.execute("SELECT item_id FROM reviews ORDER BY id").fetchall()
        assert [r[0] for r in rows] == [1]
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews_legacy'"
        ).fetchone() is None
    finally:
        conn.close()


def test_db_migrates_translation_context(monkeypatch, tmp_path):
    from app import db

    legacy_db = tmp_path / "study.db"
    raw = sqlite3.connect(legacy_db)
    raw.executescript(
        """
        CREATE TABLE translations (
          text_hash TEXT PRIMARY KEY,
          lang TEXT,
          translation TEXT,
          prompt_version TEXT,
          llm_provider TEXT,
          llm_model TEXT,
          created TEXT
        );
        INSERT INTO translations(text_hash,lang,translation,prompt_version,created)
          VALUES('t1','Simplified Chinese','translated','translate:v1','2026-01-01T00:00:00Z'),
                ('a1','summarize','assisted','assist:v1','2026-01-01T00:00:00Z');
        """
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", legacy_db)

    conn = db.connect()
    try:
        rows = {
            r["text_hash"]: dict(r)
            for r in conn.execute(
                "SELECT text_hash,context,lang FROM translations ORDER BY text_hash"
            ).fetchall()
        }
        assert rows["t1"] == {
            "text_hash": "t1",
            "context": "translate",
            "lang": "Simplified Chinese",
        }
        assert rows["a1"] == {
            "text_hash": "a1",
            "context": "summarize",
            "lang": None,
        }
    finally:
        conn.close()


def test_private_routes_fail_closed_when_auth_token_unset(client, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "AUTH_TOKEN", "")
    assert client.get("/health").status_code == 200

    cases = [
        ("GET", "/health/llm", {}),
        ("POST", "/ingest", {"json": {"url": "https://example.com"}}),
        ("POST", "/ingest/sections", {"json": {"url": "https://example.com"}}),
        ("GET", "/jobs/missing", {}),
        ("GET", "/jobs/missing/events", {}),
        ("POST", "/jobs/missing/cancel", {}),
        ("GET", "/preflight", {}),
        ("POST", "/vocab", {"json": {"lemma": "mitochondria"}}),
        ("PATCH", "/vocab/1", {"json": {"status": "known"}}),
        ("GET", "/vocab", {}),
        ("GET", "/review/queue", {}),
        ("POST", "/review/1/grade", {"json": {"grade": 3}}),
        ("GET", "/review/stats", {}),
        ("GET", "/export", {}),
        ("GET", "/annotations?source_id=S", {}),
    ]
    for method, path, kwargs in cases:
        r = client.request(method, path, **kwargs)
        assert r.status_code == 503, f"{method} {path}: {r.status_code} {r.text}"
        assert "PW_AUTH_TOKEN" in r.text


def test_private_routes_reject_missing_or_bad_token(client):
    cases = [
        ("GET", "/health/llm", {}),
        ("POST", "/ingest", {"json": {"url": "https://example.com"}}),
        ("POST", "/ingest/sections", {"json": {"url": "https://example.com"}}),
        ("GET", "/jobs/missing", {}),
        ("GET", "/jobs/missing/events", {}),
        ("POST", "/jobs/missing/cancel", {}),
        ("GET", "/preflight", {}),
        ("POST", "/vocab", {"json": {"lemma": "mitochondria"}}),
        ("PATCH", "/vocab/1", {"json": {"status": "known"}}),
        ("GET", "/vocab", {}),
        ("GET", "/review/queue", {}),
        ("POST", "/review/1/grade", {"json": {"grade": 3}}),
        ("GET", "/review/stats", {}),
        ("GET", "/export", {}),
    ]
    for method, path, kwargs in cases:
        r = client.request(method, path, **kwargs)
        assert r.status_code == 401, f"missing token accepted for {method} {path}"
        r = client.request(method, path, headers={"X-Auth-Token": "wrong"}, **kwargs)
        assert r.status_code == 401, f"wrong token accepted for {method} {path}"

    assert client.get("/jobs/missing/events?token=wrong").status_code == 401


def test_study_and_ingest_read_routes_accept_auth(client, auth, token):
    r = client.post(
        "/vocab",
        json={"kind": "word", "lemma": "mitochondria", "gloss": "energy organelle"},
        headers=auth,
    )
    assert r.status_code == 200

    assert client.get("/vocab", headers=auth).status_code == 200
    assert client.get("/review/queue", headers=auth).status_code == 200
    assert client.get("/review/stats", headers=auth).status_code == 200
    assert client.get("/export", headers=auth).status_code == 200
    assert client.get("/preflight", headers=auth).status_code == 200

    from app import ingest_runner as ir

    job = ir.Job("authroutejob")
    job.emit("hello from job")
    job.emit("__END__")
    ir.JOBS[job.id] = job
    try:
        status = client.get(f"/jobs/{job.id}", headers=auth)
        assert status.status_code == 200
        assert status.json()["lines"][0] == "hello from job"

        event_by_header = client.get(f"/jobs/{job.id}/events", headers=auth)
        assert event_by_header.status_code == 200
        assert "hello from job" in event_by_header.text

        event_by_query = client.get(f"/jobs/{job.id}/events?token={token}")
        assert event_by_query.status_code == 401
    finally:
        ir.JOBS.pop(job.id, None)


def test_build_argv_leaves_document_chaptering_to_ingest():
    from app import ingest_runner as ir

    def argv(target, **opts):
        return ir._build_argv(target, opts or {})

    # Document-vs-URL chaptering is decided by ingest.py, which can inspect the
    # local file and shares the pipeline's format policy.
    default_epub = argv("/stage/book.epub")
    assert default_epub[-3:] == ["--limit", "0", "/stage/book.epub"]
    assert "--chapters" not in default_epub
    assert "--section" not in default_epub
    assert "--section-label" not in default_epub
    assert "--chapters" not in argv("/stage/book.MOBI")
    assert "--chapters" not in argv("/stage/book.azw3")
    assert "--chapters" not in argv("/stage/paper.pdf")
    assert "--chapters" not in argv("https://example.com/book.epub")
    selected = argv("/stage/book.epub", section_heading="第二章 (测试)")
    assert selected[-4:] == [
        "--section",
        r"^第二章\ \(测试\)$",
        "--section-label=第二章 (测试)",
        "/stage/book.epub",
    ]
    assert "--section-label=--- Prologue ---" in argv(
        "/stage/book.epub", section_heading="--- Prologue ---"
    )
    assert argv("/stage/book.epub", kind="wiki")[-1] == "/stage/book.epub"


def test_preflight_is_profile_aware_for_lang(client, auth, content_dir):
    dirty = content_dir / "lang" / "scratch.tmp"
    reading = content_dir / "lang" / "_reading" / "partial.md"
    dirty.parent.mkdir(parents=True, exist_ok=True)
    dirty.write_text("partial", encoding="utf-8")
    try:
        normal = client.get("/preflight?kind=auto", headers=auth)
        assert normal.status_code == 200
        assert dirty.relative_to(content_dir).as_posix() not in normal.json()["offending"]

        lang = client.get("/preflight?kind=lang", headers=auth)
        assert lang.status_code == 200
        assert lang.json()["ok"] is True

        reading.parent.mkdir(parents=True)
        reading.write_text("partial", encoding="utf-8")
        blocked = client.get("/preflight?kind=lang", headers=auth).json()
        assert blocked["ok"] is False
        assert "lang/_reading/partial.md" in blocked["offending"]
    finally:
        dirty.unlink(missing_ok=True)
        reading.unlink(missing_ok=True)


def test_preflight_allows_untracked_taxonomy_scaffold(client, auth, content_dir):
    # A run interrupted after ensure_wiki_scaffold leaves wiki/_taxonomy.md
    # untracked; the backend pre-check must tolerate it (ingest.py commits it)
    # while still blocking a genuine untracked page.
    taxonomy = content_dir / "wiki" / "_taxonomy.md"
    page = content_dir / "wiki" / "entities" / "Leftover.md"
    taxonomy.write_text("# Taxonomy\n", encoding="utf-8")
    try:
        ok = client.get("/preflight", headers=auth).json()
        assert ok["ok"] is True, ok
        assert "wiki/_taxonomy.md" not in ok["offending"]

        page.write_text("stray\n", encoding="utf-8")     # real leftover → must block
        blocked = client.get("/preflight", headers=auth).json()
        assert blocked["ok"] is False
        assert "wiki/entities/Leftover.md" in blocked["offending"]
        assert "wiki/_taxonomy.md" not in blocked["offending"]
    finally:
        taxonomy.unlink(missing_ok=True)
        page.unlink(missing_ok=True)


def test_preflight_allows_retryable_untracked_source_sidecar(client, auth, content_dir):
    sidecar = content_dir / "sources" / "failed-run.epub.md"
    sidecar.parent.mkdir()
    sidecar.write_text("retryable\n", encoding="utf-8")
    try:
        body = client.get("/preflight", headers=auth).json()
        assert body["ok"] is True, body
    finally:
        sidecar.unlink(missing_ok=True)


def test_preflight_reports_repo_wide_staged_index(client, auth, content_dir):
    staged = content_dir / "outside-watched-scope.txt"
    staged.write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(content_dir), "add", staged.name], check=True)
    try:
        body = client.get("/preflight", headers=auth).json()
        assert body["ok"] is False
        assert staged.name in body["offending"]
    finally:
        subprocess.run(
            ["git", "-C", str(content_dir), "reset", "--", staged.name],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        staged.unlink(missing_ok=True)


def test_preflight_reports_cjk_leftover_artifact(client, auth, content_dir):
    from app import ingest_runner as ir

    dirty = content_dir / "wiki" / "entities" / "漢字.failed"
    dirty.write_text("leftover", encoding="utf-8")
    try:
        ok, msg, offending = ir.preflight({"kind": "auto"})
        assert ok is False
        assert "Leftover artifacts" in msg
        assert "wiki/entities/漢字.failed" in offending
    finally:
        dirty.unlink(missing_ok=True)


def test_preflight_splits_rename_status_paths(client, auth, content_dir):
    from app import ingest_runner as ir

    old_rel = "wiki/entities/ATP.md"
    new_rel = "wiki/entities/ATP-renamed.md"
    try:
        subprocess.run(["git", "-C", str(content_dir), "mv", old_rel, new_rel], check=True)
        ok, _msg, offending = ir.preflight({"kind": "auto"})
        assert ok is False
        assert old_rel in offending
        assert new_rel in offending
    finally:
        subprocess.run(["git", "-C", str(content_dir), "reset", "--hard", "HEAD"], check=True, stdout=subprocess.DEVNULL)


def test_preflight_blocks_on_git_status_failure(client, auth, monkeypatch):
    from app import ingest_runner as ir

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 128, "", "fatal: unsafe repository")

    monkeypatch.setattr(ir.subprocess, "run", fake_run)
    r = client.get("/preflight", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "git status failed" in body["message"]
    assert "unsafe repository" in body["message"]


def test_ensure_content_git_auto_initializes_plain_folder(tmp_path, monkeypatch):
    from app import ingest_runner as ir

    monkeypatch.setattr(ir, "AUTO_INIT_GIT", True)
    content = tmp_path / "wiki"
    (content / "wiki").mkdir(parents=True)
    (content / "wiki" / "note.md").write_text("# note\n", encoding="utf-8")

    ok, msg = ir.ensure_content_git(content)

    assert ok is True
    assert "initialized" in msg
    assert (content / ".git").is_dir()
    head = subprocess.run(["git", "-C", str(content), "rev-parse", "HEAD"], capture_output=True, text=True)
    assert head.returncode == 0  # baseline commit exists → sync's `git archive HEAD` works
    # Idempotent: an existing repo is left alone with no message.
    ok2, msg2 = ir.ensure_content_git(content)
    assert ok2 is True and msg2 == ""


def test_ensure_content_git_blocks_when_opted_out(tmp_path, monkeypatch):
    from app import ingest_runner as ir

    monkeypatch.setattr(ir, "AUTO_INIT_GIT", False)
    content = tmp_path / "wiki"
    content.mkdir()

    ok, msg = ir.ensure_content_git(content)

    assert ok is False
    assert "not a git repo" in msg
    assert not (content / ".git").exists()


def test_job_logs_are_bounded(client, auth, monkeypatch):
    from app import ingest_runner as ir

    monkeypatch.setattr(ir, "JOB_LOG_LIMIT", 3)
    job = ir.Job("boundedjob")
    for i in range(5):
        job.emit(f"line {i}")
    job.emit("__END__")
    ir.JOBS[job.id] = job
    try:
        r = client.get(f"/jobs/{job.id}", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["dropped_lines"] == 3
        assert body["lines"] == [
            "... 3 earlier log line(s) truncated ...",
            "line 3",
            "line 4",
            "__END__",
        ]

        events = client.get(f"/jobs/{job.id}/events", headers=auth)
        assert events.status_code == 200
        assert "earlier log line(s) truncated" in events.text
        assert "line 3" in events.text
        assert "line 4" in events.text
    finally:
        ir.JOBS.pop(job.id, None)


def test_job_emit_persists_to_disk_and_keeps_ring_on_log_failure(tmp_path, monkeypatch):
    from app import ingest_runner as ir

    log_dir = tmp_path / "logs"
    monkeypatch.setattr(ir, "JOB_LOG_DIR", log_dir)
    job = ir.Job("persistjob")
    job.emit("first")
    job.emit("__END__")

    durable = (log_dir / "persistjob.log").read_text(encoding="utf-8").splitlines()
    assert durable[0].endswith(" first")
    assert durable[1].endswith(" __END__")
    assert job.visible_lines() == ["first", "__END__"]
    job.finish_terminal("done", {"status": "done"})
    assert job._log_fh is None

    blocked_parent = tmp_path / "not-a-dir"
    blocked_parent.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ir, "JOB_LOG_DIR", blocked_parent)
    broken = ir.Job("brokenjob")
    broken.emit("still live")

    assert broken.visible_lines() == ["still live"]
    assert broken.log_failed is True


def test_sse_data_splits_multiline_payloads():
    from app.routers.ingest import _sse_data

    assert _sse_data("one\ntwo") == "data: one\ndata: two\n\n"
    assert _sse_data("") == "data: \n\n"


def test_queued_job_log_handler_does_not_capture_active_job(tmp_path, monkeypatch):
    from app import ingest_runner as ir

    async def run():
        monkeypatch.setattr(ir, "JOB_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(ir, "STUB", True)
        monkeypatch.setattr(ir, "REBUILD_CMD", "")
        monkeypatch.setattr(ir, "ensure_content_git", lambda _content: (True, ""))
        monkeypatch.setattr(ir, "preflight", lambda _options: (True, "clean", []))

        first = ir.Job("firstjob")
        second = ir.Job("secondjob")
        first_task = asyncio.create_task(ir.run_job(first, "https://example.com/a", {"kind": "auto"}))
        while first.status != "running":
            await asyncio.sleep(0.01)
        second_task = asyncio.create_task(ir.run_job(second, "https://example.com/b", {"kind": "auto"}))
        await asyncio.sleep(0.05)
        ir.LOGGER.warning("active job marker job_id=firstjob")
        await asyncio.gather(first_task, second_task)
        return first, second

    first, second = asyncio.run(run())

    assert first.status == "done"
    assert second.status == "done"
    second_log = (tmp_path / "logs" / "secondjob.log").read_text(encoding="utf-8")
    assert "job_id=firstjob" not in second_log
    assert "target=https://example.com/b" in second_log


def test_start_job_keeps_task_reference(monkeypatch):
    from app import ingest_runner as ir

    async def fake_run_job(job, target, options):
        job.status = "done"
        job.result = {"status": "done", "target": target, "options": options}

    async def run():
        monkeypatch.setattr(ir, "run_job", fake_run_job)
        job_id = ir.start_job("https://example.com", {"kind": "auto"})
        job = ir.JOBS[job_id]
        try:
            assert job.task is not None
            await job.task
            assert job.status == "done"
            assert job.result["target"] == "https://example.com"
        finally:
            ir.JOBS.pop(job_id, None)

    asyncio.run(run())


def test_run_job_passes_run_id_uses_threads_and_removes_stage_upload(tmp_path, monkeypatch):
    from app import ingest_runner as ir
    from app import settings

    content = tmp_path / "content"
    content.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    slot = stage / "slot"
    slot.mkdir()
    upload = slot / "upload.txt"
    upload.write_text("uploaded", encoding="utf-8")
    script = tmp_path / "ingest_stub.py"
    script.write_text(
        "import os\n"
        "print('run-id=' + os.environ.get('PW_RUN_ID', ''))\n"
        "print('content-dir=' + os.environ.get('PW_CONTENT_DIR', ''))\n",
        encoding="utf-8",
    )
    main_thread = threading.get_ident()
    seen = {}

    def fake_ensure_content_git(content_arg):
        seen["ensure_thread"] = threading.get_ident()
        seen["content"] = content_arg
        return True, ""

    def fake_preflight(options):
        seen["preflight_thread"] = threading.get_ident()
        seen["options"] = options
        return True, "clean", []

    async def run():
        monkeypatch.setattr(settings, "STAGE_DIR", stage)
        monkeypatch.setattr(ir, "CONTENT_DIR", content)
        monkeypatch.setattr(ir, "INGEST_CMD", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
        monkeypatch.setattr(ir, "REBUILD_CMD", "")
        monkeypatch.setattr(ir, "STUB", False)
        monkeypatch.setattr(ir, "JOB_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(ir, "ensure_content_git", fake_ensure_content_git)
        monkeypatch.setattr(ir, "preflight", fake_preflight)
        job = ir.Job("runidjob")
        await ir.run_job(job, str(upload), {"kind": "auto"})
        return job

    job = asyncio.run(run())

    assert job.status == "done"
    assert job.result == {"status": "done"}
    assert not upload.exists()
    assert not slot.exists()
    assert f"run-id={job.id}" in job.visible_lines()
    assert f"content-dir={content}" in job.visible_lines()
    assert seen["content"] == content
    assert seen["options"] == {"kind": "auto"}
    assert seen["ensure_thread"] != main_thread
    assert seen["preflight_thread"] != main_thread
    assert "run-id=runidjob" in (tmp_path / "logs" / "runidjob.log").read_text(encoding="utf-8")


def test_idle_sleep_guard_wraps_only_on_macos_with_caffeinate(monkeypatch):
    from app import ingest_runner as ir

    argv = ["python3", "ingest.py", "book.epub"]
    monkeypatch.setattr(ir.sys, "platform", "darwin")
    monkeypatch.setattr(
        ir.shutil,
        "which",
        lambda name: "/usr/bin/caffeinate" if name == "caffeinate" else None,
    )
    guarded = ir._idle_sleep_guarded_argv(argv)
    assert guarded == ["/usr/bin/caffeinate", "-i", *argv]
    assert guarded is not argv
    assert argv == ["python3", "ingest.py", "book.epub"]

    monkeypatch.setattr(ir.sys, "platform", "linux")
    assert ir._idle_sleep_guarded_argv(argv) is argv


def test_idle_sleep_guard_falls_back_when_caffeinate_is_missing(monkeypatch):
    from app import ingest_runner as ir

    argv = ["python3", "ingest.py"]
    monkeypatch.setattr(ir.sys, "platform", "darwin")
    monkeypatch.setattr(ir.shutil, "which", lambda _name: None)
    assert ir._idle_sleep_guarded_argv(argv) is argv


def test_run_job_catches_unhandled_exception_ends_and_cleans_stage_upload(tmp_path, monkeypatch):
    from app import ingest_runner as ir
    from app import settings

    content = tmp_path / "content"
    content.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    upload = stage / "upload.txt"
    upload.write_text("uploaded", encoding="utf-8")

    def boom(_content):
        raise RuntimeError("unexpected preflight crash")

    async def run():
        monkeypatch.setattr(settings, "STAGE_DIR", stage)
        monkeypatch.setattr(ir, "CONTENT_DIR", content)
        monkeypatch.setattr(ir, "JOB_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(ir, "ensure_content_git", boom)
        job = ir.Job("crashjob")
        await ir.run_job(job, str(upload), {"kind": "auto"})
        return job

    job = asyncio.run(run())

    assert job.status == "error"
    assert job.result["status"] == "error"
    assert not upload.exists()
    assert job.visible_lines()[-1] == "__END__"
    durable = (tmp_path / "logs" / "crashjob.log").read_text(encoding="utf-8")
    assert "unexpected preflight crash" in durable
    assert "__END__" in durable


def test_run_job_cancel_after_process_assignment_kills_ingest(tmp_path, monkeypatch):
    from app import ingest_runner as ir

    content = tmp_path / "content"
    content.mkdir()
    script = tmp_path / "sleep.py"
    script.write_text("import time\nprint('started', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    launched = {}

    async def run():
        monkeypatch.setattr(ir, "CONTENT_DIR", content)
        monkeypatch.setattr(ir, "INGEST_CMD", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
        monkeypatch.setattr(ir, "REBUILD_CMD", "")
        monkeypatch.setattr(ir, "STUB", False)
        monkeypatch.setattr(ir, "ensure_content_git", lambda _content: (True, ""))
        monkeypatch.setattr(ir, "preflight", lambda _options: (True, "clean", []))
        original_create = asyncio.create_subprocess_exec

        async def create_and_cancel(*args, **kwargs):
            proc = await original_create(*args, **kwargs)
            launched["proc"] = proc
            launched["limit"] = kwargs.get("limit")
            job.cancel_requested = True
            return proc

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", create_and_cancel)
        job = ir.Job("cancelracejob")
        await ir.run_job(job, str(tmp_path / "target.txt"), {"kind": "auto"})
        return job

    job = asyncio.run(run())

    assert job.status == "canceled"
    assert job.result == {"status": "canceled"}
    assert launched["limit"] == 2**20
    assert launched["proc"].returncode is not None
    assert job.visible_lines()[-2:] == ["canceled by request", "__END__"]


def test_terminal_jobs_are_reaped(monkeypatch):
    from app import ingest_runner as ir

    monkeypatch.setattr(ir, "JOB_TTL_S", 60)
    job = ir.Job("oldjob")
    job.status = "done"
    job.updated_at = 100
    ir.JOBS[job.id] = job
    try:
        ir.reap_jobs(now=161)
        assert job.id not in ir.JOBS
    finally:
        ir.JOBS.pop(job.id, None)


def test_cancel_job_route_marks_queued_job_canceled(client, auth):
    from app import ingest_runner as ir

    job = ir.Job("cancelroutejob")
    ir.JOBS[job.id] = job
    try:
        r = client.post(f"/jobs/{job.id}/cancel", headers=auth)
        assert r.status_code == 200
        assert r.json()["status"] == "canceled"
        assert job.status == "canceled"
        assert job.result == {"status": "canceled"}
        assert job.visible_lines()[-2:] == ["canceled by request", "__END__"]
    finally:
        ir.JOBS.pop(job.id, None)


def test_cancel_job_kills_active_process_group(monkeypatch):
    from app import ingest_runner as ir

    async def run():
        monkeypatch.setattr(ir, "PROCESS_CLEANUP_TIMEOUT_S", 2)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            start_new_session=True,
        )
        job = ir.Job("cancelprocjob")
        job.status = "running"
        job.process = proc
        ir.JOBS[job.id] = job
        try:
            canceled = await ir.cancel_job(job.id)
            assert canceled is job
            assert job.status == "canceled"
            assert proc.returncode is not None
            assert job.visible_lines()[-2:] == ["canceled by request", "__END__"]
        finally:
            ir.JOBS.pop(job.id, None)

    asyncio.run(run())


def test_timeout_cleanup_kills_and_awaits_process_group(monkeypatch):
    from app import ingest_runner as ir

    async def run():
        monkeypatch.setattr(ir, "PROCESS_TERMINATE_GRACE_S", 2)
        monkeypatch.setattr(ir, "PROCESS_CLEANUP_TIMEOUT_S", 2)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            start_new_session=True,
        )
        job = ir.Job("killjob")
        await ir._kill_process_group(proc, job.emit, "test")
        assert proc.returncode is not None
        assert "did not exit" not in "\n".join(job.visible_lines())

    asyncio.run(run())


def test_timeout_cleanup_allows_sigterm_cleanup(monkeypatch, tmp_path):
    from app import ingest_runner as ir

    script = tmp_path / "term_cleanup.py"
    marker = tmp_path / "started"
    cleaned = tmp_path / "cleaned"
    script.write_text(
        "import pathlib, signal, sys, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        f"cleaned = pathlib.Path({str(cleaned)!r})\n"
        "marker.write_text('started', encoding='utf-8')\n"
        "def term(_sig, _frame):\n"
        "    cleaned.write_text('cleaned', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, term)\n"
        "while True:\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )

    async def run():
        monkeypatch.setattr(ir, "PROCESS_TERMINATE_GRACE_S", 2)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            start_new_session=True,
        )
        deadline = time.time() + 2
        while not marker.exists() and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert marker.exists()
        job = ir.Job("termcleanupjob")
        await ir._kill_process_group(proc, job.emit, "test")
        assert proc.returncode == 0
        assert cleaned.read_text(encoding="utf-8") == "cleaned"
        assert "SIGKILL" not in "\n".join(job.visible_lines())

    asyncio.run(run())


def test_timeout_cleanup_escalates_to_sigkill(monkeypatch, tmp_path):
    from app import ingest_runner as ir

    marker = tmp_path / "stubborn-ready"

    async def run():
        monkeypatch.setattr(ir, "PROCESS_TERMINATE_GRACE_S", 0.1)
        monkeypatch.setattr(ir, "PROCESS_CLEANUP_TIMEOUT_S", 2)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import pathlib, signal, sys, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8'); "
            "time.sleep(30)",
            str(marker),
            start_new_session=True,
        )
        deadline = time.time() + 2
        while not marker.exists() and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert marker.exists()
        job = ir.Job("stubbornkilljob")
        await ir._kill_process_group(proc, job.emit, "test")
        assert proc.returncode is not None
        assert "escalating to SIGKILL" in "\n".join(job.visible_lines())

    asyncio.run(run())


def test_timeout_cleanup_kills_ignoring_descendant_after_leader_exits(monkeypatch, tmp_path):
    from app import ingest_runner as ir

    child_ready = tmp_path / "child-ready"
    child_pid_file = tmp_path / "child-pid"
    parent_script = tmp_path / "parent.py"
    child_code = (
        "import pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid()), encoding='utf-8'); "
        "time.sleep(30)"
    )
    parent_script.write_text(
        "import pathlib, subprocess, sys, time\n"
        f"child_code = {child_code!r}\n"
        "child = subprocess.Popen([sys.executable, '-c', child_code, sys.argv[1]])\n"
        "pathlib.Path(sys.argv[2]).write_text(str(child.pid), encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )

    async def run():
        monkeypatch.setattr(ir, "PROCESS_TERMINATE_GRACE_S", 0.2)
        monkeypatch.setattr(ir, "PROCESS_CLEANUP_TIMEOUT_S", 3)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(parent_script),
            str(child_ready),
            str(child_pid_file),
            start_new_session=True,
        )
        deadline = time.time() + 3
        while (not child_ready.exists() or not child_pid_file.exists()) and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert child_ready.exists()
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        job = ir.Job("descendantkilljob")
        try:
            await ir._kill_process_group(proc, job.emit, "test")
            assert proc.returncode is not None
            assert "escalating to SIGKILL" in "\n".join(job.visible_lines())
            deadline = time.time() + 2
            while ir._process_group_exists(proc.pid) and time.time() < deadline:
                await asyncio.sleep(0.05)
            assert not ir._process_group_exists(proc.pid)
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    asyncio.run(run())


def test_ingest_upload_streams_to_stage_before_starting_job(client, auth, monkeypatch, tmp_path):
    from app import ingest_runner as ir
    from app import settings

    called = {}

    def fake_start_job(target, options):
        called["target"] = Path(target)
        called["options"] = options
        return "uploadjob"

    monkeypatch.setattr(settings, "STAGE_DIR", tmp_path)
    monkeypatch.setattr(ir, "start_job", fake_start_job)

    r = client.post(
        "/ingest",
        files={"file": ("note.txt", b"hello", "text/plain")},
        data={"options": json.dumps({"kind": "auto"})},
        headers=auth,
    )

    assert r.status_code == 200
    assert r.json()["job_id"] == "uploadjob"
    assert called["options"] == {
        "kind": "auto",
        "section_heading": None,
    }
    assert called["target"].name == "note.txt"
    assert called["target"].parent.parent == tmp_path
    assert called["target"].read_bytes() == b"hello"


def test_ingest_sections_lists_headings_and_cleans_staged_upload(client, auth, monkeypatch, tmp_path):
    from app import settings
    from app.routers import ingest as ingest_routes

    seen = {}

    async def fake_list_sections(target):
        seen["target"] = Path(target)
        seen["bytes"] = Path(target).read_bytes()
        return ["第一章 起点", "第二章 生命力"]

    monkeypatch.setattr(settings, "STAGE_DIR", tmp_path)
    monkeypatch.setattr(ingest_routes, "_list_sections", fake_list_sections)

    r = client.post(
        "/ingest/sections",
        files={"file": ("book.epub", b"fake-epub", "application/epub+zip")},
        headers=auth,
    )

    assert r.status_code == 200
    assert r.json() == {"sections": ["第一章 起点", "第二章 生命力"]}
    assert seen["bytes"] == b"fake-epub"
    assert list(tmp_path.iterdir()) == []  # staged copy removed after listing


def test_ingest_sections_url_mode_validates_scheme(client, auth, monkeypatch):
    from app.routers import ingest as ingest_routes

    async def fake_list_sections(target):
        return ["Chapter 1"]

    monkeypatch.setattr(ingest_routes, "_list_sections", fake_list_sections)

    ok = client.post("/ingest/sections", json={"url": "https://example.com/a"}, headers=auth)
    assert ok.status_code == 200
    assert ok.json() == {"sections": ["Chapter 1"]}

    bad = client.post("/ingest/sections", json={"url": "ftp://example.com/a"}, headers=auth)
    assert bad.status_code == 400


def test_list_sections_fetches_urls_with_source_identity_policy(monkeypatch, tmp_path):
    from app import settings
    from app.routers import ingest as ingest_routes

    calls = []

    async def fake_run(*argv):
        calls.append(argv)
        if "--fetch-only" in argv:
            Path(argv[-1]).write_text("downloaded", encoding="utf-8")
            return b""
        return b"Chapter 1\n"

    monkeypatch.setattr(settings, "STAGE_DIR", tmp_path)
    monkeypatch.setattr(ingest_routes, "_run_sections_tool", fake_run)
    sections = asyncio.run(ingest_routes._list_sections("https://example.com/book"))

    assert sections == ["Chapter 1"]
    assert calls[0][1:3] == ("--fetch-only", "https://example.com/book")
    assert calls[1][0] == str(ingest_routes._EXTRACT_SCRIPT)
    assert not list(tmp_path.iterdir())


def test_sections_tool_kills_process_group_when_canceled(monkeypatch):
    from app.routers import ingest as ingest_routes

    started = asyncio.Event()
    release = asyncio.Event()
    killed = []

    class Proc:
        pid = 123
        returncode = None

        async def communicate(self):
            started.set()
            await release.wait()

    async def fake_create(*_argv, **kwargs):
        assert kwargs["start_new_session"] is True
        return Proc()

    async def fake_kill(proc, emit, label):
        killed.append((proc.pid, emit, label))

    monkeypatch.setattr(ingest_routes.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(ingest_routes.ir, "_kill_process_group", fake_kill)

    async def run():
        task = asyncio.create_task(ingest_routes._run_sections_tool("tool"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert killed[0][0::2] == (123, "section listing")
    assert callable(killed[0][1])


def test_write_upload_offloads_disk_writes(monkeypatch, tmp_path):
    from app.routers import ingest as ingest_routes

    class FakeUpload:
        def __init__(self):
            self.chunks = [b"he", b"llo", b""]

        async def read(self, _size):
            return self.chunks.pop(0)

    calls = []

    async def fake_to_thread(fn, *args, **kwargs):
        calls.append(fn.__name__)
        return fn(*args, **kwargs)

    monkeypatch.setattr(ingest_routes.asyncio, "to_thread", fake_to_thread)
    dest = tmp_path / "upload.txt"

    total = asyncio.run(ingest_routes._write_upload(FakeUpload(), dest))

    assert total == 5
    assert dest.read_bytes() == b"hello"
    assert calls == ["write", "write"]


def test_ingest_upload_rejects_over_limit_and_removes_partial(client, auth, monkeypatch, tmp_path):
    from app import settings

    monkeypatch.setattr(settings, "STAGE_DIR", tmp_path)
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 4)

    r = client.post(
        "/ingest",
        files={"file": ("large.txt", b"abcde", "text/plain")},
        data={"options": "{}"},
        headers=auth,
    )

    assert r.status_code == 413
    assert "PW_MAX_UPLOAD_MB" in r.text
    assert list(tmp_path.iterdir()) == []


def test_ingest_options_validate_kind_and_section(client, auth, monkeypatch, tmp_path):
    from app import settings

    monkeypatch.setattr(settings, "STAGE_DIR", tmp_path)

    invalid = [
        {"url": "https://example.com", "options": {"kind": "bogus"}},
        {"url": "https://example.com", "options": {"kind": "auto", "section_label": ["bad"]}},
        {"url": "https://example.com", "options": {"kind": "auto", "section_label": "ch1"}},
        {"url": "https://example.com", "options": {"kind": "lang", "section_label": "ch1"}},
        {"url": "https://example.com", "options": {"kind": "lang", "section_heading": "ch1"}},
        {"url": "https://example.com", "options": {"kind": "video", "section_heading": "ch1"}},
        {"url": "https://example.com", "options": {"kind": "audio", "section_heading": "ch1"}},
        {"url": "https://example.com", "options": {"kind": "image_note", "section_heading": "ch1"}},
        {
            "url": "https://example.com",
            "options": {"kind": "wiki", "section_heading": "ch1", "section_label": "chapter one"},
        },
        {"url": "https://example.com", "options": {"kind": "wiki", "section_heading": "bad\nheading"}},
        {"url": "https://example.com", "options": {"kind": "wiki", "section_heading": "x" * 201}},
        {
            "url": "https://example.com",
            "options": {"kind": "wiki", "section_heading": "ch1", "section_label": "bad\u007flabel"},
        },
    ]
    for payload in invalid:
        r = client.post("/ingest", json=payload, headers=auth)
        assert r.status_code == 400

    upload = client.post(
        "/ingest",
        files={"file": ("note.txt", b"hello", "text/plain")},
        data={"options": json.dumps({"kind": "bogus"})},
        headers=auth,
    )
    assert upload.status_code == 400
    assert list(tmp_path.iterdir()) == []

    label_only_upload = client.post(
        "/ingest",
        files={"file": ("book.epub", b"book", "application/epub+zip")},
        data={"options": json.dumps({"kind": "auto", "section_label": "第二章"})},
        headers=auth,
    )
    assert label_only_upload.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_ingest_section_heading_accepts_200_characters(client, auth, monkeypatch):
    from app import ingest_runner as ir

    called = {}
    monkeypatch.setattr(
        ir,
        "start_job",
        lambda target, options: called.update(target=target, options=options) or "sectionjob",
    )
    heading = "章" * 200

    response = client.post(
        "/ingest",
        json={
            "url": "https://example.com/book",
            "options": {"kind": "wiki", "section_heading": heading},
        },
        headers=auth,
    )

    assert response.status_code == 200
    assert called["options"]["section_heading"] == heading

def test_ingest_section_heading_selects_and_labels_the_same_text(client, auth, monkeypatch):
    from app import ingest_runner as ir

    called = {}

    def fake_start_job(target, options):
        called["target"] = target
        called["options"] = options
        return "sectionjob"

    monkeypatch.setattr(ir, "start_job", fake_start_job)
    response = client.post(
        "/ingest",
        json={
            "url": "https://example.com/book",
            "options": {"kind": "auto", "section_heading": "第二章 (测试)"},
        },
        headers=auth,
    )

    assert response.status_code == 200
    assert called["options"] == {
        "kind": "auto",
        "section_heading": "第二章 (测试)",
    }
    assert ir._build_argv(called["target"], called["options"])[-4:] == [
        "--section",
        r"^第二章\ \(测试\)$",
        "--section-label=第二章 (测试)",
        "https://example.com/book",
    ]


def test_ingest_media_alias_maps_to_video_kind(client, auth, monkeypatch):
    from app import ingest_runner as ir

    called = {}

    def fake_start_job(target, options):
        called["target"] = target
        called["options"] = options
        return "mediajob"

    monkeypatch.setattr(ir, "start_job", fake_start_job)
    r = client.post(
        "/ingest",
        json={"url": "https://youtube.com/watch?v=x", "options": {"kind": "media"}},
        headers=auth,
    )

    assert r.status_code == 200
    assert called["options"]["kind"] == "video"
    argv = ir._build_argv("target", called["options"])
    assert argv[-3:] == ["--kind", "video", "target"]


def test_ingest_url_rejects_non_http_targets(client, auth, monkeypatch):
    from app import ingest_runner as ir

    calls = []

    def fake_start_job(target, options):
        calls.append((target, options))
        return "should-not-start"

    monkeypatch.setattr(ir, "start_job", fake_start_job)

    for target in ("ftp://example.com/file", "file:///tmp/source.txt", "example.com/source"):
        r = client.post("/ingest", json={"url": target, "options": {}}, headers=auth)
        assert r.status_code == 400
        assert "http:// or https://" in r.text

    assert calls == []


def test_json_routes_reject_malformed_or_non_object_payloads(client, auth):
    json_headers = {**auth, "Content-Type": "application/json"}

    malformed = client.post("/vocab", content="{", headers=json_headers)
    assert malformed.status_code == 400
    assert "valid JSON" in malformed.text

    for method, path in [
        ("POST", "/ingest"),
        ("POST", "/vocab"),
        ("PATCH", "/vocab/1"),
        ("POST", "/review/1/grade"),
        ("POST", "/translate"),
        ("POST", "/assist"),
        ("POST", "/annotations"),
        ("PATCH", "/annotations/an_missing"),
        ("POST", "/annotations/an_missing/promote"),
    ]:
        r = client.request(method, path, content="[]", headers=json_headers)
        assert r.status_code == 400, f"{method} {path}: {r.status_code} {r.text}"
        assert "JSON object" in r.text


def test_json_routes_reject_bad_field_shapes(client, auth, tmp_path, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "STAGE_DIR", tmp_path)

    cases = [
        client.post("/ingest", json={"url": "https://example.com", "options": []}, headers=auth),
        client.post("/vocab", json={"lemma": ["not", "a", "string"]}, headers=auth),
        client.post("/translate", json={"text": ["not", "a", "string"]}, headers=auth),
        client.post("/assist", json={"text": "hello", "lang": ["en"]}, headers=auth),
        client.post("/annotations", json={"source_id": "S", "target": []}, headers=auth),
        client.post("/annotations", json=_mk(quote={"bad": "shape"}, start=0, end=1), headers=auth),
        client.post("/annotations", json=_mk(quote="x", start="0", end=1), headers=auth),
        client.post("/annotations", json=_mk(quote="x", start=2, end=1), headers=auth),
    ]
    for r in cases:
        assert r.status_code == 400

    upload = client.post(
        "/ingest",
        files={"file": ("note.txt", b"hello", "text/plain")},
        data={"options": "[]"},
        headers=auth,
    )
    assert upload.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_mark_known_removes_vocab_item_from_review_queue(client, auth):
    add = client.post(
        "/vocab",
        json={"kind": "word", "lemma": "already-known-test-word", "gloss": "known"},
        headers=auth,
    )
    assert add.status_code == 200
    item_id = add.json()["id"]

    patch = client.patch(f"/vocab/{item_id}", json={"status": "known"}, headers=auth)
    assert patch.status_code == 200

    vocab = client.get("/vocab", headers=auth).json()
    item = next(i for i in vocab if i["id"] == item_id)
    assert item["status"] == "known"
    assert item["state"] == 1
    assert item["due"] is None

    queue = client.get("/review/queue", headers=auth).json()
    assert all(i["id"] != item_id for i in queue)


def test_review_after_mark_known_uses_first_review_path(client, auth):
    add = client.post(
        "/vocab",
        json={"kind": "word", "lemma": "known-then-reviewed-test-word", "gloss": "known"},
        headers=auth,
    )
    assert add.status_code == 200
    item_id = add.json()["id"]
    assert client.patch(f"/vocab/{item_id}", json={"status": "known"}, headers=auth).status_code == 200

    reviewed = client.post(f"/review/{item_id}/grade", json={"grade": 3}, headers=auth)

    assert reviewed.status_code == 200
    item = next(i for i in client.get("/vocab", headers=auth).json() if i["id"] == item_id)
    assert item["stability"] > 0
    assert item["reps"] == 1


def test_vocab_patch_can_clear_editor_fields(client, auth):
    add = client.post(
        "/vocab",
        json={
            "kind": "word",
            "lemma": "clear-fields-test-word",
            "reading": "old reading",
            "pos": "noun",
            "gloss": "old gloss",
            "example": "old example",
        },
        headers=auth,
    )
    assert add.status_code == 200
    item_id = add.json()["id"]

    patch = client.patch(
        f"/vocab/{item_id}",
        json={"reading": "", "pos": "", "gloss": "", "example": ""},
        headers=auth,
    )

    assert patch.status_code == 200
    item = next(i for i in client.get("/vocab", headers=auth).json() if i["id"] == item_id)
    assert item["reading"] == ""
    assert item["pos"] == ""
    assert item["gloss"] == ""
    assert item["example"] == ""


def test_duplicate_vocab_save_merges_new_context_without_resetting_schedule(client, auth):
    lemma = "context-merge-test-word"
    first = client.post(
        "/vocab",
        json={"kind": "word", "lemma": lemma, "gloss": "old gloss"},
        headers=auth,
    )
    assert first.status_code == 200
    item_id = first.json()["id"]
    assert client.patch(f"/vocab/{item_id}", json={"status": "known"}, headers=auth).status_code == 200

    second = client.post(
        "/vocab",
        json={
            "kind": "word",
            "lemma": lemma,
            "reading": "ctx",
            "pos": "noun",
            "gloss": "new gloss",
            "example": "new example",
            "source_id": "01SOURCE",
            "anchor": "p-1",
        },
        headers=auth,
    )
    assert second.status_code == 200
    assert second.json()["id"] == item_id

    item = next(i for i in client.get("/vocab", headers=auth).json() if i["id"] == item_id)
    assert item["status"] == "known"
    assert item["state"] == 1
    assert item["due"] is None
    assert item["reading"] == "ctx"
    assert item["pos"] == "noun"
    assert item["gloss"] == "new gloss"
    assert item["example"] == "new example"
    assert item["source_id"] == "01SOURCE"
    assert item["anchor"] == "p-1"


def test_review_grade_rejects_out_of_range_and_non_integer_values(client, auth):
    add = client.post(
        "/vocab",
        json={"kind": "word", "lemma": "grade-range-test-word", "gloss": "grade"},
        headers=auth,
    )
    assert add.status_code == 200
    item_id = add.json()["id"]

    for grade in (0, 5, "bad", "3", 2.0, True, False):
        r = client.post(f"/review/{item_id}/grade", json={"grade": grade}, headers=auth)
        assert r.status_code == 400

    ok = client.post(f"/review/{item_id}/grade", json={"grade": 3}, headers=auth)
    assert ok.status_code == 200


def test_review_again_on_mature_card_stays_due_today(client, auth):
    from app import db

    today = db.today().isoformat()
    conn = db.connect()
    try:
        cur = conn.execute(
            """INSERT INTO items(kind,norm_key,lemma,status,stability,difficulty,state,reps,lapses,due,last_review,created)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "word",
                "word:mature-again-test-word",
                "mature-again-test-word",
                "known",
                120.0,
                3.0,
                1,
                12,
                2,
                "2027-01-01",
                db.now_iso(),
                "2026-01-01T00:00:00Z",
            ),
        )
        item_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    r = client.post(f"/review/{item_id}/grade", json={"grade": 1}, headers=auth)

    assert r.status_code == 200
    assert r.json()["interval"] == 0
    assert r.json()["due"] == today
    item = next(i for i in client.get("/vocab", headers=auth).json() if i["id"] == item_id)
    assert item["status"] == "learning"
    assert item["due"] == today
    assert item["lapses"] == 3
    queue = client.get("/review/queue", headers=auth).json()
    assert any(i["id"] == item_id for i in queue)


def test_fsrs_first_review_again_does_not_increment_lapses():
    from app.fsrs import Card, schedule

    card, interval = schedule(Card(), 1, 0)

    assert card.reps == 1
    assert card.lapses == 0
    assert interval >= 1


def test_fsrs_again_never_increases_stability():
    from app.fsrs import Card, schedule

    original = Card(stability=1.0, difficulty=1.0, state=1, reps=4, lapses=0)
    card, _interval = schedule(original, 1, 365)

    assert card.stability <= original.stability
    assert card.lapses == 1


def test_review_elapsed_days_uses_local_date(monkeypatch, client, auth):
    from app import db
    from app.fsrs import Card
    from app.routers import study

    conn = db.connect()
    try:
        cur = conn.execute(
            """INSERT INTO items(kind,norm_key,lemma,status,stability,difficulty,state,reps,lapses,due,last_review,created)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "word",
                "word:local-date-elapsed",
                "local-date-elapsed",
                "learning",
                2.0,
                5.0,
                1,
                1,
                0,
                "2026-01-02",
                "2026-01-02T07:30:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        item_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    seen = {}

    def fake_schedule(card, grade, elapsed_days):
        seen["elapsed_days"] = elapsed_days
        return Card(stability=card.stability, difficulty=card.difficulty, state=1, reps=card.reps + 1, lapses=card.lapses), 2

    original_tz = os.environ.get("TZ")
    try:
        monkeypatch.setattr(db, "today", lambda: dt.date(2026, 1, 2))
        monkeypatch.setenv("TZ", "America/Los_Angeles")
        if hasattr(time, "tzset"):
            time.tzset()
        monkeypatch.setattr(study, "schedule", fake_schedule)

        r = client.post(f"/review/{item_id}/grade", json={"grade": 3}, headers=auth)

        assert r.status_code == 200
        assert seen["elapsed_days"] == 1
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        if hasattr(time, "tzset"):
            time.tzset()


def test_export_rejects_unsupported_formats(client, auth):
    assert client.get("/export?format=csv", headers=auth).status_code == 200
    r = client.get("/export?format=anki", headers=auth)
    assert r.status_code == 400
    assert "unsupported export format" in r.text


def test_llm_health_requires_local_command(client, auth, monkeypatch):
    monkeypatch.delenv("LLM_CMD", raising=False)
    monkeypatch.delenv("PW_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("PW_LLM_API_ENABLED", "1")
    monkeypatch.setenv("PW_LLM_API_KEY", "unused-api-key")

    r = client.get("/health/llm", headers=auth)

    assert r.status_code == 503
    assert "Local LLM command/provider is not configured" in r.json()["message"]


def test_llm_health_runs_command(client, auth, monkeypatch, tmp_path):
    script = tmp_path / "llm_health_stub.py"
    script.write_text("import sys\nsys.stdin.read()\nprint('ok')\n", encoding="utf-8")
    monkeypatch.setenv("LLM_CMD", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    monkeypatch.setenv("PW_LLM_MODEL", "test-local-model")

    r = client.get("/health/llm", headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "command"
    assert body["model"] == "test-local-model"
    assert body["matched_expected"] is True


def test_llm_health_runs_codex_provider(client, auth, monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "out = ''\n"
        "for i, arg in enumerate(args):\n"
        "    if arg == '-o' and i + 1 < len(args):\n"
        "        out = args[i + 1]\n"
        "if out:\n"
        "    pathlib.Path(out).write_text('ok\\n', encoding='utf-8')\n"
        "else:\n"
        "    print('ok')\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    monkeypatch.delenv("LLM_CMD", raising=False)
    monkeypatch.setenv("PW_LLM_PROVIDER", "codex")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    r = client.get("/health/llm", headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "codex"
    assert body["matched_expected"] is True


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


def test_annotations_fail_closed_and_authz(client, auth):
    # No token header at all → 401 (token IS configured in tests).
    assert client.get("/annotations?source_id=S").status_code == 401
    # Wrong token → 401.
    assert client.get("/annotations?source_id=S", headers={"X-Auth-Token": "nope"}).status_code == 401
    # Correct token → 200.
    assert client.get("/annotations?source_id=S", headers=auth).status_code == 200


def _mk(**sel):
    return {
        "source_id": "01SRC", "color": "note",
        "target": {"block_id": "p-abc", "section_id": "s-1",
                   "context": {"prev_block_id": "", "next_block_id": ""},
                   "selector": sel},
        "body": "",
    }


def test_annotation_crud(client, auth):
    payload = _mk(quote="hello world", prefix="", suffix="", start=0, end=11)
    r = client.post("/annotations", json=payload, headers=auth)
    assert r.status_code == 200
    a = r.json()
    aid = a["id"]
    assert a["target"]["selector"]["quote"] == "hello world"

    # list
    got = client.get("/annotations?source_id=01SRC", headers=auth).json()
    assert any(x["id"] == aid for x in got)

    # patch body + color
    p = client.patch(f"/annotations/{aid}", json={"body": "my note", "color": "important"}, headers=auth)
    assert p.status_code == 200 and p.json()["body"] == "my note" and p.json()["color"] == "important"

    # delete
    assert client.delete(f"/annotations/{aid}", headers=auth).status_code == 200
    assert client.delete(f"/annotations/{aid}", headers=auth).status_code == 404


def test_annotation_validation_rejects_unsafe_fields(client, auth):
    bad_color = _mk(quote="x", start=0, end=1)
    bad_color["color"] = 'bad" onclick="alert(1)'
    assert client.post("/annotations", json=bad_color, headers=auth).status_code == 400

    bad_tags = _mk(quote="x", start=0, end=1)
    bad_tags["tags"] = "not-a-list"
    assert client.post("/annotations", json=bad_tags, headers=auth).status_code == 400

    a = client.post("/annotations", json=_mk(quote="x", start=0, end=1), headers=auth).json()
    assert client.patch(f"/annotations/{a['id']}", json={"color": "bad"}, headers=auth).status_code == 400
    assert client.patch(f"/annotations/{a['id']}", json={"tags": ["ok", 3]}, headers=auth).status_code == 400

    unsafe_link = [{"type": "human-zone", "wiki_rel": "entities/ATP", "href": "javascript:alert(1)"}]
    assert client.patch(f"/annotations/{a['id']}", json={"links": unsafe_link}, headers=auth).status_code == 400

    unsupported_link = [{"type": "external", "wiki_rel": "entities/ATP", "href": "/wiki/entities/ATP"}]
    assert client.patch(f"/annotations/{a['id']}", json={"links": unsupported_link}, headers=auth).status_code == 400

    safe_link = [{"type": "human-zone", "wiki_rel": "entities/ATP", "href": "/wiki/entities/ATP"}]
    r = client.patch(f"/annotations/{a['id']}", json={"links": safe_link}, headers=auth)
    assert r.status_code == 200
    assert r.json()["links"] == safe_link

    bad_region = _mk(quote="", region={"x": 0.9, "y": 0.2, "w": 0.2, "h": 0.25})
    bad_region["target"]["block_id"] = "i-fig1"
    assert client.post("/annotations", json=bad_region, headers=auth).status_code == 400


def test_image_region_roundtrip(client, auth):
    payload = _mk(quote="", region={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25})
    payload["target"]["block_id"] = "i-fig1"
    payload["color"] = "important"
    a = client.post("/annotations", json=payload, headers=auth).json()
    region = a["target"]["selector"].get("region")
    assert region == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25}
    # survives a re-read
    got = client.get("/annotations?source_id=01SRC", headers=auth).json()
    match = next(x for x in got if x["id"] == a["id"])
    assert match["target"]["selector"]["region"]["w"] == 0.3


def test_promote_into_human_zone(client, auth, content_dir):
    payload = _mk(quote="线粒体 <script>", prefix="", suffix="", start=0, end=3)
    payload["body"] = "body <b>bold</b>\n<!-- /human-zone -->"
    a = client.post("/annotations", json=payload, headers=auth).json()
    r = client.post(f"/annotations/{a['id']}/promote",
                    json={"wiki_rel": "entities/ATP", "source_title": "Nick [Lane] <b>"}, headers=auth)
    assert r.status_code == 200
    res = r.json()
    assert res["ok"] and res["href"] == "/wiki/entities/ATP"
    page = (content_dir / "wiki" / "entities" / "ATP.md").read_text(encoding="utf-8")
    assert f"<!-- anno:{a['id']} -->" in page and "线粒体 &lt;script&gt;" in page
    assert "body &lt;b&gt;bold&lt;/b&gt;" in page
    assert "&lt;!-- /human-zone --&gt;" in page
    assert "[Nick \\[Lane\\] &lt;b&gt;]" in page
    assert "<script>" not in page and "<b>bold</b>" not in page
    assert page.count("<!-- /human-zone -->") == 1
    # the promotion is recorded on the annotation
    assert any(l.get("wiki_rel") == "entities/ATP" for l in res["annotation"]["links"])

    # idempotent: re-promoting updates in place (still one block)
    r2 = client.post(f"/annotations/{a['id']}/promote",
                     json={"wiki_rel": "entities/ATP", "source_title": "Nick Lane"}, headers=auth)
    assert r2.status_code == 200
    page2 = (content_dir / "wiki" / "entities" / "ATP.md").read_text(encoding="utf-8")
    assert page2.count(f"<!-- anno:{a['id']} -->") == 1


def test_human_zone_get_put_roundtrip(client, auth, content_dir):
    # shared session content repo: other tests may have written the zone already
    r = client.get("/wiki/human-zone?rel=entities/ATP", headers=auth)
    assert r.status_code == 200
    assert r.json()["exists"] is True and isinstance(r.json()["text"], str)

    r = client.put("/wiki/human-zone", json={"rel": "entities/ATP", "text": "my note\n\nsecond para"}, headers=auth)
    assert r.status_code == 200 and r.json()["ok"]
    page = (content_dir / "wiki" / "entities" / "ATP.md").read_text(encoding="utf-8")
    assert page.count("<!-- human-zone -->") == 1 and "my note" in page

    r = client.get("/wiki/human-zone?rel=entities/ATP", headers=auth)
    assert r.json() == {"wiki_rel": "entities/ATP", "text": "my note\n\nsecond para", "exists": True}

    # replace, not append; still one zone
    client.put("/wiki/human-zone", json={"rel": "entities/ATP", "text": "edited"}, headers=auth)
    page = (content_dir / "wiki" / "entities" / "ATP.md").read_text(encoding="utf-8")
    assert "my note" not in page and "edited" in page and page.count("<!-- human-zone -->") == 1

    # guardrails
    assert client.get("/wiki/human-zone?rel=../secrets", headers=auth).status_code == 400
    assert client.get("/wiki/human-zone?rel=entities/NOPE", headers=auth).status_code == 404
    assert client.put("/wiki/human-zone", json={"rel": "entities/ATP", "text": 5}, headers=auth).status_code == 400
    assert client.put("/wiki/human-zone", json={"rel": "entities/ATP", "text": "x"}).status_code in (401, 403)


def test_promote_replaces_note_with_regex_replacement_literals():
    from app import promote

    aid = "an_regex"
    page = (
        "# Page\n\n<!-- human-zone -->\n\n"
        "<!-- anno:an_regex -->\n> old\n<!-- /anno:an_regex -->\n\n"
        "<!-- /human-zone -->\n"
    )
    note = "<!-- anno:an_regex -->\n> body with \\1 and \\g<0>\n<!-- /anno:an_regex -->"

    updated = promote.insert_note(page, aid, note)

    assert "\\1 and \\g<0>" in updated
    assert updated.count("<!-- anno:an_regex -->") == 1


def test_promote_replacement_does_not_cross_into_other_annotation_blocks():
    from app import promote

    page = (
        "# Page\n\n<!-- human-zone -->\n\n"
        "<!-- anno:an_one -->\n"
        "Human text that must survive.\n\n"
        "<!-- anno:an_two -->\n> second\n<!-- /anno:an_two -->\n\n"
        "<!-- /human-zone -->\n"
    )
    note = "<!-- anno:an_one -->\n> replacement\n<!-- /anno:an_one -->"

    updated = promote.insert_note(page, "an_one", note)

    assert "Human text that must survive." in updated
    assert "<!-- anno:an_two -->" in updated
    assert "<!-- /anno:an_two -->" in updated
    assert "<!-- /anno:an_one -->" in updated


def test_promote_insert_requires_human_close_marker_at_line_start():
    from app import promote

    page = "# Page\n\n<!-- human-zone -->\nHuman text mentions <!-- /human-zone --> inline.\n"
    note = "<!-- anno:an_inline -->\n> replacement\n<!-- /anno:an_inline -->"

    with pytest.raises(ValueError, match="malformed human-zone close marker"):
        promote.insert_note(page, "an_inline", note)


def test_promote_takes_content_ingest_flock(monkeypatch, tmp_path):
    from app import promote

    content = tmp_path / "content"
    page = content / "wiki" / "entities" / "Lock.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Lock\n\n<!-- human-zone -->\n<!-- /human-zone -->\n", encoding="utf-8")
    calls = []

    def fake_flock(fd, op):
        calls.append(op)

    monkeypatch.setattr(promote.fcntl, "flock", fake_flock)

    result = promote.promote_to_page(
        {"id": "an_lock", "source_id": "S", "target": {"selector": {"quote": "q"}}, "body": "b"},
        "Source",
        content,
        "entities/Lock",
    )

    assert result["ok"] is True
    assert calls == [promote.fcntl.LOCK_EX, promote.fcntl.LOCK_UN]
    assert (content / ".wiki" / "ingest.lock").exists()


def test_promote_restores_original_page_when_commit_fails(monkeypatch, tmp_path):
    from app import promote

    content = tmp_path / "content"
    page = content / "wiki" / "entities" / "Rollback.md"
    page.parent.mkdir(parents=True)
    original = "# Rollback\n\n<!-- human-zone -->\n<!-- /human-zone -->\n"
    page.write_text(original, encoding="utf-8")
    subprocess.run(["git", "-C", str(content), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(content), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(content), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(content), "add", "wiki/entities/Rollback.md"], check=True)
    subprocess.run(["git", "-C", str(content), "commit", "-q", "-m", "init"], check=True)

    def fail_commit(content_dir, path, *_args):
        rel = str(path.relative_to(content_dir))
        subprocess.run(["git", "-C", str(content_dir), "add", rel], check=True)
        raise RuntimeError("commit failed")

    monkeypatch.setattr(promote, "_git_commit", fail_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        promote.promote_to_page(
            {"id": "an_rollback", "source_id": "S", "target": {"selector": {"quote": "q"}}, "body": "b"},
            "Source",
            content,
            "entities/Rollback",
        )

    assert page.read_text(encoding="utf-8") == original
    status = subprocess.run(
        ["git", "-C", str(content), "status", "--short", "--", "wiki/entities/Rollback.md"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""


def test_promote_rejects_dirty_target_page(tmp_path):
    from app import promote

    content = tmp_path / "content"
    page = content / "wiki" / "entities" / "Dirty.md"
    page.parent.mkdir(parents=True)
    original = "# Dirty\n\n<!-- human-zone -->\n<!-- /human-zone -->\n"
    page.write_text(original, encoding="utf-8")
    subprocess.run(["git", "-C", str(content), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(content), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(content), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(content), "add", "wiki/entities/Dirty.md"], check=True)
    subprocess.run(["git", "-C", str(content), "commit", "-q", "-m", "init"], check=True)
    page.write_text(original + "\nuser draft\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="target wiki page has uncommitted changes"):
        promote.promote_to_page(
            {"id": "an_dirty", "source_id": "S", "target": {"selector": {"quote": "q"}}, "body": "b"},
            "Source",
            content,
            "entities/Dirty",
        )

    assert page.read_text(encoding="utf-8") == original + "\nuser draft\n"


def test_promote_commit_is_limited_to_promoted_page(client, auth, content_dir):
    page = content_dir / "wiki" / "entities" / "Pathspec.md"
    page.write_text(
        "# Pathspec\n\n<!-- human-zone -->\n<!-- /human-zone -->\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(content_dir), "add", "wiki/entities/Pathspec.md"], check=True)
    subprocess.run(["git", "-C", str(content_dir), "commit", "-q", "-m", "add pathspec page"], check=True)
    staged = content_dir / "wiki" / "entities" / "Staged.md"
    staged.write_text("# staged but unrelated\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(content_dir), "add", "wiki/entities/Staged.md"], check=True)
    payload = _mk(quote="pathspec", prefix="", suffix="", start=0, end=8)
    payload["body"] = "pathspec body"
    a = client.post("/annotations", json=payload, headers=auth).json()

    r = client.post(
        f"/annotations/{a['id']}/promote",
        json={"wiki_rel": "entities/Pathspec", "source_title": "Pathspec"},
        headers=auth,
    )

    assert r.status_code == 200
    committed = subprocess.run(
        ["git", "-C", str(content_dir), "show", "--name-only", "--format=", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert committed == ["wiki/entities/Pathspec.md"]
    staged_status = subprocess.run(
        ["git", "-C", str(content_dir), "status", "--short", "--", "wiki/entities/Staged.md"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert staged_status.startswith("A  wiki/entities/Staged.md")


def test_promote_rejects_bad_paths(client, auth):
    a = client.post("/annotations", json=_mk(quote="x", start=0, end=1), headers=auth).json()
    # missing wiki_rel
    assert client.post(f"/annotations/{a['id']}/promote", json={}, headers=auth).status_code == 400
    # traversal outside wiki/
    r = client.post(f"/annotations/{a['id']}/promote", json={"wiki_rel": "../../etc/x"}, headers=auth)
    assert r.status_code == 400
    # absolute paths are rejected before path resolution
    r = client.post(f"/annotations/{a['id']}/promote", json={"wiki_rel": "/entities/ATP"}, headers=auth)
    assert r.status_code == 400
    # unknown page
    r = client.post(f"/annotations/{a['id']}/promote", json={"wiki_rel": "entities/NOPE"}, headers=auth)
    assert r.status_code == 400


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
