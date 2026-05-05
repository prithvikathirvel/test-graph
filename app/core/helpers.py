import asyncio
import httpx
import logging
import re
from datetime import datetime
from typing import Any, Dict, Optional
from fastapi import Request, HTTPException

from app.core.config import settings, DEFAULT_VOICE_CONFIG
from app.engine.compiler import GraphCompiler
from app.services.voice.service import UniversalVoiceService, should_run_tts
from app.utils.memory import save_conversation_turn

logger = logging.getLogger(__name__)

# ── Module-level checkpointer registry ────────────────────────────────────────
_checkpointer = None

def set_checkpointer(cp):
    global _checkpointer
    _checkpointer = cp

def get_checkpointer():
    return _checkpointer


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
    """
    Fetch schema and compile graph — now awaits build() because agentflow
    nodes inline child schemas fetched asynchronously at compile time.
    """
    logger.info(f"Compiling graph for agent: {agent_id}")
    schema = await fetch_schema_by_agent_id(agent_id)
    compiler = GraphCompiler(schema, checkpointer=request.app.state.checkpointer)
    graph = await compiler.build()          # ✅ awaited — async compile
    logger.info(f"Graph compilation complete for agent: {agent_id}")
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
        has_voice_in  = bool(req.userInput and req.userInput.voiceInput)
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