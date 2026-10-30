"""Deterministic code generation from frozen OpenAPI contracts.

Output is a pure function of the contract artifact and its metadata, so
`aghub codegen --check` can gate CI on drift. The generated files have no
runtime dependencies beyond the standard library.
"""

import json
import keyword
import pprint
import re

import yaml

from .errors import AghubError

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


# -- contract model ------------------------------------------------------


def load_spec(blob):
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AghubError(f"contract artifact is not UTF-8: {exc}") from exc
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AghubError(f"contract artifact is not valid YAML/JSON: {exc}") from exc
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise AghubError("contract artifact must be a mapping (an OpenAPI document)")
    return spec


def extract(spec):
    raw_paths = spec.get("paths")
    if not isinstance(raw_paths, dict):
        raw_paths = {}

    operations = []
    for path in sorted(str(item) for item in raw_paths):
        item = raw_paths.get(path)
        if not isinstance(item, dict):
            item = {}
        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            operations.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "operation_id": operation.get("operationId")
                    or _operation_id(method, path),
                    "summary": operation.get("summary")
                    or operation.get("description")
                    or "",
                    "response": _response_example(operation, spec),
                }
            )

    routes = sorted({operation["path"] for operation in operations})
    if not routes:
        routes = sorted(str(item) for item in raw_paths)

    components = spec.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    if not isinstance(schemas, dict):
        schemas = spec.get("definitions")
    if not isinstance(schemas, dict):
        schemas = {}

    return {"routes": routes, "operations": operations, "schemas": schemas, "spec": spec}


def diff_specs(old_spec, new_spec):
    """Structural diff between two OpenAPI documents, for humans and the board."""
    old = extract(old_spec)
    new = extract(new_spec)

    old_operations = {
        (operation["method"], operation["path"]): operation
        for operation in old["operations"]
    }
    new_operations = {
        (operation["method"], operation["path"]): operation
        for operation in new["operations"]
    }
    old_keys = set(old_operations)
    new_keys = set(new_operations)

    def keyed(keys):
        return [
            {"method": method, "path": path}
            for method, path in sorted(keys)
        ]

    changed = []
    for method, path in sorted(old_keys & new_keys):
        before = old_operations[(method, path)]
        after = new_operations[(method, path)]
        fields = [
            field
            for field in ("operation_id", "summary", "response")
            if _canonical(before.get(field)) != _canonical(after.get(field))
        ]
        if fields:
            changed.append(
                {"method": method, "path": path, "fields": fields}
            )

    old_schemas = old["schemas"]
    new_schemas = new["schemas"]
    schema_names = set(old_schemas) | set(new_schemas)
    schema_changes = {}
    for name in sorted(schema_names):
        if name not in old_schemas:
            schema_changes[name] = {"added": True, "removed": False, "fields": []}
            continue
        if name not in new_schemas:
            schema_changes[name] = {"added": False, "removed": True, "fields": []}
            continue
        before = old_schemas.get(name) or {}
        after = new_schemas.get(name) or {}
        if _canonical(before) == _canonical(after):
            continue
        if not isinstance(before, dict):
            before = {}
        if not isinstance(after, dict):
            after = {}
        fields = sorted(
            field
            for field in set(before) & set(after)
            if _canonical(before[field]) != _canonical(after[field])
        )
        before_properties = before.get("properties")
        after_properties = after.get("properties")
        if not isinstance(before_properties, dict):
            before_properties = {}
        if not isinstance(after_properties, dict):
            after_properties = {}
        schema_changes[name] = {
            "added": False,
            "removed": False,
            "fields": fields,
            "fields_added": sorted(set(after) - set(before)),
            "fields_removed": sorted(set(before) - set(after)),
            "properties_added": sorted(set(after_properties) - set(before_properties)),
            "properties_removed": sorted(set(before_properties) - set(after_properties)),
            "properties_changed": sorted(
                prop
                for prop in set(before_properties) & set(after_properties)
                if _canonical(before_properties[prop])
                != _canonical(after_properties[prop])
            ),
        }

    return {
        "routes_added": sorted(set(new["routes"]) - set(old["routes"])),
        "routes_removed": sorted(set(old["routes"]) - set(new["routes"])),
        "operations_added": keyed(new_keys - old_keys),
        "operations_removed": keyed(old_keys - new_keys),
        "operations_changed": changed,
        "schemas_added": sorted(set(new_schemas) - set(old_schemas)),
        "schemas_removed": sorted(set(old_schemas) - set(new_schemas)),
        "schemas_changed": schema_changes,
    }


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def generate(name, version, meta, spec, target="all"):
    if target not in ("all", "ts", "py", "mock", "sql"):
        raise AghubError(f"unknown codegen target '{target}'")
    model = extract(spec)
    sha = meta.get("sha256") or ""
    files = {}
    if target in ("all", "ts"):
        files[f"{_file_stem(name)}.ts"] = render_ts(name, version, sha, model)
    if target in ("all", "py"):
        files[f"{_module_stem(name)}.py"] = render_py(name, version, sha, model)
    if target in ("all", "mock"):
        files[mock_filename(name)] = render_mock(name, version, sha, model)
    if target in ("all", "sql"):
        files[f"{_file_stem(name)}.sql"] = render_sql(name, version, sha, model)

    manifest = {
        "contract": f"{name}@{version}",
        "sha256": sha,
        "routes": model["routes"],
        "operations": [
            {
                "method": operation["method"],
                "path": operation["path"],
                "operation_id": operation["operation_id"],
            }
            for operation in model["operations"]
        ],
        "files": sorted([*files, "manifest.json"]),
    }
    files["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    return files


# -- shared helpers ------------------------------------------------------


def _file_stem(name):
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name)).strip("-")
    return stem or "contract"


