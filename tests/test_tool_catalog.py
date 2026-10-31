import json
import subprocess
import sys
import unittest

from jarvis.tool_catalog import build_tool_catalog
from jarvis.tools import DEFINITIONS


class ToolCatalogTests(unittest.TestCase):
    def test_catalog_tracks_registered_schemas_and_real_agent_scopes(self):
        from jarvis.orchestration import PERSONAL_TOOLS, SPECIALISTS

        catalog = build_tool_catalog(DEFINITIONS)
        tools = {item["name"]: item for item in catalog if item["kind"] == "tool"}
        self.assertEqual(set(tools), {item["function"]["name"] for item in DEFINITIONS})
        scoped = set(PERSONAL_TOOLS)
        for _, _, names in SPECIALISTS.values():
            scoped.update(names)
        self.assertEqual(set(tools), scoped)
        for definition in DEFINITIONS:
            function = definition["function"]
            item = tools[function["name"]]
            self.assertEqual(item["parameters"], function["parameters"])
            self.assertEqual(item["description"], function["description"])
            self.assertTrue(item["agents"])
        self.assertEqual(
            {item["name"] for item in catalog if item["kind"] == "delegation"},
            {f"delegate_{role}" for role in SPECIALISTS},
        )
        tools["fetch_page"]["parameters"]["properties"].clear()
        fresh = {item["name"]: item for item in build_tool_catalog(DEFINITIONS)}
        self.assertIn("url", fresh["fetch_page"]["parameters"]["properties"])

    def test_native_catalog_does_not_claim_delegation_or_load_maf(self):
        catalog = build_tool_catalog(DEFINITIONS, orchestrator="native")
        self.assertEqual(len(catalog), len(DEFINITIONS))
        self.assertTrue(all(item["agents"] == ["总控助手"] for item in catalog))
        command = (
            "import sys,json; from jarvis.tools import DEFINITIONS; "
            "from jarvis.tool_catalog import build_tool_catalog; "
            "build_tool_catalog(DEFINITIONS); "
            "print(json.dumps([m for m in sys.modules if "
            "m.startswith(('agent_framework','sherpa_onnx'))]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", command],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        self.assertEqual(json.loads(result.stdout), [])
