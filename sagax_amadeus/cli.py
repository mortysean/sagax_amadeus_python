"""Sagax Amadeus — 命令行入口。

    sagax-amadeus --version                  本地客户端版本（不联网）
    sagax-amadeus status                     连接、租户与配额状态
    sagax-amadeus version                    API / 引擎 / 公共规则版本
    sagax-amadeus review report.pdf          审一份文档
    sagax-amadeus review report.pdf --card r.html   顺带存一页可交互报告
    sagax-amadeus reviews list|get|card      历史文档审计
    sagax-amadeus evidence upload|list|delete
    sagax-amadeus memory|skill|wiki …        私有知识（远程 CRUD）
    sagax-amadeus boundary|candidate …       私有规则与候选规则
    sagax-amadeus mcp                        启动 MCP Server (stdio)

**每一条子命令都是一次 HTTPS 调用**，没有本地审计。配置走环境变量：

    SAGAX_AUDIT_API_BASE_URL   云端地址（旧名 SAGAX_CLOUD_URL 仍然可用）
    SAGAX_AUDIT_API_KEY        订阅 Key（旧名 SAGAX_API_KEY 仍然可用）
    SAGAX_AUDIT_PROJECT_ID     项目 id（租户内二次隔离，默认 default）

CLI 打印的内容都先过 :func:`scrub_secrets` —— 演示时截图不会把密钥带出去。

改造前的 ``init`` / ``backup`` / ``demo`` / ``cloud serve`` / ``cloud provision``
不在这里了：它们是运行时和服务端的操作，现在属于 ``sagax_audit_cloud``。
执行这些子命令会给出明确指引，而不是假装成功。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Optional

from sagax_amadeus.client import SagaxAuditClient
from sagax_amadeus.exceptions import SagaxAuditError
from sagax_amadeus.models import EvidenceItem

#: 与云端 common.scrub_secrets 同一套语义，但这里必须自带一份：SDK 不依赖云端包。
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\b(?:sagax|amadeus)_sk_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{10,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}\b", re.IGNORECASE),
)
_SECRET_KEYS = ("api_key", "apikey", "secret", "password", "token",
                "credential", "private_key", "access_key")


def scrub_secrets(value: Any) -> Any:
    """递归擦除疑似凭证 —— CLI 输出的最后一道闸。"""
    if isinstance(value, dict):
        return {k: ("[REDACTED]"
                    if any(t in str(k).lower() for t in _SECRET_KEYS)
                    and v not in (None, "") else scrub_secrets(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_secrets(v) for v in value]
    if isinstance(value, str):
        out = value
        for pattern in _SECRET_PATTERNS:
            out = pattern.sub("[REDACTED]", out)
        return out
    return value


def _out(payload: Any) -> None:
    print(json.dumps(scrub_secrets(_plain(payload)), ensure_ascii=False,
                     indent=2, default=str))


def _plain(value: Any) -> Any:
    """dataclass → dict，方便直接 json.dumps。"""
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _client(args: argparse.Namespace) -> SagaxAuditClient:
    return SagaxAuditClient(
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
        project_id=getattr(args, "project", None),
        timeout=float(getattr(args, "timeout", 60.0)),
        allow_insecure_http=bool(getattr(args, "allow_insecure_http", False)))


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace) -> int:
    """连接、租户与配额状态。"""
    with _client(args) as client:
        _out(client.status())
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """服务端版本信息。"""
    with _client(args) as client:
        _out({"sdk": _sdk_version(), **client.version().to_dict()})
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """审一份文档。

    文档直接给路径；证据可以是已上传的 id（``--evidence-id``，可重复），也可以
    是一个结构化 JSON 文件（``--evidence``），形如::

        {"evidence": [{"field": "revenue", "value": 85.6, "unit": "亿元",
                       "source": "2025年报", "source_ref": "ann:2025A#p12"}]}

    退出码：有 ``blocking`` 风险时返回 1。这样 CI 里一句 ``sagax-amadeus review``
    就能当门禁用，不必再解析 JSON 去判断能不能发。
    """
    evidence = []
    if args.evidence:
        with open(args.evidence, encoding="utf-8") as fh:
            raw = json.load(fh)
        rows = raw.get("evidence") if isinstance(raw, dict) else raw
        evidence = [EvidenceItem.from_dict(e) for e in rows or []]

    with _client(args) as client:
        result = client.review(args.document, evidence=evidence,
                               evidence_ids=args.evidence_id or [],
                               code=args.code or "",
                               auto_evidence=not args.no_auto_evidence,
                               timeout=float(args.wait))
        if args.annotated:
            client.download_annotated(result.review_id, args.annotated)
        if args.report:
            client.download_review_report(result.review_id, args.report)
        if args.card:
            client.download_review_card(result.review_id, args.card)
        _out({"review_id": result.review_id,
              "grade": result.grade.letter if result.grade else None,
              "blocking": result.blocking,
              "coverage_note": result.coverage_note,
              "annotations": [a.to_dict() for a in result.annotations],
              "annotated_saved_to": args.annotated,
              "report_saved_to": args.report,
              "card_saved_to": args.card})
    return 1 if result.blocking else 0


def cmd_reviews(args: argparse.Namespace) -> int:
    """历史文档审计的查询与下载。"""
    with _client(args) as client:
        if args.action == "list":
            _out([r.to_dict() for r in client.list_reviews(args.limit)])
        elif args.action == "get":
            _out(client.get_review(
                _need(args.review_id, "--review-id")).to_dict())
        elif args.action == "annotated":
            dest = args.out or f"{args.review_id}-annotated.pdf"
            _out({"saved": client.download_annotated(
                _need(args.review_id, "--review-id"), dest)})
        elif args.action == "report":
            dest = args.out or f"{args.review_id}-report.md"
            _out({"saved": client.download_review_report(
                _need(args.review_id, "--review-id"), dest)})
        elif args.action == "card":
            dest = args.out or f"{args.review_id}-report.html"
            _out({"saved": client.download_review_card(
                _need(args.review_id, "--review-id"), dest)})
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    """Evidence 上传 / 查询 / 删除。"""
    with _client(args) as client:
        if args.action == "upload":
            _out(client.upload_evidence(path=_need(args.file, "--file")))
        elif args.action == "list":
            _out(client.list_evidence(args.limit))
        elif args.action == "get":
            _out(client.get_evidence(_need(args.evidence_id, "--evidence-id")))
        elif args.action == "download":
            dest = args.out or args.evidence_id
            _out({"saved": client.download_evidence(
                _need(args.evidence_id, "--evidence-id"), dest)})
        elif args.action == "delete":
            _out(client.delete_evidence(_need(args.evidence_id, "--evidence-id")))
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    """私有 Memory 的远程 CRUD。"""
    with _client(args) as client:
        if args.action == "add":
            _out(client.create_memory(
                title=_need(args.title, "--title"), body=args.body or "",
                kind=args.kind, tags=_tags(args.tags)))
        elif args.action == "search":
            _out(client.list_memories(query=args.query or "", limit=args.limit))
        elif args.action == "list":
            _out(client.list_memories(limit=args.limit))
        elif args.action == "get":
            _out(client.get_memory(_need(args.id, "--id")))
        elif args.action == "update":
            _out(client.update_memory(_need(args.id, "--id"),
                                      **_changes(args, ("title", "body", "kind"))))
        elif args.action == "delete":
            _out(client.delete_memory(_need(args.id, "--id")))
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    """私有 Skill 的远程 CRUD。"""
    with _client(args) as client:
        if args.action == "add":
            _out(client.create_skill(
                name=_need(args.name, "--name"), body=args.body or "",
                description=args.description or "", tags=_tags(args.tags),
                tool_scope=_tags(args.tool_scope)))
        elif args.action == "search":
            _out(client.list_skills(query=args.query or "", limit=args.limit))
        elif args.action == "list":
            _out(client.list_skills(limit=args.limit))
        elif args.action == "get":
            _out(client.get_skill(_need(args.id, "--id")))
        elif args.action == "update":
            _out(client.update_skill(
                _need(args.id, "--id"),
                **_changes(args, ("body", "description"))))
        elif args.action == "delete":
            _out(client.delete_skill(_need(args.id, "--id")))
        elif args.action == "official":
            _out(client.official_skills())
    return 0


def cmd_wiki(args: argparse.Namespace) -> int:
    """私有 / 公共 LLM Wiki。"""
    with _client(args) as client:
        if args.action == "write":
            _out(client.create_wiki_document(
                slug=_need(args.slug, "--slug"),
                title=args.title or args.slug, body=args.body or "",
                tags=_tags(args.tags), section=args.section))
        elif args.action == "search":
            _out(client.list_wiki_documents(query=args.query or "",
                                            limit=args.limit))
        elif args.action == "list":
            _out(client.list_wiki_documents(limit=args.limit))
        elif args.action == "get":
            _out(client.get_wiki_document(_need(args.slug, "--slug")))
        elif args.action == "delete":
            _out(client.delete_wiki_document(_need(args.slug, "--slug")))
        elif args.action == "public":
            _out(client.public_wiki(args.query or "", limit=args.limit))
    return 0


def cmd_boundary(args: argparse.Namespace) -> int:
    """私有规则管理。"""
    with _client(args) as client:
        if args.action == "add-rule":
            _out(client.create_boundary({
                "rule_id": _need(args.rule_id, "--rule-id"),
                "title": args.title or args.rule_id,
                "statement": args.statement or "",
                "tier": args.tier, "origin": "user",
                "severity": args.severity, "validator": args.validator,
                "applies_to": _tags(args.applies_to) or ["*"],
                "params": json.loads(args.params) if args.params else {}}))
        elif args.action == "list":
            _out(client.list_boundaries())
        elif args.action == "public":
            _out(client.public_boundaries(task=args.query or ""))
        elif args.action == "disable":
            _out(client.delete_boundary(_need(args.rule_id, "--rule-id")))
    return 0


def cmd_candidate(args: argparse.Namespace) -> int:
    """候选规则（持续学习产出，永远不自动生效）。"""
    with _client(args) as client:
        if args.action == "list":
            _out(client.candidates())
        elif args.action == "approve":
            _out(client.approve_candidate(_need(args.id, "--id"),
                                          _need(args.by, "--by")))
        elif args.action == "contribute":
            if not args.confirm:
                print("贡献给公共库需要显式确认：加 --confirm", file=sys.stderr)
                return 2
            _out(client.contribute_candidate(_need(args.id, "--id"),
                                             confirmed=True))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    """订阅用量。"""
    with _client(args) as client:
        _out(client.usage())
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """启动 MCP Server（stdio）。"""
    from sagax_amadeus.mcp_server import serve_stdio
    with _client(args) as client:
        serve_stdio(client)
    return 0


def cmd_moved(args: argparse.Namespace) -> int:
    """改造后不再属于 SDK 的子命令。给出去处，不假装成功。"""
    print(args.moved_message, file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _need(value: Optional[str], flag: str) -> str:
    if not value:
        raise SystemExit(f"缺少必需参数 {flag}")
    return value


def _tags(raw: Optional[str]) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _changes(args: argparse.Namespace, names: tuple[str, ...]) -> dict[str, Any]:
    out = {n: getattr(args, n) for n in names if getattr(args, n, None) is not None}
    tags = getattr(args, "tags", None)
    if tags is not None:
        out["tags"] = _tags(tags)
    if not out:
        raise SystemExit("update 至少要给一个要改的字段")
    return out


def _sdk_version() -> str:
    from sagax_amadeus import __version__
    return __version__


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="sagax-amadeus",
        description="Sagax Amadeus — 公网审计 API 的命令行客户端")
    parser.add_argument("--base-url", default=None,
                        help="云端地址（默认取 SAGAX_AUDIT_API_BASE_URL）")
    parser.add_argument("--api-key", default=None,
                        help="订阅 Key（默认取 SAGAX_AUDIT_API_KEY）")
    parser.add_argument("--project", default=None, help="项目 id")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="单次 HTTP 请求超时（秒）")
    parser.add_argument("--allow-insecure-http", action="store_true",
                        help="允许对非本机地址使用明文 HTTP（不建议）")
    # 客户端版本，**不联网**。和下面的 `version` 子命令是两回事：那个查的是服务端
    # （API / 引擎 / 公共规则），要 base_url + Key 才跑得起来。而最需要查版本的时刻
    # 恰恰是还没配凭据、或者刚被 4.0 的接口墙撞出 AttributeError 的时候 —— 那会儿
    # 唯一想知道的就是「我装的到底是哪一版」，不该还要先有个能连的服务端。
    parser.add_argument("--version", action="version",
                        version=f"sagax-amadeus {_sdk_version()}",
                        help="打印客户端版本并退出（服务端版本见 version 子命令）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="连接与配额状态").set_defaults(func=cmd_status)
    sub.add_parser("version", help="服务端版本").set_defaults(func=cmd_version)
    sub.add_parser("usage", help="订阅用量").set_defaults(func=cmd_usage)
    sub.add_parser("mcp", help="启动 MCP Server (stdio)").set_defaults(func=cmd_mcp)

    p = sub.add_parser("review", help="审一份文档（有 blocking 风险时退出码 1）")
    p.add_argument("document", help="待审文档：PDF / markdown / 纯文本")
    p.add_argument("--evidence", default=None, help="结构化证据 JSON 文件")
    p.add_argument("--evidence-id", action="append", default=None,
                   help="已上传证据的 id，可重复")
    p.add_argument("--code", default=None,
                   help="证券代码（如 600519.SH）；不给则由服务端从正文里认")
    p.add_argument("--no-auto-evidence", action="store_true",
                   help="关掉服务端自动取证，只用你给的证据")
    p.add_argument("--wait", type=float, default=300.0, help="最长等待秒数")
    p.add_argument("--annotated", default=None, help="标注版 PDF 存到这个路径")
    p.add_argument("--report", default=None, help="审计报告存到这个路径")
    p.add_argument("--card", default=None,
                   help="一页报告（可交互 HTML）存到这个路径，双击即可打开")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("reviews", help="历史文档审计")
    p.add_argument("action",
                   choices=["list", "get", "annotated", "report", "card"])
    p.add_argument("--review-id", default=None)
    p.add_argument("--out", default=None, help="下载落盘路径")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_reviews)

    p = sub.add_parser("evidence", help="Evidence（会上传到云端）")
    p.add_argument("action", choices=["upload", "list", "get", "download",
                                      "delete"])
    p.add_argument("--file", default=None)
    p.add_argument("--evidence-id", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("memory", help="私有 Memory")
    p.add_argument("action", choices=["add", "search", "list", "get", "update",
                                      "delete"])
    p.add_argument("--id", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--body", default=None)
    p.add_argument("--kind", default="note")
    p.add_argument("--tags", default=None)
    p.add_argument("--query", default=None)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("skill", help="私有 Skill")
    p.add_argument("action", choices=["add", "search", "list", "get", "update",
                                      "delete", "official"])
    p.add_argument("--id", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--body", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--tags", default=None)
    p.add_argument("--tool-scope", default=None)
    p.add_argument("--query", default=None)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_skill)

    p = sub.add_parser("wiki", help="私有 / 公共 LLM Wiki")
    p.add_argument("action", choices=["write", "search", "list", "get",
                                      "delete", "public"])
    p.add_argument("--slug", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--body", default=None)
    p.add_argument("--tags", default=None)
    p.add_argument("--section", default="pages")
    p.add_argument("--query", default=None)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_wiki)

    p = sub.add_parser("boundary", help="私有规则")
    p.add_argument("action", choices=["add-rule", "list", "public", "disable"])
    p.add_argument("--rule-id", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--statement", default=None)
    p.add_argument("--tier", default="P1")
    p.add_argument("--severity", default="medium")
    p.add_argument("--validator", default="")
    p.add_argument("--applies-to", default=None)
    p.add_argument("--params", default=None, help="JSON 参数")
    p.add_argument("--query", default=None)
    p.set_defaults(func=cmd_boundary)

    p = sub.add_parser("candidate", help="候选规则")
    p.add_argument("action", choices=["list", "approve", "contribute"])
    p.add_argument("--id", default=None)
    p.add_argument("--by", default=None)
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(func=cmd_candidate)

    # 改造后搬走的子命令。保留是为了让敲旧命令的人拿到去处，而不是 "invalid choice"。
    for name, message in (
        ("init", "init 已移除：私有数据现在存在云端，第一次写入时自动就绪。"
                 "先看 sagax-amadeus status 确认连得上。"),
        ("backup", "backup 已移除：私有数据在云端，备份由平台负责。"
                   "要给某次审计留档用 sagax-amadeus reviews report --review-id …"),
        ("demo", "demo 已移到云端服务包：python3 -m sagax_audit_cloud.demo"),
        ("cloud", "cloud serve / provision 属于服务端：\n"
                  "  启动服务  python3 -m sagax_audit_cloud.api.app --port 4600\n"
                  "  开通租户  POST /v1/admin/tenants（需 SAGAX_AUDIT_ADMIN_TOKEN）"),
    ):
        sp = sub.add_parser(name, help=f"（已移除）{message.splitlines()[0]}")
        sp.add_argument("rest", nargs="*", help=argparse.SUPPRESS)
        sp.set_defaults(func=cmd_moved, moved_message=message)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口。

    Returns:
        进程退出码。SDK 异常在这里被翻成一行人话 + 非零退出码，
        而不是给用户一段 traceback。
    """
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SagaxAuditError as exc:
        # str(exc) 已经脱敏（transport 保证 API Key 不进异常）。
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"文件不存在: {exc.filename}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:                              # pragma: no cover
        return 130


if __name__ == "__main__":                                 # pragma: no cover
    sys.exit(main())