def _module_stem(name):
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", str(name)).strip("_") or "contract"
    if stem[0].isdigit():
        stem = "_" + stem
    return stem


def mock_filename(name):
    return f"{_module_stem(name)}_mock.py"


def _operation_id(method, path):
    words = [part for part in re.split(r"[^A-Za-z0-9]+", path) if part]
    suffix = "".join(word[:1].upper() + word[1:] for word in words)
    return method + (suffix or "Root")


def _type_name(value):
    name = re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_") or "Schema"
    if name[0].isdigit():
        name = "_" + name
    if keyword.iskeyword(name):
        name += "_"
    return name


def _field_name(value):
    name = re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_") or "field"
    if name[0].isdigit():
        name = "_" + name
    if keyword.iskeyword(name):
        name += "_"
    return name


def _snake(value):
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text or "value"


def _py_method(value):
    return _field_name(_snake(value))


def _ts_method(value):
    text = re.sub(r"[^A-Za-z0-9_$]+", "_", str(value)).strip("_")
    if not text:
        text = "operation"
    if text[0].isdigit():
        text = "_" + text
    return text


def _sql_ident(value):
    name = re.sub(r"[^a-z0-9_]+", "_", _snake(value)).strip("_") or "value"
    if name[0].isdigit():
        name = "_" + name
    return name


def _resolve(spec, ref):
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return {}
    node = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _doc(text, prefix="//"):
    lines = []
    for line in str(text or "").splitlines():
        lines.append(f"{prefix} {line}".rstrip())
    return lines


# -- TypeScript ----------------------------------------------------------


