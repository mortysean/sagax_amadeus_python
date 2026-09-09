# 快速开始：给你的投研报告加一道审计

你的智能体写完一份 PDF，Sagax Amadeus 在**它输出的那份 PDF 上**标出没有依据的
数字 —— 高亮 + 批注 + 一个评级。它不生成文档，也不改你的文档。

**最少三步，第三步只有一行代码。**

---

## 第 1 步：拿到服务地址和 API Key

审计**引擎跑在服务端**，你这边只装一个客户端。引擎按订阅或私有化授权：

**A. 用托管服务** —— 找我们要两样东西：

```
服务地址   https://audit.example.com
API Key    sagax_sk_…            （只发一次，服务端只存哈希）
```

**B. 私有化部署** —— 数据一步都不能出机房时走这条。交付的是容器镜像：

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

> **镜像本身不带 TLS。** 内网用没问题；要跨网段，前面必须放一层 TLS 终止，
> 否则 API Key 和你的原始数据是明文过网的。

两种形态下**客户端代码一个字都不用改**，只换 `base_url`。

## 第 2 步：装客户端

```bash
pip install sagax-amadeus       # 零依赖，只用标准库，Python 3.10+
```

```bash
export SAGAX_AUDIT_API_BASE_URL=https://audit.example.com
export SAGAX_AUDIT_API_KEY=sagax_sk_…
```

## 第 3 步：在你的智能体里加一行

```python
from sagax_amadeus import SagaxAuditClient

client = SagaxAuditClient()          # 读上面两个环境变量

# 你的智能体照常输出 report.pdf，然后：
result = client.review("report.pdf")

if result.blocking:
    print("这份不能直接发，评级", result.grade.letter)

client.download_annotated(result.review_id, "annotated.pdf")   # 带高亮的 PDF
client.download_review_card(result.review_id, "report.html")   # 一页纸，双击就能看
```

**就这些。** 不需要你准备证据 —— 报告里点了名的财务指标，服务端自己去数据源
取真值来对。

---

## 你会拿到什么

| | |
|---|---|
| `result.blocking` | 一个布尔值：能不能发。程序读这一个就够 |
| `result.grade.letter` | `A` 查遍了且都对 · `B` 有轻微问题 · `C` 有严重问题 · `D` 触发红线 |
| `result.annotations` | 每处高亮：页码 + 坐标 + 原文 + 为什么 + 该怎么改 |
| `result.coverage_note` | 抽了多少条断言、核了多少、**没核多少** |
| `download_annotated()` | **在你的 PDF 上**画好高亮的副本，首页附一条综合评价 |
| `download_review_card()` | 一页纸的**可交互报告**（单文件 HTML）：结论、评分、站不住的数字、整条执行链，每步可展开。任何源格式都有 |
| `download_review_card_pdf()` | 同一页纸的 **PDF 版**，供存档、签字、进合规留痕 |

`coverage_note` 请一定读。**「没发现问题」和「没有问题」是两回事** —— 抽不到的
正文算未验证，不等于通过。

## 服务端替你查了什么

报告里写「营业收入 1688.4 亿元」，服务端会：认出证券代码 → 认出这是营业收入 →
认出是 2025 年报 → 去数据源取真值 → 和你写的数比。对不上就标出来，**并告诉你
真值是多少**：

```
🔴 3500.0 亿元  [数字算错]
   数据源显示总资产是 3,038.35亿元（2025年，来自 sagax-data）。
   按这个数改，或说明你用的是别的口径。
```

三条它**不猜**的规矩，因为猜错会把一个正确的数字标成错的：

- **认不出证券代码就不查。** 报告里写明 `600519.SH` 这类带交易所后缀的代码即可；
  也可以显式传 `client.review("report.pdf", code="600519.SH")`
- **口径不唯一的词不查。** 裸写「净利润」不查（含/不含少数股东损益是两个数），
  写「归母净利润」才查
- **推不出报告期不查。** 正文里写明「2025年年报」「2025Q3」这类

没查的部分**不会被算成通过**，它们进 `coverage_note` 的「未核验」。

## 想自己提供证据也可以

你的智能体如果本来就记录了取数结果，给出来更强 —— 那是你声明「这个数来自
这里」，比服务端事后独立查更贴近你实际用的口径。两者会合并：

```python
from sagax_amadeus import EvidenceItem

result = client.review("report.pdf", evidence=[
    EvidenceItem(field="revenue", value=1688.4, unit="亿元", period="2025A",
                 source="2025年年度报告", source_ref="ann:2025A#p12")])
```

## 当 CI 门禁用

```bash
sagax-amadeus review report.pdf || exit 1
```

有 blocking 风险时退出码是 **1**，一句就能挡住不该发的东西。

---

## 常见问题

**Q：报错说「没有可提取的文字」？**
你的 PDF 是图片型的（扫描件，或整页渲染成图）。审计需要文字层才能定位到具体
的字。用程序直接导出的 PDF，或先做 OCR。

**这一条会明确报错而不是给你一个「通过」** —— 一个字都没读到却回 A 级，是比
不能用更糟的事。

**Q：为什么评级是 `D`？**
看 `result.annotations`。要么是数字和数据源对不上，要么是那个数既没有依据、
服务端也查不到。

**Q：`download_annotated` 报 409？**
源文档不是 PDF。markdown / 纯文本没有版面坐标，用 `annotations` 里的
`char_start` / `char_end` 自己渲染 —— 或者直接用 `download_review_card()`，
它不依赖版面，任何源格式都有。

**Q：要把结论给不看代码的人，交哪一份？**
`download_review_card()` 的 HTML。它是单文件、不引 CDN，存下来双击就开，
断网和转发都不掉样式。要逐条排查用 Markdown 版（`download_review_report()`），
要进合规留痕用 `download_annotated()` 的 PDF。

**Q：同样的报告跑两次，结果会变吗？**
不会，连标注的 id 都一样 —— 三个 id 都是按内容算的哈希。可以直接 diff 两次结果。

服务端自动取数发生在**判定之前**，取到的值落成快照；判定只看快照。所以「同一份
快照重跑结果一致」这条承诺不受影响。

**Q：它会改我的报告吗？**
不会。只回意见，改不改是你的智能体自己决定。

**Q：报 `InsecureTransportError`？**
`base_url` 是 `http://` 且不是本机地址。要么放 TLS，要么本机调试时用 `127.0.0.1`。

**Q：想加我机构自己的口径和风控阈值？**
可以。私有规则存在你的租户空间，**原文不出租户** —— 见 [SDK 教程](sdk-guide.md)。

## 接下来

- 完整 API → [SDK 使用教程](sdk-guide.md)
- 让 Agent 自己调用 → [MCP 接入教程](mcp-guide.md)
- 给 Agent 读的行为准则 → [`skills/sagax-audit/SKILL.md`](../skills/SKILL.md)
