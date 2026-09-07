# 快速开始：让你的投研智能体输出带批注的报告

目标：你的智能体写完一份报告，Sagax 把里面**没有依据的数字**标出来，回一份带
高亮批注的 PDF。

全过程三步：**拿到地址和 Key → 装客户端 → 在智能体里加几行**。

---

## 第 1 步：拿到服务地址和 API Key

审计**引擎跑在服务端**，你这边只装一个客户端。引擎按订阅或私有化授权，
所以第一步取决于你是哪种：

**A. 用托管服务** —— 找我们要两样东西，装引擎的事你不用管：

```
服务地址   https://audit.example.com
API Key    sagax_sk_…            （只发一次，服务端只存哈希）
```

**B. 私有化部署** —— 数据一步都不能出机房时走这条。我们交付的是**容器镜像**
（引擎以字节码交付，不含源码），起在你自己的网络里：

```bash
docker run -d -p 4600:4600 -v sagax-data:/data \
  -e SAGAX_AUDIT_ADMIN_TOKEN=$(openssl rand -hex 24) \
  sagax-cloud:1.0.0
```

起来之后开租户拿 Key：

```bash
curl -X POST http://127.0.0.1:4600/v1/admin/tenants \
  -H "X-Sagax-Admin-Token: 上面那个 token" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"myfirm","user_id":"me","plan":"pro"}'
```

> **镜像本身不带 TLS。** 内网用没问题；要跨网段，前面必须放一层 TLS 终止
> （Nginx / LB），否则 API Key 和你的原始数据是明文过网的。

两种形态下**客户端代码一个字都不用改**，只换 `base_url`。

## 第 2 步：装客户端

```bash
pip install sagax-amadeus       # 零依赖，只用标准库，Python 3.10+
```

## 第 3 步：在你的智能体里加几行

```python
from sagax_amadeus import SagaxAuditClient, EvidenceItem

client = SagaxAuditClient(base_url="https://audit.example.com",
                          api_key="sagax_sk_…")

# 你的智能体照常写报告，写完之后：
result = client.review(
    "report.pdf",
    evidence=[
        # ↓ 这里放你的智能体**查数据时拿到的真实数值**（见下一节）
        EvidenceItem(field="revenue", value=85.6, unit="亿元", period="2025A",
                     source="2025年年度报告", source_ref="ann:2025A#p12"),
    ])

if result.blocking:
    print("这份不能直接发，评级", result.grade.letter)
    for note in result.annotations:
        print(" ", note.spans[0].quote, "→", note.comment)

client.download_annotated(result.review_id, "annotated.pdf")   # 带高亮的 PDF
```

---

## 最关键的一步：证据从哪来

**这是唯一容易踩空的地方。**

Sagax 判断一个数字有没有依据，靠的是你交上来的 `evidence`。**不给证据，报告里
每一个数字都算无源** —— 必然触发平台红线，评级必然是 `D`。那不是审计在为难你，
是它在如实说：这些数字目前谁也证明不了。

好消息是：**你的智能体本来就有这些数据。** 它写报告之前查过接口、拉过年报、
调过行情 —— 那些返回值就是证据。顺手收集起来就行：

```python
evidence = []

def 查财务指标(code, field, period):
    value = 你的数据源.query(code, field, period)      # 你原来就有的调用
    evidence.append(EvidenceItem(
        field=field, value=value, unit="亿元", period=period,
        source="东方财富",                              # 从哪个源来的
        source_ref=f"{code}:{field}:{period}"))        # 你自己能回查的定位串
    return value

# …智能体用这些值写报告…

result = client.review("report.pdf", evidence=evidence)
```

对得上证据的数字算有源，对不上的被标出来 —— **模型顺手编的那个数字，恰恰是
查不到证据的那个**。这就是它能抓幻觉的原理。

---

## 你会拿到什么

| | |
|---|---|
| `result.blocking` | 一个布尔值：能不能发。程序读这一个就够 |
| `result.grade.letter` | `A` 查遍了且都对 · `B` 有轻微问题或没查过的 · `C` 有严重问题 · `D` 触发红线 |
| `result.annotations` | 每处高亮：页码 + 坐标 + 原文 + 为什么 + 该怎么改 |
| `result.coverage_note` | 抽了多少条断言、核了多少、**没核多少** |
| `download_annotated()` | 带高亮批注的 PDF（仅 PDF 源文档） |

`coverage_note` 请一定读。**「没发现问题」和「没有问题」是两回事** —— 抽不到的
正文算未验证，不等于通过。纯定性表述（「行业格局趋于缓和」）目前抽不出来，
也就没法核验。

## 当 CI 门禁用

```bash
sagax-amadeus review report.pdf --evidence ev.json || exit 1
```

有 blocking 风险时退出码是 **1**，一句就能挡住不该发的东西。

---

## 常见问题

**Q：为什么每次跑都是 `D`？**
八成是没给证据，或者给的值和正文里的数字对不上。看 `grade.deductions` 里有没有
`p0_violation`。

**Q：`download_annotated` 报 409？**
源文档不是 PDF。markdown / 纯文本没有版面坐标，用 `annotations` 里的
`char_start` / `char_end` 自己渲染。

**Q：报错说 PDF 解析不可用？**
服务端缺 PDF 解析组件（pdfminer.six + pypdf）。托管服务上不会出现；私有化部署
请用带 `[pdf]` 的镜像标签，或联系我们。**客户端不需要装任何东西。**

**Q：同样的报告跑两次，结果会变吗？**
不会，连标注的 id 都一样 —— 三个 id 都是按内容算的哈希。可以直接 diff 两次结果。

**Q：它会改我的报告吗？**
不会。只回意见，改不改是你的智能体自己决定。

**Q：报 `InsecureTransportError`？**
`base_url` 是 `http://` 且不是本机地址。要么放 TLS，要么本机调试时用
`127.0.0.1`（在白名单里）。

**Q：想加我机构自己的口径和风控阈值？**
可以。私有规则存在你的租户空间，**原文不出租户**，只以匿名 id 参与公共侧判定 ——
见 [SDK 教程](sdk-guide.md)。

## 接下来

- 完整 API → [SDK 使用教程](sdk-guide.md)
- 让 Agent 自己调用 → [MCP 接入教程](mcp-guide.md)
