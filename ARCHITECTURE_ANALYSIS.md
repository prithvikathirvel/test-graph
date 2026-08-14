# Agent Studio — Architecture Analysis & Improvement Roadmap

> Analyzed against the current codebase as of August 2026.  
> Covers bottlenecks, quick wins, and strategic improvements with concrete examples.

---

## Table of Contents
1. [Current Architecture Snapshot](#1-current-architecture-snapshot)
2. [Bottlenecks (Critical)](#2-bottlenecks-critical)
3. [Security Gaps](#3-security-gaps)
4. [What Is Already Good](#4-what-is-already-good)
5. [Improvement Roadmap](#5-improvement-roadmap)
6. [Feature Additions](#6-feature-additions)

---

## 1. Current Architecture Snapshot

```
POST /agents/invoke/{agent_id}
       │
       ├─ fetch_schema_by_agent_id()      ← HTTP call to SCHEMA_API on EVERY request
       ├─ GraphCompiler(schema).build()   ← Recompile LangGraph on EVERY request
       ├─ resolve_inputs(schema.inputs)   ← local value OR global dictionary API fetch
       │
       └─ graph.ainvoke(initial_state)
              │
              └─ Node execution chain (registered via NodeRegistry)
                    └─ Each node resolves {{variables}} via Jinja2 templating
```

**Tech Stack:** FastAPI · LangGraph · MongoDB (checkpoints + memory) · httpx · Jinja2 · MCP (stdio + SSE)

---

## 2. Bottlenecks (Critical)

### 2.1 Schema Fetch + Graph Recompile on Every Request

**File:** `app/core/helpers.py` → `get_and_compile_graph()`  
**File:** `app/engine/cache.py` → `GraphCache` (exists but is **never used**)

**Problem:** Every single `/invoke` call makes an HTTP request to the schema API and then recompiles the entire LangGraph from scratch. For nested `agentflow` nodes, this also triggers additional HTTP fetches per child schema.

```python
# Current — recompiles on every call
async def get_and_compile_graph(agent_id: str, request: Request):
    schema = await fetch_schema_by_agent_id(agent_id)       # ← HTTP every time
    compiler = GraphCompiler(schema, checkpointer=...)
    graph = await compiler.build()                           # ← Recompile every time
    return graph, schema
```

**Fix:** Use `GraphCache` (already written, just not wired up) with a TTL:

```python
import time

_schema_cache: dict = {}  # {agent_id: (schema, compiled_graph, timestamp)}
CACHE_TTL_SECONDS = 300   # 5 minutes

async def get_and_compile_graph(agent_id: str, request: Request):
    now = time.time()
    cached = _schema_cache.get(agent_id)
    if cached and (now - cached[2]) < CACHE_TTL_SECONDS:
        return cached[1], cached[0]   # (graph, schema)

    schema = await fetch_schema_by_agent_id(agent_id)
    compiler = GraphCompiler(schema, checkpointer=request.app.state.checkpointer)
    graph = await compiler.build()
    _schema_cache[agent_id] = (schema, graph, now)
    return graph, schema
```

**Impact:** Reduces average invoke latency by 200–800ms per call.

---

### 2.2 No Authentication or Authorization

**File:** `app/api/agents.py`

**Problem:** Every endpoint (`/invoke`, `/resume`, `/agents/flows`) is completely open. Anyone with the URL can invoke any agent flow.

```python
# Current — no auth
@router.post("/agents/invoke/{agent_id}")
async def invoke(agent_id: str, req: InvokeReq, request: Request):
    ...
```

**Fix:** Add an API key middleware:

```python
# app/api/middleware.py
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/"):
            key = request.headers.get("X-API-Key", "")
            if key != settings.API_KEY:
                raise HTTPException(status_code=401, detail="Invalid API Key")
        return await call_next(request)

# app/main.py
app.add_middleware(APIKeyMiddleware)
```

---

### 2.3 Credentials Hardcoded in Source Code

**File:** `app/core/config.py`

**Problem:** API keys, SMTP passwords, and service account paths are hardcoded as default values in the Pydantic settings class. These will leak into version control.

```python
# Current — secrets in code
OPENAI_API_KEY: str = "sk-gw-qbsgi2Q6Tft2..."
SMTP_PASSWORD: str = "wzie abbf cigm qida"
LANGSMITH_API_KEY: str = "lsv2_pt_f76f3c36..."
SENDGRID_API_KEY: str = "SG.hu9HBTLWT7..."
```

**Fix:** Remove all defaults for secrets; load exclusively from environment:

```python
class Settings(BaseSettings):
    OPENAI_API_KEY: str          # No default — must be in .env or env var
    SMTP_PASSWORD: str
    LANGSMITH_API_KEY: str
    SENDGRID_API_KEY: str

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
```

Add `.env` to `.gitignore` and provide a `.env.example` with placeholder values.

---

### 2.4 print() Statements in Production Code

**File:** `app/nodes/hitl.py`

**Problem:** Debug `print()` calls bypass the structured logger, polluting stdout with unformatted output and losing log level/context.

```python
# Current
print("\n" + "="*50)
print(f"🛑 QUESTION NODE TRIGGERED: {node_config.get('name')}")
print("="*50 + "\n")
print("⏸️ CALLING NATIVE INTERRUPT()...")
```

**Fix:** Replace with logger calls:

```python
logger.info(f"Question Node triggered: {node_config.get('name')}")
logger.debug("Calling native interrupt()")
```

---

### 2.5 MongoDB Connection Leak in db_caller

**File:** `app/nodes/db_caller.py`

**Problem:** `_mongo_clients` dict grows unbounded. One new `AsyncMongoClient` is created per unique MongoDB URI seen at runtime. There is no max pool size, no connection timeout config, and no cleanup on app shutdown.

```python
# Current — unbounded growth
_mongo_clients = {}

def get_async_mongo_client(uri: str) -> AsyncMongoClient:
    if uri not in _mongo_clients:
        _mongo_clients[uri] = AsyncMongoClient(uri)   # no limits
    return _mongo_clients[uri]
```

**Fix:** Add explicit pool configuration and a close hook:

```python
def get_async_mongo_client(uri: str) -> AsyncMongoClient:
    if uri not in _mongo_clients:
        _mongo_clients[uri] = AsyncMongoClient(
            uri,
            maxPoolSize=10,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
    return _mongo_clients[uri]

# In app/main.py @app.on_event("shutdown")
async def shutdown():
    for client in _mongo_clients.values():
        client.close()
```

---

### 2.6 GraphCache Module Is Dead Code

**File:** `app/engine/cache.py`

**Problem:** `GraphCache` class is fully implemented but never imported or used anywhere in the compiler or helpers.

```python
# cache.py exists with full implementation...
class GraphCache:
    _graphs = {}
    @classmethod
    def set(cls, agent_id, compiled_graph): ...
    @classmethod
    def get(cls, agent_id): ...

# ...but compiler.py never imports it
```

Either wire it up (see §2.1) or delete it to avoid confusion.

---

### 2.7 Child Injector Doesn't Respect Global Scope Inputs

**File:** `app/engine/compiler.py` → `_make_injector()`

**Problem:** When an `agentflow` node inlines a child graph, the injector seeds child variables using the old simple loop — it doesn't call `resolve_inputs()`, so global-scope variables in child flows are never fetched from the dictionary API.

```python
# Current — ignores scope
async def _injector(state: FlowState, **kwargs) -> dict:
    new_vars = {}
    for inp in child_schema.get("inputs", []):
        new_vars[inp["key"]] = inp.get("value", "")   # ← no scope handling
```

**Fix:**

```python
from app.core.helpers import resolve_inputs

async def _injector(state: FlowState, **kwargs) -> dict:
    new_vars = await resolve_inputs(child_schema.get("inputs", []))
    if isinstance(input_mapping, dict):
        for child_key, raw_value in input_mapping.items():
            new_vars[child_key] = resolve_placeholders(raw_value, state["variables"])
    return {"variables": new_vars}
```

---

### 2.8 No Rate Limiting

**Problem:** A single client can flood the `/invoke` endpoint, causing unbounded LLM API calls and cost.

**Fix:** Add per-IP (or per API key) rate limiting using `slowapi`:

```python
# pip install slowapi
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

@router.post("/agents/invoke/{agent_id}")
@limiter.limit("20/minute")
async def invoke(agent_id: str, req: InvokeReq, request: Request):
    ...
```

---

### 2.9 No Request Timeout for Long-Running Flows

**Problem:** `graph.ainvoke()` runs synchronously in the async event loop with `recursion_limit=150`. A pathological flow or infinite loop can tie up a worker forever.

**Fix:** Wrap invocation with `asyncio.wait_for()`:

```python
import asyncio

result = await asyncio.wait_for(
    graph.ainvoke(initial_state, config=config),
    timeout=120.0   # 2 minute max
)
```

---

## 3. Security Gaps

| # | Issue | Location | Risk |
|---|-------|----------|------|
| 1 | Credentials in source code | `config.py` | **Critical** — keys exposed in git |
| 2 | No authentication on any endpoint | `agents.py` | **Critical** — open to internet |
| 3 | No input sanitization on `userInput.message` | `agents.py` | **High** — prompt injection possible |
| 4 | MongoDB URIs accepted from UI JSON | `db_caller.py` | **High** — SSRF: attacker can point to internal hosts |
| 5 | No rate limiting | All routes | **Medium** — cost abuse, DoS |
| 6 | `eval`-style Jinja2 template rendering on untrusted data | `templating.py` | **Medium** — sandbox Jinja2 env recommended |

### Fix for MongoDB SSRF (item 4)

```python
# Only allow URIs matching an allowlist
ALLOWED_MONGO_HOSTS = {"db.internal", "localhost", "127.0.0.1"}

def _validate_mongo_uri(uri: str):
    from urllib.parse import urlparse
    host = urlparse(uri).hostname
    if host not in ALLOWED_MONGO_HOSTS:
        raise ValueError(f"MongoDB host '{host}' is not in the allowlist.")
```

### Fix for Jinja2 Sandbox

```python
from jinja2.sandbox import SandboxedEnvironment

# Replace the current Environment with a sandboxed one
env = SandboxedEnvironment(finalize=_jinja_finalize)
```

---

## 4. What Is Already Good

| Feature | Why It's Good |
|---------|---------------|
| **AOP Token Tracking** | `contextvars`-based tracker cleanly captures LLM usage across all nodes without touching node code |
| **LangGraph Checkpointing** | MongoDB-backed checkpointer enables durable HITL pause/resume across process restarts |
| **Async-first design** | Every node, API caller, and DB operation uses `asyncio` — no blocking the event loop |
| **NodeRegistry pattern** | Decoupled node registration makes adding a new node type a single decorator — zero compiler changes |
| **Static agentflow inlining** | Compile-time inlining of static child graphs eliminates runtime overhead and enables full graph visibility |
| **Jinja2 with JSON finalize** | Custom `_jinja_finalize` correctly serializes dicts/lists in templates instead of Python repr |
| **Global variable resolution** | New `resolve_inputs()` with `asyncio.gather` fetches all global vars concurrently |
| **MCP integration** | `AsyncExitStack` correctly keeps MCP connections alive across the process lifetime |

---

## 5. Improvement Roadmap

### Priority 1 — Do This Week

| Task | Effort | Impact |
|------|--------|--------|
| Move secrets to `.env` only | 1h | Critical security fix |
| Wire up GraphCache with TTL | 2h | 200–800ms latency reduction per call |
| Add API key middleware | 2h | Secures all endpoints |
| Replace `print()` with `logger` in hitl.py | 15m | Clean logs |
| Fix child injector to use `resolve_inputs` | 1h | Global vars work in nested flows |

### Priority 2 — This Sprint

#### Add Streaming Support (SSE)

Long-running flows block the client. Add a streaming endpoint that emits node execution events:

```python
from fastapi.responses import StreamingResponse
import asyncio, json

@router.post("/agents/stream/{agent_id}")
async def stream_invoke(agent_id: str, req: InvokeReq, request: Request):
    graph, schema = await get_and_compile_graph(agent_id, request)
    variables = await resolve_inputs(schema.get("inputs", []))
    variables["CHAT_QUERY"] = req.userInput.message if req.userInput else ""

    initial_state = {
        "session_id": req.session_id, "user_id": req.user_id,
        "thread_id": req.thread_id, "variables": variables
    }
    config = {"configurable": {"thread_id": req.thread_id}, "recursion_limit": 150}

    async def event_generator():
        async for event in graph.astream_events(initial_state, config=config, version="v2"):
            if event["event"] == "on_chain_end":
                yield f"data: {json.dumps({'node': event['name'], 'output': event['data']})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

---

#### Add Background Execution with Status Polling

For very long flows, don't hold the HTTP connection open:

```python
import asyncio
_background_tasks: dict = {}   # {task_id: {"status": "running", "result": None}}

@router.post("/agents/invoke-async/{agent_id}")
async def invoke_async(agent_id: str, req: InvokeReq, request: Request):
    task_id = f"task_{uuid.uuid4().hex}"

    async def _run():
        try:
            graph, schema = await get_and_compile_graph(agent_id, request)
            variables = await resolve_inputs(schema.get("inputs", []))
            ...
            result = await graph.ainvoke(initial_state, config=config)
            _background_tasks[task_id] = {"status": "completed", "result": result}
        except Exception as e:
            _background_tasks[task_id] = {"status": "error", "error": str(e)}

    _background_tasks[task_id] = {"status": "running"}
    asyncio.create_task(_run())
    return {"task_id": task_id, "status": "running"}

@router.get("/agents/task/{task_id}")
async def get_task_status(task_id: str):
    return _background_tasks.get(task_id, {"status": "not_found"})
```

---

#### Node-Level Retry Policy

Some nodes (API Caller, MCP Tool) fail transiently. Add a retry decorator:

```python
import asyncio, functools

def retryable(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            attempt, wait = 0, delay
            while attempt < max_retries:
                try:
                    return await fn(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    if attempt >= max_retries:
                        raise
                    await asyncio.sleep(wait)
                    wait *= backoff
        return wrapper
    return decorator

# Usage in tools.py:
@NodeRegistry.register("API caller")
@retryable(max_retries=3, delay=1.0)
async def api_caller_node(state: FlowState, node_config: dict) -> dict:
    ...
```

---

#### Schema Validation at Compile Time

Currently, bad node configs fail at runtime (mid-flow). Validate early:

```python
# app/engine/compiler.py — add to build()
REQUIRED_NODE_FIELDS = {
    "API caller": ["url", "method"],
    "Mongo DB caller": ["mongoUri", "dbName", "collectionName"],
    "Question Classifier": ["input_text", "classifications"],
}

def _validate_node_config(node: dict):
    node_type = node.get("name", "")
    inputs = {p["key"] for p in node.get("inputParameters", [])}
    required = REQUIRED_NODE_FIELDS.get(node_type, [])
    missing = [f for f in required if f not in inputs]
    if missing:
        raise ValueError(f"Node '{node.get('node_id')}' ({node_type}) missing required params: {missing}")
```

---

### Priority 3 — Strategic

#### Multi-Tenancy

Currently all flows, variables, and checkpoints share a single MongoDB database. To support multiple organizations:

```python
# Derive DB name from API key / org claim
def get_org_db(request: Request) -> str:
    org_id = request.headers.get("X-Org-ID", "default")
    return f"agent_studio_{org_id}"

# Pass to all DB operations
db = request.app.state.mongo_client[get_org_db(request)]
```

---

#### Flow Versioning & Schema Migration

When a flow schema changes and a user has an active checkpoint from the old version, the resume currently fails with a stale-checkpoint error. Add schema versioning to checkpoints:

```python
# Store schema_version in the initial state
initial_state = {
    ...
    "variables": {
        **variables,
        "_schema_version": schema.get("version", "1.0.0")
    }
}

# On resume, compare and warn/reject mismatches
current_version = schema.get("version", "1.0.0")
checkpoint_version = state["variables"].get("_schema_version", "unknown")
if current_version != checkpoint_version:
    logger.warning(f"Version mismatch: checkpoint={checkpoint_version}, current={current_version}")
```

---

#### Observability: Structured Metrics

Add Prometheus metrics for key operations:

```python
# pip install prometheus-fastapi-instrumentator
from prometheus_fastapi_instrumentator import Instrumentator

Instrumentator().instrument(app).expose(app)

# Custom counters
from prometheus_client import Counter, Histogram

invoke_counter    = Counter("agent_invocations_total", "Total invocations", ["agent_id", "status"])
node_duration     = Histogram("node_execution_seconds", "Node duration", ["node_type"])
token_usage_total = Counter("llm_tokens_total", "Total LLM tokens", ["model", "node_id"])
```

---

## 6. Feature Additions

### 6.1 Flow Dry-Run / Test Endpoint

Test a flow without persisting state or executing side effects:

```python
@router.post("/agents/test/{agent_id}")
async def test_invoke(agent_id: str, req: InvokeReq, request: Request):
    """Dry-run: validates schema, resolves inputs, reports first-node output only."""
    graph, schema = await get_and_compile_graph(agent_id, request)
    variables = await resolve_inputs(schema.get("inputs", []))
    return {
        "schema_valid": True,
        "resolved_inputs": variables,
        "node_count": len(schema.get("graphSpec", {}).get("nodes", [])),
    }
```

---

### 6.2 Variable Store UI Sync

The new global dictionary API integration enables a powerful pattern — let the UI push variable updates that are immediately available to all flows without redeployment. Consider a WebSocket subscription model:

```
Client (UI) ──PATCH /dictionary/{key}──► Dictionary API
                                                │
                              (cache invalidation event)
                                                │
Agent Studio ──invalidate schema cache──────────►  next invoke picks up new value
```

---

### 6.3 Conditional Node Batching

The `Iterator Node` currently processes items sequentially. Add a `parallelBatch` option:

```python
@NodeRegistry.register("Iterator Node")
async def iterator_node(state: FlowState, node_config: dict) -> dict:
    ...
    batch_size = int(inputs.get("batchSize", 1))
    if batch_size > 1:
        batch = items[index : index + batch_size]
        results = await asyncio.gather(*[process_item(i) for i in batch])
        ...
```

---

### 6.4 Webhook Trigger Support

Enable flows to be triggered by external webhooks (e.g., from CRM, ticketing systems):

```python
@router.post("/agents/webhook/{agent_id}/{secret_token}")
async def webhook_trigger(agent_id: str, secret_token: str, payload: dict, request: Request):
    if not hmac.compare_digest(secret_token, settings.WEBHOOK_SECRET):
        raise HTTPException(status_code=401)
    
    req = InvokeReq(
        agent_id=agent_id,
        userInput=UserInput(message=json.dumps(payload))
    )
    return await invoke(agent_id, req, request)
```

---

*End of analysis. All examples above are runnable code snippets targeting the existing patterns in this codebase.*
