import uuid
import httpx
import json
import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Any
from pydantic import BaseModel, Field

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import PydanticOutputParser
from langchain_google_vertexai import ChatVertexAI
from langchain_openai import ChatOpenAI

from app.core.config import settings
from app.services.mcp_client import mcp_client_manager
from app.utils.memory import get_conversation_history, save_conversation_turn
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

# ==========================================
# 1. PYDANTIC MODELS (Strict Schema)
# ==========================================
class FlowParameter(BaseModel):
    key: str
    value: Any
    type: str = "text"

class FlowNode(BaseModel):
    node_id: str = Field(description="Unique ID, e.g., 'Node_123'")
    name: str = Field(description="The exact tool/agent/node name")
    displayName: str = Field(description="Display name for the UI")
    type: str = Field(description="'start', 'output', 'outputs', 'tool', 'agent', 'conditions', 'inputs', 'node', 'iterator', or 'classifier'")
    description: Optional[str] = ""
    interrupt: Optional[bool] = False
    next: Optional[List[str]] = []
    loopPath: Optional[str] = None
    completionPath: Optional[str] = None
    completePath: Optional[str] = None
    inputParameters: List[FlowParameter] = []
    outputParameters: List[FlowParameter] = []

class FlowEdge(BaseModel):
    from_node: str = Field(alias="from")
    to_node: str = Field(alias="to")
    condition: Optional[str] = None

class FlowGraphSpec(BaseModel):
    nodes: List[FlowNode]
    edges: List[FlowEdge]

class DynamicFlowResponse(BaseModel):
    name: str = Field(description="A short, descriptive name for this workflow.")
    description: str = Field(description="A detailed description of what this workflow does.")
    graphSpec: FlowGraphSpec

# ==========================================
# 2. CORE UI NODES BLUEPRINT
# ==========================================
CORE_NODES_BLUEPRINT = """
### CORE UI NODES BLUEPRINT ###
When building the graph, use these exact structures for core logic. Replace {{variables}} as needed.

1. START NODE
{"name": "Start Node", "displayName": "Start Node", "type": "start", "interrupt": false, "inputParameters": [], "outputParameters": []}

2. END NODE
{"name": "End Node", "displayName": "End Node", "type": "output", "interrupt": false, "inputParameters": [{"key": "final_input", "value": "{{variable}}", "type": "string"}], "outputParameters": [{"key": "output", "value": "final_output", "type": "string"}]}

3. QUESTION NODE (To ask user for input)
{"name": "Question Node", "displayName": "Ask User", "type": "inputs", "interrupt": true, "inputParameters": [{"key": "question_text", "value": "Your question here?", "type": "string"}, {"key": "options", "value": {}, "type": "object"}], "outputParameters": [{"key": "output", "value": "user_response_var", "type": "string"}]}

4. DECISION NODE (If/Else routing)
{"name": "Decision Node", "displayName": "Evaluate Choice", "type": "conditions", "inputParameters": [{"key": "inputValue", "value": "{{variable}}", "type": "text"}, {"key": "conditions", "value": [{"operator": "equal_to", "comparisonValue": "yes", "nextNode": "Node_ID_Here"}], "type": "condition"}], "outputParameters": [{"key": "output", "value": "decision_result", "type": "string"}]}
Operators available: "equal_to", "not_equals", "greater_than", "less_than", "is_empty", "is_not_empty", "contains", "not_contains".

5. ITERATOR NODE (Looping)
{"name": "Iterator Node", "displayName": "Iterator Node", "type": "iterator", "loopPath": "Node_ID_For_Loop", "completePath": "Node_ID_For_Done", "inputParameters": [{"key": "array", "value": "{{array_var}}", "type": "text"}, {"key": "iterationVariable", "value": "item", "type": "text"}], "outputParameters": []}
NOTE: Iterator nodes MUST have `loopPath` and `completePath` pointing to valid node_ids!

6. KNOWLEDGE RETRIEVAL NODE (Vector Search)
{"name": "Knowledge Retrieval Node", "displayName": "Knowledge Retrieval Node", "type": "tool", "inputParameters": [{"key": "knowledge_base_name", "value": "kb_name", "type": "text"}, {"key": "user_prompt", "value": "{{CHAT_QUERY}}", "type": "text"}, {"key": "limit", "value": 3, "type": "number"}, {"key": "max_distance", "value": 0.7, "type": "number"}], "outputParameters": [{"key": "output", "value": "kb_context", "type": "string"}]}

7. QUESTION CLASSIFIER
{"name": "Question Classifier", "displayName": "Question Classifier", "type": "classifier", "inputParameters": [{"key": "input_text", "value": "{{CHAT_QUERY}}", "type": "string"}, {"key": "classifications", "value": [{"label": "Support", "description": "Support queries"}], "type": "array"}, {"key": "model", "value": "Gemini", "type": "string"}, {"key": "instructions", "value": "Classify intent", "type": "string"}], "outputParameters": [{"key": "output", "value": "question_category", "type": "string"}]}

8. TEXT NODE / RESPONSE FORMATTER
{"name": "response_formatter", "displayName": "Response Formatter", "type": "outputs", "inputParameters": [{"key": "template", "value": "Formatted: {{var}}", "type": "text"}], "outputParameters": [{"key": "formatted_output", "value": "out_var", "type": "text"}]}
"""

