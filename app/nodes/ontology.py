"""Ontology Nodes: Vocabulary Extractor + Canonical Resolver"""
import asyncio
import json
import logging

from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

gliner2_model = None


def load_gliner2_model():
    """Call from FastAPI lifespan to load model once at startup."""
    global gliner2_model
    from gliner2 import GLiNER2
    logger.info("⏳ Loading GLiNER2 model...")
    gliner2_model = GLiNER2.from_pretrained("fastino/gliner2-base-v1")
    logger.info("✅ GLiNER2 model loaded.")


def _to_dict(val):
    if isinstance(val, str):
        return json.loads(val)
    return val


def _strip_empty(obj):
    if isinstance(obj, dict):
        return {k: _strip_empty(v) for k, v in obj.items() if v not in (None, "", [], {})}
    if isinstance(obj, list):
        return [_strip_empty(i) for i in obj if i not in (None, "", [], {})]
    return obj


def _normalize_schema(entity_schema) -> dict:
    """Normalize entity_schema into GLiNER2's expected format: {"parent": [specs...]}."""
    if isinstance(entity_schema, list):
        return {"query": entity_schema}
    if isinstance(entity_schema, dict):
        if any(isinstance(v, list) for v in entity_schema.values()):
            return entity_schema
        return {"query": [f"{k}::str::{v}" for k, v in entity_schema.items()]}
    raise ValueError(f"entity_schema must be a list or dict, got {type(entity_schema).__name__}")


@NodeRegistry.register("Vocabulary Extractor")
async def vocabulary_extraction_node(state: FlowState, node_config: dict) -> dict:
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    user_query = resolve_placeholders(inputs.get("user_query", ""), state["variables"])
    entity_schema = resolve_placeholders(inputs.get("entity_schema", []), state["variables"])

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "extracted_entities"

    if not user_query or not user_query.strip():
        logger.warning("Vocabulary Extractor: empty user_query, skipping extraction")
        return {"variables": {output_key: {}}}

    entity_schema = _to_dict(entity_schema)

    if not entity_schema:
        logger.warning("Vocabulary Extractor: empty entity_schema, skipping extraction")
        return {"variables": {output_key: {}}}

    if gliner2_model is None:
        logger.error("Vocabulary Extractor: GLiNER2 model not loaded")
        return {"variables": {output_key: {}}}

    schema_dict = _normalize_schema(entity_schema)

    result = await asyncio.to_thread(gliner2_model.extract_json, user_query, schema_dict)

    if len(schema_dict) == 1:
        parent_key = next(iter(schema_dict))
        result = result.get(parent_key, result)

    compact = _strip_empty(result)
    logger.info(f"✅ Extracted {len(compact)} field(s) from: '{user_query[:60]}'")
    return {"variables": {output_key: compact}}


@NodeRegistry.register("Canonical Resolver")
async def canonical_term_resolution_node(state: FlowState, node_config: dict) -> dict:
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    user_data = resolve_placeholders(inputs.get("user_data", {}), state["variables"])
    dictionary = resolve_placeholders(inputs.get("dictionary", {}), state["variables"])
    threshold = float(inputs.get("threshold", 80))

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "canonical_entities"

    user_data = _to_dict(user_data)
    dictionary = _to_dict(dictionary)

    # Handle list input (e.g. from Vocabulary Extractor which returns a list of dicts)
    if isinstance(user_data, list):
        if len(user_data) == 0:
            logger.warning("Canonical Resolver: user_data list is empty, skipping")
            return {"variables": {output_key: {}}}
        user_data = user_data[0] if len(user_data) == 1 else {k: v for d in user_data for k, v in d.items()}

    if not isinstance(user_data, dict) or not user_data:
        logger.warning("Canonical Resolver: user_data is empty or not a dict, skipping")
        return {"variables": {output_key: {}}}

    if not isinstance(dictionary, dict) or not dictionary:
        logger.warning("Canonical Resolver: dictionary is empty or not a dict, returning raw data")
        return {"variables": {output_key: user_data}}

    from rapidfuzz import fuzz, process

    resolved = dict(user_data)
    for field, raw_value in user_data.items():
        candidates = dictionary.get(field)
        if not candidates or not isinstance(raw_value, str):
            continue
        match = process.extractOne(raw_value, candidates, scorer=fuzz.WRatio, score_cutoff=threshold)
        if match:
            resolved[field] = match[0]

    logger.info(f"✅ Canonical resolution done for {len(resolved)} field(s)")
    return {"variables": {output_key: resolved}}


