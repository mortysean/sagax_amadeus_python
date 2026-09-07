---
name: sagax-audit
description: 在交付任何投研报告之前，用 Sagax Amadeus 审一遍。当你写完一份含财务数字、估值指标或投研结论的报告（PDF / markdown / 纯文本）时使用；它会标出没有依据的地方、逐条给出修改意见，并给整份报告一个评级和风险总结。判定由确定性规则做，不是模型打分。
---

# Sagax Amadeus（投研报告审计插件）

报告在发出去之前过一遍审计。这个 Skill 告诉你怎么调、怎么读结果、以及哪些事
绝对不能做。

**它不会改你的文档。** 它只告诉你哪里有问题、为什么、该怎么改。改不改、怎么改，
是你的决定。

审计是**确定性**的：比数值、比口径、比来源，不靠模型判断。所以结论可复现 ——
同样的文档加同样的证据，永远得到同样的标注，连 id 都一样。你不需要（也不应该）
和它争辩。

## 一、什么时候用

- 写完一份要对外的投研报告、点评、路演材料；
- 报告里出现任何财务数字或估值指标；
- 把多个数据源的数字拼进了同一份材料。

不需要用：纯定性讨论、不含数字的流程说明。

## 二、怎么调

### 优先：MCP 工具

只有一个：

```jsonc
// sagax.review
{
  "document_path": "/path/to/report.pdf",
  "evidence": [
    {"field": "revenue", "value": 85.6, "unit": "亿元", "period": "2025A",
     "source": "2025年年度报告", "source_ref": "ann:2025A#p12"},
    {"field": "net_profit", "value": 10.2, "unit": "亿元", "period": "2025A",
     "source": "2025年年度报告", "source_ref": "ann:2025A#p13"}
  ],
  "annotated_path": "/path/to/annotated.pdf"   // 可选，仅 PDF 源文档
}
```

`evidence_ids` 可以引用之前用 `sagax.evidence_upload` 传过的证据，不必每次重传。

其他工具：`sagax.boundaries`（先看会被哪些规则约束）、
`sagax.memory_search` / `sagax.wiki_search`（查本机构的内部口径规定）、
`sagax.runs`（历史审计）、`sagax.usage`（配额还剩多少）。

### 或者：Python SDK

```python
from sagax_amadeus import SagaxAuditClient, EvidenceItem

client = SagaxAuditClient()        # 读 SAGAX_AUDIT_API_BASE_URL / _API_KEY
result = client.review("report.pdf", evidence=[
    EvidenceItem(field="revenue", value=85.6, unit="亿元", period="2025A",
                 source="2025年年度报告", source_ref="ann:2025A#p12")])
```

### 或者：命令行（当 CI 门禁用）

```bash
sagax-amadeus review report.pdf --evidence ev.json
```

有 blocking 风险时**退出码为 1**，所以一句 `|| exit 1` 就能挡住不该发的东西。

## 三、**证据是判定的依据**

这是最容易搞错的一点：**不给证据，报告里每一个数字都算无源**。

这不是缺陷。P0 规则 `evidence_required` 要求任何对外数值必须带可回溯来源，而 P0
不可关闭。你随文档交上来的证据，**就是**这些数字的来源 —— 值对得上的算有源，
对不上的就是无源。

所以：**先把你查到的数据整理成 evidence，再提交审计**。省掉这一步，你只会拿到
一份「满篇都是问题」的报告，那不是审计在为难你，是它在如实告诉你这些数字目前
谁也证明不了。

每条证据要给：

| 字段 | 含义 | 不给会怎样 |
|---|---|---|
| `field` | 规范字段名 | 无法与断言对应 |
| `value` / `unit` | 数值与单位 | 无法做量级检查 |
| `period` | 报告期（2025A / 2025Q3） | 触发口径规则 |
| `source` / `source_ref` | 可回溯来源 | 这条证据背书不了任何数字 |

## 四、怎么读结果

### 先读这一个值

```
result.blocking     # True = 不得交付
```

宿主要做的决定只有这一个。其余是给人看的。

### 评级

| 等级 | 含义 |
|---|---|
| `A` | 查遍了且都对 |
| `B` | 无严重问题，但有 MEDIUM 以下问题、或有没查过的内容 |
| `C` | 有 HIGH 及以上问题，或证据覆盖率 < 90%，或公私规则冲突待人工裁决 |
| `D` | 触发平台 P0（无来源数值 / 模型估值冒充数据源），**不得交付** |

`grade.deductions` 逐条说明为什么不是上一级，每条都指得回具体问题和位置。

### 标注

`result.annotations` 里每一条：

- `spans` —— 位置。PDF 有 `page` + `bbox`，其余格式只有 `char_start` / `char_end`。
  **空的 `spans` 不是错误**：字段级问题定位不到正文，当文档级批注看，别丢掉。
- `comment` —— 为什么被标出来
- `suggested_fix` —— 该往哪个方向改
- `rule_id` —— 哪条规则判的。**每条标注都有**；没有 rule_id 的标注不会出现，
  因为那等于「模型觉得这里不对」，而这个产品不做这件事。

### 覆盖度声明 —— 必须读，也必须转述

```
result.coverage_note
→ "本次自 pdf 文档抽取 21 条断言，其中 21 条可核验（带版面定位）。
   未被抽取的正文属于「未验证」，不等于通过。"
```

**「没发现问题」和「没有问题」是两回事**，两者之间隔着的就是这句话。向用户汇报
审计结果时必须带上它，不能只说「审计通过」。

## 五、绝对禁止

1. 把 API Key 放进任何请求体、Trace、日志或输出。
2. 没有证据就补一个「看起来合理」的数字 —— 这会直接违反 P0。
3. 把模型推测标成数据来源。
4. 用预测/一致预期盈利计算、却标注为历史口径的 PE。
5. 公私规则冲突（`NEED_HUMAN`）时自行选一边 —— 交给人裁决。
6. 拿到 `blocking: true` 还继续交付。
7. 只汇报「审计通过」而不提覆盖度 —— 见上一节。
8. 把本机构私有规则的**原文**塞进提交内容交给公共规则引擎 —— 它按匿名 id
   参与判定，塞原文既没用也不该做。
9. 记录模型隐藏思维链（Trace 只接受结构化事件）。

## 六、拿不到审计结论时

SDK 是**薄 HTTP 客户端**：它不在本地跑审计，远程失败也不会回落到本地跑一遍。
拿不到云端就没有审计结论 —— 这时**如实说明**，不要自己判一个。

## 七、产物在哪

```python
client.download_annotated(review_id, "annotated.pdf")       # 高亮批注版（仅 PDF）
client.download_review_report(review_id, "report.md")       # 人读的报告
```

报告会写明：抽取了多少条断言、哪些被哪条规则判定、哪些**没被验证**、以及剩余
风险。要向用户说明审计过程时，引用这份报告，不要凭记忆复述。

非 PDF 源文档没有版面坐标，`download_annotated` 会返回 409 —— 用 annotations
里的字符区间自行渲染。
