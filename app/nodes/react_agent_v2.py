"""
Autonomous ReAct Agent v2
=========================

A hardened rewrite of `app/nodes/react_agent.py` that fixes the three things
that made v1 unreliable:

1. TOOL CALLING
   v1 exposed every non-MCP tool with ONE opaque argument
   (`parameters_json: str`) and a description that never told the model WHICH
   keys to put inside it. The LLM therefore sent `{}` most of the time, the
   underlying node ran with unresolved `{{placeholders}}`, returned garbage,
   and the agent replied "I don't know".
   v2 builds a REAL typed schema per tool (from `parameters` if the UI supplies
   one, otherwise auto-inferred from the `{{placeholders}}` left inside the
   tool config), so the model sees named, described, typed arguments.

2. MEMORY
   v1 only read `state["messages"]`. If the UI opened a fresh `thread_id` for
   every turn (or the checkpoint was cleared), history was empty and the agent
   forgot the user's name. v2 uses HYBRID memory: LangGraph checkpoint first,
   MongoDB `conversation_memory` (keyed by session_id) as a fallback/merge,
   plus a sticky "facts" block so short profile facts survive window trimming.
   It also sanitises history (no orphan ToolMessages, no duplicated current
   question) which is what caused silent provider 400s in v1.

3. OBSERVABILITY / SAFETY
   Every Thought → Action → Observation step is captured into a structured
   trace, tool errors are fed back to the model as actionable text instead of
   killing the run, tool output is truncated, and there is always a non-empty
   final answer.

Registered as: "Autonomous ReAct Agent v2"  (v1 stays untouched).
"""

import json
import re
import asyncio
import inspect
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, create_model
from langchain_core.tools import StructuredTool
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    BaseMessage,
)
from langgraph.prebuilt import create_react_agent

try:  # langgraph >= 0.2
    from langgraph.errors import GraphRecursionError
except Exception:  # pragma: no cover - very old langgraph
    class GraphRecursionError(Exception):
        """Fallback so the node never crashes on import."""

from app.core.config import settings
from app.core.state import FlowState
from app.engine.registry import NodeRegistry
from app.nodes.agents import _get_llm
from app.utils.templating import resolve_placeholders
from app.services.mcp_client import mcp_client_manager
from app.core.model import MCPToolCall

logger = logging.getLogger(__name__)

# How much of a tool result we let the model see (protects the context window)
MAX_TOOL_OUTPUT_CHARS = 4000
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-\[\]]+)\s*\}\}")

# Execution status contract (read by app/core/helpers.derive_completion_status)
STATUS_COMPLETED = "COMPLETED"    # the agent finished its reasoning and answered
STATUS_INCOMPLETE = "INCOMPLETE"  # budget/loop guard stopped it — partial answer returned
STATUS_FAILED = "FAILED"          # hard error — fallback answer returned
STATUS_PREFIX = "_agent_status_"  # per-node key, collision free, scanned by the API layer

# Dedicated memory collection for this node (never mixes with the API-layer store)
MEMORY_COLLECTION = "react_agent_memory"

_TYPE_MAP = {
    "string": str, "str": str, "text": str,
    "integer": int, "int": int,
    "number": float, "float": float,
    "boolean": bool, "bool": bool,
    "array": list, "list": list,
    "object": dict, "dict": dict,
}


