import json
import logging
import inspect
from typing import List, Dict, Any

from pydantic import BaseModel, Field, create_model
from langchain_core.tools import StructuredTool, tool
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState
from app.nodes.agents import _get_llm

# Import your existing legacy client which already has the tools loaded!
from app.services.mcp_client import mcp_client_manager
from app.core.model import MCPToolCall

logger = logging.getLogger(__name__)

# --- 1. DYNAMIC TOOL SCHEMA ---
class DynamicToolInput(BaseModel):
    parameters_json: str = Field(
        default="{}",
        description="A valid JSON string containing variables needed for this tool."
    )

def _extract_pure_text(content: Any) -> str:
    """Strips block formatting to ensure pure text is returned."""
    if isinstance(content, str): return content
    if isinstance(content, list):
        return "\n".join([b.get("text", "") if isinstance(b, dict) else str(b) for b in content])
    return str(content)

# --- 2. DYNAMIC SCHEMA GENERATOR ---
def _json_schema_to_pydantic(schema: dict, name: str):
    """Converts an MCP JSON Schema directly into a Pydantic Model for LangChain."""
    fields = {}
    props = schema.get("properties", {})
    required = schema.get("required", [])
    
    type_mapping = {
        "string": str, "integer": int, "number": float,
        "boolean": bool, "array": list, "object": dict
    }
    
    for k, v in props.items():
        py_type = type_mapping.get(v.get("type", "string"), Any)
        desc = v.get("description", "")
        if k in required:
            fields[k] = (py_type, Field(..., description=desc))
        else:
            fields[k] = (py_type, Field(default=None, description=desc))
            
    if not fields:
        return create_model(name, dummy=(str, Field(default="", description="Not used")))
        
    return create_model(name, **fields)

# --- 3. DYNAMIC TOOL FACTORY ---
def _build_dynamic_tool(tool_def: dict, state: FlowState) -> Any:
    raw_name = tool_def.get("name", "unnamed_tool")
    tool_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in raw_name.replace(" ", "_")).lower()
    description = tool_def.get("description", "No description provided.")
    node_type = tool_def.get("node_type", "API caller") 
    base_config = tool_def.get("config", {})

    # ==============================================================
    # 🌟 DIRECT MCP INJECTION (The Magic Fix)
    # Extracts the exact tool from your connected MCP client.
    # ==============================================================
    if node_type == "MCP Tool":
        server_id = resolve_placeholders(base_config.get("server_id", ""), state["variables"])
        actual_tool_name = resolve_placeholders(base_config.get("tool_name", ""), state["variables"])
        
        mcp_tool_obj = None
        for t in mcp_client_manager.server_tools.get(server_id, []):
            if t.name == actual_tool_name:
                mcp_tool_obj = t
                break
                
        if mcp_tool_obj:
            logger.info(f"⚡ Injecting Direct MCP Tool: {actual_tool_name} from {server_id}")
            schema_model = _json_schema_to_pydantic(mcp_tool_obj.inputSchema or {}, f"Schema_{actual_tool_name}")
            
            async def _direct_mcp_call(**kwargs) -> str:
                req = MCPToolCall(server_id=server_id, tool_name=actual_tool_name, arguments=kwargs)
                res = await mcp_client_manager.call_tool(req)
                return json.dumps(res)

            return StructuredTool.from_function(
                func=lambda **kwargs: "Async required",
                coroutine=_direct_mcp_call,
                name=tool_name,
                description=mcp_tool_obj.description or description,
                args_schema=schema_model
            )
        else:
            logger.warning(f"⚠️ MCP Tool '{actual_tool_name}' not found on server '{server_id}'. Check connections.")

    # ==============================================================
    # 🛡️ STANDARD API CALLER FALLBACK
    # ==============================================================
    async def _execute_tool(parameters_json: str = "{}") -> str:
        try: llm_args = json.loads(parameters_json)
        except: return "Error: Invalid JSON."
        temp_state = state.copy()
        temp_state["variables"] = {**state.get("variables", {}), **llm_args}
        try:
            executor = NodeRegistry.get_executor(node_type)
            fake_config = {
                "name": tool_name,
                "inputParameters": [{"key": k, "value": v} for k, v in base_config.items()],
                "outputParameters": [{"key": "output", "value": "react_temp_output"}]
            }
            result = await executor(temp_state, fake_config)
            output = result.get("variables", {}).get("react_temp_output", str(result))
            return json.dumps(output) if isinstance(output, (dict, list)) else str(output)
        except Exception as e:
            return f"Tool execution failed: {str(e)}"

    return StructuredTool.from_function(
        func=lambda parameters_json="{}": "Async required.",
        coroutine=_execute_tool,
        name=tool_name,
        description=description,
        args_schema=DynamicToolInput
    )

