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


def _to_singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _derive_mapping_from_topology(topology: dict, root_label: str) -> dict:
    """Build filter_key → (rel_type, target_label, lookup_prop) from schema topology.

    Registers plural, singular, and rel-type-derived keys so user filter keys always resolve.
    Only includes relationships where root_label appears in from_labels.
    """
    mapping: dict = {}
    for rel_type, info in topology.items():
        from_labels = info.get("from_labels", [])
        to_labels = info.get("to_labels", [])
        if not to_labels:
            continue
        # Skip relationships that don't originate from the root entity
        if from_labels and root_label not in from_labels:
            continue
        target_label = to_labels[0]
        lookup_prop = info.get("lookup_property", "name")
        entry = (rel_type, target_label, lookup_prop)

        label_key = target_label.lower().replace(" ", "_")
        rel_last = rel_type.split("_")[-1].lower()

        for key in {label_key, _to_singular(label_key), rel_last, _to_singular(rel_last)}:
            if key and len(key) > 1:
                mapping.setdefault(key, entry)
    return mapping


# Fields that are exclusively relationship-semantic; if not resolved by topology they must be dropped,
# never treated as node properties in a WHERE clause.
_REL_SEMANTIC_FIELDS: frozenset = frozenset({
    "brand", "category", "color", "gender", "material",
    "occasion", "fit", "pattern", "season", "subcategory",
    "type", "style", "collection", "tag",
})

# Fields that should use case-insensitive CONTAINS instead of exact equality.
_TEXT_SEARCH_FIELDS: frozenset = frozenset({
    "name", "description", "title", "label", "notes", "body", "summary",
})


def _build_where_clauses(filters: dict, mapping: dict, params: dict,
                          root_alias: str, skip_fields: set) -> list:
    """Build WHERE fragments: range filters for {base}_min/_max/_operator, equality for the rest."""
    clauses = []
    processed: set = set()

    # Collect range filter groups keyed by base field name
    range_bases: dict = {}
    for key in filters:
        if key in skip_fields or key in mapping:
            continue
        if key.endswith("_min"):
            range_bases.setdefault(key[:-4], {})["min"] = filters[key]
            processed.add(key)
        elif key.endswith("_max"):
            range_bases.setdefault(key[:-4], {})["max"] = filters[key]
            processed.add(key)
        elif key.endswith("_operator"):
            range_bases.setdefault(key[:-9], {})["op"] = filters[key]
            processed.add(key)

    def _cast(v):
        try:
            f = float(str(v))
            return int(f) if f == int(f) else f
        except (ValueError, TypeError):
            return v

    # Scalar operator map for non-range fields: {field}_operator without _min/_max
    _SCALAR_OPS = {
        "eq":          lambda a, p, k: (f"{a}.{k} = ${k}",          {k: p}),
        "neq":         lambda a, p, k: (f"{a}.{k} <> ${k}",         {k: p}),
        "contains":    lambda a, p, k: (f"{a}.{k} CONTAINS ${k}",   {k: p}),
        "starts_with": lambda a, p, k: (f"{a}.{k} STARTS WITH ${k}", {k: p}),
        "ends_with":   lambda a, p, k: (f"{a}.{k} ENDS WITH ${k}",  {k: p}),
        "in":          lambda a, p, k: (f"{a}.{k} IN ${k}",         {k: p if isinstance(p, list) else [p]}),
        "not_in":      lambda a, p, k: (f"NOT {a}.{k} IN ${k}",     {k: p if isinstance(p, list) else [p]}),
        "is_null":     lambda a, p, k: (f"{a}.{k} IS NULL",         {}),
        "is_not_null": lambda a, p, k: (f"{a}.{k} IS NOT NULL",     {}),
    }

    for base, vals in range_bases.items():
        op = vals.get("op", "lte")
        val_min = vals.get("min")
        val_max = vals.get("max")

        # Scalar operator — has _operator but no _min/_max
        if val_min is None and val_max is None:
            scalar_val = filters.get(base)
            processed.add(base)
            if scalar_val is None and op not in ("is_null", "is_not_null"):
                continue
            if op in _SCALAR_OPS:
                clause, extra_params = _SCALAR_OPS[op](root_alias, scalar_val, base)
                clauses.append(clause)
                params.update(extra_params)
            continue

        # Range operator — has _min and/or _max
        # price is stored as STRING in the DB so numeric comparison needs toFloat()
        lhs = f"toFloat({root_alias}.{base})" if base == "price" else f"{root_alias}.{base}"
        if op == "eq" and val_max is not None and str(val_max) != "0":
            params[f"{base}_exact"] = _cast(val_max)
            clauses.append(f"{lhs} = ${base}_exact")
        elif op == "gt" and val_min is not None and str(val_min) != "0":
            params[f"{base}_min"] = _cast(val_min)
            clauses.append(f"{lhs} > ${base}_min")
        else:
            if val_min is not None and str(val_min) != "0":
                params[f"{base}_min"] = _cast(val_min)
                clauses.append(f"{lhs} >= ${base}_min")
            if val_max is not None and str(val_max) != "0":
                params[f"{base}_max"] = _cast(val_max)
                op_sym = "<" if op == "lt" else "<="
                clauses.append(f"{lhs} {op_sym} ${base}_max")

    # Property WHERE for remaining scalar fields
    for key, value in filters.items():
        if key in processed or key in skip_fields or key in mapping:
            continue
        if key.endswith("_min") or key.endswith("_max") or key.endswith("_operator"):
            continue
        if value is None or value == "":
            continue
        # Relationship-semantic fields not resolved by topology must be skipped entirely;
        # they have no corresponding property on the base node.
        if key in _REL_SEMANTIC_FIELDS:
            continue
        if key in _TEXT_SEARCH_FIELDS:
            params[key] = str(value)
            clauses.append(f"toLower({root_alias}.{key}) CONTAINS toLower(${key})")
        elif isinstance(value, list):
            params[key] = value
            clauses.append(f"{root_alias}.{key} IN ${key}")
        else:
            params[key] = value
            clauses.append(f"{root_alias}.{key} = ${key}")

    return clauses


