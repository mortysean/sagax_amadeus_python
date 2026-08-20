# Sagax Amadeus

**投研审计智能体的客户端。**在你的结论发出去之前，用确定性校验核对每一个数字：
和证据比、和定义比、和口径比。

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

## 三种工作模式

同一个引擎，三种接入位置，按你对"拦截"和"自动化"的需要选：

### 1. 同步拦截 —— 发出去之前挡住

```python
result = client.check(task="核对 2025 年报", candidate_output=我的结论,
                      evidence=我的数据)
if result.status == "PASS":
    交付()
else:
    停下(result.findings())
```

阻塞调用，拿到裁决才继续。它**只审不改**，不碰你的输出 —— 修不修、怎么修由你
决定。用在交付前的最后一道关口。

### 2. 异步事后审计 —— 不挡路，留证据

```python
job = client.create_audit(task="…", evidence_ids=[ev.evidence_id])
# 该干嘛干嘛，不阻塞
result = client.wait_for_audit(job.audit_id)      # 或轮询 get_audit_status
client.download_bundle(job.audit_id, "bundle.tar.gz")
```

提交完立刻返回 `audit_id`，审计在后台跑。用在批量回溯、日终复核、合规留痕 ——
产出的**审计包**含完整证据链与本次生效的规则快照，将来被问起来能整个拿出来。

### 3. 修复循环 —— 审完不通过就定向改

```python
result = client.audit(task="…", candidate_output=我的结论, auto_repair=True)
```

不通过时引擎生成 `RepairPlan`：`fields_to_regenerate` 是要重写的字段，
`locked_fields` 是已通过、一个字都不许动的字段。带着这个约束重新生成，再审，
最多两轮。

**锁定是关键**。没有它，重试就是"整篇重写"—— 改好一个字段又碰坏另一个，来回
震荡不收敛。锁定之后每一轮的通过字段单调增加。

> Agent 场景直接用内置的 MCP Server，让它自己审、自己改、自己再审，整个来回
> 不需要人插手 —— 见[下文](#让-agent-自己调用)。

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
                               ├─ RETRY        定向修复 → 再审（≤2 轮）
                               ├─ BLOCK        阻断并出报告
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

三种模式在两种部署下都成立，客户端代码一个字不用改 —— 只换 `base_url`：

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

## 五分钟跑通

拿到服务地址和 API Key 后（见[快速开始](docs/quickstart.md)）：

```python
from sagax_amadeus import SagaxAuditClient

client = SagaxAuditClient(base_url="https://audit.example.com",
                          api_key="sagax_sk_...")

result = client.check(
    task="核对某公司 2025 年估值指标",
    candidate_output={"fields": [
        {"name": "pe", "value": 18.5, "unit": "x", "period": "2025A",
         "basis": "forward",                      # ← 预测口径
         "source_refs": ["broker:2026E"]},
    ]},
    evidence=[
        {"field": "eps", "value": 5.0, "unit": "元", "period": "2026E",
         "source": "券商预测", "source_ref": "broker:2026E",
         "is_forecast": True},
    ],
    required_fields=["pe"],
)

print(result.status)
for f in result.findings():
    print(f.field, "|", f.rule_id, "|", f.reason)

plan = result.repair_plan()
print("不许动:", plan.locked_fields, " 要重写:", plan.fields_to_regenerate)
```

```
RETRY
pe | pub.pe_basis_disclosed | pe 口径 'forward' 不在允许集合 ['historical', 'trailing', 'ttm', 'reported']
不许动: []  要重写: ['pe']
```

这个数字每一位都能对上出处，但它把 2026 年的预测盈利当成了 2025 年的估值依据。
规则不是"禁止预测"—— 是**预测口径必须由你的租户规则显式放开**，默认不许悄悄混用。

## 你会拿到什么

| 裁决 | 意思 | 你该做什么 |
|---|---|---|
| `PASS` | 通过 | 可以发 |
| `RETRY` | 有问题，能定位到字段 | 按 `repair_plan()` 改，只改指定字段 |
| `BLOCK` | 有问题且改不动 | 不要发，人工看 |
| `NEED_HUMAN` | 公私规则冲突 | 别自己选一边，找人拍板 |

不通过时还有一份人能读的审计报告（Markdown）和一个审计包（完整证据链 + 本次
生效的规则快照，含 origin / version / content_hash）。

## 让 Agent 自己调用

MCP Server 就在这个包里，不用另装：

```jsonc
{"mcpServers": {"sagax-audit": {
  "command": "python3", "args": ["-m", "sagax_amadeus.mcp_server"],
  "env": {"SAGAX_AUDIT_API_BASE_URL": "https://audit.example.com",
          "SAGAX_AUDIT_API_KEY": "sagax_sk_..."}}}}
```

配好之后 Claude Code / Cursor 里的 Agent 会自己审、自己看哪个字段错、自己改、
再审，通过才交付。完整说明见 [MCP 接入教程](docs/mcp-guide.md)，
[`skills/SKILL.md`](skills/SKILL.md) 是写给 Agent 直接加载的版本。

## 命令行

```bash
sagax-amadeus status                      # 连接、租户与配额
sagax-amadeus audit --input run.json --report out.md
sagax-amadeus audits list|get|report|bundle|trace|cancel
sagax-amadeus evidence upload|list|download|delete
sagax-amadeus memory|skill|wiki …         # 你的私有知识
sagax-amadeus mcp                         # 启动 MCP Server (stdio)
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