# --- 4. THE TRACEABLE REACT AGENT NODE ---
@NodeRegistry.register("Autonomous ReAct Agent")
async def react_agent_node(state: FlowState, node_config: dict) -> dict:
    logger.info("🧠 Entering Autonomous ReAct Agent Node...")
    
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    model_choice = resolve_placeholders(inputs.get("model", "gemini-2.5-pro"), state["variables"])
    user_query = str(resolve_placeholders(inputs.get("user_query", "{{CHAT_QUERY}}"), state["variables"])).strip()
    system_prompt = resolve_placeholders(inputs.get("system_prompt", "You are an expert autonomous agent."), state["variables"])
    memory_window = int(resolve_placeholders(inputs.get("memory_window", 10), state["variables"]))
    
    tools_json = resolve_placeholders(inputs.get("tools", []), state["variables"])
    if isinstance(tools_json, str):
        try: tools_json = json.loads(tools_json.replace("'", '"'))
        except: tools_json = []

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "react_final_answer"

    langchain_tools = [_build_dynamic_tool(t, state) for t in tools_json]
    
    llm = _get_llm(model_choice)
    all_messages = state.get("messages", [])
    recent_history = all_messages[-(memory_window * 2):] if memory_window > 0 and len(all_messages) > 0 else []

    agent_kwargs = {}
    sig = inspect.signature(create_react_agent)
    if "state_modifier" in sig.parameters: agent_kwargs["state_modifier"] = system_prompt
    elif "messages_modifier" in sig.parameters: agent_kwargs["messages_modifier"] = system_prompt
    elif "prompt" in sig.parameters: agent_kwargs["prompt"] = system_prompt

    agent_executor = create_react_agent(llm, tools=langchain_tools, **agent_kwargs)

    try:
        messages_payload = []
        if not agent_kwargs and system_prompt:
            messages_payload.append(SystemMessage(content=system_prompt))
            
        messages_payload.extend(recent_history)
        messages_payload.append(HumanMessage(content=user_query))
        
        logger.info(f"🚀 Firing ReAct Loop. Query: '{user_query[:50]}...'")
        final_answer = ""
        run_name = f"ReAct: {user_query[:20]}..."
        
        async for chunk in agent_executor.astream(
            {"messages": messages_payload}, 
            config={"recursion_limit": 50, "run_name": run_name},
            stream_mode="updates"
        ):
            for node_name, state_update in chunk.items():
                msgs = state_update.get("messages", [])
                if not msgs: continue
                latest_msg = msgs[-1]

                if node_name == "agent":
                    if hasattr(latest_msg, "tool_calls") and latest_msg.tool_calls:
                        for tc in latest_msg.tool_calls:
                            logger.info(f"🤔 [Thought] Calling tool ➡️ '{tc.get('name')}' with Args: {tc.get('args')}")
                    elif latest_msg.content:
                        clean_text = _extract_pure_text(latest_msg.content)
                        logger.info(f"🗣️ [Answer]  {clean_text[:200]}...")
                        final_answer = clean_text
                elif node_name == "tools":
                    safe_content = _extract_pure_text(latest_msg.content)
                    logger.info(f"✅ [Tool Output] {safe_content[:300]}...")

        if not final_answer:
            final_answer = "Task complete."

        return {
            "variables": {output_key: final_answer},
            "messages": [HumanMessage(content=user_query), AIMessage(content=final_answer)]
        }
        
    except Exception as e:
        logger.error(f"❌ ReAct Agent Error: {e}", exc_info=True)
        return {"variables": {output_key: f"Error: {str(e)}" }}