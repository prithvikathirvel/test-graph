import pytest

from app.agents.contracts import AgentNodeSpec
from app.agents.policy import AgentRuntimeContext
from app.agents.profiles import expand_profile
from app.agents.tool_gateway import ToolGateway, is_private_hostname


def _runtime():
    return AgentRuntimeContext(
        tenant_id="tenant-1",
        user_id="user-1",
        roles=("customer",),
        scopes=(),
        session_id="session-1",
        thread_id="thread-1",
        request_id="request-1",
        authenticated=True,
        identity_verified=True,
    )


def _spec(profile="safe_chat", tools=None, budgets=None):
    return AgentNodeSpec.model_validate(
        expand_profile(
            {
                "schema_version": "2.0",
                "profile": profile,
                "user_query": "test",
                "tools": tools or [{"name": "calculate", "risk": "compute"}],
                **({"budgets": budgets} if budgets else {}),
            }
        )
    )


@pytest.mark.asyncio
async def test_gateway_returns_structured_success():
    spec = _spec()
    gateway = ToolGateway(spec, _runtime(), durable_approvals=False)

    async def operation(args):
        return {"answer": args["x"] + 1}

    result = await gateway.execute(spec.tools[0], {"x": 1}, operation)

    assert result.ok is True
    assert result.data == {"answer": 2}
    assert result.for_model()["trust"] == "untrusted_tool_output"
    assert gateway.total_calls == 1


@pytest.mark.asyncio
async def test_gateway_enforces_budget_before_operation():
    spec = _spec(budgets={"max_tool_calls": 0})
    gateway = ToolGateway(spec, _runtime(), durable_approvals=False)
    called = False

    async def operation(args):
        nonlocal called
        called = True
        return "should not run"

    result = await gateway.execute(spec.tools[0], {}, operation)

    assert result.ok is False
    assert result.error.code == "TOOL_CALL_BUDGET_EXCEEDED"
    assert called is False


@pytest.mark.asyncio
async def test_approval_required_tool_fails_closed_without_checkpointer():
    spec = _spec(
        profile="commerce",
        tools=[{"name": "cancel_order", "risk": "high_impact_write"}],
    )
    gateway = ToolGateway(spec, _runtime(), durable_approvals=False)

    async def operation(args):
        return {"cancelled": True}

    result = await gateway.execute(spec.tools[0], {"order_id": "O-1"}, operation)

    assert result.ok is False
    assert result.error.code == "APPROVAL_UNAVAILABLE"


@pytest.mark.asyncio
async def test_gateway_rejects_invalid_tool_input_schema():
    spec = _spec(
        tools=[
            {
                "name": "calculate",
                "risk": "compute",
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    gateway = ToolGateway(spec, _runtime(), durable_approvals=False)
    called = False

    async def operation(args):
        nonlocal called
        called = True
        return {"answer": 1}

    result = await gateway.execute(spec.tools[0], {"value": "wrong"}, operation)

    assert result.ok is False
    assert result.error.code == "INVALID_TOOL_INPUT"
    assert called is False


@pytest.mark.asyncio
async def test_gateway_rejects_invalid_tool_output_schema():
    spec = _spec(
        tools=[
            {
                "name": "calculate",
                "risk": "compute",
                "output_schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "integer"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    gateway = ToolGateway(spec, _runtime(), durable_approvals=False)

    async def operation(args):
        return {"answer": "not-an-integer"}

    result = await gateway.execute(spec.tools[0], {}, operation)

    assert result.ok is False
    assert result.error.code == "INVALID_TOOL_OUTPUT"


def test_private_host_detection():
    assert is_private_hostname("localhost")
    assert is_private_hostname("127.0.0.1")
    assert is_private_hostname("169.254.169.254")
    assert is_private_hostname("metadata.google.internal")
    assert not is_private_hostname("api.example.com")
