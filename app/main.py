import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pymongo import MongoClient
from langgraph.checkpoint.mongodb import MongoDBSaver

# --- Local Project Imports ---
from app.core.config import settings
from app.services.voice.service import UniversalVoiceService
from app.services.mcp_client import mcp_client_manager
from app.services.mcp_server import load_exposed_flows
from app.core.logger import setup_logging

# --- API Routers ---
from app.api.mcp import router as mcp_router
from app.api.agents import router as agents_router
from app.api.system import router as system_router

# Auto-register all nodes for LangGraph compiler discovery
import app.nodes.tools
import app.nodes.hitl
import app.nodes.agents
import app.nodes.db_caller
import app.nodes.basics
import app.nodes.classifiers
import app.nodes.logic
import app.nodes.mcp_node
import app.nodes.agent_flow
import app.nodes.db_chat
import app.nodes.react_agent

setup_logging()
logger = logging.getLogger(__name__)

# ==========================================
# 1. FastAPI Lifespan
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Core DB & Checkpointer
    mongo_client = MongoClient(settings.MONGO_URI)
    checkpointer = MongoDBSaver(
        client=mongo_client,
        db_name=getattr(settings, "MONGO_DB_NAME", "agent_studio"),
        collection_name=getattr(settings, "MONGO_CHECKPOINTER_COLLECTION_NAME", "checkpoints")
    )
    
    # State Services
    voice_service = UniversalVoiceService()

    app.state.mongo_client = mongo_client
    app.state.checkpointer = checkpointer
    app.state.voice_service = voice_service
    
    # Init MCP Server & Client
    load_exposed_flows(mongo_client, getattr(settings, "MONGO_DB_NAME", "agent_studio"))
    await mcp_client_manager.load_from_json("mcpServers.json")
    
    yield
    
    await mcp_client_manager.close_all()
    mongo_client.close()

# ==========================================
# 2. App Initialization
# ==========================================
tags_metadata = [
    {"name": "Agent Flows", "description": "Endpoints for invoking and resuming LangGraph agent flows."},
    {"name": "MCP", "description": "Model Context Protocol endpoints for both server (IDE) and client (external tools) operations."},
    {"name": "Admin", "description": "Administrative and system health endpoints."},
]

app = FastAPI(
    title="Sify Aurora",
    lifespan=lifespan,
    openapi_tags=tags_metadata, 
    root_path="/engine"
)

# ==========================================
# 3. Router Inclusions
# ==========================================
app.include_router(mcp_router)
app.include_router(agents_router)
app.include_router(system_router)