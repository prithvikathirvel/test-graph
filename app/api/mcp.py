import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

# --- Local Project Imports ---
from app.core.config import settings
from app.core.model import MCPServerConfig
from app.services.mcp_client import mcp_client_manager
from app.services.mcp_server import mcp_server, register_flow_as_tool
from mcp.server.sse import SseServerTransport

logger = logging.getLogger(__name__)
router = APIRouter(tags=["MCP"])

# ==========================================
# MCP SERVER ROUTES (SSE Backdoor)
# ==========================================
core_mcp_server = getattr(mcp_server, "_mcp_server", None)
sse_transport = SseServerTransport("/messages")

@router.get("/sse")
async def mcp_sse_stream(request: Request):
    """The endpoint Cursor/Claude connects to."""
    if not core_mcp_server:
        logger.error("CRITICAL: Could not extract core Server from FastMCP.")
        raise HTTPException(status_code=500, detail="MCP Server not initialized")
        
    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as streams:
        await core_mcp_server.run(streams[0], streams[1], core_mcp_server.create_initialization_options())

@router.post("/messages")
async def mcp_messages(request: Request):
    """The endpoint IDEs post execution requests to."""
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)


# ==========================================
# MCP CLIENT API (Outbound Tools)
# ==========================================
@router.get("/mcp/servers")
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

@router.post("/mcp/servers")
async def register_mcp_server(config: MCPServerConfig):
    success = await mcp_client_manager.connect_server(config)
    if success:
        return {"message": f"Server {config.server_id} registered successfully."}
    raise HTTPException(status_code=400, detail="Failed to connect server.")

@router.get("/mcp/tools")
async def list_mcp_tools():
    """List all tools grouped by MCP server in the standardized Agent Studio node schema."""
    try:
        grouped = mcp_client_manager.get_all_tools()
        return JSONResponse(content=grouped)
    except Exception as e:
        logger.exception("Error listing all MCP tools")
        return JSONResponse(content={"error": str(e)}, status_code=500)


# ==========================================
# MCP SERVER ADMIN API (Exposing Flows)
# ==========================================
@router.get("/mcp/flows")
async def list_exposed_flows(request: Request):
    db_name = getattr(settings, "MONGO_DB_NAME", "agent_studio")
    db = request.app.state.mongo_client[db_name]
    config = db["mcp_flow_config"].find_one({"server_name": "agent-flows"})
    flows = config.get("exposed_flows", []) if config else []
    return JSONResponse(content={"success": True, "count": len(flows), "flows": flows})

@router.post("/mcp/flows/{agent_id}/expose")
async def expose_flow(agent_id: str, request: Request):
    data = await request.json()
    if "tool_name" not in data or "description" not in data:
        raise HTTPException(status_code=400, detail="Missing tool_name or description")

    db_name = getattr(settings, "MONGO_DB_NAME", "agent_studio")
    db = request.app.state.mongo_client[db_name]
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

@router.delete("/mcp/flows/{agent_id}/unexpose")
async def unexpose_flow(agent_id: str, request: Request):
    db_name = getattr(settings, "MONGO_DB_NAME", "agent_studio")
    db = request.app.state.mongo_client[db_name]
    res = db["mcp_flow_config"].update_one(
        {"server_name": "agent-flows"},
        {"$pull": {"exposed_flows": {"agent_id": agent_id}}}
    )
    if res.modified_count == 0:
        raise HTTPException(status_code=404, detail="Flow not found.")
        
    return {"success": True, "message": "Flow removed. Note: Requires server restart to unbind from active memory."}
