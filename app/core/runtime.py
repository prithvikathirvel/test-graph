"""Process-local references to application runtime services.

Nodes do not receive the FastAPI ``Request`` object.  This small registry makes
already-created infrastructure (Mongo client/checkpointer) available to nested
agent harnesses without creating duplicate connections.  Per-request identity
must still come from FlowState/runtime context, never from this registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimeServices:
    mongo_client: Any = None
    checkpointer: Any = None


_services: RuntimeServices | None = None


def set_runtime_services(*, mongo_client: Any, checkpointer: Any) -> RuntimeServices:
    global _services
    _services = RuntimeServices(mongo_client=mongo_client, checkpointer=checkpointer)
    return _services


def get_runtime_services() -> RuntimeServices:
    return _services or RuntimeServices()


def clear_runtime_services() -> None:
    global _services
    _services = None