def render_ts(name, version, sha, model):
    lines = [
        f"// Generated by aghub from {name}@{version} (sha256 {sha or 'unknown'}).",
        "// Do not edit. Regenerate with `aghub codegen`.",
        "",
        "export const CONTRACT = {",
        f'  name: {json.dumps(name)},',
        f"  version: {version},",
        f'  sha256: {json.dumps(sha)},',
        "} as const;",
        "",
        "export const ROUTES = [",
    ]
    lines.extend(f"  {json.dumps(route)}," for route in model["routes"])
    lines.extend(
        [
            "] as const;",
            "",
            "export type Route = (typeof ROUTES)[number];",
            "",
            "export const OPERATIONS = {",
        ]
    )
    for operation in model["operations"]:
        key = f"{operation['method']} {operation['path']}"
        line = f"  {json.dumps(key)}: {json.dumps(operation['operation_id'])}"
        lines.append(line + ",")
    lines.extend(
        [
            "} as const;",
            "",
            "export type Operation = keyof typeof OPERATIONS;",
        ]
    )

    schemas = model["schemas"]
    if schemas:
        lines.extend(["", "// -- schemas --"])
        for key in sorted(schemas):
            schema = schemas[key]
            lines.extend(_doc(schema.get("description") or schema.get("summary")))
            type_name = _type_name(key)
            properties = schema.get("properties") if isinstance(schema, dict) else None
            if isinstance(properties, dict) and properties:
                lines.append(f"export interface {type_name} {{")
                required = set(schema.get("required") or [])
                for prop in properties:
                    lines.append(
                        _ts_property(prop, properties[prop], prop in required, model["spec"])
                    )
                lines.append("}")
            else:
                lines.append(
                    f"export type {type_name} = {_ts_type(schema, model['spec'])};"
                )

    lines.extend(
        [
            "",
            "export interface RequestOptions {",
            "  pathParams?: Record<string, string | number>;",
            "  query?: Record<string, string | number | boolean | undefined>;",
            "  body?: unknown;",
            "  headers?: Record<string, string>;",
            "}",
            "",
            "export class ContractClient {",
            '  constructor(public baseUrl = "", private fetcher: typeof fetch = fetch) {}',
            "",
            "  async call<T = unknown>(operation: string, options: RequestOptions = {}): Promise<T> {",
            '    const [method, template] = operation.split(" ", 2);',
            "    const path = template.replace(/\\{([^}]+)\\}/g, (_, name: string) =>",
            "      encodeURIComponent(String((options.pathParams || {})[name] ?? \"\")));",
            '    let url = (this.baseUrl || "").replace(/\\/$/, "") + path;',
            "    const search = new URLSearchParams();",
            "    for (const [key, value] of Object.entries(options.query || {})) {",
            "      if (value !== undefined) search.set(key, String(value));",
            "    }",
            '    if (search.toString()) url += "?" + search.toString();',
            "    const response = await this.fetcher(url, {",
            "      method,",
            "      headers: {",
            '        "Content-Type": "application/json",',
            "        ...(options.headers || {}),",
            "      },",
            "      body: options.body === undefined ? undefined : JSON.stringify(options.body),",
            "    });",
            "    const text = await response.text();",
            "    const data = text ? JSON.parse(text) : null;",
            "    if (!response.ok) {",
            "      throw new Error(method + \" \" + path + \" failed: \" + response.status + \" \" + text);",
            "    }",
            "    return data as T;",
            "  }",
        ]
    )
    used = set()
    for operation in model["operations"]:
        base_name = _ts_method(operation["operation_id"])
        method_name = base_name
        counter = 2
        while method_name in used:
            method_name = f"{base_name}{counter}"
            counter += 1
        used.add(method_name)
        key = f"{operation['method']} {operation['path']}"
        lines.append(
            f"  async {method_name}(options: RequestOptions = {{}}): Promise<unknown> "
            f"{{ return this.call({json.dumps(key)}, options); }}"
        )
    lines.append("}")
    return "\n".join(lines).rstrip() + "\n"


def _ts_property(name, schema, required, spec):
    rendered = _ts_type(schema, spec)
    optional = "" if required else "?"
    prop = name if re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", str(name)) else json.dumps(name)
    parts = rendered.splitlines()
    if len(parts) == 1:
        return f"  {prop}{optional}: {parts[0]};"
    body = [f"  {prop}{optional}: {parts[0]}"]
    body.extend("  " + part for part in parts[1:-1])
    body.append("  " + parts[-1] + ";")
    return "\n".join(body)


def _ts_type(schema, spec, depth=0):
    if not isinstance(schema, dict) or depth > 8:
        return "unknown"
    if schema.get("$ref"):
        return _type_name(str(schema["$ref"]).rsplit("/", 1)[-1])
    if schema.get("enum"):
        values = [json.dumps(value, ensure_ascii=False) for value in schema["enum"]]
        return " | ".join(values) or "never"
    for union in ("oneOf", "anyOf"):
        if isinstance(schema.get(union), list) and schema[union]:
            return " | ".join(_ts_type(item, spec, depth + 1) for item in schema[union])
    if isinstance(schema.get("allOf"), list) and schema["allOf"]:
        return " & ".join(_ts_type(item, spec, depth + 1) for item in schema["allOf"])

    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(
            _ts_type({**schema, "type": item, "nullable": False}, spec, depth + 1)
            for item in kind
        ) or "unknown"
    if kind == "array" or isinstance(schema.get("items"), dict):
        item = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        base = f"Array<{_ts_type(item, spec, depth + 1)}>"
    elif kind == "object" or isinstance(schema.get("properties"), dict):
        properties = schema.get("properties") or {}
        if not properties:
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                base = f"Record<string, {_ts_type(additional, spec, depth + 1)}>"
            else:
                base = "Record<string, unknown>"
        else:
            required = set(schema.get("required") or [])
            lines = ["{"]
            for prop in properties:
                lines.append(
                    _ts_property(prop, properties[prop], prop in required, spec)
                )
            lines.append("}")
            base = "\n".join(lines)
    else:
        base = {
            "string": "string",
            "integer": "number",
            "number": "number",
            "boolean": "boolean",
        }.get(kind, "unknown")
    if schema.get("nullable"):
        base += " | null"
    return base