def _get_relationship_filters(filters: dict, mapping: dict, skip_fields: set) -> dict:
    """Extract filters that map to graph relationships, excluding skip_fields."""
    return {k: v for k, v in filters.items()
            if k in mapping and v and k not in skip_fields}


def _build_strict_query(filters: dict, mapping: dict, limit: int, return_fields: list,
                         root_label: str, order_by: str, skip_fields: set) -> dict:
    """All relationship filters as required MATCH patterns (AND logic)."""
    params = {}
    match_parts = [f"(p:{root_label})"]
    rel_filters = _get_relationship_filters(filters, mapping, skip_fields)

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

    where_clauses = _build_where_clauses(filters, mapping, params, "p", skip_fields)

    for i, (key, value) in enumerate(rel_filters.items()):
        if isinstance(value, list):
            rel_type, target_label, prop = mapping[key]
            where_clauses.append(f"n{i}.{prop} IN ${key}_val")

    cypher = "MATCH " + ",\n      ".join(match_parts)
    if where_clauses:
        cypher += "\nWHERE " + " AND ".join(where_clauses)

    return_clause = ", ".join(f"p.{f}" for f in return_fields)
    order_clause = f"\nORDER BY p.{order_by}" if order_by else ""
    cypher += f"\nRETURN {return_clause}{order_clause} LIMIT {limit}"

    return {"cypher": cypher, "params": params}


def _build_flexible_query(filters: dict, mapping: dict, priority_fields: list,
                           limit: int, return_fields: list, root_label: str,
                           order_by: str, skip_fields: set) -> dict:
    """Priority fields as required MATCH, others as OPTIONAL MATCH with relevance scoring."""
    params = {}
    rel_filters = _get_relationship_filters(filters, mapping, skip_fields)

    required = {k: v for k, v in rel_filters.items() if k in priority_fields}
    optional = {k: v for k, v in rel_filters.items() if k not in priority_fields}

    match_parts = [f"(p:{root_label})"]
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

    where_clauses = _build_where_clauses(filters, mapping, params, "p", skip_fields)

    for i, (key, value) in enumerate(required.items()):
        if isinstance(value, list):
            rel_type, target_label, prop = mapping[key]
            where_clauses.append(f"r{i}.{prop} IN ${key}_val")

    # WHERE must come before OPTIONAL MATCH so it filters the base node, not the optional pattern
    cypher = "MATCH " + ",\n      ".join(match_parts) + "\n"
    if where_clauses:
        cypher += "WHERE " + " AND ".join(where_clauses) + "\n"
    cypher += "".join(optional_parts)

    return_clause = ", ".join(f"p.{f}" for f in return_fields)
    order_suffix = f", p.{order_by}" if order_by else ""
    if score_parts:
        score_expr = " + ".join(score_parts)
        cypher += f"WITH p, ({score_expr}) AS relevance\n"
        cypher += f"RETURN {return_clause}, relevance\nORDER BY relevance DESC{order_suffix} LIMIT {limit}"
    else:
        plain_order = f"\nORDER BY p.{order_by}" if order_by else ""
        cypher += f"RETURN {return_clause}{plain_order} LIMIT {limit}"

    return {"cypher": cypher, "params": params}


