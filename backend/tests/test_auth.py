"""Health, database, and API authentication tests."""


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["auth"] is True
    assert "content" in body  # regression: /health used to reference a removed attr
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cross-Origin-Opener-Policy"] == "same-origin"


def test_static_site_mount_is_last():
    from app.main import app

    assert app.routes[-1].name == "site"
    assert app.routes[-1].path == ""


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


def test_private_routes_fail_closed_when_auth_token_unset(client, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "AUTH_TOKEN", "")
    assert client.get("/health").status_code == 200

    cases = [
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
