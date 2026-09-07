"""Sagax Audit SDK — HTTP 传输层。

标准库 ``urllib`` 实现，没有第三方依赖。它负责且只负责六件事：

    1. base_url 校验与 HTTPS 策略
    2. 鉴权头
    3. 序列化 / 反序列化
    4. 超时
    5. 重试（只对幂等请求）
    6. HTTP 状态 → :mod:`sagax_amadeus.exceptions` 的异常

**这里没有任何审计逻辑。** 传输层看不懂 Finding，也不该看懂。

三条安全性质，都有测试守着：

  * API Key 只出现在请求头里。它不进 URL（会被代理日志、浏览器历史记下来）、
    不进异常、不进 ``repr``、不进任何 SDK 打的日志。
  * 非本机地址一律要求 HTTPS。Evidence 和私有 Memory 走这条连接，明文发出去
    等于没有租户隔离。
  * 只有幂等方法会重试。``POST /v1/audits`` 重试一次就是多跑一次审计、多扣
    一次配额 —— 宁可把超时抛给调用方。
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, BinaryIO, Callable, Optional

from sagax_amadeus.exceptions import (APIConnectionError, APITimeoutError,
                                    ConfigurationError, InsecureTransportError,
                                    SagaxAuditError, error_for_status)

#: HTTP 语义上幂等的方法 —— 只有它们会被自动重试。
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: 值得重试的状态码。4xx 里只有 429：其余 4xx 重试多少次都还是同样的错。
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: 本机地址允许明文 HTTP（开发环境）。
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2
USER_AGENT = "sagax-amadeus-python"


def mask_key(api_key: Optional[str]) -> str:
    """API Key → 可以安全打印的形式。

    ``sagax_sk_1a2b3c…`` → ``sagax_sk_…3c4d``。保留前缀（认得出是哪类 Key）
    和末四位（对得上是哪一把），中间全部丢掉。空值返回 ``<none>``，
    **不返回空串** —— 日志里一个空串看不出是「没配」还是「被抹了」。
    """
    if not api_key:
        return "<none>"
    text = str(api_key)
    prefix = text.split("_")[0] + "_" if "_" in text[:12] else text[:4]
    return f"{prefix}…{text[-4:]}" if len(text) > 8 else "…"


def insecure_http_allowed() -> bool:
    """是否由环境放行了明文 HTTP（``SAGAX_AUDIT_ALLOW_INSECURE_HTTP``）。

    存在的理由：服务端还没上 TLS 时，明文放行是**部署环境**的属性，不是每处
    调用点的属性。逼客户在每个 ``SagaxAuditClient(...)`` 里加一个
    ``allow_insecure_http=True``，结果是这个参数被复制得到处都是 ——
    等真上了 HTTPS 也没人记得删，防护就永久失效了。

    放在环境变量里，上了 HTTPS 之后删掉一行 env 就全线恢复防护。

    仍然是**显式 opt-in**：不设就照样拒绝。
    """
    import os
    raw = (os.environ.get("SAGAX_AUDIT_ALLOW_INSECURE_HTTP")
           or os.environ.get("SAGAX_ALLOW_INSECURE_HTTP") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def build_ssl_context(ca_bundle: Optional[str] = None):
    """构造 TLS 校验上下文。

    Args:
        ca_bundle: 额外信任的 CA 证书文件（PEM）。默认取
            ``SAGAX_AUDIT_CA_BUNDLE``。

    Returns:
        :class:`ssl.SSLContext`；没有 ca_bundle 时返回 None（用系统默认信任库）。

    Raises:
        ConfigurationError: 证书文件不存在或不是合法 PEM。

    私有 CA（例如服务端用 Caddy 内置 CA 自签）时需要它。**不要**改用
    ``SSL_CERT_FILE`` 环境变量绕过去：那个变量会替换掉整个进程的信任库，
    你的程序访问其他 HTTPS 站点会跟着一起挂。这里是**追加**信任一张 CA，
    系统原有的信任链不受影响。

    刻意没有 ``verify=False``。关掉校验之后，中间人可以完整读写你的
    API Key 与 Evidence，而调用方多半永远不会把它改回来。要么信任一张
    明确的 CA，要么用公信 CA 的证书。
    """
    import os
    import ssl

    path = ca_bundle or os.environ.get("SAGAX_AUDIT_CA_BUNDLE") or ""
    path = path.strip()
    if not path:
        return None
    if not os.path.isfile(path):
        raise ConfigurationError(f"CA 证书文件不存在: {path}")
    context = ssl.create_default_context()      # 先带上系统默认信任
    try:
        context.load_verify_locations(cafile=path)   # 再追加这一张
    except (ssl.SSLError, OSError) as exc:
        raise ConfigurationError(f"CA 证书加载失败（{path}）: {exc}") from exc
    return context


def normalize_base_url(base_url: str, *, allow_insecure_http: bool = False
                       ) -> str:
    """校验并归一化 base_url。

    Args:
        base_url: 云端地址，如 ``https://audit.example.com``。
        allow_insecure_http: 是否允许对非本机地址使用明文 HTTP；False 时还会
            看 ``SAGAX_AUDIT_ALLOW_INSECURE_HTTP``（见 :func:`insecure_http_allowed`）。

    Returns:
        去掉尾部斜杠的地址。

    Raises:
        ConfigurationError: 地址为空、缺 scheme、或 scheme 不是 http/https。
        InsecureTransportError: 对非本机地址用了 http 且没有任何显式放行。
    """
    text = str(base_url or "").strip().rstrip("/")
    if not text:
        raise ConfigurationError(
            "缺少云端地址。请传 base_url=…，或设置 SAGAX_AUDIT_API_BASE_URL。")
    parsed = urllib.parse.urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        raise ConfigurationError(
            f"base_url 必须是完整地址（含 https://），收到: {text!r}")
    if parsed.scheme not in ("http", "https"):
        raise ConfigurationError(
            f"base_url 只支持 http / https，收到 {parsed.scheme!r}")
    if parsed.scheme == "http":
        host = (parsed.hostname or "").lower()
        if (host not in LOCAL_HOSTS and not allow_insecure_http
                and not insecure_http_allowed()):
            raise InsecureTransportError(
                f"拒绝对 {host} 使用明文 HTTP：你的 API Key、原始 Evidence 与"
                "私有 Memory / Skill / Wiki 都走这条连接，明文即链路上任何一跳"
                "都能读到。\n"
                "  · 正解：服务端上 HTTPS，base_url 换成 https://\n"
                "  · 服务端确实还没上 TLS（或 TLS 在更外层终止）时，显式放行：\n"
                "      export SAGAX_AUDIT_ALLOW_INSECURE_HTTP=1\n"
                "    或 SagaxAuditClient(..., allow_insecure_http=True)")
    return text


class HttpTransport:
    """一个到 Sagax Audit Cloud 的 HTTP 连接配置。"""

    def __init__(self, base_url: str, api_key: Optional[str] = None, *,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 project_id: str = "default",
                 allow_insecure_http: bool = False,
                 ca_bundle: Optional[str] = None,
                 user_agent: str = USER_AGENT,
                 opener: Optional[Callable[..., Any]] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """Args:
            base_url: 云端地址。
            api_key: 订阅 API Key。
            timeout: 单次请求超时（秒），必须为正。
            max_retries: 幂等请求的最大重试次数（0 = 不重试）。
            project_id: 默认项目 id（租户内二次隔离）。
            allow_insecure_http: 对非本机地址放行明文 HTTP；不传时还会看
                ``SAGAX_AUDIT_ALLOW_INSECURE_HTTP``。
            ca_bundle: 额外信任的 CA 证书（PEM）；服务端用私有 CA 时需要。
                默认取 ``SAGAX_AUDIT_CA_BUNDLE``。
            user_agent: User-Agent。
            opener: 注入的 urlopen（测试用）。
            sleep: 注入的 sleep（测试用，免得真的等）。

        Raises:
            ConfigurationError: base_url / timeout / max_retries 不合法。
            InsecureTransportError: 非本机地址用了明文 HTTP。
        """
        self.base_url = normalize_base_url(
            base_url, allow_insecure_http=allow_insecure_http)
        self.api_key = (api_key or "").strip() or None
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"timeout 必须是数字，收到 {timeout!r}") from exc
        if self.timeout <= 0:
            raise ConfigurationError(f"timeout 必须为正数，收到 {self.timeout}")
        if int(max_retries) < 0:
            raise ConfigurationError("max_retries 不能为负")
        self.max_retries = int(max_retries)
        self.project_id = project_id or "default"
        self.ssl_context = build_ssl_context(ca_bundle)
        self.user_agent = user_agent
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._closed = False

    # ---- 表示 -----------------------------------------------------------
    def __repr__(self) -> str:
        """**永远**脱敏。一个不小心被打进日志的 repr 不该泄露客户的 Key。"""
        return (f"HttpTransport(base_url={self.base_url!r}, "
                f"api_key={mask_key(self.api_key)!r}, "
                f"timeout={self.timeout}, max_retries={self.max_retries})")

    __str__ = __repr__

    # ---- 生命周期 --------------------------------------------------------
    def close(self) -> None:
        """标记关闭。之后再发请求会直接报错，而不是悄悄继续用。"""
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    # ---- 请求 ------------------------------------------------------------
    def headers(self, *, content_type: Optional[str] = None,
                extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        """构造请求头（含鉴权）。"""
        out = {"Accept": "application/json", "User-Agent": self.user_agent,
               "X-Sagax-Project": self.project_id}
        if content_type:
            out["Content-Type"] = content_type
        if self.api_key:
            out["Authorization"] = f"Bearer {self.api_key}"
        out.update(extra or {})
        return out

    def request(self, method: str, path: str, *,
                json_body: Optional[dict[str, Any]] = None,
                raw_body: Optional[bytes] = None,
                content_type: Optional[str] = None,
                params: Optional[dict[str, Any]] = None,
                extra_headers: Optional[dict[str, str]] = None,
                expect_binary: bool = False,
                idempotent: Optional[bool] = None) -> Any:
        """发一个请求。

        Args:
            method: HTTP 方法。
            path: 以 ``/`` 开头的路径。
            json_body: JSON 请求体。
            raw_body: 原始字节请求体（文件上传）。与 json_body 互斥。
            content_type: raw_body 的 MIME 类型。
            params: query 参数（None / "" 的项会被丢掉）。
            extra_headers: 附加请求头。
            expect_binary: True 时返回 ``(bytes, headers)``，不做 JSON 解析。
            idempotent: 覆盖幂等判定。**只在你确信重发安全时才传 True。**

        Returns:
            解析后的 JSON（``dict``），或 ``(bytes, dict)``。

        Raises:
            SagaxAuditError: 及其子类。
        """
        if self._closed:
            raise ConfigurationError(
                "客户端已关闭（close() 调用过了），不能再发请求。")
        if json_body is not None and raw_body is not None:
            raise ConfigurationError("json_body 与 raw_body 不能同时给")

        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)

        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False,
                              default=str).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        else:
            body = raw_body
            ctype = content_type

        retryable = (method.upper() in IDEMPOTENT_METHODS
                     if idempotent is None else bool(idempotent))
        attempts = self.max_retries + 1 if retryable else 1
        last_exc: Optional[SagaxAuditError] = None

        for attempt in range(1, attempts + 1):
            try:
                return self._once(method, url, body, ctype, extra_headers,
                                  expect_binary)
            except SagaxAuditError as exc:
                last_exc = exc
                status = getattr(exc, "status", None)
                worth_retry = (isinstance(exc, APIConnectionError)
                               or (status in RETRYABLE_STATUS))
                if attempt >= attempts or not worth_retry:
                    raise
                self._sleep(self._backoff(attempt, exc))
        assert last_exc is not None                      # pragma: no cover
        raise last_exc                                   # pragma: no cover

    def _once(self, method: str, url: str, body: Optional[bytes],
              content_type: Optional[str],
              extra_headers: Optional[dict[str, str]],
              expect_binary: bool) -> Any:
        req = urllib.request.Request(
            url, data=body,
            headers=self.headers(content_type=content_type, extra=extra_headers),
            method=method.upper())
        kwargs = {"timeout": self.timeout}
        if self.ssl_context is not None:
            kwargs["context"] = self.ssl_context
        try:
            with self._opener(req, **kwargs) as resp:
                payload = resp.read()
                headers = dict(getattr(resp, "headers", {}) or {})
                if expect_binary:
                    return payload, headers
                return _decode_json(payload, headers)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except TimeoutError as exc:
            raise APITimeoutError(
                f"请求超时（{self.timeout}s）: {method.upper()} {_safe_url(url)}"
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason):
                raise APITimeoutError(
                    f"请求超时（{self.timeout}s）: {method.upper()} "
                    f"{_safe_url(url)}") from exc
            raise APIConnectionError(
                f"连不上 Sagax Audit Cloud: {reason}") from exc
        except OSError as exc:
            raise APIConnectionError(f"连不上 Sagax Audit Cloud: {exc}") from exc

    def _backoff(self, attempt: int, exc: SagaxAuditError) -> float:
        """指数退避 + 抖动；服务端给了 Retry-After 就听它的。

        抖动是必须的：没有它，一次 503 会让所有客户端在同一毫秒重试，
        把刚缓过来的服务再打下去。
        """
        retry_after = (exc.payload or {}).get("retry_after")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except (TypeError, ValueError):
                pass
        return min(0.25 * (2 ** (attempt - 1)), 8.0) * (0.5 + random.random())

    # ---- 便捷方法 --------------------------------------------------------
    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw: Any) -> Any:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    def download(self, path: str, dest: Optional[str] = None, *,
                 params: Optional[dict[str, Any]] = None
                 ) -> "bytes | str":
        """下载一个二进制产物。

        Args:
            path: 端点路径。
            dest: 落盘路径；None 时直接返回字节。
            params: query 参数。

        Returns:
            ``dest`` 为 None 时返回 ``bytes``，否则返回写入的路径。
        """
        data, _headers = self.request("GET", path, params=params,
                                      expect_binary=True)
        if dest is None:
            return data
        import os
        parent = os.path.dirname(os.path.abspath(dest))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        return dest

    def upload(self, path: str, *, data: "bytes | BinaryIO",
               filename: str = "", content_type: str = "",
               metadata: Optional[dict[str, Any]] = None) -> Any:
        """上传一个文件。

        文件走 raw body（不是 multipart，也不是 base64）：多一层编码只会让
        20 MB 的附件变成 27 MB，且两端都要多一次内存拷贝。文件名与自定义元数据
        走请求头。

        Args:
            path: 端点路径。
            data: 字节或已打开的二进制文件对象。
            filename: 原始文件名。
            content_type: MIME 类型。
            metadata: 自定义元数据（JSON 序列化后进 ``X-Sagax-Metadata``）。
        """
        payload = data if isinstance(data, bytes) else data.read()
        headers = {}
        if filename:
            headers["X-Sagax-Filename"] = _header_safe(filename)
        if metadata:
            headers["X-Sagax-Metadata"] = json.dumps(
                metadata, ensure_ascii=True, default=str)
        return self.request(
            "POST", path, raw_body=payload,
            content_type=content_type or "application/octet-stream",
            extra_headers=headers)


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _decode_json(payload: bytes, headers: dict[str, str]) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SagaxAuditError(
            f"服务端返回的不是合法 JSON: {exc}",
            request_id=headers.get("X-Request-Id", "")) from exc
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def _http_error(exc: urllib.error.HTTPError) -> SagaxAuditError:
    """HTTPError → SDK 异常。

    错误体里可能有服务端拼进去的内容，但**绝不会**有请求头 —— API Key 只在
    请求头里，所以它进不了这条路径。
    """
    try:
        raw = exc.read()
    except Exception:                              # noqa: BLE001
        raw = b""
    payload: dict[str, Any] = {}
    if raw:
        try:
            decoded = json.loads(raw.decode("utf-8"))
            if isinstance(decoded, dict):
                payload = decoded
        except (UnicodeDecodeError, ValueError):
            payload = {"message": raw.decode("utf-8", "replace")[:500]}
    message = str(payload.get("message") or payload.get("error")
                  or exc.reason or f"HTTP {exc.code}")
    headers = dict(getattr(exc, "headers", {}) or {})
    retry_after = headers.get("Retry-After")
    if retry_after:
        payload.setdefault("retry_after", retry_after)
    return error_for_status(exc.code)(
        message, status=exc.code, code=str(payload.get("error") or ""),
        request_id=str(payload.get("request_id")
                       or headers.get("X-Request-Id") or ""),
        payload=payload)


def _safe_url(url: str) -> str:
    """URL → 可以进错误消息的形式（去掉 query，那里可能有客户填的检索词）。"""
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _header_safe(value: str) -> str:
    """把值裁成能安全放进 HTTP 头的形式（去掉换行，避免头注入）。"""
    return str(value).replace("\r", "").replace("\n", "")[:200]


__all__ = ["HttpTransport", "normalize_base_url", "mask_key",
           "IDEMPOTENT_METHODS", "RETRYABLE_STATUS", "LOCAL_HOSTS",
           "DEFAULT_TIMEOUT", "DEFAULT_MAX_RETRIES"]
