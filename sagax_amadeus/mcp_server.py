"""Sagax Audit — MCP Server（stdio JSON-RPC 2.0）。

把 Sagax Audit Cloud 的能力暴露给任何支持 MCP 的 Agent：

    sagax.audit_run        跑一次审计（云端执行）
    sagax.audit_check      只审不修，拿 verdict + RepairPlan
    sagax.audit_status     查任务状态
    sagax.audit_result     取结果
    sagax.evidence_upload  上传结构化证据
    sagax.memory_add       写私有 Memory
    sagax.memory_search    检索私有 Memory
    sagax.skill_add        写私有 Skill
    sagax.skill_search     检索私有 Skill
    sagax.skill_official   平台公共 Skill
    sagax.wiki_write       写私有 LLM Wiki
    sagax.wiki_search      检索私有 LLM Wiki
    sagax.wiki_public      平台公共 LLM Wiki
    sagax.boundaries       当前生效的私有规则
    sagax.runs             历史审计任务
    sagax.usage            订阅用量

**每个工具都是一次 HTTPS 调用**，一行审计逻辑都不在这个进程里。改造前它包着
一个本地运行时；现在它包着一个 HTTP 客户端，工具名与入参**保持不变**，
已经配好的 Agent 不用改。

配置：

    {"mcpServers": {"sagax-audit": {
        "command": "python3", "args": ["-m", "sagax_amadeus.mcp_server"],
        "env": {"SAGAX_AUDIT_API_BASE_URL": "https://audit.example.com",
                "SAGAX_AUDIT_API_KEY": "sagax_sk_…"}}}}
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Optional

from sagax_amadeus.cli import scrub_secrets
from sagax_amadeus.client import SagaxAuditClient
from sagax_amadeus.models import EvidenceItem

PROTOCOL_VERSION = "2024-11-05"
#: 发行包改名成 ``sagax-amadeus`` 之后这个名字**故意没跟着改**：MCP 客户端拿
#: 它做工具的命名空间（`mcp__sagax-audit__sagax.audit_check`），改一次就等于
#: 让所有已配好的 Agent 一起失灵。工具名同理，见下面的 ``sagax.*``。
SERVER_NAME = "sagax-audit"
#: 改名前的工具前缀。别的机器上已经配好的 Agent 还在按 ``amadeus.*`` 调，
#: 继续受理；``tools/list`` 只列新名字，不把旧名字也铺出去当成两套工具。
LEGACY_TOOL_PREFIX = "amadeus."


def _alias(name: str) -> str:
    """``amadeus.foo`` → ``sagax.foo``；其他名字原样返回。"""
    if name.startswith(LEGACY_TOOL_PREFIX):
        return "sagax." + name[len(LEGACY_TOOL_PREFIX):]
    return name


def _text(payload: Any) -> dict[str, Any]:
    """把返回值包成 MCP 的 content 结构。"""
    return {"content": [{"type": "text",
                         "text": json.dumps(scrub_secrets(_plain(payload)),
                                            ensure_ascii=False, indent=2,
                                            default=str)}]}


def _plain(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class SagaxMCPServer:
    """MCP Server —— Sagax Audit Cloud 的薄封装。"""

    def __init__(self, client: SagaxAuditClient) -> None:
        """Args:
            client: 已配置好的 :class:`~sagax_amadeus.client.SagaxAuditClient`。
        """
        self.client = client
        self.tools: dict[str, tuple[dict[str, Any], Callable[..., Any]]] = {}
        self._register_tools()

    # ---- 工具定义 --------------------------------------------------------
    def _register_tools(self) -> None:
        c = self.client

        output_schema = {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "fields": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "value": {"type": "number"},
                        "text": {"type": "string"},
                        "unit": {"type": "string"},
                        "period": {"type": "string"},
                        "basis": {"type": "string"},
                        "source_refs": {"type": "array",
                                        "items": {"type": "string"}},
                    }, "required": ["name"]}},
                "narrative": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "object"}},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "required_fields": {"type": "array", "items": {"type": "string"}},
            }, "required": ["task", "fields"]}

        self._tool("sagax.audit_run",
                   "跑一次完整审计（云端执行：公共规则 + 你的私有规则 + 修复循环）",
                   output_schema,
                   lambda **kw: _text(self._audit(auto_repair=True, **kw)))
        self._tool("sagax.audit_check",
                   "只审不修：返回裁决与 RepairPlan，由你自己去改",
                   output_schema,
                   lambda **kw: _text(self._audit(auto_repair=False, **kw)))
        self._tool("sagax.audit_status", "查一个审计任务的状态",
                   {"type": "object",
                    "properties": {"audit_id": {"type": "string"}},
                    "required": ["audit_id"]},
                   lambda audit_id: _text(c.get_audit_status(audit_id)))
        self._tool("sagax.audit_result", "取一个已完成审计的结果",
                   {"type": "object",
                    "properties": {"audit_id": {"type": "string"}},
                    "required": ["audit_id"]},
                   lambda audit_id: _text(c.get_audit_result(audit_id)))

        self._tool("sagax.evidence_upload",
                   "上传一批结构化证据到你的租户空间，返回 evidence_id",
                   {"type": "object",
                    "properties": {"evidence": {"type": "array",
                                                "items": {"type": "object"}}},
                    "required": ["evidence"]},
                   lambda evidence: _text(c.upload_evidence_items(
                       [EvidenceItem.from_dict(e) for e in evidence])))

        self._tool("sagax.memory_add", "写一条私有 Memory（绑定你的租户）",
                   {"type": "object",
                    "properties": {"title": {"type": "string"},
                                   "body": {"type": "string"},
                                   "kind": {"type": "string", "default": "note"},
                                   "tags": {"type": "array",
                                            "items": {"type": "string"}}},
                    "required": ["title", "body"]},
                   lambda title, body, kind="note", tags=None: _text(
                       c.create_memory(title=title, body=body, kind=kind,
                                       tags=tags)))
        self._tool("sagax.memory_search", "检索私有 Memory", _query_schema(),
                   lambda query="", limit=5: _text(
                       c.list_memories(query=query, limit=limit)))

        self._tool("sagax.skill_add", "写一个私有 Skill",
                   {"type": "object",
                    "properties": {"name": {"type": "string"},
                                   "body": {"type": "string"},
                                   "description": {"type": "string"},
                                   "tags": {"type": "array",
                                            "items": {"type": "string"}},
                                   "tool_scope": {"type": "array",
                                                  "items": {"type": "string"}}},
                    "required": ["name", "body"]},
                   lambda name, body, description="", tags=None,
                   tool_scope=None: _text(c.create_skill(
                       name=name, body=body, description=description,
                       tags=tags, tool_scope=tool_scope)))
        self._tool("sagax.skill_search", "检索私有 Skill", _query_schema(),
                   lambda query="", limit=5: _text(
                       c.list_skills(query=query, limit=limit)))
        self._tool("sagax.skill_official", "平台公共 Skill（所有租户只读共享）",
                   {"type": "object", "properties": {}},
                   lambda: _text(c.official_skills()))

        self._tool("sagax.wiki_write", "写一页私有 LLM Wiki",
                   {"type": "object",
                    "properties": {"slug": {"type": "string"},
                                   "title": {"type": "string"},
                                   "body": {"type": "string"},
                                   "tags": {"type": "array",
                                            "items": {"type": "string"}},
                                   "section": {"type": "string",
                                               "default": "pages"}},
                    "required": ["slug", "title", "body"]},
                   lambda slug, title, body, tags=None, section="pages": _text(
                       c.create_wiki_document(slug=slug, title=title, body=body,
                                              tags=tags, section=section)))
        self._tool("sagax.wiki_search", "检索私有 LLM Wiki", _query_schema(),
                   lambda query="", limit=5: _text(
                       c.list_wiki_documents(query=query, limit=limit)))
        self._tool("sagax.wiki_public", "平台公共 LLM Wiki", _query_schema(),
                   lambda query="", limit=5: _text(
                       c.public_wiki(query, limit=limit)))

        self._tool("sagax.boundaries", "当前生效的私有规则",
                   {"type": "object", "properties": {}},
                   lambda: _text(c.list_boundaries()))
        self._tool("sagax.runs", "历史审计任务",
                   {"type": "object",
                    "properties": {"limit": {"type": "integer", "default": 20}}},
                   lambda limit=20: _text(c.list_audits(limit)))
        self._tool("sagax.usage", "订阅用量与配额",
                   {"type": "object", "properties": {}},
                   lambda: _text(c.usage()))

    def _tool(self, name: str, description: str, schema: dict[str, Any],
              handler: Callable[..., Any]) -> None:
        self.tools[name] = ({"name": name, "description": description,
                             "inputSchema": schema}, handler)

    # ---- 工具实现 --------------------------------------------------------
    def _audit(self, *, task: str, fields: list[dict[str, Any]],
               narrative: str = "", evidence: Optional[list[dict]] = None,
               evidence_ids: Optional[list[str]] = None,
               required_fields: Optional[list[str]] = None,
               auto_repair: bool = True, timeout: float = 300.0) -> Any:
        """提交审计并等结果 —— Agent 要的是结论，不是一个任务 id。"""
        result = self.client.audit(
            task=task,
            candidate_output={"fields": fields, "narrative": narrative},
            evidence=[EvidenceItem.from_dict(e) for e in (evidence or [])],
            evidence_ids=evidence_ids or [],
            required_fields=required_fields or [],
            auto_repair=auto_repair, timeout=timeout)
        return {
            "audit_id": result.audit_id,
            "status": result.status,
            "attempts": result.attempts,
            "findings": [f.to_dict() for f in result.findings()],
            "repair_plan": _plain(result.repair_plan()),
            "final_output": result.final_output,
            "artifacts": result.artifacts,
        }

    # ---- JSON-RPC --------------------------------------------------------
    def handle(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        """处理一条 JSON-RPC 请求。

        Args:
            message: 解析后的请求对象。

        Returns:
            响应对象；通知（无 ``id``）返回 None。
        """
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            result: Any = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": _version()},
            }
        elif method in ("notifications/initialized", "initialized"):
            return None
        elif method == "tools/list":
            result = {"tools": [spec for spec, _ in self.tools.values()]}
        elif method == "tools/call":
            name = params.get("name", "")
            entry = self.tools.get(name) or self.tools.get(_alias(name))
            if entry is None:
                return _error(msg_id, -32602, f"未知工具 {name}")
            try:
                result = entry[1](**(params.get("arguments") or {}))
            except Exception as exc:  # noqa: BLE001 - 工具错误回给调用方，不杀进程
                return {"jsonrpc": "2.0", "id": msg_id,
                        "result": {"isError": True,
                                   "content": [{"type": "text",
                                                "text": str(scrub_secrets(str(exc)))}]}}
        elif method == "ping":
            result = {}
        else:
            return _error(msg_id, -32601, f"未实现的方法 {method}")

        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def serve_stdio(self, stdin=None, stdout=None) -> None:
        """在 stdin/stdout 上跑 JSON-RPC 循环（行分隔 JSON）。"""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(json.dumps(_error(None, -32700, "解析失败")) + "\n")
                stdout.flush()
                continue
            response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False,
                                        default=str) + "\n")
                stdout.flush()


#: 改造前的类名。它当时确实是 "Local" 的 —— 现在不是了，但别的地方按旧名字
#: import，保留同一个类对象。
LocalMCPServer = SagaxMCPServer


def _query_schema() -> dict[str, Any]:
    return {"type": "object",
            "properties": {"query": {"type": "string", "default": ""},
                           "limit": {"type": "integer", "default": 5}}}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def _version() -> str:
    from sagax_amadeus import __version__
    return __version__


def build_server(*, base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 project_id: Optional[str] = None) -> SagaxMCPServer:
    """按环境变量构造一个 MCP Server。"""
    return SagaxMCPServer(SagaxAuditClient(
        base_url=base_url, api_key=api_key, project_id=project_id))


def serve_stdio(client: SagaxAuditClient) -> None:
    """用一个现成的客户端跑 stdio 循环（CLI ``sagax-amadeus mcp`` 走这里）。"""
    SagaxMCPServer(client).serve_stdio()


def main(argv: Optional[list[str]] = None) -> int:
    """``python3 -m sagax_amadeus.mcp_server`` 入口。"""
    parser = argparse.ArgumentParser(
        prog="sagax_amadeus.mcp_server",
        description="Sagax Audit MCP Server（stdio）")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--project", default=None)
    args = parser.parse_args(argv)
    build_server(base_url=args.base_url, api_key=args.api_key,
                 project_id=args.project).serve_stdio()
    return 0


if __name__ == "__main__":                                 # pragma: no cover
    sys.exit(main())
