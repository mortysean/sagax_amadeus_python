"""Sagax Audit SDK — 异步客户端。

    from sagax_amadeus import AsyncSagaxAuditClient

    async with AsyncSagaxAuditClient(base_url="https://audit.example.com",
                                     api_key="sagax_sk_…") as client:
        audit = await client.create_audit(task="…", evidence_ids=[…])
        result = await client.wait_for_audit(audit.audit_id)

实现方式：把同步客户端的调用丢进线程（``asyncio.to_thread``）。

为什么不写一个真正的 async HTTP 栈：那需要 ``aiohttp`` / ``httpx``，而这个包
的卖点之一是**零依赖**。审计请求是低频、长耗时的 I/O（创建、轮询、下载），
一次调用占一个线程几十毫秒到几秒，线程池完全够用；换成原生 async 省下的是
连接数，而不是延迟。真到了需要几千并发审计的规模，那时的瓶颈是云端配额，
不是客户端的事件循环。

事件循环不会被阻塞 —— 这是 ``to_thread`` 的全部意义，也是这一层存在的理由：
在 async 代码里直接调同步客户端会把整个循环卡住。
"""
from __future__ import annotations

import asyncio
from typing import Any, BinaryIO, Iterable, Optional, Union

from sagax_amadeus.client import SagaxAuditClient
from sagax_amadeus.exceptions import AuditTimeoutError
from sagax_amadeus.models import (Audit, AuditResult, CandidateOutput, Evidence,
                                EvidenceItem, Memory, ServiceVersion, Skill,
                                TraceEvent, Usage, WikiDocument)
from sagax_amadeus.transport import (DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT,
                                   HttpTransport)


