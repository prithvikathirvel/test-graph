# Agent Flow Builder (Agent Studio Backend)

## Overview

Agent Studio Backend is a robust FastAPI-based engine designed to compile, execute, and manage complex, stateful agent workflows using LangGraph. It provides a highly flexible execution environment capable of handling conversational flows, complex automated logic, voice-enabled (Speech-to-Text and Text-to-Speech) interactions, and Model Context Protocol (MCP) integrations.

## Architecture and ReAct Agent Blueprint

- For the full codebase review and implementation blueprint, see [`REACT_AGENT_2026_BLUEPRINT.md`](REACT_AGENT_2026_BLUEPRINT.md).
- For focused runtime behavior, schema fields, complete sample flow JSON, approval/resume calls, and output examples, see [`REACT_AGENT_USAGE_GUIDE.md`](REACT_AGENT_USAGE_GUIDE.md).
- For ready-to-copy node JSON, a full public-API test flow, and the single-file Next.js React Flow component, see [`examples/`](examples/README.md).

## Key Features

- **LangGraph Execution Engine:** Dynamically build and invoke persistent, stateful agent graphs. The application automatically manages checkpoints and session state via a MongoDB checkpointer.
- **Voice Integration:** Built-in capabilities to transcribe user audio inputs (using Whisper configurations) and synthesize voice responses (using Piper TTS or other providers) directly within the event loop.
- **Model Context Protocol (MCP):** 
  - *Client Mode:* Automatically connect to external tool ecosystem servers defined in `mcpServers.json` to equip workflows with diverse capabilities.
  - *Server Mode (SSE):* Expose agent workflows back to compliant IDEs (e.g., Cursor, Claude) as executable tools via Server-Sent Events.
- **FastAPI Foundation:** High-performance, asynchronous REST architecture designed to integrate behind Nginx reverse proxies (configured on the `/engine/` root path).

## Tech Stack

- **Framework:** FastAPI, Python (Uvicorn as ASGI server)
- **Agent Orchestration:** LangGraph, LangChain
- **State Management (Checkpointer):** MongoDB (`langgraph-checkpoint-mongodb`, `motor`)
- **Protocol:** Official Model Context Protocol (`mcp`) SDK
- **Process Management:** PM2 (`ecosystem.config.js`)

## Autonomous ReAct Agent v2

The existing `Autonomous ReAct Agent` node is backward compatible: legacy JSON still returns its configured text output. Add `schema_version: "2.0"` and a server-owned profile (`safe_chat`, `support`, `commerce`, `analyst`, or `deep_ops`) to enable the guarded harness.

ReAct v2 includes:

- LangChain `create_agent` with a compatibility fallback for older deployments;
- model/tool/time/concurrency budgets and model fallback middleware;
- typed nested tool schemas for MCP and NodeRegistry tools;
- runtime scope filtering and fail-closed tool policy;
- risk-based durable approve/edit/reject interrupts;
- retries, circuit breaking, idempotency, and a MongoDB action journal;
- input/output PII masking, untrusted tool-result envelopes, and safe logging;
- structured `AgentResult` output while preserving `react_final_answer`;
- token-aware summarization and optional todo planning through middleware.

See [the v2 JSON specification](REACT_AGENT_2026_BLUEPRINT.md#7-backward-compatible-react-agent-v2-json) for complete examples. Side-effecting v2 tools must declare a `risk`, and privileged tools must declare `required_scopes`.

API authentication is compatibility-disabled by default. Production deployments should set `AUTH_REQUIRED=true`, configure `AGENT_API_KEY` and server-owned scopes, or replace the API-key boundary with OIDC/JWT middleware that provides `request.state.auth_context`.

## Getting Started

### Prerequisites

- Python 3.10+
- A running MongoDB instance (locally or cloud-hosted)
- Node.js and PM2 installed centrally (for production process management)

### Installation

1. **Clone and Setup Virtual Environment:**
   Navigate into the project directory, then create and activate a Python virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install Dependencies:**
   Install all required Python packages listed in the requirements file:
   ```bash
   pip install -r requirements.txt
   ```

3. **Environment Configuration:**
   Copy the example environment file and populate it with your specific database URI and API keys:
   ```bash
   cp .env.example .env
   # Edit .env using your preferred editor (nano, vim, etc.)
   ```

### Running the Application

**Development Mode:**
To run the service with hot-reloading for local development, run Uvicorn manually:
```bash
uvicorn app.main:app --port 5000 --reload
```

**Production Mode (PM2):**
To spin the application up reliably in a background daemon with auto-restart capabilities, use PM2:
```bash
pm2 start ecosystem.config.js
```
*To view system logs, use:* `pm2 logs agent-studio-v2`

## API Documentation (Swagger / OpenAPI)

The application automatically provisions self-documenting, interactive API pages via FastAPI. Due to the FastAPI `root_path` configuration targeting `/engine`, the routes are slightly adjusted.

Assuming the server is running on the default port `5000`:

- **Swagger UI Docs:** Check endpoints, test tools, and view schemas at  
  [http://localhost:5000/engine/docs](http://localhost:5000/engine/docs)
  
- **ReDoc Docs:** For alternative, structurally rigid documentation, visit  
  [http://localhost:5000/engine/redoc](http://localhost:5000/engine/redoc)

*(Note: If Nginx is serving the application over the internet on ports 80/443, simply drop the port and visit `https://<your-domain>/engine/docs`)*

## Core System Architecture & Structure

- **`app/main.py`:** The FastAPI application lifespan manager, initializers, and HTTP routes (Invokes, Resumes, and FastMCP SSE hooks).
- **`app/core/`:** Centralized settings and base configurations.
- **`app/engine/`:** Custom Graph Compilers turning JSON schemas into executable LangGraph states.
- **`app/nodes/`:** Modular components mapped to graph workflows (e.g., custom tools, logical switches, DB callers, hitl/Human-in-the-Loop nodes).
- **`app/services/`:** Peripheral services handling Universal Voice (TTS/STT provider mappings) and MCP connection managers.
- **`ecosystem.config.js`:** The Node PM2 config targeting Python app binding on Port 5000.
- **`mcpServers.json`:** JSON spec containing third-party executable servers to append to MCP tools.

## Basic Usage Actions

### Invoking an Agent
Fire a `POST` request to `/engine/agents/invoke/{agent_id}` providing a structured `InvokeReq` body with user inputs or voice files directly. The graph runs until it either `COMPLETES` or successfully `PAUSES` explicitly requiring a prompt response.

### Human-in-the-Loop Resume
If an agent returns a `PAUSED` state awaiting intervention, execute a `POST` request to `/engine/agents/resume/{agent_id}` matching the specific node and thread ID to supply the contextual response block and resume the flow tree computation. 
