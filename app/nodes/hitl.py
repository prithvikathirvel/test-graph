import logging
from langgraph.types import interrupt
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

@NodeRegistry.register("Question Node")
async def question_node(state: FlowState, node_config: dict) -> dict:
    
    # 🚀 ADD THIS PRINT STATEMENT
    print("\n" + "="*50)
    print(f"🛑 QUESTION NODE TRIGGERED: {node_config.get('name')}")
    print("="*50 + "\n")
    
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    question_text = resolve_placeholders(inputs.get("question_text", "Input required:"), state["variables"])
    options = resolve_placeholders(inputs.get("options", {}), state["variables"])
    
    input_type = "text"
    if isinstance(options, dict) and len(options) > 0:
        input_type = "selection"
    elif isinstance(options, list) and len(options) > 0:
        input_type = "selection"

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "selected_choice"

    # 🚀 NATIVE LANGGRAPH PAUSE
    print("⏸️ CALLING NATIVE INTERRUPT()...")
    user_response = interrupt({
        "node_id": node_config["node_id"],
        "node_name": node_config.get("name", "Question Node"),
        "node_type": node_config.get("type", "inputs"),
        "input_type": input_type,
        "question": question_text,
        "options": options if input_type == "selection" else {}
    })
    
    print(f"▶️ GRAPH RESUMED WITH ANSWER: {user_response}")
    return {"variables": {output_key: user_response}}