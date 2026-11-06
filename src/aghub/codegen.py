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

    schemes = _security_schemes(spec)
    default_security = _security_names(spec.get("security"), schemes)

    operations = []
    for path in sorted(str(item) for item in raw_paths):
        item = raw_paths.get(path)
        if not isinstance(item, dict):
            item = {}
        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            if "security" in operation:
                security = _security_names(operation.get("security"), schemes)
            else:
                security = list(default_security)
            raw_parameters = _merged_parameters(item, operation, spec)
            operations.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "operation_id": operation.get("operationId")
                    or _operation_id(method, path),
                    "summary": operation.get("summary")
                    or operation.get("description")
                    or "",
                    "parameters": _parameters(raw_parameters),
                    "request_body": _request_body(
                        item, operation, spec, raw_parameters
                    ),
                    "security": security,
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

    return {
        "routes": routes,
        "operations": operations,
        "schemas": schemas,
        "security_schemes": schemes,
        "security": default_security,
        "spec": spec,
    }


_PARAM_KINDS = {
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "string": "string",
    "array": "array",
}


def _param_type(schema):
    if not isinstance(schema, dict):
        return "string"
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    if kind == "array" or isinstance(schema.get("items"), dict):
        return "array"
    return _PARAM_KINDS.get(kind, "string")


def _merged_parameters(item, operation, spec):
    """Path- and operation-level parameters, operation wins on conflict."""
    collected = {}
    for source in (item.get("parameters"), operation.get("parameters")):
        if not isinstance(source, list):
            continue
        for param in source:
            if isinstance(param, dict) and param.get("$ref"):
                param = _resolve(spec, param["$ref"])
            if not isinstance(param, dict):
                continue
            name = param.get("name")
            location = param.get("in")
            if not name or not location:
                continue
            collected[(location, name)] = param
    return [collected[key] for key in sorted(collected)]


def _parameters(raw_parameters):
    """Normalize request parameters; body/formData are handled as a request body."""
    collected = {}
    for param in raw_parameters:
        name = param.get("name")
        location = param.get("in")
        if not name or not location or location in ("body", "formData"):
            continue
        schema = param.get("schema")
        if not isinstance(schema, dict):
            # Swagger 2 keeps type/enum directly on the parameter.
            schema = {
                "type": param.get("type"),
                "enum": param.get("enum"),
            }
        collected[(location, name)] = {
            "name": str(name),
            "in": str(location),
            "required": bool(param.get("required")) or location == "path",
            "type": _param_type(schema),
            "enum": [value for value in (schema.get("enum") or [])],
        }
    return [collected[key] for key in sorted(collected)]


def _json_media(content):
    """Pick the JSON media type from an OpenAPI 3 `content` map."""
    if not isinstance(content, dict) or not content:
        return "application/json", None
    if "application/json" in content:
        media = content.get("application/json")
        return "application/json", media if isinstance(media, dict) else {}
    for media_type in sorted(str(key) for key in content):
        if media_type.endswith("+json"):
            media = content.get(media_type)
            return media_type, media if isinstance(media, dict) else {}
    return None, None


def _request_body(item, operation, spec, raw_parameters):
    """Describe the request body: presence, media type, and JSON schema.

    OpenAPI 3 `requestBody` wins; Swagger 2 `in: body` / `in: formData` fall back.
    Only JSON bodies carry a schema we can validate; other media types are
    presence-checked and passed through as opaque bytes.
    """
    body = operation.get("requestBody")
    if body is None:
        body = item.get("requestBody")
    if isinstance(body, dict) and body.get("$ref"):
        body = _resolve(spec, body["$ref"])
    if isinstance(body, dict):
        media_type, media = _json_media(body.get("content"))
        if media_type is None:
            content = body.get("content")
            candidates = sorted(str(key) for key in content) if isinstance(content, dict) else []
            media_type = candidates[0] if candidates else "application/json"
            media = {}
        schema = media.get("schema") if isinstance(media, dict) else None
        return {
            "required": bool(body.get("required")),
            "content_type": media_type,
            "json": media_type == "application/json" or media_type.endswith("+json"),
            "schema": schema if isinstance(schema, dict) else None,
            "description": str(body.get("description") or ""),
        }

    for param in raw_parameters:
        if param.get("in") == "body":
            schema = param.get("schema")
            consumes = (
                operation.get("consumes")
                or item.get("consumes")
                or spec.get("consumes")
                or ["application/json"]
            )
            media_type = "application/json"
            if isinstance(consumes, list):
                json_types = [
                    str(value)
                    for value in consumes
                    if str(value) == "application/json" or str(value).endswith("+json")
                ]
                chosen = json_types[:1] or [str(value) for value in consumes][:1]
                if chosen:
                    media_type = chosen[0]
            return {
                "required": bool(param.get("required")),
                "content_type": media_type,
                "json": media_type == "application/json" or media_type.endswith("+json"),
                "schema": schema if isinstance(schema, dict) else None,
                "description": str(param.get("description") or ""),
            }

    form = [param for param in raw_parameters if param.get("in") == "formData"]
    if form:
        return {
            "required": any(bool(param.get("required")) for param in form),
            "content_type": "multipart/form-data",
            "json": False,
            "schema": None,
            "description": "",
        }
    return None


