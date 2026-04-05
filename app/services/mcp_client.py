import os
import json
import logging
from typing import Dict, Any
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp import ClientSession
from app.core.model import MCPServerConfig, MCPToolCall, MCPTransportType

logger = logging.getLogger(__name__)

class UnifiedMCPClient:
    """Manages connections to external MCP Servers (Filesystem, Infrastructure)."""
    
    def __init__(self):
        self.servers: Dict[str, MCPServerConfig] = {}
        self.active_sessions: Dict[str, ClientSession] = {}
        self.server_tools: Dict[str, list] = {}
        self._session_managers = {}

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

            # Initialize Connection
            read, write = await session_manager.__aenter__()
            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()
            
            self.active_sessions[config.server_id] = session
            self._session_managers[config.server_id] = session_manager
            
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

    async def close_all(self):
        for sid, session in self.active_sessions.items():
            await session.__aexit__(None, None, None)
            if sid in self._session_managers:
                await self._session_managers[sid].__aexit__(None, None, None)
        logger.info("🛑 All MCP Sessions closed.")

# Singleton Instance
mcp_client_manager = UnifiedMCPClient()