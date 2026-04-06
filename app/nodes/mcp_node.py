import logging
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState
from app.services.mcp_client import mcp_client_manager
from app.core.model import MCPToolCall

logger = logging.getLogger(__name__)

@NodeRegistry.register("MCP Tool")
async def mcp_tool_caller_node(state: FlowState, node_config: dict) -> dict:
    """Executes a tool on an external MCP Server."""
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    server_id = resolve_placeholders(inputs.get("server_id"), state["variables"])
    tool_name = resolve_placeholders(inputs.get("tool_name"), state["variables"])
    arguments = resolve_placeholders(inputs.get("arguments", {}), state["variables"])
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "mcp_result"

    if not server_id or not tool_name:
        return {"variables": {output_key: {"error": "Missing server_id or tool_name"}}}

    logger.info(f"🔧 Firing MCP Tool: {server_id} -> {tool_name}")
    call_req = MCPToolCall(server_id=server_id, tool_name=tool_name, arguments=arguments)
    
    result = await mcp_client_manager.call_tool(call_req)
    return {"variables": {output_key: result}}