import logging
import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# 1. Create the FastMCP instance exactly like your old code!
mcp_server = FastMCP("Agent Flow Builder 2026")

def register_flow_as_tool(agent_id: str, tool_name: str, description: str):
    """Dynamically creates a tool and adds it using FastMCP."""
    
    # We define the tool function
    async def dynamic_flow_tool(chat_query: str) -> str:
        url = "http://127.0.0.1:8000/invoke"
        payload = {
            "agent_id": agent_id,
            "userInput": {"message": chat_query},
            "voice_enabled": False
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data.get("agent_response", str(data))
        except Exception as e:
            return f"Error executing flow {agent_id}: {str(e)}"

    # FastMCP uses the function's internal name
    dynamic_flow_tool.__name__ = tool_name.replace(" ", "_").replace("-", "_").lower()
    dynamic_flow_tool.__doc__ = description
    
    # Use FastMCP's native method to add the tool!
    mcp_server.add_tool(dynamic_flow_tool, name=tool_name, description=description)
    logger.info(f"🌐 Exposed Flow '{agent_id}' to IDEs as tool: '{tool_name}'")

def load_exposed_flows(mongo_client, db_name: str):
    """Reads MongoDB on startup and registers all exposed flows."""
    db = mongo_client[db_name]
    config = db["mcp_flow_config"].find_one({"server_name": "agent-flows"})
    if config and "exposed_flows" in config:
        for flow in config["exposed_flows"]:
            if flow.get("enabled", True):
                register_flow_as_tool(flow["agent_id"], flow["tool_name"], flow["description"])

# --- OPTIONAL: You can even use decorators exactly like your old code! ---
# @mcp_server.tool()
# async def my_custom_tool(query: str) -> str:
#     return f"You said: {query}"