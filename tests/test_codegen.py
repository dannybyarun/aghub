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


if __name__ == "__main__":
    unittest.main()
