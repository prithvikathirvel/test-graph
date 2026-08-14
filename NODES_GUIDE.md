# Agent Studio — Nodes Guide

> Complete reference: how `Autonomous ReAct Agent` correlates with the rest of the engine,
> the full inventory of existing nodes, and every new node proposed with schema + usage examples.
>
> **2026 v2 update:** `app/nodes/react_agent.py` is now a thin backward-compatible adapter over `app/agents/*`. New flows use LangChain `create_agent`, profiles, the guarded Tool Gateway, typed schemas, budgets, durable approvals, retries/idempotency, and structured results. Older flow JSON continues to use the `legacy` compatibility profile. The original call-chain examples below describe the legacy behavior; use [`REACT_AGENT_2026_BLUEPRINT.md`](REACT_AGENT_2026_BLUEPRINT.md) for the implemented v2 contract.

---

## Table of Contents
1. [How ReAct Agent Connects to the Codebase](#1-how-react-agent-connects-to-the-codebase)
2. [Existing Nodes — Quick Reference](#2-existing-nodes--quick-reference)
3. [New Nodes to Introduce](#3-new-nodes-to-introduce)

---

## 1. How ReAct Agent Connects to the Codebase

### 1.1 The Full Call Chain

```
POST /agents/invoke/{agent_id}
  │
  └── graph.ainvoke(initial_state)
        │
        └── react_agent_node()            ← app/nodes/react_agent.py
              │
              ├── _get_llm(model_choice)  ← app/nodes/agents.py  (shared LLM factory)
              │
              ├── _build_dynamic_tool()   ← react_agent.py  (per tool in tools_json)
              │     │
              │     ├─ [MCP Tool path]
              │     │    └── mcp_client_manager.call_tool()
              │     │          └── app/services/mcp_client.py → active MCP session
              │     │
              │     └─ [Any other node type path]
              │          └── NodeRegistry.get_executor("API caller")
              │                └── app/nodes/tools.py → api_caller_node()
              │
              ├── create_react_agent(llm, tools)   ← langgraph.prebuilt
              │     └── builds internal Think → Act → Observe graph
              │
              └── agent_executor.astream(messages)
                    │
                    ├── "agent" events  → LLM thinks, decides which tool to call
                    ├── "tools" events  → tool executes, result logged
                    └── final answer    → stored in FlowState.variables[output_key]
```

### 1.2 What `_build_dynamic_tool()` Does

This is the bridge between Agent Studio's node system and LangChain's tool interface.
It takes a tool definition from the UI JSON and returns a `StructuredTool` that the ReAct loop can call.

```python
# react_agent.py — _build_dynamic_tool() path decision
if node_type == "MCP Tool":
    # Direct: extracts the real MCP tool object from mcp_client_manager.server_tools
    # and wraps it with a proper Pydantic schema from the MCP server's inputSchema
    return StructuredTool(coroutine=_direct_mcp_call, args_schema=schema_model)

else:
    # Generic: calls NodeRegistry.get_executor(node_type) — the exact same function
    # that the graph compiler calls when executing that node normally in a flow
    executor = NodeRegistry.get_executor(node_type)   # e.g. api_caller_node
    result = await executor(temp_state, fake_config)
    return StructuredTool(coroutine=_execute_tool, args_schema=DynamicToolInput)
```

**Key consequence:** Every node registered in `NodeRegistry` can become a tool inside the ReAct loop — with zero extra code.

### 1.3 Shared Components

| Component | File | Used by ReAct as |
|-----------|------|-----------------|
| `_get_llm()` | `nodes/agents.py` | LLM backbone for the agent |
| `NodeRegistry.get_executor()` | `engine/registry.py` | Turns any node into a callable tool |
| `resolve_placeholders()` | `utils/templating.py` | Resolves `{{variables}}` in tool configs |
| `mcp_client_manager` | `services/mcp_client.py` | Direct MCP tool injection |
| `FlowState.variables` | `core/state.py` | Shared variable bag passed into temp_state |
| `token_callback_handler` | `core/token_tracker.py` | Tracks every LLM call inside the ReAct loop |

### 1.4 End-to-End Example

**Flow:** User asks → ReAct agent searches KB, calls an API, returns a composed answer.

**UI flow JSON (inputs to the node):**
```json
{
  "type": "react_agent",
  "name": "Autonomous ReAct Agent",
  "displayName": "IT Support Agent",
  "inputParameters": [
    { "key": "model",         "value": "gemini-2.5-pro" },
    { "key": "user_query",    "value": "{{CHAT_QUERY}}" },
    { "key": "memory_window", "value": "10" },
    { "key": "system_prompt", "value": "You are an IT support agent. Use the tools provided to answer user queries." },
    { "key": "tools", "value": [
        {
          "name":        "Search Knowledge Base",
          "description": "Search the IT knowledge base for known issues and solutions.",
          "node_type":   "Knowledge Retrieval Node",
          "config": {
            "knowledge_base_name": "it-kb",
            "user_prompt":         "{{CHAT_QUERY}}",
            "limit":               "5"
          }
        },
        {
          "name":        "Get Ticket Status",
          "description": "Fetch the status of a support ticket by ticket_id.",
          "node_type":   "API caller",
          "config": {
            "url":    "https://helpdesk.internal/api/tickets/{{ticket_id}}",
            "method": "GET",
            "headers": { "Authorization": "Bearer {{HELPDESK_TOKEN}}" }
          }
        }
      ]
    }
  ],
  "outputParameters": [
    { "key": "output", "value": "agent_response" }
  ]
}
```

**What happens at runtime:**
```
User: "My ticket INC-1042 is still open. What does the KB say about VPN issues?"

[Think]  I need to search the KB and check ticket INC-1042.
[Act]    → search_knowledge_base(user_prompt="VPN issues")
[Observe] KB returned 3 docs about VPN client configuration.
[Act]    → get_ticket_status(ticket_id="INC-1042")
[Observe] Ticket status: Open, assigned to Network Team.
[Answer] Your ticket INC-1042 is open and assigned to the Network Team.
         For VPN issues, try: 1) Restart VPN client 2) Clear cached credentials...
```

The final answer is saved to `state.variables["agent_response"]` and flows to the next node.

### 1.5 How ReAct Differs from `LLM invoker`

| Aspect | `LLM invoker` | `Autonomous ReAct Agent` |
|--------|--------------|--------------------------|
| LLM calls per turn | 1 (always) | Multiple (Think → Act → Observe loop) |
| Can call tools | No (chatty_mode routes, not calls) | Yes — any registered node |
| Memory | Sliding window from FlowState | Sliding window from FlowState |
| Best for | Structured Q&A, classification, formatting | Multi-step tasks requiring tool use |
| MCP support | No | Yes — direct injection |
| Max iterations | 1 | 50 (configurable via recursion_limit) |

---

## 2. Existing Nodes — Quick Reference

| Registered Name | File | Purpose |
|----------------|------|---------|
| `Start Node` | `basics.py` | Graph entry point (no-op) |
| `End Node` | `basics.py` | Evaluates `final_input`, writes `final_output` |
| `Text Node` | `basics.py` | Resolve a Jinja2 template to a string variable |
| `response_formatter` | `basics.py` | Format output from a template |
| `LLM invoker` | `agents.py` | Single LLM call — text, JSON, PDF, DOCX, chatty mode |
| `Question Classifier` | `classifiers.py` | Classify user input into labelled categories |
| `Decision Node` | `logic.py` | Evaluate conditions, route to matching node |
| `Iterator Node` | `logic.py` | Loop over an array one item at a time |
| `Question Node` | `hitl.py` | HITL pause — asks user a question, resumes on answer |
| `API caller` | `tools.py` | HTTP request (GET/POST/PUT/PATCH/DELETE) |
| `Send Email` | `tools.py` | Send email via SendGrid |
| `Knowledge Retrieval Node` | `tools.py` | Vector KB semantic search |
| `Mongo DB caller` | `db_caller.py` | MongoDB CRUD operations |
| `MCP Tool` | `mcp_node.py` | Call a tool on a connected MCP server |
| `Autonomous ReAct Agent` | `react_agent.py` | Multi-step agent with dynamic tool use |
| `Vocabulary Extractor` | `ontology.py` | Entity extraction with GLiNER2 |
| `Canonical Resolver` | `ontology.py` | Fuzzy-match user terms to canonical dictionary |
| `DB Chat` | `db_chat.py` | Natural language → SQL → answer |
| `Agent Flow Node` | `agent_flow.py` | Run a nested child flow (agentflow type) |

---

## 3. New Nodes to Introduce

Each node below follows the same pattern as every existing node:
- Register with `@NodeRegistry.register("Node Name")`
- Accept `(state: FlowState, node_config: dict) -> dict`
- Return `{"variables": {output_key: value}}`

---

### 3.1 Data Transform Node

**Purpose:** Extract, filter, reshape, and map JSON data without writing any code. Think of it as `jq` built into the flow.

**Why it's needed:** API responses are often deeply nested. Currently every flow needs an LLM or custom code to extract `response.data.items[0].name`. This node handles it natively.

**Implementation file:** `app/nodes/transform.py`

```python
import json, logging, re
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

@NodeRegistry.register("Data Transform")
async def data_transform_node(state: FlowState, node_config: dict) -> dict:
    inputs  = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    data    = resolve_placeholders(inputs.get("data", {}), state["variables"])
    ops     = resolve_placeholders(inputs.get("operations", []), state["variables"])

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "transformed"

    result = data
    for op in ops:
        action = op.get("action")
        if action == "extract":                          # pull a nested field
            path = op.get("path", "").split(".")
            for key in path:
                if isinstance(result, list): result = [r.get(key) for r in result if isinstance(r, dict)]
                elif isinstance(result, dict): result = result.get(key)
        elif action == "filter" and isinstance(result, list):
            field, val = op.get("field"), op.get("value")
            result = [r for r in result if str(r.get(field, "")) == str(val)]
        elif action == "map" and isinstance(result, list):
            pick = op.get("fields", [])
            result = [{f: r.get(f) for f in pick} for r in result if isinstance(r, dict)]
        elif action == "first" and isinstance(result, list):
            result = result[0] if result else None
        elif action == "count":
            result = len(result) if isinstance(result, list) else 0
        elif action == "join" and isinstance(result, list):
            result = op.get("separator", ", ").join(str(i) for i in result)

    logger.info(f"Data Transform: {len(ops)} operation(s) applied.")
    return {"variables": {output_key: result}}
```

**UI Node Schema:**
```json
{
  "type": "data_transform",
  "name": "Data Transform",
  "displayName": "Extract User Names",
  "inputParameters": [
    { "key": "data",       "value": "{{api_response}}" },
    { "key": "operations", "value": [
        { "action": "extract", "path": "data.users" },
        { "action": "filter",  "field": "active", "value": "true" },
        { "action": "map",     "fields": ["id", "name", "email"] }
      ]
    }
  ],
  "outputParameters": [
    { "key": "output", "value": "active_users" }
  ]
}
```

**Usage in a flow:**
```
API caller (GET /users) → [api_response]
  → Data Transform (extract data.users → filter active=true → map id,name,email) → [active_users]
  → LLM invoker (summarise {{active_users}}) → [final_output]
```

---

### 3.2 Parallel Executor Node

**Purpose:** Fan out to multiple sub-flows or node configs simultaneously and collect all results — like `Promise.all()` for flows.

**Why it's needed:** Today, calling three APIs means three sequential API caller nodes. With Parallel Executor, all three fire at once and results are merged when all complete.

**Implementation file:** `app/nodes/parallel.py`

```python
import asyncio, json, logging
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

@NodeRegistry.register("Parallel Executor")
async def parallel_executor_node(state: FlowState, node_config: dict) -> dict:
    inputs  = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    tasks   = resolve_placeholders(inputs.get("tasks", []), state["variables"])

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "parallel_results"

    async def _run_task(task: dict):
        node_type   = task.get("node_type")
        task_config = {
            "node_id":         task.get("name", "parallel_task"),
            "name":            node_type,
            "inputParameters": [{"key": k, "value": v} for k, v in task.get("config", {}).items()],
            "outputParameters":[{"key": "out", "value": "_tmp"}],
        }
        executor = NodeRegistry.get_executor(node_type)
        result   = await executor(state, task_config)
        return task.get("name"), result.get("variables", {}).get("_tmp")

    results_list = await asyncio.gather(*[_run_task(t) for t in tasks], return_exceptions=True)

    merged = {}
    for item in results_list:
        if isinstance(item, Exception):
            logger.warning(f"Parallel task failed: {item}")
        else:
            name, value = item
            merged[name] = value

    logger.info(f"Parallel Executor: {len(merged)}/{len(tasks)} tasks completed.")
    return {"variables": {output_key: merged}}
```

**UI Node Schema:**
```json
{
  "type": "parallel_executor",
  "name": "Parallel Executor",
  "displayName": "Fetch CRM + Inventory + Tickets",
  "inputParameters": [
    { "key": "tasks", "value": [
        {
          "name":      "crm_data",
          "node_type": "API caller",
          "config":    { "url": "https://crm.internal/contact/{{user_id}}", "method": "GET" }
        },
        {
          "name":      "inventory",
          "node_type": "API caller",
          "config":    { "url": "https://inv.internal/stock/{{product_id}}", "method": "GET" }
        },
        {
          "name":      "kb_articles",
          "node_type": "Knowledge Retrieval Node",
          "config":    { "knowledge_base_name": "products-kb", "user_prompt": "{{CHAT_QUERY}}" }
        }
      ]
    }
  ],
  "outputParameters": [
    { "key": "output", "value": "parallel_results" }
  ]
}
```

**Result in `parallel_results`:**
```json
{
  "crm_data":   { "name": "John Doe", "tier": "Gold" },
  "inventory":  { "stock": 24, "sku": "PRD-88" },
  "kb_articles": "Return policy: 30 days..."
}
```

---

### 3.3 Data Validator Node

**Purpose:** Validate a variable against a JSON Schema. Routes flow to an error path if validation fails, instead of letting bad data propagate silently.

**Why it's needed:** When an API returns an unexpected shape, the current flow either crashes mid-execution or silently passes `None` to the next node. Validation gates catch this at the source.

**Implementation file:** `app/nodes/validator.py`

```python
import json, logging
from jsonschema import validate, ValidationError as JVE
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

@NodeRegistry.register("Data Validator")
async def data_validator_node(state: FlowState, node_config: dict) -> dict:
    inputs     = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    data       = resolve_placeholders(inputs.get("data"), state["variables"])
    schema     = resolve_placeholders(inputs.get("schema", {}), state["variables"])

    out_params = node_config.get("outputParameters", [])
    status_key = out_params[0]["value"] if out_params else "validation_status"
    error_key  = out_params[1]["value"] if len(out_params) > 1 else "validation_error"

    if isinstance(schema, str):
        try: schema = json.loads(schema)
        except: pass

    try:
        validate(instance=data, schema=schema)
        logger.info("Data Validator: PASSED")
        return {"variables": {status_key: "valid", error_key: None}}
    except JVE as e:
        logger.warning(f"Data Validator: FAILED — {e.message}")
        return {"variables": {status_key: "invalid", error_key: e.message}}
```

**UI Node Schema:**
```json
{
  "type": "data_validator",
  "name": "Data Validator",
  "displayName": "Validate Order Payload",
  "inputParameters": [
    { "key": "data", "value": "{{order_payload}}" },
    { "key": "schema", "value": {
        "type": "object",
        "required": ["order_id", "customer_id", "items"],
        "properties": {
          "order_id":    { "type": "string" },
          "customer_id": { "type": "string" },
          "items":       { "type": "array",  "minItems": 1 }
        }
      }
    }
  ],
  "outputParameters": [
    { "key": "status", "value": "validation_status" },
    { "key": "error",  "value": "validation_error"  }
  ]
}
```

**Pair with a Decision Node:**
```
Validator → [validation_status]
  → Decision Node
        "valid"   → Process Order Node
        "invalid" → Send Email (validation error notification)
```

---

### 3.4 Multi-Channel Notification Node

**Purpose:** Send a notification through Slack, Microsoft Teams, WhatsApp, or SMS from a single node — no more separate `Send Email` nodes for every channel.

**Implementation file:** `app/nodes/notification.py`

```python
import httpx, logging, json
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

@NodeRegistry.register("Notification Node")
async def notification_node(state: FlowState, node_config: dict) -> dict:
    inputs  = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    channel = resolve_placeholders(inputs.get("channel", "slack"), state["variables"]).lower()
    message = resolve_placeholders(inputs.get("message", ""), state["variables"])
    target  = resolve_placeholders(inputs.get("target", ""), state["variables"])   # webhook URL / phone / channel ID

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "notification_status"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if channel == "slack":
                resp = await client.post(target, json={"text": message})
            elif channel == "teams":
                resp = await client.post(target, json={"text": message})
            elif channel == "whatsapp":
                # Twilio WhatsApp endpoint
                resp = await client.post(target, data={"Body": message, "To": f"whatsapp:{target}"})
            else:
                return {"variables": {output_key: f"Unsupported channel: {channel}"}}

            resp.raise_for_status()
            logger.info(f"Notification sent via {channel}")
            return {"variables": {output_key: "sent"}}

    except Exception as e:
        logger.error(f"Notification failed ({channel}): {e}")
        return {"variables": {output_key: f"failed: {str(e)}"}}
```

**UI Node Schema:**
```json
{
  "type": "notification",
  "name": "Notification Node",
  "displayName": "Alert on Slack",
  "inputParameters": [
    { "key": "channel", "value": "slack" },
    { "key": "target",  "value": "{{SLACK_WEBHOOK_URL}}" },
    { "key": "message", "value": "🚨 Order {{order_id}} failed validation: {{validation_error}}" }
  ],
  "outputParameters": [
    { "key": "output", "value": "notification_status" }
  ]
}
```

---

### 3.5 LLM Judge Node

**Purpose:** Have an LLM evaluate the quality, accuracy, or safety of another node's output. Returns a score + reasoning. Ideal for self-checking flows.

**Why it's needed:** When an LLM invoker produces an answer, there is currently no automated way to verify quality before sending it to the user. The judge creates a feedback loop.

**Implementation file:** `app/nodes/judge.py`

```python
import logging
from pydantic import BaseModel, Field
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState
from app.nodes.agents import _get_llm

logger = logging.getLogger(__name__)

class JudgeVerdict(BaseModel):
    score:     float = Field(description="Quality score from 0.0 (worst) to 1.0 (best).")
    verdict:   str   = Field(description="One of: 'pass', 'fail', 'retry'.")
    reasoning: str   = Field(description="Brief explanation of the score.")

@NodeRegistry.register("LLM Judge")
async def llm_judge_node(state: FlowState, node_config: dict) -> dict:
    inputs     = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    content    = resolve_placeholders(inputs.get("content", ""), state["variables"])
    criteria   = resolve_placeholders(inputs.get("criteria", "Is this response helpful, accurate, and safe?"), state["variables"])
    model      = inputs.get("model", "gemini-2.5-flash")
    threshold  = float(inputs.get("pass_threshold", 0.7))

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "judge_verdict"

    llm = _get_llm(model)
    structured_llm = llm.with_structured_output(JudgeVerdict)

    system = f"You are an impartial quality evaluator.\nCriteria: {criteria}"
    from langchain_core.prompts import ChatPromptTemplate
    prompt = ChatPromptTemplate.from_messages([
        ("system", system),
        ("human", "Evaluate this content:\n\n{content}")
    ])
    chain   = prompt | structured_llm
    verdict: JudgeVerdict = await chain.ainvoke({"content": str(content)})

    if verdict.score < threshold:
        verdict.verdict = "fail"

    logger.info(f"LLM Judge: score={verdict.score:.2f} verdict={verdict.verdict}")
    return {"variables": {output_key: {
        "score":     verdict.score,
        "verdict":   verdict.verdict,
        "reasoning": verdict.reasoning
    }}}
```

**UI Node Schema:**
```json
{
  "type": "llm_judge",
  "name": "LLM Judge",
  "displayName": "Evaluate Agent Response",
  "inputParameters": [
    { "key": "content",        "value": "{{agent_response}}" },
    { "key": "criteria",       "value": "Is the answer factually grounded, professional, and free from harmful content?" },
    { "key": "model",          "value": "gemini-2.5-flash" },
    { "key": "pass_threshold", "value": "0.75" }
  ],
  "outputParameters": [
    { "key": "output", "value": "judge_verdict" }
  ]
}
```

**Self-healing flow pattern:**
```
LLM invoker → [agent_response]
  → LLM Judge → [judge_verdict]
  → Decision Node
        judge_verdict.verdict == "pass"  → End Node (send to user)
        judge_verdict.verdict == "fail"  → LLM invoker (retry with judge reasoning as context)
        judge_verdict.verdict == "retry" → Question Node (ask user to clarify)
```

---

### 3.6 Redis Cache Node

**Purpose:** Get or set values in Redis with an optional TTL. Enables flows to avoid redundant API calls and share data across concurrent threads.

**Implementation file:** `app/nodes/cache_node.py`

```python
import json, logging
import redis.asyncio as aioredis
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)
_redis_pool = None

def get_redis():
    global _redis_pool
    if _redis_pool is None:
        from app.core.config import settings
        _redis_pool = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_pool

@NodeRegistry.register("Redis Cache")
async def redis_cache_node(state: FlowState, node_config: dict) -> dict:
    inputs    = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    operation = resolve_placeholders(inputs.get("operation", "get"), state["variables"]).lower()
    key       = resolve_placeholders(inputs.get("key", ""), state["variables"])
    value     = resolve_placeholders(inputs.get("value", ""), state["variables"])
    ttl       = int(inputs.get("ttl", 300))

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "cache_result"

    r = get_redis()
    try:
        if operation == "set":
            payload = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            await r.setex(key, ttl, payload)
            return {"variables": {output_key: "ok"}}

        elif operation == "get":
            raw = await r.get(key)
            if raw is None:
                return {"variables": {output_key: None}}
            try: return {"variables": {output_key: json.loads(raw)}}
            except: return {"variables": {output_key: raw}}

        elif operation == "delete":
            await r.delete(key)
            return {"variables": {output_key: "deleted"}}

    except Exception as e:
        logger.error(f"Redis Cache error ({operation}): {e}")
        return {"variables": {output_key: None}}
```

**UI Node Schema — Cache miss / hit pattern:**
```json
{
  "type": "redis_cache",
  "name": "Redis Cache",
  "displayName": "Check Price Cache",
  "inputParameters": [
    { "key": "operation", "value": "get" },
    { "key": "key",       "value": "price_{{product_id}}" },
    { "key": "ttl",       "value": "600" }
  ],
  "outputParameters": [
    { "key": "output", "value": "cached_price" }
  ]
}
```

**Cache-aside flow pattern:**
```
Redis Cache (get "price_{{product_id}}") → [cached_price]
  → Decision Node
        cached_price is_not_empty → End Node (return cached_price)
        cached_price is_empty     → API caller (fetch live price)
                                      → Redis Cache (set "price_{{product_id}}", ttl=600)
                                      → End Node
```

---

### 3.7 File Parser Node

**Purpose:** Parse uploaded files (PDF, DOCX, CSV, Excel) and extract plain text or structured data into a flow variable.

**Why it's needed:** Users upload documents through the chat interface (`userInput.uploadedFiles`). Currently there is no node that can read and extract content from those files.

**Implementation file:** `app/nodes/file_parser.py`

```python
import base64, io, json, logging
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

@NodeRegistry.register("File Parser")
async def file_parser_node(state: FlowState, node_config: dict) -> dict:
    import asyncio
    inputs    = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    file_b64  = resolve_placeholders(inputs.get("file_base64", ""), state["variables"])
    file_type = resolve_placeholders(inputs.get("file_type", "pdf"), state["variables"]).lower()
    max_chars = int(inputs.get("max_chars", 10000))

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "parsed_text"

    def _extract():
        raw = base64.b64decode(file_b64)
        buf = io.BytesIO(raw)

        if file_type == "pdf":
            import pdfplumber
            with pdfplumber.open(buf) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)

        elif file_type in ("doc", "docx"):
            from docx import Document
            doc = Document(buf)
            return "\n".join(p.text for p in doc.paragraphs)

        elif file_type == "csv":
            import csv, io as _io
            reader = csv.DictReader(_io.StringIO(raw.decode("utf-8")))
            return json.dumps(list(reader))

        elif file_type in ("xls", "xlsx"):
            import openpyxl
            wb = openpyxl.load_workbook(buf, data_only=True)
            rows = []
            for row in wb.active.iter_rows(values_only=True):
                rows.append(list(row))
            return json.dumps(rows)

        return ""

    try:
        text = await asyncio.to_thread(_extract)
        return {"variables": {output_key: text[:max_chars]}}
    except Exception as e:
        logger.error(f"File Parser error: {e}")
        return {"variables": {output_key: f"Parse error: {str(e)}"}}
```

**UI Node Schema:**
```json
{
  "type": "file_parser",
  "name": "File Parser",
  "displayName": "Extract Contract Text",
  "inputParameters": [
    { "key": "file_base64", "value": "{{uploadedFiles.0.content}}" },
    { "key": "file_type",   "value": "pdf" },
    { "key": "max_chars",   "value": "15000" }
  ],
  "outputParameters": [
    { "key": "output", "value": "contract_text" }
  ]
}
```

**Document Q&A flow:**
```
Start → File Parser (pdf) → [contract_text]
      → LLM invoker
          system: "Answer questions about this contract: {{contract_text}}"
          user:   "{{CHAT_QUERY}}"
        → [final_output] → End
```

---

### 3.8 Batch Processor Node

**Purpose:** Process an array in parallel chunks (fan-out), unlike `Iterator Node` which is strictly sequential.

**Why it's needed:** Processing 100 records one-by-one through an Iterator + API caller takes 100× the latency of a single call. Batch Processor fires `n` concurrent calls.

**Implementation file:** `app/nodes/batch.py`

```python
import asyncio, json, logging
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

@NodeRegistry.register("Batch Processor")
async def batch_processor_node(state: FlowState, node_config: dict) -> dict:
    inputs       = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    array_data   = resolve_placeholders(inputs.get("array", []), state["variables"])
    node_type    = inputs.get("node_type", "API caller")
    item_key     = inputs.get("item_variable", "item")
    base_config  = inputs.get("config", {})
    batch_size   = int(inputs.get("batch_size", 5))

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "batch_results"

    if isinstance(array_data, str):
        try: array_data = json.loads(array_data)
        except: array_data = []

    executor = NodeRegistry.get_executor(node_type)

    async def _process_item(item):
        temp_state = {**state, "variables": {**state.get("variables", {}), item_key: item}}
        fake_config = {
            "node_id": "batch_item",
            "name":    node_type,
            "inputParameters":  [{"key": k, "value": v} for k, v in base_config.items()],
            "outputParameters": [{"key": "out", "value": "_batch_tmp"}],
        }
        result = await executor(temp_state, fake_config)
        return result.get("variables", {}).get("_batch_tmp")

    results = []
    for i in range(0, len(array_data), batch_size):
        chunk = array_data[i : i + batch_size]
        chunk_results = await asyncio.gather(*[_process_item(x) for x in chunk], return_exceptions=True)
        results.extend([r if not isinstance(r, Exception) else {"error": str(r)} for r in chunk_results])
        logger.info(f"Batch Processor: {min(i+batch_size, len(array_data))}/{len(array_data)} done")

    return {"variables": {output_key: results}}
```

**UI Node Schema:**
```json
{
  "type": "batch_processor",
  "name": "Batch Processor",
  "displayName": "Enrich All Leads",
  "inputParameters": [
    { "key": "array",         "value": "{{lead_ids}}" },
    { "key": "item_variable", "value": "lead_id" },
    { "key": "node_type",     "value": "API caller" },
    { "key": "batch_size",    "value": "10" },
    { "key": "config",        "value": {
        "url":    "https://crm.internal/api/lead/{{lead_id}}",
        "method": "GET"
      }
    }
  ],
  "outputParameters": [
    { "key": "output", "value": "enriched_leads" }
  ]
}
```

---

### 3.9 Webhook Emitter Node

**Purpose:** Send an outbound HTTP webhook with HMAC signature verification — fire-and-forget, does not block the flow on the recipient's response time.

**Implementation file:** `app/nodes/webhook.py`

```python
import hashlib, hmac, json, logging, asyncio
import httpx
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

@NodeRegistry.register("Webhook Emitter")
async def webhook_emitter_node(state: FlowState, node_config: dict) -> dict:
    inputs  = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    url     = resolve_placeholders(inputs.get("url", ""), state["variables"])
    payload = resolve_placeholders(inputs.get("payload", {}), state["variables"])
    secret  = resolve_placeholders(inputs.get("secret", ""), state["variables"])
    wait    = str(inputs.get("wait_for_response", "false")).lower() == "true"

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "webhook_status"

    body = json.dumps(payload, default=str).encode()
    headers = {"Content-Type": "application/json"}

    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Signature-256"] = f"sha256={sig}"

    async def _fire():
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(url, content=body, headers=headers)

    if wait:
        try:
            resp = await _fire()
            resp.raise_for_status()
            return {"variables": {output_key: "sent"}}
        except Exception as e:
            return {"variables": {output_key: f"failed: {e}"}}
    else:
        asyncio.create_task(_fire())   # fire-and-forget
        return {"variables": {output_key: "queued"}}
```

**UI Node Schema:**
```json
{
  "type": "webhook_emitter",
  "name": "Webhook Emitter",
  "displayName": "Notify Order System",
  "inputParameters": [
    { "key": "url",                "value": "{{ORDER_WEBHOOK_URL}}" },
    { "key": "secret",             "value": "{{WEBHOOK_SECRET}}" },
    { "key": "wait_for_response",  "value": "false" },
    { "key": "payload",            "value": {
        "event":    "order.created",
        "order_id": "{{order_id}}",
        "customer": "{{customer_name}}"
      }
    }
  ],
  "outputParameters": [
    { "key": "output", "value": "webhook_status" }
  ]
}
```

---

### 3.10 Variable Merger Node

**Purpose:** Combine multiple variables into one structured object or flatten a nested object into top-level keys — replacing multi-node Text Node workarounds.

**Implementation file:** `app/nodes/merger.py`

```python
import logging
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

@NodeRegistry.register("Variable Merger")
async def variable_merger_node(state: FlowState, node_config: dict) -> dict:
    inputs     = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    mode       = inputs.get("mode", "combine")   # combine | flatten | pick
    mapping    = resolve_placeholders(inputs.get("mapping", {}), state["variables"])

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "merged"

    if mode == "combine":
        # mapping is {output_field: "{{source_variable}}"}
        result = {k: resolve_placeholders(v, state["variables"]) for k, v in mapping.items()}

    elif mode == "flatten":
        # flatten a nested dict into top-level variables
        source = resolve_placeholders(inputs.get("source", {}), state["variables"])
        result = {}
        def _flat(obj, prefix=""):
            for k, v in (obj.items() if isinstance(obj, dict) else []):
                full_key = f"{prefix}{k}" if not prefix else f"{prefix}_{k}"
                if isinstance(v, dict): _flat(v, full_key)
                else: result[full_key] = v
        _flat(source)

    elif mode == "pick":
        keys   = inputs.get("keys", [])
        result = {k: state["variables"].get(k) for k in keys}

    else:
        result = {}

    return {"variables": {output_key: result}}
```

**UI Node Schema (combine mode):**
```json
{
  "type": "variable_merger",
  "name": "Variable Merger",
  "displayName": "Build Ticket Payload",
  "inputParameters": [
    { "key": "mode", "value": "combine" },
    { "key": "mapping", "value": {
        "customer_name":  "{{crm_data.name}}",
        "customer_email": "{{crm_data.email}}",
        "issue_summary":  "{{agent_response}}",
        "ticket_priority":"{{priority_decision}}"
      }
    }
  ],
  "outputParameters": [
    { "key": "output", "value": "ticket_payload" }
  ]
}
```

---

## Summary — Node Decision Guide

```
Need to call an API once?                        → API caller
Need to call 3 APIs at the same time?            → Parallel Executor
Need to loop over a list one by one?             → Iterator Node
Need to process a list in parallel chunks?       → Batch Processor
Need an LLM to answer a question?               → LLM invoker
Need an LLM that can call tools itself?          → Autonomous ReAct Agent
Need to classify user input into categories?     → Question Classifier
Need to route based on a variable's value?       → Decision Node
Need to ask the user a question mid-flow?        → Question Node
Need to validate a data structure?               → Data Validator
Need to extract a field from nested JSON?        → Data Transform
Need to merge variables into one object?         → Variable Merger
Need to read/write KB articles?                  → Knowledge Retrieval Node
Need to query a SQL database in plain English?   → DB Chat
Need to query MongoDB directly?                  → Mongo DB caller
Need to send a notification?                     → Notification Node (Slack/Teams/WhatsApp)
Need to evaluate quality of an LLM output?       → LLM Judge
Need to cache an API result across threads?      → Redis Cache
Need to parse an uploaded PDF/DOCX/CSV?          → File Parser
Need to trigger an external system (no wait)?    → Webhook Emitter
Need to extract entities from text?              → Vocabulary Extractor
Need to normalize user terms to canonical ones?  → Canonical Resolver
Need to call an external service via MCP?        → MCP Tool / Autonomous ReAct Agent
Need to run a child flow as a step?              → Agent Flow Node
```
