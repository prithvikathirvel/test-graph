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
from app.nodes.ontology import load_gliner2_model
from app.core.logger import setup_logging
from app.core.runtime import set_runtime_services, clear_runtime_services
from app.core.security import APIKeyAuthMiddleware

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
import app.nodes.ontology

setup_logging()
logger = logging.getLogger(__name__)

# ==========================================
# 1. FastAPI Lifespan
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting Sify Aurora Engine...")
    
    # Core DB & Checkpointer
    try:
        mongo_client = MongoClient(settings.MONGO_URI)
        # Trigger a connection check
        mongo_client.admin.command('ping')
        logger.info("✅ Connected to MongoDB successfully.")
    except Exception as e:
        logger.error(f"❌ Failed to connect to MongoDB: {e}")
        raise

    checkpointer = MongoDBSaver(
        client=mongo_client,
        db_name=getattr(settings, "MONGO_DB_NAME", "agent_studio"),
        collection_name=getattr(settings, "MONGO_CHECKPOINTER_COLLECTION_NAME", "checkpoints")
    )
    
    # State Services
    voice_service = UniversalVoiceService()
    logger.info("Initialized Universal Voice Service.")

    app.state.mongo_client = mongo_client
    app.state.checkpointer = checkpointer
    app.state.voice_service = voice_service
    set_runtime_services(mongo_client=mongo_client, checkpointer=checkpointer)

    # Best-effort uniqueness for side-effect idempotency. Existing deployments
    # continue even if the database role cannot create indexes.
    try:
        mongo_client[getattr(settings, "MONGO_DB_NAME", "agent_studio")][
            "agent_action_journal"
        ].create_index("idempotency_key", unique=True)
    except Exception as e:
        logger.warning(f"Could not ensure agent action journal index: {e}")
    
    # Init MCP Server & Client
    try:
        load_exposed_flows(mongo_client, getattr(settings, "MONGO_DB_NAME", "agent_studio"))
        logger.info("Exposed flows loaded for MCP server.")
        
        await mcp_client_manager.load_from_json("mcpServers.json")
        logger.info("External MCP clients initialized.")
    except Exception as e:
        logger.warning(f"Problem during MCP initialization: {e}")

    # Load GLiNER2 model once at startup
    try:
        load_gliner2_model()
    except Exception as e:
        logger.warning(f"GLiNER2 model not loaded (ontology nodes will be unavailable): {e}")
    
    yield
    
    logger.info("🛑 Shutting down Sify Aurora Engine...")
    await mcp_client_manager.close_all()
    clear_runtime_services()
    mongo_client.close()
    logger.info("Cleanup complete. Goodbye!")

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
app.add_middleware(APIKeyAuthMiddleware)

# ==========================================
# 3. Router Inclusions
# ==========================================
app.include_router(mcp_router)
app.include_router(agents_router)
app.include_router(system_router)