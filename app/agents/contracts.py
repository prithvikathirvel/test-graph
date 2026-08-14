"""Typed contracts for the Autonomous ReAct Agent v2 harness.

All new fields have conservative defaults and models allow unknown fields so a
newer UI can talk to an older engine during rolling deployments.  Legacy node
JSON is normalized into these contracts by :func:`build_agent_spec`.
"""

from __future__ import annotations

import ast
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RiskLevel = Literal[
    "compute",
    "public_read",
    "read",
    "read_sensitive",
    "reversible_write",
    "external_communication",
    "high_impact_write",
    "financial",
    "destructive",
]

AgentStatus = Literal[
    "COMPLETED",
    "PARTIAL",
    "NEEDS_INPUT",
    "PENDING_APPROVAL",
    "ESCALATED",
    "FAILED",
    "CANCELLED",
]


class ForwardCompatibleModel(BaseModel):
    """Base model used for version-tolerant flow configuration."""

    model_config = ConfigDict(extra="allow")


class BudgetSpec(ForwardCompatibleModel):
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_model_calls: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=16, ge=0, le=200)
    max_calls_per_tool: int = Field(default=4, ge=1, le=50)
    max_parallel_tools: int = Field(default=3, ge=1, le=20)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int = Field(default=4000, ge=1, le=100_000)
    max_cost_usd: float | None = Field(default=None, ge=0)
    on_limit: Literal["return_partial", "escalate", "error"] = "return_partial"


class PlanningSpec(ForwardCompatibleModel):
    mode: Literal["direct", "adaptive", "todo", "deep"] = "adaptive"
    todo_enabled: bool = False
    replan_on_tool_error: bool = True
    max_plan_steps: int = Field(default=8, ge=1, le=50)
    max_replans: int = Field(default=2, ge=0, le=10)
    parallel_read_tools: bool = True
    parallel_write_tools: bool = False


class ShortTermMemorySpec(ForwardCompatibleModel):
    enabled: bool = True
    strategy: Literal["window", "summarize"] = "window"
    recent_messages: int = Field(default=20, ge=0, le=200)
    summarize_at_tokens: int = Field(default=30_000, ge=1000)


class LongTermMemorySpec(ForwardCompatibleModel):
    enabled: bool = False
    namespace: str = "tenant/{{TENANT_ID}}/user/{{USER_ID}}"
    retrieve_top_k: int = Field(default=5, ge=0, le=50)
    write_policy: Literal["disabled", "validated_facts_only", "explicit_user_facts"] = (
        "validated_facts_only"
    )
    require_provenance: bool = True
    ttl_days: int | None = Field(default=365, ge=1)


class ArtifactSpec(ForwardCompatibleModel):
    enabled: bool = False
    max_inline_chars: int = Field(default=12_000, ge=1000, le=1_000_000)


class MemorySpec(ForwardCompatibleModel):
    short_term: ShortTermMemorySpec = Field(default_factory=ShortTermMemorySpec)
    long_term: LongTermMemorySpec = Field(default_factory=LongTermMemorySpec)
    artifacts: ArtifactSpec = Field(default_factory=ArtifactSpec)


class PIISpec(ForwardCompatibleModel):
    enabled: bool = False
    strategy: Literal["mask_for_model", "redact", "block", "none"] = "mask_for_model"
    types: list[str] = Field(
        default_factory=lambda: ["credit_card", "api_key", "password"]
    )


class InputGuardrailSpec(ForwardCompatibleModel):
    max_chars: int = Field(default=30_000, ge=1, le=2_000_000)
    prompt_injection_detection: bool = False
    content_policy: str = "default"
    pii: PIISpec = Field(default_factory=PIISpec)


class ToolGuardrailSpec(ForwardCompatibleModel):
    default_action: Literal["allow", "ask", "deny"] = "deny"
    validate_input_schema: bool = True
    validate_output_schema: bool = True
    sanitize_untrusted_output: bool = True
    enforce_resource_ownership: bool = True
    block_private_network_egress: bool = True
    max_result_chars: int = Field(default=20_000, ge=100, le=2_000_000)


class OutputGuardrailSpec(ForwardCompatibleModel):
    pii_scan: bool = True
    content_policy: str = "default"
    require_action_evidence: bool = True
    grounding_required_for: list[str] = Field(default_factory=list)


