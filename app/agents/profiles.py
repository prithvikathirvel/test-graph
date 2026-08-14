"""Versioned, server-owned harness profiles.

Profiles provide safe defaults while keeping the UI simple.  A node may narrow
these defaults; real authorization is still enforced by the policy gateway at
execution time.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

PROFILE_VERSION = "2026-08-14.1"


_PROFILES: dict[str, dict[str, Any]] = {
    # Exact compatibility profile for pre-v2 node JSON.  It adds hard execution
    # bounds and structured failures but does not suddenly deny existing tools.
    "legacy": {
        "schema_version": "1.0",
        "budgets": {
            "timeout_seconds": 180,
            "max_model_calls": 25,
            "max_tool_calls": 50,
            "max_calls_per_tool": 10,
            "max_parallel_tools": 5,
            "max_output_tokens": 8000,
            "on_limit": "return_partial",
        },
        "guardrails": {
            "fail_mode": "open",
            "input": {"max_chars": 100_000, "prompt_injection_detection": False},
            "tool": {
                "default_action": "allow",
                "block_private_network_egress": False,
                "max_result_chars": 50_000,
            },
            "output": {"pii_scan": False, "require_action_evidence": False},
        },
        "approval_policy": {"mode": "disabled", "default": "allow", "rules": []},
        "memory": {"short_term": {"strategy": "window", "recent_messages": 20}},
        "response": {"format": "text", "include_action_summary": False},
    },
    "safe_chat": {
        "schema_version": "2.0",
        "planning": {"mode": "direct", "todo_enabled": False},
        "budgets": {
            "timeout_seconds": 90,
            "max_model_calls": 8,
            "max_tool_calls": 8,
            "max_calls_per_tool": 3,
            "max_parallel_tools": 2,
            "max_output_tokens": 3000,
        },
        "guardrails": {
            "fail_mode": "closed",
            "input": {
                "max_chars": 30_000,
                "prompt_injection_detection": True,
                "pii": {"enabled": True, "strategy": "mask_for_model"},
            },
            "tool": {
                "default_action": "deny",
                "block_private_network_egress": True,
                "max_result_chars": 20_000,
            },
            "output": {"pii_scan": True, "require_action_evidence": True},
        },
        "approval_policy": {
            "mode": "risk_based",
            "default": "deny",
            "rules": [
                {"match": {"risk": "compute"}, "decision": "allow"},
                {"match": {"risk": "public_read"}, "decision": "allow"},
                {"match": {"risk": "read"}, "decision": "allow"},
            ],
        },
        "memory": {"short_term": {"strategy": "window", "recent_messages": 20}},
        "response": {"format": "agent_result"},
    },
    "support": {
        "schema_version": "2.0",
        "planning": {"mode": "adaptive", "todo_enabled": True, "max_plan_steps": 8},
        "budgets": {
            "timeout_seconds": 120,
            "max_model_calls": 12,
            "max_tool_calls": 16,
            "max_calls_per_tool": 4,
            "max_parallel_tools": 3,
            "max_output_tokens": 4000,
        },
        "guardrails": {
            "fail_mode": "closed",
            "input": {
                "max_chars": 30_000,
                "prompt_injection_detection": True,
                "content_policy": "customer_service",
                "pii": {"enabled": True, "strategy": "mask_for_model"},
            },
            "tool": {
                "default_action": "deny",
                "block_private_network_egress": True,
                "max_result_chars": 20_000,
            },
            "output": {
                "pii_scan": True,
                "content_policy": "customer_service",
                "require_action_evidence": True,
            },
        },
        "approval_policy": {
            "mode": "risk_based",
            "default": "ask",
            "rules": [
                {"match": {"risk": "compute"}, "decision": "allow"},
                {"match": {"risk": "public_read"}, "decision": "allow"},
                {"match": {"risk": "read"}, "decision": "allow"},
                {"match": {"risk": "read_sensitive"}, "decision": "allow"},
                {"match": {"risk": "reversible_write"}, "decision": "allow"},
                {
                    "match": {"risk": "external_communication"},
                    "decision": "ask",
                    "allowed_decisions": ["approve", "edit", "reject"],
                },
                {
                    "match": {"risk": "high_impact_write"},
                    "decision": "ask",
                },
                {"match": {"risk": "financial"}, "decision": "deny"},
                {"match": {"risk": "destructive"}, "decision": "deny"},
            ],
        },
        "memory": {
            "short_term": {
                "strategy": "summarize",
                "recent_messages": 20,
                "summarize_at_tokens": 30_000,
            }
        },
        "response": {"format": "agent_result", "include_action_summary": True},
    },
    "commerce": {
        "schema_version": "2.0",
        "planning": {"mode": "adaptive", "todo_enabled": True, "max_plan_steps": 8},
        "budgets": {
            "timeout_seconds": 120,
            "max_model_calls": 12,
            "max_tool_calls": 16,
            "max_calls_per_tool": 4,
            "max_parallel_tools": 3,
            "max_output_tokens": 4000,
            "max_cost_usd": 0.50,
        },
        "guardrails": {
            "fail_mode": "closed",
            "input": {
                "max_chars": 30_000,
                "prompt_injection_detection": True,
                "content_policy": "customer_service",
                "pii": {"enabled": True, "strategy": "mask_for_model"},
            },
            "tool": {
                "default_action": "deny",
                "block_private_network_egress": True,
                "enforce_resource_ownership": True,
                "max_result_chars": 20_000,
            },
            "output": {
                "pii_scan": True,
                "content_policy": "customer_service",
                "require_action_evidence": True,
            },
        },
        "approval_policy": {
            "mode": "risk_based",
            "default": "ask",
            "rules": [
                {"match": {"risk": "compute"}, "decision": "allow"},
                {"match": {"risk": "public_read"}, "decision": "allow"},
                {"match": {"risk": "read"}, "decision": "allow"},
                {"match": {"risk": "read_sensitive"}, "decision": "allow"},
                {"match": {"risk": "reversible_write"}, "decision": "ask"},
                {"match": {"risk": "external_communication"}, "decision": "ask"},
                {"match": {"risk": "high_impact_write"}, "decision": "ask"},
                {
                    "match": {"risk": "financial"},
                    "decision": "ask",
                    "allowed_decisions": ["approve", "edit", "reject"],
                },
                {"match": {"risk": "destructive"}, "decision": "deny"},
            ],
        },
        "memory": {
            "short_term": {
                "strategy": "summarize",
                "recent_messages": 20,
                "summarize_at_tokens": 30_000,
            }
        },
        "response": {"format": "agent_result", "include_action_summary": True},
    },
    "analyst": {
        "schema_version": "2.0",
        "planning": {"mode": "todo", "todo_enabled": True, "max_plan_steps": 12},
        "budgets": {
            "timeout_seconds": 300,
            "max_model_calls": 20,
            "max_tool_calls": 30,
            "max_parallel_tools": 5,
            "max_output_tokens": 8000,
        },
        "guardrails": {"fail_mode": "closed", "tool": {"default_action": "deny"}},
        "approval_policy": {
            "mode": "risk_based",
            "default": "deny",
            "rules": [
                {"match": {"risk": "compute"}, "decision": "allow"},
                {"match": {"risk": "public_read"}, "decision": "allow"},
                {"match": {"risk": "read"}, "decision": "allow"},
                {"match": {"risk": "read_sensitive"}, "decision": "allow"},
            ],
        },
        "response": {"format": "agent_result", "include_citations": True},
    },
    "deep_ops": {
        "schema_version": "2.0",
        "planning": {"mode": "deep", "todo_enabled": True, "max_plan_steps": 20},
        "budgets": {
            "timeout_seconds": 600,
            "max_model_calls": 30,
            "max_tool_calls": 50,
            "max_parallel_tools": 5,
            "max_output_tokens": 12_000,
        },
        "guardrails": {"fail_mode": "closed", "tool": {"default_action": "deny"}},
        "approval_policy": {
            "mode": "risk_based",
            "default": "ask",
            "rules": [
                {"match": {"risk": "compute"}, "decision": "allow"},
                {"match": {"risk": "public_read"}, "decision": "allow"},
                {"match": {"risk": "read"}, "decision": "allow"},
                {"match": {"risk": "read_sensitive"}, "decision": "allow"},
                {"match": {"risk": "destructive"}, "decision": "deny"},
            ],
        },
        "delegation": {"enabled": True, "max_subagents": 3, "max_depth": 1},
        "memory": {
            "short_term": {
                "strategy": "summarize",
                "recent_messages": 30,
                "summarize_at_tokens": 40_000,
            },
            "artifacts": {"enabled": True, "max_inline_chars": 12_000},
        },
        "response": {"format": "agent_result", "include_citations": True},
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge dictionaries; lists/scalars are replaced explicitly."""

    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def available_profiles() -> tuple[str, ...]:
    return tuple(_PROFILES)


def expand_profile(config: dict[str, Any]) -> dict[str, Any]:
    """Expand a user node config over a known server-side profile."""

    profile = str(config.get("profile", "legacy"))
    if profile == "custom":
        # Custom starts from safe_chat, never from unrestricted legacy.
        base = _PROFILES["safe_chat"]
    else:
        if profile not in _PROFILES:
            raise ValueError(
                f"Unknown agent profile '{profile}'. Available: {', '.join(available_profiles())}, custom"
            )
        base = _PROFILES[profile]

    expanded = deep_merge(base, config)
    expanded["profile"] = profile
    expanded.setdefault("observability", {})
    expanded["observability"].setdefault("tags", [])
    expanded["observability"]["tags"] = list(
        dict.fromkeys(
            [
                *expanded["observability"]["tags"],
                f"profile:{profile}",
                f"profiles:{PROFILE_VERSION}",
            ]
        )
    )
    return expanded
