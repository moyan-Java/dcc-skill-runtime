"""通过现有 Host Service WebSocket 调用宿主。"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import uuid4

from websockets.sync.client import connect

from .models import HostCallRequest, HostCallResponse, HostInfo


class HostTransportError(RuntimeError):
    """Host Service 调用失败。"""


class HostWebSocketClient:
    """保持 JSON/host.call 协议，不在 Runtime 内导入 DCC API。"""

    def __init__(self, service_url: str, timeout: float = 30.0, connection_factory=connect) -> None:
        _validate_loopback_url(service_url)
        self.service_url = service_url
        self.timeout = timeout
        self._connection_factory = connection_factory

    def call_skill(self, request: HostCallRequest) -> HostCallResponse:
        message = {
            "action": "host.call",
            "host_id": request.host_id,
            "request_id": request.run_id,
            "payload": {
                "action": "skill.run",
                "skill_id": request.skill_id,
                "entry": request.entry,
                "run_id": request.run_id,
                "args": dict(request.args),
                "confirm": request.confirm,
            },
        }
        response = self._send(message)
        return _parse_response(response)

    def list_hosts(self) -> tuple[HostInfo, ...]:
        """读取 Host Service 当前登记的宿主快照。"""

        response = self._send(
            {
                "action": "host.list",
                "request_id": "runtime-%s" % uuid4().hex,
            }
        )
        if response.get("status") != "success":
            error = response.get("error")
            message = error.get("message") if isinstance(error, dict) else "宿主列表读取失败"
            raise HostTransportError(message)
        raw_hosts = response.get("hosts")
        if not isinstance(raw_hosts, list):
            raise HostTransportError("Host Service 宿主列表格式无效")
        return tuple(_parse_host(item) for item in raw_hosts)

    def _send(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """在 Host Service JSON 边界完成一次请求和响应解析。"""

        try:
            with self._connection_factory(
                self.service_url,
                open_timeout=self.timeout,
                close_timeout=self.timeout,
            ) as socket:
                socket.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
                raw_response = socket.recv(timeout=self.timeout)
        except Exception as exc:
            raise HostTransportError("Host Service 调用失败") from exc
        try:
            response = json.loads(raw_response)
        except (TypeError, ValueError) as exc:
            raise HostTransportError("Host Service 返回了无效 JSON") from exc
        if not isinstance(response, dict):
            raise HostTransportError("Host Service 响应必须是对象")
        return response


def _parse_response(value: Any) -> HostCallResponse:
    if not isinstance(value, dict):
        raise HostTransportError("Host Service 响应必须是对象")
    status = value.get("status")
    action = value.get("action", "")
    if status not in ("success", "failed") or not isinstance(action, str):
        raise HostTransportError("Host Service 响应状态无效")
    error = value.get("error")
    result = value.get("result") if isinstance(value.get("result"), dict) else None
    nested_status = result.get("status") if result is not None else None
    if action == "host.call" and nested_status in ("success", "failed"):
        nested_error = result.get("error")
        if nested_status == "failed":
            return HostCallResponse(
                status="failed",
                action=action,
                request_id=value.get("request_id"),
                error_code=nested_error.get("code") if isinstance(nested_error, dict) else "skill_failed",
                error_message=nested_error.get("message") if isinstance(nested_error, dict) else "Skill 执行失败",
            )
        result = result.get("result") if isinstance(result.get("result"), dict) else {}
    return HostCallResponse(
        status=status,
        action=action,
        request_id=value.get("request_id"),
        result=result,
        error_code=error.get("code") if isinstance(error, dict) else None,
        error_message=error.get("message") if isinstance(error, dict) else None,
    )


def _parse_host(value: Any) -> HostInfo:
    """把 Host Service 宿主 JSON 转成 Runtime 内部对象。"""

    if not isinstance(value, dict):
        raise HostTransportError("Host Service 宿主信息格式无效")
    host_id = value.get("host_id")
    host_type = value.get("host_type")
    endpoint = value.get("endpoint")
    capabilities = value.get("capabilities", [])
    status = value.get("status")
    last_seen = value.get("last_seen")
    if (
        not isinstance(host_id, str)
        or not isinstance(host_type, str)
        or not isinstance(endpoint, str)
        or not isinstance(capabilities, list)
        or not all(isinstance(item, str) for item in capabilities)
        or not isinstance(status, str)
        or not isinstance(last_seen, (int, float))
    ):
        raise HostTransportError("Host Service 宿主信息字段无效")
    return HostInfo(
        host_id=host_id,
        host_type=host_type,
        endpoint=endpoint,
        capabilities=tuple(capabilities),
        status=status,
        last_seen=float(last_seen),
    )


def _validate_loopback_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("ws", "wss") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Host Service 地址必须是回环 WebSocket")
