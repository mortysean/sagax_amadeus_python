"""Sagax Audit SDK — 公开 API 数据结构。

这里**只有公网 API 契约里出现过的东西**。云端内部模型（validator 状态、
修复循环状态、内部规则模型、Prompt 上下文、ORM 行、任务队列、计量明细）
一个都不在，也不该在：SDK 是 HTTP 客户端，它不需要知道审计是怎么算出来的。

全部用标准库 ``dataclasses``，所以这个包**没有任何运行时依赖**。改造前
SDK 依赖 pydantic，那是因为它要在本地跑完整审计、要做业务级校验；现在它只
拼 JSON、发请求、读响应，再拖一个 pydantic 进客户的依赖树没有道理。

宽进严出：:meth:`from_dict` 忽略不认识的字段，所以云端**加**字段不会让老 SDK
崩掉。删字段或改语义要走 ``/v1/version`` 的 API 版本协商，不靠 SDK 猜。
"""
from __future__ import annotations

# ``field`` 得改名导入：AuditFinding / EvidenceItem 自己就有一个叫 field 的
# 属性，在类体里它会把 dataclasses.field 顶掉，于是 default_factory 那行
# 变成 None(...)。改名比给这两个属性换名字好 —— 属性名是 API 契约。
from dataclasses import asdict, dataclass, fields
from dataclasses import field as dc_field
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class AuditStatus(str, Enum):
    """审计**任务**的生命周期状态（不是审计结论）。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """终态：不会再变了，轮询可以停。"""
        return self in (AuditStatus.COMPLETED, AuditStatus.FAILED,
                        AuditStatus.CANCELLED)


class VerdictStatus(str, Enum):
    """审计**结论**。任务 ``completed`` 之后才有意义。

    PASS 通过；RETRY 有可定向修复的问题；BLOCK 不可自动修复；
    NEED_HUMAN 需要人工判断。
    """

    PASS = "PASS"
    RETRY = "RETRY"
    BLOCK = "BLOCK"
    NEED_HUMAN = "NEED_HUMAN"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Visibility(str, Enum):
    """资源可见范围。

    ``private`` / ``tenant`` 都不会离开你的租户；``public`` 只用于平台自己
    维护的公共内容 —— 客户创建的资源不会自动变成 public。
    """

    PRIVATE = "private"
    TENANT = "tenant"
    PUBLIC = "public"


# --------------------------------------------------------------------------- #
# 基类
# --------------------------------------------------------------------------- #
class _Model:
    """dataclass 的 JSON 互转。"""

    def to_dict(self) -> dict[str, Any]:
        """转成可直接进 JSON 的字典（丢掉 None，服务端按默认值处理）。"""
        return {k: _plain(v) for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        """从响应体构造。**忽略不认识的字段** —— 云端加字段不能让老 SDK 崩。"""
        known = {f.name for f in fields(cls)}          # type: ignore[arg-type]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# 请求侧
# --------------------------------------------------------------------------- #
@dataclass
class OutputField(_Model):
    """待审输出里的一个结构化字段（审计的基本单位）。"""

    name: str
    value: Optional[float] = None
    text: Optional[str] = None
    unit: str = ""
    period: str = ""
    basis: str = ""              # 口径，如 "reported" / "trailing" / "forward"
    source_refs: list[str] = dc_field(default_factory=list)
    inputs: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class CandidateOutput(_Model):
    """要送去审计的一份产出：结构化字段 + 叙述文本。"""

    fields: list[OutputField] = dc_field(default_factory=list)
    narrative: str = ""
    sections: dict[str, str] = dc_field(default_factory=dict)
    attempt: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"fields": [f.to_dict() if isinstance(f, OutputField) else f
                           for f in self.fields],
                "narrative": self.narrative, "sections": self.sections,
                "attempt": self.attempt}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateOutput":
        data = data or {}
        return cls(
            fields=[OutputField.from_dict(f) for f in data.get("fields") or []],
            narrative=data.get("narrative", ""),
            sections=data.get("sections") or {},
            attempt=int(data.get("attempt", 1)))


@dataclass
class EvidenceItem(_Model):
    """一条结构化证据。审计的确定性来源 —— 数值必须能落回某条 evidence。"""

    field: str = ""
    value: Optional[float] = None
    raw_value: Optional[str] = None
    unit: str = ""
    period: str = ""
    source: str = ""             # 数据来源（交易所 / 数据商 / 公告）
    source_ref: str = ""         # 可回溯引用（URL / tool_call_id / 文件）
    retrieved_at: Optional[str] = None
    is_forecast: bool = False    # 预测值 vs 已实现值 —— PE 口径规则要用
    evidence_id: Optional[str] = None
    #: ``private`` / ``tenant``。标记的是**这条证据在你租户内的敏感度**，
    #: 会写进审计报告，也决定它能不能被贡献进公共库。它**不**表示「不上传」——
    #: 整条证据本来就在云端的租户空间里，只是别的租户看不到。
    visibility: Optional[str] = None


@dataclass
class AuditProfile(_Model):
    """审计档位：决定云端启用哪些公共校验组。"""

    name: str = "equity_research_default"
    domains: list[str] = dc_field(
        default_factory=lambda: ["equity", "financial_statement"])
    tolerance: float = 0.005
    require_source_refs: bool = True
    max_retries: int = 2


# --------------------------------------------------------------------------- #
# 响应侧
# --------------------------------------------------------------------------- #
@dataclass
class Audit(_Model):
    """一个审计任务。``POST /v1/audits`` 与 ``GET /v1/audits/{id}`` 的响应。"""

    audit_id: str = ""
    status: str = AuditStatus.QUEUED.value
    task: str = ""
    tenant_id: str = ""
    project_id: str = "default"
    run_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    artifacts: dict[str, bool] = dc_field(default_factory=dict)

    @property
    def audit_status(self) -> AuditStatus:
        """状态枚举。云端出现新状态时回落到 ``QUEUED`` 之外的原值不做猜测。"""
        return AuditStatus(self.status)

    @property
    def is_terminal(self) -> bool:
        return self.status in (AuditStatus.COMPLETED.value,
                               AuditStatus.FAILED.value,
                               AuditStatus.CANCELLED.value)


@dataclass
class AuditFinding(_Model):
    """一条审计问题。必须能定位到字段 / 步骤 / 证据，否则无法定向修复。"""

    finding_id: str = ""
    origin: str = "local"        # local = 你的私有规则 / cloud = 平台公共规则
    rule_id: str = ""
    field: Optional[str] = None
    step_id: Optional[str] = None
    actual_value: Optional[Any] = None
    expected_value: Optional[Any] = None
    reason: str = ""
    severity: str = Severity.MEDIUM.value
    impact: str = ""
    repair_strategy: str = ""
    status: str = "open"
    source_refs: list[str] = dc_field(default_factory=list)
    attempt: int = 1


@dataclass
class RepairPlan(_Model):
    """定向修复计划。``locked_fields`` 是硬承诺：这些字段不要动。"""

    run_id: str = ""
    strategy: str = ""
    repair_instructions: list[str] = dc_field(default_factory=list)
    locked_fields: list[str] = dc_field(default_factory=list)
    fields_to_regenerate: list[str] = dc_field(default_factory=list)
    sections_to_regenerate: list[str] = dc_field(default_factory=list)
    required_sources: list[str] = dc_field(default_factory=list)
    max_retries: int = 2
    attempt: int = 1


@dataclass
class AuditVerdict(_Model):
    """审计裁决。"""

    run_id: str = ""
    status: str = VerdictStatus.PASS.value
    passed_fields: list[str] = dc_field(default_factory=list)
    failed_fields: list[str] = dc_field(default_factory=list)
    #: 没有被任何规则覆盖到的字段 —— 既不是通过也不是失败，是「没查」。
    unverified_fields: list[str] = dc_field(default_factory=list)
    confidence: float = 1.0
    evidence_coverage: float = 0.0
    cloud_verified: bool = False
    not_cloud_verified: list[str] = dc_field(default_factory=list)
    retry_allowed: bool = True
    attempt: int = 1
    local_findings: list[dict[str, Any]] = dc_field(default_factory=list)
    cloud_findings: list[dict[str, Any]] = dc_field(default_factory=list)
    merged_findings: list[dict[str, Any]] = dc_field(default_factory=list)

    @property
    def verdict_status(self) -> VerdictStatus:
        return VerdictStatus(self.status)

    def findings(self) -> list[AuditFinding]:
        """合并后的 Finding 列表。"""
        return [AuditFinding.from_dict(f) for f in self.merged_findings]


@dataclass
class AuditResult(_Model):
    """一次审计的完整结果（``GET /v1/audits/{id}/result``）。"""

    audit_id: str = ""
    run_id: str = ""
    status: str = VerdictStatus.PASS.value
    attempts: int = 1
    final_output: Optional[dict[str, Any]] = None
    verdict: Optional[dict[str, Any]] = None
    attempt_verdicts: list[dict[str, Any]] = dc_field(default_factory=list)
    repair_history: list[dict[str, Any]] = dc_field(default_factory=list)
    artifacts: dict[str, bool] = dc_field(default_factory=dict)
    usage_recorded: bool = False
    started_at: str = ""
    finished_at: Optional[str] = None

    @property
    def verdict_status(self) -> VerdictStatus:
        return VerdictStatus(self.status)

    @property
    def passed(self) -> bool:
        return self.status == VerdictStatus.PASS.value

    def output(self) -> Optional[CandidateOutput]:
        """最终产出（经修复循环之后的那一版）。"""
        return (CandidateOutput.from_dict(self.final_output)
                if self.final_output else None)

    def audit_verdict(self) -> Optional[AuditVerdict]:
        return AuditVerdict.from_dict(self.verdict) if self.verdict else None

    def findings(self) -> list[AuditFinding]:
        """最后一轮的合并 Finding。首轮的在 ``attempt_verdicts[0]`` 里。"""
        v = self.audit_verdict()
        return v.findings() if v else []

    def repair_plan(self) -> Optional[RepairPlan]:
        """最后一轮的修复计划 —— RETRY 时按它去改。"""
        if not self.repair_history:
            return None
        plan = self.repair_history[-1].get("plan")
        return RepairPlan.from_dict(plan) if plan else None


@dataclass
class Evidence(_Model):
    """一条已上传的 Evidence 的元数据（不含正文）。"""

    evidence_id: str = ""
    tenant_id: str = ""
    project_id: str = "default"
    filename: str = ""
    content_type: str = ""
    size_bytes: int = 0
    sha256: str = ""
    kind: str = "file"           # file / json / csv
    item_count: int = 0          # 解析出的结构化证据条数
    metadata: dict[str, Any] = dc_field(default_factory=dict)
    retention: str = "active"
    created_at: str = ""
    deleted_at: Optional[str] = None


@dataclass
class VisibilityMeta(_Model):
    """资源的可见性与版本元数据。"""

    visibility: str = Visibility.PRIVATE.value
    storage_location: str = "cloud"
    sync_policy: str = "never"
    owner_id: Optional[str] = None
    tenant_id: Optional[str] = None
    project_id: Optional[str] = None
    version: int = 1
    content_hash: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Memory(_Model):
    """一条私有 Memory。"""

    memory_id: str = ""
    title: str = ""
    body: str = ""
    kind: str = "note"           # note / rule_hint / error_case / preference
    tags: list[str] = dc_field(default_factory=list)
    slug: Optional[str] = None
    meta: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def version(self) -> int:
        return int(self.meta.get("version", 1))


@dataclass
class Skill(_Model):
    """一个私有 Skill。"""

    skill_id: str = ""
    name: str = ""
    description: str = ""
    body: str = ""
    tags: list[str] = dc_field(default_factory=list)
    tool_scope: list[str] = dc_field(default_factory=list)
    scripts: list[str] = dc_field(default_factory=list)
    references: list[str] = dc_field(default_factory=list)
    signature: Optional[str] = None
    compatibility: str = ""
    meta: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        """是否启用。删除是软删除，禁用的 Skill 不参与审计。"""
        return str(self.meta.get("status", "active")) == "active"

    @property
    def version(self) -> int:
        return int(self.meta.get("version", 1))


@dataclass
class WikiDocument(_Model):
    """一页私有 LLM Wiki。"""

    page_id: str = ""
    slug: str = ""
    title: str = ""
    body: str = ""
    tags: list[str] = dc_field(default_factory=list)
    related_rules: list[str] = dc_field(default_factory=list)
    meta: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def version(self) -> int:
        return int(self.meta.get("version", 1))


@dataclass
class TraceEvent(_Model):
    """一条结构化 Trace 事件。

    刻意没有「reasoning / thinking / chain_of_thought」字段：模型隐藏思维链
    不记录，也就不会经这条通道传给你。
    """

    run_id: str = ""
    seq: int = 0
    at: str = ""
    step_id: str = ""
    kind: str = ""
    status: str = "ok"
    duration_ms: Optional[int] = None
    data: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class Usage(_Model):
    """订阅用量摘要。"""

    tenant_id: str = ""
    plan: str = ""
    quota_monthly: int = 0
    audits_used: int = 0
    recent: list[dict[str, Any]] = dc_field(default_factory=list)

    @property
    def unlimited(self) -> bool:
        return self.quota_monthly < 0

    @property
    def remaining(self) -> Optional[int]:
        """剩余配额；不限量时为 None。"""
        if self.unlimited:
            return None
        return max(0, self.quota_monthly - self.audits_used)


@dataclass
class ServiceVersion(_Model):
    """``GET /v1/version`` 的响应。

    四个版本各自独立：API 契约、SDK、审计引擎、公共规则内容。混成一个号会让
    「规则更新了但引擎没动」这种最常见的发布无法表达。
    """

    api_version: str = ""
    audit_engine_version: str = ""
    public_rule_version: Optional[str] = None
    public_rule_count: Optional[int] = None
    sdk_min_version: str = ""
    schema_version: str = ""


__all__ = [
    "AuditStatus", "VerdictStatus", "Severity", "Visibility",
    "OutputField", "CandidateOutput", "EvidenceItem", "AuditProfile",
    "Audit", "AuditFinding", "RepairPlan", "AuditVerdict", "AuditResult",
    "Evidence", "VisibilityMeta", "Memory", "Skill", "WikiDocument",
    "TraceEvent", "Usage", "ServiceVersion",
]
