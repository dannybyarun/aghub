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


if __name__ == "__main__":
    unittest.main()
