import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub import codegen  # noqa: E402
from aghub.errors import AghubError  # noqa: E402

SPEC = """\
openapi: 3.0.0
info:
  title: Items
  version: "1"
paths:
  /items:
    get:
      operationId: listItems
      summary: List items
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: "#/components/schemas/Item"
  /items/{id}:
    get:
      summary: Get one item
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Item"
components:
  schemas:
    Item:
      type: object
      required: [id, name]
      properties:
        id:
          type: string
        name:
          type: string
        state:
          type: string
          enum: [new, done]
        tags:
          type: array
          items:
            type: string
        meta:
          type: object
          properties:
            count:
              type: integer
"""


class CodegenTests(unittest.TestCase):
    def setUp(self):
        self.meta = {"sha256": "a" * 64}
        self.spec = codegen.load_spec(SPEC.encode())

    def test_typescript_routes_and_schemas(self):
        files = codegen.generate("api", 1, self.meta, self.spec)
        ts = files["api.ts"]
        self.assertIn('"/items"', ts)
        self.assertIn('"/items/{id}"', ts)
        self.assertIn("export interface Item", ts)
        self.assertIn("id: string;", ts)
        self.assertIn("name: string;", ts)
        self.assertIn('state?: "new" | "done";', ts)
        self.assertIn("meta?: {", ts)
        self.assertIn("getItemsId", ts)

    def test_python_and_mock_compile(self):
        files = codegen.generate("api", 1, self.meta, self.spec)
        py = files["api.py"]
        self.assertIn("ROUTES = (", py)
        self.assertIn("class Item:", py)
        self.assertIn("Literal['new', 'done']", py)
        self.assertIn("class Client:", py)
        self.assertIn("def list_items(", py)
        compile(py, "api.py", "exec")
        mock = files["api_mock.py"]
        self.assertIn("RESPONSES =", mock)
        self.assertIn("listItems", mock)
        compile(mock, "api_mock.py", "exec")

    def test_typescript_client_and_sql_migration(self):
        files = codegen.generate("api", 1, self.meta, self.spec)
        ts = files["api.ts"]
        self.assertIn("export class ContractClient", ts)
        self.assertIn("async listItems(", ts)
        sql = files["api.sql"]
        self.assertIn('CREATE TABLE IF NOT EXISTS "item"', sql)
        self.assertIn('"id" TEXT PRIMARY KEY', sql)
        self.assertIn('"name" TEXT NOT NULL', sql)
        self.assertIn('"tags" JSONB', sql)
        self.assertIn("GET /items -> listItems", sql)
        self.assertEqual(set(codegen.generate("api", 1, self.meta, self.spec, "sql")), {"api.sql", "manifest.json"})

    def test_deterministic_manifest_and_target_filter(self):
        first = codegen.generate("api", 1, self.meta, self.spec)
        second = codegen.generate("api", 1, self.meta, self.spec)
        self.assertEqual(first, second)
        manifest = json.loads(first["manifest.json"])
        self.assertEqual(manifest["contract"], "api@1")
        self.assertEqual(manifest["routes"], ["/items", "/items/{id}"])
        self.assertEqual(
            [item["operation_id"] for item in manifest["operations"]],
            ["listItems", "getItemsId"],
        )
        ts_only = codegen.generate("api", 1, self.meta, self.spec, "ts")
        self.assertEqual(set(ts_only), {"api.ts", "manifest.json"})

    def test_structural_diff(self):
        new_spec = copy.deepcopy(self.spec)
        new_spec["paths"]["/items"]["get"]["summary"] = "List all items"
        new_spec["paths"]["/tags"] = {"get": {"summary": "List tags"}}
        del new_spec["paths"]["/items/{id}"]
        new_spec["components"]["schemas"]["Tag"] = {
            "type": "object",
            "properties": {"id": {"type": "string"}},
        }
        del new_spec["components"]["schemas"]["Item"]["properties"]["state"]

        diff = codegen.diff_specs(self.spec, new_spec)
        self.assertEqual(diff["routes_added"], ["/tags"])
        self.assertEqual(diff["routes_removed"], ["/items/{id}"])
        self.assertEqual(
            diff["operations_added"], [{"method": "GET", "path": "/tags"}]
        )
        self.assertEqual(
            diff["operations_removed"], [{"method": "GET", "path": "/items/{id}"}]
        )
        self.assertEqual(
            diff["operations_changed"],
            [{"method": "GET", "path": "/items", "fields": ["summary", "response"]}],
        )
        self.assertEqual(diff["schemas_added"], ["Tag"])
        self.assertEqual(diff["schemas_removed"], [])
        self.assertEqual(
            diff["schemas_changed"]["Item"]["properties_removed"], ["state"]
        )

    def test_yaml_dates_are_kept_as_strings(self):
        spec = codegen.load_spec(
            b"openapi: 3.0.0\n"
            b"info: {title: Items, version: '1'}\n"
            b"paths:\n"
            b"  /items:\n"
            b"    get:\n"
            b"      responses:\n"
            b"        '200':\n"
            b"          description: ok\n"
            b"          content:\n"
            b"            application/json:\n"
            b"              example: 2026-01-01\n"
        )
        example = spec["paths"]["/items"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["example"]
        self.assertEqual(example, "2026-01-01")
        self.assertIsInstance(example, str)
        json.dumps(spec)  # the board snapshots contracts as JSON

    def test_invalid_artifact_is_rejected(self):
        with self.assertRaises(AghubError):
            codegen.load_spec(b"[1, 2, 3]")
        with self.assertRaises(AghubError):
            codegen.load_spec(b"openapi: [")


SECSPEC = """\
openapi: 3.0.0
info:
  title: Items
  version: "1"
security:
  - bearerAuth: []
paths:
  /items:
    get:
      operationId: listItems
      parameters:
        - name: state
          in: query
          required: true
          schema:
            type: string
            enum: [new, done]
        - name: limit
          in: query
          schema:
            type: integer
      responses:
        "200":
          description: ok
          content:
            application/json:
              example: {ok: true}
  /public:
    get:
      operationId: getPublic
      security: []
      responses:
        "200":
          description: ok
          content:
            application/json:
              example: {public: true}
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
  schemas:
    Item:
      type: object
      required: [id]
      properties:
        id:
          type: string
        count:
          type: integer
        created:
          type: string
          format: date-time
        meta:
          type: object
"""


def _exec_module(source, name):
    import types
    import uuid

    unique = f"{name}_{uuid.uuid4().hex}"
    module = types.ModuleType(unique)
    sys.modules[unique] = module
    try:
        exec(compile(source, name, "exec"), module.__dict__)
    finally:
        sys.modules.pop(unique, None)
    return module.__dict__


class CodegenDeepTests(unittest.TestCase):
    def setUp(self):
        self.meta = {"sha256": "a" * 64}
        self.spec = codegen.load_spec(SECSPEC.encode())

    def test_manifest_records_parameters_and_security(self):
        manifest = json.loads(codegen.generate("api", 1, self.meta, self.spec)["manifest.json"])
        self.assertEqual(manifest["security_schemes"], {"bearerAuth": "http"})
        by_op = {item["operation_id"]: item for item in manifest["operations"]}
        self.assertEqual(by_op["listItems"]["security"], ["bearerAuth"])
        self.assertEqual(by_op["getPublic"]["security"], [])
        names = {param["name"]: param for param in by_op["listItems"]["parameters"]}
        self.assertTrue(names["state"]["required"])
        self.assertFalse(names["limit"]["required"])

    def test_mock_validates_params_and_auth(self):
        mock = _exec_module(
            codegen.generate("api", 1, self.meta, self.spec, "mock")["api_mock.py"],
            "api_mock",
        )
        # Required query parameter missing -> 400-style error.
        self.assertIsNotNone(mock["validate_params"]("GET", "/items", {}))
        # Bad enum value -> error.
        self.assertIsNotNone(mock["validate_params"]("GET", "/items", {"state": "bogus"}))
        # Valid value -> no error.
        self.assertIsNone(mock["validate_params"]("GET", "/items", {"state": "new"}))
        # Secured route with no credential -> 401-style error.
        self.assertIsNotNone(mock["check_auth"]("GET", "/items", {}, {}))
        self.assertIsNone(
            mock["check_auth"]("GET", "/items", {"Authorization": "Bearer t"}, {})
        )
        # Explicitly public route -> never requires auth.
        self.assertIsNone(mock["check_auth"]("GET", "/public", {}, {}))

    def test_python_client_applies_auth_and_validates(self):
        py = codegen.generate("api", 1, self.meta, self.spec, "py")["api.py"]
        self.assertIn("bearer_token", py)
        self.assertIn("SECURITY_SCHEMES", py)
        self.assertIn("OPERATION_PARAMETERS", py)
        module = _exec_module(py, "api")
        client = module["Client"](base_url="http://x", bearer_token="secret")
        headers, values = {}, {}
        client._apply_auth(headers, values)
        self.assertEqual(headers["Authorization"], "Bearer secret")
        with self.assertRaises(ValueError):
            client._validate("GET", "/items", None, {})  # missing required `state`
        client._validate("GET", "/items", None, {"state": "new"})  # ok

    def test_typescript_client_has_auth_and_parameter_metadata(self):
        ts = codegen.generate("api", 1, self.meta, self.spec, "ts")["api.ts"]
        self.assertIn("export interface AuthOptions", ts)
        self.assertIn("SECURITY_SCHEMES", ts)
        self.assertIn("OPERATION_PARAMETERS", ts)
        self.assertIn("private applyAuth", ts)
        self.assertIn("missing required", ts)

    def test_sql_dialects(self):
        postgres = codegen.generate("api", 1, self.meta, self.spec, "sql", "postgres")["api.sql"]
        self.assertIn('"meta" JSONB', postgres)
        self.assertIn('"created" TIMESTAMPTZ', postgres)
        self.assertIn('"id" TEXT PRIMARY KEY', postgres)

        sqlite = codegen.generate("api", 1, self.meta, self.spec, "sql", "sqlite")["api.sql"]
        self.assertIn('"meta" TEXT', sqlite)
        self.assertIn('"count" INTEGER', sqlite)
        self.assertIn("Dialect: sqlite", sqlite)

        mysql = codegen.generate("api", 1, self.meta, self.spec, "sql", "mysql")["api.sql"]
        self.assertIn('"meta" JSON', mysql)
        self.assertIn('"id" VARCHAR(255) PRIMARY KEY', mysql)
        self.assertIn('"created" DATETIME', mysql)

        with self.assertRaises(AghubError):
            codegen.generate("api", 1, self.meta, self.spec, "sql", "oracle")


BODYSPEC = """\
openapi: 3.0.0
info:
  title: Items
  version: "1"
paths:
  /items:
    post:
      operationId: createItem
      summary: Create an item
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/NewItem"
      responses:
        "201":
          description: created
          content:
            application/json:
              example: {id: it-1, name: widget}
  /items/{id}:
    put:
      operationId: replaceItem
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      requestBody:
        required: false
        content:
          application/json:
            schema:
              type: object
              required: [name]
              properties:
                name:
                  type: string
      responses:
        "200":
          description: ok
          content:
            application/json:
              example: {ok: true}
  /upload:
    post:
      operationId: uploadBlob
      requestBody:
        required: true
        content:
          application/octet-stream:
            schema:
              type: string
              format: binary
      responses:
        "202":
          description: accepted
          content:
            application/json:
              example: {queued: true}
components:
  schemas:
    NewItem:
      type: object
      additionalProperties: false
      required: [name]
      properties:
        name:
          type: string
          minLength: 1
        qty:
          type: integer
          minimum: 1
          maximum: 9
        state:
          type: string
          enum: [new, done]
        note:
          type: string
          nullable: true
        tags:
          type: array
          minItems: 1
          items:
            type: string
        meta:
          type: object
          additionalProperties: false
          properties:
            source:
              type: string
              enum: [web, api]
"""


SWAGGER_BODY = """\
swagger: "2.0"
info:
  title: Items
  version: "1"
consumes: [application/json]
paths:
  /items:
    post:
      operationId: createItem
      parameters:
        - name: body
          in: body
          required: true
          schema:
            $ref: "#/definitions/NewItem"
      responses:
        "201":
          description: created
definitions:
  NewItem:
    type: object
    required: [name]
    properties:
      name:
        type: string
"""


class CodegenRequestBodyTests(unittest.TestCase):
    def setUp(self):
        self.meta = {"sha256": "b" * 64}
        self.spec = codegen.load_spec(BODYSPEC.encode())
        self.files = codegen.generate("api", 1, self.meta, self.spec)

    def test_manifest_records_request_bodies(self):
        self.assertEqual(
            self.files, codegen.generate("api", 1, self.meta, self.spec)
        )
        manifest = json.loads(self.files["manifest.json"])
        by_op = {item["operation_id"]: item for item in manifest["operations"]}
        body = by_op["createItem"]["request_body"]
        self.assertTrue(body["required"])
        self.assertEqual(body["content_type"], "application/json")
        self.assertTrue(body["json"])
        self.assertEqual(
            body["schema"], {"$ref": "#/components/schemas/NewItem"}
        )
        self.assertFalse(by_op["replaceItem"]["request_body"]["required"])
        upload = by_op["uploadBlob"]["request_body"]
        self.assertTrue(upload["required"])
        self.assertFalse(upload["json"])

    def test_python_client_rejects_invalid_bodies(self):
        module = _exec_module(self.files["api.py"], "api_body")
        client = module["Client"]("http://127.0.0.1:1")
        client._check_body("POST", "/items", {"name": "widget"})
        client._check_body("PUT", "/items/{id}", {"name": "widget"})
        client._check_body("PUT", "/items/{id}", module["_UNSET"])
        client._check_body("POST", "/items", {"name": "widget", "note": None})
        cases = [
            ({"qty": 1}, "missing required property"),
            ({"name": "w", "qty": 0}, "must be >= 1"),
            ({"name": "w", "qty": 10}, "must be <= 9"),
            ({"name": "w", "state": "bogus"}, "must be one of"),
            ({"name": "", "tags": ["a"]}, "at least 1 character"),
            ({"name": "w", "tags": []}, "at least 1 item"),
            ({"name": "w", "tags": [1]}, "expected string"),
            ({"name": "w", "meta": {"source": "cli"}}, "must be one of"),
            ({"name": "w", "meta": {"other": 1}}, "unexpected property"),
            ({"name": "w", "extra": True}, "unexpected property"),
            ({"name": "w", "note": 5}, "expected string"),
        ]
        for body, expected in cases:
            with self.assertRaises(ValueError) as caught:
                client._check_body("POST", "/items", body)
            self.assertIn(expected, str(caught.exception))
        with self.assertRaises(ValueError) as missing:
            client._check_body("POST", "/items", {"qty": 1})
        self.assertIn(
            "/name: missing required property", str(missing.exception)
        )
        with self.assertRaises(ValueError) as caught:
            client._check_body("POST", "/items", module["_UNSET"])
        self.assertIn("missing required request body", str(caught.exception))
        # Non-JSON bodies are presence-checked, not schema-checked.
        client._check_body("POST", "/upload", b"raw")
        with self.assertRaises(ValueError):
            client._check_body("POST", "/upload", module["_UNSET"])

    def test_mock_validates_bodies(self):
        mock = _exec_module(self.files["api_mock.py"], "api_body_mock")
        self.assertIsNone(
            mock["validate_body"](
                "POST", "/items", b'{"name":"widget"}', "application/json"
            )
        )
        self.assertEqual(
            mock["validate_body"]("POST", "/items", b"", "application/json")[
                "error"
            ],
            "missing request body",
        )
        self.assertEqual(
            mock["validate_body"]("POST", "/items", b"nope", "application/json")[
                "error"
            ],
            "invalid JSON body",
        )
        problem = mock["validate_body"](
            "POST", "/items", b'{"qty":2}', "application/json"
        )
        self.assertEqual(problem["error"], "invalid request body")
        self.assertEqual(problem["path"], "/name")
        self.assertIn("name", problem["detail"])
        self.assertEqual(
            mock["validate_body"]("POST", "/items", b"{}", "text/plain")[
                "error"
            ],
            "unsupported content type",
        )
        self.assertIsNone(
            mock["validate_body"](
                "POST", "/upload", b"binary", "application/octet-stream"
            )
        )
        self.assertIsNone(
            mock["validate_body"]("PUT", "/items/{id}", b"", "application/json")
        )

    def test_typescript_body_types_and_validator(self):
        ts = self.files["api.ts"]
        self.assertIn("export interface BodySpec", ts)
        self.assertIn("export const OPERATION_BODIES", ts)
        self.assertIn("function bodyError(", ts)
        self.assertIn("function resolveRef(", ts)
        self.assertIn(
            'async createItem(options: Omit<RequestOptions, "body"> & '
            "{ body: NewItem })",
            ts,
        )
        self.assertIn("async replaceItem(options: RequestOptions & { body?: {", ts)
        self.assertIn("invalid request body at", ts)
        self.assertIn("missing required request body", ts)
        self.assertIn('throw new Error(operation + ": invalid request body at "', ts)
        # Non-JSON bodies skip JSON.stringify and are sent as-is.
        self.assertIn("bodySpec && !bodySpec.json", ts)
        self.assertIn("(options.body as BodyInit)", ts)

    def test_live_client_and_mock_agree(self):
        import threading
        import urllib.error
        import urllib.request

        module = _exec_module(self.files["api.py"], "api_live")
        mock = _exec_module(self.files["api_mock.py"], "api_live_mock")
        server = mock["ThreadingHTTPServer"](("127.0.0.1", 0), mock["Handler"])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            client = module["Client"](base)
            self.assertEqual(
                client.create_item(body={"name": "widget", "tags": ["a"]}),
                {"id": "it-1", "name": "widget"},
            )
            # A non-JSON body is sent verbatim, not JSON-encoded.
            self.assertEqual(client.upload_blob(body=b"raw-bytes"), {"queued": True})
            self.assertEqual(client.upload_blob(body="raw-text"), {"queued": True})
            with self.assertRaises(ValueError):
                client.create_item(body={"qty": 1})
            request = urllib.request.Request(
                base + "/items",
                data=json.dumps({"qty": 1}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 400)
            payload = json.load(caught.exception)
            caught.exception.close()
            self.assertEqual(payload["error"], "invalid request body")
            self.assertEqual(payload["path"], "/name")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_diff_reports_request_body_changes(self):
        changed = copy.deepcopy(self.spec)
        changed["paths"]["/items"]["post"]["requestBody"]["required"] = False
        diff = codegen.diff_specs(self.spec, changed)
        self.assertEqual(
            diff["operations_changed"],
            [{"method": "POST", "path": "/items", "fields": ["request_body"]}],
        )
        changed_schema = copy.deepcopy(self.spec)
        changed_schema["components"]["schemas"]["NewItem"]["properties"]["name"][
            "minLength"
        ] = 3
        diff = codegen.diff_specs(self.spec, changed_schema)
        self.assertEqual(
            diff["schemas_changed"]["NewItem"]["properties_changed"], ["name"]
        )


class SwaggerRequestBodyTests(unittest.TestCase):
    def test_swagger2_body_parameter_becomes_a_request_body(self):
        spec = codegen.load_spec(SWAGGER_BODY.encode())
        operation = codegen.extract(spec)["operations"][0]
        # An `in: body` parameter is not a query/path/header parameter.
        self.assertEqual(operation["parameters"], [])
        body = operation["request_body"]
        self.assertTrue(body["required"])
        self.assertEqual(body["content_type"], "application/json")
        self.assertEqual(body["schema"], {"$ref": "#/definitions/NewItem"})
        py = codegen.generate("api", 1, {"sha256": ""}, spec, "py")["api.py"]
        module = _exec_module(py, "api_swagger_body")
        client = module["Client"]("http://127.0.0.1:1")
        client._check_body("POST", "/items", {"name": "x"})
        with self.assertRaises(ValueError):
            client._check_body("POST", "/items", module["_UNSET"])


if __name__ == "__main__":
    unittest.main()