from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

@NodeRegistry.register("Start Node")
async def start_node(state: FlowState, node_config: dict) -> dict:
    """Mock Start Node just in case the compiler hits it."""
    return {}

@NodeRegistry.register("End Node")
async def end_node(state: FlowState, node_config: dict) -> dict:
    """Evaluates the final_input and prepares the response for the user."""
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    # Resolve the final dynamic message (e.g., "Sorry, couldn't find {{customer}}")
    final_text = resolve_placeholders(inputs.get("final_input", ""), state["variables"])
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "final_output"
    
    return {"variables": {output_key: final_text}}

@NodeRegistry.register("Text Node")
async def text_node(state: FlowState, node_config: dict) -> dict:
    """Basic text processing node."""
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    text = resolve_placeholders(inputs.get("text", ""), state["variables"])
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "output"
    
    return {"variables": {output_key: text}}

@NodeRegistry.register("response_formatter")
async def response_formatter_node(state: FlowState, node_config: dict) -> dict:
    """Formats output based on a template."""
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    template = resolve_placeholders(inputs.get("template", ""), state["variables"])
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "formatted_output"
    
    return {"variables": {output_key: template}}