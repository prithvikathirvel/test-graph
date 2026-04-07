import uuid
import logging
import asyncio
import httpx
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient

from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.types import Command
from langgraph.errors import GraphInterrupt

# --- Local Project Imports ---
from app.core.config import settings, DEFAULT_VOICE_CONFIG
from app.engine.compiler import GraphCompiler
from app.services.voice.service import UniversalVoiceService, should_run_stt, should_run_tts
from app.utils.memory import save_conversation_turn
from app.services.dynamic_flow import generate_workflow_schema


# --- MCP Imports ---
from app.services.mcp_client import mcp_client_manager
from app.services.mcp_server import mcp_server, load_exposed_flows, register_flow_as_tool
from app.core.model import MCPServerConfig
from mcp.server.sse import SseServerTransport


# Auto-register all nodes
import app.nodes.tools
import app.nodes.hitl
import app.nodes.agents
import app.nodes.db_caller
import app.nodes.basics
import app.nodes.classifiers
import app.nodes.logic
import app.nodes.mcp_node
import app.nodes.agent_flow

from app.core.logger import setup_logging, trace_ctx
import uuid

setup_logging()
logger = logging.getLogger(__name__)

# ==========================================
# 1. FastAPI Lifespan
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    mongo_client = MongoClient(settings.MONGO_URI)
    checkpointer = MongoDBSaver(
        client=mongo_client,
        db_name=getattr(settings, "MONGO_DB_NAME", "agent_studio"),
        collection_name=getattr(settings, "MONGO_CHECKPOINTER_COLLECTION_NAME", "checkpoints")
    )
    
    voice_service = UniversalVoiceService()

    app.state.mongo_client = mongo_client
    app.state.checkpointer = checkpointer
    app.state.voice_service = voice_service
    
    # Load exposed flows to IDEs
    load_exposed_flows(mongo_client, getattr(settings, "MONGO_DB_NAME", "agent_studio"))
    
    # Auto-connect to external tools
    await mcp_client_manager.load_from_json("mcpServers.json")
    
    yield
    
    await mcp_client_manager.close_all()
    mongo_client.close()

tags_metadata = [
    {"name": "Agent Flows", "description": "Endpoints for invoking and resuming LangGraph agent flows."},
    {"name": "MCP", "description": "Model Context Protocol endpoints for both server (IDE) and client (external tools) operations."},
]

app = FastAPI(
    title="Sify Aurora",
    lifespan=lifespan,
    openapi_tags=tags_metadata, 
    root_path="/engine"
)




# ==========================================
# 2. Pydantic Models
# ==========================================

class DynamicFlowReq(BaseModel):
    query: str = Field(description="Natural language description of the workflow to build.")
    session_id: str = Field(default_factory=lambda: f"arch_session_{uuid.uuid4().hex}")
    thread_id: str = Field(default_factory=lambda: f"arch_thread_{uuid.uuid4().hex}")

class UserInput(BaseModel):
    message: Optional[str] = ""
    voiceInput: Optional[str] = None
    uploadedFiles: Optional[List[Any]] = []

class VoiceConfig(BaseModel):
    tts_provider: str = "piper"
    stt_provider: str = "whisper"
    mode: str = "voice_in_voice_out"

class InvokeReq(BaseModel):
    agent_id: str
    userInput: Optional[UserInput] = None
    voice_config: Optional[VoiceConfig] = None
    voice_enabled: bool = False
    user_id: str = "default_user"
    session_id: str = Field(default_factory=lambda: f"session_{uuid.uuid4().hex}")
    thread_id: str = Field(default_factory=lambda: f"thread_{uuid.uuid4().hex}")

class ResumeReq(BaseModel):
    agent_id: str
    user_id: str
    session_id: str
    thread_id: str
    node_id: str
    user_response: Any
    input_type: str = "text"
    voice_config: Optional[VoiceConfig] = None
    voice_enabled: bool = False
    userInput: Optional[UserInput] = None


# ==========================================
# 3. Core Engine Helpers
# ==========================================
async def fetch_schema_by_agent_id(agent_id: str) -> dict:
    base_url = settings.SCHEMA_API_URL.rstrip("/")
    url = f"{base_url}/{agent_id}"
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
    schema = await fetch_schema_by_agent_id(agent_id)
    compiler = GraphCompiler(schema, checkpointer=request.app.state.checkpointer)
    graph = compiler.build()
    return graph, schema

async def format_exact_response(status: str, result_state: dict, req: Any, request: Request, interrupt_data: dict = None, final_user_message: str = None) -> dict:
    agent_response = ""
    payload = None
    response_type = "GENERIC"

    user_msg = final_user_message if final_user_message is not None else (req.userInput.message if req.userInput else "")

    if status == "PAUSED" and interrupt_data:
        agent_response = interrupt_data.get("question", interrupt_data.get("message", "Input required"))
        payload = interrupt_data
        response_type = "QUESTION"
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
        "payload": payload,
        "response_type": response_type,
        "session_id": req.session_id,
        "status": status,
        "thread_id": req.thread_id,
        "user_id": req.user_id
    }

    if req.voice_enabled and agent_response:
        v_config = req.voice_config.model_dump() if req.voice_config else DEFAULT_VOICE_CONFIG
        has_voice_input = bool(req.userInput and req.userInput.voiceInput)
        if should_run_tts(v_config, has_voice_input):
            voice_svc: UniversalVoiceService = request.app.state.voice_service
            b64_audio = await voice_svc.process_tts(str(agent_response), v_config.get("tts_provider", "piper"))
            if b64_audio:
                response["voiceOutput"] = b64_audio

    return response


