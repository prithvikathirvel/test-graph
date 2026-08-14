"""
GraphCompiler — builds a LangGraph StateGraph from a flow schema.

Child flow inlining (agentflow nodes)
───────────────────────────────────────
When a node of type "agentflow" has a STATIC agent_id (plain UUID string),
we inline all child nodes directly into the parent graph at compile time.
Every child node ID is prefixed:

    {agentflow_node_id}__{child_node_id}

so there are zero ID collisions even with deeply nested flows.

When the agent_id is DYNAMIC (contains {{ }}), we cannot resolve it at
compile time. In that case we keep the agentflow node as a normal runtime
node (registered as "Agent Flow Node") so it executes at runtime as before.

Wire-up for inlined children:

    predecessor → injector_node → child_first_node
                                      …child nodes…
                  child_end_node → successor

Decision/iterator nodes that point at an agentflow node have their
path_map entries remapped to the injector_id (static) or kept as-is
(dynamic, handled as a normal node).
"""

import json
import logging
from functools import partial
from typing import Optional

import httpx
from langgraph.graph import StateGraph, START, END

from app.core.config import settings
from app.core.state import FlowState
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.token_tracker import get_tracker
from app.core.redaction import safe_log_value


logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_dynamic(value: str) -> bool:
    """Returns True if value contains an unresolved template placeholder."""
    return "{{" in str(value) and "}}" in str(value)


def _truncate(value, max_len=1000):
    """Redact and cap node values before logging.

    Full values remain in FlowState; only the log representation is changed.
    """
    return safe_log_value(value, max_chars=max_len or 1000)


# ── Router functions ───────────────────────────────────────────────────────────

def decision_router(state: FlowState, node_config: dict) -> str:
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "decision_result"
    raw        = state["variables"].get(output_key, "__END__")
    if raw == "__END__":
        return "__END__"
    prefix = node_config.get("_prefix", "")
    return f"{prefix}{raw}" if prefix else raw


def iterator_router(state: FlowState, node_config: dict) -> str:
    node_id = node_config["node_id"]
    status  = state["variables"].get(f"_iter_status_{node_id}", "complete")
    prefix  = node_config.get("_prefix", "")

    def _p(val):
        return f"{prefix}{val}" if prefix and val else val

    if status == "loop":
        return _p(node_config.get("loopPath"))
    return _p(node_config.get("completePath") or node_config.get("completionPath", "__END__"))


# ── Logging wrapper ────────────────────────────────────────────────────────────

def _make_logged_executor(bound_executor, node_config: dict):
    display = node_config.get("displayName") or node_config.get("name", "Unknown")
    node_id = node_config.get("node_id", "?")

    async def _logged(state, **kwargs):
        logger.info(f"▶️  [{display}] (id={node_id}) — executing…")

        # ── AOP: Tag active node for token tracking ─────────────
        tracker = get_tracker()
        if tracker:
            tracker.set_active_node(node_id)

        # ── Log resolved input parameters ──────────────────────────
        raw_inputs = node_config.get("inputParameters", [])
        if raw_inputs:
            for p in raw_inputs:
                key = p.get("key", "?")
                raw_val = p.get("value", "")
                try:
                    resolved = resolve_placeholders(raw_val, state.get("variables", {}))
                except Exception:
                    resolved = raw_val
                logger.info(f"   inp | {key} = {_truncate(resolved)}")
        else:
            logger.info("   inp | (no input parameters)")

        try:
            result = await bound_executor(state, **kwargs)
        except Exception as exc:
            logger.error(f"❌ [{display}] (id={node_id}) — raised {type(exc).__name__}: {exc}")
            raise
        except BaseException:
            logger.info(f"⏸️  [{display}] (id={node_id}) — paused, waiting for user input.")
            raise

        out_vars = result.get("variables", {}) if isinstance(result, dict) else {}
        for key, val in out_vars.items():
            logger.info(f"   out | {key} = {_truncate(val)}")
        if not out_vars:
            logger.info("   out | (no output variables)")
        msgs = result.get("messages", []) if isinstance(result, dict) else []
        if msgs:
            logger.info(f"   msg | +{len(msgs)} message(s) added.")
        return result

    return _logged


# ── Child schema fetcher ───────────────────────────────────────────────────────

async def _fetch_schema(agent_id: str) -> dict:
    base_url = settings.SCHEMA_API_URL.rstrip("/")
    url      = f"{base_url}/{agent_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        schema = resp.json()
        if not schema:
            raise ValueError(f"Empty schema for agent '{agent_id}'")
        return schema


# ── Injector node factory ──────────────────────────────────────────────────────

