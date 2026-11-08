import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import yaml

from dcc_skill_runtime.catalog import SkillCatalog, SkillCatalogError
from dcc_skill_runtime.knowledge import KnowledgeIndex
from dcc_skill_runtime.models import HostCallRequest, HostCallResponse
from dcc_skill_runtime.runtime import SkillRuntime, SkillRuntimeError
from dcc_skill_runtime.security import SkillSecurityError, SkillSignature, SkillSigner
from dcc_skill_runtime.transport import HostWebSocketClient
from dcc_skill_runtime.session import SessionError, SessionStore


class FakeEmbedding:
    def embed(self, text):
        return (1.0, 0.0) if "maya" in text.lower() else (0.0, 1.0)


class FakeHostClient:
    def __init__(self):
        self.requests = []

    def call_skill(self, request):
        self.requests.append(request)
        return HostCallResponse(
            status="success",
            action="skill.run",
            request_id=request.run_id,
            result={"created": 1},
        )


class RuntimeTests(unittest.TestCase):
    def test_repository_maya_skill_is_indexable_without_loading_code(self):
        root = Path(__file__).resolve().parents[1] / "skills"
        catalog = SkillCatalog(root)

        manifests = catalog.refresh()

        self.assertEqual([item.skill_id for item in manifests], ["maya.scene.plan"])
        self.assertEqual(manifests[0].entry.function, "run")

    def test_catalog_indexes_metadata_without_importing_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root, keys = _create_signed_skill(Path(temp))
            catalog = SkillCatalog(root.parent, keys)
            manifests = catalog.refresh()

            self.assertEqual([item.skill_id for item in manifests], ["maya.scene.plan"])
            self.assertNotIn("skill_fixture_code", __import__("sys").modules)

            package = catalog.load("maya.scene.plan")
            self.assertIn("plan", package.instructions)
            module_name, entry = catalog.load_entrypoint(package)
            self.assertEqual(entry(None, {"value": 3}), {"value": 3})
            catalog.unload_entrypoint(module_name)
            self.assertNotIn(module_name, __import__("sys").modules)

    def test_catalog_rejects_tampered_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            root, keys = _create_signed_skill(Path(temp))
            (root / "SKILL.md").write_text("被篡改", encoding="utf-8")
            catalog = SkillCatalog(root.parent, keys)
            catalog.refresh()

            with self.assertRaises(SkillSecurityError):
                catalog.load("maya.scene.plan")

    def test_catalog_rejects_invalid_entry_module(self):
        with tempfile.TemporaryDirectory() as temp:
            skill_root = Path(temp) / "maya.scene.plan"
            skill_root.mkdir(parents=True)
            (skill_root / "skill.yaml").write_text(
                "id: maya.scene.plan\n"
                "version: 1.0.0\n"
                "description: demo\n"
                "hosts: [maya]\n"
                "entry:\n  module: outside.entry\n  function: run\n"
                "actions: [skill.run]\nrequires: []\npermissions: {}\n",
                encoding="utf-8",
            )
            catalog = SkillCatalog(Path(temp), {"test": b"0" * 32})
            with self.assertRaises(SkillCatalogError):
                catalog.refresh()

    def test_session_rejects_two_active_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(Path(temp) / "session.db")
            session = store.create_session("project-a", "maya-001")
            store.start_run(session.session_id, "maya.scene.plan", {}, True)

            with self.assertRaises(SessionError):
                store.start_run(session.session_id, "maya.scene.plan", {}, True)

    def test_knowledge_uses_keyword_and_vector_results(self):
        with tempfile.TemporaryDirectory() as temp:
            index = KnowledgeIndex(Path(temp) / "knowledge.db", FakeEmbedding())
            index.add_document(
                "maya-doc",
                "Maya 场景",
                "Maya 主线程创建 Transform 节点。",
                "maya.md",
            )
            index.add_document(
                "other-doc",
                "其他工具",
                "其他宿主的说明。",
                "other.md",
            )

            hits = index.search("Maya 节点", limit=2)
            self.assertTrue(hits)
            self.assertEqual(hits[0].document_id, "maya-doc")

    def test_runtime_calls_existing_host_call_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root, keys = _create_signed_skill(Path(temp))
            catalog = SkillCatalog(root.parent, keys)
            sessions = SessionStore(Path(temp) / "session.db")
            client = FakeHostClient()
            runtime = SkillRuntime(catalog, sessions, client)
            runtime.start()
            session = sessions.create_session("project-a", "maya-001")

            result = runtime.run(
                session.session_id,
                "maya.scene.plan",
                "maya-001",
                {"nodes": []},
                confirm=True,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(client.requests[0].skill_id, "maya.scene.plan")
            self.assertTrue(client.requests[0].confirm)

    def test_runtime_requires_confirmation_for_mutating_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            root, keys = _create_signed_skill(Path(temp), mutates_scene=True)
            catalog = SkillCatalog(root.parent, keys)
            sessions = SessionStore(Path(temp) / "session.db")
            runtime = SkillRuntime(catalog, sessions, FakeHostClient())
            runtime.start()
            session = sessions.create_session("project-a")

            with self.assertRaises(SkillRuntimeError):
                runtime.run(session.session_id, "maya.scene.plan", "maya-001", {})

    def test_signer_generates_package_signature(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            package_root, _ = _create_signed_skill(base)
            private_key = base / "private.key"
            trust_config = base / "trust.json"
            SkillSigner.generate_key_pair(private_key, trust_config, "dev")
            signature_path = SkillSigner.sign_package(package_root, private_key, "dev")

            trusted = SkillSignature.load_trusted_keys(trust_config)
            catalog = SkillCatalog(package_root.parent, trusted)
            catalog.refresh()
            self.assertTrue(signature_path.is_file())
            self.assertEqual(catalog.load("maya.scene.plan").manifest.skill_id, "maya.scene.plan")

    def test_transport_unwraps_nested_host_call_result(self):
        class Socket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def send(self, _message):
                return None

            def recv(self, timeout=None):
                return json.dumps(
                    {
                        "status": "success",
                        "action": "host.call",
                        "request_id": "run-1",
                        "result": {
                            "status": "success",
                            "action": "skill.run",
                            "result": {"created": 2},
                        },
                    }
                )

        client = HostWebSocketClient(
            "ws://127.0.0.1:8890",
            connection_factory=lambda *_args, **_kwargs: Socket(),
        )
        response = client.call_skill(
            HostCallRequest(
                host_id="maya-001",
                skill_id="maya.scene.plan",
                entry="run",
                run_id="run-1",
            )
        )
        self.assertEqual(response.result, {"created": 2})


def _create_signed_skill(base: Path, mutates_scene: bool = False):
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
        "requires: [plan.validate, plan.apply]\n"
        "permissions:\n"
        "  maya_api: true\n"
        "  network: false\n"
        "  subprocess: false\n"
        "mutates_scene: %s\n" % ("true" if mutates_scene else "false")
    )
    (root / "skill.yaml").write_text(manifest_text, encoding="utf-8")
    (root / "SKILL.md").write_text("# plan\nMaya plan skill", encoding="utf-8")
    (code_root / "entry.py").write_text(
        "def run(context, args):\n    return args\n", encoding="utf-8"
    )

    manifest = yaml.safe_load(manifest_text)
    file_hashes = {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in ("skill.yaml", "SKILL.md", "code/entry.py")
    }
    payload = SkillSignature._canonical_payload(manifest, dict(sorted(file_hashes.items())))
    private_key = Ed25519PrivateKey.generate()
    signature = private_key.sign(payload)
    (root / "signature.json").write_text(
        json.dumps(
            {
                "algorithm": "ed25519",
                "key_id": "test",
                "files": file_hashes,
                "signature": base64.b64encode(signature).decode("ascii"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    public_key = private_key.public_key().public_bytes_raw()
    return root, {"test": public_key}


if __name__ == "__main__":
    unittest.main()
