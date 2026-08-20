---
name: sagax-audit
description: 在交付任何投研结论之前，用 Sagax Audit 做公私双侧审计。当你要对外给出财务数字、估值指标（营业收入/归母净利润/ROE/PE 等）或投研结论时使用；它会在云端用你所在机构的私有规则 + 平台公共规则做确定性校验，返回 PASS/RETRY/BLOCK/NEED_HUMAN 和定向修复计划。
---

# Sagax Audit Agent（Sagax审计智能体）

投研结论在交付前必须过审计。这个 Skill 告诉你怎么调、怎么读结果、以及哪些事
绝对不能做。

审计是**确定性**的：比数值、比口径、比来源，不靠模型判断。所以它的结论可复现，
你不需要（也不应该）和它争辩。

审计在 **Sagax Audit Cloud** 上执行，你这边只是发一次 HTTPS 请求。你提交的
候选输出与证据会上传到云端你所在机构的租户空间，别的租户看不到。

## 一、什么时候必须用

- 要对外给出任何财务数字或估值指标；
- 要生成投研结论、点评、路演材料；
- 要把多个数据源的数字拼进同一张表。

不需要用：纯定性讨论、不含数字的流程说明。

## 二、怎么调

### 优先：MCP 工具

```jsonc
// sagax.audit_run
{
  "task": "分析某上市公司2025年的营业收入、归母净利润、ROE和PE",
  "fields": [
    {"name": "revenue",    "value": 85.6, "unit": "亿元", "period": "2025A",
     "basis": "reported",  "source_refs": ["ann:2025A#p12"]},
    {"name": "net_profit", "value": 10.2, "unit": "亿元", "period": "2025A",
     "basis": "reported",  "source_refs": ["ann:2025A#p13"]},
    {"name": "pe",         "value": 30.0, "unit": "倍",   "period": "2026-08-18",
     "basis": "trailing",  "source_refs": ["mkt:2026-08-18", "ann:2025A#p13"]}
  ],
  "narrative": "……",
  "evidence": [
    {"field": "revenue", "value": 85.6, "unit": "亿元", "period": "2025A",
     "source": "2025年年度报告", "source_ref": "ann:2025A#p12"}
  ],
  "required_fields": ["revenue", "net_profit", "roe", "pe"]
}
```

`sagax.audit_check` 是同一件事的「只审不修」版本：拿裁决和修复计划，
自己去改。

其他工具：`sagax.boundaries`（先看会被哪些规则约束）、
`sagax.memory_search` / `sagax.wiki_search`（查本机构的内部口径规定）、
`sagax.evidence_upload`（先上传证据，之后用 evidence_ids 引用）、
`sagax.runs`（历史审计任务）、`sagax.usage`（配额还剩多少）。

### 或者：Python SDK

```python
from sagax_amadeus import SagaxAuditClient

client = SagaxAuditClient()        # 读 SAGAX_AUDIT_API_BASE_URL / _API_KEY
result = client.check(task=..., candidate_output=my_output,
                      evidence=[...], required_fields=[...])
print(result.status, result.repair_plan())
```

SDK 是**薄 HTTP 客户端**：它不在本地跑审计，远程失败也不会回落到本地跑一遍。
拿不到云端就没有审计结论 —— 这时如实说明，不要自己判一个。

## 三、**结构化字段是硬要求**

只给一段文字，审计就只能做文本级检查，也**无法定向修复** —— 系统会被迫要求你
整篇重写，而整篇重写会把本来正确的内容也改坏。

每个数值字段都要给：

| 字段 | 含义 | 不给会怎样 |
|---|---|---|
| `name` | 规范字段名 | 无法匹配规则 |
| `value` / `unit` | 数值与单位 | 无法做量级检查 |
| `period` | 报告期（2025A / 2025Q3） | 触发 `pub.period_label` |
| `basis` | 口径（reported / trailing / ttm / forward） | 触发口径规则 |
| `source_refs` | 可回溯来源 | 触发 P0 `p0.evidence_required`，**必然失败** |

## 四、怎么读结果

| status | 含义 | 你该做什么 |
|---|---|---|
| `PASS` | 通过 | 可以交付 |
| `RETRY` | 有可定位的问题 | 按 `repair_plan` 修，**只改** `fields_to_regenerate` |
| `BLOCK` | 不可自动修复或已超重试上限 | 不得交付；把 `merged_findings` 如实告诉用户 |
| `NEED_HUMAN` | 公私规则冲突 | **不要自己选一边**，交给人裁决 |

`repair_plan` 里两个列表是硬承诺：

- `locked_fields` —— 已通过审计，**一个字都不许动**（改了会被系统打回并留痕）；
- `fields_to_regenerate` —— 只准改这些。

自动重生成最多两次。两次之后仍不过，如实报告剩余风险，不要继续试。

## 五、Findings 分两侧，含义不同

- `local_findings`（`origin: local`）—— 本机构**私有规则**判定的。公共规则引擎
  拿不到这些规则的原文，只收到匿名 id。这些是你所在机构的硬约束，优先级高于
  公共规则。
- `cloud_findings`（`origin: cloud`）—— Sagax **公共规则**判定的（定义自洽、
  与证据一致、口径披露、单位量级、正文数值有支撑）。

两侧对同一字段各自报错是正常的 —— 那是两条独立证据，不是重复。

## 六、绝对禁止

1. 把 API Key 放进任何请求体、Trace、日志或输出。
2. 把本机构私有规则的**原文**塞进 `task` / `narrative` 交给公共规则引擎 ——
   它按匿名 id 参与判定，塞原文既没用也不该做。
3. 没有证据就补一个"看起来合理"的数字 —— 这会直接违反 P0 平台规则。
4. 把模型推测标成数据来源（`basis: model_estimate` 一定失败）。
5. 用预测/一致预期盈利计算标注为历史口径的 PE。
6. 审计不通过就整篇重写 —— 已通过的字段必须逐字保持不变。
7. 公私规则冲突时自行选一边。
8. 记录模型隐藏思维链（Trace 只接受结构化事件）。

## 七、审计包在哪

每次运行在**云端**生成，用 `audit_id` 下载：

```python
client.download_report(audit_id, "audit_report.md")
client.download_bundle(audit_id, "bundle.tar.gz")   # 完整证据链
```

包里含 manifest、公私边界快照、逐轮 findings、证据、每一版候选输出与 diff、
修复计划和 `audit_report.md`。报告会明确写出：哪些由**你的私有规则**验证、
哪些由**平台公共规则**验证、哪些**没被验证**、以及剩余风险。
要向用户说明审计过程时，引用这份报告，不要凭记忆复述。