def _build_similar_query(filters: dict, mapping: dict, limit: int, return_fields: list,
                          root_label: str, order_by: str, skip_fields: set) -> dict:
    """OR-based scoring — matches as many relationship filters as possible."""
    params = {}
    rel_filters = _get_relationship_filters(filters, mapping, skip_fields)

    if not rel_filters:
        return _build_exploratory_query(filters, mapping, limit, return_fields, root_label, order_by, skip_fields)

    cypher = f"MATCH (p:{root_label})\n"
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

    # WHERE before OPTIONAL MATCHes so it filters the base node first
    where_clauses = _build_where_clauses(filters, mapping, params, "p", skip_fields)

    cypher_header = f"MATCH (p:{root_label})\n"
    if where_clauses:
        cypher_header += "WHERE " + " AND ".join(where_clauses) + "\n"
    cypher = cypher_header + cypher[len(f"MATCH (p:{root_label})\n"):]

    score_expr = " + ".join(score_parts)
    return_clause = ", ".join(f"p.{f}" for f in return_fields)
    order_suffix = f", p.{order_by}" if order_by else ""
    cypher += f"WITH p, ({score_expr}) AS relevance\n"
    cypher += f"WHERE relevance > 0\n"
    cypher += f"RETURN {return_clause}, relevance\nORDER BY relevance DESC{order_suffix} LIMIT {limit}"

    return {"cypher": cypher, "params": params}


def _build_exploratory_query(filters: dict, mapping: dict, limit: int, return_fields: list,
                              root_label: str, order_by: str, skip_fields: set) -> dict:
    """Minimal query — browse with property filters only, no graph traversals."""
    params = {}
    where_clauses = _build_where_clauses(filters, mapping, params, "p", skip_fields)

    cypher = f"MATCH (p:{root_label})\n"
    if where_clauses:
        cypher += "WHERE " + " AND ".join(where_clauses) + "\n"

    return_clause = ", ".join(f"p.{f}" for f in return_fields)
    order_clause = f"\nORDER BY p.{order_by}" if order_by else ""
    cypher += f"RETURN {return_clause}{order_clause} LIMIT {limit}"

    return {"cypher": cypher, "params": params}


def _auto_select_mode(filters: dict, mapping: dict, skip_fields: set) -> str:
    count = len(_get_relationship_filters(filters, mapping, skip_fields))
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
    priority_fields = inputs.get("priority_fields", "[]")
    return_fields = inputs.get("return_fields", "[]")
    root_label = inputs.get("root_label", "")
    order_by = inputs.get("order_by", "")
    skip_fields_raw = inputs.get("skip_fields", "[]")

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "graph_query"

    if not root_label:
        logger.warning("Cypher Query Builder: root_label is required but not provided")
        return {"variables": {output_key: {}}}

    filters = _to_dict(filters) if filters else {}
    schema_topology = _to_dict(schema_topology) if schema_topology else {}
    if isinstance(priority_fields, str):
        priority_fields = json.loads(priority_fields)
    if isinstance(return_fields, str):
        return_fields = json.loads(return_fields)
    if isinstance(skip_fields_raw, str):
        skip_fields_raw = json.loads(skip_fields_raw)
    skip_fields: set = set(skip_fields_raw) if isinstance(skip_fields_raw, list) else set()

    if isinstance(filters, list):
        filters = filters[0] if len(filters) == 1 else {k: v for d in filters for k, v in d.items()} if filters else {}
    if not isinstance(filters, dict):
        filters = {}

    # Mapping is built exclusively from schema_topology — no static fallback
    mapping = _derive_mapping_from_topology(schema_topology, root_label) if schema_topology else {}

    if mode == "auto":
        mode = _auto_select_mode(filters, mapping, skip_fields)

    logger.info(f"🔨 Cypher Query Builder: root={root_label}, mode={mode}, filters={len(filters)}, limit={limit}")

    if mode == "strict":
        result = _build_strict_query(filters, mapping, limit, return_fields, root_label, order_by, skip_fields)
    elif mode == "flexible":
        result = _build_flexible_query(filters, mapping, priority_fields, limit, return_fields, root_label, order_by, skip_fields)
    elif mode == "similar":
        result = _build_similar_query(filters, mapping, limit, return_fields, root_label, order_by, skip_fields)
    elif mode == "exploratory":
        result = _build_exploratory_query(filters, mapping, limit, return_fields, root_label, order_by, skip_fields)
    else:
        result = _build_flexible_query(filters, mapping, priority_fields, limit, return_fields, root_label, order_by, skip_fields)

    result["explanation"] = (
        f"Generated via {mode} mode with "
        f"{len(_get_relationship_filters(filters, mapping, skip_fields))} relationship filter(s)"
    )

    logger.info(f"✅ Cypher Query Builder: {result['cypher'][:80]}...")
    return {"variables": {output_key: result}}
