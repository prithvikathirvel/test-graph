import os
import json
import uuid
import logging
import datetime
from typing import Dict, Any, List

from contextlib import AsyncExitStack

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp import ClientSession
from app.core.model import MCPServerConfig, MCPToolCall, MCPTransportType

logger = logging.getLogger(__name__)

# Default values per JSON Schema type
_TYPE_DEFAULTS = {
    "string": "",
    "number": 0,
    "integer": 0,
    "boolean": False,
    "array": [],
    "object": {},
}


class UnifiedMCPClient:
    """Manages connections to external MCP Servers (Filesystem, Infrastructure)."""
    
    def __init__(self):
        self.servers: Dict[str, MCPServerConfig] = {}
        self.active_sessions: Dict[str, ClientSession] = {}
        self.server_tools: Dict[str, list] = {}
        
        # 🚀 FIX: AsyncExitStack safely manages background connections
        self.exit_stack = AsyncExitStack()

    async def load_from_json(self, filepath: str = "mcpServers.json"):
        """Loads external MCP servers on startup."""
        if not os.path.exists(filepath):
            logger.warning(f"⚠️ MCP Config '{filepath}' not found.")
            return

        with open(filepath, 'r') as f:
            data = json.load(f)
            
        for srv in data.get("servers", []):
            config = MCPServerConfig(**srv)
            if config.enabled:
                await self.connect_server(config)

    async def connect_server(self, config: MCPServerConfig) -> bool:
        self.servers[config.server_id] = config
        try:
            # 1. STDIO (Filesystem)
            if config.transport_type == MCPTransportType.STDIO:
                params = StdioServerParameters(
                    command=config.connection_params.get("command"),
                    args=config.connection_params.get("args", []),
                    env=config.connection_params.get("env", {})
                )
                session_manager = stdio_client(params)
                
            # 2. SSE (Infrastructure)
            elif config.transport_type == MCPTransportType.SSE:
                url = config.connection_params.get("url")
                headers = config.connection_params.get("headers", {})
                session_manager = sse_client(url, headers=headers)
                
            else:
                logger.error(f"Unsupported transport: {config.transport_type}")
                return False

            # 🚀 FIX: Use enter_async_context to safely lock the connections in memory
            read, write = await self.exit_stack.enter_async_context(session_manager)
            session = ClientSession(read, write)
            await self.exit_stack.enter_async_context(session)
            
            await session.initialize()
            
            self.active_sessions[config.server_id] = session
            
            # Cache tools
            tools_response = await session.list_tools()
            self.server_tools[config.server_id] = tools_response.tools
            logger.info(f"✅ Connected MCP Server: {config.server_id} ({len(tools_response.tools)} tools via {config.transport_type.upper()})")
            return True
                
        except Exception as e:
            logger.error(f"❌ Failed to connect MCP server {config.server_id}: {e}")
            return False

    async def call_tool(self, call: MCPToolCall) -> Dict[str, Any]:
        if call.server_id not in self.active_sessions:
            return {"error": f"Server {call.server_id} is offline."}

        session = self.active_sessions[call.server_id]
        try:
            result = await session.call_tool(name=call.tool_name, arguments=call.arguments)
            content = [{"type": "text", "text": c.text} for c in result.content if hasattr(c, "text")]
            return {"success": not getattr(result, "isError", False), "data": content}
        except Exception as e:
            logger.error(f"MCP Tool Error [{call.tool_name}]: {e}")
            return {"error": str(e), "success": False}

    def list_server_tools(self, server_id: str) -> List[Dict[str, Any]]:
        """Transform raw MCP Tool objects into the standardized Agent Studio node schema."""
        if server_id not in self.server_tools:
            return []

        now = datetime.datetime.utcnow().isoformat() + "Z"
        transformed = []

        for tool in self.server_tools[server_id]:
            schema = getattr(tool, "inputSchema", {}) or {}
            props = schema.get("properties", {})

            # Build arguments object with defaults from the tool's input schema
            arguments_value = {}
            for key, details in props.items():
                param_type = details.get("type", "string")
                arguments_value[key] = details.get(
                    "default", _TYPE_DEFAULTS.get(param_type, "")
                )

            # Standardized input parameters matching Agent Studio node format
            input_params = [
                {"key": "server_id", "value": server_id, "type": "text"},
                {"key": "tool_name", "value": tool.name, "type": "text"},
                {"key": "arguments", "value": arguments_value, "type": "object"},
                {"key": "timeout", "value": 30, "type": "number"},
            ]

            transformed.append({
                "id": str(uuid.uuid4()),
                "name": tool.name,
                "displayName": tool.name,
                "type": "MCP Tool",
                "tags": ["Run tools from MCP servers", "External tools caller"],
                "description": f"Runs the {tool.name} tool from MCP Server. {tool.description or ''}",
                "specifications": {
                    "original_schema": schema,
                    "server_id": server_id,
                },
                "inputParameters": input_params,
                "outputParameters": [
                    {"key": "output", "value": "tool_execution_result", "type": "object"}
                ],
                "status": True,
                "version": "1.0.0",
                "isPublic": True,
                "isActive": True,
                "createdAt": now,
                "updatedAt": now,
            })

        return transformed

    def get_all_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return all tools grouped by server_id in the standardized schema."""
        return {
            sid: self.list_server_tools(sid)
            for sid in self.server_tools
        }

    async def close_all(self):
        # 🚀 FIX: A single command perfectly unwinds and destroys all connections securely.
        await self.exit_stack.aclose()
        self.active_sessions.clear()
        self.server_tools.clear()
        logger.info("🛑 All MCP Sessions closed securely.")

# Singleton Instance
mcp_client_manager = UnifiedMCPClient()