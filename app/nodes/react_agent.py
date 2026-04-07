import json
import logging
from typing import List, Dict, Any

from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.prebuilt import create_react_agent
from langgraph.types import interrupt

from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState
from app.nodes.agents import _get_llm

logger = logging.getLogger(__name__)

# --- 1. DYNAMIC TOOL SCHEMA ---
class DynamicToolInput(BaseModel):
    parameters_json: str = Field(
        description="A single, valid JSON string containing all the variables needed for this tool. E.g., '{\"server_id\": \"web-01\", \"action\": \"reboot\"}'"
    )

# --- 2. DYNAMIC TOOL FACTORY ---
def _build_dynamic_tool(tool_def: dict, state: FlowState) -> StructuredTool:
    """Converts a UI Tool JSON definition into a native LangChain ReAct Tool."""
    tool_name = tool_def.get("name", "unnamed_tool").replace(" ", "_").lower()
    description = tool_def.get("description", "No description provided.")
    node_type = tool_def.get("node_type", "API caller") 
    base_config = tool_def.get("config", {})

    async def _execute_tool(parameters_json: str = "{}") -> str:
        logger.info(f"🛠️ ReAct Agent executing tool '{tool_name}' with payload: {parameters_json}")
        
        try:
            llm_args = json.loads(parameters_json)
        except json.JSONDecodeError:
            return "Error: Tool execution failed. You did not provide a valid JSON string in parameters_json."

        # 1. Temporarily inject the LLM's arguments into the FlowState variables
        # This lets your existing nodes (like API caller) use {{server_id}} in their URLs/Bodies!
        temp_state = state.copy()
        temp_state["variables"] = {**state.get("variables", {}), **llm_args}

        try:
            # 2. Grab the actual Python executor from your backend registry
            executor = NodeRegistry.get_executor(node_type)
            
            # 3. Build a fake node_config mapping the UI config to the node's expected inputParameters
            fake_config = {
                "name": tool_name,
                "inputParameters": [{"key": k, "value": v} for k, v in base_config.items()],
                "outputParameters": [{"key": "output", "value": "react_temp_output"}]
            }

            # 4. Execute the tool natively!
            result = await executor(temp_state, fake_config)
            
            # 5. Return the raw text/JSON back to the Agent's observation loop
            output = result.get("variables", {}).get("react_temp_output", str(result))
            return json.dumps(output) if isinstance(output, (dict, list)) else str(output)
            
        except Exception as e:
            logger.error(f"Dynamic Tool Error: {e}")
            return f"Tool execution failed: {str(e)}"

    return StructuredTool.from_function(
        func=_execute_tool,
        name=tool_name,
        description=description,
        args_schema=DynamicToolInput
    )

# --- 3. THE REACT AGENT NODE ---
@NodeRegistry.register("Autonomous ReAct Agent")
async def react_agent_node(state: FlowState, node_config: dict) -> dict:
    """The master node orchestrating Reasoning and Tools."""
    logger.info("🧠 Entering Autonomous ReAct Agent Node...")
    
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    model_choice = resolve_placeholders(inputs.get("Model", "gemini-2.5-pro"), state["variables"])
    user_query = str(resolve_placeholders(inputs.get("user_query", "{{CHAT_QUERY}}"), state["variables"])).strip()
    system_prompt = resolve_placeholders(inputs.get("system_prompt", "You are an expert autonomous agent."), state["variables"])
    memory_window = int(resolve_placeholders(inputs.get("memory_window", 10), state["variables"]))
    
    # Extract dynamic tools array
    tools_json = resolve_placeholders(inputs.get("tools", []), state["variables"])
    if isinstance(tools_json, str):
        try: tools_json = json.loads(tools_json.replace("'", '"'))
        except: tools_json = []

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "react_final_answer"

    # Build LangChain Tools
    langchain_tools = [_build_dynamic_tool(t, state) for t in tools_json]

    # Initialize LLM & State
    llm = _get_llm(model_choice)
    all_messages = state.get("messages", [])
    recent_history = all_messages[-(memory_window * 2):] if memory_window > 0 and len(all_messages) > 0 else []

    # Build ReAct Sub-Graph using LangGraph's prebuilt executor
    agent_executor = create_react_agent(llm, tools=langchain_tools, state_modifier=system_prompt)

    try:
        agent_input = {"messages": recent_history + [HumanMessage(content=user_query)]}
        
        # Execute the Autonomous Loop! (Thought -> Action -> Observation -> Final Answer)
        agent_result = await agent_executor.ainvoke(
            agent_input,
            config={"recursion_limit": 50, "run_name": "NOC ReAct Agent"} # LangSmith Traceability
        )
        
        final_answer = agent_result["messages"][-1].content
        
        return {
            "variables": {output_key: final_answer},
            "messages": [HumanMessage(content=user_query), AIMessage(content=final_answer)]
        }
        
    except Exception as e:
        logger.error(f"❌ ReAct Agent Critical Error: {e}", exc_info=True)
        return {"variables": {output_key: f"Agent encountered a critical error: {str(e)}" }}