# ─── Cypher Query Builder ───────────────────────────────────────────────────────

# Default mapping: filter_key → (relationship_type, target_label, property_name)
# Used when no schema_topology is provided or for known property-only fields
DEFAULT_RELATIONSHIP_MAP = {
    "brand":       ("MADE_BY",        "Brand",        "name"),
    "category":    ("BELONGS_TO",     "Category",     "name"),
    "color":       ("AVAILABLE_IN",   "Color",        "name"),
    "gender":      ("TARGETS",        "Gender",       "name"),
    "material":    ("MADE_OF",        "Material",     "name"),
    "occasion":    ("SUITABLE_FOR",   "Occasion",     "name"),
    "fit":         ("HAS_FIT",        "Fit",          "name"),
    "pattern":     ("HAS_PATTERN",    "Pattern",      "name"),
    "season":      ("BEST_FOR",       "Season",       "name"),
    "subcategory": ("PART_OF",        "SubCategory",  "name"),
}

# Fields that map to WHERE clauses on Product properties (not relationships)
PROPERTY_FILTERS = {"price_min", "price_max", "price_operator", "size", "age_group", "usage"}

# Fields to skip (metadata, not query-relevant)
SKIP_FIELDS = {"price_operator", "age_group", "usage"}


def _derive_mapping_from_topology(topology: dict) -> dict:
    """Auto-derive filter_key → (rel_type, target_label, property) from schema topology."""
    mapping = {}
    for rel_type, info in topology.items():
        to_labels = info.get("to_labels", [])
        if not to_labels:
            continue
        target_label = to_labels[0]
        # Derive filter key from target label (e.g. "Brand" → "brand", "SubCategory" → "subcategory")
        filter_key = target_label.lower().replace(" ", "_")
        mapping[filter_key] = (rel_type, target_label, "name")
    return mapping


def _build_where_clauses(filters: dict, params: dict) -> list:
    """Build WHERE clause fragments from price/size/property filters."""
    clauses = []
    price_op = filters.get("price_operator", "lte")
    price_min = filters.get("price_min")
    price_max = filters.get("price_max")

    if price_min and str(price_min) != "0":
        params["price_min"] = int(price_min)
        if price_op == "gte" or price_op == "between" or price_min:
            clauses.append("p.price >= $price_min")

    if price_max and str(price_max) != "0":
        params["price_max"] = int(price_max)
        if price_op in ("lt", "lte", "between") or price_max:
            op = "<" if price_op == "lt" else "<="
            clauses.append(f"p.price {op} $price_max")

    if price_op == "eq" and price_max and str(price_max) != "0":
        params["price_exact"] = int(price_max)
        clauses.clear()
        clauses.append("p.price = $price_exact")

    if price_op == "gt" and price_min and str(price_min) != "0":
        clauses.clear()
        params["price_min"] = int(price_min)
        clauses.append("p.price > $price_min")

    size = filters.get("size")
    if size:
        params["size"] = size
        clauses.append("p.size = $size")

    return clauses


def _get_relationship_filters(filters: dict, mapping: dict) -> dict:
    """Extract only the filters that map to graph relationships."""
    rel_filters = {}
    for key, value in filters.items():
        if key in PROPERTY_FILTERS or key in SKIP_FIELDS:
            continue
        if key in mapping and value:
            rel_filters[key] = value
    return rel_filters


def _build_strict_query(filters: dict, mapping: dict, limit: int, return_fields: list) -> dict:
    """All relationship filters as required MATCH patterns (AND logic)."""
    params = {}
    match_parts = ["(p:Product)"]
    rel_filters = _get_relationship_filters(filters, mapping)

    for i, (key, value) in enumerate(rel_filters.items()):
        rel_type, target_label, prop = mapping[key]
        alias = f"n{i}"
        param_key = f"{key}_val"

        if isinstance(value, list):
            params[param_key] = value
            match_parts.append(f"(p)-[:{rel_type}]->({alias}:{target_label})")
        else:
            params[param_key] = value
            match_parts.append(f"(p)-[:{rel_type}]->({alias}:{target_label} {{{prop}: ${param_key}}})")

    where_clauses = _build_where_clauses(filters, params)

    # Handle multi-value IN clauses
    for i, (key, value) in enumerate(rel_filters.items()):
        if isinstance(value, list):
            rel_type, target_label, prop = mapping[key]
            alias = f"n{i}"
            param_key = f"{key}_val"
            where_clauses.append(f"{alias}.{prop} IN ${param_key}")

    cypher = "MATCH " + ",\n      ".join(match_parts)
    if where_clauses:
        cypher += "\nWHERE " + " AND ".join(where_clauses)

    return_clause = ", ".join(f"p.{f}" for f in return_fields)
    cypher += f"\nRETURN {return_clause}\nORDER BY p.price LIMIT {limit}"

    return {"cypher": cypher, "params": params}


