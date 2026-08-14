import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.nodes import react_agent as react_module


def test_tool_local_placeholders_survive_outer_resolution():
    params = react_module._parameters(
        {
            "inputParameters": [
                {"key": "user_query", "value": "{{CHAT_QUERY}}"},
                {
                    "key": "tools",
                    "value": [
                        {
                            "name": "lookup",
                            "risk": "read",
                            "config": {
                                "url": "{{BASE_URL}}/orders/{{order_id}}",
                                "query": "{{query}}",
                            },
                        }
                    ],
                },
            ]
        },
        {"variables": {"CHAT_QUERY": "hello", "BASE_URL": "https://api.example.com"}},
    )

    assert params["user_query"] == "hello"
    # Mixed outer/tool-local templates are deferred together until tool call.
    assert params["tools"][0]["config"]["url"] == "{{BASE_URL}}/orders/{{order_id}}"
    assert params["tools"][0]["config"]["query"] == "{{query}}"


@pytest.mark.asyncio
async def test_legacy_node_keeps_string_output(monkeypatch):
    model = FakeMessagesListChatModel(responses=[AIMessage(content="legacy answer")])
    monkeypatch.setattr(react_module, "_get_llm", lambda *args, **kwargs: model)

    result = await react_module.react_agent_node(
        {
            "session_id": "session-1",
            "user_id": "user-1",
            "thread_id": "thread-1",
            "variables": {"CHAT_QUERY": "hello"},
            "messages": [],
        },
        {
            "node_id": "react-1",
            "name": "Autonomous ReAct Agent",
            "inputParameters": [
                {"key": "model", "value": "fake"},
                {"key": "user_query", "value": "{{CHAT_QUERY}}"},
                {"key": "tools", "value": []},
            ],
            "outputParameters": [{"key": "output", "value": "react_final_answer"}],
        },
    )

    assert result["variables"]["react_final_answer"] == "legacy answer"
    assert result["messages"][-1].content == "legacy answer"


@pytest.mark.asyncio
async def test_v2_node_returns_structured_and_compatibility_outputs(monkeypatch):
    model = FakeMessagesListChatModel(responses=[AIMessage(content="safe answer")])
    monkeypatch.setattr(react_module, "_get_llm", lambda *args, **kwargs: model)

    result = await react_module.react_agent_node(
        {
            "session_id": "session-1",
            "user_id": "user-1",
            "thread_id": "thread-1",
            "variables": {"CHAT_QUERY": "hello"},
            "messages": [],
        },
        {
            "node_id": "react-2",
            "name": "Autonomous ReAct Agent",
            "inputParameters": [
                {"key": "schema_version", "value": "2.0"},
                {"key": "profile", "value": "safe_chat"},
                {"key": "model", "value": "fake"},
                {"key": "user_query", "value": "{{CHAT_QUERY}}"},
                {"key": "tools", "value": []},
            ],
            "outputParameters": [
                {"key": "result", "value": "agent_result"},
                {"key": "answer", "value": "react_final_answer"},
                {"key": "status", "value": "react_status"},
            ],
        },
    )

    assert result["variables"]["agent_result"]["answer"] == "safe answer"
    assert result["variables"]["react_final_answer"] == "safe answer"
    assert result["variables"]["react_status"] == "COMPLETED"
