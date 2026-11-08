"""Skill Runtime 使用的少量明确数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class SkillEntry:
    """Skill 在宿主进程中的模块和函数入口。"""

    module: str
    function: str


@dataclass(frozen=True)
class SkillManifest:
    """Skill 索引阶段需要的机器可读信息。"""

    skill_id: str
    version: str
    description: str
    hosts: Tuple[str, ...]
    entry: SkillEntry
    actions: Tuple[str, ...]
    requires: Tuple[str, ...]
    permissions: Mapping[str, bool]
    mutates_scene: bool


@dataclass(frozen=True)
class SkillPackage:
    """通过签名校验并按需读取后的 Skill 内容。"""

    manifest: SkillManifest
    root: Path
    instructions: str
    references: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class HostCallRequest:
    """发送给 Host Service 的一次宿主调用。"""

    host_id: str
    skill_id: str
    entry: str
    run_id: str
    args: Mapping[str, Any] = field(default_factory=dict)
    confirm: bool = False


@dataclass(frozen=True)
class HostInfo:
    """Host Service 返回的宿主快照。"""

    host_id: str
    host_type: str
    endpoint: str
    capabilities: Tuple[str, ...]
    status: str
    last_seen: float


@dataclass(frozen=True)
class HostCallResponse:
    """Host Service 返回的协议响应。"""

    status: str
    action: str
    request_id: Optional[str]
    result: Optional[Mapping[str, Any]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        """判断顶层请求是否成功。"""

        return self.status == "success"


@dataclass(frozen=True)
class SkillRunResult:
    """一次 Skill 运行的内部结果。"""

    run_id: str
    status: str
    skill_id: str
    result: Optional[Mapping[str, Any]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
