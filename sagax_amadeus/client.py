"""Sagax Amadeus SDK — 同步客户端。

    from sagax_amadeus import SagaxAuditClient

    client = SagaxAuditClient(base_url="https://audit.example.com",
                              api_key="sagax_sk_…")
    evidence = client.upload_evidence(path="fy2025.json")
    result = client.review("研报.md", evidence_ids=[evidence.evidence_id])
    print(result.grade.letter, result.blocking, result.coverage_note)
    client.download_review_report(result.review_id, "review.md")

这个类是**纯 HTTP 客户端**。它不解析文档、不跑校验器、不评级、不生成报告，
也不在本地存私有知识库。所有审阅逻辑在 Sagax Amadeus 云端引擎上执行，SDK 只负责
把文档发过去、把标注取回来。

远程失败时**不会**回落到本地审阅 —— 没有本地审阅可回落，这是有意的：
两套引擎会产生两套结论，而审阅结论的价值全部来自「只有一套」。

我们**不改宿主的文档**：审阅只给高亮、comment、评级和风险总结，改不改是
调用方的事。所以这里没有重试循环，一次请求一个结果。
"""
from __future__ import annotations

import os
from typing import Any, BinaryIO, Iterable, Optional, Union

from sagax_amadeus.exceptions import ConfigurationError
from sagax_amadeus.models import (Evidence, EvidenceItem, Memory, ReviewResult,
                                ServiceVersion, Skill, Usage, WikiDocument)
from sagax_amadeus.transport import (DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT,
                                   HttpTransport, mask_key)

#: 开发环境默认地址。生产必须显式给 https:// 地址 —— 一个「默认连本机」的
#: 生产配置会静默地什么都审不了，而不是响亮地报错。
DEFAULT_DEV_BASE_URL = "http://127.0.0.1:4600"


def _env(*names: str, default: str = "") -> str:
    """按顺序读第一个非空的环境变量。

    顺序固定为「新名字 → 项目里已经在用的名字 → 改名前的名字」，所以已经配好
    ``SAGAX_CLOUD_URL`` / ``AMADEUS_API_KEY`` 的机器不用为了 SDK 升级重配一遍。
    """
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return default