def _build_flexible_query(filters: dict, mapping: dict, priority_fields: list,
                          limit: int, return_fields: list) -> dict:
    """Priority fields as required MATCH, others as OPTIONAL MATCH with relevance scoring."""
    params = {}
    rel_filters = _get_relationship_filters(filters, mapping)

    required = {k: v for k, v in rel_filters.items() if k in priority_fields}
    optional = {k: v for k, v in rel_filters.items() if k not in priority_fields}

    # If no required filters, use Product as base
    match_parts = ["(p:Product)"]
    for i, (key, value) in enumerate(required.items()):
        rel_type, target_label, prop = mapping[key]
        alias = f"r{i}"
        param_key = f"{key}_val"
        if isinstance(value, list):
            params[param_key] = value
            match_parts.append(f"(p)-[:{rel_type}]->({alias}:{target_label})")
        else:
            params[param_key] = value
            match_parts.append(f"(p)-[:{rel_type}]->({alias}:{target_label} {{{prop}: ${param_key}}})")

    optional_parts = []
    score_parts = []
    for i, (key, value) in enumerate(optional.items()):
        rel_type, target_label, prop = mapping[key]
        alias = f"o{i}"
        param_key = f"{key}_val"
        if isinstance(value, list):
            params[param_key] = value
            optional_parts.append(f"OPTIONAL MATCH (p)-[:{rel_type}]->({alias}:{target_label})\n")
            score_parts.append(f"CASE WHEN {alias}.{prop} IN ${param_key} THEN 1 ELSE 0 END")
        else:
            params[param_key] = value
            optional_parts.append(f"OPTIONAL MATCH (p)-[:{rel_type}]->({alias}:{target_label} {{{prop}: ${param_key}}})\n")
            score_parts.append(f"CASE WHEN {alias} IS NOT NULL THEN 1 ELSE 0 END")

    where_clauses = _build_where_clauses(filters, params)

    # Handle multi-value IN for required filters
    for i, (key, value) in enumerate(required.items()):
        if isinstance(value, list):
            rel_type, target_label, prop = mapping[key]
            alias = f"r{i}"
            param_key = f"{key}_val"
            where_clauses.append(f"{alias}.{prop} IN ${param_key}")

    cypher = "MATCH " + ",\n      ".join(match_parts) + "\n"
    cypher += "".join(optional_parts)

    if where_clauses:
        cypher += "WHERE " + " AND ".join(where_clauses) + "\n"

    return_clause = ", ".join(f"p.{f}" for f in return_fields)
    if score_parts:
        score_expr = " + ".join(score_parts)
        cypher += f"WITH p, ({score_expr}) AS relevance\n"
        cypher += f"RETURN {return_clause}, relevance\nORDER BY relevance DESC, p.price LIMIT {limit}"
    else:
        cypher += f"RETURN {return_clause}\nORDER BY p.price LIMIT {limit}"

    return {"cypher": cypher, "params": params}