# ==========================================
# 3. HELPER: METADATA TRACKING
# ==========================================
def _get_flow_metadata(mongo_client, session_id: str):
    """Retrieves existing agent_id and name for a continuing session."""
    db = mongo_client[settings.MONGO_DB_NAME]
    record = db["conversation_memory"].find_one({"session_id": session_id})
    if record and "agent_id" in record:
        return record["agent_id"], record.get("agent_name")
    return None, None

def _set_flow_metadata(mongo_client, session_id: str, agent_id: str, agent_name: str):
    """Saves the agent_id and name so it doesn't change on the next turn."""
    db = mongo_client[settings.MONGO_DB_NAME]
    db["conversation_memory"].update_one(
        {"session_id": session_id},
        {"$set": {"agent_id": agent_id, "agent_name": agent_name}},
        upsert=True
    )

# ==========================================
# 4. DATA AGGREGATION & LLM GENERATION
# ==========================================
async def fetch_available_blocks() -> str:
    """Combines Core Nodes with Dynamic Agents, Tools, and MCP Tools."""
    blocks_context = CORE_NODES_BLUEPRINT + "\n"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"{settings.SCHEMA_API_URL.replace('agent-flow/', 'agents')}")
            if res.status_code == 200:
                blocks_context += "### DYNAMIC AGENTS ###\n"
                for a in res.json():
                    blocks_context += f"- Name: '{a.get('name')}'\n  Inputs: {[p.get('key') for p in a.get('inputParameters', [])]}\n"
    except Exception: pass

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"{settings.SCHEMA_API_URL.replace('agent-flow/', 'tools')}")
            if res.status_code == 200:
                blocks_context += "\n### DYNAMIC TOOLS ###\n"
                for t in res.json():
                    blocks_context += f"- Name: '{t.get('name')}'\n  Inputs: {[p.get('key') for p in t.get('inputParameters', [])]}\n"
    except Exception: pass

    blocks_context += "\n### MCP EXTERNAL TOOLS ###\n"
    blocks_context += "For these, create a tool node with name: 'MCP Tool Caller'. Inputs MUST be: 'server_id', 'tool_name', 'arguments'.\n"
    for server_id, tools in mcp_client_manager.server_tools.items():
        for t in tools:
            blocks_context += f"- server_id: '{server_id}', tool_name: '{t.name}'\n"

    return blocks_context

