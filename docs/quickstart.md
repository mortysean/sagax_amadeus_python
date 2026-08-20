# 快速开始：5 分钟跑通

目标：装上、连上、审一次、看懂结果。

---

## 第 1 步：装

```bash
pip install sagax-amadeus
```

没有任何依赖，Python 3.10 以上就行。

## 第 2 步：拿到三样东西

用我们托管的服务就找我们要；自己私有化部署的话，这三样都由你自己的部署产生：

| 东西 | 长什么样 | 干什么的 |
|---|---|---|
| 服务地址 | `https://audit.example.com` | 连哪里 |
| API Key | `sagax_sk_xxxx...` | 你是谁 |
| 根证书 | `sagax-ca.crt` | 确认你连的确实是我们 |

> 根证书这一步只有当前这个部署需要（我们用的是自签证书，因为服务器只有 IP
> 没有域名）。以后换成正式域名就不用它了。

```bash
# 下载根证书
curl -k -o sagax-ca.crt https://audit.example.com/ca.crt

# 核对指纹 —— 和我们另外发给你的那串比一比，对不上就别用
openssl x509 -in sagax-ca.crt -noout -fingerprint -sha256
```

## 第 3 步：配好环境变量

```bash
export SAGAX_AUDIT_API_BASE_URL=https://audit.example.com
export SAGAX_AUDIT_API_KEY=sagax_sk_你的key
export SAGAX_AUDIT_CA_BUNDLE=$PWD/sagax-ca.crt
```

验一下通不通：

```bash
sagax-amadeus status
```

看到你的租户名和配额就说明通了。

## 第 4 步：审一次

新建 `try.py`：

```python
from sagax_amadeus import SagaxAuditClient

client = SagaxAuditClient()          # 自动读上面那三个环境变量

result = client.check(
    # 这次要干什么
    task="核对某公司 2025 年营业收入",

    # 你的结论：每个数字单独一个字段
    candidate_output={"fields": [
        {"name": "revenue", "value": 85.6, "unit": "亿元",
         "period": "2025A", "basis": "reported",
         "source_refs": ["年报p12"]},
    ]},

    # 你的依据：数字是从哪来的
    evidence=[
        {"field": "revenue", "value": 85.6, "unit": "亿元", "period": "2025A",
         "source": "2025年年度报告", "source_ref": "年报p12"},
    ],

    # 这次任务必须给出哪些字段
    required_fields=["revenue"],
)

print(result.status)
```

```bash
python try.py
```

输出：

```
PASS
```

## 第 5 步：故意写错，看它怎么抓

把结论里的 `85.6` 改成 `92.0`（证据里还是 85.6），再跑：

```python
print(result.status)                  # RETRY

for f in result.findings():
    print(f.field, "|", f.rule_id, "|", f.reason)

plan = result.repair_plan()
print("不许动:", plan.locked_fields)
print("要重写:", plan.fields_to_regenerate)
```

输出：

```
RETRY
revenue | pub.value_matches_evidence | 字段 revenue 为 92.0亿元，证据 2025年年度报告 为 85.6亿元（相对差 7.48% > 容差 0.50%）
不许动: []
要重写: ['revenue']
```

它没有说"这份报告有问题"，而是说**哪个字段、和哪条证据对不上、差多少**。
你的程序拿到 `fields_to_regenerate` 就知道只需要重新生成 `revenue`，
其他已经通过的字段一个字都不用动。

## 第 6 步：把来源删掉试试

把 `source_refs` 整行删掉再跑：

```
RETRY
revenue | p0.evidence_required | ...
revenue | task.revenue.evidence_backed | ...
```

**没有来源的数字一定过不了。** 这是平台的硬规则，关不掉——一个说不清出处的
数字，本来也不该发出去。

---

## 你已经会的

- `client.check(...)` 审一份结论，**不改**它，只告诉你对不对
- `result.status` 能不能发
- `result.findings()` 哪里错了
- `result.repair_plan()` 该改哪些、不该动哪些

## 接下来

- 把它接进你的程序 → [SDK 使用教程](sdk-guide.md)
- 让 AI Agent 自动调用它 → [MCP 接入教程](mcp-guide.md)

## 常见问题

**Q：报 `CERTIFICATE_VERIFY_FAILED`？**
根证书没配好。检查 `SAGAX_AUDIT_CA_BUNDLE` 指向的文件存在、且指纹对得上。

**Q：报 `InsecureTransportError`？**
地址写成 `http://` 了，改成 `https://`。SDK 不允许明文传输——你的 API Key 和
数据都走这条连接。

**Q：报 401？**
Key 没设、写错了、或者已经被吊销。`echo $SAGAX_AUDIT_API_KEY` 看一眼。

**Q：`value` 和 `text` 有什么区别？**
`value` 放数字（会参与数值比对），`text` 放文字。数字千万别塞进 `text`，
那样审计只能当字符串看，比不了。

**Q：`basis` 该写什么？**
数字的口径。已实现的写 `reported` / `trailing` / `ttm`，预测的写 `forward`。
写错会被抓——比如用 `forward` 的利润算历史 PE。

**Q：一定要给 `evidence` 吗？**
不给的话，审计只能检查内部一致性（定义对不对、口径有没有标、单位量级合不合理），
没法验证数字本身是不是真的。给了才能比。