def _build_similar_query(filters: dict, mapping: dict, limit: int, return_fields: list) -> dict:
    """OR-based scoring — matches as many filters as possible, scores by hit count."""
    params = {}
    rel_filters = _get_relationship_filters(filters, mapping)

    if not rel_filters:
        return _build_exploratory_query(filters, limit, return_fields)

    cypher = "MATCH (p:Product)\n"
    score_parts = []

    for i, (key, value) in enumerate(rel_filters.items()):
        rel_type, target_label, prop = mapping[key]
        alias = f"s{i}"
        param_key = f"{key}_val"
        if isinstance(value, list):
            params[param_key] = value
            cypher += f"OPTIONAL MATCH (p)-[:{rel_type}]->({alias}:{target_label})\n"
            score_parts.append(f"CASE WHEN {alias}.{prop} IN ${param_key} THEN 1 ELSE 0 END")
        else:
            params[param_key] = value
            cypher += f"OPTIONAL MATCH (p)-[:{rel_type}]->({alias}:{target_label} {{{prop}: ${param_key}}})\n"
            score_parts.append(f"CASE WHEN {alias} IS NOT NULL THEN 1 ELSE 0 END")

    where_clauses = _build_where_clauses(filters, params)
    if where_clauses:
        cypher += "WHERE " + " AND ".join(where_clauses) + "\n"

    score_expr = " + ".join(score_parts)
    return_clause = ", ".join(f"p.{f}" for f in return_fields)
    cypher += f"WITH p, ({score_expr}) AS relevance\n"
    cypher += f"WHERE relevance > 0\n"
    cypher += f"RETURN {return_clause}, relevance\nORDER BY relevance DESC, p.price LIMIT {limit}"

    return {"cypher": cypher, "params": params}


def _build_exploratory_query(filters: dict, limit: int, return_fields: list) -> dict:
    """Minimal query — browse by basic filters or return all products."""
    params = {}
    where_clauses = _build_where_clauses(filters, params)

    cypher = "MATCH (p:Product)\n"
    if where_clauses:
        cypher += "WHERE " + " AND ".join(where_clauses) + "\n"

    return_clause = ", ".join(f"p.{f}" for f in return_fields)
    cypher += f"RETURN {return_clause}\nORDER BY p.price LIMIT {limit}"

    return {"cypher": cypher, "params": params}


def _auto_select_mode(filters: dict, mapping: dict) -> str:
    """Auto-detect the best query mode based on filter count."""
    rel_filters = _get_relationship_filters(filters, mapping)
    count = len(rel_filters)
    if count == 0:
        return "exploratory"
    if count <= 2:
        return "similar"
    return "flexible"


@NodeRegistry.register("Cypher Query Builder")
async def cypher_query_builder_node(state: FlowState, node_config: dict) -> dict:
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}

    filters = resolve_placeholders(inputs.get("filters", {}), state["variables"])
    schema_topology = resolve_placeholders(inputs.get("schema_topology", {}), state["variables"])
    mode = inputs.get("mode", "auto")
    limit = int(inputs.get("limit", 20))
    priority_fields = inputs.get("priority_fields", '["brand", "category", "color"]')
    return_fields = inputs.get("return_fields", '["name", "brand", "price", "color", "size", "sku"]')

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "graph_query"

    # Parse JSON string inputs
    filters = _to_dict(filters) if filters else {}
    schema_topology = _to_dict(schema_topology) if schema_topology else {}
    if isinstance(priority_fields, str):
        priority_fields = json.loads(priority_fields)
    if isinstance(return_fields, str):
        return_fields = json.loads(return_fields)

    # Handle list input from Canonical Resolver
    if isinstance(filters, list):
        filters = filters[0] if len(filters) == 1 else {k: v for d in filters for k, v in d.items()} if filters else {}

    if not isinstance(filters, dict):
        filters = {}

    # Build relationship mapping: auto-derive from topology or use defaults
    if schema_topology:
        mapping = _derive_mapping_from_topology(schema_topology)
        # Merge with defaults for any missing keys
        for k, v in DEFAULT_RELATIONSHIP_MAP.items():
            mapping.setdefault(k, v)
    else:
        mapping = dict(DEFAULT_RELATIONSHIP_MAP)

    # Auto-select mode if not specified
    if mode == "auto":
        mode = _auto_select_mode(filters, mapping)

    logger.info(f"🔨 Cypher Query Builder: mode={mode}, filters={len(filters)} fields, limit={limit}")

    # Build query based on mode
    if mode == "strict":
        result = _build_strict_query(filters, mapping, limit, return_fields)
    elif mode == "flexible":
        result = _build_flexible_query(filters, mapping, priority_fields, limit, return_fields)
    elif mode == "similar":
        result = _build_similar_query(filters, mapping, limit, return_fields)
    elif mode == "exploratory":
        result = _build_exploratory_query(filters, limit, return_fields)
    else:
        result = _build_flexible_query(filters, mapping, priority_fields, limit, return_fields)

    result["explanation"] = f"Generated via {mode} mode with {len(_get_relationship_filters(filters, mapping))} relationship filter(s)"

    logger.info(f"✅ Cypher Query Builder: {result['cypher'][:80]}...")
    return {"variables": {output_key: result}}
