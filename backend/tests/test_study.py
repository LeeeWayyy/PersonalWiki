"""Vocabulary, review, and export route tests."""

import datetime as dt
import os
import time


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