class GuardrailsSpec(ForwardCompatibleModel):
    fail_mode: Literal["open", "closed"] = "closed"
    input: InputGuardrailSpec = Field(default_factory=InputGuardrailSpec)
    tool: ToolGuardrailSpec = Field(default_factory=ToolGuardrailSpec)
    output: OutputGuardrailSpec = Field(default_factory=OutputGuardrailSpec)


class ApprovalRule(ForwardCompatibleModel):
    match: dict[str, Any] = Field(default_factory=dict)
    decision: Literal["allow", "ask", "deny"] = "deny"
    conditions: dict[str, Any] = Field(default_factory=dict)
    allowed_decisions: list[Literal["approve", "edit", "reject"]] = Field(
        default_factory=lambda: ["approve", "reject"]
    )


class ApprovalPolicySpec(ForwardCompatibleModel):
    mode: Literal["disabled", "risk_based"] = "risk_based"
    default: Literal["allow", "ask", "deny"] = "deny"
    rules: list[ApprovalRule] = Field(default_factory=list)
    approval_timeout_seconds: int = Field(default=86_400, ge=30, le=2_592_000)


class RetrySpec(ForwardCompatibleModel):
    max_attempts: int = Field(default=2, ge=1, le=10)
    backoff: Literal["none", "fixed", "exponential", "exponential_jitter"] = (
        "exponential_jitter"
    )
    retry_on: list[str] = Field(
        default_factory=lambda: ["timeout", "rate_limit", "server_error"]
    )
    never_retry_on: list[str] = Field(
        default_factory=lambda: ["validation", "authorization", "policy_denied"]
    )


class CircuitBreakerSpec(ForwardCompatibleModel):
    enabled: bool = True
    failure_threshold: int = Field(default=5, ge=1, le=100)
    reset_seconds: int = Field(default=60, ge=1, le=86_400)


class IdempotencySpec(ForwardCompatibleModel):
    required_for_side_effects: bool = True
    scope: str = "tenant_thread_plan_step"


class ReliabilitySpec(ForwardCompatibleModel):
    model_retry: RetrySpec = Field(default_factory=RetrySpec)
    tool_retry: RetrySpec = Field(default_factory=lambda: RetrySpec(max_attempts=3))
    circuit_breaker: CircuitBreakerSpec = Field(default_factory=CircuitBreakerSpec)
    idempotency: IdempotencySpec = Field(default_factory=IdempotencySpec)


class DelegationSpec(ForwardCompatibleModel):
    enabled: bool = False
    max_subagents: int = Field(default=3, ge=0, le=10)
    max_depth: int = Field(default=1, ge=0, le=3)
    allowed_subagents: list[str] = Field(default_factory=list)
    share_context: Literal["none", "task_only", "selected", "all"] = "task_only"


class ToolExecutionSpec(ForwardCompatibleModel):
    timeout_seconds: int = Field(default=30, ge=1, le=3600)
    max_attempts: int = Field(default=2, ge=1, le=10)
    cache_ttl_seconds: int = Field(default=0, ge=0, le=86_400)
    idempotency_required: bool = False
    idempotency_argument: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)


class ResourcePolicySpec(ForwardCompatibleModel):
    resource_type: str | None = None
    owner_argument: str | None = None
    owner_source: str = "runtime.user_id"


class ToolSpec(ForwardCompatibleModel):
    id: str | None = None
    name: str
    description: str = "No description provided."
    adapter: Literal["node_registry", "mcp", "agent_flow", "a2a", "http"] = (
        "node_registry"
    )
    node_type: str = "API caller"
    risk: RiskLevel = "compute"
    enabled: bool = True
    required_scopes: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    resource_policy: ResourcePolicySpec | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    execution: ToolExecutionSpec = Field(default_factory=ToolExecutionSpec)

    @model_validator(mode="after")
    def normalize_adapter(self) -> ToolSpec:
        if self.node_type == "MCP Tool":
            self.adapter = "mcp"
        if not self.id:
            self.id = self.name
        return self


class ResponseSpec(ForwardCompatibleModel):
    format: Literal["text", "agent_result"] = "text"
    include_citations: bool = False
    include_action_summary: bool = True
    include_debug: bool = False
    max_answer_chars: int = Field(default=12_000, ge=100, le=1_000_000)


