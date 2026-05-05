"""
Token Tracking Aspect (AOP) — Cross-cutting concern for LLM token usage.

This module provides:
  1. `TokenTracker`     — Request-scoped accumulator for token usage stats.
  2. `TokenCallbackHandler` — LangChain callback that intercepts all LLM calls
                              and feeds usage data into the active TokenTracker.
  3. `token_tracker_ctx`    — contextvars-based scoping so each async request
                              gets its own isolated tracker.

### How it works (AOP pattern):
  - Before a request starts, `start_tracking()` creates a fresh tracker in contextvars.
  - The callback handler is injected into every LLM via `_get_llm()` (single line change).
  - When ANY LLM call finishes (from any node — agents, db_chat, react_agent),
    the callback automatically records prompt/completion/total tokens.
  - At the end of the request, `get_tracker().summary()` returns the full report.
  - No node code is modified — the concern is fully separated.
"""

import time
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from contextvars import ContextVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)


# ── 1. Token Tracker (Request-scoped accumulator) ────────────────────────────

@dataclass
class LLMCallRecord:
    """Single LLM invocation record."""
    model: str
    node_id: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class TokenTracker:
    """Accumulates token usage across all LLM calls in a single request."""
    calls: List[LLMCallRecord] = field(default_factory=list)
    _active_node_id: str = ""
    _call_start: float = 0.0

    def set_active_node(self, node_id: str):
        """Called by the logging wrapper to tag which node is currently executing."""
        self._active_node_id = node_id

    def record_start(self):
        """Mark the start of an LLM call for duration tracking."""
        self._call_start = time.time()

    def record(self, model: str, prompt_tokens: int, completion_tokens: int, total_tokens: int):
        """Record a completed LLM call."""
        duration_ms = (time.time() - self._call_start) * 1000 if self._call_start else 0
        record = LLMCallRecord(
            model=model,
            node_id=self._active_node_id or "unknown",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            duration_ms=round(duration_ms, 1),
        )
        self.calls.append(record)
        logger.info(
            f"📊 [Token Tracking] node={record.node_id} | model={model} | "
            f"prompt={prompt_tokens} + completion={completion_tokens} = "
            f"total={total_tokens} | {record.duration_ms}ms"
        )

    def summary(self) -> Dict[str, Any]:
        """Produce the final token usage summary for the API response."""
        total_prompt = sum(c.prompt_tokens for c in self.calls)
        total_completion = sum(c.completion_tokens for c in self.calls)
        total_all = sum(c.total_tokens for c in self.calls)
        total_duration = sum(c.duration_ms for c in self.calls)

        per_node = {}
        for c in self.calls:
            if c.node_id not in per_node:
                per_node[c.node_id] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "call_count": 0,
                    "duration_ms": 0,
                    "model": c.model,
                }
            entry = per_node[c.node_id]
            entry["prompt_tokens"] += c.prompt_tokens
            entry["completion_tokens"] += c.completion_tokens
            entry["total_tokens"] += c.total_tokens
            entry["call_count"] += 1
            entry["duration_ms"] += c.duration_ms

        return {
            "total_llm_calls": len(self.calls),
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_all,
            "total_llm_duration_ms": round(total_duration, 1),
            "per_node": per_node,
            "calls": [
                {
                    "node_id": c.node_id,
                    "model": c.model,
                    "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens,
                    "total_tokens": c.total_tokens,
                    "duration_ms": c.duration_ms,
                }
                for c in self.calls
            ],
        }


# ── 2. Context Variable (request-scoped, async-safe) ─────────────────────────

token_tracker_ctx: ContextVar[Optional[TokenTracker]] = ContextVar(
    "token_tracker_ctx", default=None
)


def start_tracking() -> TokenTracker:
    """Start a new tracker for the current async request context."""
    tracker = TokenTracker()
    token_tracker_ctx.set(tracker)
    return tracker


def get_tracker() -> Optional[TokenTracker]:
    """Get the current request's tracker (None if tracking not started)."""
    return token_tracker_ctx.get()


# ── 3. LangChain Callback Handler (the AOP "advice") ─────────────────────────

class TokenCallbackHandler(BaseCallbackHandler):
    """
    LangChain callback that intercepts on_llm_start / on_llm_end events
    and feeds usage data into the active TokenTracker.

    This is the core AOP mechanism — it wraps around all LLM calls
    without modifying any node code.
    """

    def on_llm_start(self, serialized: Dict[str, Any], prompts: List[str], **kwargs):
        tracker = token_tracker_ctx.get()
        if tracker:
            tracker.record_start()

    def on_llm_end(self, response: LLMResult, **kwargs):
        tracker = token_tracker_ctx.get()
        if not tracker:
            return

        # Extract token usage from LLM response metadata
        # Works for both VertexAI (Gemini) and OpenAI (Llama) providers
        llm_output = response.llm_output or {}
        usage = llm_output.get("usage_metadata") or llm_output.get("token_usage") or {}

        # VertexAI uses 'input_tokens'/'output_tokens', OpenAI uses
        # 'prompt_tokens'/'completion_tokens'
        prompt_tokens = (
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0
        )
        completion_tokens = (
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
        )
        total_tokens = (
            usage.get("total_tokens")
            or (prompt_tokens + completion_tokens)
        )

        # Try to get model name
        model = llm_output.get("model_name") or llm_output.get("model") or "unknown"

        # Also check generation-level usage (some providers put it here)
        if total_tokens == 0 and response.generations:
            for gen_list in response.generations:
                for gen in gen_list:
                    gen_info = getattr(gen, "generation_info", {}) or {}
                    gen_usage = gen_info.get("usage_metadata", {})
                    if gen_usage:
                        prompt_tokens = gen_usage.get("prompt_token_count", gen_usage.get("input_tokens", 0))
                        completion_tokens = gen_usage.get("candidates_token_count", gen_usage.get("output_tokens", 0))
                        total_tokens = gen_usage.get("total_token_count", prompt_tokens + completion_tokens)

        tracker.record(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )


# Singleton callback instance — safe to share (all state is in contextvars)
token_callback_handler = TokenCallbackHandler()
