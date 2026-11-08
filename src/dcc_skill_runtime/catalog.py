"""Skill 元数据索引和按需内容加载。"""

from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import yaml

from .models import SkillEntry, SkillManifest, SkillPackage
from .security import SkillSecurityError, SkillSignature


class SkillCatalogError(ValueError):
    """Skill 清单或内容无效。"""


class SkillCatalog:
    """只在刷新时读取清单，选中 Skill 后才读取正文。"""

    def __init__(
        self,
        root: Path,
        trusted_keys: Optional[Mapping[str, bytes]] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self._trusted_keys = dict(trusted_keys or {})
        self._manifests: dict[str, tuple[SkillManifest, Path, dict[str, object]]] = {}

    def refresh(self) -> tuple[SkillManifest, ...]:
        """扫描 Skill 目录，只读取 skill.yaml。"""

        manifests: dict[str, tuple[SkillManifest, Path, dict[str, object]]] = {}
        if not self.root.is_dir():
            self._manifests = {}
            return ()
        for manifest_path in sorted(self.root.glob("*/skill.yaml")):
            raw = _read_yaml(manifest_path)
            manifest = _parse_manifest(raw)
            if manifest.skill_id in manifests:
                raise SkillCatalogError("Skill id 重复: %s" % manifest.skill_id)
            manifests[manifest.skill_id] = (manifest, manifest_path.parent, raw)
        self._manifests = manifests
        return tuple(item[0] for item in manifests.values())

    def list(self) -> tuple[SkillManifest, ...]:
        """返回已经建立的索引，不触发文件扫描。"""

        return tuple(item[0] for item in self._manifests.values())

    def load(self, skill_id: str) -> SkillPackage:
        """校验签名并读取选中 Skill 的说明和引用文档。"""

        item = self._manifests.get(skill_id)
        if item is None:
            raise SkillCatalogError("Skill 不存在: %s" % skill_id)
        manifest, root, raw_manifest = item
        if not self._trusted_keys:
            raise SkillSecurityError("未配置 Skill 信任公钥")
        SkillSignature.verify(root, raw_manifest, self._trusted_keys)

        instructions_path = root / "SKILL.md"
        if not instructions_path.is_file():
            raise SkillCatalogError("Skill 缺少 SKILL.md: %s" % skill_id)
        references = []
        references_root = root / "references"
        if references_root.is_dir():
            for path in sorted(references_root.rglob("*.md")):
                references.append((path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")))
        return SkillPackage(
            manifest=manifest,
            root=root,
            instructions=instructions_path.read_text(encoding="utf-8"),
            references=tuple(references),
        )

    def load_entrypoint(self, package: SkillPackage) -> tuple[str, Callable[..., Any]]:
        """只在执行时导入 manifest 声明的入口函数。"""

        module_path = _module_path(package.root, package.manifest.entry.module)
        if not module_path.is_file():
            raise SkillCatalogError("Skill 入口文件不存在: %s" % module_path.name)
        module_name = "_dcc_skill_%s" % uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise SkillCatalogError("无法加载 Skill 入口模块")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            entry = getattr(module, package.manifest.entry.function, None)
            if not callable(entry):
                raise SkillCatalogError("Skill 入口函数不存在")
            return module_name, entry
        except Exception:
            sys.modules.pop(module_name, None)
            raise

    @staticmethod
    def unload_entrypoint(module_name: str) -> None:
        """清理 Runtime 自己创建的入口模块引用。"""

        sys.modules.pop(module_name, None)


def _read_yaml(path: Path) -> dict[str, object]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SkillCatalogError("无法读取 Skill 清单: %s" % path.name) from exc
    if not isinstance(raw, dict):
        raise SkillCatalogError("Skill 清单必须是对象")
    return raw


def _parse_manifest(raw: Mapping[str, object]) -> SkillManifest:
    skill_id = _required_string(raw, "id")
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", skill_id):
        raise SkillCatalogError("Skill id 格式无效: %s" % skill_id)
    version = _required_string(raw, "version")
    description = _required_string(raw, "description")
    hosts = _string_tuple(raw.get("hosts"), "hosts")
    actions = _string_tuple(raw.get("actions"), "actions")
    requires = _string_tuple(raw.get("requires", []), "requires")
    entry_raw = raw.get("entry")
    if not isinstance(entry_raw, dict):
        raise SkillCatalogError("Skill entry 必须是对象")
    module = _required_string(entry_raw, "module")
    function = _required_string(entry_raw, "function")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
        raise SkillCatalogError("Skill 入口模块格式无效")
    if not module.startswith("code."):
        raise SkillCatalogError("Skill 入口必须位于 code 包下")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", function):
        raise SkillCatalogError("Skill 入口函数格式无效")
    permissions = raw.get("permissions", {})
    if not isinstance(permissions, dict) or any(
        not isinstance(key, str) or not isinstance(value, bool)
        for key, value in permissions.items()
    ):
        raise SkillCatalogError("Skill permissions 必须是布尔字段")
    args_schema = _parse_args_schema(raw.get("args", {}))
    return SkillManifest(
        skill_id=skill_id,
        version=version,
        description=description,
        hosts=hosts,
        entry=SkillEntry(module, function),
        actions=actions,
        requires=requires,
        permissions=dict(permissions),
        mutates_scene=bool(raw.get("mutates_scene", False)),
        args_schema=args_schema,
    )


def _required_string(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SkillCatalogError("Skill %s 必须是非空字符串" % name)
    return value.strip()


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SkillCatalogError("Skill %s 必须是字符串列表" % name)
    return tuple(value)


def _parse_args_schema(value: object) -> dict[str, object]:
    """解析 Skill 的顶层参数声明，不引入完整 JSON Schema 实现。"""

    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise SkillCatalogError("Skill args 必须是对象")
    properties = value.get("properties", {})
    required = value.get("required", [])
    additional = value.get("additionalProperties", False)
    if not isinstance(properties, dict):
        raise SkillCatalogError("Skill args.properties 必须是对象")
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise SkillCatalogError("Skill args.required 必须是字符串列表")
    if not isinstance(additional, bool):
        raise SkillCatalogError("Skill args.additionalProperties 必须是布尔值")
    for name, definition in properties.items():
        if not isinstance(name, str) or not name.strip():
            raise SkillCatalogError("Skill args 参数名必须是非空字符串")
        if not isinstance(definition, dict):
            raise SkillCatalogError("Skill args 参数声明必须是对象")
        value_type = definition.get("type")
        if value_type not in {"string", "object", "array", "boolean", "number", "integer", "null"}:
            raise SkillCatalogError("Skill args 参数类型不受支持: %s" % name)
        enum = definition.get("enum")
        if enum is not None and (not isinstance(enum, list) or not enum):
            raise SkillCatalogError("Skill args 参数 enum 必须是非空列表: %s" % name)
    unknown_required = set(required) - set(properties)
    if unknown_required:
        raise SkillCatalogError(
            "Skill args.required 包含未声明参数: %s"
            % ", ".join(sorted(unknown_required))
        )
    return {
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": additional,
    }


def _module_path(root: Path, module: str) -> Path:
    parts = module.split(".")
    if parts[0] != "code":
        raise SkillCatalogError("Skill 入口必须位于 code 包下")
    path = root.joinpath(*parts).with_suffix(".py")
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise SkillCatalogError("Skill 入口越过包目录") from exc
    return resolved