class ObservabilitySpec(ForwardCompatibleModel):
    trace: bool = True
    trace_content: bool = False
    metrics: bool = True
    audit_actions: bool = True
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


class AgentNodeSpec(ForwardCompatibleModel):
    schema_version: str = "1.0"
    profile: str = "legacy"
    model: str = "gemini-2.5-pro"
    fallback_models: list[str] = Field(default_factory=list)
    user_query: str = ""
    system_prompt: str = "You are an expert autonomous agent."
    success_criteria: list[str] = Field(default_factory=list)
    memory_window: int = Field(default=10, ge=0, le=200)
    planning: PlanningSpec = Field(default_factory=PlanningSpec)
    budgets: BudgetSpec = Field(default_factory=BudgetSpec)
    memory: MemorySpec = Field(default_factory=MemorySpec)
    guardrails: GuardrailsSpec = Field(default_factory=GuardrailsSpec)
    approval_policy: ApprovalPolicySpec = Field(default_factory=ApprovalPolicySpec)
    reliability: ReliabilitySpec = Field(default_factory=ReliabilitySpec)
    delegation: DelegationSpec = Field(default_factory=DelegationSpec)
    tools: list[ToolSpec] = Field(default_factory=list)
    response: ResponseSpec = Field(default_factory=ResponseSpec)
    observability: ObservabilitySpec = Field(default_factory=ObservabilitySpec)

    @property
    def is_legacy(self) -> bool:
        return self.profile == "legacy" or self.schema_version.startswith("1")


class ErrorDetail(ForwardCompatibleModel):
    code: str
    message: str
    safe_for_user: bool = True


class ToolProvenance(ForwardCompatibleModel):
    tool: str
    server: str | None = None
    request_id: str | None = None
    executed_at: str | None = None


class SideEffectRecord(ForwardCompatibleModel):
    occurred: bool | Literal["unknown"] = False
    idempotency_key: str | None = None
    external_reference: str | None = None


class ToolResult(ForwardCompatibleModel):
    ok: bool
    status: str
    data: Any = None
    error: ErrorDetail | None = None
    retryable: bool = False
    provenance: ToolProvenance
    side_effect: SideEffectRecord = Field(default_factory=SideEffectRecord)

    def for_model(self) -> dict[str, Any]:
        """Return a recognizable, machine-readable tool message envelope."""

        return {
            "_agent_tool_result": True,
            **self.model_dump(mode="json"),
            "trust": "untrusted_tool_output",
            "instructions_allowed": False,
        }


class ActionRecord(ForwardCompatibleModel):
    tool: str
    status: str
    risk: str | None = None
    external_reference: str | None = None
    request_id: str | None = None


class AgentRunSummary(ForwardCompatibleModel):
    model_calls: int = 0
    tool_calls: int = 0
    elapsed_ms: float = 0.0
    terminated_by_budget: bool = False


class AgentResult(ForwardCompatibleModel):
    status: AgentStatus = "COMPLETED"
    answer: str = ""
    task_summary: str | None = None
    actions: list[ActionRecord] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    needs_input: dict[str, Any] | None = None
    pending_approval: dict[str, Any] | None = None
    error: ErrorDetail | None = None
    run: AgentRunSummary = Field(default_factory=AgentRunSummary)


def parse_json_value(value: Any, default: Any) -> Any:
    """Parse UI values without unsafe/eager quote replacement.

    JSON is preferred.  ``ast.literal_eval`` is retained only for backwards
    compatibility with older UI payloads that serialized Python literals.
    """

    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return default
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        try:
            parsed = ast.literal_eval(stripped)
            return parsed
        except (ValueError, SyntaxError):
            return default


def normalize_agent_input(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy and v2 UI inputParameters into one dictionary."""

    data = dict(raw)
    has_v2_marker = "schema_version" in data or "profile" in data
    data.setdefault("schema_version", "2.0" if has_v2_marker else "1.0")
    data.setdefault("profile", "safe_chat" if has_v2_marker else "legacy")

    for key, default in (
        ("tools", []),
        ("fallback_models", []),
        ("success_criteria", []),
        ("planning", {}),
        ("budgets", {}),
        ("memory", {}),
        ("guardrails", {}),
        ("approval_policy", {}),
        ("reliability", {}),
        ("delegation", {}),
        ("response", {}),
        ("observability", {}),
    ):
        if key in data:
            data[key] = parse_json_value(data[key], default)

    return data
