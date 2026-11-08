import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dcc_skill_runtime.catalog import SkillCatalog
from dcc_skill_runtime.mcp_server import SkillRuntimeMcpServer
from dcc_skill_runtime.models import HostCallResponse, HostInfo
from dcc_skill_runtime.runtime import SkillRuntime
from dcc_skill_runtime.security import SkillSignature
from dcc_skill_runtime.session import SessionStore
import yaml


class FakeHostClient:
    def list_hosts(self):
        return (
            HostInfo(
                host_id="maya-001",
                host_type="maya",
                endpoint="pipe://maya-001",
                capabilities=("plan.validate",),
                status="online",
                last_seen=100.0,
            ),
        )

    def call_skill(self, request):
        return HostCallResponse("success", "skill.run", request.run_id, {"created": 1})


class McpServerTests(unittest.TestCase):
    def test_registers_stable_read_only_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            server = _build_server(Path(temp))

            tools = asyncio.run(server.mcp.list_tools())

            self.assertEqual(
                {tool.name for tool in tools},
                {"skill_list", "skill_describe", "host_list", "skill_run"},
            )

    def test_skill_list_only_returns_index_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "skills" / "maya.scene.plan"
            root.mkdir(parents=True)
            (root / "skill.yaml").write_text(
                "id: maya.scene.plan\n"
                "version: 1.0.0\n"
                "description: Maya 场景计划\n"
                "hosts: [maya]\n"
                "entry:\n"
                "  module: code.entry\n"
                "  function: run\n"
                "actions: [skill.run]\n"
                "requires: []\n"
                "permissions: {}\n",
                encoding="utf-8",
            )
            server = _build_server(Path(temp))

            result = server.skill_list()

            self.assertEqual(result[0]["skill_id"], "maya.scene.plan")
            self.assertNotIn("instructions", result[0])

    def test_host_list_returns_explicit_host_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            server = _build_server(Path(temp))

            result = server.host_list()

            self.assertEqual(result[0]["host_id"], "maya-001")
            self.assertEqual(result[0]["status"], "online")
            self.assertEqual(result[0]["capabilities"], ["plan.validate"])

    def test_skill_describe_reads_signed_instructions_without_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root, trusted_keys = _create_signed_skill(Path(temp))
            catalog = SkillCatalog(root.parent, trusted_keys)
            runtime = SkillRuntime(catalog, SessionStore(Path(temp) / "session.db"), FakeHostClient())
            runtime.start()
            server = SkillRuntimeMcpServer(runtime, host_client=FakeHostClient())

            result = server.skill_describe("maya.scene.plan")

            self.assertIn("Maya plan skill", result["instructions"])
            self.assertEqual(result["entry"], {"module": "code.entry", "function": "run"})
            self.assertNotIn("code", result)

    def test_skill_run_returns_queued_run_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root, trusted_keys = _create_signed_skill(Path(temp))
            catalog = SkillCatalog(root.parent, trusted_keys)
            sessions = SessionStore(Path(temp) / "session.db")
            client = FakeHostClient()
            runtime = SkillRuntime(catalog, sessions, client)
            runtime.start()
            session = sessions.create_session("project-a", "maya-001")
            server = SkillRuntimeMcpServer(runtime, host_client=client)

            result = server.skill_run(
                session.session_id,
                "project-a",
                "maya-001",
                "maya.scene.plan",
                {"nodes": []},
            )

            self.assertEqual(result["status"], "queued")
            self.assertTrue(result["run_id"])
            runtime.stop()

    def test_resources_include_catalog_and_skill_template(self):
        with tempfile.TemporaryDirectory() as temp:
            server = _build_server(Path(temp))

            resources = asyncio.run(server.mcp.list_resources())
            templates = asyncio.run(server.mcp.list_resource_templates())

            self.assertEqual([str(resource.uri) for resource in resources], ["skill://catalog"])
            self.assertEqual(
                [str(template.uriTemplate) for template in templates],
                ["skill://{skill_id}"],
            )
            catalog_text = server._catalog_resource()
            self.assertEqual(json.loads(catalog_text), {"skills": []})


def _build_server(temp: Path) -> SkillRuntimeMcpServer:
    catalog = SkillCatalog(temp / "skills")
    runtime = SkillRuntime(catalog, SessionStore(temp / "session.db"), FakeHostClient())
    runtime.start()
    return SkillRuntimeMcpServer(runtime, host_client=FakeHostClient())


def _create_signed_skill(base: Path):
    root = base / "maya.scene.plan"
    code_root = root / "code"
    code_root.mkdir(parents=True)
    manifest_text = (
        "id: maya.scene.plan\n"
        "version: 1.0.0\n"
        "description: Maya 场景计划\n"
        "hosts: [maya]\n"
        "entry:\n"
        "  module: code.entry\n"
        "  function: run\n"
        "actions: [skill.run]\n"
        "requires: []\n"
        "permissions: {}\n"
    )
    (root / "skill.yaml").write_text(manifest_text, encoding="utf-8")
    (root / "SKILL.md").write_text("# plan\nMaya plan skill", encoding="utf-8")
    (code_root / "entry.py").write_text("def run(context, args):\n    return args\n", encoding="utf-8")
    manifest = yaml.safe_load(manifest_text)
    files = {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in ("skill.yaml", "SKILL.md", "code/entry.py")
    }
    private_key = Ed25519PrivateKey.generate()
    payload = SkillSignature._canonical_payload(manifest, dict(sorted(files.items())))
    signature = private_key.sign(payload)
    (root / "signature.json").write_text(
        json.dumps(
            {
                "algorithm": "ed25519",
                "key_id": "test",
                "files": files,
                "signature": base64.b64encode(signature).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )
    return root, {"test": private_key.public_key().public_bytes_raw()}


if __name__ == "__main__":
    unittest.main()