# ==========================================
# 4. FAST MCP SERVER ROUTES (SSE Backdoor)
# ==========================================
core_mcp_server = getattr(mcp_server, "_mcp_server", None)
if not core_mcp_server:
    logger.error("CRITICAL: Could not extract core Server from FastMCP.")

sse_transport = SseServerTransport("/messages")

@app.get("/sse", tags=["MCP"])

async def mcp_sse_stream(request: Request):
    """The endpoint Cursor/Claude connects to."""
    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as streams:
        await core_mcp_server.run(streams[0], streams[1], core_mcp_server.create_initialization_options())

@app.post("/messages", tags=["MCP"])

async def mcp_messages(request: Request):
    """The endpoint IDEs post execution requests to."""
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)


# ==========================================
# 5. MCP CLIENT API (Outbound Tools)
# ==========================================
@app.get("/mcp/servers", tags=["MCP"])

async def list_mcp_servers():
    status = {}
    for sid, config in mcp_client_manager.servers.items():
        status[sid] = {
            "name": config.name,
            "connected": sid in mcp_client_manager.active_sessions,
            "transport_type": config.transport_type,
            "tool_count": len(mcp_client_manager.server_tools.get(sid, []))
        }
    return JSONResponse(content=status)

@app.post("/mcp/servers", tags=["MCP"])

async def register_mcp_server(config: MCPServerConfig):
    success = await mcp_client_manager.connect_server(config)
    if success:
        return {"message": f"Server {config.server_id} registered successfully."}
    raise HTTPException(status_code=400, detail="Failed to connect server.")

@app.get("/mcp/tools", tags=["MCP"])

async def list_mcp_tools():
    grouped = {}
    for sid, tools in mcp_client_manager.server_tools.items():
        grouped[sid] = [{"name": t.name, "description": t.description} for t in tools]
    return JSONResponse(content=grouped)


# ==========================================
# 6. MCP SERVER ADMIN API (Exposing Flows)
# ==========================================
@app.get("/mcp/flows", tags=["MCP"])

async def list_exposed_flows(request: Request):
    db = request.app.state.mongo_client[getattr(settings, "MONGO_DB_NAME", "agent_studio")]
    config = db["mcp_flow_config"].find_one({"server_name": "agent-flows"})
    flows = config.get("exposed_flows", []) if config else []
    return JSONResponse(content={"success": True, "count": len(flows), "flows": flows})

@app.post("/mcp/flows/{agent_id}/expose", tags=["MCP"])

async def expose_flow(agent_id: str, request: Request):
    data = await request.json()
    if "tool_name" not in data or "description" not in data:
        raise HTTPException(status_code=400, detail="Missing tool_name or description")

    db = request.app.state.mongo_client[getattr(settings, "MONGO_DB_NAME", "agent_studio")]
    flow_config = {
        "agent_id": agent_id,
        "tool_name": data["tool_name"],
        "description": data["description"],
        "enabled": data.get("enabled", True),
    }

    db["mcp_flow_config"].update_one(
        {"server_name": "agent-flows"},
        {"$pull": {"exposed_flows": {"agent_id": agent_id}}}
    )
    db["mcp_flow_config"].update_one(
        {"server_name": "agent-flows"},
        {"$push": {"exposed_flows": flow_config}, "$setOnInsert": {"created_at": datetime.utcnow().isoformat()}},
        upsert=True
    )
    
    register_flow_as_tool(agent_id, data["tool_name"], data["description"])
    return {"success": True, "message": f"Flow exposed as '{data['tool_name']}'."}

@app.delete("/mcp/flows/{agent_id}/unexpose", tags=["MCP"])

async def unexpose_flow(agent_id: str, request: Request):
    db = request.app.state.mongo_client[getattr(settings, "MONGO_DB_NAME", "agent_studio")]
    res = db["mcp_flow_config"].update_one(
        {"server_name": "agent-flows"},
        {"$pull": {"exposed_flows": {"agent_id": agent_id}}}
    )
    if res.modified_count == 0:
        raise HTTPException(status_code=404, detail="Flow not found.")
        
    return {"success": True, "message": "Flow removed. Note: Requires server restart to unbind from active memory."}


# ==========================================
# 7. AGENT FLOW DISCOVERY API
# ==========================================
@app.get("/agents/flows", tags=["Agent Flows"])

