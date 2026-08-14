"""Guarded execution gateway shared by all agent tool adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from langgraph.types import interrupt

from app.agents.contracts import (
    ActionRecord,
    AgentNodeSpec,
    ErrorDetail,
    SideEffectRecord,
    ToolProvenance,
    ToolResult,
    ToolSpec,
)
from app.agents.policy import AgentRuntimeContext, ToolPolicyEngine
from app.core.redaction import redact_data, safe_log_value
from app.core.runtime import get_runtime_services

logger = logging.getLogger(__name__)

_SIDE_EFFECT_RISKS = {
    "reversible_write",
    "external_communication",
    "high_impact_write",
    "financial",
    "destructive",
}
_TRANSIENT_CODES = {"timeout", "rate_limit", "server_error", "connection_error"}


class _CircuitRegistry:
    """Small process-local breaker; a distributed backend can replace this API."""

    _state: dict[str, dict[str, float | int]] = {}

    @classmethod
    def is_open(cls, key: str, reset_seconds: int) -> bool:
        state = cls._state.get(key)
        if not state:
            return False
        opened_at = float(state.get("opened_at", 0))
        if opened_at and time.monotonic() - opened_at < reset_seconds:
            return True
        if opened_at:
            cls._state.pop(key, None)
        return False

    @classmethod
    def success(cls, key: str) -> None:
        cls._state.pop(key, None)

    @classmethod
    def failure(cls, key: str, threshold: int) -> None:
        state = cls._state.setdefault(key, {"failures": 0, "opened_at": 0.0})
        state["failures"] = int(state["failures"]) + 1
        if int(state["failures"]) >= threshold:
            state["opened_at"] = time.monotonic()


class ActionJournal:
    """Best-effort persistent idempotency and action audit over existing MongoDB."""

    collection_name = "agent_action_journal"

    def __init__(self):
        self.mongo = get_runtime_services().mongo_client

    def _collection(self):
        if self.mongo is None:
            return None
        # Import lazily to avoid a settings import during isolated contract tests.
        from app.core.config import settings

        return self.mongo[settings.MONGO_DB_NAME][self.collection_name]

    async def get(self, key: str) -> dict[str, Any] | None:
        collection = self._collection()
        if collection is None:
            return None
        return await asyncio.to_thread(collection.find_one, {"idempotency_key": key})

    async def reserve(
        self, document: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | None]:
        collection = self._collection()
        if collection is None:
            return True, None

        def _reserve():
            existing = collection.find_one(
                {"idempotency_key": document["idempotency_key"]}
            )
            if existing:
                return False, existing
            try:
                collection.insert_one(document)
                return True, None
            except Exception:
                # Handles races even when the unique index is installed by another replica.
                existing_after_race = collection.find_one(
                    {"idempotency_key": document["idempotency_key"]}
                )
                if existing_after_race:
                    return False, existing_after_race
                raise

        return await asyncio.to_thread(_reserve)

    async def complete(self, key: str, result: ToolResult) -> None:
        collection = self._collection()
        if collection is None:
            return
        update = {
            "$set": {
                "status": result.status,
                "ok": result.ok,
                "result": redact_data(result.model_dump(mode="json")),
                "updated_at": datetime.now(timezone.utc),
            }
        }
        await asyncio.to_thread(collection.update_one, {"idempotency_key": key}, update)


class ToolGateway:
    """Enforces policy, budgets, approvals, resilience, and result contracts."""

    def __init__(
        self,
        spec: AgentNodeSpec,
        runtime: AgentRuntimeContext,
        *,
        durable_approvals: bool,
        base_variables: dict[str, Any] | None = None,
    ):
        self.spec = spec
        self.runtime = runtime
        self.policy = ToolPolicyEngine(spec, runtime)
        self.durable_approvals = durable_approvals
        self.base_variables = dict(base_variables or {})
        self.total_calls = 0
        self.per_tool_calls: Counter[str] = Counter()
        self.actions: list[ActionRecord] = []
        self._counter_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(spec.budgets.max_parallel_tools)
        self.journal = ActionJournal()

    async def execute(
        self,
        tool: ToolSpec,
        args: dict[str, Any],
        operation: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> ToolResult:
        started = time.perf_counter()
        args = dict(args)
        request_id = self._tool_request_id(tool, args)

        budget_error = await self._reserve_budget(tool, request_id)
        if budget_error:
            return budget_error

        input_error = self._validate_input(tool, args, request_id)
        if input_error:
            return self._record(tool, input_error)

        policy = self.policy.evaluate(tool, args)
        if policy.decision == "deny":
            return self._record(
                tool,
                self._failure(
                    tool,
                    request_id,
                    "POLICY_DENIED",
                    f"Tool policy denied this action ({policy.reason_code}).",
                    retryable=False,
                ),
            )

        if policy.decision == "ask":
            if not self.durable_approvals:
                return self._record(
                    tool,
                    self._failure(
                        tool,
                        request_id,
                        "APPROVAL_UNAVAILABLE",
                        "This action requires approval, but durable approval is unavailable.",
                        retryable=False,
                    ),
                )
            approval_request = {
                "type": "tool_approval",
                "request_id": request_id,
                "tool": tool.name,
                "risk": tool.risk,
                "question": self._approval_question(tool, args),
                "arguments": redact_data(args),
                "allowed_decisions": list(policy.allowed_decisions),
                "reason_code": policy.reason_code,
            }
            response = interrupt(approval_request)
            decision, edited = self._parse_approval(response, request_id)
            if decision == "reject":
                return self._record(
                    tool,
                    self._failure(
                        tool,
                        request_id,
                        "USER_REJECTED",
                        "The proposed tool action was rejected.",
                        retryable=False,
                        status="CANCELLED",
                    ),
                )
            if decision == "edit":
                if "edit" not in policy.allowed_decisions or not isinstance(
                    edited, dict
                ):
                    return self._record(
                        tool,
                        self._failure(
                            tool,
                            request_id,
                            "INVALID_APPROVAL_DECISION",
                            "Edited tool arguments were not allowed or invalid.",
                            retryable=False,
                        ),
                    )
                args = {**args, **edited}
                edited_input_error = self._validate_input(tool, args, request_id)
                if edited_input_error:
                    return self._record(tool, edited_input_error)
            elif decision != "approve":
                return self._record(
                    tool,
                    self._failure(
                        tool,
                        request_id,
                        "INVALID_APPROVAL_DECISION",
                        "Approval response must approve, edit, or reject the action.",
                        retryable=False,
                    ),
                )

            # Approval never bypasses identity/scope/ownership checks, especially after edits.
            authority_failure = self.policy.validate_authority(tool, args)
            if authority_failure:
                return self._record(
                    tool,
                    self._failure(
                        tool,
                        request_id,
                        "POLICY_DENIED",
                        f"Edited action failed policy ({authority_failure.reason_code}).",
                        retryable=False,
                    ),
                )

        egress_error = await self._validate_egress(tool, args, request_id)
        if egress_error:
            return self._record(tool, egress_error)

        breaker_key = f"{tool.adapter}:{tool.id or tool.name}"
        breaker = self.spec.reliability.circuit_breaker
        if breaker.enabled and _CircuitRegistry.is_open(
            breaker_key, breaker.reset_seconds
        ):
            return self._record(
                tool,
                self._failure(
                    tool,
                    request_id,
                    "CIRCUIT_OPEN",
                    "The tool is temporarily unavailable after repeated failures.",
                    retryable=True,
                ),
            )

        side_effect = tool.risk in _SIDE_EFFECT_RISKS
        idem_key = self._idempotency_key(tool, args) if side_effect else None
        if idem_key:
            args = self._inject_idempotency_argument(tool, args, idem_key)
            reused = await self._reuse_or_reserve(tool, args, request_id, idem_key)
            if isinstance(reused, ToolResult):
                return self._record(tool, reused)
            if reused is False:
                return self._record(
                    tool,
                    self._failure(
                        tool,
                        request_id,
                        "ACTION_IN_PROGRESS",
                        "An identical side-effecting action is already pending or has unknown status. Reconcile it before retrying.",
                        retryable=False,
                        idempotency_key=idem_key,
                    ),
                )

        attempts = max(
            1,
            min(
                tool.execution.max_attempts,
                self.spec.reliability.tool_retry.max_attempts,
            ),
        )
        # A gateway journal prevents repeated requests from this engine, but it
        # cannot prove whether an upstream side effect happened after a timeout.
        # Retry writes only when the tool explicitly accepts our stable key.
        if side_effect and not tool.execution.idempotency_argument:
            attempts = 1
        last_result: ToolResult | None = None
        async with self._semaphore:
            for attempt in range(1, attempts + 1):
                try:
                    raw = await asyncio.wait_for(
                        operation(args), timeout=tool.execution.timeout_seconds
                    )
                    result = self._normalize_raw_result(
                        tool, raw, request_id, idem_key, side_effect
                    )
                except asyncio.TimeoutError:
                    result = self._failure(
                        tool,
                        request_id,
                        "UPSTREAM_TIMEOUT",
                        "The tool did not respond within its timeout.",
                        retryable=True,
                        idempotency_key=idem_key,
                        occurred="unknown" if side_effect else False,
                    )
                except Exception as exc:
                    logger.warning(
                        "Tool '%s' attempt %s/%s failed (%s)",
                        tool.name,
                        attempt,
                        attempts,
                        type(exc).__name__,
                    )
                    result = self._failure(
                        tool,
                        request_id,
                        "TOOL_EXECUTION_ERROR",
                        "The tool failed to execute.",
                        retryable=self._exception_is_retryable(exc),
                        idempotency_key=idem_key,
                    )

                last_result = result
                if result.ok:
                    _CircuitRegistry.success(breaker_key)
                    break
                if not result.retryable or attempt >= attempts:
                    if breaker.enabled:
                        _CircuitRegistry.failure(breaker_key, breaker.failure_threshold)
                    break
                await asyncio.sleep(self._backoff_seconds(attempt))

        assert last_result is not None
        if idem_key:
            await self.journal.complete(idem_key, last_result)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            "Agent tool completed | tool=%s status=%s risk=%s attempts<=%s elapsed_ms=%s",
            tool.name,
            last_result.status,
            tool.risk,
            attempts,
            elapsed_ms,
        )
        return self._record(tool, last_result)

    async def _reserve_budget(
        self, tool: ToolSpec, request_id: str
    ) -> ToolResult | None:
        async with self._counter_lock:
            if self.total_calls >= self.spec.budgets.max_tool_calls:
                return self._failure(
                    tool,
                    request_id,
                    "TOOL_CALL_BUDGET_EXCEEDED",
                    "The agent reached its tool-call budget.",
                    retryable=False,
                    status="BUDGET_EXCEEDED",
                )
            if self.per_tool_calls[tool.name] >= self.spec.budgets.max_calls_per_tool:
                return self._failure(
                    tool,
                    request_id,
                    "PER_TOOL_BUDGET_EXCEEDED",
                    f"The agent reached the call budget for {tool.name}.",
                    retryable=False,
                    status="BUDGET_EXCEEDED",
                )
            self.total_calls += 1
            self.per_tool_calls[tool.name] += 1
        return None

    async def _reuse_or_reserve(
        self,
        tool: ToolSpec,
        args: dict[str, Any],
        request_id: str,
        idem_key: str,
    ) -> ToolResult | bool | None:
        existing = await self.journal.get(idem_key)
        if (
            existing
            and existing.get("status") == "SUCCEEDED"
            and existing.get("result")
        ):
            try:
                result = ToolResult.model_validate(existing["result"])
                result.provenance.request_id = request_id
                return result
            except Exception:
                logger.warning(
                    "Could not restore journal result for key prefix %s", idem_key[:12]
                )
        if existing and existing.get("status") in {"PENDING", "RUNNING"}:
            return False

        document = {
            "idempotency_key": idem_key,
            "tenant_id": self.runtime.tenant_id,
            "user_id": self.runtime.user_id,
            "thread_id": self.runtime.thread_id,
            "tool": tool.name,
            "risk": tool.risk,
            "arguments_hash": hashlib.sha256(
                json.dumps(args, sort_keys=True, default=str).encode()
            ).hexdigest(),
            "status": "PENDING",
            "request_id": request_id,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        reserved, raced = await self.journal.reserve(document)
        if reserved:
            return None
        if raced and raced.get("status") == "SUCCEEDED" and raced.get("result"):
            try:
                return ToolResult.model_validate(raced["result"])
            except Exception:
                pass
        return False

    def _validate_input(
        self, tool: ToolSpec, args: dict[str, Any], request_id: str
    ) -> ToolResult | None:
        if not tool.input_schema or not self.spec.guardrails.tool.validate_input_schema:
            return None
        try:
            from jsonschema import validate

            validate(instance=args, schema=tool.input_schema)
            return None
        except Exception as validation_error:
            logger.info(
                "Tool input schema validation failed | tool=%s error=%s",
                tool.name,
                type(validation_error).__name__,
            )
            return self._failure(
                tool,
                request_id,
                "INVALID_TOOL_INPUT",
                "Tool arguments did not match the required schema.",
                retryable=False,
            )

    def _normalize_raw_result(
        self,
        tool: ToolSpec,
        raw: Any,
        request_id: str,
        idempotency_key: str | None,
        side_effect: bool,
    ) -> ToolResult:
        if isinstance(raw, ToolResult):
            return raw

        failed = False
        retryable = False
        error_message = "Tool returned an error."
        if isinstance(raw, dict):
            if raw.get("success") is False or raw.get("ok") is False or "error" in raw:
                failed = True
                error_message = str(
                    raw.get("error") or raw.get("detail") or error_message
                )
                lowered = error_message.lower()
                retryable = any(
                    token in lowered
                    for token in ("timeout", "rate", "tempor", "offline", "server")
                )

        if failed:
            return self._failure(
                tool,
                request_id,
                "UPSTREAM_TOOL_ERROR",
                error_message,
                retryable=retryable,
                idempotency_key=idempotency_key,
            )

        if tool.output_schema and self.spec.guardrails.tool.validate_output_schema:
            try:
                from jsonschema import validate

                validate(instance=raw, schema=tool.output_schema)
            except Exception as validation_error:
                logger.warning(
                    "Tool output schema validation failed | tool=%s error=%s",
                    tool.name,
                    type(validation_error).__name__,
                )
                return self._failure(
                    tool,
                    request_id,
                    "INVALID_TOOL_OUTPUT",
                    "The tool returned data that did not match its output schema.",
                    retryable=True,
                    idempotency_key=idempotency_key,
                    occurred="unknown" if side_effect else False,
                )

        data = redact_data(raw)
        max_chars = self.spec.guardrails.tool.max_result_chars
        serialized = json.dumps(data, default=str, ensure_ascii=False)
        if len(serialized) > max_chars:
            data = {
                "truncated": True,
                "preview": serialized[:max_chars],
                "original_chars": len(serialized),
            }

        external_reference = self._external_reference(data)
        return ToolResult(
            ok=True,
            status="SUCCEEDED",
            data=data,
            retryable=False,
            provenance=ToolProvenance(
                tool=tool.name,
                server=str(tool.config.get("server_id"))
                if tool.config.get("server_id")
                else None,
                request_id=request_id,
                executed_at=datetime.now(timezone.utc).isoformat(),
            ),
            side_effect=SideEffectRecord(
                occurred=side_effect,
                idempotency_key=idempotency_key,
                external_reference=external_reference,
            ),
        )

    def _failure(
        self,
        tool: ToolSpec,
        request_id: str,
        code: str,
        message: str,
        *,
        retryable: bool,
        status: str = "FAILED",
        idempotency_key: str | None = None,
        occurred: bool | str = False,
    ) -> ToolResult:
        return ToolResult(
            ok=False,
            status=status,
            data=None,
            error=ErrorDetail(code=code, message=safe_log_value(message, 500)),
            retryable=retryable,
            provenance=ToolProvenance(
                tool=tool.name,
                server=str(tool.config.get("server_id"))
                if tool.config.get("server_id")
                else None,
                request_id=request_id,
                executed_at=datetime.now(timezone.utc).isoformat(),
            ),
            side_effect=SideEffectRecord(
                occurred=occurred, idempotency_key=idempotency_key
            ),
        )

    def _record(self, tool: ToolSpec, result: ToolResult) -> ToolResult:
        self.actions.append(
            ActionRecord(
                tool=tool.name,
                status=result.status,
                risk=tool.risk,
                external_reference=result.side_effect.external_reference,
                request_id=result.provenance.request_id,
            )
        )
        return result

    def _tool_request_id(self, tool: ToolSpec, args: dict[str, Any]) -> str:
        stable = {
            "tenant": self.runtime.tenant_id,
            "request": self.runtime.request_id,
            "tool": tool.id or tool.name,
            "args": args,
        }
        digest = hashlib.sha256(
            json.dumps(stable, sort_keys=True, default=str).encode()
        ).hexdigest()
        return f"tool_{digest[:24]}"

    def _idempotency_key(self, tool: ToolSpec, args: dict[str, Any]) -> str:
        stable = {
            "tenant": self.runtime.tenant_id,
            "thread": self.runtime.thread_id,
            "request": self.runtime.request_id,
            "tool": tool.id or tool.name,
            "args": args,
        }
        digest = hashlib.sha256(
            json.dumps(stable, sort_keys=True, default=str).encode()
        ).hexdigest()
        return f"agent:{digest}"

    @staticmethod
    def _inject_idempotency_argument(
        tool: ToolSpec, args: dict[str, Any], key: str
    ) -> dict[str, Any]:
        argument = tool.execution.idempotency_argument
        if argument:
            return {**args, argument: key}
        return args

    async def _validate_egress(
        self, tool: ToolSpec, args: dict[str, Any], request_id: str
    ) -> ToolResult | None:
        if tool.node_type != "API caller" and tool.adapter != "http":
            return None
        from app.utils.templating import resolve_placeholders

        resolved_config = resolve_placeholders(
            tool.config, {**self.base_variables, **args}
        )
        raw_url = args.get("url") or (
            resolved_config.get("url") if isinstance(resolved_config, dict) else None
        )
        if not raw_url:
            return None
        if "{{" in str(raw_url):
            if self.spec.is_legacy:
                return None
            return self._failure(
                tool,
                request_id,
                "UNRESOLVED_EGRESS_URL",
                "Tool URL contains unresolved values.",
                retryable=False,
            )
        parsed = urlparse(str(raw_url))
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return self._failure(
                tool,
                request_id,
                "INVALID_EGRESS_URL",
                "Tool URL is invalid.",
                retryable=False,
            )
        allowed = tool.execution.allowed_hosts
        if allowed and not any(fnmatch_host(host, pattern) for pattern in allowed):
            return self._failure(
                tool,
                request_id,
                "EGRESS_HOST_DENIED",
                "Tool URL host is not on the allowlist.",
                retryable=False,
            )
        if self.spec.guardrails.tool.block_private_network_egress:
            if is_private_hostname(host):
                return self._failure(
                    tool,
                    request_id,
                    "PRIVATE_NETWORK_EGRESS_DENIED",
                    "Private or local network egress is not allowed.",
                    retryable=False,
                )
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(host, None)
            except OSError:
                if self.spec.guardrails.fail_mode == "closed":
                    return self._failure(
                        tool,
                        request_id,
                        "EGRESS_DNS_FAILED",
                        "Tool URL host could not be resolved safely.",
                        retryable=True,
                    )
                infos = []
            for info in infos:
                resolved_ip = str(info[4][0])
                if is_private_hostname(resolved_ip):
                    return self._failure(
                        tool,
                        request_id,
                        "PRIVATE_NETWORK_EGRESS_DENIED",
                        "Tool URL resolved to a private or local network address.",
                        retryable=False,
                    )
        return None

    @staticmethod
    def _approval_question(tool: ToolSpec, args: dict[str, Any]) -> str:
        resource = args.get("order_id") or args.get("ticket_id") or args.get("id")
        suffix = f" for {resource}" if resource else ""
        return f"Approve '{tool.name}'{suffix}? Risk: {tool.risk}."

    @staticmethod
    def _parse_approval(
        response: Any, expected_request_id: str
    ) -> tuple[str, dict[str, Any] | None]:
        if isinstance(response, str):
            normalized = response.strip().lower()
            if normalized in {"approve", "approved", "yes", "y", "confirm"}:
                return "approve", None
            if normalized in {"reject", "rejected", "no", "n", "cancel"}:
                return "reject", None
            return "invalid", None
        if isinstance(response, dict):
            supplied_request_id = response.get("request_id")
            if supplied_request_id and str(supplied_request_id) != expected_request_id:
                return "invalid", None
            decision = str(
                response.get("decision") or response.get("type") or ""
            ).lower()
            edited = response.get("edited_arguments")
            return decision, edited if isinstance(edited, dict) else None
        return "invalid", None

    def _backoff_seconds(self, attempt: int) -> float:
        mode = self.spec.reliability.tool_retry.backoff
        if mode == "none":
            return 0
        if mode == "fixed":
            return 0.5
        value = min(0.5 * (2 ** (attempt - 1)), 8.0)
        return value + (
            random.uniform(0, value / 4) if mode == "exponential_jitter" else 0
        )

    @staticmethod
    def _exception_is_retryable(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        return any(token in name or token in text for token in _TRANSIENT_CODES)

    @staticmethod
    def _external_reference(data: Any) -> str | None:
        if isinstance(data, dict):
            for key in (
                "external_reference",
                "ticket_id",
                "order_id",
                "refund_id",
                "inserted_id",
                "id",
            ):
                if key in data and data[key] is not None:
                    return str(data[key])
            nested = data.get("data")
            if isinstance(nested, dict):
                return ToolGateway._external_reference(nested)
        return None


def fnmatch_host(host: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(host, pattern.lower())


def is_private_hostname(host: str) -> bool:
    """Conservative hostname/IP screen. DNS rebinding must also be checked by transport."""

    import ipaddress

    normalized = host.rstrip(".").lower()
    if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith(
        ".local"
    ):
        return True
    try:
        address = ipaddress.ip_address(normalized)
        return bool(
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        )
    except ValueError:
        # Known cloud metadata hostnames are blocked even before DNS resolution.
        return normalized in {"metadata.google.internal", "instance-data.ec2.internal"}
