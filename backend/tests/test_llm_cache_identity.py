"""Provider/model changes must not reuse another LLM's cached output."""


def test_translation_cache_is_provider_and_model_specific(client, auth, monkeypatch):
    from app.routers import llm as routes

    identity = {
        "provider": "codex",
        "model": "model-a",
        "api_base_url": None,
    }
    calls = []
    monkeypatch.setattr(routes.llm_client, "configured", lambda: True)
    monkeypatch.setattr(
        routes.llm_client, "execution_identity", lambda: identity.copy()
    )
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
        {"provider": "codex", "model": "model-a", "api_base_url": None},
        {"provider": "claude", "model": "model-b", "api_base_url": None},
    ]


def test_translation_cache_uses_full_execution_identity(client, auth, monkeypatch):
    from app.routers import llm as routes

    identity = {
        "provider": "api",
        "model": "same-model",
        "api_base_url": "https://one.example/v1",
    }
    calls = []
    monkeypatch.setattr(routes.llm_client, "configured", lambda: True)
    monkeypatch.setattr(
        routes.llm_client, "execution_identity", lambda: identity.copy()
    )
    monkeypatch.setattr(
        routes.llm_client,
        "complete",
        lambda *_args, **_kwargs: calls.append(identity["api_base_url"])
        or identity["api_base_url"],
    )

    body = {"text": "execution identity cache fixture"}
    first = client.post("/translate", json=body, headers=auth).json()
    identity["api_base_url"] = "https://two.example/v1"
    switched = client.post("/translate", json=body, headers=auth).json()

    assert first["cached"] is False
    assert switched["cached"] is False
    assert calls == ["https://one.example/v1", "https://two.example/v1"]
