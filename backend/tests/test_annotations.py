"""Annotation and human-zone route tests."""

import subprocess
from pathlib import Path

import pytest


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


def test_page_remove_route_requires_confirmation(client, auth, monkeypatch):
    from app import ingest_runner as ir
    from app import promote

    calls = []
    monkeypatch.setattr(ir, "REBUILD_CMD", "")
    monkeypatch.setattr(
        promote,
        "remove_page",
        lambda content, repo, rel, merge: calls.append((content, repo, rel, merge))
        or {"ok": True, "wiki_rel": rel, "merge_into": merge, "committed": True},
    )

    endpoint = "/wiki/page/remove"
    payload = {"rel": "entities/X", "merge_into": "entities/Y", "confirmation": "wrong"}
    assert client.post(endpoint, json=payload, headers=auth).status_code == 400
    assert client.post(endpoint, json={**payload, "confirmation": "entities/X"}, headers=auth).status_code == 200
    assert client.post(endpoint, json={"rel": "entities/X", "confirmation": "entities/X"}, headers=auth).status_code == 200
    assert client.post(endpoint, json={"rel": "entities/X", "confirmation": "entities/X"}).status_code in (401, 403)
    assert [call[2:] for call in calls] == [
        ("entities/X", "entities/Y"),
        ("entities/X", None),
    ]


def test_remove_page_commits_graph_changes(tmp_path):
    from app import promote

    content = tmp_path / "content"
    entities = content / "wiki" / "entities"
    maps = content / "wiki" / "_maps"
    entities.mkdir(parents=True)
    maps.mkdir()
    (content / ".gitignore").write_text("wiki/.alias-index.json\n.wiki/ingest.lock\n", encoding="utf-8")
    (content / "wiki" / "_taxonomy.md").write_text(
        "# Taxonomy\n\n## Domain\n- `test/domain`\n\n"
        "## Form\n- `concept`\n\n## Reserved\n- `taxonomy-gap`\n",
        encoding="utf-8",
    )

    def write_page(stem, pid, body):
        (entities / f"{stem}.md").write_text(
            "---\n"
            "type: Entity\n"
            f"page_id: {pid}\n"
            f"aliases: [{stem}]\n"
            "tags: [test/domain, concept]\n"
            "sources: []\n"
            "last_ingested: 2026-07-13\n"
            "---\n"
            f"# {stem}\n\n<!-- llm-zone -->\n{body}\n<!-- /llm-zone -->\n",
            encoding="utf-8",
        )

    write_page("X", "X" * 26, "doomed")
    write_page("Keep", "K" * 26, "[[X]] survives as text")
    (maps / "source.md").write_text("[[X]]\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(content), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(content), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(content), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(content), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(content), "commit", "-q", "-m", "init"], check=True)

    result = promote.remove_page(content, Path(__file__).resolve().parents[2], "entities/X")

    assert result["ok"] and not (entities / "X.md").exists()
    assert "[[X]]" not in (entities / "Keep.md").read_text(encoding="utf-8")
    assert (maps / "source.md").read_text(encoding="utf-8") == "X\n"
    assert subprocess.run(
        ["git", "-C", str(content), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout == ""
    assert subprocess.run(
        ["git", "-C", str(content), "log", "-1", "--pretty=%s"],
        check=True, capture_output=True, text=True,
    ).stdout.strip() == "wiki: delete X"


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
