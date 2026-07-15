"""Ingest route and job-runner tests."""

import asyncio
import json
import os
import signal
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


def _mk(**selector):
    return {
        "source_id": "01SRC", "color": "note",
        "target": {"block_id": "p-abc", "section_id": "s-1",
                   "context": {"prev_block_id": "", "next_block_id": ""},
                   "selector": selector},
        "body": "",
    }


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


def test_build_argv_routes_lang_url_through_fetch_transcript():
    from app import ingest_runner as ir

    # A media URL under lang must be transcribed, not web-scraped: route to
    # fetch-transcript.py (ASR → .transcript.json → ingest.py), never ingest.py
    # directly on the URL (which would scrape the page as HTML).
    url_argv = ir._build_argv("https://youtube.com/watch?v=x", {"kind": "lang"})
    assert url_argv[0] == sys.executable
    assert url_argv[1].endswith("fetch-transcript.py")
    assert url_argv[2] == "https://youtube.com/watch?v=x"
    assert "--out" in url_argv
    assert "--profile" not in url_argv  # not the scrape path

    # A local transcript file (what fetch-transcript feeds back) still goes
    # straight to the lang generator.
    file_argv = ir._build_argv("/stage/vid.transcript.json", {"kind": "lang"})
    assert file_argv[-3:] == ["--profile", "lang", "/stage/vid.transcript.json"]


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
    taxonomy.write_text(
        "# Taxonomy\n\n## Domain\n- `general/knowledge`\n\n"
        "## Form\n- `concept`\n\n## Reserved\n- `taxonomy-gap`\n",
        encoding="utf-8",
    )
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
