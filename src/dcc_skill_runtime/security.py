"""Skill 包的路径、文件哈希和 Ed25519 签名校验。"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Mapping


class SkillSecurityError(ValueError):
    """Skill 包不可信或内容被篡改。"""


class SkillSignature:
    """验证 Skill manifest、文件清单和 Ed25519 签名。"""

    @staticmethod
    def load_trusted_keys(config_path: Path) -> dict[str, bytes]:
        """读取安装配置中的 base64 公钥。"""

        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SkillSecurityError("无法读取 Skill 信任配置") from exc
        raw_keys = data.get("keys") if isinstance(data, dict) else None
        if not isinstance(raw_keys, dict):
            raise SkillSecurityError("Skill 信任配置缺少 keys")

        keys: dict[str, bytes] = {}
        for key_id, value in raw_keys.items():
            if not isinstance(key_id, str) or not isinstance(value, str):
                raise SkillSecurityError("Skill 公钥配置无效")
            try:
                keys[key_id] = base64.b64decode(value, validate=True)
            except (ValueError, TypeError) as exc:
                raise SkillSecurityError("Skill 公钥不是有效的 base64") from exc
        return keys

    @classmethod
    def verify(
        cls,
        package_root: Path,
        manifest_data: Mapping[str, object],
        trusted_keys: Mapping[str, bytes],
    ) -> None:
        """验证签名文件和清单中的所有文件哈希。"""

        signature_path = package_root / "signature.json"
        try:
            signature_data = json.loads(signature_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SkillSecurityError("Skill 缺少有效的 signature.json") from exc

        if not isinstance(signature_data, dict):
            raise SkillSecurityError("Skill 签名文件必须是对象")
        if signature_data.get("algorithm") != "ed25519":
            raise SkillSecurityError("Skill 只支持 Ed25519 签名")

        key_id = signature_data.get("key_id")
        encoded_signature = signature_data.get("signature")
        files = signature_data.get("files")
        if not isinstance(key_id, str) or not isinstance(encoded_signature, str):
            raise SkillSecurityError("Skill 签名字段不完整")
        if not isinstance(files, dict) or not files:
            raise SkillSecurityError("Skill 签名缺少文件清单")

        public_key = trusted_keys.get(key_id)
        if public_key is None:
            raise SkillSecurityError("Skill 使用了未信任的公钥")

        normalized_files = cls._verify_files(package_root, files)
        payload = cls._canonical_payload(manifest_data, normalized_files)
        try:
            signature = base64.b64decode(encoded_signature, validate=True)
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )

            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
        except Exception as exc:
            raise SkillSecurityError("Skill 签名校验失败") from exc

    @staticmethod
    def _verify_files(package_root: Path, files: Mapping[str, object]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_path, expected_hash in files.items():
            if not isinstance(raw_path, str) or not isinstance(expected_hash, str):
                raise SkillSecurityError("Skill 文件清单无效")
            path = _safe_package_path(package_root, raw_path)
            if not path.is_file():
                raise SkillSecurityError("Skill 签名文件不存在: %s" % raw_path)
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_hash != expected_hash.lower():
                raise SkillSecurityError("Skill 文件哈希不匹配: %s" % raw_path)
            normalized[raw_path.replace("\\", "/")] = actual_hash
        return dict(sorted(normalized.items()))

    @staticmethod
    def _canonical_payload(
        manifest_data: Mapping[str, object], files: Mapping[str, str]
    ) -> bytes:
        payload = {"manifest": manifest_data, "files": files}
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def _safe_package_path(package_root: Path, relative_path: str) -> Path:
    """拒绝绝对路径和路径穿越。"""

    candidate = (package_root / relative_path).resolve()
    root = package_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SkillSecurityError("Skill 文件路径越过包根目录") from exc
    return candidate
