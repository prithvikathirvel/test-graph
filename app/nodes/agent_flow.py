"""
Agent Flow Node — Execute a sub-flow within the current agent flow.

This node fetches another agent flow's schema, compiles it into an ephemeral
LangGraph, maps input parameters from the parent flow, executes the sub-flow
to completion, and merges the child's output variables back into the parent state.

Schema format:
    inputParameters:
        - agent_id   (required) — The ID of the child agent flow to execute.
        - input_mapping (optional) — An object of key-value pairs where each
          key is a child flow variable and each value is a template resolved
          from the parent flow (e.g. {"FROM_DATE": "{{FROM_DATE}}"}).
          Mapped values OVERRIDE the child flow's default inputs.

Design decisions:
    - Ephemeral execution: Child flows run without a checkpointer (no nested HITL).
    - Error isolation: Child errors are captured, never crash the parent.
    - Circular reference protection: Max depth of 5 nested sub-flows.
    - Override semantics: child defaults loaded first, then input_mapping overrides.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Dict

import httpx

from app.core.config import settings
from app.core.state import FlowState
from app.engine.compiler import GraphCompiler
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders

logger = logging.getLogger(__name__)

# Hard ceiling to prevent infinite recursion (Flow A → B → A → …)
MAX_SUB_FLOW_DEPTH = 5

# Default timeout for child flow execution (seconds)
DEFAULT_TIMEOUT_SECONDS = 120


# ──────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────

async def _fetch_sub_flow_schema(agent_id: str) -> dict:
    """Fetch the JSON schema for a sub-flow from the Schema API."""
    base_url = settings.SCHEMA_API_URL.rstrip("/")
    url = f"{base_url}/{agent_id}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        schema = response.json()
        if not schema:
            raise ValueError(f"Schema API returned empty payload for agent '{agent_id}'")
        return schema


def _build_child_variables(
    input_mapping: Any,
    parent_variables: dict,
    child_schema: dict,
) -> dict:
    """
    Construct the child flow's initial variables.

    1. Seed with child schema default inputs
    2. Overlay (override) with resolved values from ``input_mapping``

    ``input_mapping`` is a dict of {child_key: template_value}, e.g.:
        {"FROM_DATE": "{{FROM_DATE}}", "TO_COUNTRY": "Japan"}

    Mapped values OVERRIDE anything the child schema defines as defaults.
    """
    child_vars: Dict[str, Any] = {}

    # 1. Seed with child schema defaults
    for inp in child_schema.get("inputs", []):
        child_vars[inp["key"]] = inp.get("value", "")

    # 2. Override with parent → child mappings
    if isinstance(input_mapping, dict):
        for child_key, raw_value in input_mapping.items():
            child_vars[child_key] = resolve_placeholders(raw_value, parent_variables)

    return child_vars


# ──────────────────────────────────────────────
# Node executor
# ──────────────────────────────────────────────

@NodeRegistry.register("Agent Flow Node")
async def agent_flow_node(state: FlowState, node_config: dict) -> dict:
    """
    Execute another agent flow as a sub-graph.

    Expected inputParameters:
        agent_id      (required) — ID of the child flow to invoke
        input_mapping (optional) — Object of {child_key: "{{parent_var}}"} pairs
                                   that override the child flow's default inputs

    Example schema:
        "inputParameters": [
            {"key": "agent_id",      "value": "abc-123",                    "type": "string"},
            {"key": "input_mapping", "value": {"FROM_DATE": "{{FROM_DATE}}", "BUDGET": "{{BUDGET}}"}, "type": "object"}
        ]
    """

    node_name = node_config.get("name", "Agent Flow Node")
    node_id = node_config.get("node_id", "unknown")

    logger.info(f"🔗 [{node_name}] Entering Agent Flow Node (id={node_id})")

    # ── 1. Parse inputParameters ───────────────────────────────────────
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}

    # agent_id: required
    agent_id = str(inputs.get("agent_id", "")).strip()
    agent_id = resolve_placeholders(agent_id, state["variables"])

    # input_mapping: optional object of key-value overrides
    raw_mapping = inputs.get("input_mapping", {})

    # Handle case where input_mapping arrives as a JSON string from the UI
    if isinstance(raw_mapping, str):
        try:
            raw_mapping = json.loads(raw_mapping)
        except (json.JSONDecodeError, TypeError):
            raw_mapping = {}

    input_mapping = resolve_placeholders(raw_mapping, state["variables"]) if isinstance(raw_mapping, dict) else {}
    print(f"📊 [{node_name}] Resolved input_mapping: {input_mapping}")

    # Output key
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "sub_flow_output"

    # ── 2. Validate agent_id ───────────────────────────────────────────
    if not agent_id:
        error = "Agent Flow Node requires 'agent_id' in inputParameters."
        logger.error(f"❌ [{node_name}] {error}")
        return {"variables": {output_key: {"error": error, "status": "FAILED"}}}

    # ── 3. Circular-reference guard ────────────────────────────────────
    current_depth = state["variables"].get("_sub_flow_depth", 0)
    if current_depth >= MAX_SUB_FLOW_DEPTH:
        error = (
            f"Maximum sub-flow nesting depth ({MAX_SUB_FLOW_DEPTH}) exceeded. "
            f"Possible circular reference detected."
        )
        logger.error(f"🚫 [{node_name}] {error}")
        return {"variables": {output_key: {"error": error, "status": "FAILED"}}}

    # ── 4. Fetch & compile the child flow ──────────────────────────────
    try:
        logger.info(f"📥 [{node_name}] Fetching schema for sub-flow '{agent_id}'…")
        child_schema = await _fetch_sub_flow_schema(agent_id)
    except Exception as e:
        error = f"Failed to fetch sub-flow schema for '{agent_id}': {e}"
        logger.error(f"❌ [{node_name}] {error}")
        return {"variables": {output_key: {"error": error, "status": "FAILED"}}}

    try:
        # Compile without checkpointer — ephemeral, one-shot execution
        compiler = GraphCompiler(child_schema, checkpointer=None)
        child_graph = await compiler.build()
        logger.info(f"✅ [{node_name}] Sub-flow '{agent_id}' compiled successfully.")
    except Exception as e:
        error = f"Failed to compile sub-flow '{agent_id}': {e}"
        logger.error(f"❌ [{node_name}] {error}")
        return {"variables": {output_key: {"error": error, "status": "FAILED"}}}

    # ── 5. Build child initial state ───────────────────────────────────
    child_variables = _build_child_variables(input_mapping, state["variables"], child_schema)

    # Propagate the depth counter so the child knows its nesting level
    child_variables["_sub_flow_depth"] = current_depth + 1

    # Auto-propagate essential runtime variables from parent → child
    # so child LLM nodes don't fail with empty user input
    for auto_key in ("CHAT_QUERY",):
        if auto_key in state["variables"] and auto_key not in child_variables:
            child_variables[auto_key] = state["variables"][auto_key]

    child_state: Dict[str, Any] = {
        "session_id": state.get("session_id", ""),
        "user_id": state.get("user_id", ""),
        "variables": child_variables,
        # Forward parent conversation history to child flows
        # so child LLM nodes have context for their prompts
        "messages": list(state.get("messages", [])),
    }

    child_thread_id = f"subflow_{uuid.uuid4().hex}"
    child_config = {
        "configurable": {"thread_id": child_thread_id},
        "recursion_limit": 100,
    }

    # ── 6. Execute the child flow ──────────────────────────────────────
    try:
        logger.info(
            f"🚀 [{node_name}] Invoking sub-flow '{agent_id}' "
            f"(depth={current_depth + 1}, timeout={DEFAULT_TIMEOUT_SECONDS}s, "
            f"overrides={list(input_mapping.keys()) if input_mapping else 'none'})…"
        )

        child_result = await asyncio.wait_for(
            child_graph.ainvoke(child_state, config=child_config),
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )

        child_output_vars = child_result.get("variables", {})

        # Strip internal bookkeeping keys before surfacing to parent
        sanitised_output = {
            k: v for k, v in child_output_vars.items()
            if not k.startswith("_sub_flow_") and not k.startswith("_iter_")
        }

        logger.info(
            f"✅ [{node_name}] Sub-flow '{agent_id}' completed. "
            f"Output keys: {list(sanitised_output.keys())}"
        )

        # Flatten output: child variables stored directly under output_key
        # so {{weather_response.response}} works in downstream prompts.
        return {
            "variables": {
                output_key: sanitised_output,
                f"{output_key}_status": "COMPLETED",
            }
        }

    except asyncio.TimeoutError:
        error = f"Sub-flow '{agent_id}' timed out after {DEFAULT_TIMEOUT_SECONDS}s."
        logger.error(f"⏰ [{node_name}] {error}")
        return {"variables": {
            output_key: {"error": error},
            f"{output_key}_status": "TIMEOUT",
        }}

    except Exception as e:
        error = f"Sub-flow '{agent_id}' execution error: {e}"
        logger.exception(f"❌ [{node_name}] {error}")
        return {"variables": {
            output_key: {"error": error},
            f"{output_key}_status": "FAILED",
        }}
