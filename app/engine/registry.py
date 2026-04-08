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
            logger.debug(f"Registered node executor: '{node_name}'")
            return func
        return decorator

    @classmethod
    def get_executor(cls, node_name: str) -> Callable:
        if node_name not in cls._executors:
            logger.error(f"Lookup Failed: Node implementation for '{node_name}' not found in registry.")
            raise NotImplementedError(
                f"CRITICAL: Node '{node_name}' is required by the UI JSON but has no Python implementation registered."
            )
        logger.debug(f"Retrieved node executor for: '{node_name}'")
        return cls._executors[node_name]