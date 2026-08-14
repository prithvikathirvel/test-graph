from app.core import security


def test_auth_is_compatibility_disabled_by_default(monkeypatch):
    monkeypatch.setattr(security.settings, "AUTH_REQUIRED", False)

    decision = security.authorize_request("/agents/invoke/a", "POST", {})

    assert decision.allowed is True
    assert decision.context["verified"] is False


def test_auth_rejects_bad_key_and_missing_admin_scope(monkeypatch):
    monkeypatch.setattr(security.settings, "AUTH_REQUIRED", True)
    monkeypatch.setattr(security.settings, "AGENT_API_KEY", "correct-key")
    monkeypatch.setattr(
        security.settings, "AGENT_API_SCOPES", "agent:access,agent:invoke"
    )

    bad_key = security.authorize_request(
        "/agents/invoke/a", "POST", {"x-api-key": "wrong"}
    )
    missing_scope = security.authorize_request(
        "/system/checkpoints/clear", "POST", {"x-api-key": "correct-key"}
    )

    assert bad_key.allowed is False
    assert bad_key.status_code == 401
    assert missing_scope.allowed is False
    assert missing_scope.status_code == 403


def test_auth_builds_verified_context_from_server_scopes(monkeypatch):
    monkeypatch.setattr(security.settings, "AUTH_REQUIRED", True)
    monkeypatch.setattr(security.settings, "AGENT_API_KEY", "correct-key")
    monkeypatch.setattr(
        security.settings, "AGENT_API_SCOPES", "agent:access,agent:invoke,agent:admin"
    )
    monkeypatch.setattr(security.settings, "AGENT_API_ROLES", "service,admin")
    monkeypatch.setattr(security.settings, "AGENT_API_TENANT_ID", "tenant-fixed")
    monkeypatch.setattr(security.settings, "AGENT_API_USER_ID", "service")

    decision = security.authorize_request(
        "/agents/invoke/a",
        "POST",
        {
            "x-api-key": "correct-key",
            "x-user-id": "operator-1",
            "x-tenant-id": "ignored-because-fixed",
        },
    )

    assert decision.allowed is True
    assert decision.context["verified"] is True
    assert decision.context["tenant_id"] == "tenant-fixed"
    assert decision.context["user_id"] == "operator-1"
    assert "agent:invoke" in decision.context["scopes"]
