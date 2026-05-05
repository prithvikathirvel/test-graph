import operator
import json
import logging
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState
from typing import Any

logger = logging.getLogger(__name__)

class ConditionEvaluator:
    """Universal Evaluator for Decision Nodes."""
    OPERATORS = {
        "equal_to": operator.eq,
        "not_equals": operator.ne,
        "greater_than": lambda a, b: float(a) > float(b) if _is_num(a, b) else False,
        "less_than": lambda a, b: float(a) < float(b) if _is_num(a, b) else False,
        "is_empty": lambda a, b: not a,
        "is_not_empty": lambda a, b: bool(a),
        "contains": lambda a, b: str(b).lower() in str(a).lower() if a and b else False,
        "not_contains": lambda a, b: str(b).lower() not in str(a).lower() if a and b else True,
    }

    @staticmethod
    def evaluate(left: Any, op_str: str, right: Any) -> bool:
        op_func = ConditionEvaluator.OPERATORS.get(op_str)
        if not op_func: return False
        try:
            if isinstance(right, str) and (right.startswith("[") or right.startswith("{")):
                try: right = json.loads(right)
                except:
                    try: right = json.loads(right.replace("'", '"'))
                    except: pass
            if isinstance(left, str) and (left.startswith("[") or left.startswith("{")):
                try: left = json.loads(left)
                except:
                    try: left = json.loads(left.replace("'", '"'))
                    except: pass
            if str(left).isdigit() and str(right).isdigit():
                return int(left) == int(right)
            return op_func(left, right)
        except Exception:
            return False

def _is_num(a, b) -> bool:
    try: float(a); float(b); return True
    except: return False


# --- 1. DECISION NODE LOGIC (NEW!) ---
@NodeRegistry.register("Decision Node")
async def decision_node(state: FlowState, node_config: dict) -> dict:
    """Evaluates conditions and saves the winning target to the output parameter."""
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "decision_result"

    left_value = resolve_placeholders(inputs.get("inputValue"), state["variables"])
    conditions = inputs.get("conditions", [])

    for condition in conditions:
        op = condition.get("operator")
        right_value = resolve_placeholders(condition.get("comparisonValue"), state["variables"])
        
        if ConditionEvaluator.evaluate(left_value, op, right_value):
            target_node = condition.get("nextNode")
            logger.info(f"🔀 Decision | {left_value} {op} {right_value} -> Match! Routing to: {target_node}")
            return {"variables": {output_key: target_node}}

    logger.warning(f"⚠️ Decision Node: No condition met. Defaulting to END.")
    return {"variables": {output_key: "__END__"}}


# --- 2. ITERATOR NODE LOGIC ---
@NodeRegistry.register("Iterator Node")
async def iterator_node(state: FlowState, node_config: dict) -> dict:
    """Stateful sequential iterator."""
    node_id = node_config["node_id"]
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}

    array_data = resolve_placeholders(inputs.get("array", ""), state["variables"])
    iter_var = inputs.get("iterationVariable", "item")

    if isinstance(array_data, str):
        try: array_data = json.loads(array_data)
        except:
            try: array_data = json.loads(array_data.replace("'", '"'))
            except: array_data = [x.strip() for x in array_data.split(",") if x.strip()]
    if not isinstance(array_data, list):
        array_data = []

    idx_key = f"_iter_idx_{node_id}"
    current_idx = state["variables"].get(idx_key, 0)

    new_vars = {}
    if current_idx < len(array_data):
        item = array_data[current_idx]
        logger.debug(f"🔄 Iterator | idx: {current_idx}/{len(array_data)} | next item: {str(item)[:50]}...")
        new_vars[iter_var] = item
        new_vars[idx_key] = current_idx + 1
        new_vars[f"_iter_status_{node_id}"] = "loop"
    else:
        logger.info(f"🏁 Iterator | reached end of list ({len(array_data)} items).")
        new_vars[idx_key] = 0
        new_vars[f"_iter_status_{node_id}"] = "complete"

    return {"variables": new_vars}