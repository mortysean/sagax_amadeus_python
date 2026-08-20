"""Sagax Amadeus SDK — Sagax Audit 公网 API 的 Python 客户端。

    pip install sagax-amadeus

    from sagax_amadeus import SagaxAuditClient

    client = SagaxAuditClient(base_url="https://audit.example.com",
                              api_key="sagax_sk_…")
    result = client.audit(task="核对 2025 年报关键指标",
                          candidate_output=my_output,
                          required_fields=["revenue", "net_profit"])
    print(result.status, [f.field for f in result.findings()])

**这是一个薄 HTTP 客户端。** 它不在本地执行审计：没有校验器、没有修复循环、
不生成报告、不生成 Audit Bundle、不存私有知识库。这些全部在 Sagax Audit Cloud
上执行，SDK 负责把请求发过去、把结果取回来。

数据去向要说清楚：你上传的原始 Evidence、私有 Memory、私有 Skill、私有
LLM Wiki 都会经 HTTPS **上传到云端**，按租户隔离存放。它们不会被其他租户检索
到，默认也不用于模型训练。需要数据完全不出机房的部署形态，联系我们谈私有化 ——
那是另一套交付，不是这个包的默认行为。

模块地图::

    client.py        SagaxAuditClient（同步）
    async_client.py  AsyncSagaxAuditClient（异步）
    transport.py     HTTP 传输：鉴权、超时、重试、错误映射
    models.py        公开 API 数据结构（标准库 dataclass，零依赖）
    exceptions.py    异常类型
    cli.py           sagax-amadeus 命令行
    mcp_server.py    MCP Server（把云端能力暴露给 Agent）

这个包**没有任何运行时依赖**，也不依赖 ``sagax_audit_cloud``（云端服务包）。

发行名与导入名在 2.1.0 从 ``sagax-audit`` / ``sagax_audit`` 改成
3.0.0 是第一个公开发布的版本，Apache-2.0。它不带任何改名兼容层：只有
``sagax_amadeus`` 一个导入名、``sagax-amadeus`` 一个命令。

环境变量 ``SAGAX_AUDIT_*`` 与 MCP 工具名 ``sagax.*`` 沿用产品原名，和包名不一致
是**有意的** —— 它们是线上契约，客户配置与服务端认证里写死的就是它们。
"""

import importlib.metadata as _metadata

from sagax_amadeus.async_client import AsyncAuditClient, AsyncSagaxAuditClient
from sagax_amadeus.client import AuditClient, SagaxAuditClient
from sagax_amadeus.exceptions import (APIConnectionError, APIStatusError,
                                    APITimeoutError, AuditFailedError,
                                    AuditTimeoutError, AuthenticationError,
                                    ConfigurationError, ConflictError,
                                    InsecureTransportError, NotFoundError,
                                    PayloadTooLargeError, PermissionDeniedError,
                                    QuotaExceededError, RateLimitError,
                                    SagaxAuditError, ServerError,
                                    ValidationError)
from sagax_amadeus.models import (Audit, AuditFinding, AuditProfile, AuditResult,
                                AuditStatus, AuditVerdict, CandidateOutput,
                                Evidence, EvidenceItem, Memory, OutputField,
                                RepairPlan, ServiceVersion, Severity, Skill,
                                TraceEvent, Usage, VerdictStatus, Visibility,
                                VisibilityMeta, WikiDocument)
from sagax_amadeus.transport import HttpTransport, mask_key

#: 版本号的唯一真相是 ``pyproject.toml``，这里从已安装的包元数据读回来。
#:
#: 写成字面量会和 pyproject 各自漂移，而且漂移了没人会发现：3.0.0 就是这么
#: 发出去的 —— pyproject 写 3.0.0、这里还留着 2.1.0，于是 ``--version`` 和
#: MCP 握手都报了错的版本，直到从 PyPI 装回来才看出来。发行包撤不回，
#: 只能再发一版；所以这里改成派生，让这类错误在结构上不可能发生。
try:
    __version__ = _metadata.version("sagax-amadeus")
except _metadata.PackageNotFoundError:   # 直接从源码树 import，没走安装
    __version__ = "0.0.0.dev0+source"

__product__ = "Sagax Amadeus SDK"
__product_zh__ = "Sagax 审计智能体 SDK"

#: 这个 SDK 说的 API 版本。与云端 ``GET /v1/version`` 的 ``api_version`` 比对。
API_VERSION = "v1"

__all__ = [
    "__version__", "__product__", "__product_zh__", "API_VERSION",
    # 客户端
    "SagaxAuditClient", "AuditClient",
    "AsyncSagaxAuditClient", "AsyncAuditClient",
    "HttpTransport", "mask_key",
    # 数据结构
    "Audit", "AuditStatus", "AuditResult", "AuditVerdict", "AuditFinding",
    "RepairPlan", "AuditProfile", "CandidateOutput", "OutputField",
    "Evidence", "EvidenceItem", "Memory", "Skill", "WikiDocument",
    "TraceEvent", "Usage", "ServiceVersion", "VisibilityMeta",
    "VerdictStatus", "Severity", "Visibility",
    # 异常
    "SagaxAuditError", "ConfigurationError", "InsecureTransportError",
    "APIConnectionError", "APITimeoutError", "APIStatusError",
    "AuthenticationError", "QuotaExceededError", "PermissionDeniedError",
    "NotFoundError", "ConflictError", "PayloadTooLargeError",
    "ValidationError", "RateLimitError", "ServerError",
    "AuditFailedError", "AuditTimeoutError",
]
