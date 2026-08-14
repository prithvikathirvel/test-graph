import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import NotRequired, TypedDict

from app.agents.contracts import AgentNodeSpec
from app.agents.factory import BuiltAgent, build_agent
from app.agents.policy import AgentRuntimeContext
from app.agents.profiles import expand_profile
from app.agents.tool_catalog import build_guarded_tools
from app.agents.tool_gateway import ToolGateway
from app.engine.registry import NodeRegistry
from app.nodes.react_agent import _invoke_with_approval_bridge


class ToolCallingFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
async def test_high_impact_tool_interrupts_and_resumes_once():
    executions = []

    async def cancel_executor(state, node_config):
        executions.append(state["variables"]["order_id"])
        output = node_config["outputParameters"][0]["value"]
        return {"variables": {output: {"order_id": "O-1", "cancelled": True}}}

    NodeRegistry._executors["Test Cancel Order"] = cancel_executor
    spec = AgentNodeSpec.model_validate(
        expand_profile(
            {
                "schema_version": "2.0",
                "profile": "commerce",
                "user_query": "cancel O-1",
                "planning": {"todo_enabled": False},
                "tools": [
                    {
                        "name": "cancel_order",
                        "node_type": "Test Cancel Order",
                        "risk": "high_impact_write",
                        "input_schema": {
                            "type": "object",
                            "properties": {"order_id": {"type": "string"}},
                            "required": ["order_id"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        )
    )
    runtime = AgentRuntimeContext(
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
    gateway = ToolGateway(spec, runtime, durable_approvals=True)
    state = {
        "session_id": "session-1",
        "user_id": "user-1",
        "thread_id": "thread-1",
        "variables": {},
        "messages": [],
    }
    tools = build_guarded_tools(spec, state, gateway)
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "cancel_order",
                        "args": {"order_id": "O-1"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Order O-1 was cancelled."),
        ]
    )
    agent = build_agent(
        model=model,
        tools=tools,
        spec=spec,
        checkpointer=InMemorySaver(),
    ).graph
    config = {"configurable": {"thread_id": "inner-thread"}}

    first = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "cancel O-1"}]}, config=config
    )
    assert first.get("__interrupt__")
    assert executions == []

    final = await agent.ainvoke(Command(resume={"decision": "approve"}), config=config)

    assert executions == ["O-1"]
    assert final["messages"][-1].content == "Order O-1 was cancelled."
    tool_messages = [message for message in final["messages"] if message.type == "tool"]
    envelope = json.loads(tool_messages[-1].content)
    assert envelope["ok"] is True
    assert envelope["side_effect"]["external_reference"] == "O-1"


class InnerState(TypedDict):
    answer: NotRequired[dict]


class OuterState(TypedDict):
    result: NotRequired[dict]


@pytest.mark.asyncio
async def test_inner_approval_is_bridged_to_outer_graph_resume():
    async def inner_node(state):
        decision = interrupt(
            {
                "type": "tool_approval",
                "tool": "cancel_order",
                "risk": "high_impact_write",
                "question": "Approve cancellation?",
                "allowed_decisions": ["approve", "reject"],
            }
        )
        return {"answer": decision}

    inner_builder = StateGraph(InnerState)
    inner_builder.add_node("approve", inner_node)
    inner_builder.add_edge(START, "approve")
    inner_builder.add_edge("approve", END)
    inner = inner_builder.compile(checkpointer=InMemorySaver())
    built = BuiltAgent(graph=inner, modern=True)
    inner_config = {"configurable": {"thread_id": "inner-thread-bridge"}}
    node_config = {
        "node_id": "react-approval",
        "name": "Autonomous ReAct Agent",
    }

    async def outer_node(state):
        result = await _invoke_with_approval_bridge(
            built,
            {},
            inner_config,
            node_config,
        )
        return {"result": result}

    outer_builder = StateGraph(OuterState)
    outer_builder.add_node("react", outer_node)
    outer_builder.add_edge(START, "react")
    outer_builder.add_edge("react", END)
    outer = outer_builder.compile(checkpointer=InMemorySaver())
    outer_config = {"configurable": {"thread_id": "outer-thread-bridge"}}

    first = await outer.ainvoke({}, config=outer_config)
    assert first.get("__interrupt__")
    assert first["__interrupt__"][0].value["input_type"] == "approval"

    final = await outer.ainvoke(
        Command(resume={"decision": "approve"}), config=outer_config
    )
    assert final["result"]["answer"] == {"decision": "approve"}
