# Sagax Amadeus

**投研报告的审计插件。**你的投研智能体产出一份 PDF 或 markdown 报告，
Sagax 接过去，把**没有依据的数字标出来**，逐条说明为什么，并给整份报告一个
评级和一份风险总结。

```text
你的投研智能体 ──产出 report.pdf──▶ Sagax Amadeus
                                        │
                          高亮      哪几个字没有依据（页码 + 坐标 + 字符区间）
                          comment   为什么被标出来、该怎么改
                          评级      A / B / C / D，每一级都能逐条解释
                          风险总结  还有什么没查过、能不能发
```

```bash
pip install sagax-amadeus
```

零依赖，只用标准库，Python 3.10+。Apache-2.0。

---

## 问题

大模型写投研报告，数字看起来都很像样 —— 但"像样"和"对"是两回事。真正难抓的
不是抄错小数点，是**每个数字单看都对、组合起来无意义**：

```
结论：PE = 18.5x（股价 92.5 元 / EPS 5.0 元）
```

股价 92.5 是对的，EPS 5.0 也是对的 —— 但前者是 2025 年收盘价，后者是券商
2026 年**预测**值。两个正确的数字相除，得到一个没有意义的估值。

这类错误有三个共同点，决定了它必须被这样解决：

1. **问模型问不出来。** "你确定吗"只会换来一句更自信的"确定"。
2. **人工复核容易漏。** 每个数字都能对上出处，错在关系上，不在数值上。
3. **它是确定性的。** 口径是否匹配、数值是否等于证据、单位量级是否合理 ——
   这些都能**算**出来，不需要判断力。

所以审计不该由第二个模型来做。**Sagax Amadeus 用代码去比，不用模型去猜**：
同样的输入永远得到同样的结论，可复现、可复核、可留档。

## 怎么用

```bash
pip install sagax-amadeus
```

```python
from sagax_amadeus import SagaxAuditClient

client = SagaxAuditClient(base_url="https://audit.example.com",
                          api_key="sagax_sk_…")

result = client.review("report.pdf")    # 路径或 bytes 都行；证据可以不给

print(result.grade.letter)      # A / B / C / D
print(result.blocking)          # True —— 读这一个值就够决定要不要拦
print(result.coverage_note)     # 抽了多少、核了多少、没核多少
for note in result.annotations:
    span = note.spans[0]
    print(f"p{span.page} {span.quote!r} · {note.rule_id}")
    print(f"   {note.comment}")
    print(f"   怎么改：{note.suggested_fix}")

client.download_annotated(result.review_id, "annotated.pdf")
```

**证据可以不给。** 报告里点了名的财务指标，服务端会自己去数据源取真值。
自己给证据仍然更强：那是你声明「这个数来自这里」，比事后独立查更贴近你实际
用的口径。两者会合并。取不到的部分**不会被当成通过**，它们进 `coverage_note`
的「未核验」。

### 你会拿到什么

| | |
|---|---|
| `result.annotations` | 每处高亮：页码 + bbox + 字符区间 + comment + 修改建议 |
| `result.grade` | `A`/`B`/`C`/`D`，`deductions` 逐条说明为什么不是上一级 |
| `result.risk_summary` | 结构化风险；`blocking` 是给程序读的单一布尔信号 |
| `result.coverage_note` | 抽了多少条断言、核了多少、没核多少 |
| `download_annotated()` | 带高亮批注的 PDF（仅 PDF 源文档） |

### 三件事决定了它和「再问一遍模型」不是一回事

**判定不靠模型。** 抽哪些数字可以交给模型（召回更高），但「这个数字对不对」
一律由代码判。同样的输入永远得到同样的结论 —— `annotations.json` 都是逐字节
一样的，三个 id 全部内容寻址，你可以直接 diff 两次审计结果。

**没查过 ≠ 通过。** `coverage_note` 会明写「本次抽取了 N 条断言」，抽不到的
部分算**未验证**，不会被算成通过。

**评级要能解释。** 每个等级都附扣分项，每条都指得回具体问题和具体位置。
不会出现「评级 A，底下列着一条扣分项」这种自相矛盾的交付物。

### 支持的格式

PDF、markdown、纯文本。PDF 会给出页码与坐标，其余格式只有字符区间 ——
`result.document["has_layout"]` 告诉你这次有没有版面定位，报告里也会写明。

PDF 解析是可选依赖（`pdfminer.six` MIT + `pypdf` BSD-3），装在**服务端**：

```bash
pip install "sagax-audit-cloud[pdf]"
```

SDK 本身仍然零依赖。

## 架构

审计规则拆成两半，**分开存储、分开执行、合并裁决**：

```
你的 Agent ──SDK──▶ 云端引擎
                      │
                      ├─ 检索你租户的私有 Memory / Skill / LLM Wiki
                      ├─ 生成私有边界        ← 你机构的口径与风控阈值
                      ├─ 取平台公共边界      ← PE/ROE 定义、数值对证、口径披露
                      ├─ 合并（冲突显式化，不静默择一）
                      ├─ 执行确定性校验器
                      └─ 裁决 ─┬─ PASS         可交付
                               ├─ BLOCK        不得交付，逐处给出标注
                               └─ NEED_HUMAN   公私规则冲突，等人拍板
```

**公共规则**由平台维护，订阅即得。两条 P0 任何租户都不能关闭：

| | |
|---|---|
| `p0.evidence_required` | 任何对外数值必须带可回溯来源 |
| `p0.no_model_estimate_as_source` | 模型只能组织已有证据，不能生产数据 |

