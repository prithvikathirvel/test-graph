import time

import pytest
from pydantic import ValidationError

from app.agents.schemas import json_schema_to_pydantic
from app.agents.tool_catalog import _normalize_mcp_result
from app.core.redaction import redact_data, safe_log_value
from app.engine.cache import GraphCache


def test_nested_json_schema_is_enforced():
    model = json_schema_to_pydantic(
        {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "minLength": 2},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string"},
                            "quantity": {"type": "integer"},
                        },
                        "required": ["sku", "quantity"],
                        "additionalProperties": False,
                    },
                },
                "reason": {"type": "string", "enum": ["damaged", "late"]},
            },
            "required": ["order_id", "items", "reason"],
            "additionalProperties": False,
        },
        "CancelOrder",
    )

    value = model.model_validate(
        {
            "order_id": "O-1",
            "items": [{"sku": "SKU-1", "quantity": 1}],
            "reason": "late",
        }
    )
    assert value.items[0].quantity == 1

    with pytest.raises(ValidationError):
        model.model_validate(
            {
                "order_id": "O-1",
                "items": [{"sku": "SKU-1", "quantity": 1}],
                "reason": "invented",
            }
        )


def test_v2_mcp_result_unwraps_structured_text():
    result = _normalize_mcp_result(
        {
            "success": True,
            "data": [
                {
                    "type": "text",
                    "text": '{"ticket_id":"T-1","status":"open"}',
                }
            ],
        }
    )

    assert result == {"ticket_id": "T-1", "status": "open"}


def test_redaction_covers_nested_secrets_and_caps_logs():
    clean = redact_data(
        {
            "headers": {"Authorization": "Bearer very-secret-token"},
            "password": "do-not-log",
            "message": "card 4111 1111 1111 1111",
        }
    )
    rendered = safe_log_value(clean, max_chars=1000)

    assert "very-secret-token" not in rendered
    assert "do-not-log" not in rendered
    assert "4111 1111 1111 1111" not in rendered
    assert "REDACTED" in rendered
    assert len(safe_log_value("x" * 500, max_chars=50)) < 100


def test_graph_cache_ttl_and_schema_hash():
    GraphCache.clear()
    graph = object()
    entry = GraphCache.set("agent-1", graph, schema={"version": 1}, ttl_seconds=10)

    assert GraphCache.get("agent-1") is graph
    assert GraphCache.get_entry("agent-1").schema_hash == entry.schema_hash
    assert entry.schema_hash != GraphCache.schema_hash({"version": 2})

    # Avoid sleeping in the test: force expiration.
    entry.expires_at = time.monotonic() - 1
    assert GraphCache.get("agent-1") is None
