# MCP 接入教程

让 AI Agent（Claude Code、Cursor、以及任何支持 MCP 的客户端）**自己**调用审计，
而不用你写代码。

---

## 这解决什么问题

不接 MCP 的时候，流程是：

```
Agent 写完稿 → 你（人）把文件找出来 → 调 SDK 审一遍 → 把问题贴回去 → Agent 改
```

接了 MCP 之后：

```
Agent 写完稿 → 自己提交审阅 → 自己拿到高亮和修改意见 → 自己决定改不改 → 再审
```

整个来回不需要你插手。而且 Agent 拿到的不是"你这里可能有问题"，是
**"第 3 页这句话里的 92.0 亿元，证据里是 85.6 亿元，差 7.48%"**——带位置、带依据、
带一句可执行的修改建议。

**审阅不会改 Agent 的稿子。** 它返回的是意见，改不改、怎么改由 Agent 自己决定。
一个会偷偷重写你文档的审计工具，你没法信任它的结论。

---

## 第 1 步：装

```bash
pip install sagax-amadeus
```

MCP Server 就在这个包里，不用另外装东西。

## 第 2 步：配

在你的 MCP 配置文件里加一段。**注意 `env` 里那三行是必须的**——
MCP Server 是被客户端拉起来的独立进程，读不到你 shell 里的环境变量。

```jsonc
{
  "mcpServers": {
    "sagax-audit": {
      "command": "python3",
      "args": ["-m", "sagax_amadeus.mcp_server"],
      "env": {
        "SAGAX_AUDIT_API_BASE_URL": "https://audit.example.com",
        "SAGAX_AUDIT_API_KEY": "sagax_sk_你的key",
        "SAGAX_AUDIT_CA_BUNDLE": "/绝对路径/sagax-ca.crt"
      }
    }
  }
}
```

配置文件放哪：

| 客户端 | 位置 |
|---|---|
| Claude Code（项目级） | 项目根目录 `.mcp.json` |
| Claude Code（用户级） | `claude mcp add` 或 `~/.claude.json` |
| Cursor | `~/.cursor/mcp.json` 或项目内 `.cursor/mcp.json` |
| 其他 | 查它自己的 MCP 文档，字段名都一样 |

> `SAGAX_AUDIT_CA_BUNDLE` 要写**绝对路径**——MCP Server 的工作目录不一定是你以为的那个。
> 以后服务端换成正式域名证书，这一行就能删掉。

## 第 3 步：验

重启客户端，然后问 Agent：

> 列一下 sagax-audit 有哪些工具

应该能看到 13 个 `sagax.*` 工具。看不到就查客户端的 MCP 日志——
八成是 `python3` 不在 PATH 里，或者包没装在同一个 Python 环境。

手动验一把（不依赖任何客户端）：

```bash
export SAGAX_AUDIT_API_BASE_URL=https://audit.example.com
export SAGAX_AUDIT_API_KEY=sagax_sk_你的key
export SAGAX_AUDIT_CA_BUNDLE=$PWD/sagax-ca.crt

echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | python3 -m sagax_amadeus.mcp_server
```

能刷出一大串工具定义就说明通了。

---

## 有哪些工具

一共 13 个，审计只占其中**一个**。

### 审阅

| 工具 | 干什么 |
|---|---|
| `sagax.review` | 审一份文档：高亮 + comment + 修改建议 + 评级 + 风险总结 |

入参：

| 参数 | 说明 |
|---|---|
| `document_path` | **必填**，待审文档路径（PDF / markdown / 纯文本）。PDF 必须带文字层 |
| `evidence` | 结构化证据，**可以不给** —— 服务端会为点名的财务指标自己取真值 |
| `evidence_ids` | 已上传证据的 id |
| `code` | 证券代码（如 `600519.SH`）。不给则从正文里认 |
| `auto_evidence` | 是否允许服务端自动取证，默认 true |
| `annotated_path` | 把标注版 PDF 存到这个路径（仅 PDF 源文档） |
| `card_path` | 把**一页可交互报告**存到这个路径（单文件 HTML，任何源格式都有） |
| `card_pdf_path` | 同一页纸的 PDF 版存到这个路径（存档转发用） |

