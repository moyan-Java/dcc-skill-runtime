"""通过现有 Host Service WebSocket 调用宿主。"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import urlparse

from websockets.sync.client import connect

from .models import HostCallRequest, HostCallResponse


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
        return _parse_response(response)


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


def _validate_loopback_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("ws", "wss") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Host Service 地址必须是回环 WebSocket")
