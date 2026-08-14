import asyncio
import httpx
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import Request, HTTPException

from app.core.config import settings, DEFAULT_VOICE_CONFIG
from app.engine.compiler import GraphCompiler
from app.engine.cache import GraphCache
from app.services.voice.service import UniversalVoiceService, should_run_tts
from app.utils.memory import save_conversation_turn

logger = logging.getLogger(__name__)

# ── Module-level checkpointer registry ────────────────────────────────────────
_checkpointer = None
_graph_compile_locks: Dict[str, asyncio.Lock] = {}

def set_checkpointer(cp):
    global _checkpointer
    _checkpointer = cp

def get_checkpointer():
    return _checkpointer


async def fetch_global_variable(key: str) -> Any:
    """Fetch a global variable value from the dictionary API."""
    base_url = settings.DICTIONARY_API_URL.rstrip("/")
    url = f"{base_url}/{key}"
    logger.debug(f"Fetching global variable '{key}' from {url}")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "success":
                return data["data"]["value"]
            raise ValueError(f"Dictionary API returned non-success status for key '{key}'")
    except Exception as e:
        logger.error(f"Failed to fetch global variable '{key}': {str(e)}")
        return None


async def resolve_inputs(inputs: List[dict]) -> Dict[str, Any]:
    """Resolve flow inputs by scope: local uses value directly, global fetches from dictionary API."""
    local_vars: Dict[str, Any] = {}
    global_keys: List[str] = []

    for inp in inputs:
        key = inp.get("key", "")
        if not key:
            continue
        scope = inp.get("scope", "local")  # backward-compat: no scope → local
        if scope == "global":
            global_keys.append(key)
        else:
            local_vars[key] = inp.get("value", "")

    if not global_keys:
        return local_vars

    # Fetch all global variables concurrently
    results = await asyncio.gather(
        *[fetch_global_variable(k) for k in global_keys],
        return_exceptions=True,
    )
    for key, result in zip(global_keys, results):
        if isinstance(result, Exception):
            logger.warning(f"Could not resolve global variable '{key}': {result}")
            local_vars[key] = None
        else:
            local_vars[key] = result

    return local_vars


async def fetch_schema_by_agent_id(agent_id: str) -> dict:
    base_url = settings.SCHEMA_API_URL.rstrip("/")
    url = f"{base_url}/{agent_id}"
    logger.debug(f"Fetching schema for agent '{agent_id}' from {url}")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            schema = response.json()
            if not schema:
                raise ValueError("API returned empty schema")
            return schema
    except Exception as e:
        logger.error(f"Schema Fetch Error for {agent_id}: {str(e)}")
        raise HTTPException(status_code=404, detail=f"Failed to fetch schema for agent {agent_id}: {e}")


async def get_and_compile_graph(agent_id: str, request: Request):
    """Return a cached graph or fetch and compile one exactly once per agent.

    The cache is intentionally TTL-bound so schema updates become visible while
    repeated invoke/resume requests avoid network fetches and recompilation.
    """
    cached = GraphCache.get_entry(agent_id)
    if cached:
        logger.debug(f"Using cached graph for agent: {agent_id} ({cached.schema_hash[:12]})")
        return cached.graph, cached.schema

    lock = _graph_compile_locks.setdefault(agent_id, asyncio.Lock())
    async with lock:
        # Another request may have filled the cache while this one waited.
        cached = GraphCache.get_entry(agent_id)
        if cached:
            return cached.graph, cached.schema

        logger.info(f"Compiling graph for agent: {agent_id}")
        schema = await fetch_schema_by_agent_id(agent_id)
        compiler = GraphCompiler(schema, checkpointer=request.app.state.checkpointer)
        graph = await compiler.build()
        entry = GraphCache.set(
            agent_id,
            graph,
            schema=schema,
            ttl_seconds=getattr(settings, "GRAPH_CACHE_TTL_SECONDS", 300),
        )
        logger.info(
            f"Graph compilation complete for agent: {agent_id} "
            f"(schema={entry.schema_hash[:12]})"
        )
        return graph, schema


async def format_exact_response(status, result_state, req, request, interrupt_data=None, final_user_message=None):
    agent_response = ""
    payload        = None
    response_type  = "GENERIC"

    user_msg = final_user_message if final_user_message is not None else (
        req.userInput.message if req.userInput else ""
    )

    if status == "PAUSED" and interrupt_data:
        agent_response = interrupt_data.get("question", interrupt_data.get("message", "Input required"))
        payload        = interrupt_data
        response_type  = "QUESTION"
    elif status == "COMPLETED":
        agent_response = result_state.get("variables", {}).get("final_output", "Flow Completed.")

    if user_msg or agent_response:
        await asyncio.to_thread(
            save_conversation_turn,
            request.app.state.mongo_client,
            req.session_id,
            req.thread_id,
            user_msg,
            str(agent_response)
        )

    response = {
        "agent_response": agent_response,
        "payload":        payload,
        "response_type":  response_type,
        "session_id":     req.session_id,
        "status":         status,
        "thread_id":      req.thread_id,
        "user_id":        req.user_id,
    }

    if req.voice_enabled and agent_response:
        v_config      = req.voice_config.model_dump() if req.voice_config else DEFAULT_VOICE_CONFIG
        top_level_voice = getattr(req, "voiceInput", None)
        has_voice_in  = bool((req.userInput and req.userInput.voiceInput) or top_level_voice)
        if should_run_tts(v_config, has_voice_in):
            voice_svc: UniversalVoiceService = request.app.state.voice_service
            b64_audio = await voice_svc.process_tts(
                str(agent_response), v_config.get("tts_provider", "piper")
            )
            if b64_audio:
                response["voiceOutput"] = b64_audio

    return response


def parse_log_line(line: str):
    pattern = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| ([A-Z ]+) \| (\[[^\]]+\]) \| ([\w\.]+) \| (.*)$"
    match   = re.match(pattern, line.strip())
    if match:
        return {
            "timestamp": match.group(1),
            "level":     match.group(2).strip(),
            "flow_id":   match.group(3).strip(),
            "module":    match.group(4),
            "message":   match.group(5),
        }
    return {"timestamp": "", "level": "INFO", "flow_id": "system", "module": "raw", "message": line.strip()}