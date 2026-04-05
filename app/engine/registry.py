from typing import Callable, Dict
import logging

logger = logging.getLogger(__name__)

class NodeRegistry:
    """Production Node Registry. Fails strictly if a node is missing."""
    _executors: Dict[str, Callable] = {}

    @classmethod
    def register(cls, node_name: str):
        def decorator(func: Callable):
            cls._executors[node_name] = func
            return func
        return decorator

    @classmethod
    def get_executor(cls, node_name: str) -> Callable:
        if node_name not in cls._executors:
            raise NotImplementedError(
                f"CRITICAL: Node '{node_name}' is required by the UI JSON but has no Python implementation registered."
            )
        return cls._executors[node_name]