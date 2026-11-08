"""DCC Skill Runtime 的 MCP 中转站入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .catalog import SkillCatalog
from .knowledge import KnowledgeIndex
from .models import HostInfo, SkillManifest, SkillPackage
from .runtime import RuntimePaths, SkillRuntime
from .security import SkillSignature
from .session import SessionStore
from .transport import HostWebSocketClient


class SkillRuntimeMcpServer:
    """把 Runtime 的只读能力注册为稳定的 MCP Tools 和 Resources。"""

    def __init__(
        self,
        runtime: SkillRuntime,
        host_client: Optional[HostWebSocketClient] = None,
        knowledge: Optional[KnowledgeIndex] = None,
        host: str = "127.0.0.1",
        port: int = 8892,
    ) -> None:
        self.runtime = runtime
        self.host_client = host_client or runtime.host_client
        self.knowledge = knowledge
        self.mcp = FastMCP(
            "DCC Skill Runtime",
            instructions="通过受信任的 Skill 连接本地 DCC 宿主。",
            host=host,
            port=port,
            streamable_http_path="/mcp",
        )
        self._register_tools()
        self._register_resources()

    def skill_list(self) -> list[dict[str, object]]:
        """返回索引阶段可读取的 Skill 元数据，不加载代码。"""

        return [_manifest_message(manifest) for manifest in self.runtime.catalog.list()]

    def skill_describe(self, skill_id: str) -> dict[str, object]:
        """校验签名后返回一个 Skill 的说明和有界引用内容。"""

        package = self.runtime.catalog.load(skill_id)
        return _package_message(package)

    def host_list(self) -> list[dict[str, object]]:
        """读取 Host Service 当前已注册的宿主。"""

        return [_host_message(host) for host in self.host_client.list_hosts()]

    def run(self, transport: str = "streamable-http") -> None:
        """启动 MCP Server；默认使用本地 Streamable HTTP。"""

        if transport != "streamable-http":
            raise ValueError("当前只支持 streamable-http")
        self.mcp.run(transport="streamable-http")

    def _register_tools(self) -> None:
        self.mcp.add_tool(
            self.skill_list,
            name="skill_list",
            description="列出已建立索引的 Skill 元数据。",
        )
        self.mcp.add_tool(
            self.skill_describe,
            name="skill_describe",
            description="读取一个已签名 Skill 的说明和引用文档。",
        )
        self.mcp.add_tool(
            self.host_list,
            name="host_list",
            description="列出当前 Host Service 已注册的 DCC 宿主。",
        )

    def _register_resources(self) -> None:
        self.mcp.resource(
            "skill://catalog",
            name="skill_catalog",
            description="已建立索引的 Skill 元数据目录。",
            mime_type="application/json",
        )(self._catalog_resource)
        self.mcp.resource(
            "skill://{skill_id}",
            name="skill_detail",
            description="一个已签名 Skill 的说明和引用文档。",
            mime_type="application/json",
        )(self._skill_resource)

    def _catalog_resource(self) -> str:
        return _json_text({"skills": self.skill_list()})

    def _skill_resource(self, skill_id: str) -> str:
        return _json_text(self.skill_describe(skill_id))


def build_runtime_mcp_server(
    paths: Optional[RuntimePaths] = None,
    service_url: str = "ws://127.0.0.1:8890",
    host: str = "127.0.0.1",
    port: int = 8892,
) -> SkillRuntimeMcpServer:
    """组装 Runtime、Host Service 客户端和 MCP Server。"""

    paths = paths or RuntimePaths.from_local_app_data()
    trusted_keys = _load_trusted_keys(paths.trust_config)
    catalog = SkillCatalog(paths.skill_root, trusted_keys)
    sessions = SessionStore(paths.session_db)
    host_client = HostWebSocketClient(service_url)
    knowledge = KnowledgeIndex(paths.knowledge_db)
    runtime = SkillRuntime(catalog, sessions, host_client)
    runtime.start()
    return SkillRuntimeMcpServer(
        runtime,
        host_client=host_client,
        knowledge=knowledge,
        host=host,
        port=port,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """启动本地 MCP Server 命令行入口。"""

    parser = argparse.ArgumentParser(description="启动 DCC Skill Runtime MCP Server")
    parser.add_argument("--skills", type=Path, help="Skill 根目录")
    parser.add_argument("--session-db", type=Path, help="会话数据库路径")
    parser.add_argument("--knowledge-db", type=Path, help="知识库数据库路径")
    parser.add_argument("--trust-config", type=Path, help="Skill 信任公钥配置")
    parser.add_argument("--service-url", default="ws://127.0.0.1:8890")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8892)
    args = parser.parse_args(argv)

    defaults = RuntimePaths.from_local_app_data()
    paths = RuntimePaths(
        skill_root=args.skills or defaults.skill_root,
        session_db=args.session_db or defaults.session_db,
        knowledge_db=args.knowledge_db or defaults.knowledge_db,
        trust_config=args.trust_config or defaults.trust_config,
    )
    build_runtime_mcp_server(
        paths=paths,
        service_url=args.service_url,
        host=args.host,
        port=args.port,
    ).run()
    return 0


def _load_trusted_keys(path: Path) -> dict[str, bytes]:
    if not path.is_file():
        return {}
    return SkillSignature.load_trusted_keys(path)


def _manifest_message(manifest: SkillManifest) -> dict[str, object]:
    return {
        "skill_id": manifest.skill_id,
        "version": manifest.version,
        "description": manifest.description,
        "hosts": list(manifest.hosts),
        "actions": list(manifest.actions),
        "requires": list(manifest.requires),
        "permissions": dict(manifest.permissions),
        "mutates_scene": manifest.mutates_scene,
    }


def _package_message(package: SkillPackage) -> dict[str, object]:
    message = _manifest_message(package.manifest)
    message["entry"] = {
        "module": package.manifest.entry.module,
        "function": package.manifest.entry.function,
    }
    message["instructions"] = package.instructions[:65536]
    message["references"] = [
        {"path": path, "content": content[:16384]}
        for path, content in package.references[:20]
    ]
    return message


def _host_message(host: HostInfo) -> dict[str, object]:
    return {
        "host_id": host.host_id,
        "host_type": host.host_type,
        "endpoint": host.endpoint,
        "capabilities": list(host.capabilities),
        "status": host.status,
        "last_seen": host.last_seen,
    }


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


if __name__ == "__main__":  # pragma: no cover - 交由命令行启动
    raise SystemExit(main())


__all__ = ["SkillRuntimeMcpServer", "build_runtime_mcp_server", "main"]
