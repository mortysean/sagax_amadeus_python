# MCP 接入教程

让 AI Agent（Claude Code、Cursor、以及任何支持 MCP 的客户端）**自己**调用审计，
而不用你写代码。

---

## 这解决什么问题

不接 MCP 的时候，流程是：

```
Agent 写出结论 → 你（人）复制出来 → 调 SDK 审一遍 → 把问题贴回去 → Agent 改
```

接了 MCP 之后：

```
Agent 写出结论 → 自己调审计 → 自己看到哪个字段错了 → 自己改 → 再审 → 通过才交付
```

整个来回不需要你插手。而且 Agent 拿到的不是"你这里可能有问题"，是
**"revenue 字段是 92.0，证据里是 85.6，差 7.48%，只改这个字段，其他别动"**。

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

应该能看到 16 个 `sagax.*` 工具。看不到就查客户端的 MCP 日志——
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

### 审计

| 工具 | 干什么 |
|---|---|
| `sagax.audit_check` | **最常用**：只审不改，返回裁决 + 修复计划 |
| `sagax.audit_run` | 审完不通过就自动改（需服务端配了模型） |
| `sagax.audit_status` | 查任务状态 |
| `sagax.audit_result` | 取已完成任务的结果 |
| `sagax.runs` | 历史审计任务 |

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

### 平台公共内容

| 工具 | 干什么 |
|---|---|
| `sagax.skill_official` | 平台公共 Skill |
| `sagax.wiki_public` | 平台公共 LLM Wiki |
| `sagax.usage` | 配额还剩多少 |

> 每个工具都是一次云端 API 调用的薄封装。MCP 这一层**不重复实现审计逻辑**——
> 否则就会出现两套结论。

---

## 怎么让 Agent 用起来

光配上工具，Agent 不一定知道什么时候该调。给它一条明确指令。

### 放进项目的 `CLAUDE.md` / `.cursorrules` / 系统提示

```markdown
## 投研结论交付前必须审计

任何对外的财务数字或估值指标（营业收入、净利润、ROE、PE 等），
交付前必须调用 `sagax.audit_check`。

调用时每个数字单独成字段，带上 name / value / unit / period / basis /
source_refs，并把依据的数据放进 evidence。只给一段文字的话审计没法定位问题。

看结果：
- PASS       → 可以交付
- RETRY      → 按 repair_plan 改，**只改** fields_to_regenerate 里的字段，
               locked_fields 里的一个字都不许动，然后重新审
- BLOCK      → 不要交付，把问题如实告诉用户
- NEED_HUMAN → 规则冲突了，不要自己选一边，交给人

自动重试最多两次。两次还不过就如实报告剩余风险，别接着试。
```

### 或者直接用现成的 Skill

仓库里的 [`skills/sagax-audit/SKILL.md`](../skills/sagax-audit/SKILL.md)（同步到公开仓）
就是写给 Agent 看的完整版：什么时候必须调、怎么给字段、怎么读结果、
以及八条绝对禁止。支持 Skill 的客户端可以直接加载它。

---

## 一次实际对话长什么样

```
你：帮我分析一下这家公司 2025 年的营收和净利润，数据在 annual_report.md 里

Agent：（读文件，算出结果）

      我先过一遍审计……
      → 调用 sagax.audit_check

      审计返回 RETRY，发现一个问题：
        · net_profit 字段是 12.5 亿元，但证据里 2025 年报是 10.2 亿元
          （相对差 22.5%，超过 0.5% 容差）

      我看了下，12.5 亿是券商对 2026 年的预测值，我取错了。
      审计说 revenue 已通过、不要动，只重新生成 net_profit。

      （只改 net_profit，重新调 sagax.audit_check）

      → PASS

      结果：
        营业收入   85.6 亿元（2025A，年报p12）
        归母净利润 10.2 亿元（2025A，年报p13）
```

关键在于 Agent 拿到的是**可定位、可执行**的反馈，不是一句"你再检查检查"。

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
`sagax.audit_check` 默认等最多 300 秒。真的很慢的话改用异步：
`sagax.audit_run` 拿到 audit_id，之后用 `sagax.audit_status` 轮询。

**Q：以前配的是 `amadeus.*` 工具名？**
继续能用，不用改。新名字是 `sagax.*`，`tools/list` 只列新名字。

**Q：能限制 Agent 只用部分工具吗？**
可以，在客户端侧配工具白名单（各家做法不同，查客户端文档）。
比如只放开 `sagax.audit_check`，不给它写 Memory / Skill 的权限。

---

## 想直接在命令行里用

不走 MCP 也行：

```bash
sagax-amadeus mcp        # 启动 MCP Server（stdio）
sagax-amadeus status     # 或者直接用 CLI
```

CLI 的完整命令见 [SDK 使用教程](sdk-guide.md#10-配置项速查)。
