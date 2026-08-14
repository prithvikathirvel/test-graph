"""Backward-compatible, policy-aware Autonomous ReAct Agent node.

Legacy flows keep the same registered node name and input/output behavior.  A
v2 node (``schema_version``/``profile`` present) gains typed profiles, precise
tool schemas, hard budgets, deterministic policy, durable approvals,
idempotency, retries, structured results, and modern ``create_agent`` support.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.config import get_config
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from app.agents.contracts import (
    ActionRecord,
    AgentNodeSpec,
    AgentResult,
    AgentRunSummary,
    ErrorDetail,
    normalize_agent_input,
)
from app.agents.factory import BuiltAgent, build_agent
from app.agents.policy import AgentRuntimeContext
from app.agents.profiles import expand_profile
from app.agents.tool_catalog import (
    build_guarded_tools,
    json_schema_to_pydantic,
)
from app.agents.tool_gateway import ToolGateway
from app.core.redaction import mask_for_model, safe_log_value
from app.core.runtime import get_runtime_services
from app.core.state import FlowState
from app.core.token_tracker import get_tracker, price_from_summary
from app.engine.registry import NodeRegistry
from app.nodes.agents import _get_llm
from app.utils.templating import resolve_placeholders

logger = logging.getLogger(__name__)

# Compatibility alias for code/tests that imported the old helper.
_json_schema_to_pydantic = json_schema_to_pydantic


def _extract_pure_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", block.get("content", ""))))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def _resolve_tool_config(value: Any, variables: dict[str, Any]) -> Any:
    """Resolve outer variables while preserving arguments supplied by the LLM.

    Tool configs commonly contain ``{{query}}`` or ``{{order_id}}``. Those names
    do not exist until the model calls the tool, so resolving the entire tools
    array at node entry would erase them. A value is resolved now only when all
    referenced roots are already present in outer FlowState.
    """
    if isinstance(value, str):
        paths = re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}", value)
        if paths and all(path.split(".", 1)[0] in variables for path in paths):
            return resolve_placeholders(value, variables)
        return value
    if isinstance(value, dict):
        return {
            key: _resolve_tool_config(item, variables) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_tool_config(item, variables) for item in value]
    return value


def _parameters(node_config: dict[str, Any], state: FlowState) -> dict[str, Any]:
    raw = {
        item.get("key"): item.get("value")
        for item in node_config.get("inputParameters", [])
        if item.get("key")
    }
    raw_tools = raw.pop("tools", [])
    variables = state.get("variables", {})
    resolved = resolve_placeholders(raw, variables)
    if not isinstance(resolved, dict):
        raise ValueError("Agent inputParameters must resolve to an object")
    resolved["tools"] = _resolve_tool_config(raw_tools, variables)
    normalized = normalize_agent_input(resolved)
    normalized.setdefault("user_query", variables.get("CHAT_QUERY", ""))
    return normalized


def _load_spec(node_config: dict[str, Any], state: FlowState) -> AgentNodeSpec:
    normalized = _parameters(node_config, state)
    expanded = expand_profile(normalized)
    spec = AgentNodeSpec.model_validate(expanded)
    if not spec.is_legacy:
        privileged_risks = {
            "read_sensitive",
            "reversible_write",
            "external_communication",
            "high_impact_write",
            "financial",
            "destructive",
        }
        for tool in spec.tools:
            if "risk" not in tool.model_fields_set:
                raise ValueError(
                    f"v2 tool '{tool.name}' must declare a risk class; refusing implicit authority"
                )
            if tool.risk in privileged_risks and not tool.required_scopes:
                raise ValueError(
                    f"v2 privileged tool '{tool.name}' must declare required_scopes"
                )
    return spec


def _inside_langgraph_execution() -> bool:
    try:
        get_config()
        return True
    except RuntimeError:
        return False


def _prepare_query(spec: AgentNodeSpec) -> str:
    query = str(spec.user_query or "").strip()
    if not query:
        query = "..."  # Existing provider compatibility for empty message parts.
    if len(query) > spec.guardrails.input.max_chars:
        if spec.is_legacy:
            query = query[: spec.guardrails.input.max_chars]
        else:
            raise ValueError(
                f"User input exceeds the configured {spec.guardrails.input.max_chars} character limit"
            )
    pii = spec.guardrails.input.pii
    if pii.enabled and pii.strategy in {"mask_for_model", "redact"}:
        query = mask_for_model(query, pii.types)
    return query


def _behavior_hash(spec: AgentNodeSpec) -> str:
    data = spec.model_dump(mode="json")
    data.pop("user_query", None)
    serialized = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()[:12]


async def _get_snapshot(graph: Any, config: dict[str, Any]) -> Any:
    try:
        if hasattr(graph, "aget_state"):
            return await graph.aget_state(config)
        if hasattr(graph, "get_state"):
            return await asyncio.to_thread(graph.get_state, config)
    except Exception as exc:
        logger.debug("Inner agent snapshot unavailable: %s", type(exc).__name__)
    return None


def _snapshot_messages(snapshot: Any) -> list[Any]:
    if snapshot is None:
        return []
    values = getattr(snapshot, "values", None)
    if isinstance(values, dict):
        return list(values.get("messages", []))
    return []


def _snapshot_interrupt(snapshot: Any) -> Any | None:
    if snapshot is None or not getattr(snapshot, "next", None):
        return None
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            return getattr(item, "value", item)
    return None


def _result_interrupt(result: Any) -> Any | None:
    if not isinstance(result, dict):
        return None
    raw = result.get("__interrupt__")
    if not raw:
        return None
    item = raw[0] if isinstance(raw, (list, tuple)) else raw
    return getattr(item, "value", item)


def _outer_approval_payload(value: Any, node_config: dict[str, Any]) -> dict[str, Any]:
    detail = (
        value if isinstance(value, dict) else {"detail": safe_log_value(value, 1000)}
    )
    question = (
        detail.get("question")
        or f"Approve tool action '{detail.get('tool', 'unknown')}'?"
    )
    return {
        "node_id": node_config.get("node_id", "react-agent"),
        "node_name": node_config.get("displayName")
        or node_config.get("name", "Autonomous ReAct Agent"),
        "node_type": "tool_approval",
        "input_type": "approval",
        "question": question,
        "approval": detail,
        "options": {
            "approve": "Approve",
            "reject": "Reject",
            **(
                {"edit": "Edit and approve"}
                if "edit" in detail.get("allowed_decisions", [])
                else {}
            ),
        },
    }


async def _invoke_with_approval_bridge(
    built: BuiltAgent,
    payload: dict[str, Any],
    config: dict[str, Any],
    node_config: dict[str, Any],
) -> dict[str, Any]:
    graph = built.graph
    snapshot = await _get_snapshot(graph, config)
    pending = _snapshot_interrupt(snapshot)

    if pending is not None:
        # On outer resume the node restarts, encounters the same outer interrupt,
        # consumes the supplied decision, and resumes the namespaced inner graph.
        decision = interrupt(_outer_approval_payload(pending, node_config))
        result = await graph.ainvoke(Command(resume=decision), config=config)
    else:
        result = await graph.ainvoke(payload, config=config)

    pending_result = _result_interrupt(result)
    if pending_result is not None:
        # Suspend the outer graph.  Durable inner state has already been stored.
        interrupt(_outer_approval_payload(pending_result, node_config))
        raise RuntimeError("Unreachable after LangGraph interrupt")
    return result


def _result_messages(result: Any) -> list[Any]:
    if isinstance(result, dict):
        return list(result.get("messages", []))
    return []


def _final_text(result: Any) -> str:
    messages = _result_messages(result)
    for message in reversed(messages):
        if isinstance(message, AIMessage) or getattr(message, "type", "") == "ai":
            text = _extract_pure_text(getattr(message, "content", ""))
            if text:
                return text
    return "Task complete."


def _structured_result(result: Any) -> AgentResult | None:
    if not isinstance(result, dict) or "structured_response" not in result:
        return None
    candidate = result.get("structured_response")
    try:
        if isinstance(candidate, AgentResult):
            return candidate
        return AgentResult.model_validate(candidate)
    except (ValidationError, TypeError, ValueError):
        return None


def _parse_tool_envelope(content: Any) -> dict[str, Any] | None:
    text = _extract_pure_text(content).strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return (
        parsed
        if isinstance(parsed, dict) and parsed.get("_agent_tool_result")
        else None
    )


def _actions_for_current_turn(
    messages: list[Any], query: str, spec: AgentNodeSpec
) -> list[ActionRecord]:
    start = 0
    for index, message in enumerate(messages):
        message_type = getattr(message, "type", "")
        if isinstance(message, HumanMessage) or message_type in {"human", "user"}:
            if (
                _extract_pure_text(getattr(message, "content", "")).strip()
                == query.strip()
            ):
                start = index

    risk_by_name = {re_normalize_tool_name(tool.name): tool.risk for tool in spec.tools}
    actions: list[ActionRecord] = []
    for message in messages[start:]:
        envelope = _parse_tool_envelope(getattr(message, "content", ""))
        if not envelope:
            continue
        provenance = envelope.get("provenance", {}) or {}
        side_effect = envelope.get("side_effect", {}) or {}
        tool_name = str(provenance.get("tool") or getattr(message, "name", "unknown"))
        actions.append(
            ActionRecord(
                tool=tool_name,
                status=str(envelope.get("status", "UNKNOWN")),
                risk=risk_by_name.get(re_normalize_tool_name(tool_name)),
                external_reference=side_effect.get("external_reference"),
                request_id=provenance.get("request_id"),
            )
        )
    return actions


def re_normalize_tool_name(value: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_-]", "_", value.replace(" ", "_")).lower()[:64]


def _token_call_count(start_index: int) -> int:
    tracker = get_tracker()
    if tracker is None:
        return 0
    return max(0, len(tracker.calls) - start_index)


def _cost_since(start_index: int) -> float:
    tracker = get_tracker()
    if tracker is None:
        return 0.0
    # Reuse the existing pricing implementation on a lightweight slice.
    original = tracker.calls
    try:
        tracker.calls = original[start_index:]
        return float(price_from_summary(tracker).get("total_usd", 0.0))
    finally:
        tracker.calls = original


def _build_result(
    raw_result: Any,
    spec: AgentNodeSpec,
    query: str,
    gateway: ToolGateway,
    started: float,
    token_start: int,
) -> AgentResult:
    structured = _structured_result(raw_result)
    answer = structured.answer if structured else _final_text(raw_result)
    if spec.guardrails.output.pii_scan:
        answer = mask_for_model(answer, spec.guardrails.input.pii.types)
    answer = answer[: spec.response.max_answer_chars]

    messages = _result_messages(raw_result)
    actions = _actions_for_current_turn(messages, query, spec)
    if not actions:
        actions = gateway.actions

    budget_failure = any(action.status == "BUDGET_EXCEEDED" for action in actions)
    cancelled = any(action.status == "CANCELLED" for action in actions)
    failed_actions = any(
        action.status not in {"SUCCEEDED", "CANCELLED"} for action in actions
    )
    successful_actions = any(action.status == "SUCCEEDED" for action in actions)
    if structured:
        status = structured.status
    elif cancelled:
        status = "CANCELLED"
    elif budget_failure or (failed_actions and not successful_actions):
        status = "PARTIAL"
    else:
        status = "COMPLETED"
    error = structured.error if structured else None
    cost = _cost_since(token_start)
    if spec.budgets.max_cost_usd is not None and cost > spec.budgets.max_cost_usd:
        status = "PARTIAL"
        error = ErrorDetail(
            code="COST_BUDGET_EXCEEDED",
            message="The run completed after exceeding its configured cost budget.",
        )
        budget_failure = True

    result = structured or AgentResult()
    result.status = status
    result.answer = answer
    result.actions = actions
    result.error = error
    result.run = AgentRunSummary(
        model_calls=_token_call_count(token_start),
        tool_calls=len(actions),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        terminated_by_budget=budget_failure,
    )
    return result


def _output_variables(
    node_config: dict[str, Any], spec: AgentNodeSpec, result: AgentResult
) -> dict[str, Any]:
    outputs = node_config.get("outputParameters", [])
    if not outputs:
        return {"react_final_answer": result.answer}

    variables: dict[str, Any] = {}
    for index, output in enumerate(outputs):
        logical = str(output.get("key", "output")).lower()
        variable = output.get("value") or (
            "react_final_answer" if index == 0 else f"react_{logical}"
        )
        if logical in {"result", "agent_result"}:
            variables[variable] = result.model_dump(mode="json")
        elif logical in {"status", "agent_status"}:
            variables[variable] = result.status
        elif logical in {"actions", "action_summary"}:
            variables[variable] = [
                item.model_dump(mode="json") for item in result.actions
            ]
        else:
            # Existing {key: output, value: react_final_answer} remains text.
            variables[variable] = result.answer
    return variables


@NodeRegistry.register("Autonomous ReAct Agent")
async def react_agent_node(state: FlowState, node_config: dict) -> dict:
    started = time.perf_counter()
    tracker = get_tracker()
    token_start = len(tracker.calls) if tracker else 0
    node_id = str(node_config.get("node_id", "react-agent"))
    logger.info("Entering Autonomous ReAct Agent | node=%s", node_id)

    spec: AgentNodeSpec | None = None
    try:
        spec = _load_spec(node_config, state)
        query = _prepare_query(spec)
        runtime = AgentRuntimeContext.from_state(state, node_id)
        services = get_runtime_services()
        durable = services.checkpointer is not None and _inside_langgraph_execution()

        gateway = ToolGateway(
            spec,
            runtime,
            durable_approvals=durable,
            base_variables=state.get("variables", {}),
        )
        tools = build_guarded_tools(spec, state, gateway)
        llm = _get_llm(
            spec.model,
            max_tokens=min(
                spec.budgets.max_output_tokens, spec.response.max_answer_chars
            ),
        )
        fallback_models: list[Any] = []
        for fallback_name in spec.fallback_models:
            try:
                fallback_models.append(
                    _get_llm(
                        fallback_name,
                        max_tokens=min(
                            spec.budgets.max_output_tokens,
                            spec.response.max_answer_chars,
                        ),
                    )
                )
            except Exception as fallback_error:
                logger.warning(
                    "Could not initialize fallback model '%s' (%s)",
                    fallback_name,
                    type(fallback_error).__name__,
                )
        built = build_agent(
            model=llm,
            tools=tools,
            spec=spec,
            checkpointer=services.checkpointer if durable else None,
            fallback_models=fallback_models,
        )

        history = list(state.get("messages", []))
        memory_messages = spec.memory.short_term.recent_messages
        if spec.is_legacy:
            memory_messages = max(memory_messages, spec.memory_window * 2)
        recent_history = history[-memory_messages:] if memory_messages > 0 else []

        inner_thread = (
            f"{runtime.thread_id}:react:{node_id}:{_behavior_hash(spec)}"
            if durable
            else f"ephemeral:{runtime.request_id}"
        )
        config = {
            "configurable": {"thread_id": inner_thread},
            "recursion_limit": min(
                250,
                max(
                    10,
                    (spec.budgets.max_model_calls + spec.budgets.max_tool_calls) * 2
                    + 5,
                ),
            ),
            "run_name": f"ReAct:{node_id}",
            "tags": [
                spec.profile,
                "react-v2" if not spec.is_legacy else "react-legacy",
            ],
        }

        snapshot = await _get_snapshot(built.graph, config) if durable else None
        prior_inner_messages = _snapshot_messages(snapshot)
        messages_payload: list[Any] = []
        if not built.modern and not prior_inner_messages and spec.system_prompt:
            # Older create_react_agent versions without a prompt parameter need
            # the system message in their initial payload.
            messages_payload.append(SystemMessage(content=spec.system_prompt))
        if not prior_inner_messages:
            messages_payload.extend(recent_history)
        messages_payload.append(HumanMessage(content=query))

        raw_result = await asyncio.wait_for(
            _invoke_with_approval_bridge(
                built,
                {"messages": messages_payload},
                config,
                node_config,
            ),
            timeout=spec.budgets.timeout_seconds,
        )
        result = _build_result(raw_result, spec, query, gateway, started, token_start)
        variables = _output_variables(node_config, spec, result)
        logger.info(
            "Autonomous ReAct Agent completed | node=%s profile=%s status=%s tools=%s elapsed_ms=%s",
            node_id,
            spec.profile,
            result.status,
            len(result.actions),
            result.run.elapsed_ms,
        )
        return {
            "variables": variables,
            "messages": [HumanMessage(content=query), AIMessage(content=result.answer)],
        }

    except asyncio.TimeoutError:
        logger.warning("Autonomous ReAct Agent timed out | node=%s", node_id)
        if spec and not spec.is_legacy:
            result = AgentResult(
                status="PARTIAL",
                answer="I could not complete the task within the configured time limit.",
                error=ErrorDetail(
                    code="AGENT_TIMEOUT", message="Agent time budget exceeded."
                ),
                run=AgentRunSummary(
                    model_calls=_token_call_count(token_start),
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                    terminated_by_budget=True,
                ),
            )
            return {"variables": _output_variables(node_config, spec, result)}
        output_key = (
            node_config.get("outputParameters", [{}])[0].get(
                "value", "react_final_answer"
            )
            if node_config.get("outputParameters")
            else "react_final_answer"
        )
        return {"variables": {output_key: "Error: Agent execution timed out."}}

    except (ValidationError, ValueError) as exc:
        logger.warning(
            "Invalid ReAct configuration | node=%s error=%s",
            node_id,
            safe_log_value(exc, 500),
        )
        if spec and not spec.is_legacy:
            result = AgentResult(
                status="FAILED",
                answer="The agent is not configured correctly. Please contact an administrator.",
                error=ErrorDetail(
                    code="INVALID_AGENT_CONFIG", message=safe_log_value(exc, 500)
                ),
            )
            return {"variables": _output_variables(node_config, spec, result)}
        output_key = (
            node_config.get("outputParameters", [{}])[0].get(
                "value", "react_final_answer"
            )
            if node_config.get("outputParameters")
            else "react_final_answer"
        )
        return {"variables": {output_key: f"Error: {safe_log_value(exc, 500)}"}}

    except Exception as exc:
        logger.error(
            "Autonomous ReAct Agent failed | node=%s error_type=%s",
            node_id,
            type(exc).__name__,
            exc_info=True,
        )
        if spec and not spec.is_legacy:
            result = AgentResult(
                status="FAILED",
                answer="I could not complete this task safely. Please try again or contact support.",
                error=ErrorDetail(
                    code="AGENT_EXECUTION_FAILED",
                    message="The agent encountered an internal execution error.",
                ),
                run=AgentRunSummary(
                    model_calls=_token_call_count(token_start),
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                ),
            )
            return {"variables": _output_variables(node_config, spec, result)}
        output_key = (
            node_config.get("outputParameters", [{}])[0].get(
                "value", "react_final_answer"
            )
            if node_config.get("outputParameters")
            else "react_final_answer"
        )
        return {"variables": {output_key: f"Error: {safe_log_value(exc, 500)}"}}