返回：`review_id`、`coverage_note`、`grade`、`grade_rationale`、`blocking`、
`annotations`、`risk_summary`、`annotated_saved_to`、`card_saved_to`、
`card_pdf_saved_to`。

**哪个字段是 `null` 就不要附那条链接。** 附属产物取不到时它们回 `null`，
不会让整次审阅失败 —— 编一个 URL 出来，用户点开是 404，而那正是这个工具要防的。

要把结论**交给人看**就传 `card_path`：一页纸讲清结论、评分、站不住的数字和整条
执行链，比 Agent 把 `annotations` 复述一遍更完整，也不会在转述里丢掉「哪些没查」。

`coverage_note` 被刻意摆在返回值的前面：调用方最容易犯的错是把"没发现问题"读成
"没有问题"，而两者之间隔着的就是这句话。

### 证据

| 工具 | 干什么 |
|---|---|
| `sagax.evidence_upload` | 上传结构化证据，返回 evidence_id 供反复引用 |

### 你的私有知识

| 工具 | 干什么 |
|---|---|
| `sagax.memory_add` / `sagax.memory_search` | 内部案例、经验教训 |
| `sagax.skill_add` / `sagax.skill_search` | 内部检查清单 |
| `sagax.wiki_write` / `sagax.wiki_search` | 内部制度说明 |
| `sagax.boundaries` | 当前生效的私有规则 |

### 平台公共内容与账务

| 工具 | 干什么 |
|---|---|
| `sagax.skill_official` | 平台公共 Skill |
| `sagax.wiki_public` | 平台公共 LLM Wiki |
| `sagax.runs` | 历史审计任务 |
| `sagax.usage` | 配额还剩多少 |

> 每个工具都是一次云端 API 调用的薄封装。MCP 这一层**不重复实现审计逻辑**——
> 否则就会出现两套结论。

> 以前配的 `sagax.audit_run` / `audit_check` / `audit_status` / `audit_result` 已经
> 下线。调它们不会得到一句含糊的"未知工具"，而是直接被告知改用 `sagax.review`——
> 否则 Agent 只会换个参数再试一遍，然后再失败一遍。

---

## 怎么让 Agent 用起来

光配上工具，Agent 不一定知道什么时候该调。给它一条明确指令。

### 放进项目的 `CLAUDE.md` / `.cursorrules` / 系统提示

```markdown
## 投研结论交付前必须审计

任何对外交付的文档（研报、点评、尽调备忘），定稿后交付前必须调用 `sagax.review`，
把文件路径给它。证据可以不给：报告里点了名的财务指标，服务端会自己取真值。
如果你已经记录了取数结果，放进 `evidence` 更强。

看返回：
- blocking == true   → 不要交付。逐条读 annotations，自己判断该怎么改。
- grade 是 D / C     → 交付前必须处理掉扣分项。
- coverage_note      → 连同结论一起转述给用户。"没发现问题"不等于"没有问题"，
                       没被抽取到的部分是没查过。
- risk_summary.boundary_conflicts 非空 → 规则冲突了，不要自己选一边，交给人。

审阅**不会**修改你的文档。annotations 里的 suggested_fix 是建议，
改哪几处、怎么改由你决定，改完可以再审一次。

改完再审最多两轮。两轮还不干净就如实报告剩余风险，别接着试。
```

### 或者直接用现成的 Skill

仓库里的 [`skills/sagax-audit/SKILL.md`](../skills/SKILL.md)（同步到公开仓）
就是写给 Agent 看的完整版：什么时候必须调、怎么给证据、怎么读标注和评级、
以及若干条绝对禁止。支持 Skill 的客户端可以直接加载它。

---

## 一次实际对话长什么样