# -- Python --------------------------------------------------------------


def render_py(name, version, sha, model):
    lines = [
        f'"""Generated by aghub from {name}@{version} (sha256 {sha or "unknown"}).',
        "",
        "Do not edit. Regenerate with `aghub codegen`.",
        '"""',
        "from __future__ import annotations",
        "",
        "import json as _json",
        "import urllib.parse as _urlparse",
        "import urllib.request as _urlreq",
        "from dataclasses import dataclass",
        "from typing import Any, Literal",
        "",
        "CONTRACT = {",
        f'    "name": {name!r},',
        f'    "version": {version},',
        f'    "sha256": {sha!r},',
        "}",
        "",
        "ROUTES = (",
    ]
    lines.extend(f"    {route!r}," for route in model["routes"])
    lines.extend([")", "", "OPERATIONS = {"])
    for operation in model["operations"]:
        lines.append(
            f"    ({operation['method']!r}, {operation['path']!r}): "
            f"{operation['operation_id']!r},"
        )
    lines.append("}")
    lines.append("")
    lines.append("SCHEMAS: dict[str, Any] = {")
    for key in sorted(model["schemas"]):
        schema = model["schemas"][key]
        lines.append(f"    {key!r}: {_py_literal(schema)},")
    lines.append("}")

    for key in sorted(model["schemas"]):
        schema = model["schemas"][key]
        lines.append("")
        description = schema.get("description") or schema.get("summary")
        if description:
            lines.extend(_doc(description, "#"))
        type_name = _type_name(key)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict) or not properties:
            lines.append(f"{type_name} = {_py_ann(schema, model['spec'])}")
            continue
        lines.append("@dataclass")
        lines.append(f"class {type_name}:")
        required = set(schema.get("required") or [])
        ordered = [prop for prop in properties if prop in required]
        ordered += [prop for prop in properties if prop not in required]
        if not ordered:
            lines.append("    pass")
            continue
        for prop in ordered:
            annotation = _py_ann(properties[prop], model["spec"])
            if prop not in required:
                if "None" not in annotation:
                    annotation = f"{annotation} | None"
                lines.append(f"    {_field_name(prop)}: {annotation} = None")
            else:
                lines.append(f"    {_field_name(prop)}: {annotation}")

    lines.extend(
        [
            "",
            "class Client:",
            '    """Typed, dependency-free HTTP client for this contract."""',
            "",
            '    def __init__(self, base_url: str = "", headers: dict[str, str] | None = None):',
            "        self.base_url = base_url.rstrip('/')",
            "        self.headers = dict(headers or {})",
            "",
            "    def call(",
            "        self,",
            "        method: str,",
            "        path_template: str,",
            "        path_params: dict[str, Any] | None = None,",
            "        query: dict[str, Any] | None = None,",
            "        body: Any = None,",
            "        headers: dict[str, str] | None = None,",
            "    ) -> Any:",
            "        path = path_template",
            "        for key, value in (path_params or {}).items():",
            "            path = path.replace('{' + key + '}', _urlparse.quote(str(value), safe=''))",
            "        values = {key: value for key, value in (query or {}).items() if value is not None}",
            "        if values:",
            "            path += ('&' if '?' in path else '?') + _urlparse.urlencode(values)",
            "        payload = None",
            "        request_headers = {**self.headers, **(headers or {})}",
            "        if body is not None:",
            "            payload = _json.dumps(body).encode('utf-8')",
            "            request_headers.setdefault('Content-Type', 'application/json')",
            "        request = _urlreq.Request(",
            "            self.base_url + path, data=payload, method=method, headers=request_headers",
            "        )",
            "        with _urlreq.urlopen(request) as response:",
            "            text = response.read().decode('utf-8')",
            "        return _json.loads(text) if text else None",
        ]
    )
    used = set()
    for operation in model["operations"]:
        base_name = _py_method(operation["operation_id"])
        method_name = base_name
        counter = 2
        while method_name in used:
            method_name = f"{base_name}_{counter}"
            counter += 1
        used.add(method_name)
        lines.extend(
            [
                "",
                f"    def {method_name}(",
                "        self,",
                "        path_params: dict[str, Any] | None = None,",
                "        query: dict[str, Any] | None = None,",
                "        body: Any = None,",
                "    ) -> Any:",
                f"        return self.call({operation['method']!r}, {operation['path']!r}, "
                "path_params, query, body)",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _py_ann(schema, spec, depth=0):
    if not isinstance(schema, dict) or depth > 8:
        return "Any"
    if schema.get("$ref"):
        return _type_name(str(schema["$ref"]).rsplit("/", 1)[-1])
    if schema.get("enum"):
        return "Literal[" + ", ".join(repr(value) for value in schema["enum"]) + "]"
    for union in ("oneOf", "anyOf"):
        if isinstance(schema.get(union), list) and schema[union]:
            return " | ".join(_py_ann(item, spec, depth + 1) for item in schema[union])
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(
            _py_ann({**schema, "type": item, "nullable": False}, spec, depth + 1)
            for item in kind
        ) or "Any"
    if kind == "array" or isinstance(schema.get("items"), dict):
        item = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        base = f"list[{_py_ann(item, spec, depth + 1)}]"
    elif kind == "object" or isinstance(schema.get("properties"), dict):
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            base = f"dict[str, {_py_ann(additional, spec, depth + 1)}]"
        else:
            base = "dict[str, Any]"
    else:
        base = {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
        }.get(kind, "Any")
    if schema.get("nullable") and "None" not in base:
        base = f"{base} | None"
    return base


def _py_literal(value):
    return pprint.pformat(value, width=100, sort_dicts=False)


# -- SQL migration -------------------------------------------------------


def _sql_type(schema, spec, depth=0):
    if not isinstance(schema, dict) or depth > 8:
        return "JSONB"
    if schema.get("$ref"):
        return _sql_type(_resolve(spec, schema["$ref"]), spec, depth + 1)
    if schema.get("enum"):
        return "TEXT"
    for union in ("oneOf", "anyOf"):
        if isinstance(schema.get(union), list) and schema[union]:
            return _sql_type(schema[union][0], spec, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    if kind == "array" or isinstance(schema.get("items"), dict):
        return "JSONB"
    if kind == "object" or isinstance(schema.get("properties"), dict):
        return "JSONB"
    fmt = schema.get("format")
    if kind == "string":
        return {"uuid": "UUID", "date": "DATE", "date-time": "TIMESTAMPTZ"}.get(
            fmt, "TEXT"
        )
    if kind == "integer":
        return "BIGINT" if fmt in ("int64", None) else "INTEGER"
    if kind == "number":
        return "DOUBLE PRECISION"
    if kind == "boolean":
        return "BOOLEAN"
    return "JSONB"


def render_sql(name, version, sha, model):
    lines = [
        f"-- Generated by aghub from {name}@{version} (sha256 {sha or 'unknown'}).",
        "-- Review before applying; this is a starting migration, not a live diff.",
        "",
    ]
    for operation in model["operations"]:
        lines.append(
            f"-- {operation['method']} {operation['path']} -> {operation['operation_id']}"
        )
    if model["operations"]:
        lines.append("")
    created = 0
    for key in sorted(model["schemas"]):
        schema = model["schemas"][key]
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict) or not properties:
            continue
        table = _sql_ident(key)
        required = set(schema.get("required") or [])
        columns = []
        for prop, prop_schema in properties.items():
            column = _sql_ident(prop)
            kind = _sql_type(prop_schema, model["spec"])
            constraints = ""
            if column == "id" and kind in ("TEXT", "UUID", "BIGINT"):
                constraints = " PRIMARY KEY"
            elif prop in required:
                constraints = " NOT NULL"
            columns.append(f'  "{column}" {kind}{constraints}')
        lines.append(f'CREATE TABLE IF NOT EXISTS "{table}" (')
        lines.append(",\n".join(columns))
        lines.append(");")
        lines.append("")
        created += 1
    if not created:
        lines.append("-- No object schemas were found; add tables for your domain.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# -- mock server ---------------------------------------------------------


def render_mock(name, version, sha, model):
    contract = f"{name}@{version}"
    routes = pprint.pformat(tuple(model["routes"]), width=100)
    operations = "\n".join(
        f"    ({operation['method']!r}, {operation['path']!r}): "
        f"{operation['operation_id']!r},"
        for operation in model["operations"]
    )
    responses = pprint.pformat(
        {
            (operation["method"], operation["path"]): operation["response"]
            for operation in model["operations"]
        },
        width=100,
        sort_dicts=False,
    )
    template = '''#!/usr/bin/env python3
"""Mock server generated by aghub from $CONTRACT$ (sha256 $SHA$)."""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROUTES = $ROUTES$

OPERATIONS = {
$OPERATIONS$
}

RESPONSES = $RESPONSES$


def route_pattern(route):
    parts = re.split(r"(\\{[^}]+\\})", route)
    pattern = "".join(
        "([^/]+)" if part.startswith("{") and part.endswith("}") else re.escape(part)
        for part in parts
    )
    return re.compile("^" + pattern + "$")


COMPILED = [(route, route_pattern(route)) for route in ROUTES]


def match(method, path):
    allowed = []
    for route, pattern in COMPILED:
        found = pattern.match(path)
        if not found:
            continue
        if (method, route) in OPERATIONS:
            names = re.findall(r"\\{([^}]+)\\}", route)
            return {
                "route": route,
                "operation": OPERATIONS[(method, route)],
                "params": dict(zip(names, found.groups())),
                "response": RESPONSES.get((method, route), {"ok": True}),
            }
        allowed.append(method)
    if allowed:
        return {"error": "method not allowed", "allowed": sorted(set(allowed))}
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "aghub-mock/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path}", flush=True)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        )

    def _send(self, status, payload):
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _handle(self):
        path = urlparse(self.path).path
        result = match(self.command, path)
        if result is None:
            self._send(404, {"error": "no such route", "path": path})
        elif "error" in result:
            self._send(405, result)
        else:
            self._send(200, result["response"])

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mock server for $CONTRACT$")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4010)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        "mock $CONTRACT$ listening on "
        f"http://{server.server_address[0]}:{server.server_address[1]}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
'''
    return (
        template.replace("$CONTRACT$", contract)
        .replace("$SHA$", sha or "unknown")
        .replace("$ROUTES$", routes)
        .replace("$OPERATIONS$", operations)
        .replace("$RESPONSES$", responses)
    )


def _response_example(operation, spec):
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return {"ok": True}
    for code in ("200", "201", "202", "204", "default"):
        response = responses.get(code)
        if not isinstance(response, dict):
            continue
        content = response.get("content")
        media = None
        if isinstance(content, dict) and content:
            media = content.get("application/json")
            if not isinstance(media, dict):
                media = next(
                    (value for value in content.values() if isinstance(value, dict)),
                    None,
                )
        if isinstance(media, dict):
            if "example" in media:
                return media["example"]
            examples = media.get("examples")
            if isinstance(examples, dict) and examples:
                first = next(iter(examples.values()), None)
                if isinstance(first, dict) and "value" in first:
                    return first["value"]
            if isinstance(media.get("schema"), dict):
                return _sample(media["schema"], spec)
        if isinstance(response.get("schema"), dict):
            return _sample(response["schema"], spec)
        examples = response.get("examples")
        if isinstance(examples, dict) and examples:
            first = next(iter(examples.values()))
            if isinstance(first, (dict, list)):
                return first
    return {"ok": True}


def _sample(schema, spec, depth=0):
    if not isinstance(schema, dict) or depth > 8:
        return None
    if schema.get("$ref"):
        return _sample(_resolve(spec, schema["$ref"]), spec, depth + 1)
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    if isinstance(schema.get("allOf"), list):
        merged = {}
        for part in schema["allOf"]:
            value = _sample(part, spec, depth + 1)
            if isinstance(value, dict):
                merged.update(value)
        return merged
    for union in ("oneOf", "anyOf"):
        if isinstance(schema.get(union), list) and schema[union]:
            return _sample(schema[union][0], spec, depth + 1)

    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    if kind == "array" or isinstance(schema.get("items"), dict):
        item = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        return [_sample(item, spec, depth + 1)]
    if kind == "object" or isinstance(schema.get("properties"), dict):
        properties = schema.get("properties") or {}
        return {
            key: _sample(value, spec, depth + 1)
            for key, value in properties.items()
        }
    if kind == "integer":
        return 0
    if kind == "number":
        return 0
    if kind == "boolean":
        return True
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "date":
            return "2026-01-01"
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "uuid":
            return "00000000-0000-0000-0000-000000000000"
        if fmt == "email":
            return "user@example.com"
        return "string"
    return None
