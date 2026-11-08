"""DCC Skill Runtime 的 MCP 中转站入口。"""

from __future__ import annotations

import argparse
import hmac
import json
from pathlib import Path
import secrets
from typing import Any, Optional
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

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
        token_path: Optional[Path] = None,
    ) -> None:
        self.runtime = runtime
        self.host_client = host_client or runtime.host_client
        self.knowledge = knowledge
        self._knowledge_results: dict[str, dict[str, object]] = {}
        token_verifier = None
        auth = None
        if token_path is not None:
            token = _load_or_create_token(token_path)
            token_verifier = LocalTokenVerifier(token)
            auth = AuthSettings(
                issuer_url="http://%s:%s" % (host, port),
                resource_server_url="http://%s:%s" % (host, port),
                validate_token_resource=False,
                required_scopes=[],
            )
        self.mcp = FastMCP(
            "DCC Skill Runtime",
            instructions="通过受信任的 Skill 连接本地 DCC 宿主。",
            host=host,
            port=port,
            streamable_http_path="/mcp",
            auth=auth,
            token_verifier=token_verifier,
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

    def session_open(self, project_id: str, host_id: Optional[str] = None) -> dict[str, object]:
        """创建一个由 Runtime 管理的会话，返回后续调用需要的 session_id。"""

        session = self.runtime.sessions.create_session(project_id, host_id)
        return {
            "session_id": session.session_id,
            "project_id": session.project_id,
            "host_id": session.host_id,
        }

    def skill_run(
        self,
        session_id: str,
        project_id: str,
        host_id: str,
        skill_id: str,
        args: dict[str, Any],
    ) -> dict[str, object]:
        """提交一次受信任 Skill 运行，并立即返回 queued 状态。"""

        session = self.runtime.sessions.get_session(session_id)
        if session.project_id != project_id:
            raise ValueError("session_id 不属于指定 project_id")
        run = self.runtime.submit(
            session_id=session_id,
            skill_id=skill_id,
            host_id=host_id,
            args=args,
            confirm=True,
        )
        return {
            "run_id": run.run_id,
            "status": run.status,
            "skill_id": run.skill_id,
        }

    def run_status(self, run_id: str) -> dict[str, object]:
        """读取一次 Skill 运行的当前状态和结果。"""

        return _run_message(self.runtime.sessions.get_run(run_id))

    def run_cancel(self, run_id: str) -> dict[str, object]:
        """只取消尚未进入宿主调用的 queued 运行。"""

        cancelled = self.runtime.sessions.cancel_queued(run_id)
        message = _run_message(self.runtime.sessions.get_run(run_id))
        message["cancelled"] = cancelled
        return message

    def knowledge_search(self, query: str, limit: int = 5) -> dict[str, object]:
        """检索本地知识库并返回带来源的有界片段。"""

        if self.knowledge is None:
            raise ValueError("知识库尚未配置")
        if not query.strip():
            raise ValueError("query 不能为空")
        if not 1 <= limit <= 20:
            raise ValueError("limit 必须在 1 到 20 之间")
        query_id = uuid4().hex
        hits = self.knowledge.search(query, limit=limit)
        result = {
            "query_id": query_id,
            "hits": [
                {
                    "document_id": hit.document_id,
                    "title": hit.title,
                    "content": hit.content[:16384],
                    "source": hit.source,
                    "score": hit.score,
                }
                for hit in hits
            ],
        }
        self._knowledge_results[query_id] = result
        return result

    def run(self, transport: str = "streamable-http") -> None:
        """启动 MCP Server；默认使用本地 Streamable HTTP。"""

        if transport != "streamable-http":
            raise ValueError("当前只支持 streamable-http")
        try:
            self.mcp.run(transport="streamable-http")
        except OSError as exc:
            raise RuntimeError("MCP 监听端口或地址不可用") from exc
        finally:
            self.runtime.stop()

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
        self.mcp.add_tool(
            self.session_open,
            name="session_open",
            description="创建一个项目会话，返回 skill_run 需要的 session_id。",
        )
        self.mcp.add_tool(
            self.skill_run,
            name="skill_run",
            description="提交一次已签名 Skill 运行并返回 run_id。",
        )
        self.mcp.add_tool(
            self.run_status,
            name="run_status",
            description="读取一次 Skill 运行的状态和结果。",
        )
        self.mcp.add_tool(
            self.run_cancel,
            name="run_cancel",
            description="取消尚未开始宿主调用的 Skill 运行。",
        )
        self.mcp.add_tool(
            self.knowledge_search,
            name="knowledge_search",
            description="检索本地知识库并返回带来源的片段。",
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
        self.mcp.resource(
            "knowledge://result/{query_id}",
            name="knowledge_result",
            description="一次知识库查询的只读结果。",
            mime_type="application/json",
        )(self._knowledge_resource)

    def _catalog_resource(self) -> str:
        return _json_text({"skills": self.skill_list()})

    def _skill_resource(self, skill_id: str) -> str:
        return _json_text(self.skill_describe(skill_id))

    def _knowledge_resource(self, query_id: str) -> str:
        result = self._knowledge_results.get(query_id)
        if result is None:
            raise ValueError("知识库查询结果不存在")
        return _json_text(result)


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
        token_path=paths.runtime_token,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """启动本地 MCP Server 命令行入口。"""

    parser = argparse.ArgumentParser(description="启动 DCC Skill Runtime MCP Server")
    parser.add_argument("--skills", type=Path, help="Skill 根目录")
    parser.add_argument("--session-db", type=Path, help="会话数据库路径")
    parser.add_argument("--knowledge-db", type=Path, help="知识库数据库路径")
    parser.add_argument("--trust-config", type=Path, help="Skill 信任公钥配置")
    parser.add_argument("--runtime-token", type=Path, help="MCP Bearer Token 文件路径")
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
        runtime_token=args.runtime_token or defaults.runtime_token,
    )
    build_runtime_mcp_server(
        paths=paths,
        service_url=args.service_url,
        host=args.host,
        port=args.port,
    ).run()
    return 0


class LocalTokenVerifier:
    """校验当前用户本地生成的 Bearer Token。"""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if not hmac.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="local-agent",
            scopes=[],
        )


def _load_or_create_token(path: Path) -> str:
    path = Path(path)
    if path.is_file():
        token = path.read_text(encoding="ascii").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="ascii")
    return token


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
        "args": dict(manifest.args_schema),
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


def _run_message(run: Any) -> dict[str, object]:
    message: dict[str, object] = {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "skill_id": run.skill_id,
        "status": run.status,
    }
    if run.result is not None:
        message["result"] = dict(run.result)
    if run.error_code:
        message["error"] = {
            "code": run.error_code,
            "message": run.error_message,
        }
    return message


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


if __name__ == "__main__":  # pragma: no cover - 交由命令行启动
    raise SystemExit(main())


__all__ = ["SkillRuntimeMcpServer", "build_runtime_mcp_server", "main"]
