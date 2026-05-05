import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from langgraph.types import Command

# --- Local Project Imports ---
from app.core.config import settings, DEFAULT_VOICE_CONFIG
from app.core.logger import trace_ctx
from app.core.model import InvokeReq, ResumeReq
from app.core.helpers import (
    fetch_schema_by_agent_id, 
    get_and_compile_graph, 
    format_exact_response
)
from app.core.token_tracker import start_tracking, get_tracker
from app.services.voice.service import UniversalVoiceService, should_run_stt

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Agent Flows"])

# ==========================================
# AGENT FLOW DISCOVERY API
# ==========================================
@router.get("/agents/flows")
async def list_agent_flows(request: Request):
    """List all available agent flows with their required input parameters."""
    logger.debug("Listing all available agent flows.")
    try:
        db_name = getattr(settings, "MONGO_DB_NAME", "agent_studio")
        db = request.app.state.mongo_client[db_name]
        collection = db["agent_flows"]

        flows = list(collection.find(
            {"status": {"$ne": "deleted"}},
            {
                "_id": 0,
                "agent_id": 1,
                "id": 1,
                "name": 1,
                "description": 1,
                "inputs": 1,
                "status": 1,
                "type": 1,
                "version": 1,
            }
        ))

        result = []
        for flow in flows:
            flow_id = flow.get("agent_id") or flow.get("id") or ""
            inputs = flow.get("inputs", [])
            input_params = [{
                "key": inp.get("key", ""),
                "type": inp.get("type", "text"),
                "default_value": inp.get("value", ""),
                "required": True,
            } for inp in inputs]

            result.append({
                "agent_id": flow_id,
                "name": flow.get("name", "Unnamed Flow"),
                "description": flow.get("description", ""),
                "type": flow.get("type", "flow"),
                "status": flow.get("status", "active"),
                "version": flow.get("version", "1.0.0"),
                "input_parameters": input_params,
                "input_count": len(input_params),
            })

        return JSONResponse(content={"success": True, "count": len(result), "flows": result})
    except Exception as e:
        logger.error(f"List Agent Flows Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/agents/flows/{agent_id}")
async def get_agent_flow_detail(agent_id: str, request: Request):
    """Get detailed info for a single agent flow."""
    logger.debug(f"Fetching detail for agent flow: {agent_id}")
    try:
        schema = await fetch_schema_by_agent_id(agent_id)
        inputs = schema.get("inputs", [])
        input_params = [{
            "key": inp.get("key", ""),
            "type": inp.get("type", "text"),
            "default_value": inp.get("value", ""),
            "required": True,
        } for inp in inputs]

        return JSONResponse(content={
            "success": True,
            "agent_id": agent_id,
            "name": schema.get("name", ""),
            "description": schema.get("description", ""),
            "type": schema.get("type", "flow"),
            "status": schema.get("status", "active"),
            "version": schema.get("version", "1.0.0"),
            "input_parameters": input_params,
            "input_count": len(input_params),
        })
    except Exception as e:
        logger.error(f"Get Agent Flow Detail Error: {str(e)}")
        raise HTTPException(status_code=404, detail=str(e))


# ==========================================
# MAIN INVOCATION ROUTES
# ==========================================
@router.post("/agents/invoke/{agent_id}")
async def invoke(agent_id: str, req: InvokeReq, request: Request):
    trace_ctx.set(f"flow:{req.thread_id[-6:]}")
    logger.info(f"▶️ Starting new invocation for agent: {agent_id} (Thread: {req.thread_id})")
    logger.debug(f"Invoke Params - User: {req.user_id}, Session: {req.session_id}, Voice: {req.voice_enabled}")
    
    try:
        graph, schema = await get_and_compile_graph(agent_id, request)

        variables = {}
        for inp in schema.get("inputs", []):
            variables[inp["key"]] = inp.get("value", "")

        user_message = req.userInput.message if req.userInput else ""
        if req.voice_enabled and req.userInput and req.userInput.voiceInput:
            v_config = req.voice_config.model_dump() if req.voice_config else DEFAULT_VOICE_CONFIG
            if should_run_stt(v_config, has_voice=True):
                voice_svc: UniversalVoiceService = request.app.state.voice_service
                provider = v_config.get("stt_provider", "whisper")
                logger.info(f"Processing STT via {provider}...")
                user_message = await voice_svc.process_stt(req.userInput.voiceInput, provider)
                logger.debug(f"STT Result: {user_message}")

        if user_message:
            variables["CHAT_QUERY"] = user_message

        initial_state = {
            "session_id": req.session_id, 
            "user_id": req.user_id, 
            "thread_id": req.thread_id, 
            "variables": variables
        }
        config = {"configurable": {"thread_id": req.thread_id}, "recursion_limit": 150}

        # ── AOP: Start token tracking for this request ──────────
        start_tracking()

        logger.debug(f"Entering graph execution for {agent_id}")
        result = await graph.ainvoke(initial_state, config=config)
        snapshot = graph.get_state(config)

        # ── AOP: Collect token usage summary ────────────────────
        tracker = get_tracker()
        token_summary = tracker.summary() if tracker else {}
        
        if snapshot.next:
            interrupt_data = snapshot.tasks[0].interrupts[0].value if snapshot.tasks[0].interrupts else {}
            logger.info(f"⏸️ Flow '{agent_id}' PAUSED for input at: {snapshot.next}")
            resp = await format_exact_response("PAUSED", result, req, request, interrupt_data)
            resp["token_usage"] = token_summary
            return resp
        
        logger.info(f"✅ Flow '{agent_id}' COMPLETED successfully.")
        resp = await format_exact_response("COMPLETED", result, req, request)
        resp["token_usage"] = token_summary
        return resp
    except Exception as e:
        logger.error(f"❌ Invoke Error for {agent_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/agents/resume/{agent_id}")
