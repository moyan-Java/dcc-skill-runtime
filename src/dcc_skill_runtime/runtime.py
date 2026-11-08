"""Skill Runtime 主流程：选 Skill、记录会话并调用宿主。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .catalog import SkillCatalog
from .models import HostCallRequest, SkillManifest, SkillRunResult
from .session import SessionStore


class SkillRuntimeError(RuntimeError):
    """Skill 编排或宿主执行失败。"""


@dataclass(frozen=True)
class RuntimePaths:
    """当前用户的 Runtime 文件位置。"""

    skill_root: Path
    session_db: Path
    knowledge_db: Path
    trust_config: Path

    @classmethod
    def from_local_app_data(cls, local_app_data: Optional[str] = None) -> "RuntimePaths":
        root = Path(local_app_data or _local_app_data()) / "DccHost"
        return cls(
            skill_root=root / "skills",
            session_db=root / "runtime" / "session.db",
            knowledge_db=root / "knowledge" / "index.db",
            trust_config=root / "config" / "skill-trust.json",
        )


class SkillRuntime:
    """只编排 Skill，不在 Runtime 进程执行 Maya API。"""

    def __init__(self, catalog: SkillCatalog, sessions: SessionStore, host_client: Any) -> None:
        self.catalog = catalog
        self.sessions = sessions
        self.host_client = host_client
        self._started = False

    def start(self) -> tuple[SkillManifest, ...]:
        """建立常驻元数据索引。"""

        manifests = self.catalog.refresh()
        self._started = True
        return manifests

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
        if host_type not in manifest.hosts:
            raise SkillRuntimeError("Skill 不支持宿主: %s" % host_type)
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


def _local_app_data() -> str:
    import os

    value = os.environ.get("LOCALAPPDATA")
    if value:
        return value
    return str(Path.home() / "AppData" / "Local")
