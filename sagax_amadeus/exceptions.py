"""Sagax Audit SDK — 异常类型。

HTTP 状态码到异常的映射是**稳定契约**：调用方按异常类型分支，不按状态码数字。

    401  AuthenticationError    Key 缺失 / 无效 / 已吊销
    402  QuotaExceededError     订阅配额用尽
    403  PermissionDeniedError  订阅停用、租户不匹配、管理员令牌无效
    404  NotFoundError          资源不存在，**或者属于别的租户**
    409  ConflictError          状态冲突（如审计还没跑完就取结果）
    413  PayloadTooLargeError   上传超限
    422  ValidationError        请求体不合法
    429  RateLimitError         限流
    5xx  ServerError            服务端错误

404 同时表示「不属于你」是有意的：返回 403 等于确认「这个 id 存在」，
那本身就是一次跨租户信息泄露。

所有异常的 ``str()`` 都**不含 API Key**。:class:`SagaxAuditError` 的构造函数
会把响应体过一遍脱敏，请求头从来不进异常 —— 见
:mod:`sagax_amadeus.transport` 里的 ``_redact``。
"""
from __future__ import annotations

from typing import Any, Optional


class SagaxAuditError(Exception):
    """SDK 所有异常的基类。"""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: str = "", request_id: str = "",
                 payload: Optional[dict[str, Any]] = None) -> None:
        """Args:
            message: 人类可读的错误说明（已脱敏）。
            status: HTTP 状态码；网络层错误为 None。
            code: 服务端返回的机器可读错误码。
            request_id: 服务端请求 id，报障时给我们看这个。
            payload: 服务端返回的完整错误体（已脱敏）。
        """
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.request_id = request_id
        self.payload = payload or {}

    def __str__(self) -> str:
        bits = [self.message]
        if self.code:
            bits.append(f"code={self.code}")
        if self.status is not None:
            bits.append(f"status={self.status}")
        if self.request_id:
            bits.append(f"request_id={self.request_id}")
        return " · ".join(bits)


class ConfigurationError(SagaxAuditError):
    """客户端配置有问题（缺 base_url、缺 API Key、超时值非法…）。

    这类错误在**发请求之前**抛出：一个配错的客户端应该立刻失败，
    而不是在生产流量里变成一串 401。
    """


class InsecureTransportError(ConfigurationError):
    """对非本机地址用了明文 HTTP。

    Evidence、私有 Memory、私有 Skill 都会经这条连接上传。明文发出去就等于
    没有租户隔离 —— 中间任何一跳都能读到全部内容。开发环境连 127.0.0.1 时
    自动放行；确实需要在别处走明文（例如内网已有 TLS 终止在更外层），
    显式传 ``allow_insecure_http=True``。
    """


class APIConnectionError(SagaxAuditError):
    """连不上云端（DNS、TCP、TLS 失败）。"""


class APITimeoutError(APIConnectionError):
    """请求超时。"""


class APIStatusError(SagaxAuditError):
    """服务端返回了一个非 2xx 状态。"""


class AuthenticationError(APIStatusError):
    """401 —— API Key 缺失、无效或已吊销。"""


class QuotaExceededError(APIStatusError):
    """402 —— 订阅配额用尽。"""


class PermissionDeniedError(APIStatusError):
    """403 —— 订阅停用，或请求里的 tenant_id 与 Key 不符。"""


class NotFoundError(APIStatusError):
    """404 —— 资源不存在，或不属于当前租户。"""


class ConflictError(APIStatusError):
    """409 —— 与当前状态冲突（如审计尚未完成就取结果）。"""


class PayloadTooLargeError(APIStatusError):
    """413 —— 上传超过服务端上限。"""


class ValidationError(APIStatusError):
    """422 —— 请求体不合法。"""


class RateLimitError(APIStatusError):
    """429 —— 触发限流。可重试（SDK 已经自动重试过若干次）。"""


class ServerError(APIStatusError):
    """5xx —— 服务端错误。可重试。"""


class AuditFailedError(SagaxAuditError):
    """审计任务以 ``failed`` / ``cancelled`` 结束。

    这**不是** ``BLOCK`` 裁决：裁决是审计的正常结论，会作为结果返回。
    这个异常表示审计根本没跑完。
    """

    def __init__(self, message: str, *, audit_id: str = "",
                 status: str = "failed", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.audit_id = audit_id
        self.audit_status = status


class AuditTimeoutError(SagaxAuditError):
    """``wait_for_audit`` 超时。

    审计任务仍在云端跑 —— 它不会因为客户端不等了就被取消。拿着 ``audit_id``
    以后可以继续查，或者显式调 ``cancel_audit``。
    """

    def __init__(self, message: str, *, audit_id: str = "",
                 last_status: str = "", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.audit_id = audit_id
        self.last_status = last_status


#: HTTP 状态 → 异常类。没列出的 4xx 落到 :class:`APIStatusError`，
#: 5xx 落到 :class:`ServerError`。
STATUS_MAP: dict[int, type[APIStatusError]] = {
    401: AuthenticationError,
    402: QuotaExceededError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    413: PayloadTooLargeError,
    422: ValidationError,
    429: RateLimitError,
}


def error_for_status(status: int) -> type[APIStatusError]:
    """状态码 → 异常类。"""
    if status in STATUS_MAP:
        return STATUS_MAP[status]
    if status >= 500:
        return ServerError
    return APIStatusError


__all__ = [
    "SagaxAuditError", "ConfigurationError", "InsecureTransportError",
    "APIConnectionError", "APITimeoutError", "APIStatusError",
    "AuthenticationError", "QuotaExceededError", "PermissionDeniedError",
    "NotFoundError", "ConflictError", "PayloadTooLargeError",
    "ValidationError", "RateLimitError", "ServerError", "AuditFailedError",
    "AuditTimeoutError", "STATUS_MAP", "error_for_status",
]
