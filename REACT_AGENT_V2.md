# ReAct Agent v2 — Complete Guide (in plain English)

> Node name in the UI: **`Autonomous ReAct Agent v2`**
> Code: `app/nodes/react_agent_v2.py`
> The old node (`Autonomous ReAct Agent`, `app/nodes/react_agent.py`) is **untouched**, so existing flows keep working.

---

## Table of Contents

1. [What is a ReAct agent? (layman explanation)](#1-what-is-a-react-agent-layman-explanation)
2. [What can this node actually DO? (capability list)](#2-what-can-this-node-actually-do-capability-list)
3. [Why v1 misbehaved — the 3 real bugs](#3-why-v1-misbehaved--the-3-real-bugs)
4. [What v2 changes (side-by-side)](#4-what-v2-changes-side-by-side)
5. [The complete working FLOW (step by step)](#5-the-complete-working-flow-step-by-step)
6. [Full node JSON reference](#6-full-node-json-reference)
7. [Worked examples with real JSON + real conversation](#7-worked-examples-with-real-json--real-conversation)
8. [How memory works now (the "it forgot my name" fix)](#8-how-memory-works-now-the-it-forgot-my-name-fix)
9. [How tools are called correctly now](#9-how-tools-are-called-correctly-now)
10. [Scenario matrix — what to configure for what](#10-scenario-matrix--what-to-configure-for-what)
11. [Migration from v1 + troubleshooting](#11-migration-from-v1--troubleshooting)

---

## 1. What is a ReAct agent? (layman explanation)

**ReAct = REAsoning + ACTing.**

A normal `LLM invoker` node is like asking a knowledgeable friend a question: they answer **once**, from memory only. If they need to look something up, they can't — they just guess or say "I don't know".

A **ReAct agent** is like giving that friend a phone, a laptop and your company's systems, and telling them: *"Think about the question, use whatever tool you need, look at the result, and only then answer me."*

Internally it repeats a tiny loop:

```
Thought      → "To answer this I need the ticket status."
Action       → get_ticket_status(ticket_id="INC-1042")
Observation  → {"status": "Open", "team": "Network"}
Thought      → "Now I also need the KB article."
Action       → search_knowledge_base(query="VPN not connecting")
Observation  → 3 documents found
Thought      → "I have everything."
Final Answer → "Your ticket INC-1042 is open with the Network team. For the VPN issue, try…"
```

That loop is the entire idea. Everything else in this document is about making the loop **reliable**: pick the right tool, pass the right arguments, remember the conversation, and never return an empty answer.

**Analogy for the two failures you saw:**

| What happened | Real-world analogy |
|---|---|
| "It said I don't know" after tools existed | You gave your assistant a form to fill, but the form had **no field labels**. They submitted it blank, got nothing back, and told you "sorry, no info". |
| "I told my name in the first question and it didn't use it" | Your assistant took notes on a whiteboard, but **someone wiped the whiteboard between messages** (a new thread), and they never checked the notebook (MongoDB) where the notes were also saved. |

v2 fixes both: the form now has labelled, typed fields, and the assistant checks the notebook when the whiteboard is empty.

---

## 2. What can this node actually DO? (capability list)

| # | Capability | Plain meaning | How to switch it on |
|---|---|---|---|
| 1 | **Multi-step tool use** | Calls several tools, in any order it decides, up to `max_iterations` | `tools: [...]` |
| 2 | **Uses ANY registered node as a tool** | `API caller`, `Knowledge Retrieval Node`, `Web Search`, `Send Email`, `Mongo DB caller`, `Chat with DB`, `Question Classifier`, `Vocabulary Extractor`, `Cypher Query Builder`, `Agent Flow Node`… anything in `NodeRegistry` | `"node_type": "<registered node name>"` |
| 3 | **MCP tools (direct injection)** | Real tools from external MCP servers, with their real remote schema | `"node_type": "MCP Tool"` |
| 4 | **Typed arguments** | The model sees named, described, typed fields — not a blob of JSON | `parameters: [...]` or auto-inferred |
| 5 | **Auto schema inference** | Any `{{placeholder}}` left in a tool config that the flow can't fill becomes a tool argument automatically | nothing to do — it's default |
| 6 | **Secrets stay safe** | Placeholders that *do* exist in flow state (tokens, base URLs) are filled by the engine, never asked from the LLM | automatic |
| 7 | **Conversation memory** | Remembers earlier turns of the same session | `memory_window`, `memory_mode` |
| 8 | **DB memory fallback** | If the LangGraph checkpoint is empty/new, it reloads history from MongoDB `conversation_memory` by `session_id` | `memory_mode: "auto"` (default) |
| 9 | **Sticky facts** | Values like `user_name`, `customer_id`, `remembered_*` are injected every turn, so they survive window trimming | store them in flow variables |
| 10 | **History sanitisation** | Removes orphan tool messages / dangling tool-calls that make Gemini & OpenAI return 400 | automatic |
| 11 | **Error recovery on tools** | A failing tool returns `TOOL_ERROR: …` *to the model* so it can retry with better arguments instead of the whole flow dying | automatic |
| 12 | **Never-empty answer** | If the loop ends without text, it runs one recovery completion, else returns `fallback_answer` | `fallback_answer` |
| 13 | **Structured trace** | Machine-readable list of every Thought → Action → Observation, for debugging / audit UI | `return_trace: "true"` |
| 14 | **Tool-call log variable** | `{{<output>_tool_calls}}` — what it called and with what arguments | automatic |
| 15 | **Output formatting** | `text`, `html` (for chat bubbles) or `json` (for the next node to parse) | `response_format` |
| 16 | **Model agnostic** | Gemini / Vertex AI and Llama / OpenAI-compatible endpoints, via the shared `_get_llm()` | `model` |
| 17 | **Token & cost tracking** | Every internal LLM call is counted by the existing `token_callback_handler` | automatic |
| 18 | **Safe loop budget** | Hard cap on reasoning steps so a confused model can't burn tokens forever | `max_iterations` |
| 19 | **Works with zero tools** | Becomes a memory-aware chat node | `tools: []` |
| 20 | **LangGraph-version agnostic** | Detects `prompt` / `state_modifier` / `messages_modifier` automatically | automatic |

**What it deliberately does NOT do:** it does not pause and ask the user mid-loop (use a `Question Node` before/after it), and it does not write to your DB unless you give it a tool that does.

---

## 3. Why v1 misbehaved — the 3 real bugs

### Bug 1 — The tool form had no labels (this is why it said "I don't know")

v1 gave **every non-MCP tool the same single argument**:

```python
class DynamicToolInput(BaseModel):
    parameters_json: str = Field(default="{}",
        description="A valid JSON string containing variables needed for this tool.")
```

The model was told *"send me a JSON string"* but never told **which keys**. So it sent `{}`.
Then the tool ran the node with the raw config, `{{ticket_id}}` was never filled, the API was called as
`https://helpdesk.internal/api/tickets/{{ticket_id}}` → 404 → the agent concluded *"I don't know"*.

### Bug 2 — Memory could silently be empty

v1 read history from exactly one place:

```python
all_messages = state.get("messages", [])
```

That is the LangGraph checkpoint, keyed by **`thread_id`**. Meanwhile the API layer (`app/core/helpers.py → save_conversation_turn`) saves the same conversation to MongoDB `conversation_memory`, keyed by **`session_id`** — and **nothing ever read it back**. So if the UI sent a new `thread_id` per message (or `clear_stale.py` was run, or the flow schema changed and the checkpoint was reset), turn 2 started with **zero** history → *"I don't know your name."*

Two more memory papercuts in v1:
* `all_messages[-(memory_window * 2):]` slices by **message count**, so it can cut a conversation in half mid-turn and can leave a `ToolMessage` without its parent tool call → provider 400.
* The current question was appended even if it was already the last message in state → the model sometimes answered the *previous* question.

### Bug 3 — Silent dead ends

If the loop ended on a tool step, or the model returned content blocks instead of a string, `final_answer` stayed empty and the user got the literal text **"Task complete."**. Any exception became `"Error: <stacktrace-ish>"` shown to the end user.

---

## 4. What v2 changes (side-by-side)

| Area | v1 | v2 |
|---|---|---|
| Tool arguments | 1 opaque `parameters_json` string | Real typed schema per tool (declared **or** auto-inferred) |
| Argument discovery | none | `{{placeholders}}` in config that state can't fill become required args |
| Secrets | model could be asked for them | placeholders resolvable from state are filled by the engine |
| Unknown `node_type` | crashes inside the loop | tool skipped + warning, agent keeps working |
| Tool failure | `"Tool execution failed: …"` string, no guidance | `TOOL_ERROR: … fix the arguments and retry once` — model recovers |
| Tool output size | unbounded (context blow-ups) | truncated at 4 000 chars |
| Memory source | checkpoint only | checkpoint **+ MongoDB fallback** (`memory_mode: auto`) |
| Memory trimming | last *N×2 messages* | last *N human turns*, tool-call integrity preserved |
| Duplicate question | possible | removed |
| Orphan tool messages | possible → 400 errors | sanitised |
| Sticky facts | none | `user_*`, `customer_*`, `remembered_*`, `profile_*` injected each turn |
| Empty query | Gemini 400 | falls back to last human message |
| Empty answer | `"Task complete."` | recovery completion → `fallback_answer` |
| Behaviour rules | free-form system prompt | your prompt **+ 6 hard operating rules** |
| Debuggability | logs only | logs + `_trace` + `_tool_calls` output variables |
| Loop budget | fixed `recursion_limit: 50` | `max_iterations` (default 8) |

---

## 5. The complete working FLOW (step by step)

### 5.1 Where the node sits in the engine

```
POST /engine/agents/invoke/{agent_id}          app/api/agents.py
   │  variables["CHAT_QUERY"] = user message
   ▼
GraphCompiler.build()                          app/engine/compiler.py
   │  (MongoDBSaver checkpointer, thread_id)
   ▼
react_agent_v2_node(state, node_config)        app/nodes/react_agent_v2.py
   │
   ├─ 1. read + resolve inputParameters        utils/templating.resolve_placeholders
   ├─ 2. build tools  ──────────────► _build_tool()
   │        ├─ MCP Tool  → real remote JSON-Schema → StructuredTool
   │        └─ any node  → declared/inferred schema → NodeRegistry.get_executor()
   ├─ 3. build memory ──────────────► _build_memory()
   │        ├─ state["messages"]  (LangGraph checkpoint)
   │        ├─ MongoDB conversation_memory   (fallback, by session_id)
   │        ├─ _sanitize()  → drop orphan/empty messages
   │        └─ _trim_to_turns() → last N human turns
   ├─ 4. build prompt = system_prompt + BASE_RULES + KNOWN FACTS
   ├─ 5. create_react_agent(llm, tools, prompt)      langgraph.prebuilt
   ├─ 6. astream(...) → Thought / Action / Observation loop
   ├─ 7. recovery completion if no final text
   └─ 8. return {variables: {...}, messages: [Human, AI]}
                                     │
                                     ▼
                    checkpoint saved (thread) + conversation_memory saved (session)
```

### 5.2 The runtime loop in words

1. **Resolve inputs** — `{{CHAT_QUERY}}`, `{{customer_id}}` etc. are replaced with live flow values.
2. **Build the toolbox** — each entry in `tools` becomes a callable with a *typed, described* signature.
3. **Load memory** — checkpoint history, or MongoDB history if the checkpoint is new; cleaned and trimmed.
4. **Compose the prompt** — your instructions + built-in operating rules + known facts block.
5. **Think** — the model either answers directly or emits a tool call with arguments.
6. **Act** — v2 executes the underlying node (or MCP tool) with those arguments merged into a *copy* of flow state.
7. **Observe** — result is compacted to text and fed back. Errors come back as `TOOL_ERROR: …`.
8. **Repeat** 5–7 until the model produces a final message or `max_iterations` is reached.
9. **Guarantee an answer** — recovery completion, else `fallback_answer`.
10. **Write memory back** — the turn is appended to state `messages` (checkpoint), and the API layer persists it to MongoDB.

---

## 6. Full node JSON reference

### 6.1 `inputParameters`

| Key | Type | Default | What it does |
|---|---|---|---|
| `model` | string | `gemini-2.5-pro` | Any model supported by `_get_llm()` (Gemini or Llama/OpenAI-compatible) |
| `user_query` | string | `{{CHAT_QUERY}}` | The question for this turn |
| `system_prompt` | string | *"You are a helpful autonomous agent."* | Your persona/policy. Built-in rules are appended automatically |
| `tools` | array | `[]` | Tool definitions (see 6.2) |
| `memory_window` | int | `10` | How many **previous human turns** to keep |
| `memory_mode` | `auto`\|`state`\|`db`\|`off` | `auto` | Where history comes from (see §8) |
| `memory_context` | string | `""` | Extra facts to pin into every turn, e.g. `Plan: {{plan_name}}` |
| `max_iterations` | int | `8` | Max Think→Act cycles |
| `temperature` | float | `0` | Creativity |
| `response_format` | `text`\|`html`\|`json` | `text` | Output shape hint |
| `return_trace` | bool | `false` | Also emit the structured reasoning trace |
| `fallback_answer` | string | *"I could not complete that request…"* | Shown if everything fails |

### 6.2 A tool definition

```json
{
  "name": "Get Ticket Status",
  "description": "Fetch the live status of a helpdesk ticket by its id.",
  "node_type": "API caller",
  "config": {
    "url": "https://helpdesk.internal/api/tickets/{{ticket_id}}",
    "method": "GET",
    "headers": { "Authorization": "Bearer {{HELPDESK_TOKEN}}" }
  },
  "parameters": [
    { "name": "ticket_id", "type": "string", "required": true,
      "description": "Ticket id like INC-1042", "example": "INC-1042" }
  ]
}
```

* `node_type` — any name registered in `NodeRegistry` (or `"MCP Tool"`).
* `config` — exactly what that node expects as `inputParameters`.
* `parameters` — **optional**. If omitted, v2 infers arguments from unresolvable `{{placeholders}}` in `config`.
  Here `{{ticket_id}}` → argument for the LLM; `{{HELPDESK_TOKEN}}` exists in flow state → filled silently by the engine.
* Supported `type`: `string`, `integer`, `number`, `boolean`, `array`, `object`. Extras: `required`, `enum`, `example`.

### 6.3 `outputParameters`

```json
"outputParameters": [ { "key": "output", "value": "agent_response" } ]
```

Produces:

| Variable | Content |
|---|---|
| `{{agent_response}}` | The final answer text |
| `{{agent_response_tool_calls}}` | `[{"tool":"get_ticket_status","args":{"ticket_id":"INC-1042"}}]` |
| `{{agent_response_trace}}` | Full observations (only when `return_trace: "true"`) |

---

## 7. Worked examples with real JSON + real conversation

### Example A — IT Support agent (KB + API + memory) ✅ the main scenario

```json
{
  "node_id": "node-2001",
  "type": "react_agent_v2",
  "name": "Autonomous ReAct Agent v2",
  "displayName": "IT Support Agent",
  "inputParameters": [
    { "key": "model",          "value": "gemini-2.5-pro" },
    { "key": "user_query",     "value": "{{CHAT_QUERY}}" },
    { "key": "memory_window",  "value": "10" },
    { "key": "memory_mode",    "value": "auto" },
    { "key": "max_iterations", "value": "8" },
    { "key": "response_format","value": "html" },
    { "key": "return_trace",   "value": "true" },
    { "key": "system_prompt",
      "value": "You are Sify's IT support agent. Be concise and friendly. Always greet the user by name if you know it. Use the knowledge base for how-to questions and the ticket API for ticket status." },
    { "key": "tools", "value": [
      {
        "name": "Search Knowledge Base",
        "description": "Search the IT knowledge base for known issues, how-tos and solutions.",
        "node_type": "Knowledge Retrieval Node",
        "config": {
          "knowledge_base_name": "it-kb",
          "user_prompt": "{{search_query}}",
          "limit": "5",
          "max_distance": "0.7"
        },
        "parameters": [
          { "name": "search_query", "type": "string", "required": true,
            "description": "What to look for, e.g. 'VPN not connecting on Windows'" }
        ]
      },
      {
        "name": "Get Ticket Status",
        "description": "Fetch the live status of a helpdesk ticket by its id.",
        "node_type": "API caller",
        "config": {
          "url": "https://helpdesk.internal/api/tickets/{{ticket_id}}",
          "method": "GET",
          "headers": { "Authorization": "Bearer {{HELPDESK_TOKEN}}" }
        },
        "parameters": [
          { "name": "ticket_id", "type": "string", "required": true,
            "description": "Ticket id such as INC-1042" }
        ]
      }
    ]}
  ],
  "outputParameters": [ { "key": "output", "value": "agent_response" } ]
}
```

**Turn 1 — the exact case that failed before**

Request:

```json
{
  "session_id": "sess-77", "thread_id": "thread-77", "user_id": "u-9",
  "userInput": { "message": "Hi, my name is Prithvi. My VPN keeps disconnecting on Windows — what should I do?" }
}
```

Internal loop:

```
[Memory]      0 previous turns (new session)
[Thought]     Greeting + a how-to question. I need the knowledge base.
[Action]      search_knowledge_base(search_query="VPN keeps disconnecting Windows")
[Observation] {"context": "1) Update the VPN client 2) Disable IPv6 3) Switch to TCP mode ..."}
[Thought]     I have enough to answer.
[Answer]      "Hi Prithvi! Here are three fixes for the VPN drops ..."
```

Response (trimmed):

```json
{
  "agent_response": "Hi <b>Prithvi</b>! Try these in order:<ul><li>Update the VPN client…</li><li>Disable IPv6…</li><li>Switch the tunnel to TCP…</li></ul>",
  "status": "COMPLETED", "session_id": "sess-77", "thread_id": "thread-77"
}
```

Variables produced:

```json
{
  "agent_response_tool_calls": [
    { "tool": "search_knowledge_base", "args": { "search_query": "VPN keeps disconnecting Windows" } }
  ],
  "agent_response_trace": [
    { "tool": "search_knowledge_base",
      "args": { "search_query": "VPN keeps disconnecting Windows" },
      "observation": "{\"context\": \"1) Update the VPN client 2) Disable IPv6 ...\"}" }
  ]
}
```

**Turn 2 — "What was my name again, and check INC-1042"**

```
[Memory]      2 messages restored (checkpoint; MongoDB used automatically if the thread is new)
[Thought]     History says the user is Prithvi. I still need the ticket status.
[Action]      get_ticket_status(ticket_id="INC-1042")
[Observation] {"status":"Open","assigned_to":"Network Team"}
[Answer]      "You're Prithvi 🙂 — ticket INC-1042 is Open with the Network Team."
```

**In v1** turn 2 answered *"I don't know your name"* whenever the thread was new, and `get_ticket_status` was called with `{}` because the model never saw a `ticket_id` field.

---

### Example B — Zero-config tools (auto-inferred arguments)

No `parameters` block at all — v2 reads the placeholders:

```json
{ "key": "tools", "value": [
  {
    "name": "Create Ticket",
    "description": "Create a helpdesk ticket for the user.",
    "node_type": "API caller",
    "config": {
      "url": "https://helpdesk.internal/api/tickets",
      "method": "POST",
      "headers": { "Authorization": "Bearer {{HELPDESK_TOKEN}}" },
      "data": { "title": "{{title}}", "priority": "{{priority}}", "requester": "{{user_email}}" }
    }
  }
]}
```

If flow state already contains `HELPDESK_TOKEN` and `user_email`, the generated tool signature the model sees is exactly:

```json
{ "title": {"type": "string", "required": true},
  "priority": {"type": "string", "required": true} }
```

The token and the email are filled by the engine — the model can neither see nor invent them.

---

### Example C — MCP tools

```json
{ "key": "tools", "value": [
  { "name": "Jira Search", "node_type": "MCP Tool",
    "description": "Search Jira issues.",
    "config": { "server_id": "atlassian", "tool_name": "jira_search" } }
]}
```

v2 pulls the **real** input schema from the connected MCP server (`mcp_client_manager.server_tools`) and hands it to the model unchanged, so all remote fields, types and enums are respected. If the server is offline, the tool is skipped with a warning and the agent still answers.

---

### Example D — Pure conversational agent (no tools, memory only)

```json
"inputParameters": [
  { "key": "model",         "value": "gemini-2.5-flash" },
  { "key": "user_query",    "value": "{{CHAT_QUERY}}" },
  { "key": "tools",         "value": [] },
  { "key": "memory_window", "value": "20" },
  { "key": "memory_mode",   "value": "auto" },
  { "key": "memory_context","value": "Customer: {{customer_name}}, Plan: {{plan_name}}" },
  { "key": "system_prompt", "value": "You are a warm onboarding assistant." }
]
```

---

### Example E — Full mini flow schema (Start → ReAct v2 → End)

```json
{
  "agent_id": "it_support_react_v2",
  "name": "IT Support (ReAct v2)",
  "inputs": [
    { "key": "HELPDESK_TOKEN", "value": "{{global.helpdesk_token}}" },
    { "key": "user_email",     "value": "prithvi@sify.com" }
  ],
  "graphSpec": {
    "nodes": [
      { "node_id": "node-1", "type": "start",  "name": "Start Node" },
      { "node_id": "node-2", "type": "react_agent_v2", "name": "Autonomous ReAct Agent v2",
        "displayName": "IT Support Agent",
        "inputParameters": [
          { "key": "model",       "value": "gemini-2.5-pro" },
          { "key": "user_query",  "value": "{{CHAT_QUERY}}" },
          { "key": "memory_mode", "value": "auto" },
          { "key": "system_prompt", "value": "You are Sify's IT support agent." },
          { "key": "tools", "value": [
            { "name": "Get Ticket Status", "description": "Ticket status by id.",
              "node_type": "API caller",
              "config": { "url": "https://helpdesk.internal/api/tickets/{{ticket_id}}",
                          "method": "GET",
                          "headers": { "Authorization": "Bearer {{HELPDESK_TOKEN}}" } } }
          ]}
        ],
        "outputParameters": [ { "key": "output", "value": "agent_response" } ] },
      { "node_id": "node-3", "type": "output", "name": "End Node",
        "inputParameters": [ { "key": "final_output", "value": "{{agent_response}}" } ] }
    ],
    "edges": [
      { "source": "node-1", "target": "node-2" },
      { "source": "node-2", "target": "node-3" }
    ]
  }
}
```

> `final_output` matters: `format_exact_response()` returns `variables["final_output"]` as `agent_response` in the API payload.

---

## 8. How memory works now (the "it forgot my name" fix)

There are **two** stores in this project, and v1 only used the first:

| Store | Key | Written by | Read by |
|---|---|---|---|
| LangGraph checkpoint (`checkpoints`) | `thread_id` | the graph itself | `state["messages"]` |
| `conversation_memory` collection | `session_id` | `save_conversation_turn()` in `helpers.py` | **v2** (fallback) |

`memory_mode` decides the source:

| Mode | Behaviour | Use when |
|---|---|---|
| `auto` *(default)* | checkpoint first; if empty → load from MongoDB by `session_id` | **almost always** |
| `state` | checkpoint only (v1 behaviour) | single-thread flows, tests |
| `db` | MongoDB only | the UI rotates `thread_id` every message |
| `off` | stateless | batch/automation flows |

On top of the source, v2 applies:

1. **`_sanitize()`** — drops empty messages, orphan `ToolMessage`s and dangling tool-call stubs (these were causing provider 400s and mysterious empty answers).
2. **`_trim_to_turns(window)`** — keeps the last *N human turns intact* instead of slicing a raw message count in the middle of a turn.
3. **Duplicate-question guard** — if the last stored message is identical to the current question, it's dropped, so the model doesn't answer the previous turn.
4. **Sticky facts block** — any flow variable named `user_*`, `customer_*`, `remembered_*`, `profile_*` is rendered into the prompt every single turn:

```
### KNOWN FACTS ABOUT THIS USER/SESSION (already established) ###
- user_name: Prithvi
- customer_id: CUST-9931
```

So even at turn 50, with a window of 10, the agent still knows the name.

**Recommended setup for a chat UI:** keep `session_id` **and** `thread_id` stable for the whole conversation, and set `memory_mode: "auto"`. If your front-end can only keep `session_id`, use `memory_mode: "db"`.

**To make a fact sticky**, store it in a variable, e.g. with an earlier `Vocabulary Extractor`/`LLM invoker`/`Text Node` writing `user_name`.

---

## 9. How tools are called correctly now

Five mechanisms, in order of impact:

1. **Typed schema per tool.** The model sees `ticket_id: string (required) — "Ticket id such as INC-1042"`, not `parameters_json: str`.
2. **Auto-inference from `{{placeholders}}`.** Existing flow configs become correct tools with no extra authoring; unresolvable placeholders become required arguments, resolvable ones stay engine-filled.
3. **Built-in operating rules** appended to every system prompt:

```
1. Read the conversation history first … never say you don't know it.
2. Decide if a tool is needed …
3. Fill EVERY required argument with real values … never pass '{{placeholders}}'.
4. If a result starts with TOOL_ERROR, fix the arguments and retry ONCE …
5. Never call the same tool twice with the same arguments.
6. Finish with one clear, complete answer …
```

4. **Actionable errors.** Exceptions become an observation the model can act on:
   `TOOL_ERROR: 404 Not Found. Check your arguments (ticket_id) and retry once…`
5. **Safety rails.** Unknown `node_type` → tool skipped (not a crash); output truncated at 4 000 chars; tool names sanitised to `[a-z0-9_-]{1,60}` so every provider accepts them.

---

## 10. Scenario matrix — what to configure for what

| Scenario | Key settings |
|---|---|
| Customer-support chat with memory | `memory_mode: auto`, `memory_window: 10-20`, `response_format: html` |
| Long conversations, must remember profile | store `user_*` / `customer_*` variables + `memory_context` |
| One-shot automation (no chat) | `memory_mode: off`, `max_iterations: 3-5` |
| Heavy multi-tool research | `max_iterations: 12`, `return_trace: true`, `temperature: 0` |
| Output feeds another node | `response_format: json` + a `Decision Node` after it |
| Cheap/fast triage | `model: gemini-2.5-flash`, `max_iterations: 4` |
| External systems via MCP | `node_type: "MCP Tool"` |
| Tool must never be misused | declare `parameters` explicitly with `enum` and sharp `description`s |
| Debugging a wrong tool call | `return_trace: true`, then inspect `{{<out>_trace}}` and `{{<out>_tool_calls}}` |

---

## 11. Migration from v1 + troubleshooting

### Migration (2 minutes)

1. Change the node name in your flow JSON: `"Autonomous ReAct Agent"` → `"Autonomous ReAct Agent v2"`.
2. Keep `model`, `user_query`, `system_prompt`, `memory_window`, `tools` exactly as they are — all v1 keys are supported.
3. Optionally add `memory_mode`, `max_iterations`, `return_trace`, and `parameters` on the tools you care most about.
4. Nothing else changes: same `outputParameters`, same state, same checkpointer.
   The node is already registered in `app/main.py` (`import app.nodes.react_agent_v2`).

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Still forgets earlier turns | UI sends a new `thread_id` each message | `memory_mode: "db"` and keep `session_id` stable |
| Facts lost after many turns | window trimming | store them as `user_*`/`customer_*` variables, or use `memory_context` |
| Tool never called | weak `description` | say *when* to use it: "Use this whenever the user mentions a ticket id" |
| Wrong arguments | inferred names unclear | declare `parameters` with `description`, `example`, `enum` |
| `Tool 'x' skipped — unknown node_type` | typo in `node_type` | must exactly match a `NodeRegistry.register(...)` name |
| MCP tool missing | server not connected at startup | check `mcpServers.json` and the startup logs |
| Answer is the `fallback_answer` | loop hit `max_iterations` or model returned nothing | raise `max_iterations`, simplify the toolset, check the trace |
| Tool result looks cut off | 4 000-char truncation | make the tool return less (e.g. lower KB `limit`) |

### Log cheat-sheet

```
🧠 Entering Autonomous ReAct Agent v2...
🛠️  2 tool(s) ready: ['search_knowledge_base', 'get_ticket_status']
🧠 Memory: 4 message(s) in context (mode=auto, window=10).
🚀 ReAct v2 loop | model=gemini-2.5-pro | query='what is my name and check INC-1042'
🤔 [Thought→Action] get_ticket_status {'ticket_id': 'INC-1042'}
🔧 [API caller] get_ticket_status args={'ticket_id': 'INC-1042'}
✅ [Observation] {"status": "Open", "assigned_to": "Network Team"}
🗣️ [Answer] You're Prithvi — ticket INC-1042 is Open with the Network Team.
```
