"""Dependency-light JSON Schema to Pydantic conversion for agent tools."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, create_model


class EmptyToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _safe_model_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"Schema_{cleaned}"
    return cleaned[:100]


def _schema_type(schema: dict[str, Any], name: str) -> Any:
    if not isinstance(schema, dict):
        return Any

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        try:
            return Literal.__getitem__(tuple(enum))
        except Exception:
            return Any

    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list) and variants:
        non_null = [item for item in variants if item.get("type") != "null"]
        nullable = len(non_null) != len(variants)
        types = tuple(
            _schema_type(item, f"{name}Variant{i}") for i, item in enumerate(non_null)
        )
        if not types:
            return Any
        result = types[0] if len(types) == 1 else Union[types]  # type: ignore[arg-type]
        return Optional[result] if nullable else result

    schema_type = schema.get("type", "string")
    if isinstance(schema_type, list):
        nullable = "null" in schema_type
        actual = next((item for item in schema_type if item != "null"), "string")
        result = _schema_type({**schema, "type": actual}, name)
        return Optional[result] if nullable else result
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        item_type = _schema_type(schema.get("items", {}), f"{name}Item")
        return list[item_type]
    if schema_type == "object":
        properties = schema.get("properties")
        if isinstance(properties, dict) and properties:
            return json_schema_to_pydantic(schema, name)
        return dict[str, Any]
    return Any


def json_schema_to_pydantic(schema: dict[str, Any], name: str) -> type[BaseModel]:
    """Convert nested JSON Schema primitives into a strict Pydantic model.

    Remote ``$ref`` resolution is intentionally excluded from this trust
    boundary; MCP/API providers should return a dereferenced schema.
    """

    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
    if not properties:
        return create_model(_safe_model_name(name), __base__=EmptyToolInput)

    fields: dict[str, tuple[Any, Any]] = {}
    for key, details in properties.items():
        if not str(key).isidentifier():
            raise ValueError(f"Tool schema property '{key}' is not a valid identifier")
        details = details if isinstance(details, dict) else {}
        py_type = _schema_type(details, f"{name}_{key}")
        field_kwargs: dict[str, Any] = {"description": details.get("description", "")}
        for source, target in (
            ("minLength", "min_length"),
            ("maxLength", "max_length"),
            ("minimum", "ge"),
            ("maximum", "le"),
            ("pattern", "pattern"),
        ):
            if source in details:
                field_kwargs[target] = details[source]

        if key in required:
            fields[key] = (py_type, Field(..., **field_kwargs))
        else:
            fields[key] = (
                Optional[py_type],
                Field(default=details.get("default", None), **field_kwargs),
            )

    config = ConfigDict(
        extra="forbid" if schema.get("additionalProperties") is False else "ignore"
    )
    return create_model(_safe_model_name(name), __config__=config, **fields)
