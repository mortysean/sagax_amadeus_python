"""SDK 契约测试 —— 不依赖任何服务端实现。

全部用例只打进程内的 ``http.server`` 桩：构造、鉴权、密钥脱敏、请求形状、
错误映射、重试退避、轮询等待。**这一份会同步到公开的 SDK 仓**，所以它不能
提到闭源引擎的任何模块。

需要真云端服务的端到端用例在 ``test_sagax_audit_live.py``，那份不公开。
不碰网络（只监听 127.0.0.1 随机端口）、不碰用户的 ~/.sagax-audit。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.join(REPO_ROOT, "backend")
SDK_ROOT = os.path.join(BACKEND_ROOT, "packages", "sagax-amadeus-sdk")

# 这份文件同时在两处跑：monorepo（包在 backend/packages/… 下）和公开的 SDK 仓
# （包就在仓库根）。只把**存在**的目录加进 sys.path —— 写死 monorepo 布局的话，
# 同步到公开仓就是一句 ImportError，而那时候没人会想到是路径设置的问题。
for _root in (SDK_ROOT, REPO_ROOT):
    if os.path.isdir(_root) and _root not in sys.path:
        sys.path.insert(0, _root)

from sagax_amadeus import (EvidenceItem, ReviewResult,  # noqa: E402
                           SagaxAuditClient)
from sagax_amadeus.exceptions import (APIConnectionError, APITimeoutError,  # noqa: E402
                                    AuditFailedError, AuditTimeoutError,
                                    AuthenticationError, ConfigurationError,
                                    ConflictError, InsecureTransportError,
                                    NotFoundError, PayloadTooLargeError,
                                    PermissionDeniedError, QuotaExceededError,
                                    RateLimitError, ServerError,
                                    ValidationError)
from sagax_amadeus.transport import HttpTransport, mask_key  # noqa: E402


# --------------------------------------------------------------------------- #
# Mock HTTP server：按脚本回响应，并把收到的请求原样记下来
# --------------------------------------------------------------------------- #
class _MockHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):        # 测试输出保持干净
        return

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({
            "method": method, "path": self.path, "body": body,
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        status, payload, headers = self.server.next_response(method, self.path)
        raw = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        ctype = headers.pop("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    do_GET = lambda self: self._handle("GET")          # noqa: E731
    do_POST = lambda self: self._handle("POST")        # noqa: E731
    do_PATCH = lambda self: self._handle("PATCH")      # noqa: E731
    do_PUT = lambda self: self._handle("PUT")          # noqa: E731
    do_DELETE = lambda self: self._handle("DELETE")    # noqa: E731


class MockServerMixin:
    """起一个受脚本控制的 HTTP server。"""

    def start_mock(self, script=None):
        """Args:
            script: ``[(status, payload, headers), …]`` 队列；用完后循环最后一条。

        Returns:
            ``(client, server)``。
        """
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        server.daemon_threads = True
        server.requests = []
        server.script = list(script or [(200, {"ok": True}, {})])
        server.calls = 0

        def next_response(method, path):
            idx = min(server.calls, len(server.script) - 1)
            server.calls += 1
            item = server.script[idx]
            if len(item) == 2:
                return item[0], item[1], {}
            return item

        server.next_response = next_response
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        client = SagaxAuditClient(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            api_key="sagax_sk_" + "a" * 40, max_retries=2)
        # 重试的退避不真等 —— 否则一个 429 用例要跑好几秒
        client.transport._sleep = lambda _s: None
        self.addCleanup(client.close)
        return client, server


# --------------------------------------------------------------------------- #
# 1. 客户端构造与 HTTPS 策略
# --------------------------------------------------------------------------- #
class ClientInitTest(unittest.TestCase):

    def test_base_url_is_normalised(self):
        c = SagaxAuditClient(base_url="https://audit.example.com/", api_key="k")
        self.assertEqual(c.base_url, "https://audit.example.com")
        self.assertEqual(c.project_id, "default")

    def test_https_required_for_remote_hosts(self):
        """非本机地址一律要求 HTTPS。

        Evidence、私有 Memory / Skill / Wiki 都走这条连接。明文发出去等于没有
        租户隔离 —— 中间任何一跳都能读到全部内容。
        """
        with self.assertRaises(InsecureTransportError) as ctx:
            SagaxAuditClient(base_url="http://audit.example.com", api_key="k")
        self.assertIn("https", str(ctx.exception).lower())

    def test_localhost_http_is_allowed_for_development(self):
        for url in ("http://127.0.0.1:4600", "http://localhost:4600"):
            self.assertTrue(SagaxAuditClient(base_url=url, api_key="k").base_url)

    def test_insecure_http_needs_an_explicit_opt_in(self):
        c = SagaxAuditClient(base_url="http://audit.example.com", api_key="k",
                             allow_insecure_http=True)
        self.assertEqual(c.base_url, "http://audit.example.com")

    def test_insecure_http_can_be_allowed_by_environment(self):
        """明文放行是**部署环境**的属性，不该被复制进每个调用点。

        逼调用方在每处 SagaxAuditClient(...) 里加 allow_insecure_http=True，
        结果是等服务端真上了 HTTPS 也没人记得删，防护永久失效。放在环境变量里，
        上了 TLS 之后删一行 env 就全线恢复。
        """
        with unittest.mock.patch.dict(
                os.environ, {"SAGAX_AUDIT_ALLOW_INSECURE_HTTP": "1"}):
            c = SagaxAuditClient(base_url="http://203.0.113.10", api_key="k")
            self.assertEqual(c.base_url, "http://203.0.113.10")

    def test_environment_opt_in_must_be_explicit(self):
        """没设、或设成假值，照样拒绝 —— 默认永远是拒绝。"""
        for value in ("", "0", "false", "no", "maybe"):
            with self.subTest(value=value):
                env = ({"SAGAX_AUDIT_ALLOW_INSECURE_HTTP": value} if value
                       else {})
                with unittest.mock.patch.dict(os.environ, env, clear=False):
                    if not value:
                        os.environ.pop("SAGAX_AUDIT_ALLOW_INSECURE_HTTP", None)
                        os.environ.pop("SAGAX_ALLOW_INSECURE_HTTP", None)
                    with self.assertRaises(InsecureTransportError):
                        SagaxAuditClient(base_url="http://203.0.113.10",
                                         api_key="k")

    def test_insecure_error_names_both_ways_out(self):
        """报错要同时给出正解（上 HTTPS）和临时出口（显式放行）。"""
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAGAX_AUDIT_ALLOW_INSECURE_HTTP", None)
            os.environ.pop("SAGAX_ALLOW_INSECURE_HTTP", None)
            with self.assertRaises(InsecureTransportError) as ctx:
                SagaxAuditClient(base_url="http://203.0.113.10", api_key="k")
        message = str(ctx.exception)
        self.assertIn("https://", message)
        self.assertIn("SAGAX_AUDIT_ALLOW_INSECURE_HTTP", message)

    def test_ca_bundle_adds_trust_without_replacing_the_store(self):
        """私有 CA 是**追加**信任，不是替换。

        换成 SSL_CERT_FILE 环境变量能达到类似效果，但那会顶掉整个进程的信任库
        —— 调用方的程序访问其他 HTTPS 站点会跟着一起挂。
        """
        import ssl
        import tempfile as tf

        from sagax_amadeus.transport import build_ssl_context

        self.assertIsNone(build_ssl_context(), "不给 CA 时应当用系统默认信任")

        # 拿系统里现成的一张 CA 当样本，验证 load 成功且系统信任仍在
        default_count = len(ssl.create_default_context().get_ca_certs())
        pem = ssl.get_default_verify_paths().cafile
        if not pem or not os.path.isfile(pem):
            self.skipTest("这台机器上找不到系统 CA bundle")
        with tf.NamedTemporaryFile("w", suffix=".crt", delete=False) as fh:
            with open(pem, encoding="utf-8", errors="ignore") as src:
                fh.write(src.read())
            path = fh.name
        self.addCleanup(os.unlink, path)
        ctx = build_ssl_context(path)
        self.assertIsNotNone(ctx)
        self.assertGreaterEqual(len(ctx.get_ca_certs()), default_count,
                                "系统原有信任必须还在")

    def test_ca_bundle_reads_the_environment(self):
        from sagax_amadeus.transport import build_ssl_context
        with unittest.mock.patch.dict(
                os.environ, {"SAGAX_AUDIT_CA_BUNDLE": "/definitely/not/here.crt"}):
            with self.assertRaises(ConfigurationError):
                build_ssl_context()

    def test_ca_bundle_failure_is_a_configuration_error(self):
        """坏路径要在构造客户端时就炸，而不是等第一个请求发出去。"""
        from sagax_amadeus.transport import build_ssl_context
        with self.assertRaises(ConfigurationError) as ctx:
            build_ssl_context("/nonexistent/ca.crt")
        self.assertIn("/nonexistent/ca.crt", str(ctx.exception))

    def test_there_is_no_verify_false_footgun(self):
        """SDK 刻意不提供关闭证书校验的开关。

        关掉之后中间人可以完整读写 API Key 与 Evidence，而调用方多半永远不会
        把它改回来。要么信任一张明确的 CA，要么用公信证书。
        """
        import inspect

        from sagax_amadeus.client import SagaxAuditClient
        from sagax_amadeus.transport import HttpTransport
        for target in (SagaxAuditClient.__init__, HttpTransport.__init__):
            params = set(inspect.signature(target).parameters)
            self.assertFalse(params & {"verify", "insecure", "no_verify",
                                       "verify_ssl", "check_hostname"},
                             f"{target.__qualname__} 冒出了关闭校验的参数")

    def test_bad_configuration_fails_before_any_request(self):
        """配错的客户端应当立刻失败，而不是在生产流量里变成一串 401。"""
        with self.assertRaises(ConfigurationError):
            # 显式传空串是配置错误（多半是 os.environ.get(..., "")），
            # 不能静默回落到本机地址 —— 那样生产环境会什么都审不了还不报错。
            SagaxAuditClient(base_url="", api_key="k")
        with self.assertRaises(ConfigurationError):
            SagaxAuditClient(base_url="audit.example.com", api_key="k")
        with self.assertRaises(ConfigurationError):
            SagaxAuditClient(base_url="ftp://audit.example.com", api_key="k")
        with self.assertRaises(ConfigurationError):
            SagaxAuditClient(base_url="https://x", api_key="k", timeout=0)
        with self.assertRaises(ConfigurationError):
            SagaxAuditClient(base_url="https://x", api_key="k", max_retries=-1)

    def test_reads_environment_including_legacy_names(self):
        """新变量优先；改名前配好的旧变量继续生效。"""
        env = {"SAGAX_CLOUD_URL": "https://old.example.com",
               "SAGAX_API_KEY": "sagax_sk_old"}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(SagaxAuditClient().base_url,
                             "https://old.example.com")
        env["SAGAX_AUDIT_API_BASE_URL"] = "https://new.example.com"
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(SagaxAuditClient().base_url,
                             "https://new.example.com")


# --------------------------------------------------------------------------- #
# 2. 凭证脱敏
# --------------------------------------------------------------------------- #
class SecretHygieneTest(MockServerMixin, unittest.TestCase):

    #: 假 Key。行尾那个 `notarealkey` 是给 scripts/check_sdist_contents.py 看的
    #: 显式标记：它和真 Key 在正则眼里长得一样，没有这个标记，同步到公开仓时
    #: 会被当成泄漏的订阅 Key 挡下来。
    SECRET = "sagax_sk_0123456789abcdef0123456789abcdef01234567"  # notarealkey

    def test_mask_keeps_prefix_and_tail_only(self):
        masked = mask_key(self.SECRET)
        self.assertNotIn("0123456789abcdef", masked)
        self.assertTrue(masked.startswith("sagax_"))
        self.assertTrue(masked.endswith("4567"))
        self.assertEqual(mask_key(None), "<none>")

    def test_key_never_in_repr(self):
        c = SagaxAuditClient(base_url="https://x.example.com", api_key=self.SECRET)
        for text in (repr(c), str(c), repr(c.transport), str(c.transport)):
            self.assertNotIn(self.SECRET, text)

    def test_key_never_in_exceptions(self):
        """错误路径也不能漏 —— 异常最常被原样打进日志。"""
        client, server = self.start_mock([
            (401, {"error": "invalid_api_key", "message": "API Key 无效"}, {})])
        client.transport.api_key = self.SECRET
        with self.assertRaises(AuthenticationError) as ctx:
            client.list_memories()
        blob = f"{ctx.exception} {ctx.exception.args} {ctx.exception.payload}"
        self.assertNotIn(self.SECRET, blob)

    def test_key_travels_in_the_header_not_the_url(self):
        """Key 只能在请求头里。进 URL 会被代理日志、浏览器历史一起记下来。"""
        client, server = self.start_mock([(200, {"memories": []}, {})])
        client.transport.api_key = self.SECRET
        client.list_memories(query="pe")
        req = server.requests[0]
        self.assertNotIn(self.SECRET, req["path"])
        self.assertEqual(req["headers"]["authorization"], f"Bearer {self.SECRET}")

    def test_cli_output_scrubs_secrets(self):
        from sagax_amadeus.cli import scrub_secrets
        self.assertEqual(scrub_secrets({"api_key": self.SECRET})["api_key"],
                         "[REDACTED]")
        self.assertNotIn(self.SECRET,
                         scrub_secrets(f"connected with {self.SECRET}"))

    def test_cli_version_flag_works_offline(self):
        """``--version`` 必须不联网、不要凭据就能答出装的是哪一版。

        它守的是 4.0.0 那道破坏性接口墙的退路：按 ``>=3`` 装的人升上来会撞
        ``AttributeError``，那一刻唯一要查的就是自己装了哪版。同名的 ``version``
        子命令查的是**服务端**，要 base_url + Key —— 拿它当退路等于要求用户先有
        一个能连的服务端才能诊断自己的客户端。
        """
        import contextlib
        from sagax_amadeus import __version__
        from sagax_amadeus.cli import main

        buf = io.StringIO()
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stdout(buf):
            main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(__version__, buf.getvalue())


# --------------------------------------------------------------------------- #
# 3. 请求构造
# --------------------------------------------------------------------------- #
class RequestShapeTest(MockServerMixin, unittest.TestCase):

    def test_paths_and_query_params(self):
        client, server = self.start_mock([(200, {"memories": []}, {})] * 6)
        client.list_memories(query="pe", limit=7)
        client.get_memory("mem_1")
        client.list_evidence(limit=3)
        client.get_review("rv_1")
        client.list_wiki_documents()
        paths = [r["path"] for r in server.requests]
        self.assertIn("/v1/memories?q=pe&limit=7", paths)
        self.assertIn("/v1/memories/mem_1", paths)
        self.assertIn("/v1/evidence?limit=3", paths)
        self.assertIn("/v1/reviews/rv_1", paths)
        # 空参数不进 query，免得服务端收到一堆 q=
        self.assertIn("/v1/wiki?limit=100", paths)

    def test_resource_ids_are_escaped(self):
        """id 是客户传进来的字符串；不转义就是一条路径穿越。"""
        client, server = self.start_mock([(200, {}, {})])
        client.get_memory("../../v1/admin/tenants")
        path = server.requests[0]["path"]
        # 分隔符被转义，id 仍然是**一个**路径片段，穿越不出去
        self.assertTrue(path.startswith("/v1/memories/"))
        self.assertNotIn("/", path[len("/v1/memories/"):])
        with self.assertRaises(ConfigurationError):
            client.get_memory("")

    def test_review_sends_the_document_as_raw_body(self):
        """文档走 raw body，证据 id 走 query。

        把一份 PDF base64 塞进 JSON，请求体会涨三分之一，两头还各要编解码一次。
        """
        client, server = self.start_mock([(200, {"review_id": "rv_1"}, {})])
        client.review(b"%PDF-1.4 fake", filename="report.pdf",
                      evidence_ids=["ev_1", "ev_2"])
        request = server.requests[0]
        self.assertEqual(request["body"], b"%PDF-1.4 fake")
        self.assertIn("filename=report.pdf", request["path"])
        self.assertIn("evidence_ids=ev_1%2Cev_2", request["path"])
        # 默认自动取证，不把 auto_evidence=true 当噪音传出去
        self.assertNotIn("auto_evidence", request["path"])

    def test_review_sends_code_and_auto_evidence_false(self):
        """关自动取证时必须显式传 false；证券代码走 query。"""
        client, server = self.start_mock([(200, {"review_id": "rv_1"}, {})])
        client.review(b"doc", filename="report.md", code="600519.SH",
                      auto_evidence=False)
        path = server.requests[0]["path"]
        self.assertIn("code=600519.SH", path)
        self.assertIn("auto_evidence=false", path)

    def test_each_artifact_has_its_own_endpoint(self):
        """三件产物三个端点，别互相顶掉。

        它们长得像、参数一样、返回都是 ``bytes``，所以复制粘贴时改漏一个 URL
        不会有任何报错 —— 只是要一页 HTML 时静静地拿到一份 Markdown。
        """
        client, server = self.start_mock(
            [(200, b"artifact", {"Content-Type": "application/octet-stream"})])
        client.download_annotated("rv_1")
        client.download_review_report("rv_1")
        client.download_review_card("rv_1")
        client.download_review_card_pdf("rv_1")
        self.assertEqual([r["path"] for r in server.requests],
                         ["/v1/reviews/rv_1/annotated",
                          "/v1/reviews/rv_1/report",
                          "/v1/reviews/rv_1/card",
                          "/v1/reviews/rv_1/card.pdf"])

    def test_download_review_card_returns_bytes_or_writes_a_file(self):
        """``dest=None`` 回字节，给了路径就落盘并回路径 —— 和其余产物一致。"""
        page = "<!doctype html><title>卡片</title>".encode("utf-8")
        client, _ = self.start_mock(
            [(200, page, {"Content-Type": "text/html; charset=utf-8"})])
        self.assertEqual(client.download_review_card("rv_1"), page)
        dest = os.path.join(tempfile.mkdtemp(), "nested", "card.html")
        self.addCleanup(shutil.rmtree, os.path.dirname(os.path.dirname(dest)),
                        True)
        self.assertEqual(client.download_review_card("rv_1", dest), dest)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), page)

    def test_evidence_upload_sends_raw_body_with_metadata_headers(self):
        """文件走 raw body：多一层 base64 只会让 20MB 附件变成 27MB。"""
        client, server = self.start_mock([(201, {"evidence_id": "ev_1"}, {})])
        client.upload_evidence(data=b"col_a,col_b\n1,2\n", filename="fy25.csv",
                               content_type="text/csv",
                               metadata={"source": "annual-report"})
        req = server.requests[0]
        self.assertEqual(req["body"], b"col_a,col_b\n1,2\n")
        self.assertEqual(req["headers"]["content-type"], "text/csv")
        self.assertEqual(req["headers"]["x-sagax-filename"], "fy25.csv")
        self.assertEqual(json.loads(req["headers"]["x-sagax-metadata"]),
                         {"source": "annual-report"})

    def test_upload_evidence_validates_its_inputs_locally(self):
        """接口参数层面的基础校验留在 SDK；业务校验一律在云端。"""
        client, _ = self.start_mock()
        with self.assertRaises(ConfigurationError):
            client.upload_evidence()
        with self.assertRaises(ConfigurationError):
            client.upload_evidence(path="/nonexistent/nope.json")

    def test_project_header_is_sent(self):
        client, server = self.start_mock([(200, {"memories": []}, {})])
        client.transport.project_id = "research"
        client.list_memories()
        self.assertEqual(server.requests[0]["headers"]["x-sagax-project"],
                         "research")


# --------------------------------------------------------------------------- #
# 4. 状态码 → 异常
# --------------------------------------------------------------------------- #
class ErrorMappingTest(MockServerMixin, unittest.TestCase):

    CASES = [(401, AuthenticationError), (402, QuotaExceededError),
             (403, PermissionDeniedError), (404, NotFoundError),
             (409, ConflictError), (413, PayloadTooLargeError),
             (422, ValidationError), (429, RateLimitError),
             (500, ServerError), (503, ServerError)]

    def test_status_codes_map_to_typed_exceptions(self):
        for status, exc_type in self.CASES:
            with self.subTest(status=status):
                client, _ = self.start_mock([
                    (status, {"error": "boom", "message": "出错了",
                              "request_id": "req_1"}, {})])
                # 429/5xx 会重试，脚本里最后一条会一直复用，最终仍然抛出
                with self.assertRaises(exc_type) as ctx:
                    client.list_memories()
                self.assertEqual(ctx.exception.status, status)
                self.assertEqual(ctx.exception.code, "boom")
                self.assertEqual(ctx.exception.request_id, "req_1")
                self.assertIn("出错了", str(ctx.exception))

    def test_connection_error_when_nothing_is_listening(self):
        client = SagaxAuditClient(base_url="http://127.0.0.1:9", api_key="k",
                                  max_retries=0)
        with self.assertRaises(APIConnectionError):
            client.health()

    def test_timeout_maps_to_timeout_error(self):
        import socket

        def slow_opener(req, timeout=None):
            raise socket.timeout("timed out")

        transport = HttpTransport("https://x.example.com", "k", timeout=0.01,
                                  max_retries=0, opener=slow_opener)
        with self.assertRaises(APITimeoutError):
            transport.get("/v1/usage")

    def test_non_json_error_body_still_produces_a_typed_error(self):
        client, _ = self.start_mock([
            (502, b"<html>bad gateway</html>",
             {"Content-Type": "text/html"})])
        with self.assertRaises(ServerError):
            client.list_memories()


# --------------------------------------------------------------------------- #
# 5. 重试与连接生命周期
# --------------------------------------------------------------------------- #
class RetryTest(MockServerMixin, unittest.TestCase):

    def test_idempotent_request_is_retried(self):
        client, server = self.start_mock([
            (503, {"error": "unavailable", "message": "稍后再试"}, {}),
            (200, {"memories": []}, {})])
        client.list_memories()
        self.assertEqual(len(server.requests), 2, "GET 应当重试一次")

    def test_non_idempotent_request_is_not_retried(self):
        """``POST /v1/reviews`` 重试一次 = 多跑一次审计 + 多扣一次配额。

        宁可把 503 抛给调用方，也不能悄悄下两单。
        """
        client, server = self.start_mock([
            (503, {"error": "unavailable", "message": "稍后再试"}, {})])
        with self.assertRaises(ServerError):
            client.review(b"doc", filename="a.md")
        self.assertEqual(len(server.requests), 1, "POST 不得自动重试")

    def test_four_hundreds_are_not_retried(self):
        client, server = self.start_mock([
            (404, {"error": "not_found", "message": "没有"}, {})])
        with self.assertRaises(NotFoundError):
            client.get_memory("mem_x")
        self.assertEqual(len(server.requests), 1)

    def test_retry_budget_is_finite(self):
        client, server = self.start_mock([
            (429, {"error": "rate_limited", "message": "慢点"}, {})])
        with self.assertRaises(RateLimitError):
            client.list_memories()
        self.assertEqual(len(server.requests), 3, "1 次 + 2 次重试")

    def test_closed_client_refuses_further_requests(self):
        client, server = self.start_mock([(200, {"memories": []}, {})])
        client.list_memories()
        client.close()
        self.assertTrue(client.transport.closed)
        with self.assertRaises(ConfigurationError):
            client.list_memories()
        self.assertEqual(len(server.requests), 1)

    def test_context_manager_closes(self):
        client, _ = self.start_mock()
        with client as c:
            self.assertIs(c, client)
        self.assertTrue(client.transport.closed)


# --------------------------------------------------------------------------- #
# 6. 审计任务的轮询语义
# --------------------------------------------------------------------------- #
class MCPToolWiringTest(MockServerMixin, unittest.TestCase):
    """每个注册了的 MCP 工具都必须真的调得动。

    `sagax.runs` 曾经在客户端方法被删掉之后还留在工具表里，注册看着好好的，
    一调就 AttributeError —— 而 MCP 工具的错误是包成 isError 返回的，不会让
    进程挂掉，所以这种坏法**完全静默**：Agent 那边只看到一句莫名其妙的报错。

    一个注册了但调不动的工具，比没有这个工具更糟。
    """

    def test_every_registered_tool_is_actually_callable(self):
        from sagax_amadeus.mcp_server import SagaxMCPServer

        client, _ = self.start_mock([(200, {"reviews": [], "items": [],
                                            "results": [], "ok": True}, {})])
        server = SagaxMCPServer(client)
        self.assertGreaterEqual(len(server.tools), 13)

        # 用每个工具 schema 里的 required 字段凑最小参数；没有 required 就空调。
        broken = []
        for name, (spec, handler) in sorted(server.tools.items()):
            schema = spec.get("inputSchema") or {}
            args = {key: _stub_for(schema["properties"][key])
                    for key in schema.get("required", [])}
            try:
                handler(**args)
            except AttributeError as exc:      # 客户端上没有这个方法 —— 接线断了
                broken.append(f"{name}: {exc}")
            except Exception:                  # noqa: BLE001
                pass                           # 参数/网络问题不是本用例要抓的
        self.assertEqual(broken, [], "这些工具注册了但调不动")

    def test_review_tool_description_states_behavior_boundaries(self):
        """Agent 常常不读 SKILL.md，工具描述是唯一能拦住「随手换数」的地方。"""
        from sagax_amadeus.mcp_server import SagaxMCPServer

        client, _ = self.start_mock()
        server = SagaxMCPServer(client)
        spec, _ = server.tools["sagax.review"]
        desc = spec["description"]
        for needle in ("不算通过", "不要随手换一个数", "粉饰不是修复"):
            self.assertIn(needle, desc)
        path_desc = spec["inputSchema"]["properties"]["document_path"]["description"]
        self.assertIn("文字层", path_desc)
        self.assertIn("code", spec["inputSchema"]["properties"])


def _stub_for(prop: dict):
    """按 JSON Schema 类型造一个占位值。"""
    kind = prop.get("type")
    if kind == "array":
        return []
    if kind == "integer":
        return 1
    if kind == "object":
        return {}
    return "x"


if __name__ == "__main__":
    unittest.main()
