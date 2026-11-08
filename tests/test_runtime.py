import base64
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import yaml

from dcc_skill_runtime.catalog import SkillCatalog, SkillCatalogError
from dcc_skill_runtime.knowledge import KnowledgeIndex
from dcc_skill_runtime.models import HostCallRequest, HostCallResponse, HostInfo
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
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False

    def list_hosts(self):
        return (
            HostInfo(
                "maya-001",
                "maya",
                "pipe://maya-001",
                ("plan.validate", "plan.apply"),
                "online",
                1.0,
            ),
        )

    def call_skill(self, request):
        self.requests.append(request)
        self.started.set()
        if self.block:
            self.release.wait(2.0)
        return HostCallResponse(
            status="success",
            action="skill.run",
            request_id=request.run_id,
            result={"created": 1},
        )


class MultiHostClient:
    def __init__(self):
        self.hosts = (
            HostInfo(
                "maya-001",
                "maya",
                "pipe://maya-001",
                ("plan.validate", "plan.apply"),
                "online",
                1.0,
            ),
            HostInfo(
                "maya-002",
                "maya",
                "pipe://maya-002",
                ("plan.validate", "plan.apply"),
                "online",
                1.0,
            ),
        )
        self.started = []
        self.active = {}
        self.max_active = {}
        self._lock = threading.Lock()
        self.started_event = threading.Event()
        self.host_started = {
            "maya-001": threading.Event(),
            "maya-002": threading.Event(),
        }
        self.release = threading.Event()

    def list_hosts(self):
        return self.hosts

    def call_skill(self, request):
        with self._lock:
            active = self.active.get(request.host_id, 0) + 1
            self.active[request.host_id] = active
            self.max_active[request.host_id] = max(
                active,
                self.max_active.get(request.host_id, 0),
            )
            self.started.append(request.host_id)
            self.started_event.set()
            self.host_started[request.host_id].set()
        self.release.wait(2.0)
        with self._lock:
            self.active[request.host_id] -= 1
        return HostCallResponse("success", "skill.run", request.run_id, {})


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

    def test_catalog_rejects_unsigned_extra_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root, keys = _create_signed_skill(Path(temp))
            (root / "code" / "extra.py").write_text("value = 1\n", encoding="utf-8")
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

    def test_runtime_submit_uses_queued_and_running_states(self):
        with tempfile.TemporaryDirectory() as temp:
            root, keys = _create_signed_skill(Path(temp))
            catalog = SkillCatalog(root.parent, keys)
            sessions = SessionStore(Path(temp) / "session.db")
            client = FakeHostClient()
            client.block = True
            runtime = SkillRuntime(catalog, sessions, client, worker_count=2)
            runtime.start()
            session = sessions.create_session("project-a", "maya-001")

            run = runtime.submit(session.session_id, "maya.scene.plan", "maya-001", {})

            self.assertEqual(run.status, "queued")
            self.assertTrue(client.started.wait(1.0))
            self.assertEqual(sessions.get_run(run.run_id).status, "running")
            client.release.set()
            runtime.stop()
            self.assertEqual(sessions.get_run(run.run_id).status, "success")

    def test_runtime_serializes_same_host_and_runs_other_hosts_in_parallel(self):
        with tempfile.TemporaryDirectory() as temp:
            root, keys = _create_signed_skill(Path(temp))
            catalog = SkillCatalog(root.parent, keys)
            sessions = SessionStore(Path(temp) / "session.db")
            client = MultiHostClient()
            runtime = SkillRuntime(catalog, sessions, client, worker_count=4)
            runtime.start()
            first = sessions.create_session("project-a", "maya-001")
            second = sessions.create_session("project-a", "maya-001")
            third = sessions.create_session("project-a", "maya-002")

            first_run = runtime.submit(first.session_id, "maya.scene.plan", "maya-001", {})
            second_run = runtime.submit(second.session_id, "maya.scene.plan", "maya-001", {})
            third_run = runtime.submit(third.session_id, "maya.scene.plan", "maya-002", {})

            self.assertTrue(client.host_started["maya-002"].wait(1.0))
            self.assertEqual(client.max_active.get("maya-001"), 1)
            client.release.set()
            runtime.stop()
            self.assertEqual(sessions.get_run(first_run.run_id).status, "success")
            self.assertEqual(sessions.get_run(second_run.run_id).status, "success")
            self.assertEqual(sessions.get_run(third_run.run_id).status, "success")

    def test_runtime_rejects_unknown_host_before_queuing(self):
        with tempfile.TemporaryDirectory() as temp:
            root, keys = _create_signed_skill(Path(temp))
            catalog = SkillCatalog(root.parent, keys)
            sessions = SessionStore(Path(temp) / "session.db")
            runtime = SkillRuntime(catalog, sessions, MultiHostClient())
            runtime.start()
            session = sessions.create_session("project-a")

            with self.assertRaisesRegex(SkillRuntimeError, "未知宿主"):
                runtime.submit(session.session_id, "maya.scene.plan", "maya-999", {})
            runtime.stop()

    def test_runtime_rejects_host_missing_skill_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            root, keys = _create_signed_skill(Path(temp))
            catalog = SkillCatalog(root.parent, keys)
            sessions = SessionStore(Path(temp) / "session.db")

            class MissingCapabilityClient(FakeHostClient):
                def list_hosts(self):
                    return (
                        HostInfo(
                            "maya-001",
                            "maya",
                            "pipe://maya-001",
                            ("plan.validate",),
                            "online",
                            1.0,
                        ),
                    )

            runtime = SkillRuntime(catalog, sessions, MissingCapabilityClient())
            runtime.start()
            session = sessions.create_session("project-a")

            with self.assertRaisesRegex(SkillRuntimeError, "缺少 Skill 所需能力"):
                runtime.submit(session.session_id, "maya.scene.plan", "maya-001", {})
            runtime.stop()

    def test_runtime_rejects_args_not_declared_by_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            root, keys = _create_signed_skill(Path(temp), args_schema=True)
            catalog = SkillCatalog(root.parent, keys)
            sessions = SessionStore(Path(temp) / "session.db")
            runtime = SkillRuntime(catalog, sessions, FakeHostClient())
            runtime.start()
            session = sessions.create_session("project-a")

            with self.assertRaisesRegex(SkillRuntimeError, "缺少必填参数"):
                runtime.submit(session.session_id, "maya.scene.plan", "maya-001", {})
            with self.assertRaisesRegex(SkillRuntimeError, "参数类型错误"):
                runtime.submit(
                    session.session_id,
                    "maya.scene.plan",
                    "maya-001",
                    {"plan": "not-an-object"},
                )
            runtime.stop()

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

    def test_transport_parses_host_list_into_explicit_objects(self):
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
                        "action": "host.list",
                        "hosts": [
                            {
                                "host_id": "maya-001",
                                "host_type": "maya",
                                "endpoint": "pipe://maya-001",
                                "capabilities": ["plan.validate"],
                                "status": "online",
                                "last_seen": 100,
                            }
                        ],
                    }
                )

        client = HostWebSocketClient(
            "ws://127.0.0.1:8890",
            connection_factory=lambda *_args, **_kwargs: Socket(),
        )

        hosts = client.list_hosts()

        self.assertEqual(hosts[0].host_id, "maya-001")
        self.assertEqual(hosts[0].capabilities, ("plan.validate",))


def _create_signed_skill(
    base: Path,
    mutates_scene: bool = False,
    args_schema: bool = False,
):
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
        + (
            "args:\n"
            "  properties:\n"
            "    plan:\n"
            "      type: object\n"
            "  required: [plan]\n"
            "  additionalProperties: false\n"
            if args_schema
            else ""
        )
        + ("mutates_scene: %s\n" % ("true" if mutates_scene else "false"))
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