```
你：帮我把这家公司 2025 年的业绩点评写出来，数据在 annual_report.pdf 里

Agent：（读文件，写出 点评.md）

      稿子写完了，交付前我过一遍审计……
      → 调用 sagax.review
        document_path: "点评.md"
        evidence: [ {revenue 85.6 亿元 / 2025A / 年报p12},
                    {net_profit 10.2 亿元 / 2025A / 年报p13} ]

      返回 blocking: true，评级 C：
        coverage_note: 抽取 6 条断言，其中 5 条可核验
        · [high] "归母净利润 12.5 亿元" —— 证据中 2025A 为 10.2 亿元
                 （相对差 22.5%，超过 0.5% 容差）
                 建议：改为 10.2 亿元，或注明这是 2026 年预测值
        · [medium] "PE 约 30 倍" —— 未标注口径
                 建议：补上 trailing / forward

      我核了一下，12.5 亿是券商对 2026 年的预测值，我引错了列。
      两处我都改：净利润改成 10.2 亿元，PE 补上「trailing」。

      （自己改 点评.md，重新调 sagax.review）

      → blocking: false，评级 A
        coverage_note: 抽取 6 条断言，6 条均可核验

      定稿了。另外提醒一句：文中"行业景气度回升"这类判断句不在可核验范围内，
      审计没有覆盖它。
```

三个地方值得注意：

- Agent 拿到的是**可定位、可执行**的反馈，不是一句"你再检查检查"。
- **改文档的是 Agent，不是审计。** 工具给的是意见，落笔的是稿子的作者。
- 最后那句提醒来自 `coverage_note`。没查过的部分被如实说了出来，而不是默默算通过。

---

## 常见问题

**Q：Agent 说找不到工具？**
先手动跑一遍第 3 步的 `tools/list`。能跑通说明是客户端配置的问题——
多半是 `command` 里的 `python3` 和你装包的 Python 不是同一个。用绝对路径：

```jsonc
"command": "/usr/bin/python3"
// 或者虚拟环境里的：
"command": "/path/to/venv/bin/python3"
```

**Q：报 `CERTIFICATE_VERIFY_FAILED`？**
`SAGAX_AUDIT_CA_BUNDLE` 没配、或者写的是相对路径。改成绝对路径。

**Q：报 401？**
`SAGAX_AUDIT_API_KEY` 没写进 `env`。MCP Server 是独立进程，
**读不到你 shell 里 export 的变量**，必须写在配置文件的 `env` 里。

**Q：审计太慢，Agent 等超时？**
`sagax.review` 默认等最多 300 秒，而且没有异步版本可以换——审阅是一次同步长请求，
云端把解析、抽取、校验、评级跑完才回响应，没有队列也就没有任务 id。文档太大就先
拆开审。注意超时是客户端掐断连接，**云端照样跑完、照样计费**。

**Q：为什么每次都是 D？**
先读 `coverage_note`。常见原因：报告里没写带交易所后缀的证券代码、口径不唯一
（裸写「净利润」）、推不出报告期 —— 这三条服务端都不猜，没查到的部分不算通过。
也可以显式传 `code`，或自己给 `evidence`。P0 仍然关不掉。

**Q：两次审同一份文档，结果会不会不一样？**
不会。判定由确定性校验器做，不是模型打分；三个 id 全部内容寻址，
`annotations` 逐字节一致，可以直接 diff。

**Q：以前配的是 `amadeus.*` 工具名？**
前缀继续能用，不用改。新名字是 `sagax.*`，`tools/list` 只列新名字。
但 `audit_*` 那几个工具本身已经下线，见上面的工具表。

**Q：能限制 Agent 只用部分工具吗？**
可以，在客户端侧配工具白名单（各家做法不同，查客户端文档）。
比如只放开 `sagax.review`，不给它写 Memory / Skill 的权限。

---

## 想直接在命令行里用

不走 MCP 也行：

```bash
sagax-amadeus mcp                     # 启动 MCP Server（stdio）
sagax-amadeus review 点评.pdf --evidence ev.json --annotated 标注版.pdf
```

`review` 有 `blocking` 风险时退出码为 1，CI 里可以直接当门禁。
CLI 的完整命令见 [SDK 使用教程](sdk-guide.md#11-配置项速查)。
