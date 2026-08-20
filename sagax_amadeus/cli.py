"""Sagax Audit — 命令行入口。

    sagax-amadeus status                     连接、租户与配额状态
    sagax-amadeus version                    API / 引擎 / 公共规则版本
    sagax-amadeus audit --input run.json     提交一次审计并等结果
    sagax-amadeus audits list|get|cancel     审计任务
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


def cmd_audit(args: argparse.Namespace) -> int:
    """提交一次审计并等结果。

    ``--input`` 是一个 JSON 文件::

        {"task": "…", "prompt": "…",
         "candidate_output": {"fields": [{"name": "revenue", "value": 85.6, …}]},
         "evidence": [{"field": "revenue", "value": 85.6, …}],
         "evidence_ids": ["ev_…"],
         "required_fields": ["revenue", "net_profit"]}
    """
    with open(args.input, encoding="utf-8") as fh:
        payload = json.load(fh)
    if not str(payload.get("task") or "").strip():
        print("输入文件缺少 task 字段", file=sys.stderr)
        return 2
    with _client(args) as client:
        result = client.audit(
            task=payload["task"], prompt=payload.get("prompt", ""),
            candidate_output=payload.get("candidate_output"),
            evidence=[EvidenceItem.from_dict(e)
                      for e in payload.get("evidence") or []],
            evidence_ids=payload.get("evidence_ids") or [],
            required_fields=payload.get("required_fields") or [],
            boundary_overrides=payload.get("boundary_overrides") or [],
            auto_repair=bool(payload.get("auto_repair", not args.check_only)),
            timeout=float(args.wait))
        if args.report:
            client.download_report(result.audit_id, args.report)
        if args.bundle:
            client.download_bundle(result.audit_id, args.bundle)
        _out({"audit_id": result.audit_id, "status": result.status,
              "attempts": result.attempts,
              "findings": [f.to_dict() for f in result.findings()],
              "repair_plan": _plain(result.repair_plan()),
              "final_output": result.final_output,
              "report_saved_to": args.report, "bundle_saved_to": args.bundle})
    return 0


def cmd_audits(args: argparse.Namespace) -> int:
    """审计任务的查询与取消。"""
    with _client(args) as client:
        if args.action == "list":
            _out(client.list_audits(args.limit))
        elif args.action == "get":
            _out(client.get_audit(_need(args.audit_id, "--audit-id")))
        elif args.action == "status":
            _out(client.get_audit_status(_need(args.audit_id, "--audit-id")))
        elif args.action == "result":
            _out(client.get_audit_result(_need(args.audit_id, "--audit-id")))
        elif args.action == "report":
            dest = args.out or f"{args.audit_id}-report.md"
            _out({"saved": client.download_report(
                _need(args.audit_id, "--audit-id"), dest)})
        elif args.action == "bundle":
            dest = args.out or f"{args.audit_id}-bundle.tar.gz"
            _out({"saved": client.download_bundle(
                _need(args.audit_id, "--audit-id"), dest)})
        elif args.action == "trace":
            events = client.get_trace(_need(args.audit_id, "--audit-id"))
            if args.out:
                _out({"saved": client.download_trace(args.audit_id, args.out)})
            else:
                _out(events)
        elif args.action == "cancel":
            _out(client.cancel_audit(_need(args.audit_id, "--audit-id")))
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
        description="Sagax Audit — 公网审计 API 的命令行客户端")
    parser.add_argument("--base-url", default=None,
                        help="云端地址（默认取 SAGAX_AUDIT_API_BASE_URL）")
    parser.add_argument("--api-key", default=None,
                        help="订阅 Key（默认取 SAGAX_AUDIT_API_KEY）")
    parser.add_argument("--project", default=None, help="项目 id")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="单次 HTTP 请求超时（秒）")
    parser.add_argument("--allow-insecure-http", action="store_true",
                        help="允许对非本机地址使用明文 HTTP（不建议）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="连接与配额状态").set_defaults(func=cmd_status)
    sub.add_parser("version", help="服务端版本").set_defaults(func=cmd_version)
    sub.add_parser("usage", help="订阅用量").set_defaults(func=cmd_usage)
    sub.add_parser("mcp", help="启动 MCP Server (stdio)").set_defaults(func=cmd_mcp)

    p = sub.add_parser("audit", help="提交一次审计并等结果")
    p.add_argument("--input", required=True, help="审计请求 JSON 文件")
    p.add_argument("--wait", type=float, default=300.0, help="最长等待秒数")
    p.add_argument("--check-only", action="store_true",
                   help="只审不修（不进入自动修复循环）")
    p.add_argument("--report", default=None, help="把报告存到这个路径")
    p.add_argument("--bundle", default=None, help="把审计包存到这个路径")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("audits", help="审计任务")
    p.add_argument("action", choices=["list", "get", "status", "result",
                                      "report", "bundle", "trace", "cancel"])
    p.add_argument("--audit-id", default=None)
    p.add_argument("--out", default=None, help="下载落盘路径")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_audits)

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
                   "要给某次审计留档用 sagax-amadeus audits bundle --audit-id …"),
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
