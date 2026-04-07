import json
from typing import Any
from jinja2 import Environment, meta, TemplateError

# Native Jinja2 environment
env = Environment()

def resolve_placeholders(data: Any, variables: dict) -> Any:
    """
    Recursively scans strings, dicts, and lists. 
    Replaces {{ variable.path }} with actual data from the LangGraph state.
    """
    if isinstance(data, str):
        if not data.strip() or "{{" not in data:
            return data
            
        try:
            # 1. If it's a direct object reference (e.g., EXACTLY "{{user_data}}")
            # We want to return the raw Dict/List, not a stringified version of it.
            ast = env.parse(data)
            vars_in_string = meta.find_undeclared_variables(ast)
            
            if len(vars_in_string) == 1 and data.strip() == f"{{{{{vars_in_string.copy().pop()}}}}}":
                var_name = data.replace("{{", "").replace("}}", "").strip()
                # Direct lookup for simple names (no dots) to preserve type (dict/list)
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