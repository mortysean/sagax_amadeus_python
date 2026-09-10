"""Sagax Amadeus — MCP Server（stdio JSON-RPC 2.0）。

把 Sagax Amadeus 云端引擎的能力暴露给任何支持 MCP 的 Agent：

    sagax.review           审一份文档：高亮 + comment + 评级 + 风险
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
from sagax_amadeus.exceptions import SagaxAuditError
from sagax_amadeus.models import EvidenceItem

PROTOCOL_VERSION = "2024-11-05"
#: 发行包改名成 ``sagax-amadeus`` 之后这个名字**故意没跟着改**：MCP 客户端拿
#: 它做工具的命名空间（`mcp__sagax-audit__sagax.review`），改一次就等于
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
    """MCP Server —— Sagax Amadeus 云端引擎的薄封装。"""

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

        review_schema = {
            "type": "object",
            "properties": {
                "document_path": {
                    "type": "string",
                    "description": "待审文档的本地路径：PDF / markdown / 纯文本。"
                                   "**PDF 必须带文字层** —— 扫描件或整页转成图片"
                                   "的 PDF 抽不出字，会明确报错（而不是给你一个"
                                   "假的「通过」）"},
                "evidence": {
                    "type": "array", "items": {"type": "object"},
                    "description": "结构化证据，**可以不给** —— 服务端会为报告里"
                                   "点名的财务指标自己取真值。给了更强：那是你"
                                   "声明「这个数来自这里」，比事后独立查更贴近"
                                   "你实际用的口径。两者会合并"},
                "evidence_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "已上传证据的 id"},
                "code": {"type": "string",
                         "description": "证券代码（如 600519.SH）。给了它，服务端"
                                        "会为报告里点名的财务指标自动取真值核对；"
                                        "不给则从正文里认"},
                "auto_evidence": {
                    "type": "boolean",
                    "description": "是否允许服务端自动取证，默认 true"},
                "annotated_path": {
                    "type": "string",
                    "description": "把标注版 PDF 存到这个路径（仅 PDF 源文档）"},
                "card_path": {
                    "type": "string",
                    "description": "把**一页可交互报告**存到这个路径（单文件 "
                                   "HTML，任何源格式都有）。要给人看审计结论时"
                                   "存这个：一页纸讲清结论、评分、站不住的数字"
                                   "和整条执行链，每步可展开。不引 CDN，断网可看，"
                                   "可直接转发。路径以 .html 结尾"},
                "card_pdf_path": {
                    "type": "string",
                    "description": "把一页报告的 **PDF 版**存到这个路径。和 "
                                   "card_path 是同一份内容的两种形态：PDF 供"
                                   "存档、签字、进合规留痕；要展开看每步调了"
                                   "什么用 HTML。路径以 .pdf 结尾"},
            }, "required": ["document_path"]}

        self._tool("sagax.review",
                   "审一份文档：在它上面标出没有依据的数字，逐条给出修改意见，"
                   "并给整份文档一个评级。判定由确定性规则做，不是模型打分，"
                   "所以同样的输入永远得到同样的结论 —— 重复调用不会改变结果。"
                   "\n\n**证据可以不给**：报告里点了名的财务指标，服务端会自己"
                   "去数据源取真值来对。"
                   "\n\n拿到结果后："
                   "\n· 先读 coverage_note —— 它说明「查了多少、没查多少」。"
                   "没查的部分**不算通过**，交付时要如实说明。"
                   "\n· 标注里写「数据源显示…是 X」的，那是独立查到的真值，"
                   "按它改，改完重新审一遍。"
                   "\n· 只说「没有依据」而没给真值的，你**不知道**正确值是多少 ——"
                   "去查或标注「待确认」，**不要随手换一个数**，那只是把一个无据"
                   "的数字换成另一个，而且看起来像核对过。"
                   "\n· 加「约」字、改成区间、换个说法都是粉饰不是修复。"
                   "查不到依据的数字只有两条路：补来源，或删掉。"
                   "\n· workflow.problems 非空时是**执行过程**有问题，改文字解决"
                   "不了，报给人。"
                   "\n\n要把结论**交给人看**，传 card_path：会存一份一页纸的可交互"
                   "报告（单文件 HTML，双击即开、可直接转发），比你把 annotations "
                   "复述一遍更完整，也不会在转述里丢掉「哪些没查」。",
                   review_schema,
                   lambda **kw: _text(self._review(**kw)))

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
        self._tool("sagax.runs",
                   "历史文档审计（摘要：只有等级字母，完整结果按 review_id 取）",
                   {"type": "object",
                    "properties": {"limit": {"type": "integer", "default": 20}}},
                   lambda limit=20: _text(
                       [r.to_dict() for r in c.list_reviews(limit)]))
        self._tool("sagax.usage", "订阅用量与配额",
                   {"type": "object", "properties": {}},
                   lambda: _text(c.usage()))

    def _tool(self, name: str, description: str, schema: dict[str, Any],
              handler: Callable[..., Any]) -> None:
        self.tools[name] = ({"name": name, "description": description,
                             "inputSchema": schema}, handler)

    # ---- 工具实现 --------------------------------------------------------
    def _review(self, *, document_path: str,
                evidence: Optional[list[dict]] = None,
                evidence_ids: Optional[list[str]] = None,
                code: str = "", auto_evidence: bool = True,
                annotated_path: str = "", card_path: str = "",
                card_pdf_path: str = "", timeout: float = 300.0) -> Any:
        """审一份文档并直接返回结论 —— Agent 要的是结论，不是一个任务 id。

        返回里刻意把 ``coverage_note`` 摆在前面：调用方最容易犯的错是把
        「没发现问题」读成「没有问题」，而两者之间隔着的就是这句话。
        """
        result = self.client.review(
            document_path,
            evidence=[EvidenceItem.from_dict(e) for e in (evidence or [])],
            evidence_ids=evidence_ids or [], code=code,
            auto_evidence=auto_evidence, timeout=timeout)
        saved = None
        if annotated_path:
            saved = self.client.download_annotated(result.review_id,
                                                   annotated_path)
        # 取不到附属产物**不能**吃掉整个结论：审阅已经跑完、已经计费，
        # 而调用方要的是结论。取不到就照实回 null —— 编一个路径出来，
        # 上层会把它当链接发给用户，点开是 404。
        card_saved = card_pdf_saved = None
        if card_path:
            try:
                card_saved = self.client.download_review_card(
                    result.review_id, card_path)
            except SagaxAuditError:
                card_saved = None
        if card_pdf_path:
            try:
                card_pdf_saved = self.client.download_review_card_pdf(
                    result.review_id, card_pdf_path)
            except SagaxAuditError:
                card_pdf_saved = None
        return {
            "review_id": result.review_id,
            "coverage_note": result.coverage_note,
            "grade": result.grade.letter if result.grade else None,
            "grade_rationale": result.grade.rationale if result.grade else "",
            "blocking": result.blocking,
            "annotations": [a.to_dict() for a in result.annotations],
            "risk_summary": _plain(result.risk_summary),
            "annotated_saved_to": saved,
            "card_saved_to": card_saved,
            "card_pdf_saved_to": card_pdf_saved,
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
                return _error(msg_id, -32602, _unknown_tool(name))
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


#: 已下线的工具 → 该改用什么。
#:
#: 直接回「未知工具」，Agent 只会换个参数再试一遍，然后再失败一遍。已经配好的
#: 集成升级上来时，第一次调用就该看到「改用哪个」，而不是自己去猜。
_RETIRED_TOOLS = {
    "sagax.audit_run": "sagax.review",
    "sagax.audit_check": "sagax.review",
    "sagax.audit_status": "sagax.review",
    "sagax.audit_result": "sagax.review",
}


def _unknown_tool(name: str) -> str:
    """未知工具的报错文案；命中已下线名单时直接指路。"""
    replacement = _RETIRED_TOOLS.get(_alias(name))
    if replacement:
        return (f"{name} 已下线（同步拦截 / 异步事后审计 / 修复循环三种模式一并"
                f"移除）。改用 {replacement}：它接收一份文档，返回高亮、comment、"
                "评级与风险总结。")
    return f"未知工具 {name}"


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
        description="Sagax Amadeus MCP Server（stdio）")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--project", default=None)
    args = parser.parse_args(argv)
    build_server(base_url=args.base_url, api_key=args.api_key,
                 project_id=args.project).serve_stdio()
    return 0


if __name__ == "__main__":                                 # pragma: no cover
    sys.exit(main())
