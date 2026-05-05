import json
from typing import Any
from jinja2 import Environment, meta, TemplateError


def _jinja_finalize(value):
    """
    Custom Jinja2 finalize: when a dict or list is interpolated in a template
    string (e.g. "Data: {{customer_assets}}"), serialize it as clean JSON
    instead of Python's repr (which uses single quotes and confuses LLMs).
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return value


# Native Jinja2 environment — with JSON-safe finalize
env = Environment(finalize=_jinja_finalize)

def resolve_placeholders(data: Any, variables: dict) -> Any:
    """
    Recursively scans strings, dicts, and lists. 
    Replaces {{ variable.path }} with actual data from the LangGraph state.
    """
    if isinstance(data, str):
        if not data.strip() or "{{" not in data:
            return data
            
        try:
            # 1. If it's a direct object reference (e.g., EXACTLY "{{user_data}}" or "{{user_data.path}}")
            # We want to return the raw Dict/List, not a stringified version of it.
            stripped_data = data.strip()
            if stripped_data.startswith("{{") and stripped_data.endswith("}}"):
                var_name = stripped_data[2:-2].strip()
                # Ensure it's just a variable path, no spaces or operators
                if all(c.isalnum() or c in '._' for c in var_name):
                    if "." not in var_name and var_name in variables:
                        return variables[var_name]
                    return _resolve_dot_notation(var_name, variables)

            # 2. Otherwise, it's a mixed string (e.g., "Hello {{user.name}}!")
            template = env.from_string(data)
            return template.render(**variables)
            
        except TemplateError:
            return data

    # Recursive traversal for nested JSON configs
    elif isinstance(data, dict):
        return {k: resolve_placeholders(v, variables) for k, v in data.items()}
    elif isinstance(data, list):
        return [resolve_placeholders(i, variables) for i in data]
    
    return data

def _resolve_dot_notation(path: str, data: dict) -> Any:
    """Resolves deep paths safely."""
    try:
        template = env.from_string(f"{{{{ {path} }}}}")
        rendered = template.render(**data)
        
        # If it rendered a dict/list string, convert it back to actual JSON
        try: return json.loads(rendered.replace("'", '"'))
        except json.JSONDecodeError: return rendered
    except Exception:
        return None
def get_undeclared_variables(data: Any, variables: dict) -> list:
    """
    Recursively scans for {{ variable.paths }} and checks if they exist in state.
    Returns a list of unique missing variable paths.
    """
    import re
    found = set()
    
    def scan(obj):
        if isinstance(obj, str):
            # Capture alpha-numeric characters, dots (for dot notation), and brackets (for list indices)
            matches = re.findall(r"\{\{\s*([a-zA-Z0-9_.[\]\-]+)\s*\}\}", obj)
            for m in matches:
                path = m.strip()
                # Check if this path is resolvable
                # Since _resolve_dot_notation is in this file, we can call it
                val = _resolve_dot_notation(path, variables)
                # If it renders back as the template or is None, it's missing
                if val is None or val == f"{{{{ {path} }}}}":
                    if path not in variables:
                        found.add(path)
        elif isinstance(obj, dict):
            for v in obj.values(): scan(v)
        elif isinstance(obj, list):
            for i in obj: scan(i)

    scan(data)
    return sorted(list(found))
