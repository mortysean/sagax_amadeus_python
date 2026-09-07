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
class VerdictStatus(str, Enum):
    """审阅**结论**。

    PASS 通过；BLOCK 有高危问题，不该就这么交出去；NEED_HUMAN 需要人工判断。
    RETRY 留着是为了读得懂历史结果 —— 它是「引擎替你改一遍再审」的产物，
    而文档审阅不改宿主的文档，所以新结果里不会再出现它。
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


#: 严重度由重到轻 —— :meth:`ReviewResult.by_severity` 的分组顺序。
#:
#: 写死一份而不是 ``reversed(Severity)``：枚举的声明顺序哪天被人调整一下，
#: 分组就会静默地倒过来，最重的问题排到最后，而没有任何测试会红。
_SEVERITY_DESC = (Severity.CRITICAL.value, Severity.HIGH.value,
                  Severity.MEDIUM.value, Severity.LOW.value, Severity.INFO.value)


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


# --------------------------------------------------------------------------- #
# 响应侧
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 文档审阅
# --------------------------------------------------------------------------- #
@dataclass
class TextSpan(_Model):
    """文档里的一处位置 —— 高亮的锚点。

    ``char_start`` / ``char_end`` 落在**原文**上（云端逐字符照抄的解码结果，
    不做任何规范化），所以这对偏移量可以直接拿去切你自己那份文件。
    ``page`` / ``bbox`` 只有解析器拿得到版面信息时才有（PDF 有，markdown 没有），
    渲染时优先用 bbox，拿不到就回落到字符区间。

    ``quote`` 冗余存了一份区间原文：偏移错位是这类功能最常见也最难发现的 bug，
    而错位的高亮比没有高亮更糟 —— 它会指着一句没问题的话说这里有问题。
    交付前拿 quote 和自己切出来的文本对一下，错位就藏不住。
    """

    char_start: int = 0
    char_end: int = 0
    quote: str = ""
    page: Optional[int] = None
    #: ``[x0, y0, x1, y1]``，PDF 坐标系。
    bbox: Optional[list[float]] = None
    block_id: Optional[str] = None


@dataclass
class Annotation(_Model):
    """一处标注：高亮 + comment。这是交付给宿主的主要产物。

    每条标注都带 ``rule_id`` 与 ``finding_id``。没有规则来源的标注不会出现 ——
    那等于「模型觉得这里不对」，正是这个产品不做的事。
    """

    annotation_id: str = ""
    #: 空 spans = 问题成立但定位不到正文（例如字段级结论）。这类画不出高亮，
    #: 应当按**文档级批注**展示 —— 因为定位不到就把它丢掉，等于漏报一条问题。
    spans: list[TextSpan] = dc_field(default_factory=list)
    quote: str = ""
    #: 断言类型；没有对应断言时为 None。不要把 None 当成某种默认类型填上，
    #: 那会把一条字段级问题谎报成正文里的数值断言。
    kind: Optional[str] = None
    severity: str = Severity.MEDIUM.value
    rule_id: str = ""
    finding_id: str = ""
    claim_id: Optional[str] = None
    origin: str = "local"        # local = 你的私有规则 / cloud = 平台公共规则
    #: 给人读的一句话：这里为什么被标出来。
    comment: str = ""
    #: 可执行的修改建议。我们不改你的文档，但要说清楚该怎么改。
    suggested_fix: str = ""
    evidence_refs: list[str] = dc_field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Annotation":
        annotation = super().from_dict(data or {})
        annotation.spans = [TextSpan.from_dict(s)
                            for s in (data or {}).get("spans") or []]
        return annotation


@dataclass
class Deduction(_Model):
    """一条扣分项。

    评级必须能逐条解释「为什么是 C」，否则它只是另一个不可复核的分数 ——
    而不可复核的分数正是这套东西要替代的。
    """

    code: str = ""
    detail: str = ""
    severity: str = Severity.MEDIUM.value
    finding_ids: list[str] = dc_field(default_factory=list)
    #: 属性名就叫 fields（API 契约如此），它在类体里遮住 ``dataclasses.fields``。
    #: 这里没关系 —— 本类不调用那个函数；真要加 from_dict 时记得先改名导入。
    fields: list[str] = dc_field(default_factory=list)


@dataclass
class Grade(_Model):
    """文档级评级。纯确定性聚合，不引入任何新判断。"""

    #: 不默认成 ``A``：响应里没有评级时给出最高档，等于凭空替云端说了句
    #: 「这份文档没问题」。空字符串一眼看得出是「没评」。
    letter: str = ""
    rationale: str = ""
    evidence_coverage: float = 0.0
    #: 抽取 / 已核验 / 未核验。三者的差就是「没查的部分」，必须显式交付。
    claims_extracted: int = 0
    claims_verified: int = 0
    claims_unverified: int = 0
    #: 评级口径的版本，由云端给。SDK 不填默认值 —— 编一个口径号出来，
    #: 历史评级就没法解释了：同一个 C 在不同版本下可能不是一个意思。
    rubric_version: str = ""
    deductions: list[Deduction] = dc_field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Grade":
        grade = super().from_dict(data or {})
        grade.deductions = [Deduction.from_dict(d)
                            for d in (data or {}).get("deductions") or []]
        return grade


@dataclass
class RiskSummary(_Model):
    """结构化风险总结。宿主读 ``blocking`` 一个布尔值就够做交付决定。"""

    headline: str = ""
    residual_risks: list[str] = dc_field(default_factory=list)
    #: 没有被抽取覆盖到的正文占比。「没查」和「查过通过」必须分开报。
    unverified_text_ratio: float = 0.0
    not_cloud_verified: list[str] = dc_field(default_factory=list)
    boundary_conflicts: list[str] = dc_field(default_factory=list)
    #: 是否应当拦下不交付。
    blocking: bool = False


@dataclass
class ReviewResult(_Model):
    """一次文档审阅的结果（``POST /v1/reviews`` 与 ``GET /v1/reviews/{id}``）。

    四样交付物：``annotations`` 是高亮与 comment，``grade`` 是评级，
    ``risk_summary`` 是风险总结，``coverage_note`` 是覆盖度声明。

    最后一样别当成客套话：它写的是「抽了 N 条断言、其中 M 条可核验」。
    没被抽取到的正文属于**没查过**，不等于通过 —— 把这句话跟着结论一起转述，
    「没发现问题」才不会被读成「没有问题」。
    """

    review_id: str = ""
    #: PASS / BLOCK / NEED_HUMAN，见 :class:`VerdictStatus`。
    status: str = ""
    coverage_note: str = ""
    grade: Optional[Grade] = None
    risk_summary: Optional[RiskSummary] = None
    annotations: list[Annotation] = dc_field(default_factory=list)
    #: 解析元数据：``source_format`` / ``parser`` / ``has_layout`` / ``blocks``。
    #: 保持 dict —— 文档怎么解析是云端的事，SDK 不解释它，换解析器也不用改这里。
    document: dict[str, Any] = dc_field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewResult":
        data = data or {}
        result = super().from_dict(data)
        # 列表接口（``GET /v1/reviews``）只给摘要：一个 ``grade_letter`` 字母，
        # 完整的 Grade 对象要按 id 取详情才有。两个接口用**不同的字段名**，
        # 所以这里不需要按类型猜形状 —— 同名不同形才是最难查的那种缺陷。
        grade = data.get("grade")
        if grade is None and data.get("grade_letter"):
            grade = {"letter": data["grade_letter"]}
        result.grade = Grade.from_dict(grade) if grade else None
        result.risk_summary = (RiskSummary.from_dict(data["risk_summary"])
                               if data.get("risk_summary") else None)
        result.annotations = [Annotation.from_dict(a)
                              for a in data.get("annotations") or []]
        return result

    @property
    def blocking(self) -> bool:
        """是否应当拦下不交付。

        没有 ``risk_summary`` 时返回 **True**。响应被截断、或云端因故没给出
        风险总结时，我们知道的是「不知道」，而把「不知道」默认成 False，
        等于让一次坏掉的响应替你说出「这份文档可以交」。
        """
        return True if self.risk_summary is None else bool(self.risk_summary.blocking)

    def by_severity(self) -> dict[str, list[Annotation]]:
        """标注按严重度分组，最重的一组在前。

        云端将来加了新的严重度时，它单独成一组排在末尾：认不出的严重度
        **不能丢**，丢掉就是漏报一条问题。
        """
        buckets: dict[str, list[Annotation]] = {}
        for name in _SEVERITY_DESC:
            hits = [a for a in self.annotations if a.severity == name]
            if hits:
                buckets[name] = hits
        for annotation in self.annotations:
            if annotation.severity not in _SEVERITY_DESC:
                buckets.setdefault(annotation.severity, []).append(annotation)
        return buckets


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
    "VerdictStatus", "Severity", "Visibility",
    "OutputField", "CandidateOutput", "EvidenceItem",
    "AuditFinding", "AuditVerdict", "AuditResult",
    "ReviewResult", "Annotation", "TextSpan", "Grade", "Deduction",
    "RiskSummary",
    "Evidence", "VisibilityMeta", "Memory", "Skill", "WikiDocument",
    "TraceEvent", "Usage", "ServiceVersion",
]
