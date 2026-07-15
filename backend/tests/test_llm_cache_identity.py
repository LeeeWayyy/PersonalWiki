"""Provider/model changes must not reuse another LLM's cached output."""


def test_translation_cache_is_provider_and_model_specific(client, auth, monkeypatch):
    from app.routers import llm as routes

    identity = {"provider": "codex", "model": "model-a"}
    calls = []
    monkeypatch.setattr(routes.llm_client, "configured", lambda: True)
    monkeypatch.setattr(routes.llm_client, "identity", lambda: identity.copy())
    monkeypatch.setattr(
        routes.llm_client,
        "complete",
        lambda *_args, **_kwargs: calls.append(identity.copy()) or identity["provider"],
    )

    first = client.post(
        "/translate", json={"text": "provider cache identity fixture"}, headers=auth
    ).json()
    again = client.post(
        "/translate", json={"text": "provider cache identity fixture"}, headers=auth
    ).json()
    identity.update(provider="claude", model="model-b")
    switched = client.post(
        "/translate", json={"text": "provider cache identity fixture"}, headers=auth
    ).json()

    assert (first["translation"], first["cached"]) == ("codex", False)
    assert (again["translation"], again["cached"]) == ("codex", True)
    assert (switched["translation"], switched["cached"]) == ("claude", False)
    assert calls == [
        {"provider": "codex", "model": "model-a"},
        {"provider": "claude", "model": "model-b"},
    ]
