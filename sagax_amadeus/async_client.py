"""Sagax Amadeus SDK — 异步客户端。

    from sagax_amadeus import AsyncSagaxAuditClient

    async with AsyncSagaxAuditClient(base_url="https://audit.example.com",
                                     api_key="sagax_sk_…") as client:
        result = await client.review("研报.md", evidence=[…])
        print(result.grade.letter, result.blocking)

实现方式：把同步客户端的调用丢进线程（``asyncio.to_thread``）。

为什么不写一个真正的 async HTTP 栈：那需要 ``aiohttp`` / ``httpx``，而这个包
的卖点之一是**零依赖**。审阅请求是低频、长耗时的 I/O（上传、审阅、下载），
一次调用占一个线程几秒到几分钟，线程池完全够用；换成原生 async 省下的是
连接数，而不是延迟。真到了需要几千并发审阅的规模，那时的瓶颈是云端配额，
不是客户端的事件循环。

事件循环不会被阻塞 —— 这是 ``to_thread`` 的全部意义，也是这一层存在的理由：
在 async 代码里直接调同步客户端会把整个循环卡住。
"""
from __future__ import annotations

import asyncio
from typing import Any, BinaryIO, Iterable, Optional, Union

from sagax_amadeus.client import SagaxAuditClient
from sagax_amadeus.models import (Evidence, EvidenceItem, Memory, ReviewResult,
                                ServiceVersion, Skill, Usage, WikiDocument)
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

    # ---- 文档审阅 --------------------------------------------------------
    async def review(self, document: Union[str, bytes], *, filename: str = "",
                     content_type: str = "",
                     evidence: Iterable[Union[EvidenceItem, dict]] = (),
                     evidence_ids: Iterable[str] = (),
                     code: str = "", auto_evidence: bool = True,
                     timeout: float = 300.0) -> ReviewResult:
        """审一份文档。参数见
        :meth:`sagax_amadeus.client.SagaxAuditClient.review`。

        一次调用只占一个线程 —— 云端跑完整个审阅才回响应，中间没有轮询。
        """
        return await self._call("review", document, filename=filename,
                                content_type=content_type,
                                evidence=list(evidence),
                                evidence_ids=list(evidence_ids),
                                code=code, auto_evidence=auto_evidence,
                                timeout=timeout)

    async def list_reviews(self, limit: int = 50) -> list[ReviewResult]:
        return await self._call("list_reviews", limit)

    async def get_review(self, review_id: str) -> ReviewResult:
        return await self._call("get_review", review_id)

    # ---- 产物 ------------------------------------------------------------
    async def download_annotated(self, review_id: str,
                                 dest: Optional[str] = None
                                 ) -> Union[bytes, str]:
        return await self._call("download_annotated", review_id, dest)

    async def download_review_report(self, review_id: str,
                                     dest: Optional[str] = None
                                     ) -> Union[bytes, str]:
        return await self._call("download_review_report", review_id, dest)

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
