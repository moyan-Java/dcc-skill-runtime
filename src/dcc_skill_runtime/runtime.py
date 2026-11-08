"""Skill Runtime 主流程：选 Skill、记录会话并调用宿主。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, Mapping, Optional

from .catalog import SkillCatalog
from .models import HostCallRequest, SkillManifest, SkillRunResult
from .session import RunRecord, SessionStore


class SkillRuntimeError(RuntimeError):
    """Skill 编排或宿主执行失败。"""


@dataclass(frozen=True)
class RuntimePaths:
    """当前用户的 Runtime 文件位置。"""

    skill_root: Path
    session_db: Path
    knowledge_db: Path
    trust_config: Path
    runtime_token: Optional[Path] = None

    @classmethod
    def from_local_app_data(cls, local_app_data: Optional[str] = None) -> "RuntimePaths":
        root = Path(local_app_data or _local_app_data()) / "DccHost"
        return cls(
            skill_root=root / "skills",
            session_db=root / "runtime" / "session.db",
            knowledge_db=root / "knowledge" / "index.db",
            trust_config=root / "config" / "skill-trust.json",
            runtime_token=root / "config" / "runtime-token",
        )


class SkillRuntime:
    """只编排 Skill，不在 Runtime 进程执行 Maya API。"""

    def __init__(
        self,
        catalog: SkillCatalog,
        sessions: SessionStore,
        host_client: Any,
        worker_count: int = 4,
    ) -> None:
        if worker_count <= 0:
            raise ValueError("worker_count 必须大于 0")
        self.catalog = catalog
        self.sessions = sessions
        self.host_client = host_client
        self.worker_count = worker_count
        self._started = False
        self._executor: Optional[ThreadPoolExecutor] = None
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_locks_guard = threading.Lock()

    def start(self) -> tuple[SkillManifest, ...]:
        """建立常驻元数据索引。"""

        manifests = self.catalog.refresh()
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.worker_count,
                thread_name_prefix="DccSkillRun",
            )
        self._started = True
        return manifests

    def stop(self) -> None:
        """等待已提交的运行完成并关闭 Worker。"""

        executor = self._executor
        self._executor = None
        self._started = False
        if executor is not None:
            executor.shutdown(wait=True)

    def submit(
        self,
        session_id: str,
        skill_id: str,
        host_id: str,
        args: Mapping[str, Any],
        confirm: bool = False,
        host_type: str = "maya",
    ) -> RunRecord:
        """校验 Skill 和宿主后，把一次运行放入后台队列。"""

        self._ensure_started()
        package = self.catalog.load(skill_id)
        manifest = package.manifest
        self._validate_skill_action(manifest)
        self._validate_args(manifest, args)
        self._validate_host(host_id, host_type, manifest.hosts, manifest.requires)
        effective_confirm = bool(confirm or manifest.mutates_scene)
        run = self.sessions.queue_run(session_id, skill_id, args, effective_confirm)
        if self._executor is None:  # pragma: no cover - start() 已保证
            raise SkillRuntimeError("Skill Runtime Worker 尚未启动")
        self._executor.submit(
            self._execute_queued,
            run,
            host_id,
            args,
            manifest,
            effective_confirm,
        )
        return run

    def run(
        self,
        session_id: str,
        skill_id: str,
        host_id: str,
        args: Mapping[str, Any],
        confirm: bool = False,
        host_type: str = "maya",
    ) -> SkillRunResult:
        if not self._started:
            raise SkillRuntimeError("Skill Runtime 尚未启动")
        package = self.catalog.load(skill_id)
        manifest = package.manifest
        self._validate_skill_action(manifest)
        self._validate_args(manifest, args)
        self._validate_host(host_id, host_type, manifest.hosts, manifest.requires)
        if manifest.mutates_scene and not confirm:
            raise SkillRuntimeError("场景修改 Skill 需要用户确认")

        run = self.sessions.start_run(session_id, skill_id, args, confirm)
        request = HostCallRequest(
            host_id=host_id,
            skill_id=skill_id,
            entry=manifest.entry.function,
            run_id=run.run_id,
            args=args,
            confirm=confirm,
        )
        try:
            response = self.host_client.call_skill(request)
        except Exception as exc:
            self.sessions.finish_run(run.run_id, "failed", error_code="host_transport_failed", error_message=str(exc))
            return SkillRunResult(run.run_id, "failed", skill_id, error_code="host_transport_failed", error_message=str(exc))

        if response.succeeded:
            self.sessions.finish_run(run.run_id, "success", result=response.result)
            return SkillRunResult(run.run_id, "success", skill_id, result=response.result)

        self.sessions.finish_run(
            run.run_id,
            "failed",
            error_code=response.error_code or "skill_failed",
            error_message=response.error_message or "Skill 执行失败",
        )
        return SkillRunResult(
            run.run_id,
            "failed",
            skill_id,
            error_code=response.error_code or "skill_failed",
            error_message=response.error_message or "Skill 执行失败",
        )

    def _execute_queued(
        self,
        run: RunRecord,
        host_id: str,
        args: Mapping[str, Any],
        manifest: SkillManifest,
        confirm: bool,
    ) -> None:
        if not self.sessions.mark_running(run.run_id):
            return
        request = HostCallRequest(
            host_id=host_id,
            skill_id=run.skill_id,
            entry=manifest.entry.function,
            run_id=run.run_id,
            args=args,
            confirm=confirm,
        )
        with self._host_lock(host_id):
            try:
                response = self.host_client.call_skill(request)
            except Exception as exc:
                self.sessions.finish_run(
                    run.run_id,
                    "failed",
                    error_code="host_transport_failed",
                    error_message=str(exc),
                )
                return
            if response.succeeded:
                self.sessions.finish_run(run.run_id, "success", result=response.result)
                return
            self.sessions.finish_run(
                run.run_id,
                "failed",
                error_code=response.error_code or "skill_failed",
                error_message=response.error_message or "Skill 执行失败",
            )

    def _validate_host(
        self,
        host_id: str,
        host_type: str,
        supported_hosts: tuple[str, ...],
        required_capabilities: tuple[str, ...],
    ) -> None:
        if not host_id.strip():
            raise SkillRuntimeError("host_id 不能为空")
        try:
            hosts = self.host_client.list_hosts()
        except Exception as exc:
            raise SkillRuntimeError("无法读取 Host Service 宿主列表") from exc
        for host in hosts:
            if host.host_id != host_id:
                continue
            if host.status != "online":
                raise SkillRuntimeError("宿主当前不在线: %s" % host_id)
            if host.host_type != host_type:
                raise SkillRuntimeError("宿主类型不匹配: %s" % host_id)
            if host.host_type not in supported_hosts:
                raise SkillRuntimeError("Skill 不支持宿主类型: %s" % host.host_type)
            missing = sorted(set(required_capabilities) - set(host.capabilities))
            if missing:
                raise SkillRuntimeError(
                    "宿主缺少 Skill 所需能力: %s" % ", ".join(missing)
                )
            return
        raise SkillRuntimeError("未知宿主: %s" % host_id)

    @staticmethod
    def _validate_skill_action(manifest: SkillManifest) -> None:
        if "skill.run" not in manifest.actions:
            raise SkillRuntimeError("Skill 未声明 skill.run action")

    @staticmethod
    def _validate_args(manifest: SkillManifest, args: Mapping[str, Any]) -> None:
        if not isinstance(args, Mapping):
            raise SkillRuntimeError("Skill args 必须是对象")
        schema = manifest.args_schema
        if not schema:
            return
        properties = schema["properties"]
        required = schema["required"]
        if any(not isinstance(name, str) for name in args):
            raise SkillRuntimeError("Skill args 参数名必须是字符串")
        if not schema["additionalProperties"]:
            unknown = sorted(set(args) - set(properties))
            if unknown:
                raise SkillRuntimeError("Skill args 包含未声明参数: %s" % ", ".join(unknown))
        missing = [name for name in required if name not in args]
        if missing:
            raise SkillRuntimeError("Skill args 缺少必填参数: %s" % ", ".join(missing))
        for name, value in args.items():
            definition = properties.get(name)
            if definition is None:
                continue
            if not _matches_type(value, definition["type"]):
                raise SkillRuntimeError("Skill args 参数类型错误: %s" % name)
            enum = definition.get("enum")
            if enum is not None and value not in enum:
                raise SkillRuntimeError("Skill args 参数值不允许: %s" % name)

    def _host_lock(self, host_id: str) -> threading.Lock:
        with self._host_locks_guard:
            lock = self._host_locks.get(host_id)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host_id] = lock
            return lock

    def _ensure_started(self) -> None:
        if not self._started:
            raise SkillRuntimeError("Skill Runtime 尚未启动")


def _local_app_data() -> str:
    import os

    value = os.environ.get("LOCALAPPDATA")
    if value:
        return value
    return str(Path.home() / "AppData" / "Local")


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    return False
