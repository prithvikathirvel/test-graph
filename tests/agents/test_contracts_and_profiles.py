from app.agents.contracts import (
    AgentNodeSpec,
    AgentResult,
    ToolResult,
    normalize_agent_input,
)
from app.agents.profiles import expand_profile


def test_legacy_input_remains_legacy_and_permissive():
    normalized = normalize_agent_input(
        {
            "model": "gemini-2.5-pro",
            "user_query": "hello",
            "tools": [
                {
                    "name": "lookup",
                    "node_type": "API caller",
                    "config": {"url": "https://example.com"},
                }
            ],
        }
    )
    spec = AgentNodeSpec.model_validate(expand_profile(normalized))

    assert spec.is_legacy
    assert spec.profile == "legacy"
    assert spec.guardrails.tool.default_action == "allow"
    assert spec.response.format == "text"
    assert spec.tools[0].name == "lookup"


def test_v2_profile_expands_but_explicit_override_wins():
    normalized = normalize_agent_input(
        {
            "schema_version": "2.0",
            "profile": "support",
            "user_query": "create a ticket",
            "budgets": {"max_tool_calls": 3},
            "tools": [
                {
                    "name": "create_ticket",
                    "risk": "reversible_write",
                    "node_type": "MCP Tool",
                    "config": {"server_id": "desk", "tool_name": "create_ticket"},
                }
            ],
        }
    )
    spec = AgentNodeSpec.model_validate(expand_profile(normalized))

    assert not spec.is_legacy
    assert spec.budgets.max_tool_calls == 3
    assert spec.budgets.max_model_calls == 12
    assert spec.tools[0].adapter == "mcp"
    assert spec.response.format == "agent_result"


def test_agent_and_tool_result_contracts_are_json_serializable():
    tool = ToolResult.model_validate(
        {
            "ok": True,
            "status": "SUCCEEDED",
            "data": {"ticket_id": "T-1"},
            "provenance": {"tool": "create_ticket"},
            "side_effect": {"occurred": True, "external_reference": "T-1"},
        }
    )
    result = AgentResult(answer="done")

    assert tool.for_model()["_agent_tool_result"] is True
    assert tool.for_model()["instructions_allowed"] is False
    assert result.model_dump(mode="json")["status"] == "COMPLETED"