async def list_agent_flows(request: Request):
    """
    List all available agent flows with their required input parameters.
    
    Similar to /mcp/tools but for agent flows — returns each flow's 
    agent_id, name, description, and input parameter schema so the UI 
    can populate the agentflow node configuration.
    """
    try:
        db = request.app.state.mongo_client[getattr(settings, "MONGO_DB_NAME", "agent_studio")]
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

        # Normalize and build a clean response
        result = []
        for flow in flows:
            flow_id = flow.get("agent_id") or flow.get("id") or ""
            inputs = flow.get("inputs", [])

            # Format input parameters for easy consumption
            input_params = []
            for inp in inputs:
                input_params.append({
                    "key": inp.get("key", ""),
                    "type": inp.get("type", "text"),
                    "default_value": inp.get("value", ""),
                    "required": True,
                })

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

        return JSONResponse(content={
            "success": True,
            "count": len(result),
            "flows": result,
        })

    except Exception as e:
        logger.error(f"List Agent Flows Error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list agent flows: {e}")


@app.get("/agents/flows/{agent_id}", tags=["Agent Flows"])

async def get_agent_flow_detail(agent_id: str, request: Request):
    """
    Get detailed info for a single agent flow, including its full input 
    parameter schema. Useful for configuring an agentflow node.
    """
    try:
        # Try fetching from Schema API (same source the engine uses)
        schema = await fetch_schema_by_agent_id(agent_id)

        inputs = schema.get("inputs", [])
        input_params = [
            {
                "key": inp.get("key", ""),
                "type": inp.get("type", "text"),
                "default_value": inp.get("value", ""),
                "required": True,
            }
            for inp in inputs
        ]

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
        raise HTTPException(status_code=404, detail=f"Agent flow not found: {e}")


# ==========================================
# 8. MAIN INVOCATION ROUTES
# ==========================================
@app.post("/agents/invoke/{agent_id}", tags=["Agent Flows"])

async def invoke(agent_id: str, req: InvokeReq, request: Request):
    trace_ctx.set(f"flow:{req.thread_id[-6:]}")
    logger.info(f"Starting new invocation for agent: {req.agent_id}")
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
                user_message = await voice_svc.process_stt(req.userInput.voiceInput, provider)

        if user_message:
            variables["CHAT_QUERY"] = user_message

        initial_state = {
            "session_id": req.session_id, 
            "user_id": req.user_id, 
            "thread_id": req.thread_id, 
            "variables": variables
        }
        config = {"configurable": {"thread_id": req.thread_id}, "recursion_limit": 150}

        result = await graph.ainvoke(initial_state, config=config)
        snapshot = graph.get_state(config)
        
        if snapshot.next:
            interrupt_data = snapshot.tasks[0].interrupts[0].value if snapshot.tasks[0].interrupts else {}
            return await format_exact_response("PAUSED", result, req, request, interrupt_data)
            
        return await format_exact_response("COMPLETED", result, req, request)

    except Exception as e:
        logger.error(f"Invoke Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/agents/resume/{agent_id}", tags=["Agent Flows"])

async def resume(agent_id: str, req: ResumeReq, request: Request):
    trace_ctx.set(f"flow:{req.thread_id[-6:]}") 
    logger.info(f"Resuming flow from node: {req.node_id}")
    try:
        graph, schema = await get_and_compile_graph(agent_id, request)
        
        user_response = req.user_response
        if req.voice_enabled and req.userInput and req.userInput.voiceInput:
            v_config = req.voice_config.model_dump() if req.voice_config else DEFAULT_VOICE_CONFIG
            if should_run_stt(v_config, has_voice=True):
                voice_svc: UniversalVoiceService = request.app.state.voice_service
                provider = v_config.get("stt_provider", "whisper")
                user_response = await voice_svc.process_stt(req.userInput.voiceInput, provider)

        config = {"configurable": {"thread_id": req.thread_id}}
        
        # Native LangGraph resume API
        result = await graph.ainvoke(Command(resume=user_response), config=config)
        snapshot = graph.get_state(config)
        
        if snapshot.next:
            interrupt_data = snapshot.tasks[0].interrupts[0].value if snapshot.tasks[0].interrupts else {}
            return await format_exact_response("PAUSED", result, req, request, interrupt_data, final_user_message=user_response)
            
        return await format_exact_response("COMPLETED", result, req, request, final_user_message=user_response)

    except Exception as e:
        logger.error(f"Resume Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/dynamic-flow")
async def dynamic_flow_api(req: DynamicFlowReq, request: Request):
    """Iterative AI Architect: Generates or updates an Agent Flow Builder JSON Schema."""
    mongo_client = request.app.state.mongo_client
    
    try:
        schema_json = await generate_workflow_schema(
            user_query=req.query, 
            mongo_client=mongo_client, 
            session_id=req.session_id,
            thread_id=req.thread_id
        )
        

        return JSONResponse(content={
            "success": True, 
            "message": "Flow generated successfully"
        })

        # return JSONResponse(content={
        #     "success": True, 
        #     "session_id": req.session_id, 
        #     "schema": schema_json
        # })
        
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        logger.error(f"Dynamic Flow Generation Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error during flow generation.")


@app.get("/health")
async def health_check():
    return {"status": "ok"}