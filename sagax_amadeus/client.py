"""Sagax Audit SDK — 同步客户端。

    from sagax_amadeus import SagaxAuditClient

    client = SagaxAuditClient(base_url="https://audit.example.com",
                              api_key="sagax_sk_…")
    evidence = client.upload_evidence(path="fy2025.json")
    audit = client.create_audit(task="核对 2025 年报关键指标",
                                evidence_ids=[evidence.evidence_id],
                                required_fields=["revenue", "net_profit"])
    result = client.wait_for_audit(audit.audit_id)
    client.download_report(audit.audit_id, "report.md")

这个类是**纯 HTTP 客户端**。它不跑校验器、不跑修复循环、不生成报告、不生成
Audit Bundle、不在本地存私有知识库。所有审计逻辑在 Sagax Audit Cloud 上执行，
SDK 只负责把请求发过去、把结果取回来。

远程失败时**不会**回落到本地审计 —— 没有本地审计可回落，这是有意的：
两套引擎会产生两套结论，而审计结论的价值全部来自「只有一套」。
"""
from __future__ import annotations

import os
import time
from typing import Any, BinaryIO, Iterable, Optional, Union

from sagax_amadeus.exceptions import (AuditFailedError, AuditTimeoutError,
                                    ConfigurationError)
from sagax_amadeus.models import (Audit, AuditProfile, AuditResult, CandidateOutput,
                                Evidence, EvidenceItem, Memory, ServiceVersion,
                                Skill, TraceEvent, Usage, WikiDocument)
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
    """Sagax Audit 公网 API 的同步客户端。"""

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
            timeout: 单次 HTTP 请求超时（秒）。**不是**审计完成的等待时间 ——
                那个在 :meth:`wait_for_audit` 上。
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

        云端会把它们解析成可参与数值校验的条目，之后创建审计时用
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

    # ---- 审计任务 --------------------------------------------------------
    def create_audit(self, *, task: str, prompt: str = "",
                     candidate_output: Union[CandidateOutput, dict, None] = None,
                     evidence: Iterable[Union[EvidenceItem, dict]] = (),
                     evidence_ids: Iterable[str] = (),
                     required_fields: Iterable[str] = (),
                     boundary_overrides: Iterable[str] = (),
                     profile: Union[AuditProfile, dict, None] = None,
                     model: Optional[str] = None,
                     auto_repair: bool = True,
                     metadata: Optional[dict[str, Any]] = None) -> Audit:
        """创建一个审计任务，立刻返回（不等它跑完）。

        Args:
            task: 任务描述。
            prompt: 交给模型的原始 prompt（用于判断任务与输出是否匹配）。
            candidate_output: 待审的结构化输出。你的 Agent 自己生成好结论、
                只想过一遍审计时给它。
            evidence: 内联的结构化证据。
            evidence_ids: 已上传 Evidence 的 id。
            required_fields: 本次任务要求必须给出的字段。
            boundary_overrides: 本次停用的规则 id（P0 平台规则停不掉）。
            profile: 审计档位。
            model: 云端模型适配器名（走定向重生成时用）。
            auto_repair: 是否允许云端自动定向重生成。False = 只审不修，
                返回 verdict + RepairPlan，由你自己去改。
            metadata: 附加元数据，原样带进结果。

        Returns:
            :class:`~sagax_amadeus.models.Audit`，状态为 ``queued``。

        Raises:
            ValidationError: 请求体不合法（如 task 为空）。
            QuotaExceededError: 配额用尽。
        """
        body: dict[str, Any] = {
            "task": task, "prompt": prompt,
            "evidence": [e.to_dict() if isinstance(e, EvidenceItem) else e
                         for e in evidence],
            "evidence_ids": list(evidence_ids),
            "required_fields": list(required_fields),
            "boundary_overrides": list(boundary_overrides),
            "auto_repair": bool(auto_repair),
            "metadata": metadata or {},
        }
        if candidate_output is not None:
            body["candidate_output"] = (
                candidate_output.to_dict()
                if isinstance(candidate_output, CandidateOutput)
                else candidate_output)
        if profile is not None:
            body["profile"] = (profile.to_dict()
                               if isinstance(profile, AuditProfile) else profile)
        if model:
            body["model"] = model
        # idempotent=False 是重点：重发一次 = 多跑一次审计 + 多扣一次配额。
        return Audit.from_dict(
            self._t.post("/v1/audits", json_body=body, idempotent=False))

    def list_audits(self, limit: int = 50) -> list[Audit]:
        """列出历史审计任务。"""
        payload = self._t.get("/v1/audits", params={"limit": limit})
        return [Audit.from_dict(a) for a in payload.get("audits", [])]

    def get_audit(self, audit_id: str) -> Audit:
        """取任务详情。"""
        return Audit.from_dict(self._t.get(f"/v1/audits/{_seg(audit_id)}"))

    def get_audit_status(self, audit_id: str) -> dict[str, Any]:
        """轻量状态查询（轮询用的就是它）。"""
        return self._t.get(f"/v1/audits/{_seg(audit_id)}/status")

    def get_audit_result(self, audit_id: str) -> AuditResult:
        """取完整审计结果。

        Raises:
            ConflictError: 任务还没跑完 —— 服务端不会返回一个「暂时为空」的
                结果让你误以为审计通过了。
        """
        return AuditResult.from_dict(
            self._t.get(f"/v1/audits/{_seg(audit_id)}/result"))

    def wait_for_audit(self, audit_id: str, *, timeout: float = 300.0,
                       poll_interval: float = 1.0,
                       raise_on_failure: bool = True) -> AuditResult:
        """轮询直到审计终结，返回结果。

        Args:
            audit_id: 任务 id。
            timeout: 最长等待秒数。
            poll_interval: 轮询间隔；会按指数退避到 5 秒封顶，免得一个跑十分钟
                的审计被打上几百次无用请求。
            raise_on_failure: 任务以 failed / cancelled 结束时是否抛异常。

        Returns:
            :class:`~sagax_amadeus.models.AuditResult`。

        Raises:
            AuditTimeoutError: 超时。**任务仍在云端继续跑** —— 拿着 audit_id
                以后还能查，或者显式 :meth:`cancel_audit`。
            AuditFailedError: 任务失败或被取消（``raise_on_failure=True`` 时）。
        """
        deadline = time.monotonic() + float(timeout)
        interval = max(0.01, float(poll_interval))
        last = {"status": "unknown"}
        while True:
            last = self.get_audit_status(audit_id)
            status = str(last.get("status") or "")
            if status == "completed":
                return self.get_audit_result(audit_id)
            if status in ("failed", "cancelled"):
                if not raise_on_failure:
                    return AuditResult(audit_id=audit_id, status=status)
                raise AuditFailedError(
                    last.get("error_message") or f"审计以 {status} 结束",
                    audit_id=audit_id, status=status,
                    code=str(last.get("error_code") or ""))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuditTimeoutError(
                    f"等待审计 {audit_id} 超过 {timeout}s；任务仍在云端执行，"
                    "可以稍后用同一个 audit_id 继续查",
                    audit_id=audit_id, last_status=status)
            time.sleep(min(interval, remaining))
            interval = min(interval * 1.5, 5.0)

    def audit(self, *, task: str, prompt: str = "",
              candidate_output: Union[CandidateOutput, dict, None] = None,
              evidence: Iterable[Union[EvidenceItem, dict]] = (),
              evidence_ids: Iterable[str] = (),
              required_fields: Iterable[str] = (),
              boundary_overrides: Iterable[str] = (),
              profile: Union[AuditProfile, dict, None] = None,
              model: Optional[str] = None,
              auto_repair: bool = True,
              metadata: Optional[dict[str, Any]] = None,
              timeout: float = 300.0) -> AuditResult:
        """创建审计并等它跑完 —— 一步到位的常用形式。

        等价于 :meth:`create_audit` + :meth:`wait_for_audit`。要在等待期间做别的
        事（或者审计很慢），用那两个方法自己控制节奏。
        """
        job = self.create_audit(
            task=task, prompt=prompt, candidate_output=candidate_output,
            evidence=evidence, evidence_ids=evidence_ids,
            required_fields=required_fields,
            boundary_overrides=boundary_overrides, profile=profile,
            model=model, auto_repair=auto_repair, metadata=metadata)
        return self.wait_for_audit(job.audit_id, timeout=timeout)

    def check(self, *, task: str,
              candidate_output: Union[CandidateOutput, dict],
              evidence: Iterable[Union[EvidenceItem, dict]] = (),
              evidence_ids: Iterable[str] = (),
              required_fields: Iterable[str] = (),
              timeout: float = 300.0) -> AuditResult:
        """只审计一份已有输出，不进入自动修复循环。

        Agent 自己生成好结论、只想过一遍审计时用这个 —— 返回的 ``verdict`` 加
        ``repair_plan()`` 就是它该怎么改。
        """
        return self.audit(task=task, candidate_output=candidate_output,
                          evidence=evidence, evidence_ids=evidence_ids,
                          required_fields=required_fields, auto_repair=False,
                          timeout=timeout)

    def cancel_audit(self, audit_id: str) -> dict[str, Any]:
        """取消一个进行中的审计；已终结的则连同产物一起删除。"""
        return self._t.delete(f"/v1/audits/{_seg(audit_id)}")

    # ---- 产物 ------------------------------------------------------------
    def download_report(self, audit_id: str,
                        dest: Optional[str] = None) -> Union[bytes, str]:
        """下载云端生成的审计报告（Markdown）。"""
        return self._t.download(f"/v1/audits/{_seg(audit_id)}/report", dest)

    def download_bundle(self, audit_id: str,
                        dest: Optional[str] = None) -> Union[bytes, str]:
        """下载云端生成的 Audit Bundle（tar.gz）。"""
        return self._t.download(f"/v1/audits/{_seg(audit_id)}/bundle", dest)

    def get_trace(self, audit_id: str) -> list[TraceEvent]:
        """取一次审计的结构化 Trace。

        Trace 里没有模型隐藏思维链 —— 服务端的事件类型里就没有这个槽。
        """
        payload = self._t.get(f"/v1/audits/{_seg(audit_id)}/trace")
        return [TraceEvent.from_dict(e) for e in payload.get("events", [])]

    def download_trace(self, audit_id: str, dest: str) -> str:
        """把 Trace 写成 JSONL 文件。"""
        import json
        events = self.get_trace(audit_id)
        parent = os.path.dirname(os.path.abspath(dest))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return dest

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
                  "\n\n每个工具都是一次 Sagax Audit Cloud API 调用的薄封装；"
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

    def runs(self, limit: int = 20) -> list[Audit]:
        """:meth:`list_audits` 的旧名字。"""
        return self.list_audits(limit)

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
            "register_model 已移除：审计运行时在云端，模型由云端调用。\n"
            "  · 自己生成好结论、只想过审计 → client.check(task=…, "
            "candidate_output=…)\n"
            "  · 想让云端替你定向重生成 → 由部署方在云端注册模型适配器\n"
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
            "要把某次审计的完整证据链留档，用 "
            "client.download_bundle(audit_id, dest)。")


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


__all__ = ["SagaxAuditClient", "AuditClient", "DEFAULT_DEV_BASE_URL"]