def _security_schemes(spec):
    """Normalize OpenAPI 3 securitySchemes (or Swagger 2 securityDefinitions)."""
    components = spec.get("components")
    raw = components.get("securitySchemes") if isinstance(components, dict) else None
    if not isinstance(raw, dict):
        raw = spec.get("securityDefinitions")
    schemes = {}
    if not isinstance(raw, dict):
        return schemes
    for name in sorted(str(key) for key in raw):
        defn = raw.get(name)
        if not isinstance(defn, dict):
            continue
        kind = str(defn.get("type") or "").strip()
        entry = {"name": name, "type": kind}
        if kind == "apiKey" or (kind == "" and defn.get("in")):
            entry["type"] = "apiKey"
            entry["in"] = str(defn.get("in") or "header")
            entry["parameter"] = str(defn.get("name") or "Authorization")
        elif kind == "http":
            entry["scheme"] = str(defn.get("scheme") or "bearer").lower()
            entry["bearer_format"] = str(defn.get("bearerFormat") or "")
        elif kind in ("basic", "apiKey"):
            pass
        else:
            # oauth2 / openIdConnect carry a bearer token in practice.
            entry["scheme"] = "bearer"
        if kind == "basic":  # Swagger 2 spelling of http basic.
            entry["type"] = "http"
            entry["scheme"] = "basic"
        schemes[name] = entry
    return schemes


def _security_names(requirements, schemes):
    """Flatten a security requirement list to the scheme names it references."""
    if not isinstance(requirements, list):
        return []
    names = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        for name in requirement:
            if name in schemes and name not in names:
                names.append(name)
    return names


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
            for field in ("operation_id", "summary", "request_body", "response")
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


