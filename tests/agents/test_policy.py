from app.agents.contracts import AgentNodeSpec
from app.agents.policy import AgentRuntimeContext, ToolPolicyEngine
from app.agents.profiles import expand_profile


def _runtime(**overrides):
    values = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "roles": ("customer",),
        "scopes": ("orders:read:own", "orders:cancel:own"),
        "session_id": "session-1",
        "thread_id": "thread-1",
        "request_id": "request-1",
        "authenticated": True,
        "identity_verified": True,
    }
    values.update(overrides)
    return AgentRuntimeContext(**values)


def _commerce_spec():
    return AgentNodeSpec.model_validate(
        expand_profile(
            {
                "schema_version": "2.0",
                "profile": "commerce",
                "user_query": "cancel it",
                "tools": [
                    {
                        "name": "get_order",
                        "risk": "read_sensitive",
                        "required_scopes": ["orders:read:own"],
                        "resource_policy": {"owner_argument": "customer_id"},
                    },
                    {
                        "name": "cancel_order",
                        "risk": "high_impact_write",
                        "required_scopes": ["orders:cancel:own"],
                    },
                    {"name": "delete_all", "risk": "destructive"},
                ],
            }
        )
    )


def test_read_is_allowed_and_high_impact_write_asks():
    spec = _commerce_spec()
    engine = ToolPolicyEngine(spec, _runtime())

    assert engine.evaluate(spec.tools[0], {"customer_id": "user-1"}).decision == "allow"
    assert engine.evaluate(spec.tools[1], {"order_id": "O-1"}).decision == "ask"
    assert engine.evaluate(spec.tools[2], {}).decision == "deny"


def test_scope_and_ownership_are_enforced_before_approval():
    spec = _commerce_spec()
    no_scope = ToolPolicyEngine(spec, _runtime(scopes=()))
    wrong_owner = ToolPolicyEngine(spec, _runtime())

    assert (
        no_scope.evaluate(spec.tools[0], {"customer_id": "user-1"}).reason_code
        == "MISSING_REQUIRED_SCOPE"
    )
    assert (
        wrong_owner.evaluate(spec.tools[0], {"customer_id": "another-user"}).reason_code
        == "RESOURCE_OWNERSHIP_MISMATCH"
    )