class SagaxAuditClient:
    """Sagax Amadeus 公网 API 的同步客户端。"""

    def __init__(self, *, base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 project_id: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 allow_insecure_http: bool = False,
                 ca_bundle: Optional[str] = None,
                 transport: Optional[HttpTransport] = None) -> None:
        """构造客户端。

        Args:
            base_url: 云端地址，如 ``https://audit.example.com``。默认依次取
                ``SAGAX_AUDIT_API_BASE_URL`` / ``SAGAX_CLOUD_URL`` /
                ``AMADEUS_CLOUD_URL``，都没有时回落到本机开发地址。
            api_key: 订阅 API Key。默认依次取 ``SAGAX_AUDIT_API_KEY`` /
                ``SAGAX_API_KEY`` / ``AMADEUS_API_KEY``。
            project_id: 项目 id（租户内的二次隔离）。默认 ``SAGAX_AUDIT_PROJECT_ID``
                或 ``default``。
            timeout: 单次 HTTP 请求超时（秒）。审一份文档比这久是常事，
                所以 :meth:`review` 自己带一个更长的 timeout。
            max_retries: 幂等请求的重试次数。
            allow_insecure_http: 对非本机地址放行明文 HTTP。不传时还会看
                ``SAGAX_AUDIT_ALLOW_INSECURE_HTTP=1`` —— 明文与否是部署环境的
                属性，不该被复制到每一个调用点。
            ca_bundle: 额外信任的 CA 证书（PEM）。服务端用私有 CA（自签）时给
                它，默认取 ``SAGAX_AUDIT_CA_BUNDLE``。这是**追加**信任，
                不会动你系统里原有的信任库。
            transport: 直接注入传输层（测试用）。

        Raises:
            ConfigurationError: 地址或超时不合法。
            InsecureTransportError: 对非本机地址用了明文 HTTP。
        """
        if transport is not None:
            self._t = transport
        else:
            if base_url is not None and not str(base_url).strip():
                # 显式传了空串 —— 多半是 os.environ.get("…", "") 取空了。
                # 静默回落到本机地址会让生产环境什么都审不了，而且不报错。
                raise ConfigurationError(
                    "base_url 是空的。不传（None）才会走环境变量与开发默认值；"
                    "显式传空串通常意味着配置没读到。")
            url = base_url or _env("SAGAX_AUDIT_API_BASE_URL", "SAGAX_CLOUD_URL",
                                   "AMADEUS_CLOUD_URL",
                                   default=DEFAULT_DEV_BASE_URL)
            key = api_key or _env("SAGAX_AUDIT_API_KEY", "SAGAX_API_KEY",
                                  "AMADEUS_API_KEY") or None
            project = project_id or _env("SAGAX_AUDIT_PROJECT_ID",
                                         "SAGAX_PROJECT_ID", default="default")
            self._t = HttpTransport(
                url, key, timeout=timeout, max_retries=max_retries,
                project_id=project, allow_insecure_http=allow_insecure_http,
                ca_bundle=ca_bundle)

    # ---- 表示 / 生命周期 -------------------------------------------------
    def __repr__(self) -> str:
        """脱敏。API Key 不进 repr，也就不会被一句 print 送进日志。"""
        return (f"SagaxAuditClient(base_url={self.base_url!r}, "
                f"api_key={mask_key(self._t.api_key)!r}, "
                f"project_id={self.project_id!r})")

    __str__ = __repr__

    @property
    def base_url(self) -> str:
        return self._t.base_url

    @property
    def project_id(self) -> str:
        return self._t.project_id

    @property
    def transport(self) -> HttpTransport:
        return self._t

    def close(self) -> None:
        """关闭客户端。之后再调任何方法都会报错。"""
        self._t.close()

    def __enter__(self) -> "SagaxAuditClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- 服务状态 --------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """存活探针（无需认证）。"""
        return self._t.get("/health")

    def version(self) -> ServiceVersion:
        """API / SDK / 审计引擎 / 公共规则的版本。"""
        return ServiceVersion.from_dict(self._t.get("/v1/version"))

    def usage(self) -> Usage:
        """订阅用量与配额。"""
        return Usage.from_dict(self._t.get("/v1/usage"))

    # ---- Evidence --------------------------------------------------------
    def upload_evidence(self, *, path: Optional[str] = None,
                        data: Union[bytes, BinaryIO, None] = None,
                        filename: str = "", content_type: str = "",
                        metadata: Optional[dict[str, Any]] = None) -> Evidence:
        """上传一份原始 Evidence 到你的租户空间。

        Args:
            path: 本地文件路径（与 ``data`` 二选一）。
            data: 字节或已打开的二进制文件对象。
            filename: 原始文件名；给了 ``path`` 时默认取它的 basename。
            content_type: MIME 类型；不给时按扩展名推断。
            metadata: 自定义元数据，随 Evidence 一起保存。

        Returns:
            :class:`~sagax_amadeus.models.Evidence` 元数据（正文不回传）。

        Raises:
            ConfigurationError: 没给 path 也没给 data，或文件不存在。
        """
        if (path is None) == (data is None):
            raise ConfigurationError("upload_evidence 需要 path 或 data 之一")
        if path is not None:
            if not os.path.isfile(path):
                raise ConfigurationError(f"文件不存在: {path}")
            with open(path, "rb") as fh:
                payload = fh.read()
            filename = filename or os.path.basename(path)
            content_type = content_type or _guess_type(filename)
        else:
            payload = data if isinstance(data, bytes) else data.read()  # type: ignore[union-attr]
            content_type = content_type or _guess_type(filename)
        return Evidence.from_dict(self._t.upload(
            "/v1/evidence", data=payload, filename=filename,
            content_type=content_type, metadata=metadata))

    def upload_evidence_items(self, items: Iterable[EvidenceItem], *,
                              filename: str = "evidence.json",
                              metadata: Optional[dict[str, Any]] = None
                              ) -> Evidence:
        """上传一批结构化证据（最常用的形式）。

        云端会把它们解析成可参与数值校验的条目，之后审阅时用
        ``evidence_ids=[…]`` 引用即可，不必再传一遍内容。
        """
        import json
        payload = json.dumps(
            {"evidence": [i.to_dict() if isinstance(i, EvidenceItem) else i
                          for i in items]},
            ensure_ascii=False).encode("utf-8")
        return Evidence.from_dict(self._t.upload(
            "/v1/evidence", data=payload, filename=filename,
            content_type="application/json", metadata=metadata))

    def list_evidence(self, limit: int = 100) -> list[Evidence]:
        """列出本租户本项目下的 Evidence。"""
        payload = self._t.get("/v1/evidence", params={"limit": limit})
        return [Evidence.from_dict(e) for e in payload.get("evidence", [])]

    def get_evidence(self, evidence_id: str) -> Evidence:
        """取一条 Evidence 的元数据。"""
        return Evidence.from_dict(
            self._t.get(f"/v1/evidence/{_seg(evidence_id)}"))

    def download_evidence(self, evidence_id: str,
                          dest: Optional[str] = None) -> Union[bytes, str]:
        """下载 Evidence 原文。``dest`` 为 None 时返回字节。"""
        return self._t.download(f"/v1/evidence/{_seg(evidence_id)}/content", dest)

    def delete_evidence(self, evidence_id: str) -> dict[str, Any]:
        """删除一条 Evidence（对象立即删除，元数据留一行删除记录）。"""
        return self._t.delete(f"/v1/evidence/{_seg(evidence_id)}")

    # ---- 文档审阅 --------------------------------------------------------
    def review(self, document: Union[str, bytes], *, filename: str = "",
               content_type: str = "",
               evidence: Iterable[Union[EvidenceItem, dict]] = (),
               evidence_ids: Iterable[str] = (),
               code: str = "", auto_evidence: bool = True,
               timeout: float = 300.0) -> ReviewResult:
        """审一份文档，同步返回结果（没有队列，也就没有轮询）。

        **证据可以不给。** 服务端会为报告里点了名的指标自己去数据源取真值
        （A 股财务指标），所以最简单的用法就是 ``client.review("report.pdf")``。

        自己给证据仍然有意义，而且更强：那是你的智能体声明「这个数来自这里」，
        比服务端事后独立查更贴近它实际用的口径。两者可以同时给，会合并。

        取不到的部分**不会被当成通过** —— 它们进 ``coverage_note`` 的「未核验」。
        服务端刻意不猜：主体、期间、口径任一含糊就不取数，因为猜错会把一个正确
        的数字标成错的。

        Args:
            document: 本地文件路径，或文档原始字节。
            filename: 原文件名；给了路径时默认取它的 basename。
            content_type: MIME 类型；不给时按文件名推断。
            evidence: 结构化证据。会先经 :meth:`upload_evidence_items` 落成一条
                Evidence，再把 id 并进 ``evidence_ids``。
            evidence_ids: 已上传 Evidence 的 id。
            code: 证券代码（如 ``600519.SH``）。不给时服务端从正文里认 ——
                报告里写了带交易所后缀的代码就能认出来。显式给更稳。
            auto_evidence: 是否允许服务端自动取证。设 False 退回「只用你给的
                证据」，此时不给证据就是每个数字都无源。
            timeout: 单次请求超时（秒）。自动取证要发外部请求，比纯校验慢。

        Returns:
            :class:`~sagax_amadeus.models.ReviewResult`：标注、评级、风险总结、
            覆盖度声明。

        Raises:
            ConfigurationError: 文件不存在，或 document 既不是路径也不是字节。
            ValidationError: 文档为空、格式不支持或已损坏。
            QuotaExceededError: 配额用尽。
        """
        data, filename = _document_bytes(document, filename)
        ids = list(evidence_ids)
        items = list(evidence)
        if items:
            # 证据不内联进请求体 —— body 是文档本身，没有第二个位置放它们。
            # 走同一套 /v1/evidence，所以证据在租户里只存一份、只有一套生命周期。
            ids.append(self.upload_evidence_items(items).evidence_id)
        ctype = content_type or _guess_type(filename)
        # idempotent=False 是重点：重发一次 = 多审一遍 + 多扣一次配额。
        return ReviewResult.from_dict(self._with_timeout(timeout).post(
            "/v1/reviews", raw_body=data, content_type=ctype,
            params={"filename": filename, "content_type": ctype,
                    "evidence_ids": ",".join(ids), "code": code,
                    # 只在关的时候传 —— 服务端默认就是开，传 "true" 是噪音。
                    **({} if auto_evidence else {"auto_evidence": "false"})},
            idempotent=False))

    def list_reviews(self, limit: int = 50) -> list[ReviewResult]:
        """列出历史审阅。

        列表只回摘要：``review_id`` / ``status`` 和评级字母，``grade.letter``
        之外的字段都是空的。标注、扣分项、风险总结要按 id 取
        :meth:`get_review` —— 别拿列表里的空 ``annotations`` 当成「没问题」。
        """
        payload = self._t.get("/v1/reviews", params={"limit": limit})
        return [ReviewResult.from_dict(r) for r in payload.get("reviews", [])]

    def get_review(self, review_id: str) -> ReviewResult:
        """取一次审阅的完整结果。"""
        return ReviewResult.from_dict(
            self._t.get(f"/v1/reviews/{_seg(review_id)}"))

    def download_annotated(self, review_id: str,
                           dest: Optional[str] = None) -> Union[bytes, str]:
        """下载标注版 PDF（高亮画在原文件上）。``dest`` 为 None 时返回字节。

        Raises:
            ConflictError: 源文档不是 PDF。只有 PDF 画得出标注版，其余格式请用
                ``annotations`` 里的字符区间自己渲染。
        """
        return self._t.download(f"/v1/reviews/{_seg(review_id)}/annotated", dest)

    def download_review_report(self, review_id: str,
                               dest: Optional[str] = None) -> Union[bytes, str]:
        """下载审阅报告（Markdown）。``dest`` 为 None 时返回字节。"""
        return self._t.download(f"/v1/reviews/{_seg(review_id)}/report", dest)

    def _with_timeout(self, timeout: float) -> HttpTransport:
        """按需给单次调用换一个超时的传输层。

        传输层的 timeout 是**连接配置**（默认 60s），而审阅是一次同步长请求 ——
        云端把整份文档解析、抽取、校验、评级跑完才回响应，大文档撑破 60s 是会
        发生的。撑破的后果不是重来一次那么简单：连接是客户端掐断的，云端那边
        照样跑完、照样计费，而调用方拿到的是一个看起来像网络故障的超时。

        做法是浅拷贝一份传输层只改它的 timeout，而不是就地改
        ``self._t.timeout``：异步客户端把同步调用丢进线程池，两个并发的
        review 会互相把对方的超时改掉，且改错了没有任何迹象。
        """
        seconds = float(timeout)
        if seconds <= 0:
            raise ConfigurationError(f"timeout 必须为正数，收到 {seconds}")
        if seconds == self._t.timeout:
            return self._t
        import copy
        clone = copy.copy(self._t)
        clone.timeout = seconds
        return clone


    # ---- 私有 Memory -----------------------------------------------------
    def create_memory(self, *, title: str, body: str, kind: str = "note",
                      tags: Optional[list[str]] = None) -> Memory:
        """新建一条私有 Memory（绑定你的租户，其他租户检索不到）。"""
        return Memory.from_dict(self._t.post("/v1/memories", json_body={
            "title": title, "body": body, "kind": kind, "tags": tags or []}))

    def list_memories(self, *, query: str = "", limit: int = 100) -> list[Memory]:
        """列出或检索私有 Memory。"""
        payload = self._t.get("/v1/memories",
                              params={"q": query, "limit": limit})
        return [Memory.from_dict(m) for m in payload.get("memories", [])]

    def get_memory(self, memory_id: str) -> Memory:
        return Memory.from_dict(self._t.get(f"/v1/memories/{_seg(memory_id)}"))

    def update_memory(self, memory_id: str, **changes: Any) -> Memory:
        """更新一条私有 Memory（title / body / kind / tags）。"""
        return Memory.from_dict(
            self._t.patch(f"/v1/memories/{_seg(memory_id)}", json_body=changes))

    def delete_memory(self, memory_id: str) -> dict[str, Any]:
        return self._t.delete(f"/v1/memories/{_seg(memory_id)}")

    # ---- 私有 Skill ------------------------------------------------------
    def create_skill(self, *, name: str, body: str, description: str = "",
                     tags: Optional[list[str]] = None,
                     tool_scope: Optional[list[str]] = None) -> Skill:
        """新建一个私有 Skill。

        Raises:
            ConflictError: 本项目下已有同名 Skill。
        """
        return Skill.from_dict(self._t.post("/v1/skills", json_body={
            "name": name, "body": body, "description": description,
            "tags": tags or [], "tool_scope": tool_scope or []}))

    def list_skills(self, *, query: str = "", limit: int = 100) -> list[Skill]:
        payload = self._t.get("/v1/skills", params={"q": query, "limit": limit})
        return [Skill.from_dict(s) for s in payload.get("skills", [])]

    def get_skill(self, skill_id: str) -> Skill:
        return Skill.from_dict(self._t.get(f"/v1/skills/{_seg(skill_id)}"))

    def update_skill(self, skill_id: str, **changes: Any) -> Skill:
        """更新一个私有 Skill（description / body / tags / tool_scope）。"""
        return Skill.from_dict(
            self._t.patch(f"/v1/skills/{_seg(skill_id)}", json_body=changes))

    def delete_skill(self, skill_id: str) -> dict[str, Any]:
        return self._t.delete(f"/v1/skills/{_seg(skill_id)}")

    def official_skills(self) -> list[dict[str, Any]]:
        """平台公共 Skill（所有租户只读共享）。"""
        return self._t.get("/v1/skills/official").get("skills", [])

    # ---- 私有 LLM Wiki ---------------------------------------------------
    def create_wiki_document(self, *, slug: str, title: str, body: str,
                             tags: Optional[list[str]] = None,
                             related_rules: Optional[list[str]] = None,
                             section: str = "pages") -> WikiDocument:
        """写入一页私有 LLM Wiki（slug 已存在时是更新，返回新版本）。"""
        return WikiDocument.from_dict(self._t.post("/v1/wiki", json_body={
            "slug": slug, "title": title, "body": body, "tags": tags or [],
            "related_rules": related_rules or [], "section": section}))

    def list_wiki_documents(self, *, query: str = "",
                            limit: int = 100) -> list[WikiDocument]:
        payload = self._t.get("/v1/wiki", params={"q": query, "limit": limit})
        return [WikiDocument.from_dict(d) for d in payload.get("documents", [])]

    def get_wiki_document(self, document_id: str) -> WikiDocument:
        """按 slug 或 page_id 取一页。"""
        return WikiDocument.from_dict(
            self._t.get(f"/v1/wiki/{_seg(document_id)}"))

    def update_wiki_document(self, document_id: str, **changes: Any) -> WikiDocument:
        return WikiDocument.from_dict(
            self._t.patch(f"/v1/wiki/{_seg(document_id)}", json_body=changes))

    def delete_wiki_document(self, document_id: str) -> dict[str, Any]:
        return self._t.delete(f"/v1/wiki/{_seg(document_id)}")

    def seed_reference_pages(self) -> list[str]:
        """生成两页「索引型」私有 Wiki，内容从**活的**注册表读出来。

        手写的工具清单和 Skill 目录一定会过期。这两页每次调用都重新生成，
        所以它们要么是准的，要么不存在 —— 不会出现「看起来准其实过期」。

        Returns:
            写入的页面 slug 列表。

        Note:
            这两页是**生成物**：正文注明 `自动生成`，手工改动会在下次生成时
            被覆盖。要写自己的说明，另开一页。
        """
        from sagax_amadeus.mcp_server import SagaxMCPServer

        written: list[str] = []
        server = SagaxMCPServer(self)
        rows = ["| 工具 | 用途 |", "|---|---|"]
        for spec, _ in sorted(server.tools.values(), key=lambda x: x[0]["name"]):
            desc = str(spec["description"]).replace("|", "\\|").replace("\n", " ")
            rows.append(f"| `{spec['name']}` | {desc} |")
        self.create_wiki_document(
            slug="sagax-mcp-tools", title="MCP 工具清单（自动生成）",
            body=("本机 Sagax MCP Server 当前暴露的工具。由 "
                  "`sagax-amadeus wiki seed` 从活的注册表生成。\n\n"
                  + "\n".join(rows) +
                  "\n\n每个工具都是一次 Sagax Amadeus 云端引擎 API 调用的薄封装；"
                  "MCP 不重复实现审计逻辑。"),
            tags=["mcp", "生成"], section="mcp")
        written.append("sagax-mcp-tools")

        skills = self.list_skills(limit=500)
        lines = ["| Skill | 描述 | tool_scope | scripts | references | 版本 |",
                 "|---|---|---|---|---|---|"]
        for s in skills:
            lines.append(
                f"| `{s.name}` | {s.description or '—'} | "
                f"{', '.join(f'`{t}`' for t in s.tool_scope) or '—'} | "
                f"{len(s.scripts)} | {len(s.references)} | v{s.version} |")
        self.create_wiki_document(
            slug="private-skill-index", title="私有 Skill 索引（自动生成）",
            body=(f"本作用域共 {len(skills)} 个私有 Skill。正文与附属文件的权威"
                  "副本在云端你的租户空间里（`SKILL.md` + `scripts/` + "
                  "`references/`）。\n\n"
                  + ("\n".join(lines) if skills else "_暂无私有 Skill。_")),
            tags=["skill", "索引", "生成"], section="skills")
        written.append("private-skill-index")
        return written

    def public_wiki(self, query: str = "", slug: str = "",
                    limit: int = 5) -> list[dict[str, Any]]:
        """平台公共 LLM Wiki。"""
        return self._t.get("/v1/wiki/public", params={
            "q": query, "slug": slug, "limit": limit}).get("pages", [])

    # ---- 规则 ------------------------------------------------------------
    def create_boundary(self, boundary: dict[str, Any]) -> dict[str, Any]:
        """新增一条私有规则（P0 平台规则不允许客户创建）。"""
        return self._t.post("/v1/boundaries", json_body=boundary)

    def list_boundaries(self) -> list[dict[str, Any]]:
        """本租户生效的私有规则。"""
        return self._t.get("/v1/boundaries").get("boundaries", [])

    def delete_boundary(self, rule_id: str) -> dict[str, Any]:
        """停用一条私有规则。"""
        return self._t.delete(f"/v1/boundaries/{_seg(rule_id)}")

    def public_boundaries(self, *, task: str = "",
                          fields: Iterable[str] = ()) -> list[dict[str, Any]]:
        """平台公共规则。"""
        return self._t.get("/v1/boundaries/public", params={
            "task": task, "fields": ",".join(fields)}).get("boundaries", [])

    def candidates(self) -> list[dict[str, Any]]:
        """持续学习产出的候选规则（永远不会自动生效）。"""
        return self._t.get("/v1/boundaries/candidates").get("candidates", [])

    def approve_candidate(self, candidate_id: str,
                          approved_by: str) -> dict[str, Any]:
        """审批候选规则，使其成为生效的私有规则。"""
        return self._t.post(f"/v1/boundaries/candidates/{_seg(candidate_id)}",
                            json_body={"action": "approve",
                                       "approved_by": approved_by},
                            idempotent=False)

    def contribute_candidate(self, candidate_id: str, *,
                             confirmed: bool = False) -> dict[str, Any]:
        """把一条候选规则脱敏后贡献给平台公共库。

        Args:
            candidate_id: 候选 id。
            confirmed: **必须显式传 True**。客户私有资源不会自动变成公共资源；
                提交后也只是 ``pending_review``，要人工审核才可能进公共库。
        """
        return self._t.post(f"/v1/boundaries/candidates/{_seg(candidate_id)}",
                            json_body={"action": "contribute",
                                       "confirmed": bool(confirmed)},
                            idempotent=False)

    # ---- 兼容旧方法名 ----------------------------------------------------
    # 改造前这些方法在本地跑；现在它们是同名的远程调用，语义不变。保留是为了让
    # 已经写好的集成不用为了架构改动改一遍 —— 但它们不会「远程失败就转本地」。
    def add_memory(self, title: str, body: str, *, kind: str = "note",
                   tags: Optional[list[str]] = None) -> Memory:
        """:meth:`create_memory` 的旧名字。"""
        return self.create_memory(title=title, body=body, kind=kind, tags=tags)

    def search_memory(self, query: str, limit: int = 5) -> list[Memory]:
        """:meth:`list_memories` 的旧名字。"""
        return self.list_memories(query=query, limit=limit)

    def add_skill(self, name: str, body: str, *, description: str = "",
                  tags: Optional[list[str]] = None,
                  tool_scope: Optional[list[str]] = None) -> Skill:
        """:meth:`create_skill` 的旧名字。"""
        return self.create_skill(name=name, body=body, description=description,
                                 tags=tags, tool_scope=tool_scope)

    def search_skills(self, query: str, limit: int = 5) -> list[Skill]:
        """:meth:`list_skills` 的旧名字。"""
        return self.list_skills(query=query, limit=limit)

    def write_wiki(self, slug: str, title: str, body: str, *,
                   tags: Optional[list[str]] = None,
                   related_rules: Optional[list[str]] = None,
                   section: str = "pages") -> WikiDocument:
        """:meth:`create_wiki_document` 的旧名字。"""
        return self.create_wiki_document(
            slug=slug, title=title, body=body, tags=tags,
            related_rules=related_rules, section=section)

    def search_wiki(self, query: str, limit: int = 5) -> list[WikiDocument]:
        """:meth:`list_wiki_documents` 的旧名字。"""
        return self.list_wiki_documents(query=query, limit=limit)

    def add_boundary(self, boundary: Any) -> dict[str, Any]:
        """:meth:`create_boundary` 的旧名字。接受 dict 或带 ``model_dump`` 的对象。"""
        payload = (boundary.model_dump(mode="json")
                   if hasattr(boundary, "model_dump") else dict(boundary))
        return self.create_boundary(payload)

    def private_boundaries(self) -> list[dict[str, Any]]:
        """:meth:`list_boundaries` 的旧名字。"""
        return self.list_boundaries()

    def status(self) -> dict[str, Any]:
        """连接与订阅状态。

        改造前这个方法报的是本地私有存储的路径和容量。现在私有数据在云端，
        本地没有路径可报 —— 它改报「连的是谁、用的哪个租户、配额还剩多少」。
        """
        usage = self.usage()
        version = self.version()
        return {
            "base_url": self.base_url,
            "project_id": self.project_id,
            "api_key": mask_key(self._t.api_key),
            "tenant_id": usage.tenant_id,
            "plan": usage.plan,
            "audits_used": usage.audits_used,
            "quota_monthly": usage.quota_monthly,
            "remaining": usage.remaining,
            "api_version": version.api_version,
            "audit_engine_version": version.audit_engine_version,
            "public_rule_version": version.public_rule_version,
        }

    # ---- 已移除的本地能力 ------------------------------------------------
    def register_model(self, *_args: Any, **_kwargs: Any) -> None:
        """已移除：模型现在由云端调用。

        Raises:
            ConfigurationError: 总是。
        """
        raise ConfigurationError(
            "register_model 已移除：审阅运行时在云端，模型由云端调用。\n"
            "  · 写好的文档想过一遍审阅 → client.review(\"报告.md\", "
            "evidence=[…])\n"
            "迁移说明见 docs/MIGRATION-thin-sdk.md")

    def initialize(self) -> dict[str, Any]:
        """已移除：不再有本地私有存储要初始化。

        Raises:
            ConfigurationError: 总是。
        """
        raise ConfigurationError(
            "initialize 已移除：私有 Memory / Skill / Wiki 现在存在云端，"
            "第一次调 create_memory / create_skill / create_wiki_document "
            "时自动就绪。查看连接状态用 client.status()。")

    def backup(self, *_args: Any, **_kwargs: Any) -> str:
        """已移除：数据在云端，备份是平台的责任。

        Raises:
            ConfigurationError: 总是。
        """
        raise ConfigurationError(
            "backup 已移除：私有数据在云端，备份由平台负责。"
            "要把某次审阅留档，用 client.download_review_report(review_id, dest) "
            "取报告，或 client.download_annotated(review_id, dest) 取标注版 PDF。")


#: 改造前的类名。同一个类对象，不是第二套实现 —— 已经写死
#: ``from sagax_amadeus import AuditClient`` 的集成不必跟着改。
AuditClient = SagaxAuditClient


def _seg(value: Any) -> str:
    """路径片段转义。id 是客户传进来的字符串，不转义就会变成路径穿越。"""
    from urllib.parse import quote
    text = str(value or "").strip()
    if not text:
        raise ConfigurationError("资源 id 不能为空")
    return quote(text, safe="")


def _guess_type(filename: str) -> str:
    import mimetypes
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


def _document_bytes(document: Union[str, bytes],
                    filename: str) -> tuple[bytes, str]:
    """待审文档 → ``(字节, 文件名)``。

    路径和字节都收：宿主智能体手上是一份刚写完的文件，而流水线里的文档是内存
    里的字节，两边都不该被逼着为了调一次 review 去读盘或者落一个临时文件。

    文件名从路径推断出来是有用的 —— 云端**按扩展名判定格式**。丢了文件名，
    一份 PDF 会被当成纯文本解析，产出一整页位置错的高亮。
    """
    if isinstance(document, bytes):
        return document, filename
    if isinstance(document, str):
        if not os.path.isfile(document):
            raise ConfigurationError(f"文件不存在: {document}")
        with open(document, "rb") as fh:
            return fh.read(), filename or os.path.basename(document)
    raise ConfigurationError(
        "review 的 document 只接受文件路径（str）或文档字节（bytes），"
        f"收到 {type(document).__name__}")


__all__ = ["SagaxAuditClient", "AuditClient", "DEFAULT_DEV_BASE_URL"]