其余是 P3 领域规则：数值必须与证据一致、PE/ROE 定义自洽、PE 必须标明盈利口径、
财务字段必须带报告期、单位量级合规、正文数值必须有支撑。

**私有规则**存在你的租户空间。它们的**原文不进公共规则引擎** —— 只传匿名 id 与
内容哈希。你机构的内部口径不会因为用了这个服务而离开你的租户。

六级优先级 `P0 平台不可覆盖 > P1 客户硬约束 > P2 租户组织 > P3 公共领域 >
P4 已验证学习 > P5 任务临时`。**冲突绝不静默择一**：私有规则要求 ROE 用加权平均
净资产、公共规则接受期末净资产，这不是"谁优先"能解决的，整次运行升级为
`NEED_HUMAN`。

## 两种部署形态

两种部署下客户端代码一个字不用改 —— 只换 `base_url`：

| | 引擎在哪 | 适合 |
|---|---|---|
| **托管服务** | 我们的云端 | 快速开始、不涉及敏感数据 |
| **本地/私有化** | 你自己的机器或机房 | 数据一步都不能出网 |

私有化交付的是**容器镜像**（引擎以字节码交付，不含源码），起在你的网络里：

```bash
docker run -d -p 4600:4600 -v sagax-data:/data sagax-cloud:1.0.0
export SAGAX_AUDIT_API_BASE_URL=http://127.0.0.1:4600
```

装在本机时 SDK 允许明文 HTTP（`127.0.0.1` / `localhost` 在白名单里）；一旦
`base_url` 指向非本机地址，客户端**强制要求 HTTPS**，明文直接拒绝。数据、私有
规则、审计留痕全部落在你挂的卷里，不出你的网络。

无论哪种形态，审计都在引擎里跑，SDK 里没有第二套。连不上引擎时它**报错**，
不会在本地跑个简化版给你一个"通过" —— 两套引擎会给出两套结论，而审计结论的
价值全部来自「只有一套」。

私有化部署联系我们取镜像与授权。

## 效果

45 例 frozen 测试集（一次性冻结评测，跑完不再调参）：

| 指标 | |
|---|---|
| 端到端任务通过率 | **88.9%** |
| 工具选择准确率 | 100% |
| 溯源覆盖率 | 100% |
| 数值准确率 | 97.2% |
| 来源保真度 | 98.9% |
| 无据断言率 | 1.1% |

溯源覆盖率 100% 与无据断言率 1.1% 是这套设计的直接结果：没有来源的数字过不了
P0，而 P0 不可关闭。

## 想直接在引擎里调

不经过 HTTP 时用引擎侧的同名函数，参数与返回同形：

```python
from sagax_audit_cloud.review import review_document

result = review_document(open("report.pdf", "rb").read(),
                         filename="report.pdf", evidence=[...])
```

## 让 Agent 自己调用

MCP Server 就在这个包里，不用另装：

```jsonc
{"mcpServers": {"sagax-audit": {
  "command": "python3", "args": ["-m", "sagax_amadeus.mcp_server"],
  "env": {"SAGAX_AUDIT_API_BASE_URL": "https://audit.example.com",
          "SAGAX_AUDIT_API_KEY": "sagax_sk_..."}}}}
```

只有一个工具：**`sagax.review`** —— 给它一份文档路径即可，它回高亮、comment、
评级和风险总结。Agent 拿到 `blocking: true` 就知道这份不能发，拿到 annotations
就知道该改哪几处。证据可以不给；没查过的部分不算通过。

改不改由 Agent 自己决定 —— 这个插件**不动你的文档**。

完整说明见 [MCP 接入教程](docs/mcp-guide.md)，
[`skills/SKILL.md`](skills/SKILL.md) 是写给 Agent 直接加载的版本。

## 命令行

```bash
sagax-amadeus status                      # 连接、租户与配额
sagax-amadeus review report.pdf --annotated out.pdf
sagax-amadeus reviews list|get|annotated|report
sagax-amadeus evidence upload|list|download|delete
sagax-amadeus memory|skill|wiki …         # 你的私有知识
sagax-amadeus mcp                         # 启动 MCP Server (stdio)
```

`review` 在有 `blocking` 风险时**退出码为 1**，所以 CI 里一句就能当门禁用：

```bash
sagax-amadeus review report.pdf || echo "不能发"
```

## 文档

| | |
|---|---|
| [快速开始](docs/quickstart.md) | 第一次用，5 分钟跑通 |
| [SDK 使用教程](docs/sdk-guide.md) | 完整 API、私有规则、错误处理、环境变量、自签证书 |
| [MCP 接入教程](docs/mcp-guide.md) | 让 AI Agent 自动调用 |

---

## 为什么客户端开源、引擎不开源

| | 授权 | 在哪跑 |
|---|---|---|
| **客户端**（本仓库） | Apache-2.0，开源 | 你的进程里 |
| **审计引擎** | 订阅 / 私有化授权 | 云端或你的机房 |

客户端开源是必要条件：这个包拿到你的 API Key、读到你的原始 Evidence、决定什么
被发到网上。**处在这个位置的代码，你有权逐行读它。**没有混淆、没有二进制、
没有遥测 —— 全部出站请求只经 `transport.py` 一处，目标只有你设的 `base_url`。

## 许可

Apache License 2.0，见 [`LICENSE`](LICENSE)。审计引擎不在本许可范围内，
按订阅或私有化协议单独授权。