def generate(name, version, meta, spec, target="all", dialect="postgres"):
    if target not in ("all", "ts", "py", "mock", "sql"):
        raise AghubError(f"unknown codegen target '{target}'")
    if dialect not in SQL_DIALECTS:
        raise AghubError(
            f"unknown SQL dialect '{dialect}' (choose from {', '.join(sorted(SQL_DIALECTS))})"
        )
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
        files[f"{_file_stem(name)}.sql"] = render_sql(
            name, version, sha, model, dialect=dialect
        )

    manifest = {
        "contract": f"{name}@{version}",
        "sha256": sha,
        "dialect": dialect,
        "routes": model["routes"],
        "security_schemes": {
            key: value["type"] for key, value in model["security_schemes"].items()
        },
        "operations": [
            {
                "method": operation["method"],
                "path": operation["path"],
                "operation_id": operation["operation_id"],
                "parameters": [
                    {
                        "name": param["name"],
                        "in": param["in"],
                        "required": param["required"],
                    }
                    for param in operation["parameters"]
                ],
                "security": operation["security"],
                "request_body": _body_manifest(operation["request_body"]),
            }
            for operation in model["operations"]
        ],
        "files": sorted([*files, "manifest.json"]),
    }
    files["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    return files


def _body_manifest(body):
    """The manifest view of a request body: small, stable, and machine-readable."""
    if body is None:
        return None
    return {
        "required": body["required"],
        "content_type": body["content_type"],
        "json": body["json"],
        "schema": body["schema"],
    }


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


def _scheme_public(scheme):
    """A scheme as exposed to generated clients (drops the internal name key)."""
    return {key: value for key, value in scheme.items() if key != "name"}


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

    lines.extend(["", "export const SECURITY_SCHEMES = "
                  + json.dumps(
                      {name: _scheme_public(scheme)
                       for name, scheme in model["security_schemes"].items()},
                      indent=2,
                  )
                  + " as const;"])
    lines.append("")
    lines.append(
        "export interface ParameterSpec { name: string; in: string; "
        "required: boolean; type: string; enum: readonly unknown[]; }"
    )
    op_params = {
        f"{operation['method']} {operation['path']}": operation["parameters"]
        for operation in model["operations"]
    }
    lines.append(
        "export const OPERATION_PARAMETERS: Record<string, ParameterSpec[]> = "
        + json.dumps(op_params, indent=2)
        + ";"
    )
    op_security = {
        f"{operation['method']} {operation['path']}": operation["security"]
        for operation in model["operations"]
    }
    lines.append(
        "export const OPERATION_SECURITY: Record<string, readonly string[]> = "
        + json.dumps(op_security, indent=2)
        + ";"
    )

    op_bodies = {
        f"{operation['method']} {operation['path']}": {
            "required": operation["request_body"]["required"],
            "contentType": operation["request_body"]["content_type"],
            "json": operation["request_body"]["json"],
            "schema": operation["request_body"]["schema"],
        }
        for operation in model["operations"]
        if operation["request_body"] is not None
    }
    lines.extend(
        [
            "",
            "export interface BodySpec { required: boolean; contentType: string; "
            "json: boolean; schema: unknown; }",
            "export const OPERATION_BODIES: Record<string, BodySpec> = "
            + json.dumps(op_bodies, indent=2)
            + ";",
            "",
            "export const SCHEMAS: Record<string, unknown> = "
            + json.dumps(model["schemas"], indent=2)
            + ";",
            "",
            "function resolveRef(ref: string, schemas: Record<string, unknown>): "
            "Record<string, any> {",
            '  const match = /^#\\/(?:components\\/schemas|definitions)\\/(.+)$/.exec(ref);',
            "  if (!match) return {};",
            "  let node: any = schemas;",
            "  for (const raw of match[1].split(\"/\")) {",
            "    const part = raw.replace(/~1/g, \"/\").replace(/~0/g, \"~\");",
            "    if (!node || typeof node !== \"object\" || !(part in node)) return {};",
            "    node = node[part];",
            "  }",
            "  return node && typeof node === \"object\" && !Array.isArray(node) ? node : {};",
            "}",
            "",
            "function bodyError(",
            "  schema: unknown,",
            "  value: unknown,",
            "  path: string,",
            "  schemas: Record<string, unknown>,",
            "  depth = 0,",
            "): string | null {",
            "  if (!schema || typeof schema !== \"object\" || depth > 16) return null;",
            "  let node = schema as Record<string, any>;",
            "  let hops = 0;",
            "  while (typeof node.$ref === \"string\" && hops < 32) {",
            "    node = resolveRef(node.$ref, schemas);",
            "    hops += 1;",
            "  }",
            "  if (!node || typeof node !== \"object\") return null;",
            "  const where = path || \"/\";",
            "  const kinds: string[] = Array.isArray(node.type) ? node.type : "
            "node.type ? [node.type] : [];",
            "  const nullable = node.nullable === true || kinds.includes(\"null\");",
            "  if (value === null) return nullable ? null : where + \": null is not allowed\";",
            "  if (Array.isArray(node.enum) && node.enum.length) {",
            "    const rendered = node.enum.map((item: unknown) => JSON.stringify(item));",
            "    if (!rendered.includes(JSON.stringify(value))) {",
            "      return where + \": must be one of \" + JSON.stringify(node.enum);",
            "    }",
            "  }",
            "  if (Array.isArray(node.allOf)) {",
            "    for (const part of node.allOf) {",
            "      const error = bodyError(part, value, path, schemas, depth + 1);",
            "      if (error) return error;",
            "    }",
            "  }",
            "  const union = Array.isArray(node.anyOf) ? node.anyOf : "
            "Array.isArray(node.oneOf) ? node.oneOf : null;",
            "  if (union) {",
            "    const matched = union.some(",
            "      (part: unknown) => bodyError(part, value, path, schemas, depth + 1) === null,",
            "    );",
            "    if (!matched) return where + \": does not match any allowed schema\";",
            "  }",
            "  const kind = kinds.find((item) => item !== \"null\") || \"\";",
            "  if (kind === \"object\" || (!kind && node.properties)) {",
            "    if (typeof value !== \"object\" || Array.isArray(value)) {",
            "      return where + \": expected object\";",
            "    }",
            "    const record = value as Record<string, unknown>;",
            "    for (const key of node.required || []) {",
            "      if (!(key in record)) return where + \": missing required property \" + JSON.stringify(key);",
            "    }",
            "    const properties = node.properties && typeof node.properties === \"object\" "
            "? node.properties : {};",
            "    for (const key of Object.keys(record)) {",
            "      if (key in properties) {",
            "        const error = bodyError(properties[key], record[key], path + \"/\" + key, "
            "schemas, depth + 1);",
            "        if (error) return error;",
            "      } else if (node.additionalProperties === false) {",
            "        return where + \": unexpected property \" + JSON.stringify(key);",
            "      } else if (node.additionalProperties && "
            "typeof node.additionalProperties === \"object\") {",
            "        const error = bodyError(node.additionalProperties, record[key], "
            "path + \"/\" + key, schemas, depth + 1);",
            "        if (error) return error;",
            "      }",
            "    }",
            "  } else if (kind === \"array\" || (!kind && node.items)) {",
            "    if (!Array.isArray(value)) return where + \": expected array\";",
            "    if (typeof node.minItems === \"number\" && value.length < node.minItems) {",
            "      return where + \": expected at least \" + node.minItems + \" item(s)\";",
            "    }",
            "    if (typeof node.maxItems === \"number\" && value.length > node.maxItems) {",
            "      return where + \": expected at most \" + node.maxItems + \" item(s)\";",
            "    }",
            "    if (node.items && typeof node.items === \"object\") {",
            "      for (let index = 0; index < value.length; index += 1) {",
            "        const error = bodyError(node.items, value[index], path + \"/\" + index, "
            "schemas, depth + 1);",
            "        if (error) return error;",
            "      }",
            "    }",
            "  } else if (kind === \"string\") {",
            "    if (typeof value !== \"string\") return where + \": expected string\";",
            "    if (typeof node.minLength === \"number\" && value.length < node.minLength) {",
            "      return where + \": expected at least \" + node.minLength + \" character(s)\";",
            "    }",
            "    if (typeof node.maxLength === \"number\" && value.length > node.maxLength) {",
            "      return where + \": expected at most \" + node.maxLength + \" character(s)\";",
            "    }",
            "  } else if (kind === \"integer\" || kind === \"number\") {",
            "    const numeric = typeof value === \"number\" && Number.isFinite(value);",
            "    if (!numeric || (kind === \"integer\" && !Number.isInteger(value as number))) {",
            "      return where + \": expected \" + kind;",
            "    }",
            "    if (typeof node.minimum === \"number\" && (value as number) < node.minimum) {",
            "      return where + \": must be >= \" + node.minimum;",
            "    }",
            "    if (typeof node.maximum === \"number\" && (value as number) > node.maximum) {",
            "      return where + \": must be <= \" + node.maximum;",
            "    }",
            "  } else if (kind === \"boolean\") {",
            "    if (typeof value !== \"boolean\") return where + \": expected boolean\";",
            "  }",
            "  return null;",
            "}",
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
            "export interface AuthOptions {",
            "  bearerToken?: string;",
            "  apiKey?: string;",
            "  basic?: { user: string; password: string };",
            "}",
            "",
            "export class ContractClient {",
            "  constructor(",
            '    public baseUrl = "",',
            "    private auth: AuthOptions = {},",
            "    private fetcher: typeof fetch = fetch,",
            "  ) {}",
            "",
            "  private validate(operation: string, options: RequestOptions): void {",
            "    for (const spec of OPERATION_PARAMETERS[operation] || []) {",
            '      const bag = spec.in === "query" ? options.query : '
            '(spec.in === "path" ? options.pathParams : options.headers);',
            "      const value = bag ? (bag as Record<string, unknown>)[spec.name] : undefined;",
            "      if (spec.required && (value === undefined || value === null)) {",
            '        throw new Error(operation + ": missing required " + spec.in + '
            '" parameter " + spec.name);',
            "      }",
            "      if (value !== undefined && spec.enum.length && !spec.enum.includes(value)) {",
            '        throw new Error(operation + ": " + spec.name + " must be one of " + '
            "JSON.stringify(spec.enum));",
            "      }",
            "    }",
            "    const bodySpec = OPERATION_BODIES[operation];",
            "    if (!bodySpec) return;",
            "    if (options.body === undefined) {",
            '      if (bodySpec.required) throw new Error(operation + ": missing required request body");',
            "      return;",
            "    }",
            "    if (!bodySpec.json || !bodySpec.schema) return;",
            "    const error = bodyError(bodySpec.schema, options.body, \"\", SCHEMAS);",
            "    if (error) throw new Error(operation + \": invalid request body at \" + error);",
            "  }",
            "",
            "  private bodyContentType(operation: string): string {",
            "    const spec = OPERATION_BODIES[operation];",
            '    return spec ? spec.contentType : "application/json";',
            "  }",
            "",
            "  private applyAuth(headers: Record<string, string>, search: URLSearchParams): void {",
            "    for (const scheme of Object.values(SECURITY_SCHEMES) as any[]) {",
            '      if (scheme.type === "http" && scheme.scheme === "bearer" && this.auth.bearerToken) {',
            '        headers["Authorization"] = "Bearer " + this.auth.bearerToken;',
            '      } else if (scheme.type === "http" && scheme.scheme === "basic" && this.auth.basic) {',
            '        const raw = this.auth.basic.user + ":" + this.auth.basic.password;',
            "        const encoded = typeof btoa === \"function\" ? btoa(raw) : "
            'Buffer.from(raw).toString("base64");',
            '        headers["Authorization"] = "Basic " + encoded;',
            '      } else if (scheme.type === "apiKey" && this.auth.apiKey) {',
            '        if (scheme.in === "query") search.set(scheme.parameter, this.auth.apiKey);',
            "        else headers[scheme.parameter] = this.auth.apiKey;",
            "      }",
            "    }",
            "  }",
            "",
            "  async call<T = unknown>(operation: string, options: RequestOptions = {}): Promise<T> {",
            "    this.validate(operation, options);",
            '    const [method, template] = operation.split(" ", 2);',
            "    const path = template.replace(/\\{([^}]+)\\}/g, (_, name: string) =>",
            "      encodeURIComponent(String((options.pathParams || {})[name] ?? \"\")));",
            '    let url = (this.baseUrl || "").replace(/\\/$/, "") + path;',
            "    const search = new URLSearchParams();",
            "    for (const [key, value] of Object.entries(options.query || {})) {",
            "      if (value !== undefined) search.set(key, String(value));",
            "    }",
            "    const headers: Record<string, string> = {",
            '      "Content-Type": this.bodyContentType(operation),',
            "      ...(options.headers || {}),",
            "    };",
            "    this.applyAuth(headers, search);",
            '    if (search.toString()) url += "?" + search.toString();',
            "    const bodySpec = OPERATION_BODIES[operation];",
            "    const body = options.body === undefined",
            "      ? undefined",
            "      : bodySpec && !bodySpec.json",
            "        ? (options.body as BodyInit)",
            "        : JSON.stringify(options.body);",
            "    const response = await this.fetcher(url, {",
            "      method,",
            "      headers,",
            "      body,",
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
        signature = "options: RequestOptions = {}"
        body = operation.get("request_body")
        if body and body.get("json") and body.get("schema"):
            rendered = _body_ts_type(body["schema"], model)
            if rendered:
                if body["required"]:
                    signature = (
                        'options: Omit<RequestOptions, "body"> & { body: '
                        + rendered
                        + " }"
                    )
                else:
                    signature = (
                        "options: RequestOptions & { body?: " + rendered + " } = {}"
                    )
        lines.append(
            f"  async {method_name}({signature}): Promise<unknown> "
            f"{{ return this.call({json.dumps(key)}, options); }}"
        )
    lines.append("}")
    return "\n".join(lines).rstrip() + "\n"


def _body_ts_type(schema, model):
    """The TypeScript type for a request body, or None if its $ref is unknown."""
    if not isinstance(schema, dict):
        return None
    ref = schema.get("$ref")
    if ref:
        name = str(ref).rsplit("/", 1)[-1]
        return _type_name(name) if name in model["schemas"] else None
    return _ts_type(schema, model["spec"])


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
        "import math as _math",
        "import base64 as _base64",
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
    lines.append(
        "SECURITY_SCHEMES: dict[str, Any] = "
        + _py_literal(
            {
                name: _scheme_public(scheme)
                for name, scheme in model["security_schemes"].items()
            }
        )
    )
    lines.append("")
    lines.append("OPERATION_PARAMETERS: dict[str, Any] = {")
    for operation in model["operations"]:
        key = f"{operation['method']} {operation['path']}"
        lines.append(f"    {key!r}: {_py_literal(operation['parameters'])},")
    lines.append("}")
    lines.append("")
    lines.append("OPERATION_SECURITY: dict[str, Any] = {")
    for operation in model["operations"]:
        key = f"{operation['method']} {operation['path']}"
        lines.append(f"    {key!r}: {operation['security']!r},")
    lines.append("}")
    lines.append("")
    lines.append("SCHEMAS: dict[str, Any] = {")
    for key in sorted(model["schemas"]):
        schema = model["schemas"][key]
        lines.append(f"    {key!r}: {_py_literal(schema)},")
    lines.append("}")
    lines.append("")
    lines.append("OPERATION_BODIES: dict[str, Any] = {")
    for operation in model["operations"]:
        if operation["request_body"] is None:
            continue
        key = f"{operation['method']} {operation['path']}"
        lines.append(f"    {key!r}: {_py_literal(_body_manifest(operation['request_body']))},")
    lines.append("}")
    lines.extend(_py_body_helpers())

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
            "    def __init__(",
            "        self,",
            '        base_url: str = "",',
            "        headers: dict[str, str] | None = None,",
            "        bearer_token: str | None = None,",
            "        api_key: str | None = None,",
            "        basic: tuple[str, str] | None = None,",
            "    ):",
            "        self.base_url = base_url.rstrip('/')",
            "        self.headers = dict(headers or {})",
            "        self.bearer_token = bearer_token",
            "        self.api_key = api_key",
            "        self.basic = basic",
            "",
            "    def _validate(self, method: str, path_template: str, "
            "path_params: dict[str, Any] | None, query: dict[str, Any] | None) -> None:",
            "        for spec in OPERATION_PARAMETERS.get(method + ' ' + path_template, []):",
            "            bag = {'query': query, 'path': path_params}.get(spec['in'])",
            "            value = (bag or {}).get(spec['name']) if bag is not None else None",
            "            if spec['required'] and value is None:",
            "                raise ValueError(",
            "                    f\"{method} {path_template}: missing required \"",
            "                    f\"{spec['in']} parameter {spec['name']}\"",
            "                )",
            "            if value is not None and spec['enum'] and value not in spec['enum']:",
            "                raise ValueError(",
            "                    f\"{method} {path_template}: {spec['name']} must be one of \"",
            "                    f\"{spec['enum']}\"",
            "                )",
            "",
            "    def _check_body(self, method: str, path_template: str, body: Any) -> None:",
            "        spec = OPERATION_BODIES.get(method + ' ' + path_template)",
            "        if spec is None:",
            "            return",
            "        if body is _UNSET:",
            "            if spec.get('required'):",
            "                raise ValueError(",
            "                    f\"{method} {path_template}: missing required request body\"",
            "                )",
            "            return",
            "        if not spec.get('json') or not spec.get('schema'):",
            "            return",
            "        error = _body_error(spec['schema'], body, '', SCHEMAS)",
            "        if error:",
            "            raise ValueError(",
            "                f\"{method} {path_template}: invalid request body at {error}\"",
            "            )",
            "",
            "    def _apply_auth(self, headers: dict[str, str], values: dict[str, Any]) -> None:",
            "        for scheme in SECURITY_SCHEMES.values():",
            "            kind = scheme.get('type')",
            "            if kind == 'http' and scheme.get('scheme') == 'bearer' and self.bearer_token:",
            "                headers['Authorization'] = 'Bearer ' + self.bearer_token",
            "            elif kind == 'http' and scheme.get('scheme') == 'basic' and self.basic:",
            "                raw = f'{self.basic[0]}:{self.basic[1]}'.encode('utf-8')",
            "                headers['Authorization'] = 'Basic ' + _base64.b64encode(raw).decode('ascii')",
            "            elif kind == 'apiKey' and self.api_key:",
            "                if scheme.get('in') == 'query':",
            "                    values[scheme.get('parameter', 'api_key')] = self.api_key",
            "                else:",
            "                    headers[scheme.get('parameter', 'Authorization')] = self.api_key",
            "",
            "    def call(",
            "        self,",
            "        method: str,",
            "        path_template: str,",
            "        path_params: dict[str, Any] | None = None,",
            "        query: dict[str, Any] | None = None,",
            "        body: Any = _UNSET,",
            "        headers: dict[str, str] | None = None,",
            "    ) -> Any:",
            "        self._validate(method, path_template, path_params, query)",
            "        self._check_body(method, path_template, body)",
            "        path = path_template",
            "        for key, value in (path_params or {}).items():",
            "            path = path.replace('{' + key + '}', _urlparse.quote(str(value), safe=''))",
            "        values = {key: value for key, value in (query or {}).items() if value is not None}",
            "        request_headers = {**self.headers, **(headers or {})}",
            "        self._apply_auth(request_headers, values)",
            "        if values:",
            "            path += ('&' if '?' in path else '?') + _urlparse.urlencode(values)",
            "        payload = None",
            "        if body is not _UNSET:",
            "            spec = OPERATION_BODIES.get(method + ' ' + path_template) or {}",
            "            if (",
            "                spec",
            "                and not spec.get('json')",
            "                and isinstance(body, (bytes, bytearray, str))",
            "            ):",
            "                payload = (",
            "                    body.encode('utf-8') if isinstance(body, str) else bytes(body)",
            "                )",
            "            else:",
            "                payload = _json.dumps(body).encode('utf-8')",
            "            request_headers.setdefault(",
            "                'Content-Type', spec.get('content_type', 'application/json')",
            "            )",
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
                "        body: Any = _UNSET,",
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


_PY_BODY_HELPERS = '''\
_UNSET = object()


def _resolve_ref(ref: Any, schemas: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    if parts[0] == "components":
        if len(parts) < 3 or parts[1] != "schemas":
            return {}
        parts = parts[2:]
    elif parts[0] == "definitions":
        parts = parts[1:]
    else:
        return {}
    node: Any = schemas
    for raw in parts:
        part = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _body_error(
    schema: Any, value: Any, path: str, schemas: dict[str, Any], depth: int = 0
) -> str | None:
    if not isinstance(schema, dict) or depth > 16:
        return None
    hops = 0
    while isinstance(schema.get("$ref"), str) and hops < 32:
        schema = _resolve_ref(schema["$ref"], schemas)
        hops += 1
    if not isinstance(schema, dict):
        return None
    where = path or "/"
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else ([kinds] if kinds else [])
    nullable = schema.get("nullable") is True or "null" in kinds
    if value is None:
        return None if nullable else where + ": null is not allowed"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        return where + ": must be one of " + _json.dumps(enum)
    if isinstance(schema.get("allOf"), list):
        for part in schema["allOf"]:
            error = _body_error(part, value, path, schemas, depth + 1)
            if error:
                return error
    union = schema.get("anyOf")
    if not isinstance(union, list):
        union = schema.get("oneOf")
    if isinstance(union, list) and union:
        if not any(
            _body_error(part, value, path, schemas, depth + 1) is None
            for part in union
        ):
            return where + ": does not match any allowed schema"
    kind = next((item for item in kinds if item != "null"), "")
    if kind == "object" or (not kind and isinstance(schema.get("properties"), dict)):
        if not isinstance(value, dict):
            return where + ": expected object"
        for key in schema.get("required") or []:
            if key not in value:
                return where + ": missing required property " + _json.dumps(key)
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        for key, item in value.items():
            if key in properties:
                error = _body_error(
                    properties[key], item, path + "/" + str(key), schemas, depth + 1
                )
                if error:
                    return error
            elif schema.get("additionalProperties") is False:
                return where + ": unexpected property " + _json.dumps(key)
            elif isinstance(schema.get("additionalProperties"), dict):
                error = _body_error(
                    schema["additionalProperties"],
                    item,
                    path + "/" + str(key),
                    schemas,
                    depth + 1,
                )
                if error:
                    return error
    elif kind == "array" or (not kind and isinstance(schema.get("items"), dict)):
        if not isinstance(value, list):
            return where + ": expected array"
        minimum = schema.get("minItems")
        if isinstance(minimum, int) and len(value) < minimum:
            return where + ": expected at least " + str(minimum) + " item(s)"
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and len(value) > maximum:
            return where + ": expected at most " + str(maximum) + " item(s)"
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                error = _body_error(
                    schema["items"], item, path + "/" + str(index), schemas, depth + 1
                )
                if error:
                    return error
    elif kind == "string":
        if not isinstance(value, str):
            return where + ": expected string"
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            return where + ": expected at least " + str(minimum) + " character(s)"
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and len(value) > maximum:
            return where + ": expected at most " + str(maximum) + " character(s)"
    elif kind in ("integer", "number"):
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(value, float) and not _math.isfinite(value):
            numeric = False
        if not numeric or (kind == "integer" and not isinstance(value, int)):
            return where + ": expected " + kind
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            return where + ": must be >= " + str(minimum)
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            return where + ": must be <= " + str(maximum)
    elif kind == "boolean":
        if not isinstance(value, bool):
            return where + ": expected boolean"
    return None
'''


def _py_body_helpers():
    return _PY_BODY_HELPERS.splitlines()


# -- SQL migration -------------------------------------------------------


SQL_DIALECTS = {"postgres", "sqlite", "mysql"}

# Each dialect maps the logical column categories below to a concrete type.
_SQL_TYPES = {
    "postgres": {
        "json": "JSONB",
        "uuid": "UUID",
        "date": "DATE",
        "datetime": "TIMESTAMPTZ",
        "int": "BIGINT",
        "int32": "INTEGER",
        "number": "DOUBLE PRECISION",
        "bool": "BOOLEAN",
        "text": "TEXT",
    },
    "sqlite": {
        "json": "TEXT",
        "uuid": "TEXT",
        "date": "TEXT",
        "datetime": "TEXT",
        "int": "INTEGER",
        "int32": "INTEGER",
        "number": "REAL",
        "bool": "INTEGER",
        "text": "TEXT",
    },
    "mysql": {
        "json": "JSON",
        "uuid": "CHAR(36)",
        "date": "DATE",
        "datetime": "DATETIME",
        "int": "BIGINT",
        "int32": "INT",
        "number": "DOUBLE",
        "bool": "TINYINT(1)",
        "text": "VARCHAR(255)",
    },
}

# Categories that make sense as a single-column primary key.
_SQL_KEYABLE = {"text", "uuid", "int", "int32"}


def _sql_category(schema, spec, depth=0):
    """Reduce a schema to one of the logical column categories in `_SQL_TYPES`."""
    if not isinstance(schema, dict) or depth > 8:
        return "json"
    if schema.get("$ref"):
        return _sql_category(_resolve(spec, schema["$ref"]), spec, depth + 1)
    if schema.get("enum"):
        return "text"
    for union in ("oneOf", "anyOf"):
        if isinstance(schema.get(union), list) and schema[union]:
            return _sql_category(schema[union][0], spec, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    if kind == "array" or isinstance(schema.get("items"), dict):
        return "json"
    if kind == "object" or isinstance(schema.get("properties"), dict):
        return "json"
    fmt = schema.get("format")
    if kind == "string":
        return {"uuid": "uuid", "date": "date", "date-time": "datetime"}.get(fmt, "text")
    if kind == "integer":
        return "int" if fmt in ("int64", None) else "int32"
    if kind == "number":
        return "number"
    if kind == "boolean":
        return "bool"
    return "json"


def _sql_type(schema, spec, dialect="postgres", depth=0):
    category = _sql_category(schema, spec, depth)
    return _SQL_TYPES[dialect][category]


def render_sql(name, version, sha, model, dialect="postgres"):
    if dialect not in SQL_DIALECTS:
        raise AghubError(f"unknown SQL dialect '{dialect}'")
    lines = [
        f"-- Generated by aghub from {name}@{version} (sha256 {sha or 'unknown'}).",
        f"-- Dialect: {dialect}. Review before applying; a starting migration, not a live diff.",
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
            category = _sql_category(prop_schema, model["spec"])
            kind = _SQL_TYPES[dialect][category]
            constraints = ""
            if column == "id" and category in _SQL_KEYABLE:
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
    parameters = pprint.pformat(
        {
            (operation["method"], operation["path"]): operation["parameters"]
            for operation in model["operations"]
        },
        width=100,
        sort_dicts=False,
    )
    security = pprint.pformat(
        {
            (operation["method"], operation["path"]): operation["security"]
            for operation in model["operations"]
        },
        width=100,
        sort_dicts=False,
    )
    schemes = pprint.pformat(
        {name: _scheme_public(scheme) for name, scheme in model["security_schemes"].items()},
        width=100,
        sort_dicts=False,
    )
    bodies = pprint.pformat(
        {
            (operation["method"], operation["path"]): _body_manifest(
                operation["request_body"]
            )
            for operation in model["operations"]
            if operation["request_body"] is not None
        },
        width=100,
        sort_dicts=False,
    )
    schemas = pprint.pformat(model["schemas"], width=100, sort_dicts=False)
    body_helpers = _PY_BODY_HELPERS.replace("_json.", "json.").replace("_math.", "math.")
    template = '''#!/usr/bin/env python3
"""Mock server generated by aghub from $CONTRACT$ (sha256 $SHA$)."""

import argparse
import json
import math
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROUTES = $ROUTES$

OPERATIONS = {
$OPERATIONS$
}

RESPONSES = $RESPONSES$

PARAMETERS = $PARAMETERS$

BODIES = $BODIES$

SECURITY = $SECURITY$

SECURITY_SCHEMES = $SCHEMES$

SCHEMAS = $SCHEMAS$

$BODY_HELPERS$


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


def check_auth(method, route, headers, query):
    required = SECURITY.get((method, route)) or []
    if not required:
        return None
    for name in required:
        scheme = SECURITY_SCHEMES.get(name) or {}
        kind = scheme.get("type")
        if kind == "apiKey":
            parameter = scheme.get("parameter")
            if scheme.get("in") == "query":
                if parameter in query:
                    return None
            elif headers.get(parameter):
                return None
        elif headers.get("Authorization"):
            return None
    return {"error": "unauthorized", "required": required}


def validate_params(method, route, query):
    for spec in PARAMETERS.get((method, route), []):
        if spec["in"] != "query":
            continue
        value = query.get(spec["name"])
        if spec["required"] and value is None:
            return {"error": "missing required parameter", "parameter": spec["name"]}
        allowed = [str(item) for item in spec["enum"]]
        if value is not None and allowed and str(value) not in allowed:
            return {
                "error": "invalid parameter",
                "parameter": spec["name"],
                "allowed": spec["enum"],
            }
    return None


def validate_body(method, route, raw, content_type=""):
    spec = BODIES.get((method, route))
    if spec is None:
        return None
    if not raw:
        if spec["required"]:
            return {"error": "missing request body", "required": True}
        return None
    if not spec["json"] or not spec["schema"]:
        return None
    if content_type and "json" not in content_type.lower():
        return {"error": "unsupported content type", "expected": spec["content_type"]}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"error": "invalid JSON body"}
    problem = _body_error(spec["schema"], value, "", SCHEMAS)
    if problem:
        where, _, detail = problem.partition(": ")
        return {"error": "invalid request body", "path": where, "detail": detail}
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
        parsed = urlparse(self.path)
        path = parsed.path
        result = match(self.command, path)
        if result is None:
            self._send(404, {"error": "no such route", "path": path})
            return
        if "error" in result:
            self._send(405, result)
            return
        route = result["route"]
        raw_query = parse_qs(parsed.query)
        query = {key: value[0] if len(value) == 1 else value for key, value in raw_query.items()}
        auth_error = check_auth(self.command, route, self.headers, query)
        if auth_error is not None:
            self._send(401, auth_error)
            return
        param_error = validate_params(self.command, route, query)
        if param_error is not None:
            self._send(400, param_error)
            return
        length = self.headers.get("Content-Length")
        raw = b""
        if length:
            try:
                size = int(length)
            except ValueError:
                size = 0
            if size > 1048576:
                self._send(413, {"error": "request body too large"})
                return
            raw = self.rfile.read(size) if size else b""
        body_error = validate_body(
            self.command, route, raw, self.headers.get("Content-Type", "")
        )
        if body_error is not None:
            self._send(400, body_error)
            return
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
        .replace("$PARAMETERS$", parameters)
        .replace("$BODIES$", bodies)
        .replace("$SECURITY$", security)
        .replace("$SCHEMES$", schemes)
        .replace("$SCHEMAS$", schemas)
        .replace("$BODY_HELPERS$", body_helpers)
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