# ──────────────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────────────
def _as_text(content: Any) -> str:
    """Flattens LangChain content blocks (Gemini/Anthropic style) into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text", "") or b.get("content", "") or "")
            else:
                parts.append(str(b))
        return "\n".join(p for p in parts if p)
    return str(content)


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("true", "1", "yes", "y", "on")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _safe_tool_name(raw: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", str(raw or "tool").strip().replace(" ", "_"))
    name = re.sub(r"_+", "_", name).strip("_").lower()
    return name[:60] or "tool"


def _compact(value: Any, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, default=str, ensure_ascii=False)
    else:
        text = str(value)
    if len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text


# ──────────────────────────────────────────────────────────────────────────────
# 1. SCHEMA BUILDERS  — the core tool-calling fix
# ──────────────────────────────────────────────────────────────────────────────
def _schema_from_json_schema(schema: dict, model_name: str):
    """MCP JSON-Schema → pydantic model (used for directly injected MCP tools)."""
    fields: Dict[str, Any] = {}
    props = (schema or {}).get("properties", {}) or {}
    required = set((schema or {}).get("required", []) or [])

    for key, spec in props.items():
        py_type = _TYPE_MAP.get(str(spec.get("type", "string")).lower(), str)
        desc = spec.get("description", "") or ""
        if spec.get("enum"):
            desc = f"{desc} Allowed values: {spec['enum']}".strip()
        if key in required:
            fields[key] = (py_type, Field(..., description=desc))
        else:
            fields[key] = (Optional[py_type], Field(default=None, description=desc))

    if not fields:
        return create_model(model_name, __base__=BaseModel)
    return create_model(model_name, **fields)


def _schema_from_declaration(params: List[dict], model_name: str):
    """
    Explicit UI declaration:
      "parameters": [
        {"name":"ticket_id","type":"string","description":"Ticket id","required":true}
      ]
    """
    fields: Dict[str, Any] = {}
    for p in params or []:
        if not isinstance(p, dict):
            continue
        key = p.get("name") or p.get("key")
        if not key:
            continue
        py_type = _TYPE_MAP.get(str(p.get("type", "string")).lower(), str)
        desc = p.get("description", "") or ""
        if p.get("enum"):
            desc = f"{desc} Allowed values: {p['enum']}.".strip()
        if p.get("example"):
            desc = f"{desc} Example: {p['example']}.".strip()
        if _as_bool(p.get("required"), False):
            fields[key] = (py_type, Field(..., description=desc))
        else:
            fields[key] = (Optional[py_type], Field(default=None, description=desc))
    if not fields:
        return create_model(model_name, __base__=BaseModel)
    return create_model(model_name, **fields)


def _state_can_fill(key: str, known_vars: dict) -> bool:
    """
    True only when the flow state holds a REAL value for `key`.
    An empty string / None (e.g. a declared-but-unset flow input like
    `user_name: ""`) must NOT be treated as engine-filled, otherwise the tool
    would silently run with a blank value instead of asking the model.
    """
    if key not in known_vars:
        return False
    value = known_vars.get(key)
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _infer_params_from_config(config: Any, known_vars: dict) -> tuple:
    """
    Auto-inference: any `{{placeholder}}` still sitting inside the tool config
    that the flow state CANNOT fill becomes an argument the LLM must supply.

      "config": {"url": "https://hd/api/tickets/{{ticket_id}}"}
        → argument: ticket_id (string, required)

    Placeholders that already hold a real value in state (e.g. {{HELPDESK_TOKEN}})
    are left alone — the agent must never be asked to invent a secret.

    Returns (inferred_params, prefilled_keys) so the caller can log exactly which
    placeholders the engine is filling behind the model's back.
    """
    found: Dict[str, dict] = {}
    prefilled: List[str] = []

    def scan(node: Any):
        if isinstance(node, str):
            for match in PLACEHOLDER_RE.findall(node):
                root = match.split(".")[0].split("[")[0]
                if root in found or root in prefilled:
                    continue
                if _state_can_fill(root, known_vars):
                    prefilled.append(root)
                    continue
                found[root] = {
                    "name": root,
                    "type": "string",
                    "description": f"Value for '{root}' required by this tool.",
                    "required": True,
                }
        elif isinstance(node, dict):
            for v in node.values():
                scan(v)
        elif isinstance(node, list):
            for v in node:
                scan(v)

    scan(config)
    return list(found.values()), prefilled


# ──────────────────────────────────────────────────────────────────────────────
# 2. TOOL FACTORY
# ──────────────────────────────────────────────────────────────────────────────
def _build_tool(tool_def: dict, state: FlowState, trace: List[dict]) -> Optional[StructuredTool]:
    raw_name = tool_def.get("name") or tool_def.get("tool_name") or "tool"
    tool_name = _safe_tool_name(raw_name)
    description = (tool_def.get("description") or "").strip()
    node_type = tool_def.get("node_type") or tool_def.get("type") or "API caller"
    base_config = tool_def.get("config", {}) or {}
    variables = state.get("variables", {}) or {}

    # ── 2a. MCP tools: inject the real remote schema ─────────────────────────
    if node_type == "MCP Tool":
        server_id = resolve_placeholders(base_config.get("server_id", ""), variables)
        remote_name = resolve_placeholders(base_config.get("tool_name", ""), variables)
        mcp_tool = next(
            (t for t in mcp_client_manager.server_tools.get(server_id, []) if t.name == remote_name),
            None,
        )
        if mcp_tool is not None:
            schema_model = _schema_from_json_schema(
                getattr(mcp_tool, "inputSchema", None) or {}, f"MCP_{tool_name}_Args"
            )

            async def _run_mcp(**kwargs) -> str:
                kwargs = {k: v for k, v in kwargs.items() if v is not None}
                logger.info(f"🔧 [MCP] {server_id}.{remote_name} args={kwargs}")
                try:
                    res = await mcp_client_manager.call_tool(
                        MCPToolCall(server_id=server_id, tool_name=remote_name, arguments=kwargs)
                    )
                    out = _compact(res)
                except Exception as exc:  # noqa: BLE001
                    out = f"TOOL_ERROR: {exc}. Fix the arguments and try again, or answer without this tool."
                trace.append({"tool": tool_name, "args": kwargs, "observation": out[:600]})
                return out

            return StructuredTool.from_function(
                func=lambda **_: "This tool is async-only.",
                coroutine=_run_mcp,
                name=tool_name,
                description=(mcp_tool.description or description or f"MCP tool {remote_name}."),
                args_schema=schema_model,
            )
        logger.warning(
            f"⚠️ MCP tool '{remote_name}' not found on server '{server_id}' — tool skipped for this run "
            "(the agent will answer without it). Check mcpServers.json / the MCP connection."
        )
        return None

    # ── 2b. Any registered node becomes a typed tool ─────────────────────────
    declared = tool_def.get("parameters") or tool_def.get("args") or []
    if declared:
        schema_model = _schema_from_declaration(declared, f"{tool_name}_Args")
        param_names = [p.get("name") or p.get("key") for p in declared if isinstance(p, dict)]
        # A declared argument always WINS over a same-named flow variable for this
        # call (kwargs are merged on top of state). Surface it so nobody is surprised.
        shadowed = [p for p in param_names if _state_can_fill(str(p), variables)]
        if shadowed:
            logger.info(
                f"🧩 Tool '{tool_name}': argument(s) {shadowed} shadow existing flow variable(s) "
                "— the model's value wins for this call; the flow variable is not modified."
            )
    else:
        inferred, prefilled = _infer_params_from_config(base_config, variables)
        schema_model = _schema_from_declaration(inferred, f"{tool_name}_Args")
        param_names = [p["name"] for p in inferred]
        if prefilled:
            logger.info(
                f"🧩 Tool '{tool_name}': placeholder(s) {prefilled} are filled from flow state "
                "(not exposed to the model). Declare them under 'parameters' if the model should choose them."
            )

    try:
        executor = NodeRegistry.get_executor(node_type)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"❌ Tool '{tool_name}' skipped — unknown node_type '{node_type}': {exc}")
        return None

    full_description = description or f"Executes the '{node_type}' node."
    if param_names:
        full_description += f" Required inputs: {', '.join(str(p) for p in param_names)}."
    full_description += " Call it only when you actually need this data."

    output_var = f"_react2_out_{tool_name}"

    async def _run_node(**kwargs) -> str:
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        logger.info(f"🔧 [{node_type}] {tool_name} args={kwargs}")
        temp_state: Dict[str, Any] = dict(state)
        temp_state["variables"] = {**variables, **kwargs}
        fake_config = {
            "node_id": f"react2_{tool_name}",
            "name": tool_name,
            "type": node_type,
            "inputParameters": [{"key": k, "value": v} for k, v in base_config.items()],
            "outputParameters": [{"key": "output", "value": output_var}],
        }
        try:
            result = await executor(temp_state, fake_config)
            payload = (result or {}).get("variables", {}).get(output_var, result)
            observation = _compact(payload)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"❌ Tool '{tool_name}' failed: {exc}", exc_info=True)
            observation = (
                f"TOOL_ERROR: {exc}. Check your arguments ({', '.join(map(str, param_names)) or 'none'}) "
                "and retry once; if it keeps failing, answer the user with what you already know."
            )
        trace.append({"tool": tool_name, "args": kwargs, "observation": observation[:600]})
        return observation

    return StructuredTool.from_function(
        func=lambda **_: "This tool is async-only.",
        coroutine=_run_node,
        name=tool_name,
        description=full_description[:1024],
        args_schema=schema_model,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3. MEMORY LAYER  — the "it forgot my name" fix
# ──────────────────────────────────────────────────────────────────────────────
_mongo_client = None


def _get_mongo():
    """Lazy, cached pymongo client (nodes have no access to the FastAPI request)."""
    global _mongo_client
    if _mongo_client is None:
        try:
            from pymongo import MongoClient
            _mongo_client = MongoClient(settings.MONGO_URI, serverSelectionTimeoutMS=3000)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Memory: cannot reach MongoDB ({exc}). Using state memory only.")
            _mongo_client = False
    return _mongo_client or None


def _load_db_history(session_id: str, window: int) -> List[BaseMessage]:
    """
    Reads durable history for this session.

    Priority:
      1. `react_agent_memory` — written by THIS node (exact agent turns, no noise)
      2. `conversation_memory` — written by the API layer (`save_conversation_turn`)
    """
    client = _get_mongo()
    if not client or not session_id:
        return []

    # 1. our own store
    try:
        db = client[getattr(settings, "MONGO_DB_NAME", "agent_studio")]
        record = db[MEMORY_COLLECTION].find_one({"session_id": session_id})
        if record and record.get("messages"):
            raw = record["messages"][-(window * 2):]
            msgs: List[BaseMessage] = []
            for m in raw:
                content = m.get("content", "")
                if not content:
                    continue
                if m.get("role") == "user":
                    msgs.append(HumanMessage(content=content))
                else:
                    msgs.append(AIMessage(content=content))
            if msgs:
                return msgs
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Memory: '{MEMORY_COLLECTION}' read failed: {exc}")

    # 2. shared API-layer store
    try:
        from app.utils.memory import get_conversation_history
        return get_conversation_history(client, session_id, window) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Memory: DB history load failed: {exc}")
        return []


def _save_db_turn(session_id: str, user_query: str, answer: str, node_id: str = "") -> None:
    """
    Persists this turn so memory survives new thread_ids, cleared checkpoints and
    restarts. Blocking pymongo call — always run through asyncio.to_thread().
    """
    client = _get_mongo()
    if not client or not session_id or not (user_query or answer):
        return
    try:
        db = client[getattr(settings, "MONGO_DB_NAME", "agent_studio")]
        now = datetime.utcnow()
        db[MEMORY_COLLECTION].update_one(
            {"session_id": session_id},
            {
                "$setOnInsert": {"session_id": session_id, "created_at": now},
                "$set": {"updated_at": now, "last_node_id": node_id},
                "$push": {
                    "messages": {
                        "$each": [
                            {"role": "user", "content": user_query, "timestamp": now},
                            {"role": "assistant", "content": answer, "timestamp": now},
                        ],
                        # keep the document bounded — last 100 messages (50 turns)
                        "$slice": -100,
                    }
                },
            },
            upsert=True,
        )
        logger.debug(f"🧠 Memory: turn persisted for session '{session_id}'.")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Memory: could not persist turn: {exc}")


def _sanitize(messages: List[BaseMessage]) -> List[BaseMessage]:
    """
    Removes what makes providers throw 400s / makes the agent lose context:
      • ToolMessages whose parent AI tool_call is no longer in the window
      • AI tool-call stubs whose ToolMessage results were trimmed away
      • empty messages
    """
    clean: List[BaseMessage] = []
    pending_ids: set = set()
    for msg in messages:
        if isinstance(msg, ToolMessage):
            if getattr(msg, "tool_call_id", None) in pending_ids:
                clean.append(msg)
            continue
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            pending_ids.update(tc.get("id") for tc in msg.tool_calls if tc.get("id"))
            clean.append(msg)
            continue
        if _as_text(getattr(msg, "content", "")).strip():
            clean.append(msg)
    # Drop a trailing AI tool-call stub that has no result (invalid for the API)
    while clean and isinstance(clean[-1], AIMessage) and getattr(clean[-1], "tool_calls", None):
        clean.pop()
    return clean


def _trim_to_turns(messages: List[BaseMessage], window: int) -> List[BaseMessage]:
    """Keeps the last `window` human turns (with everything that followed them)."""
    if window <= 0:
        return []
    starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(starts) <= window:
        return messages
    return messages[starts[-window]:]


def _build_memory(
    state: FlowState,
    user_query: str,
    window: int,
    mode: str,
) -> List[BaseMessage]:
    """mode: auto | state | db | off"""
    if mode == "off" or window <= 0:
        return []

    state_msgs = list(state.get("messages", []) or [])
    session_id = str(state.get("session_id") or state.get("variables", {}).get("session_id") or "")

    history: List[BaseMessage] = []
    source = mode
    if mode == "state":
        history = state_msgs
    elif mode == "db":
        history = _load_db_history(session_id, window)
    else:  # auto — take whichever store actually has the conversation
        db_msgs = _load_db_history(session_id, window)
        state_turns = sum(1 for m in state_msgs if isinstance(m, HumanMessage))
        db_turns = sum(1 for m in db_msgs if isinstance(m, HumanMessage))
        if db_turns > state_turns:
            history, source = db_msgs, "auto→db"
            logger.info(f"🧠 Memory: using MongoDB history ({db_turns} turns) — checkpoint had {state_turns}.")
        else:
            history, source = state_msgs, "auto→checkpoint"

    history = _trim_to_turns(_sanitize(history), window)

    # Never send the current question twice — that is what makes models answer
    # the previous turn or ignore the new one.
    while history and isinstance(history[-1], HumanMessage) and \
            _as_text(history[-1].content).strip() == user_query.strip():
        history = history[:-1]

    logger.info(f"🧠 Memory: {len(history)} message(s) in context (mode={source}, window={window}).")
    return history


def _facts_block(state: FlowState, extra: str = "") -> str:
    """
    Sticky facts survive window trimming. Anything the flow stored under
    `user_*`, `customer_*`, or `remembered_*` is shown to the model every turn.
    """
    variables = state.get("variables", {}) or {}
    lines = []
    for key, value in variables.items():
        if key.startswith(("user_", "customer_", "remembered_", "profile_")) and isinstance(
            value, (str, int, float, bool)
        ):
            lines.append(f"- {key}: {value}")
    if extra:
        lines.append(f"- {extra}")
    if not lines:
        return ""
    return "\n\n### KNOWN FACTS ABOUT THIS USER/SESSION (already established) ###\n" + "\n".join(lines[:25])


# ──────────────────────────────────────────────────────────────────────────────
# 4. THE NODE
# ──────────────────────────────────────────────────────────────────────────────
BASE_RULES = """
### HOW YOU MUST WORK ###
1. Read the conversation history first. If the user already told you something
   (their name, an id, a preference), USE IT — never say you don't know it.
