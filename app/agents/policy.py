"""Deterministic runtime context and tool-policy evaluation."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Literal

from app.agents.contracts import AgentNodeSpec, ApprovalRule, ToolSpec

PolicyDecisionType = Literal["allow", "ask", "deny"]


@dataclass(frozen=True)
class AgentRuntimeContext:
    tenant_id: str
    user_id: str
    roles: tuple[str, ...]
    scopes: tuple[str, ...]
    session_id: str
    thread_id: str
    request_id: str
    locale: str = "en"
    environment: str = "production"
    authenticated: bool = False
    identity_verified: bool = False

    @classmethod
    def from_state(cls, state: dict[str, Any], node_id: str) -> AgentRuntimeContext:
        variables = state.get("variables", {}) or {}
        auth = state.get("auth_context") or variables.get("_auth_context", {})
        if not isinstance(auth, dict):
            auth = {}

        def _sequence(value: Any) -> tuple[str, ...]:
            if isinstance(value, str):
                return tuple(item.strip() for item in value.split(",") if item.strip())
            if isinstance(value, (list, tuple, set)):
                return tuple(str(item) for item in value)
            return ()

        user_id = str(auth.get("user_id") or state.get("user_id") or "")
        tenant_id = str(
            auth.get("tenant_id")
            or variables.get("TENANT_ID")
            or variables.get("tenant_id")
            or "default_tenant"
        )
        verified = bool(auth.get("verified", False))
        authenticated = bool(auth.get("authenticated", verified))
        turn_index = sum(
            1
            for message in state.get("messages", []) or []
            if getattr(message, "type", "") in {"human", "user"}
            or (isinstance(message, dict) and message.get("role") == "user")
        )
        default_request_id = (
            f"{state.get('thread_id', 'thread')}:{node_id}:turn-{turn_index}"
        )
        return cls(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=_sequence(auth.get("roles", variables.get("AUTH_ROLES", []))),
            scopes=_sequence(auth.get("scopes", variables.get("AUTH_SCOPES", []))),
            session_id=str(state.get("session_id") or ""),
            thread_id=str(
                state.get("thread_id") or variables.get("THREAD_ID") or "thread"
            ),
            request_id=str(variables.get("REQUEST_ID") or default_request_id),
            locale=str(auth.get("locale") or variables.get("LOCALE") or "en"),
            environment=str(
                auth.get("environment") or variables.get("ENVIRONMENT") or "production"
            ),
            authenticated=authenticated,
            identity_verified=verified,
        )


@dataclass(frozen=True)
class PolicyDecision:
    decision: PolicyDecisionType
    reason_code: str
    allowed_decisions: tuple[str, ...] = ("approve", "reject")


def _scope_matches(granted: str, required: str) -> bool:
    return fnmatch.fnmatchcase(required, granted) or fnmatch.fnmatchcase(
        granted, required
    )


def _has_required_scopes(runtime: AgentRuntimeContext, required: list[str]) -> bool:
    if not required:
        return True
    return all(
        any(_scope_matches(granted, wanted) for granted in runtime.scopes)
        for wanted in required
    )


def _rule_matches(rule: ApprovalRule, tool: ToolSpec, args: dict[str, Any]) -> bool:
    match = rule.match
    if not match:
        return False
    if "tool" in match and match["tool"] != tool.name:
        return False
    if "risk" in match and match["risk"] != tool.risk:
        return False
    if "adapter" in match and match["adapter"] != tool.adapter:
        return False

    if "amount_gt" in match:
        amount = args.get("amount", args.get("refund_amount", args.get("value", 0)))
        try:
            if float(amount) <= float(match["amount_gt"]):
                return False
        except (TypeError, ValueError):
            return False
    if "amount_lte" in match:
        amount = args.get("amount", args.get("refund_amount", args.get("value", 0)))
        try:
            if float(amount) > float(match["amount_lte"]):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _conditions_match(rule: ApprovalRule, runtime: AgentRuntimeContext) -> bool:
    conditions = rule.conditions
    if not conditions:
        return True
    if (
        "authenticated" in conditions
        and bool(conditions["authenticated"]) != runtime.authenticated
    ):
        return False
    if (
        "identity_verified" in conditions
        and bool(conditions["identity_verified"]) != runtime.identity_verified
    ):
        return False
    if "environment" in conditions and conditions["environment"] != runtime.environment:
        return False
    required_role = conditions.get("role")
    if required_role and required_role not in runtime.roles:
        return False
    return True


class ToolPolicyEngine:
    """Fail-closed policy interface, replaceable by OPA/Cedar later."""

    def __init__(self, spec: AgentNodeSpec, runtime: AgentRuntimeContext):
        self.spec = spec
        self.runtime = runtime

    def can_expose(self, tool: ToolSpec) -> bool:
        """Filter tools before model context; execution authorization still repeats."""
        if self.spec.is_legacy:
            return tool.enabled
        return tool.enabled and _has_required_scopes(self.runtime, tool.required_scopes)

    def validate_authority(
        self, tool: ToolSpec, args: dict[str, Any]
    ) -> PolicyDecision | None:
        if not tool.enabled:
            return PolicyDecision("deny", "TOOL_DISABLED")

        if not _has_required_scopes(self.runtime, tool.required_scopes):
            return PolicyDecision("deny", "MISSING_REQUIRED_SCOPE")

        resource = tool.resource_policy
        if (
            resource
            and self.spec.guardrails.tool.enforce_resource_ownership
            and resource.owner_argument
        ):
            supplied_owner = args.get(resource.owner_argument)
            if (
                supplied_owner is not None
                and str(supplied_owner) != self.runtime.user_id
            ):
                return PolicyDecision("deny", "RESOURCE_OWNERSHIP_MISMATCH")

        return None

    def evaluate(self, tool: ToolSpec, args: dict[str, Any]) -> PolicyDecision:
        authority_failure = self.validate_authority(tool, args)
        if authority_failure:
            return authority_failure

        if self.spec.is_legacy or self.spec.approval_policy.mode == "disabled":
            return PolicyDecision("allow", "LEGACY_COMPATIBILITY")

        for rule in self.spec.approval_policy.rules:
            if _rule_matches(rule, tool, args) and _conditions_match(
                rule, self.runtime
            ):
                return PolicyDecision(
                    rule.decision,
                    f"MATCHED_RULE_{tool.risk.upper()}",
                    tuple(rule.allowed_decisions),
                )

        default = self.spec.approval_policy.default
        # The tool guardrail can only make a default stricter, never more permissive.
        tool_default = self.spec.guardrails.tool.default_action
        ordering = {"allow": 0, "ask": 1, "deny": 2}
        strictest = max((default, tool_default), key=lambda item: ordering[item])
        return PolicyDecision(strictest, "DEFAULT_TOOL_POLICY")
