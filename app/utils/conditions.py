import operator
from typing import Any

class ConditionEvaluator:
    """Safely evaluates conditional edge logic."""
    
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
    def _is_num(a, b) -> bool:
        try:
            float(a)
            float(b)
            return True
        except (ValueError, TypeError):
            return False

    @classmethod
    def evaluate(cls, left: Any, op_str: str, right: Any) -> bool:
        op_func = cls.OPERATORS.get(op_str)
        if not op_func:
            return False
        
        try:
            return op_func(left, right)
        except Exception:
            return False