2. Decide if a tool is needed. Simple/personal/greeting questions: answer directly.
3. When you call a tool, fill EVERY required argument with real values taken from
   the user's message, the history or the known facts. Never invent ids, never
   send empty arguments, never pass '{{placeholders}}'.
4. Read the tool result before answering. If it starts with TOOL_ERROR, fix the
   arguments and retry ONCE, then answer with what you know.
5. Never call the same tool twice with the same arguments.
6. Finish with one clear, complete answer for the user. Never reply with an empty
   message and never expose raw JSON unless asked.
""".strip()


@NodeRegistry.register("ReAct Agent v2")
async def react_agent_v2_node(state: FlowState, node_config: dict) -> dict:
    logger.info("🧠 Entering Autonomous ReAct Agent v2...")
    variables = state.get("variables", {}) or {}
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}

    # ── config ───────────────────────────────────────────────────────────────
    model_choice = str(resolve_placeholders(inputs.get("model", "gemini-2.5-pro"), variables))
    system_prompt = str(resolve_placeholders(
        inputs.get("system_prompt", "You are a helpful autonomous agent."), variables))
    memory_window = _as_int(resolve_placeholders(inputs.get("memory_window", 10), variables), 10)
    memory_mode = str(resolve_placeholders(inputs.get("memory_mode", "auto"), variables)).lower()
    max_iterations = _as_int(resolve_placeholders(inputs.get("max_iterations", 8), variables), 8)
    temperature = float(resolve_placeholders(inputs.get("temperature", 0), variables) or 0)
    response_format = str(resolve_placeholders(inputs.get("response_format", "text"), variables)).lower()
    return_trace = _as_bool(resolve_placeholders(inputs.get("return_trace", False), variables), False)
    persist_memory = _as_bool(resolve_placeholders(inputs.get("persist_memory", True), variables), True)
    memory_context = str(resolve_placeholders(inputs.get("memory_context", ""), variables) or "")
    fallback_answer = str(resolve_placeholders(
        inputs.get("fallback_answer", "I could not complete that request. Could you rephrase it?"), variables))

    user_query = str(resolve_placeholders(inputs.get("user_query", "{{CHAT_QUERY}}"), variables)).strip()
    if not user_query or "{{" in user_query:
        # Never send an empty/unresolved turn to the provider (Gemini 400s on it)
        prior_humans = [m for m in (state.get("messages") or []) if isinstance(m, HumanMessage)]
        user_query = _as_text(prior_humans[-1].content).strip() if prior_humans else "Hello"
        logger.warning(f"user_query was empty/unresolved — using fallback: '{user_query[:60]}'")

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "react_final_answer"
    node_id = str(node_config.get("node_id", "react_v2"))
    status_key = f"{STATUS_PREFIX}{node_id}"

    # ── tools ────────────────────────────────────────────────────────────────
    tools_def = resolve_placeholders(inputs.get("tools", []), variables)
    if isinstance(tools_def, str):
        try:
            tools_def = json.loads(tools_def)
        except Exception:
            try:
                tools_def = json.loads(tools_def.replace("'", '"'))
            except Exception:
                logger.error("tools input is not valid JSON — running with zero tools.")
                tools_def = []
    if isinstance(tools_def, dict):
        tools_def = [tools_def]

    trace: List[dict] = []
    tools = [t for t in (_build_tool(td, state, trace) for td in tools_def or []) if t]
    logger.info(f"🛠️  {len(tools)} tool(s) ready: {[t.name for t in tools]}")

    # ── prompt ───────────────────────────────────────────────────────────────
    prompt = system_prompt.strip() + "\n\n" + BASE_RULES
    prompt += _facts_block(state, memory_context)
    if response_format == "html":
        prompt += "\n\nFORMAT: reply with light HTML (<b>, <ul>, <li>, <br>) for UI rendering."
    elif response_format == "json":
        prompt += "\n\nFORMAT: reply with raw valid JSON only, no markdown fences."
    if not tools:
        prompt += "\n\nNOTE: you have no tools this turn — answer from history and knowledge."

    history = _build_memory(state, user_query, memory_window, memory_mode)

    # ── build agent (version-agnostic kwargs) ────────────────────────────────
    try:
        llm = _get_llm(model_choice, temperature=temperature)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"❌ LLM init failed: {exc}", exc_info=True)
        return {
            "variables": {
                output_key: f"Model initialization error: {exc}",
                status_key: STATUS_FAILED,
                "execution_status": STATUS_FAILED,
            }
        }

    agent_kwargs: Dict[str, Any] = {}
    params = inspect.signature(create_react_agent).parameters
    if "prompt" in params:
        agent_kwargs["prompt"] = prompt
    elif "state_modifier" in params:
        agent_kwargs["state_modifier"] = prompt
    elif "messages_modifier" in params:
        agent_kwargs["messages_modifier"] = prompt

    agent = create_react_agent(llm, tools=tools, **agent_kwargs)

    payload: List[BaseMessage] = []
    if not agent_kwargs:                      # very old langgraph — inline system msg
        payload.append(SystemMessage(content=prompt))
    payload.extend(history)
    payload.append(HumanMessage(content=user_query))

    # ── run the ReAct loop ───────────────────────────────────────────────────
    # Recursion safety: we count the Think→Act cycles ourselves and STOP consuming
    # the stream one step before LangGraph's own limit can ever be reached, so a
    # GraphRecursionError is structurally impossible. The limit below is only a
    # second safety net (and it is caught, never propagated).
    final_answer = ""
    tool_calls_made: List[dict] = []
    status = STATUS_COMPLETED
    stop_reason = "natural_finish"
    iterations = 0
    seen_calls: Dict[str, int] = {}
    recursion_limit = max(8, max_iterations * 2 + 6)

    try:
        logger.info(
            f"🚀 ReAct v2 loop | model={model_choice} | max_iterations={max_iterations} "
            f"| recursion_limit={recursion_limit} | query='{user_query[:70]}'"
        )
        stream = agent.astream(
            {"messages": payload},
            config={"recursion_limit": recursion_limit,
                    "run_name": f"ReActV2:{user_query[:24]}"},
            stream_mode="updates",
        )
        try:
            async for chunk in stream:
                budget_hit = False
                for node_name, update in (chunk or {}).items():
                    msgs = (update or {}).get("messages") or []
                    if not msgs:
                        continue
                    last = msgs[-1]

                    if node_name in ("agent", "model", "llm"):
                        calls = getattr(last, "tool_calls", None) or []
                        if calls:
                            iterations += 1
                            for tc in calls:
                                signature = f"{tc.get('name')}::{json.dumps(tc.get('args') or {}, sort_keys=True, default=str)}"
                                seen_calls[signature] = seen_calls.get(signature, 0) + 1
                                logger.info(f"🤔 [Thought→Action {iterations}] {tc.get('name')} {tc.get('args')}")
                                tool_calls_made.append({"tool": tc.get("name"), "args": tc.get("args")})

                            # Loop guard: the same call repeated 3x means the model is stuck
                            if any(count >= 3 for count in seen_calls.values()):
                                stop_reason = "repeated_tool_call"
                                budget_hit = True
                            # Budget guard: stop BEFORE langgraph's recursion limit
                            elif iterations >= max_iterations:
                                stop_reason = "max_iterations"
                                budget_hit = True
                        else:
                            text = _as_text(last.content).strip()
                            if text:
                                final_answer = text
                                logger.info(f"🗣️ [Answer] {text[:200]}")
                    elif node_name == "tools":
                        logger.info(f"✅ [Observation] {_as_text(last.content)[:300]}")

                if budget_hit:
                    logger.warning(
                        f"🛑 Stopping ReAct loop early ({stop_reason}) after {iterations} iteration(s) "
                        "— summarising what we have instead of recursing."
                    )
                    break
        finally:
            # Always close the async generator cleanly (no pending-task warnings)
            aclose = getattr(stream, "aclose", None)
            if aclose:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    pass

        if stop_reason != "natural_finish":
            status = STATUS_INCOMPLETE
            final_answer = ""      # force a clean summary of what was gathered

        if not final_answer:
            # Recovery: one tool-free completion so the user always gets an answer
            logger.warning(f"No final answer from loop ({stop_reason}) — running recovery completion.")
            try:
                closing = (
                    "Answer the user now, using the conversation and the tool results below. "
                    "Do not call any tools. If something is still missing, say clearly what is "
                    "missing and give the best partial answer you can."
                )
                recovery = await llm.ainvoke(
                    [SystemMessage(content=prompt + "\n\n" + closing)]
                    + history
                    + [HumanMessage(content=user_query)]
                    + ([HumanMessage(content="Tool results so far:\n" + _compact(trace))] if trace else [])
                )
                final_answer = _as_text(getattr(recovery, "content", "")).strip()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Recovery completion failed: {exc}")
                status = STATUS_FAILED
        if not final_answer:
            final_answer = fallback_answer
            status = STATUS_FAILED if status == STATUS_COMPLETED else status

    except GraphRecursionError as exc:
        # Second safety net — should never trigger thanks to the budget guard above.
        logger.error(f"⚠️ Recursion limit reached (caught, never raised to the flow): {exc}")
        status, stop_reason = STATUS_INCOMPLETE, "recursion_limit"
        if not final_answer:
            try:
                recovery = await llm.ainvoke(
                    [SystemMessage(content=prompt + "\n\nYou ran out of reasoning steps. Summarise what you "
                                                    "found and state clearly what is still missing. No tools.")]
                    + history
                    + [HumanMessage(content=user_query)]
                    + ([HumanMessage(content="Tool results so far:\n" + _compact(trace))] if trace else [])
                )
                final_answer = _as_text(getattr(recovery, "content", "")).strip()
            except Exception as rec_exc:  # noqa: BLE001
                logger.error(f"Recovery after recursion limit failed: {rec_exc}")
        final_answer = final_answer or fallback_answer
    except Exception as exc:  # noqa: BLE001
        logger.error(f"❌ ReAct v2 error: {exc}", exc_info=True)
        status, stop_reason = STATUS_FAILED, f"error: {type(exc).__name__}"
        final_answer = final_answer or fallback_answer

    logger.info(
        f"🏁 ReAct v2 finished | status={status} | reason={stop_reason} | "
        f"iterations={iterations} | tools_called={len(tool_calls_made)}"
    )

    # ── outputs + memory write-back ──────────────────────────────────────────
    # parse so downstream dot-notation (e.g. {{proposed_plan.status}}) works
    stored_answer: Any = final_answer
    if response_format == "json" and isinstance(final_answer, str):
        try:
            stored_answer = json.loads(final_answer)
        except (json.JSONDecodeError, ValueError):
            pass

    out_vars: Dict[str, Any] = {
        output_key: stored_answer,
        f"{output_key}_status": status,
        f"{output_key}_tool_calls": tool_calls_made,
        f"{output_key}_iterations": iterations,
        # contract for the API layer (app/core/helpers.derive_completion_status)
        status_key: status,
        "execution_status": status,
    }
    if return_trace:
        out_vars[f"{output_key}_trace"] = trace

    # Durable memory: survives new thread_ids, cleared checkpoints and restarts.
    if persist_memory:
        session_id = str(state.get("session_id") or variables.get("session_id") or "")
        if session_id:
            await asyncio.to_thread(_save_db_turn, session_id, user_query, final_answer, node_id)

    # Checkpoint memory: the next turn on the same thread sees this exchange.
    return {
        "variables": out_vars,
        "messages": [HumanMessage(content=user_query), AIMessage(content=final_answer)],
    }
