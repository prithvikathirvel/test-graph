"""Optional fail-closed API-key boundary and trusted runtime identity hook.

Authentication is disabled by default for deployment compatibility. Production
operators can enable it with ``AUTH_REQUIRED=true``. The middleware deliberately
sources scopes/roles from server configuration, never from request headers.
OIDC/JWT gateways can instead set the same ``request.state.auth_context`` shape.
"""

from __future__ import annotations

import fnmatch
import hmac
from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse

from app.core.config import settings

_PUBLIC_PATHS = {"/health", "/openapi.json", "/docs", "/redoc"}
_ADMIN_PREFIXES = (
    "/api/logs",
    "/logs",
    "/system/",
    "/nodes/test",
    "/dynamic-flow",
    "/mcp/servers",
    "/mcp/flows",
)


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    status_code: int = 200
    code: str = "OK"
    message: str = ""
    context: dict[str, Any] | None = None


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _required_scope(path: str, method: str) -> str | None:
    if path in _PUBLIC_PATHS or path.startswith(("/docs/", "/redoc/")):
        return None
    if path.startswith(_ADMIN_PREFIXES):
        return "agent:admin"
    if path.startswith(("/agents/", "/nodes")):
        return "agent:invoke"
    if path.startswith(("/sse", "/messages", "/mcp/")):
        return "mcp:access"
    return "agent:access"


def _has_scope(granted: tuple[str, ...], required: str) -> bool:
    return any(
        fnmatch.fnmatchcase(required, item) or fnmatch.fnmatchcase(item, required)
        for item in granted
    )


def authorize_request(path: str, method: str, headers: dict[str, str]) -> AuthDecision:
    """Authorize one HTTP request and construct immutable trusted context."""

    if path == "/engine":
        path = "/"
    elif path.startswith("/engine/"):
        path = path[len("/engine") :]

    if not settings.AUTH_REQUIRED:
        return AuthDecision(
            allowed=True,
            context={"authenticated": False, "verified": False},
        )

    required = _required_scope(path, method)
    if required is None:
        return AuthDecision(
            allowed=True, context={"authenticated": False, "verified": False}
        )

    configured_key = settings.AGENT_API_KEY
    supplied_key = headers.get("x-api-key", "")
    if not configured_key or not hmac.compare_digest(supplied_key, configured_key):
        return AuthDecision(
            allowed=False,
            status_code=401,
            code="AUTHENTICATION_REQUIRED",
            message="A valid API key is required.",
        )

    scopes = _csv(settings.AGENT_API_SCOPES)
    if not _has_scope(scopes, required):
        return AuthDecision(
            allowed=False,
            status_code=403,
            code="INSUFFICIENT_SCOPE",
            message=f"The authenticated service is missing scope '{required}'.",
        )

    # A fixed configured tenant is safest for a shared service API key. Header
    # tenant/user values are accepted only after key verification and are meant
    # for a trusted upstream gateway, not direct untrusted browser clients.
    tenant_id = settings.AGENT_API_TENANT_ID or headers.get(
        "x-tenant-id", "default_tenant"
    )
    user_id = headers.get("x-user-id", settings.AGENT_API_USER_ID or "service")
    return AuthDecision(
        allowed=True,
        context={
            "authenticated": True,
            "verified": True,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "roles": list(_csv(settings.AGENT_API_ROLES)),
            "scopes": list(scopes),
            "auth_method": "api_key",
        },
    )


class APIKeyAuthMiddleware:
    """Pure ASGI middleware so MCP/SSE streaming remains unaffected."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        decision = authorize_request(
            str(scope.get("path", "")), str(scope.get("method", "GET")), headers
        )
        if not decision.allowed:
            response = JSONResponse(
                status_code=decision.status_code,
                content={
                    "error": {
                        "code": decision.code,
                        "message": decision.message,
                    }
                },
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["auth_context"] = decision.context or {}
        await self.app(scope, receive, send)
