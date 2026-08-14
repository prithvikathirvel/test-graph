"""Bounded TTL cache for compiled flow graphs and their source schemas."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any, Optional


@dataclass
class GraphCacheEntry:
    schema: dict[str, Any]
    graph: Any
    schema_hash: str
    expires_at: float


class GraphCache:
    _graphs: dict[str, GraphCacheEntry] = {}
    _lock = RLock()
    _max_entries = 256

    @staticmethod
    def schema_hash(schema: dict[str, Any]) -> str:
        payload = json.dumps(schema, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @classmethod
    def set(
        cls,
        agent_id: str,
        compiled_graph: Any,
        schema: Optional[dict[str, Any]] = None,
        ttl_seconds: int = 300,
    ) -> GraphCacheEntry:
        source = schema or {}
        entry = GraphCacheEntry(
            schema=source,
            graph=compiled_graph,
            schema_hash=cls.schema_hash(source),
            expires_at=time.monotonic() + max(1, ttl_seconds),
        )
        with cls._lock:
            if len(cls._graphs) >= cls._max_entries and agent_id not in cls._graphs:
                # Drop the entry nearest expiration. Compiled graphs have no
                # external resource cleanup requirement.
                oldest = min(cls._graphs, key=lambda key: cls._graphs[key].expires_at)
                cls._graphs.pop(oldest, None)
            cls._graphs[agent_id] = entry
        return entry

    @classmethod
    def get_entry(cls, agent_id: str) -> Optional[GraphCacheEntry]:
        with cls._lock:
            entry = cls._graphs.get(agent_id)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                cls._graphs.pop(agent_id, None)
                return None
            return entry

    @classmethod
    def get(cls, agent_id: str):
        """Backward-compatible graph-only lookup."""
        entry = cls.get_entry(agent_id)
        return entry.graph if entry else None

    @classmethod
    def invalidate(cls, agent_id: str) -> None:
        with cls._lock:
            cls._graphs.pop(agent_id, None)

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._graphs.clear()