class AsyncSagaxAuditClient:
    """:class:`~sagax_amadeus.client.SagaxAuditClient` 的异步外壳。"""

    def __init__(self, *, base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 project_id: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 allow_insecure_http: bool = False,
                 ca_bundle: Optional[str] = None,
                 transport: Optional[HttpTransport] = None) -> None:
        """参数与同步客户端完全一致，见
        :meth:`sagax_amadeus.client.SagaxAuditClient.__init__`。"""
        self._sync = SagaxAuditClient(
            base_url=base_url, api_key=api_key, project_id=project_id,
            timeout=timeout, max_retries=max_retries,
            allow_insecure_http=allow_insecure_http, ca_bundle=ca_bundle,
            transport=transport)

    def __repr__(self) -> str:
        return f"Async{self._sync!r}"

    @property
    def base_url(self) -> str:
        return self._sync.base_url

    @property
    def project_id(self) -> str:
        return self._sync.project_id

    @property
    def sync_client(self) -> SagaxAuditClient:
        """底层同步客户端（需要它的兼容方法时用）。"""
        return self._sync

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(getattr(self._sync, name), *args, **kwargs)

    async def close(self) -> None:
        await asyncio.to_thread(self._sync.close)

    async def __aenter__(self) -> "AsyncSagaxAuditClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ---- 服务状态 --------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        return await self._call("health")

    async def version(self) -> ServiceVersion:
        return await self._call("version")

    async def usage(self) -> Usage:
        return await self._call("usage")

    async def status(self) -> dict[str, Any]:
        return await self._call("status")

    # ---- Evidence --------------------------------------------------------
    async def upload_evidence(self, *, path: Optional[str] = None,
                              data: Union[bytes, BinaryIO, None] = None,
                              filename: str = "", content_type: str = "",
                              metadata: Optional[dict[str, Any]] = None
                              ) -> Evidence:
        return await self._call("upload_evidence", path=path, data=data,
                                filename=filename, content_type=content_type,
                                metadata=metadata)

    async def upload_evidence_items(self, items: Iterable[EvidenceItem], *,
                                    filename: str = "evidence.json",
                                    metadata: Optional[dict[str, Any]] = None
                                    ) -> Evidence:
        return await self._call("upload_evidence_items", list(items),
                                filename=filename, metadata=metadata)

    async def list_evidence(self, limit: int = 100) -> list[Evidence]:
        return await self._call("list_evidence", limit)

    async def get_evidence(self, evidence_id: str) -> Evidence:
        return await self._call("get_evidence", evidence_id)

    async def download_evidence(self, evidence_id: str,
                                dest: Optional[str] = None) -> Union[bytes, str]:
        return await self._call("download_evidence", evidence_id, dest)

    async def delete_evidence(self, evidence_id: str) -> dict[str, Any]:
        return await self._call("delete_evidence", evidence_id)

    # ---- 审计任务 --------------------------------------------------------
    async def create_audit(self, **kwargs: Any) -> Audit:
        return await self._call("create_audit", **kwargs)

    async def list_audits(self, limit: int = 50) -> list[Audit]:
        return await self._call("list_audits", limit)

    async def get_audit(self, audit_id: str) -> Audit:
        return await self._call("get_audit", audit_id)

    async def get_audit_status(self, audit_id: str) -> dict[str, Any]:
        return await self._call("get_audit_status", audit_id)

    async def get_audit_result(self, audit_id: str) -> AuditResult:
        return await self._call("get_audit_result", audit_id)

    async def wait_for_audit(self, audit_id: str, *, timeout: float = 300.0,
                             poll_interval: float = 1.0,
                             raise_on_failure: bool = True) -> AuditResult:
        """轮询直到审计终结。

        轮询在事件循环里做（``asyncio.sleep``），只有单次 HTTP 请求走线程 ——
        把整段等待丢进一个线程会白占一个线程好几分钟。

        Raises:
            AuditTimeoutError: 超时；任务仍在云端继续跑。
            AuditFailedError: 任务失败或被取消。
        """
        import time

        from sagax_amadeus.exceptions import AuditFailedError
        deadline = time.monotonic() + float(timeout)
        interval = max(0.01, float(poll_interval))
        while True:
            last = await self.get_audit_status(audit_id)
            status = str(last.get("status") or "")
            if status == "completed":
                return await self.get_audit_result(audit_id)
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
            await asyncio.sleep(min(interval, remaining))
            interval = min(interval * 1.5, 5.0)

    async def audit(self, *, timeout: float = 300.0, **kwargs: Any) -> AuditResult:
        """创建审计并等它跑完。"""
        job = await self.create_audit(**kwargs)
        return await self.wait_for_audit(job.audit_id, timeout=timeout)

    async def check(self, *, task: str,
                    candidate_output: Union[CandidateOutput, dict],
                    evidence: Iterable[Union[EvidenceItem, dict]] = (),
                    evidence_ids: Iterable[str] = (),
                    required_fields: Iterable[str] = (),
                    timeout: float = 300.0) -> AuditResult:
        """只审计一份已有输出，不进入自动修复循环。"""
        return await self.audit(
            task=task, candidate_output=candidate_output, evidence=evidence,
            evidence_ids=evidence_ids, required_fields=required_fields,
            auto_repair=False, timeout=timeout)

    async def cancel_audit(self, audit_id: str) -> dict[str, Any]:
        return await self._call("cancel_audit", audit_id)

    # ---- 产物 ------------------------------------------------------------
    async def download_report(self, audit_id: str,
                              dest: Optional[str] = None) -> Union[bytes, str]:
        return await self._call("download_report", audit_id, dest)

    async def download_bundle(self, audit_id: str,
                              dest: Optional[str] = None) -> Union[bytes, str]:
        return await self._call("download_bundle", audit_id, dest)

    async def get_trace(self, audit_id: str) -> list[TraceEvent]:
        return await self._call("get_trace", audit_id)

    async def download_trace(self, audit_id: str, dest: str) -> str:
        return await self._call("download_trace", audit_id, dest)

    # ---- 私有 Memory / Skill / Wiki --------------------------------------
    async def create_memory(self, **kwargs: Any) -> Memory:
        return await self._call("create_memory", **kwargs)

    async def list_memories(self, **kwargs: Any) -> list[Memory]:
        return await self._call("list_memories", **kwargs)

    async def get_memory(self, memory_id: str) -> Memory:
        return await self._call("get_memory", memory_id)

    async def update_memory(self, memory_id: str, **changes: Any) -> Memory:
        return await self._call("update_memory", memory_id, **changes)

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        return await self._call("delete_memory", memory_id)

    async def create_skill(self, **kwargs: Any) -> Skill:
        return await self._call("create_skill", **kwargs)

    async def list_skills(self, **kwargs: Any) -> list[Skill]:
        return await self._call("list_skills", **kwargs)

    async def get_skill(self, skill_id: str) -> Skill:
        return await self._call("get_skill", skill_id)

    async def update_skill(self, skill_id: str, **changes: Any) -> Skill:
        return await self._call("update_skill", skill_id, **changes)

    async def delete_skill(self, skill_id: str) -> dict[str, Any]:
        return await self._call("delete_skill", skill_id)

    async def create_wiki_document(self, **kwargs: Any) -> WikiDocument:
        return await self._call("create_wiki_document", **kwargs)

    async def list_wiki_documents(self, **kwargs: Any) -> list[WikiDocument]:
        return await self._call("list_wiki_documents", **kwargs)

    async def get_wiki_document(self, document_id: str) -> WikiDocument:
        return await self._call("get_wiki_document", document_id)

    async def update_wiki_document(self, document_id: str,
                                   **changes: Any) -> WikiDocument:
        return await self._call("update_wiki_document", document_id, **changes)

    async def delete_wiki_document(self, document_id: str) -> dict[str, Any]:
        return await self._call("delete_wiki_document", document_id)

    # ---- 规则 ------------------------------------------------------------
    async def create_boundary(self, boundary: dict[str, Any]) -> dict[str, Any]:
        return await self._call("create_boundary", boundary)

    async def list_boundaries(self) -> list[dict[str, Any]]:
        return await self._call("list_boundaries")

    async def delete_boundary(self, rule_id: str) -> dict[str, Any]:
        return await self._call("delete_boundary", rule_id)

    async def public_boundaries(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._call("public_boundaries", **kwargs)

    async def candidates(self) -> list[dict[str, Any]]:
        return await self._call("candidates")

    async def approve_candidate(self, candidate_id: str,
                                approved_by: str) -> dict[str, Any]:
        return await self._call("approve_candidate", candidate_id, approved_by)

    async def contribute_candidate(self, candidate_id: str, *,
                                   confirmed: bool = False) -> dict[str, Any]:
        return await self._call("contribute_candidate", candidate_id,
                                confirmed=confirmed)

    async def official_skills(self) -> list[dict[str, Any]]:
        return await self._call("official_skills")


#: 与同步侧对称的短名。
AsyncAuditClient = AsyncSagaxAuditClient

__all__ = ["AsyncSagaxAuditClient", "AsyncAuditClient"]
