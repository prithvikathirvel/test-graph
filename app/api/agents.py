import json
import logging
import time
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from langgraph.types import Command

# --- Local Project Imports ---
from app.core.config import settings, DEFAULT_VOICE_CONFIG
from app.core.logger import trace_ctx
from app.core.model import InvokeReq, ResumeReq, NodeTestRequest
from app.core.helpers import (
    fetch_schema_by_agent_id,
    get_and_compile_graph,
    format_exact_response,
    resolve_inputs,
)
from app.core.token_tracker import start_tracking, get_tracker, price_from_summary
from app.engine.registry import NodeRegistry
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

        variables = await resolve_inputs(schema.get("inputs", []))

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
            "variables": variables,
            # An authentication middleware/reverse proxy integration may set a
            # verified context on Request.state. Request-body identity never
            # marks itself verified.
            "auth_context": getattr(request.state, "auth_context", {}),
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
        price_usage   = price_from_summary(tracker) if tracker else {}

        if snapshot.next:
            interrupt_data = snapshot.tasks[0].interrupts[0].value if snapshot.tasks[0].interrupts else {}
            logger.info(f"⏸️ Flow '{agent_id}' PAUSED for input at: {snapshot.next}")
            resp = await format_exact_response("PAUSED", result, req, request, interrupt_data)
            resp["token_usage"] = token_summary
            resp["price_usage"] = price_usage
            return resp

        logger.info(f"✅ Flow '{agent_id}' COMPLETED successfully.")
        resp = await format_exact_response("COMPLETED", result, req, request)
        resp["token_usage"] = token_summary
        resp["price_usage"] = price_usage
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
        voice_input = (req.userInput.voiceInput if req.userInput and req.userInput.voiceInput else req.voiceInput)

        if req.voice_enabled and voice_input:
            v_config = req.voice_config.model_dump() if req.voice_config else DEFAULT_VOICE_CONFIG
            if should_run_stt(v_config, has_voice=True):
                voice_svc: UniversalVoiceService = request.app.state.voice_service
                provider = v_config.get("stt_provider", "whisper")
                logger.info(f"Processing STT via {provider}...")
                user_response = await voice_svc.process_stt(voice_input, provider)
                logger.debug(f"STT Result: {user_response}")

        if user_response is None:
            raise HTTPException(
                status_code=422,
                detail="user_response is required when no voiceInput is provided.",
            )

        config = {"configurable": {"thread_id": req.thread_id}}

        # ── AOP: Start token tracking for this request ──────────
        start_tracking()

        logger.debug(f"Resuming graph execution for thread: {req.thread_id}")
        result = await graph.ainvoke(Command(resume=user_response), config=config)
        snapshot = graph.get_state(config)

        # ── AOP: Collect token usage summary ────────────────────
        tracker = get_tracker()
        token_summary = tracker.summary() if tracker else {}
        price_usage   = price_from_summary(tracker) if tracker else {}

        if snapshot.next:
            interrupt_data = snapshot.tasks[0].interrupts[0].value if snapshot.tasks[0].interrupts else {}
            logger.info(f"⏸️ Flow '{agent_id}' PAUSED again at: {snapshot.next}")
            resp = await format_exact_response("PAUSED", result, req, request, interrupt_data, final_user_message=user_response)
            resp["token_usage"] = token_summary
            resp["price_usage"] = price_usage
            return resp

        logger.info(f"✅ Flow '{agent_id}' COMPLETED successfully after resumption.")
        resp = await format_exact_response("COMPLETED", result, req, request, final_user_message=user_response)
        resp["token_usage"] = token_summary
        resp["price_usage"] = price_usage
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


# ==========================================
# NODE ISOLATION TESTING
# ==========================================

def _safe_json(obj: Any) -> Any:
    """Coerce ObjectId / datetime / etc. to strings before JSONResponse."""
    return json.loads(json.dumps(obj, default=str))


@router.get("/nodes")
async def list_registered_nodes():
    """Return all node types currently registered in NodeRegistry."""
    return JSONResponse(content={
        "success": True,
        "count":   len(NodeRegistry._executors),
        "nodes":   sorted(NodeRegistry._executors.keys()),
    })


@router.post("/nodes/test")
async def test_node(req: NodeTestRequest):
    """Execute one node in isolation — no graph, no checkpointer, no flow side-effects."""
    try:
        executor = NodeRegistry.get_executor(req.node_name)
    except NotImplementedError:
        raise HTTPException(status_code=404, detail={
            "error": f"Node '{req.node_name}' not registered.",
            "available": sorted(NodeRegistry._executors.keys()),
        })

    config: Dict[str, Any] = {
        "node_id":          req.node_config.get("node_id", "test-node-001"),
        "name":             req.node_config.get("name", req.node_name),
        "displayName":      req.node_config.get("displayName", req.node_name),
        "inputParameters":  req.node_config.get("inputParameters", []),
        "outputParameters": req.node_config.get("outputParameters", [{"key": "output", "value": "result"}]),
        # forward extra fields: loopPath, completePath, pathMap, etc.
        **{k: v for k, v in req.node_config.items()
           if k not in ("node_id", "name", "displayName", "inputParameters", "outputParameters")},
    }

    # Synthetic FlowState — same shape as the TypedDict; nothing is persisted
    state: Dict[str, Any] = {
        "session_id":             "test-session",
        "user_id":                "test-user",
        "thread_id":              "test-thread",
        "variables":              req.variables,
        "messages":               req.messages,
        "current_iteration_item": None,
        "error":                  None,
    }

    start_tracking()
    t0 = time.perf_counter()

    try:
        result = await executor(state, config)
    except Exception as exc:
        logger.error(f"Node test failed [{req.node_name}]: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    tracker     = get_tracker()

    return JSONResponse(content=_safe_json({
        "success":        True,
        "node_name":      req.node_name,
        "duration_ms":    duration_ms,
        "output":         result.get("variables", {}),
        "messages_added": len(result.get("messages", [])),
        "token_usage":    tracker.summary() if tracker else {},
        "price_usage":    price_from_summary(tracker) if tracker else {},
    }))