async def resume(agent_id: str, req: ResumeReq, request: Request):
    trace_ctx.set(f"flow:{req.thread_id[-6:]}") 
    logger.info(f"🔄 Resuming flow '{agent_id}' from node: {req.node_id}")
    try:
        graph, _ = await get_and_compile_graph(agent_id, request)
        
        user_response = req.user_response
        if req.voice_enabled and req.userInput and req.userInput.voiceInput:
            v_config = req.voice_config.model_dump() if req.voice_config else DEFAULT_VOICE_CONFIG
            if should_run_stt(v_config, has_voice=True):
                voice_svc: UniversalVoiceService = request.app.state.voice_service
                provider = v_config.get("stt_provider", "whisper")
                logger.info(f"Processing STT via {provider}...")
                user_response = await voice_svc.process_stt(req.userInput.voiceInput, provider)
                logger.debug(f"STT Result: {user_response}")

        config = {"configurable": {"thread_id": req.thread_id}}

        # ── AOP: Start token tracking for this request ──────────
        start_tracking()

        logger.debug(f"Resuming graph execution for thread: {req.thread_id}")
        result = await graph.ainvoke(Command(resume=user_response), config=config)
        snapshot = graph.get_state(config)

        # ── AOP: Collect token usage summary ────────────────────
        tracker = get_tracker()
        token_summary = tracker.summary() if tracker else {}
        
        if snapshot.next:
            interrupt_data = snapshot.tasks[0].interrupts[0].value if snapshot.tasks[0].interrupts else {}
            logger.info(f"⏸️ Flow '{agent_id}' PAUSED again at: {snapshot.next}")
            resp = await format_exact_response("PAUSED", result, req, request, interrupt_data, final_user_message=user_response)
            resp["token_usage"] = token_summary
            return resp
        
        logger.info(f"✅ Flow '{agent_id}' COMPLETED successfully after resumption.")
        resp = await format_exact_response("COMPLETED", result, req, request, final_user_message=user_response)
        resp["token_usage"] = token_summary
        return resp

    except (KeyError, ValueError) as e:
        # ✅ Stale checkpoint — saved by an older compiled graph whose node IDs
        # no longer exist (e.g. AgentFlow_node-1024 was inlined and removed).
        # Tell the client to start a new session instead of crashing with a 500.
        err = str(e)
        if any(k in err for k in ["AgentFlow", "node", "branch", "target", "condition"]):
            logger.warning(
                f"⚠️  Stale checkpoint for thread '{req.thread_id}': {err}. "
                "Advising client to start a new session."
            )
            return await format_exact_response(
                "PAUSED",
                {},
                req,
                request,
                interrupt_data={
                    "node_id":    "system",
                    "node_name":  "System",
                    "node_type":  "inputs",
                    "input_type": "text",
                    "question":   (
                        "This session is outdated due to a system update. "
                        "Please start a new conversation."
                    ),
                    "options": {},
                },
                final_user_message=user_response,
            )
        # Not a stale checkpoint — re-raise as a real error
        logger.error(f"❌ Resume Error for {agent_id}: {err}", exc_info=True)
        raise HTTPException(status_code=500, detail=err)

    except Exception as e:
        logger.error(f"❌ Resume Error for {agent_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))