"""
Gemini Format Utilities - 统一的 Gemini 格式处理和转换工具
提供对 Gemini API 请求体和响应的标准化处理
────────────────────────────────────────────────────────────────
"""
from typing import Any, Dict, Optional

from src.log import log
from src.converter.thoughtSignature_fix import SKIP_THOUGHT_SIGNATURE_VALIDATOR

# ==================== Gemini API 配置 ====================

# ====================== Model Configuration ======================

# Default Safety Settings for Google API
DEFAULT_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_HATE", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_JAILBREAK", "threshold": "BLOCK_NONE"},
]

# Lite Safety Settings (5 categories) - Used by Code Assist
LITE_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
]


def _append_schema_hint(schema: Dict[str, Any], hint: str) -> None:
    """Move fragile validation details into description instead of sending them raw."""
    if not hint:
        return
    desc = schema.get("description")
    schema["description"] = f"{desc} ({hint})" if desc else hint


def _resolve_schema_ref(ref: str, root_schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None

    node: Any = root_schema
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]

    return node if isinstance(node, dict) else None


def _clean_parameters_json_schema(
    schema: Any,
    root_schema: Optional[Dict[str, Any]] = None,
    visited: Optional[set] = None,
) -> Any:
    """Clean a tool schema for Code Assist's parametersJsonSchema field."""
    if isinstance(schema, list):
        return [_clean_parameters_json_schema(item, root_schema, visited) for item in schema]
    if not isinstance(schema, dict):
        return schema

    if root_schema is None:
        root_schema = schema
    if visited is None:
        visited = set()

    ref = schema.get("$ref")
    if isinstance(ref, str):
        if id(schema) in visited:
            return {"type": "OBJECT", "description": "(circular ref)"}
        resolved = _resolve_schema_ref(ref, root_schema)
        if resolved is not None:
            visited.add(id(schema))
            cleaned = _clean_parameters_json_schema(resolved, root_schema, visited)
            visited.remove(id(schema))
            if isinstance(cleaned, dict):
                merged = dict(cleaned)
                for k, v in schema.items():
                    if k != "$ref":
                        merged[k] = v
                return merged

    cleaned_schema: Dict[str, Any] = {}
    allowed_keys = {
        "type", "format", "title", "description", "nullable",
        "enum", "maxItems", "minItems", "properties", "required",
        "items", "default", "example"
    }

    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        non_null_types = [t for t in raw_type if t != "null"]
        if "null" in raw_type:
            cleaned_schema["nullable"] = True
        if non_null_types:
            cleaned_schema["type"] = str(non_null_types[0]).upper()
    elif isinstance(raw_type, str):
        cleaned_schema["type"] = raw_type.upper()

    for k, v in schema.items():
        if k == "type":
            continue
        if k in allowed_keys:
            cleaned_schema[k] = v

    unsupported_rules = []
    for rule in ["pattern", "minimum", "maximum", "minLength", "maxLength", "multipleOf"]:
        if rule in schema:
            unsupported_rules.append(f"{rule}: {schema[rule]}")

    if unsupported_rules:
        _append_schema_hint(cleaned_schema, "; ".join(unsupported_rules))

    any_of = schema.get("anyOf") or schema.get("oneOf") or schema.get("allOf")
    if isinstance(any_of, list) and any_of:
        branch_hints = []
        for idx, branch in enumerate(any_of):
            if isinstance(branch, dict):
                b_type = branch.get("type", "any")
                b_desc = branch.get("description", "")
                branch_hints.append(f"option {idx+1}: type={b_type} {b_desc}".strip())

                if "type" not in cleaned_schema and "type" in branch:
                    cleaned_schema["type"] = str(branch["type"]).upper()

                if "properties" not in cleaned_schema and "properties" in branch:
                    cleaned_schema["properties"] = branch["properties"]

        if branch_hints and "description" not in cleaned_schema:
            cleaned_schema["description"] = "Allowed variants: " + " | ".join(branch_hints)

    if "properties" in cleaned_schema and isinstance(cleaned_schema["properties"], dict):
        cleaned_props = {}
        for prop_name, prop_val in cleaned_schema["properties"].items():
            cleaned_props[prop_name] = _clean_parameters_json_schema(prop_val, root_schema, visited)
        cleaned_schema["properties"] = cleaned_props

    if "items" in cleaned_schema and isinstance(cleaned_schema["items"], (dict, list)):
        cleaned_schema["items"] = _clean_parameters_json_schema(cleaned_schema["items"], root_schema, visited)

    if cleaned_schema.get("type") == "ARRAY" and "items" not in cleaned_schema:
        cleaned_schema["items"] = {"type": "STRING"}

    if cleaned_schema.get("type") == "OBJECT":
        if "properties" not in cleaned_schema:
            cleaned_schema["properties"] = {}

    return cleaned_schema


def _sanitize_function_declaration(func: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(func)
    parameters = cleaned.get("parameters")
    if isinstance(parameters, dict):
        cleaned["parameters"] = _clean_parameters_json_schema(parameters)
    return cleaned


def fix_gemini_request(
    request_data: Dict[str, Any],
    model: str = "",
    skip_thought_signature_validator: bool = SKIP_THOUGHT_SIGNATURE_VALIDATOR
) -> Dict[str, Any]:
    """
    Standardize Gemini request structure and remove invalid fields.
    """
    if not isinstance(request_data, dict):
        return request_data

    fixed_data = dict(request_data)

    # 1. Clean contents & parts
    contents = fixed_data.get("contents")
    if isinstance(contents, list):
        new_contents = []
        for content in contents:
            if not isinstance(content, dict):
                continue
            new_content = dict(content)

            parts = new_content.get("parts")
            if isinstance(parts, list):
                new_parts = []
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    new_part = dict(part)
                    # Filter empty text parts or unsupported keys
                    new_parts.append(new_part)
                new_content["parts"] = new_parts

            new_contents.append(new_content)
        fixed_data["contents"] = new_contents

    # 2. Clean tools
    tools = fixed_data.get("tools")
    if isinstance(tools, list):
        cleaned_tools = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            cleaned_tool = dict(tool)
            func_decls = cleaned_tool.get("functionDeclarations")
            if isinstance(func_decls, list):
                cleaned_tool["functionDeclarations"] = [
                    _sanitize_function_declaration(f) for f in func_decls if isinstance(f, dict)
                ]
            cleaned_tools.append(cleaned_tool)
        fixed_data["tools"] = cleaned_tools

    return fixed_data
