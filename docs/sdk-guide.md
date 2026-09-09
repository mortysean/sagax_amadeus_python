# SDK 使用教程

按"你想做什么"分节。每段代码都是可以直接跑的。

先读过 [快速开始](quickstart.md) 会更顺。

四件事先说清楚，后面的接口才讲得通：

- **判定不由模型做。** 抽取可以交给模型（召回更高），但"这个数字对不对"一律由
  确定性校验器判。同样的输入永远得到同样的结论。
- **证据可以不给。** 报告里点了名的财务指标，服务端会自己去数据源取真值。
  自己给证据更强：那是你声明「这个数来自这里」。取不到的部分进 `coverage_note`
  的「未核验」，**不会被当成通过**。
- **"没查过"不等于"通过"。** `coverage_note` 写明抽了多少条断言、其中多少条可核验。
  没抽到的正文属于没查过，不会被算成通过。
- **它不改你的文档。** 返回的是意见（`comment` + `suggested_fix`），改不改由你决定。

---

## 目录

1. [审一份文档](#1-审一份文档)
2. [怎么给证据](#2-怎么给证据)
3. [读标注](#3-读标注)
4. [读评级](#4-读评级)
5. [读风险总结](#5-读风险总结)
6. [拿到标注版和报告](#6-拿到标注版和报告)
7. [加你自己机构的规则](#7-加你自己机构的规则)
8. [存你自己的内部知识](#8-存你自己的内部知识)
9. [异步客户端](#9-异步客户端)
10. [错误处理](#10-错误处理)
11. [配置项速查](#11-配置项速查)

---

## 1. 审一份文档

```python
from sagax_amadeus import SagaxAuditClient

client = SagaxAuditClient()

result = client.review("2025年报点评.pdf")   # 路径，或者文档字节；证据可以不给

print(result.grade.letter)        # A / B / C / D
print(result.blocking)            # 读这一个值就够决定要不要拦
print(result.coverage_note)       # 抽了多少条断言、核了多少、没核多少
```

完整签名：

```
client.review(document, *, filename="", content_type="",
              evidence=(), evidence_ids=(), code="", auto_evidence=True,
              timeout=300.0) -> ReviewResult
```

| 参数 | 说明 |
|---|---|
| `document` | 本地文件路径（`str`）或文档字节（`bytes`）。别的类型直接报错 |
| `filename` | 给了路径时默认取 basename |
| `content_type` | 不给时按文件名推断 |
| `evidence` | 结构化证据，可选，见 [第 2 节](#2-怎么给证据) |
| `evidence_ids` | 已上传证据的 id |
| `code` | 证券代码（如 `600519.SH`）。不给则服务端从正文里认 |
| `auto_evidence` | 是否允许服务端自动取证，默认 `True`。设 `False` 退回只用你给的证据 |
| `timeout` | 单次请求超时，默认 300 秒 |

**给 `bytes` 时一定要带 `filename`。** 云端**按扩展名判定格式**——丢了文件名，
一份 PDF 会被当纯文本解析，产出一整页位置错的高亮。

支持 PDF、markdown、纯文本。只有 PDF 给得出页码和坐标，其余格式只有字符区间；
`result.document["has_layout"]` 告诉你这次有没有版面定位。

> 审阅是一次**同步长请求**：云端把解析、抽取、校验、评级全跑完才回响应。没有
> 队列，所以也没有轮询这一步。默认 300 秒是因为大文档撑破 60 秒会发生，而撑破
> 的后果不是重来一次那么简单——连接是客户端掐断的，云端照样跑完、照样计费，
> 你拿到的却是一个看起来像网络故障的超时。

`review` **不会**自动重试。重发一次就是多审一遍、多扣一次配额。

---

## 2. 怎么给证据

**可以不给。** 报告里点了名的财务指标，服务端会自己去数据源取真值。三条它不猜：
认不出证券代码、口径不唯一（裸写「净利润」）、推不出报告期 —— 猜错会把一个正确
的数字标成错的。没查的部分进 `coverage_note`，不算通过。

自己给证据仍然更强：那是你声明「这个数来自这里」，比服务端事后独立查更贴近你
实际用的口径。两者会合并。

### 方式一：直接内联（一次性）

```python
result = client.review(
    "点评.md",
    evidence=[
        {"field": "revenue", "value": 85.6, "unit": "亿元", "period": "2025A",
         "source": "2025年年度报告", "source_ref": "ann:2025A#p12"},
        {"field": "net_profit", "value": 10.2, "unit": "亿元", "period": "2025A",
         "source": "2025年年度报告", "source_ref": "ann:2025A#p13"},
    ],
)
```

内联的证据也会先落成一条 Evidence，再把它的 id 并进 `evidence_ids`——所以证据在
你租户里只存一份、只有一套生命周期，不会因为"内联"就变成游离的副本。

### 方式二：先上传，之后反复引用（推荐）

同一批数据要审很多次时，上传一次就够了：

```python
from sagax_amadeus import EvidenceItem

ev = client.upload_evidence_items([
    EvidenceItem(field="revenue", value=85.6, unit="亿元", period="2025A",
                 source="2025年年度报告", source_ref="ann:2025A#p12"),
    EvidenceItem(field="net_profit", value=10.2, unit="亿元", period="2025A",
                 source="2025年年度报告", source_ref="ann:2025A#p13"),
])
print(ev.evidence_id)        # ev_xxxx

result = client.review("点评.md", evidence_ids=[ev.evidence_id])
```

`EvidenceItem` 常用字段：`field` / `value` / `unit` / `period` / `source` /
`source_ref` / `is_forecast`。`is_forecast` 别省——预测值和已实现值混用是 PE 口径
规则专门要抓的东西。

### 方式三：上传附件（年报 PDF、导出的 CSV）

```python
att = client.upload_evidence(path="fy2025.pdf")
```

附件是**存档**用的，里面的数字不会被自动解析出来做比对——要参与数值校验，还是得给
结构化的 `EvidenceItem`。

### 管理证据

```python
client.list_evidence()                       # 列出
client.get_evidence(ev.evidence_id)          # 看元数据
client.download_evidence(ev.evidence_id)     # 下载原文
client.delete_evidence(ev.evidence_id)       # 删除
```

### 标记敏感证据

```python
EvidenceItem(field="net_profit", value=12.5, visibility="private")
```

`private` 表示这条在你租户内部也算敏感，会写进审阅报告的标注里。它**不影响**审阅
本身能不能用它——你的数据本来就在你的租户空间里。

---

## 3. 读标注

标注是这个产品的主交付物：一处高亮、一句为什么、一句怎么改。

```python
for note in result.annotations:
    span = note.spans[0]
    print(f"p{span.page} [{span.char_start}:{span.char_end}] {note.quote!r}")
    print(f"  {note.severity} · {note.rule_id} · {note.origin}")
    print(f"  为什么：{note.comment}")
    print(f"  怎么改：{note.suggested_fix}")
    print(f"  依据：{note.evidence_refs}")
```

| 字段 | 是什么 |
|---|---|
| `annotation_id` | 这条标注的 id（内容寻址） |
| `spans` | 命中的位置，可能多处；见下面的 `TextSpan` |
| `quote` | 被标注的原文 |
| `kind` | 标注类型 |
| `severity` | `critical` / `high` / `medium` / `low` / `info` |
| `rule_id` | 哪条规则判的 |
| `finding_id` / `claim_id` | 回溯到具体问题、具体断言 |
| `origin` | `local` = 你的规则 / `cloud` = 平台公共规则 |
| `comment` | 为什么有问题 |
| `suggested_fix` | 建议怎么改（**建议而已，文档不会被动过**） |
| `evidence_refs` | 判定依据的证据 |

`TextSpan`：`char_start`、`char_end`、`quote`、`page`、`bbox`、`block_id`。
`page` 和 `bbox` 只有 PDF 才有，其余格式靠 `char_start` / `char_end` 自己渲染。

### 按严重度分组

```python
for severity, notes in result.by_severity().items():
    print(severity, len(notes))       # critical → info，最重的一组在前
```

云端将来加了新的严重度，它单独成一组排在末尾——认不出的严重度不会被丢掉，丢掉就是
漏报一条问题。

### `origin` 分两侧是有意义的

- `local` —— **你自己机构的规则**判的。规则原文只在你的租户空间里，平台的公共规则
  引擎看不到它的内容（只拿到一个匿名 id）。
- `cloud` —— **平台公共规则**判的：定义自洽、与证据一致、口径披露、单位量级、
  正文数字有支撑。

两边对同一处各报一条不是重复，是两条独立证据。

### 结果是可 diff 的

`claim_id`、`finding_id`、`annotation_id` 三个 id 全部内容寻址，所以同一份文档配同一批
证据审两次，`annotations.json` 是**逐字节一样**的。要验证一次修改到底改动了什么，
直接 diff 两次结果就行，不需要人去比对措辞。

---

## 4. 读评级

```python
g = result.grade

g.letter                # A / B / C / D
g.rationale             # 为什么是这一级
g.evidence_coverage     # 证据覆盖率，0~1
g.claims_extracted      # 抽出多少条断言
g.claims_verified       # 其中核过多少条
g.claims_unverified     # 剩下多少条没核
g.rubric_version        # 评级规则的版本

for d in g.deductions:
    print(d.code, d.severity, d.detail)
    print(d.fields, d.finding_ids)      # 扣在哪些字段、对应哪些问题
```

| 等级 | 什么情况 |
|---|---|
| **D** | 触发了 P0（平台不可覆盖规则），不得交付 |
| **C** | 有 HIGH 及以上问题，或证据覆盖率低于 90%，或公私规则冲突待人工裁决 |
| **B** | 无严重问题，但还有 MEDIUM 及以下未处理，或存在未核验的内容 |
| **A** | 全部断言都能回溯到证据，且没有未核验内容 |

**每个等级都带 `deductions`，每条都指得回具体问题和具体位置。** 不会出现"评级 A、
底下却挂着一条扣分项"这种自相矛盾的交付物——等级就是从扣分项算出来的。

不给证据**不再**必然是 D：服务端会为点了名的财务指标自己取真值。评级 D 仍然
表示触发了 P0（无来源数值 / 模型估值冒充数据源），不得交付。没查到的部分进
`coverage_note`，不算通过。

> 翻旧结果之前先看 `rubric_version`。评级规则本身有版本，跨版本比较字母没有意义。

---

## 5. 读风险总结

```python
r = result.risk_summary

r.headline                  # 一句话结论
r.residual_risks            # 还剩什么风险
r.unverified_text_ratio     # 正文里没被核验的比例
r.not_cloud_verified        # 哪些没经过平台公共规则
r.boundary_conflicts        # 公私规则打架的地方
r.blocking                  # 该不该拦下

result.blocking             # 程序读这一个就够
result.status               # PASS / BLOCK / NEED_HUMAN
result.coverage_note        # 抽了 N 条断言、其中 M 条可核验
```

`result.blocking` 在 `risk_summary` 缺失时返回 **True**。响应被截断、或云端因故没给出
风险总结时，我们知道的是"不知道"——把"不知道"默认成 False，等于让一次坏掉的响应
替你说出"这份文档可以交"。

**`coverage_note` 要跟着结论一起转述。** "没发现问题"和"没有问题"之间隔着的就是
这句话：没被抽取到的正文属于没查过，它不会进 `annotations`，也不该被读成通过。

`boundary_conflicts` 非空时不会静默选一边：`status` 升级为 `NEED_HUMAN`、`blocking`
为真、评级不高于 C，等人拍板。

---

## 6. 拿到标注版和报告

```python
# 标注版 PDF：高亮直接画在原文件上
client.download_annotated(result.review_id, "annotated.pdf")

# 人能读的报告（Markdown）
client.download_review_report(result.review_id, "review.md")

# 一页纸的可交互报告（单文件 HTML）
client.download_review_card(result.review_id, "report.html")

# dest 不给就返回字节，不落盘
data = client.download_annotated(result.review_id)
```

标注版**只有 PDF 源文档做得出**（其余格式没有可画的版面），源文档不是 PDF 时
`download_annotated` 抛 `ConflictError`——那种情况请用 `annotations` 里的字符区间
自己渲染，或者直接交 HTML 那份，它不依赖版面。

三份的分工：

| | 读者 | 场合 |
|---|---|---|
| `download_review_card()` → HTML | 做决定的人 | 一页看完结论，要追下去就展开某一步；转发出去也不掉样式 |
| `download_review_report()` → Markdown | 要逐条排查的工程师 | 进 diff、进工单、进代码评审 |
| `download_annotated()` → PDF | 要签字留痕的人 | 高亮画在原件上，进合规归档 |

HTML 那份**单文件、不引 CDN**：另存下来双击就能看，断网也不掉样式——它被打开
的场合往往正是会议室里。

这几份都是原文件之外的**新产物**：你交给它的那份文档一个字节都没被动过。

### 翻历史

```python
for r in client.list_reviews(limit=20):
    print(r.review_id, r.status, r.grade.letter if r.grade else None)

full = client.get_review("rev_xxxx")      # 完整结果要按 id 取
```

> `list_reviews` 只回摘要：除 `review_id`、`status` 和 `grade.letter` 外都是空的。
> **别把列表里空的 `annotations` 读成"这次没问题"**——它只是没被返回。

---

## 7. 加你自己机构的规则

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

之后每次审阅自动生效。文档里写着 ROE 850% 时，`annotations` 里就会多出一条
`origin="local"`、`rule_id="my.roe_range"` 的标注，高亮落在那个数字上。

### 常用检查器

| validator | 干什么 | params |
|---|---|---|
| `field_range` | 数值必须在区间内 | `{"min": …, "max": …}` |
| `require_source_refs` | 必须有来源 | — |
| `forbid_forecast_basis` | 不许用预测口径 | `{"expected_basis": "reported"}` |
| `basis_disclosed` | 必须标注口径 | `{"allowed": [...]}` |
| `period_label_required` | 必须标注报告期 | — |
| `ratio_consistency` | 比率必须和分子分母对得上 | `{"numerator": …, "denominator": …}` |
| `value_matches_evidence` | 数值必须和证据一致 | `{"tolerance": 0.005}` |
| `unit_scale_sanity` | 单位与量级要合理 | `{"units": {...}, "ranges": {...}}` |
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

你的 P1 和平台 P3 在同一处要求相反时，**不会**静默选一边——冲突进
`risk_summary.boundary_conflicts`，整次审阅升级为 `NEED_HUMAN`，等人拍板。

### 管理规则

```python
client.list_boundaries()                    # 我的规则
client.delete_boundary("my.roe_range")      # 停用
client.public_boundaries()                  # 平台公共规则（只读）
```

---

## 8. 存你自己的内部知识

这些内容会参与审阅（帮助定位问题、生成修改建议），也方便团队共享。都绑定你的租户，
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
client.get_memory("mem_xxxx")
client.update_memory("mem_xxxx", body="改过的")
client.delete_memory("mem_xxxx")
# skill / wiki 同样是 list_ / get_ / update_ / delete_
```

> Wiki 存**人读的说明**，规则存**机器执行的检查**。两者用 `related_rules` 互相
> 引用，但不能互相替代——拿 Wiki 正文当规则跑，等于把审计依据交给自然语言解析。

---

## 9. 异步客户端

```python
from sagax_amadeus import AsyncSagaxAuditClient

async def main():
    async with AsyncSagaxAuditClient() as client:
        result = await client.review("report.pdf", evidence_ids=["ev_xxxx"])
        if result.blocking:
            await client.download_annotated(result.review_id, "annotated.pdf")
```

同步客户端上的方法这里都有一份对应的。

异步解决的是**一个进程同时审很多份**，不是"提交完就返回"——审阅本身仍然是一次
同步长请求，`await` 要等它跑完。没有任务队列，也就没有 id 和轮询这一套。

---

## 10. 错误处理

**按异常类型分支，不要按状态码数字。**

```python
from sagax_amadeus import (AuthenticationError, QuotaExceededError,
                           NotFoundError, ValidationError, RateLimitError,
                           ServerError, APIConnectionError, SagaxAuditError)

try:
    result = client.review("report.pdf", evidence_ids=["ev_xxxx"])
except QuotaExceededError:
    通知运营("配额用完了")
except ValidationError as e:
    print("文档空、格式不支持或已损坏:", e.message)
except (APIConnectionError, ServerError):
    稍后重试()
except SagaxAuditError as e:
    报障(e.request_id)          # 报障时把这个给我们，日志能直接定位
```

| 异常 | 状态 | 意思 |
|---|---|---|
| `ConfigurationError` | — | 文件不存在，或 `document` 既不是路径也不是字节 |
| `InsecureTransportError` | — | 地址写成了 `http://` |
| `AuthenticationError` | 401 | Key 没设 / 错了 / 已吊销 |
| `QuotaExceededError` | 402 | 配额用尽 |
| `PermissionDeniedError` | 403 | 订阅停用 |
| `NotFoundError` | 404 | 不存在，**或者不属于你** |
| `ConflictError` | 409 | 源文档不是 PDF，出不了标注版 |
| `PayloadTooLargeError` | 413 | 文档或证据太大 |
| `ValidationError` | 422 | 文档为空、格式不支持或已损坏 |
| `RateLimitError` | 429 | 被限流 |
| `ServerError` | 5xx | 服务端出错 |
| `APITimeoutError` | — | 请求超时（云端可能仍在跑完并计费） |

> 404 同时表示"不属于你"是有意的。返回 403 等于告诉对方"这个 id 确实存在，
> 只是不归你"——那本身就是一次信息泄露。

**注意 `BLOCK` 不是异常。** 拦下不发是一次正常的、成功的审阅结论，走返回值
（`result.blocking`）判断，不要指望它抛出来。

**关于重试**：GET/DELETE 这类幂等请求，遇到 429/5xx/断网时 SDK 会自动重试并退避。
`review` **不会**——重发一次就是多审一遍、多扣一次配额。

---

## 11. 配置项速查

```python
client = SagaxAuditClient(
    base_url="https://audit.example.com",
    api_key="sagax_sk_...",
    project_id="research",       # 同一租户下再分项目，互相隔离
    timeout=60,                  # 连接级 HTTP 超时（review 另有自己的 timeout）
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

用托管服务时，这些东西会经 HTTPS 传到服务端并存在你的租户空间里：待审文档、原始
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
sagax-amadeus status                                     # 连接与配额
sagax-amadeus review report.pdf --evidence ev.json \
                                --annotated out.pdf --report out.md
sagax-amadeus reviews list|get|annotated|report
sagax-amadeus evidence upload|list|get|download|delete
sagax-amadeus memory|skill|wiki  add|search|list|get|update|delete
sagax-amadeus boundary add-rule|list|public|disable
sagax-amadeus mcp                                        # 启动 MCP Server
```

`review` 在有 `blocking` 风险时**退出码为 1**，所以 CI 里一句就能当门禁用，不必再
解析 JSON：

```bash
sagax-amadeus review report.pdf --evidence ev.json || echo "这份不能发"
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
    "params": {"expected_basis": "reported"},
})

# 每次交付前：上传证据 → 审文档
ev = client.upload_evidence_items([
    EvidenceItem(field="revenue", value=85.6, unit="亿元", period="2025A",
                 source="2025年报", source_ref="ann:2025A#p12"),
    EvidenceItem(field="net_profit", value=10.2, unit="亿元", period="2025A",
                 source="2025年报", source_ref="ann:2025A#p13"),
])

result = client.review("2025年报点评.pdf", evidence_ids=[ev.evidence_id])

print(f"{result.grade.letter} —— {result.grade.rationale}")
print(result.coverage_note)          # 连同结论一起转述，别只说"没发现问题"

if result.blocking:
    for severity, notes in result.by_severity().items():
        for n in notes:
            print(f"[{severity}] {n.quote!r} · {n.comment}")
            print(f"         怎么改：{n.suggested_fix}")
    client.download_annotated(result.review_id, "标注版.pdf")
    client.download_review_report(result.review_id, "为什么不能发.md")
else:
    发布(我的报告)

client.close()
```

改不改、怎么改，是你的决定——**这份 `2025年报点评.pdf` 从头到尾没有被修改过**。