def _make_injector(input_mapping: dict, child_schema: dict):
    """Seeds child default inputs then overrides with mapped+resolved values."""
    async def _injector(state: FlowState, **kwargs) -> dict:
        new_vars = {}
        for inp in child_schema.get("inputs", []):
            new_vars[inp["key"]] = inp.get("value", "")
        if isinstance(input_mapping, dict):
            for child_key, raw_value in input_mapping.items():
                new_vars[child_key] = resolve_placeholders(raw_value, state["variables"])
        logger.debug(f"[injector] Setting child vars: {list(new_vars.keys())}")
        return {"variables": new_vars}
    return _injector


# ── Main compiler ──────────────────────────────────────────────────────────────

class GraphCompiler:
    def __init__(self, schema: dict, checkpointer=None):
        self.schema       = schema
        self.checkpointer = checkpointer
        self.workflow     = StateGraph(FlowState)

    async def build(self):
        logger.info("Building state graph from schema...")
        await self._inline_schema(self.schema, prefix="", is_root=True)
        return self.workflow.compile(checkpointer=self.checkpointer)

    async def _inline_schema(self, schema: dict, prefix: str, is_root: bool):
        nodes = schema.get("graphSpec", {}).get("nodes", [])
        edges = schema.get("graphSpec", {}).get("edges", [])

        def pid(node_id: str) -> str:
            return f"{prefix}{node_id}" if prefix else node_id

        start_node_id  = next((n["node_id"] for n in nodes if n["type"] == "start"), None)
        end_node_ids   = [
            n["node_id"] for n in nodes
            if n["type"] in ["output", "outputs"] or "End Node" in n.get("name", "")
        ]
        decision_nodes  = {n["node_id"]: n for n in nodes if n["type"] == "conditions"}
        iterator_nodes  = {n["node_id"]: n for n in nodes if n["type"] == "iterator"}
        agentflow_nodes = {n["node_id"]: n for n in nodes if n["type"] == "agentflow"}

        # ── Classify agentflow nodes: static (inline) vs dynamic (runtime) ────
        static_af:  dict = {}   # node_id → {injector_id, child_first_ids, child_end_ids}
        dynamic_af: set  = set()

        for af_id, af_node in agentflow_nodes.items():
            af_inputs   = {p["key"]: p["value"] for p in af_node.get("inputParameters", [])}
            agent_id    = str(af_inputs.get("agent_id", "")).strip()

            if not agent_id or _is_dynamic(agent_id):
                # Dynamic agent_id — cannot inline at compile time
                logger.info(
                    f"agentflow '{af_id}' has dynamic agent_id '{agent_id}' "
                    "— keeping as runtime node."
                )
                dynamic_af.add(af_id)
                continue

            raw_mapping = af_inputs.get("input_mapping", {})
            if isinstance(raw_mapping, str):
                try:    raw_mapping = json.loads(raw_mapping)
                except: raw_mapping = {}
            input_mapping = raw_mapping if isinstance(raw_mapping, dict) else {}

            try:
                child_schema = await _fetch_schema(agent_id)
            except Exception as e:
                logger.error(
                    f"Failed to fetch child schema '{agent_id}' for agentflow '{af_id}': {e} "
                    "— falling back to runtime node."
                )
                dynamic_af.add(af_id)
                continue

            child_nodes    = child_schema.get("graphSpec", {}).get("nodes", [])
            child_edges    = child_schema.get("graphSpec", {}).get("edges", [])
            child_start_id = next((n["node_id"] for n in child_nodes if n["type"] == "start"), None)
            child_end_ids  = [
                n["node_id"] for n in child_nodes
                if n["type"] in ["output", "outputs"] or "End Node" in n.get("name", "")
            ]
            child_prefix   = f"{pid(af_id)}__"
            injector_id    = f"{child_prefix}__injector__"

            # First real child nodes (direct successors of child start)
            child_first_ids = [
                f"{child_prefix}{e['to']}"
                for e in child_edges if e["from"] == child_start_id
            ]

            # Add injector
            self.workflow.add_node(injector_id, _make_injector(input_mapping, child_schema))
            logger.debug(f"Added injector '{injector_id}'")

            # Recursively inline child schema
            await self._inline_schema(child_schema, prefix=child_prefix, is_root=False)

            static_af[af_id] = {
                "injector_id":     injector_id,
                "child_first_ids": child_first_ids,
                "child_end_ids":   [f"{child_prefix}{eid}" for eid in child_end_ids],
            }
            logger.info(
                f"Inlined child flow '{agent_id}' under prefix '{child_prefix}' "
                f"({len(child_nodes)} nodes)"
            )

        # ── Add non-agentflow, non-start nodes ────────────────────────────────
        for node in nodes:
            nid = node["node_id"]
            if node["type"] == "start":
                continue
            if nid in static_af:
                continue  # replaced by inlined child
            if nid in decision_nodes or nid in iterator_nodes:
                continue  # wired separately below

            executor_name = node.get("name", "Unknown")
            if node["type"] == "iterator":  executor_name = "Iterator Node"
            if node["type"] == "agentflow": executor_name = "Agent Flow Node"  # dynamic fallback

            executor        = NodeRegistry.get_executor(executor_name)
            prefixed_config = {**node, "node_id": pid(nid)}
            bound           = partial(executor, node_config=prefixed_config)
            logged          = _make_logged_executor(bound, prefixed_config)
            self.workflow.add_node(pid(nid), logged)

        # ── Add decision + iterator nodes ─────────────────────────────────────
        for nid, node in {**decision_nodes, **iterator_nodes}.items():
            executor_name   = "Decision Node" if nid in decision_nodes else "Iterator Node"
            executor        = NodeRegistry.get_executor(executor_name)
            prefixed_config = {**node, "node_id": pid(nid)}
            bound           = partial(executor, node_config=prefixed_config)
            logged          = _make_logged_executor(bound, prefixed_config)
            self.workflow.add_node(pid(nid), logged)

        # ── Wire decision conditional edges ───────────────────────────────────
        for dec_id, dec_node in decision_nodes.items():
            path_map   = {"__END__": END}
            conditions = next(
                (p["value"] for p in dec_node["inputParameters"] if p["key"] == "conditions"), []
            )
            for cond in conditions:
                tid = cond.get("nextNode")
                if not tid:
                    continue
                # The router function returns the RAW node ID stored in state variables
                # (e.g. "AgentFlow_node-1024"). The path_map key must match exactly
                # what the router returns. The value is where LangGraph actually routes.
                router_key = pid(tid)   # what decision_router will return
                if tid in static_af:
                    # Static agentflow: route to injector instead of the (non-existent) af node
                    route_to = static_af[tid]["injector_id"]
                else:
                    route_to = pid(tid)
                path_map[router_key] = route_to

            prefixed_config = {**dec_node, "node_id": pid(dec_id), "_prefix": prefix}
            self.workflow.add_conditional_edges(
                pid(dec_id),
                partial(decision_router, node_config=prefixed_config),
                path_map
            )

        # ── Wire iterator conditional edges ───────────────────────────────────
        for iter_id, iter_node in iterator_nodes.items():
            loop_path     = iter_node.get("loopPath")
            complete_path = iter_node.get("completePath") or iter_node.get("completionPath")

            def _route_to(nid):
                # Where LangGraph should actually send execution
                if nid in static_af:
                    return static_af[nid]["injector_id"]
                return pid(nid)

            path_map = {"__END__": END}
            # Key = what iterator_router returns (prefixed raw path value)
            # Value = where to actually route (injector if static af, else same)
            if loop_path:
                path_map[pid(loop_path)]     = _route_to(loop_path)
            if complete_path:
                path_map[pid(complete_path)] = _route_to(complete_path)

            prefixed_config = {**iter_node, "node_id": pid(iter_id), "_prefix": prefix}
            self.workflow.add_conditional_edges(
                pid(iter_id),
                partial(iterator_router, node_config=prefixed_config),
                path_map
            )

        # ── Wire standard edges ───────────────────────────────────────────────
        for edge in edges:
            src, tgt = edge["from"], edge["to"]

            # Skip conditional edge sources — already handled above
            if src in decision_nodes or src in iterator_nodes:
                continue

            # Graph start edge
            if src == start_node_id:
                if is_root:
                    # Root: tgt may itself be an agentflow node
                    if tgt in static_af:
                        self.workflow.add_edge(START, static_af[tgt]["injector_id"])
                    else:
                        self.workflow.add_edge(START, pid(tgt))
                # Child inline: START already connected via injector by parent
                continue

            # Source is a static agentflow → already removed; wire child ends → tgt
            if src in static_af:
                for cend in static_af[src]["child_end_ids"]:
                    if cend in self.workflow.nodes:
                        _tgt = static_af[tgt]["injector_id"] if tgt in static_af else pid(tgt)
                        self.workflow.add_edge(cend, _tgt)
                continue

            # Target is a static agentflow → redirect to injector
            if tgt in static_af:
                self.workflow.add_edge(pid(src), static_af[tgt]["injector_id"])
                continue

            # Normal edge
            self.workflow.add_edge(pid(src), pid(tgt))

        # ── Wire injector → child first nodes ────────────────────────────────
        for af_id, child_info in static_af.items():
            for cfirst in child_info["child_first_ids"]:
                self.workflow.add_edge(child_info["injector_id"], cfirst)

        # ── Terminate end nodes ───────────────────────────────────────────────
        for eid in end_node_ids:
            p = pid(eid)
            if p in self.workflow.nodes:
                self.workflow.add_edge(p, END)