async def generate_workflow_schema(user_query: str, mongo_client, session_id: str, thread_id: str) -> dict:
    """Uses Model-Agnostic parsing WITH Memory to generate/update the JSON flow."""
    available_blocks = await fetch_available_blocks()
    chat_history = await asyncio.to_thread(get_conversation_history, mongo_client, session_id, 5)

    # 🚀 1. Check if we already started building a flow in this session
    existing_id, existing_name = await asyncio.to_thread(_get_flow_metadata, mongo_client, session_id)

    parser = PydanticOutputParser(pydantic_object=DynamicFlowResponse)
    format_instructions = parser.get_format_instructions()

    system_prompt = """
    You are an expert LangGraph Workflow Architect. 
    You are in an iterative conversation with the user. Look at the chat history, update the workflow, and output the ENTIRE updated valid JSON schema. Do not output partial JSON.

    {available_blocks}

    CRITICAL RULES:
    1. The graph MUST start with 'Start Node' and end with 'End Node'.
    2. To pass data between nodes, use Jinja2 templating (e.g., "{{variable_name}}") in the inputParameters.
    3. Ensure every node's `inputParameters` matches the keys defined in the blueprints.
    4. Ensure Edges strictly connect the generated `node_id`s logically.

    FORMAT INSTRUCTIONS:
    {format_instructions}
    """

    try:
        creds = service_account.Credentials.from_service_account_file("service-account.json", scopes=["https://www.googleapis.com/auth/cloud-platform"])
        llm = ChatVertexAI(model="gemini-1.5-pro", credentials=creds, temperature=0.1)
    except Exception:
        llm = ChatVertexAI(model="gemini-1.5-pro", temperature=0.1)

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder(variable_name="chat_history"),
        ("human", "{user_query}")
    ])

    chain = prompt | llm
    
    response = await chain.ainvoke(
        {
            "user_query": user_query,
            "chat_history": chat_history,
            "available_blocks": available_blocks,
            "format_instructions": format_instructions
        },
        config={"run_name": "Dynamic Flow Architect", "tags": ["dynamic-flow"]}
    )
    
    # Strip Markdown
    clean_text = response.content.strip()
    if clean_text.startswith("```json"): clean_text = clean_text[7:]
    elif clean_text.startswith("```"): clean_text = clean_text[3:]
    if clean_text.endswith("```"): clean_text = clean_text[:-3]
    clean_text = clean_text.strip()

    try:
        parsed_flow: DynamicFlowResponse = parser.parse(clean_text)
    except Exception as parse_error:
        logger.error(f"Failed to parse LLM Output into Schema: {parse_error}\nRaw Text: {clean_text}")
        raise ValueError("The AI generated an invalid schema format. Please try your request again.")

    flow_dict = parsed_flow.model_dump(by_alias=True)
    
    # Prune nulls
    for node in flow_dict["graphSpec"]["nodes"]:
        node.pop("loopPath", None) if not node.get("loopPath") else None
        node.pop("completionPath", None) if not node.get("completionPath") else None
        node.pop("completePath", None) if not node.get("completePath") else None

    # 🚀 2. Maintain ID and Name across iterations
    flow_id = existing_id if existing_id else str(uuid.uuid4())
    flow_name = existing_name if existing_name else flow_dict["name"]

    # Save to metadata if it's the first run
    if not existing_id:
        await asyncio.to_thread(_set_flow_metadata, mongo_client, session_id, flow_id, flow_name)

    now_str = datetime.utcnow().isoformat() + "Z"
    
    final_schema = {
        "name": flow_name,  # Use the locked name!
        "description": flow_dict["description"],
        "type": "flow",
        "graphSpec": flow_dict["graphSpec"],
        "status": "active",
        "version": "1.0.0",
        "isPublic": True,
        "createdBy": "AI Architect",
        "inputs": [],
        "id": flow_id,       # Use the locked ID!
        "agent_id": flow_id, # Use the locked ID!
        "voice_enabled": False,
        "createdAt": now_str,
        "updatedAt": now_str
    }

    # 🚀 3. PUT Request to External API
    try:
        api_url = "https://apidev.sifymodernization.digital/ai/api/agent-studio/agent-flow"
        async with httpx.AsyncClient(timeout=15.0) as client:
            put_res = await client.put(api_url, json=final_schema)
            put_res.raise_for_status()
            logger.info(f"✅ Successfully saved schema to external API for agent_id: {flow_id}")
    except httpx.HTTPStatusError as http_err:
        logger.error(f"❌ External API rejected the PUT request: {http_err.response.status_code} - {http_err.response.text}")
    except Exception as api_err:
        logger.error(f"❌ Failed to reach external API during PUT request: {api_err}")

    # 4. Save to memory for iterative editing
    await asyncio.to_thread(
        save_conversation_turn,
        mongo_client,
        session_id,
        thread_id,
        user_query,
        f"Successfully generated workflow: {flow_name}. Here is the schema context: {json.dumps(flow_dict)}"
    )

    return final_schema