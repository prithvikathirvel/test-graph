"""Small dependency-free redaction helpers for logs, traces, and tool envelopes."""

from __future__ import annotations

import json
import re
from typing import Any

_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "client_secret",
    "private_key",
    "smtp_password",
}

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)bearer\s+[a-z0-9._~+\-/]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bSG\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\blsv2_[A-Za-z0-9_\-]{12,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[REDACTED_PAYMENT_CARD]"),
    (
        re.compile(r"(?i)(password|passwd|api[_-]?key|secret)\s*[:=]\s*([^\s,;]+)"),
        r"\1=[REDACTED]",
    ),
)


def redact_text(value: str) -> str:
    result = value
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_data(value: Any) -> Any:
    """Recursively redact sensitive dictionary fields and string patterns."""

    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or any(
                marker in normalized
                for marker in ("password", "secret", "authorization", "private_key")
            ):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_data(item)
        return redacted
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def safe_log_value(value: Any, max_chars: int = 1000) -> str:
    """Serialize, redact, and cap a value before putting it in a log line."""

    clean = redact_data(value)
    if isinstance(clean, str):
        text = clean
    else:
        try:
            text = json.dumps(clean, default=str, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(clean)
    if len(text) > max_chars:
        return f"{text[:max_chars]}… [truncated {len(text) - max_chars} chars]"
    return text


def mask_for_model(text: str, pii_types: list[str]) -> str:
    """Mask high-risk values before model input/output.

    This deterministic layer intentionally focuses on common secrets/payment
    data.  Deployments needing broad PII support can replace/augment it with
    Presidio through the same harness boundary.
    """

    result = text
    wanted = {item.lower() for item in pii_types}
    if "credit_card" in wanted:
        result = re.sub(r"\b(?:\d[ -]*?){13,19}\b", "[PAYMENT_CARD]", result)
    if "api_key" in wanted:
        result = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[API_KEY]", result)
        result = re.sub(r"\bSG\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[API_KEY]", result)
    if "password" in wanted:
        result = re.sub(
            r"(?i)(password|passwd)\s*[:=]\s*([^\s,;]+)", r"\1=[PASSWORD]", result
        )
    if "email" in wanted:
        result = re.sub(
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", result
        )
    if "phone" in wanted:
        result = re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)", "[PHONE]", result)
    return result
