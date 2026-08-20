# SDK 使用教程

按"你想做什么"organized。每段代码都是可以直接跑的。

先读过 [快速开始](quickstart.md) 会更顺。

---

## 目录

1. [两种用法：只审 vs 审完还帮你改](#1-两种用法)
2. [怎么描述你的结论](#2-怎么描述你的结论)
3. [怎么给证据](#3-怎么给证据)
4. [看懂审计结果](#4-看懂审计结果)
5. [拿到审计报告和审计包](#5-拿到审计报告和审计包)
6. [加你自己机构的规则](#6-加你自己机构的规则)
7. [存你自己的内部知识](#7-存你自己的内部知识)
8. [长任务：异步跑](#8-长任务异步跑)
9. [错误处理](#9-错误处理)
10. [配置项速查](#10-配置项速查)

---

## 1. 两种用法

### `check()` —— 只审，不改（最常用）

你自己生成结论，让它过一遍审计，拿裁决和修改建议。你的模型、你的流程，都不用换。

```python
result = client.check(task="...", candidate_output=..., evidence=...)
```

**推荐从这个开始。** 它不碰你的生成逻辑，接入成本最低。

### `audit()` —— 审完不通过就自动改

需要服务端配了模型才行（默认没配）。审计发现问题后会**只重新生成出错的字段**，
已通过的字段锁住不动，最多自动重试两次。

```python
result = client.audit(task="...", evidence=..., required_fields=[...])
```

> 什么叫"锁住不动"：第一轮 `revenue` 和 `roe` 通过了、`pe` 错了，
> 那第二轮只准改 `pe`。整篇重写会把本来对的也改坏——这是审计工具最忌讳的事。

---

## 2. 怎么描述你的结论

**核心要求：每个数字单独成一个字段，别糊成一段话。**

只给一段文字，审计就只能做文本级检查，也没法告诉你该改哪里——只能让你整篇重写。

```python
candidate_output = {
    "fields": [
        {
            "name": "revenue",              # 字段名（必填）
            "value": 85.6,                  # 数值
            "unit": "亿元",                  # 单位
            "period": "2025A",              # 报告期
            "basis": "reported",            # 口径
            "source_refs": ["年报p12"],      # 来源（必填，不给一定失败）
        },
        {
            "name": "pe",
            "value": 30.0,
            "unit": "倍",
            "period": "2026-08-18",
            "basis": "trailing",
            "source_refs": ["行情快照", "年报p13"],
            # 派生指标写清楚是拿什么算的，能查出"用预测利润算历史PE"这类错
            "inputs": {"market_cap": 306.0, "net_profit": "reported 2025A"},
        },
    ],
    "narrative": "……你的结论文字……",     # 可选，会检查文字里的数字有没有字段支撑
}
```

各字段的作用：

| 字段 | 不给会怎样 |
|---|---|
| `name` | 匹配不上任何规则，等于没审 |
| `value` / `unit` | 没法做数值比对和量级检查 |
| `period` | 触发"报告期必须标注"规则 |
| `basis` | 触发口径规则（历史/预测混用查不出来） |
| `source_refs` | 触发平台硬规则 `p0.evidence_required`，**必然失败** |
| `inputs` | 派生指标（PE、ROE 等）查不出计算口径错误 |

---

## 3. 怎么给证据

### 方式一：直接内联（一次性）

```python
result = client.check(
    task="...",
    candidate_output=...,
    evidence=[
        {"field": "revenue", "value": 85.6, "unit": "亿元", "period": "2025A",
         "source": "2025年年度报告", "source_ref": "年报p12"},
        {"field": "net_profit", "value": 10.2, "unit": "亿元", "period": "2025A",
         "source": "2025年年度报告", "source_ref": "年报p13"},
    ],
)
```

### 方式二：先上传，之后反复引用（推荐）

同一批数据要审很多次时，上传一次就够了：

```python
from sagax_amadeus import EvidenceItem

ev = client.upload_evidence_items([
    EvidenceItem(field="revenue", value=85.6, unit="亿元", period="2025A",
                 source="2025年年度报告", source_ref="年报p12"),
    EvidenceItem(field="net_profit", value=10.2, unit="亿元", period="2025A",
                 source="2025年年度报告", source_ref="年报p13"),
])
print(ev.evidence_id)        # ev_xxxx

# 之后每次审计引用它
result = client.check(task="...", candidate_output=...,
                      evidence_ids=[ev.evidence_id])
```

### 方式三：上传附件（年报 PDF、导出的 CSV）

```python
att = client.upload_evidence(path="fy2025.pdf")
```

附件是**存档**用的，会进审计包，但里面的数字不会被自动解析出来做比对——
要参与数值校验，还是得给结构化的 `EvidenceItem`。

### 管理证据

```python
client.list_evidence()                       # 列出
client.get_evidence(ev.evidence_id)          # 看元数据
client.download_evidence(ev.evidence_id)     # 下载原文
client.delete_evidence(ev.evidence_id)       # 删除
```

### 标记敏感证据

```python
EvidenceItem(field="net_profit", value=12.5, ..., visibility="private")
```

`private` 表示这条在你租户内部也算敏感，会写进审计报告的标注里。
它**不影响**审计本身能不能用它——你的数据本来就在你的租户空间里。

---

## 4. 看懂审计结果

```python
result = client.check(...)

result.status        # PASS / RETRY / BLOCK / NEED_HUMAN
result.passed        # status == "PASS" 的简写
result.attempts      # 跑了几轮
```

### 问题清单

```python
for f in result.findings():
    print(f.origin)       # local = 你的规则 / cloud = 平台公共规则
    print(f.rule_id)      # 哪条规则
    print(f.field)        # 哪个字段
    print(f.severity)     # critical / high / medium / low / info
    print(f.reason)       # 为什么
    print(f.expected_value)  # 应该是多少
```

`origin` 分两侧是有意义的：

- `local` —— **你自己机构的规则**判的。规则原文只在你的租户空间里，
  平台的公共规则引擎看不到它的内容（只拿到一个匿名 id）。
- `cloud` —— **平台公共规则**判的：定义自洽、与证据一致、口径披露、
  单位量级、正文数字有支撑。

两边对同一个字段各报一条不是重复，是两条独立证据。

### 修复计划

```python
plan = result.repair_plan()
plan.locked_fields          # 已通过，一个字都别动
plan.fields_to_regenerate   # 只准改这些
plan.repair_instructions    # 人话说明该怎么改
```

### 最终产出

```python
result.output()             # 修复循环跑完之后的那一版（用了 audit() 才有意义）
```

---

## 5. 拿到审计报告和审计包

```python
# 人能读的报告（Markdown）
client.download_report(result.audit_id, "audit_report.md")

# 完整审计包（tar.gz）—— 存档用
client.download_bundle(result.audit_id, "bundle.tar.gz")

# 执行过程（结构化事件）
for e in client.get_trace(result.audit_id):
    print(e.kind, e.status, e.data)
```

**报告**回答四个问题：哪些由你的规则验过、哪些由平台规则验过、哪些**没被验过**、
还剩什么风险。最后一项尤其重要——"没查"和"通过"是两回事，报告不会把它们混为一谈。

**审计包**是自包含的：规则快照（含版本和内容哈希）、逐轮问题清单、证据、
每一版结论和它们之间的 diff、修复计划、报告。整个目录拷走就能独立复核。
将来被问"这个数字当时是怎么核的"，拿这个出来。

> 审计包里的 `private_boundaries.yaml` 含你自己规则的原文，不要外发给
> 租户以外的人。要对外只发 `audit_report.md`。

---

## 6. 加你自己机构的规则

平台规则是通用的（PE 定义、数值一致、口径披露……）。你机构内部的规矩得你自己加。

```python
client.create_boundary({
    "rule_id": "my.roe_range",
    "title": "ROE 必须在 0-60% 之间",
    "statement": "超出这个区间的 ROE 多半是口径或单位错了。",
    "tier": "P1",                    # P1 = 你的硬约束，平台规则不能覆盖它
    "severity": "high",
    "validator": "field_range",      # 用哪个检查器
    "applies_to": ["roe"],           # 管哪些字段
    "params": {"min": 0, "max": 60},
})
```

之后每次审计自动生效：

```python
result = client.check(task="核对 ROE", candidate_output={"fields": [
    {"name": "roe", "value": 850.0, "unit": "%", "period": "2025A",
     "basis": "reported", "source_refs": ["年报p20"]}]},
    required_fields=["roe"])

# RETRY，findings 里有 ('local', 'my.roe_range')
```

### 常用检查器

| validator | 干什么 | params |
|---|---|---|
| `field_range` | 数值必须在区间内 | `{"min": …, "max": …}` |
| `require_source_refs` | 必须有来源 | — |
| `forbid_forecast_basis` | 不许用预测口径 | `{"expected_basis": "reported"}` |
| `basis_disclosed` | 必须标注口径 | `{"allowed": [...]}` |
| `ratio_consistency` | 比率必须和分子分母对得上 | `{"numerator": …, "denominator": …}` |
| `value_matches_evidence` | 数值必须和证据一致 | `{"tolerance": 0.005}` |
| `required_fields_present` | 指定字段必须齐全 | `{"fields": [...]}` |

### 规则优先级

```
P0  平台硬规则     ← 你关不掉（比如"数字必须有来源"）
P1  你的硬约束     ← 平台规则覆盖不了它
P2  你的组织规则
P3  平台通用规则
P4  已验证的学习规则
P5  本次任务临时规则
```

你的 P1 和平台 P3 在同一个字段上要求相反时，**不会**静默选一边——
整次审计升级为 `NEED_HUMAN`，等人拍板。

### 管理规则

```python
client.list_boundaries()                    # 我的规则
client.delete_boundary("my.roe_range")      # 停用
client.public_boundaries()                  # 平台公共规则（只读）
```

---

## 7. 存你自己的内部知识

这些内容会参与审计（帮助定位问题、生成修复建议），也方便团队共享。都绑定你的租户，
别的客户看不到。

```python
# 内部案例、经验教训
client.create_memory(title="PE 口径事故", kind="error_case", tags=["pe"],
                     body="2025Q2 有份材料用了明年的预测利润算当前 PE，对外后被客户指出。")

# 内部检查清单
client.create_skill(name="估值口径检查",
                    description="交付估值指标前的自查",
                    body="逐条确认：分子是不是总市值、分母是不是已实现归母净利润、口径有没有标注。")

# 内部制度说明（人读的）
client.create_wiki_document(slug="pe-caliber", title="内部 PE 口径规定",
                            body="本机构对外材料中的 PE 一律使用已实现盈利……")
```

增删改查都有：

```python
client.list_memories(query="PE")             # 检索
client.get_memory(mem_id)
client.update_memory(mem_id, body="改过的")
client.delete_memory(mem_id)
# skill / wiki 同样是 list_ / get_ / update_ / delete_
```

> Wiki 存**人读的说明**，规则存**机器执行的检查**。两者用 `related_rules` 互相
> 引用，但不能互相替代——拿 Wiki 正文当规则跑，等于把审计依据交给自然语言解析。

---

## 8. 长任务：异步跑

一次审计要调模型、可能重试两轮，几分钟很正常。别用一个同步请求死等。

```python
job = client.create_audit(task="...", evidence_ids=[...],
                          required_fields=[...])
print(job.audit_id, job.status)      # aud_xxxx queued

# 做别的事……

result = client.wait_for_audit(job.audit_id, timeout=600)
```

也可以自己轮询：

```python
status = client.get_audit_status(job.audit_id)
# {'status': 'running', 'started_at': ..., ...}
# 状态：queued → running → completed / failed / cancelled
```

> `wait_for_audit` 超时**不代表任务没了**——它还在云端跑，拿着同一个 `audit_id`
> 以后还能查。

其他：

```python
client.list_audits(limit=20)          # 历史任务
client.get_audit(job.audit_id)        # 任务详情
client.cancel_audit(job.audit_id)     # 取消（进行中）/ 删除（已结束）
```

### 异步客户端

```python
from sagax_amadeus import AsyncSagaxAuditClient

async with AsyncSagaxAuditClient() as client:
    job = await client.create_audit(task="...")
    result = await client.wait_for_audit(job.audit_id)
```

---

## 9. 错误处理

**按异常类型分支，不要按状态码数字。**

```python
from sagax_amadeus import (AuthenticationError, QuotaExceededError,
                         NotFoundError, ValidationError, RateLimitError,
                         ServerError, APIConnectionError, SagaxAuditError)

try:
    result = client.check(...)
except QuotaExceededError:
    通知运营("配额用完了")
except ValidationError as e:
    print("请求写错了:", e.message)
except (APIConnectionError, ServerError):
    稍后重试()
except SagaxAuditError as e:
    报障(e.request_id)          # 报障时把这个给我们，日志能直接定位
```

| 异常 | 状态 | 意思 |
|---|---|---|
| `AuthenticationError` | 401 | Key 没设 / 错了 / 已吊销 |
| `QuotaExceededError` | 402 | 配额用尽 |
| `PermissionDeniedError` | 403 | 订阅停用 |
| `NotFoundError` | 404 | 不存在，**或者不属于你** |
| `ConflictError` | 409 | 审计还没跑完就取结果 |
| `PayloadTooLargeError` | 413 | 上传太大 |
| `ValidationError` | 422 | 请求体不合法 |
| `RateLimitError` | 429 | 被限流 |
| `ServerError` | 5xx | 服务端出错 |
| `AuditFailedError` | — | 任务失败/被取消（不是 BLOCK，BLOCK 是正常结论） |
| `AuditTimeoutError` | — | 等超时了，任务还在跑 |

> 404 同时表示"不属于你"是有意的。返回 403 等于告诉对方"这个 id 确实存在，
> 只是不归你"——那本身就是一次信息泄露。

**关于重试**：GET/DELETE 这类幂等请求，遇到 429/5xx/断网时 SDK 会自动重试并退避。
`create_audit` **不会**自动重试——重发一次就是多跑一次审计、多扣一次配额。

---

## 10. 配置项速查

```python
client = SagaxAuditClient(
    base_url="https://audit.example.com",
    api_key="sagax_sk_...",
    project_id="research",       # 同一租户下再分项目，互相隔离
    timeout=60,                  # 单次 HTTP 超时（不是等审计完成的时间）
    max_retries=2,
    ca_bundle="/path/ca.crt",    # 服务端用自签证书时
)
```

也可以全走环境变量（推荐，别把 Key 写进代码）：

| 变量 | 用途 |
|---|---|
| `SAGAX_AUDIT_API_BASE_URL` | 服务地址 |
| `SAGAX_AUDIT_API_KEY` | 你的 Key |
| `SAGAX_AUDIT_PROJECT_ID` | 项目（默认 `default`） |
| `SAGAX_AUDIT_CA_BUNDLE` | 自签证书 |

用完记得关（或者用 `with`）：

```python
with SagaxAuditClient() as client:
    ...
```

### 数据去向

用托管服务时，这些东西会经 HTTPS 传到服务端并存在你的租户空间里：原始
Evidence、私有 Memory / Skill / LLM Wiki、私有规则。

- 其他租户检索不到、读不到、改不到、删不到；
- 跨租户访问返回 **404 而不是 403** —— 403 等于确认「这个 id 存在，只是不属于
  你」，那本身就是一次泄露；
- 私有规则的**原文不进公共规则引擎**，只传匿名 id 与内容哈希；
- 默认**不用于训练模型**；
- 私有资源**不会自动**变成公共资源：唯一入口要求内容已脱敏 + 你显式确认，
  落库后仍需人工审核；
- 非本机地址一律要求 HTTPS，明文会被客户端直接拒绝。

要求数据一步都不出机房，就用私有化部署（引擎起在你自己的网络里），
这一节里除了「不出网」之外的隔离性质同样成立。

### 命令行

同样的功能有一套 CLI，脚本里更顺手：

```bash
sagax-amadeus status                                # 连接与配额
sagax-amadeus audit --input run.json --report out.md
sagax-amadeus audits list|get|status|result|report|bundle|trace|cancel
sagax-amadeus evidence upload|list|get|download|delete
sagax-amadeus memory|skill|wiki  add|search|list|get|update|delete
sagax-amadeus boundary add-rule|list|public|disable
```

---

## 一个完整例子

```python
from sagax_amadeus import SagaxAuditClient, EvidenceItem

client = SagaxAuditClient()

# 一次性：把机构规矩写进去
client.create_boundary({
    "rule_id": "my.no_forecast_pe",
    "title": "不许用预测利润算历史 PE",
    "tier": "P1", "severity": "critical",
    "validator": "forbid_forecast_basis", "applies_to": ["pe"],
    "params": {"expected_basis": "reported / trailing / ttm"},
})

# 每次审计
ev = client.upload_evidence_items([
    EvidenceItem(field="revenue", value=85.6, unit="亿元", period="2025A",
                 source="2025年报", source_ref="p12"),
    EvidenceItem(field="net_profit", value=10.2, unit="亿元", period="2025A",
                 source="2025年报", source_ref="p13"),
])

result = client.check(
    task="核对 2025 年报关键指标",
    candidate_output=我的模型产出的结构化结论,
    evidence_ids=[ev.evidence_id],
    required_fields=["revenue", "net_profit", "roe", "pe"],
)

if result.passed:
    发布(我的报告)
else:
    plan = result.repair_plan()
    print("这些不能动:", plan.locked_fields)
    print("重新生成:", plan.fields_to_regenerate)
    for f in result.findings():
        print(f"  {f.field}: {f.reason}")
    client.download_report(result.audit_id, "为什么没通过.md")

client.close()
```
