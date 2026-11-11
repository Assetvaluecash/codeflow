"""MCP server for coding assistants (PRD FR-037, AC-032): read-only tools over the Model Context Protocol (JSON-RPC 2.0, the
streamable-HTTP transport answered with single JSON responses) at POST /mcp, behind the same authentication and repository visibility as
the REST API.

Rules (FR-037): tools return structured report JSON; there is NO write tool (an assistant cannot record or change a relationship, a
finding or a verdict); the instructions tell the assistant to narrate only fields present in the report and that no blast-radius claim
may be made in review without a TIA analysis id.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

PROTOCOL = "2025-06-18"
INSTRUCTIONS = ("TIA is the system of record for change impact. Narrate only fields present in the report a tool returns; do not infer "
                "consumers, severities or test results that are not in it. Organisation guidance: no blast-radius claim in a review without a "
                "TIA analysis id. Unknowns and the analysis status say what TIA could not see: always mention them. For testing: coverage "
                "is static ('covered' means a test addresses an aspect, not that it passes); 'not_evaluated' is not 'safe'; a graph answer "
                "with status 'ambiguous' must be asked again with one candidate, never guessed; inferred rows are leads, not facts.")
TOOLS = [
    {"name": "get_impact_report", "title": "Get a TIA impact report",
     "description": "The full Change Impact Report of one analysis (findings, evidence, test scope, policy outcomes, unknowns), as the caller may see it.",
     "inputSchema": {"type": "object", "properties": {"analysis_id": {"type": "string"}}, "required": ["analysis_id"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "find_impact_report", "title": "Find the latest TIA report for a pull request or revision",
     "description": "The newest report for a repository's pull request number or revision (commit id or prefix of 7+ characters).",
     "inputSchema": {"type": "object", "properties": {"repository": {"type": "string"}, "pull_request": {"type": "integer"},
                                                      "revision": {"type": "string"}}, "required": ["repository"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "get_test_scope", "title": "What to test for an analysis",
     "description": "Entry points to test, rule gaps, consistency checks, recommended tests and test gaps of one analysis.",
     "inputSchema": {"type": "object", "properties": {"analysis_id": {"type": "string"}}, "required": ["analysis_id"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "get_policy_outcome", "title": "Release-policy outcome of an analysis",
     "description": "Each configured release-policy rule's outcome (pass, fail, not_evaluated, requires_review) with its evidence, including approvals recorded since.",
     "inputSchema": {"type": "object", "properties": {"analysis_id": {"type": "string"}}, "required": ["analysis_id"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
]

# ---- tools for testing agents: the tester views and direct graph questions (each is its REST route, for this caller) -----------------
_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_REPOS = {"repositories": {"type": "string", "description": "Optional: comma-separated repository names to limit the answer to."}}


def _tool(name: str, title: str, description: str, properties: dict, required: tuple = ()) -> dict:
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}, "annotations": _RO}


TOOLS += [
    _tool("get_tester_pack", "Tester pack of one change",
          "For the entry points one analysed change reaches: what to retest with a coverage gate (covered / gaps / not_evaluated), the UI locator "
          "pack of the pages that call them, and the API test matrix of the touched endpoints. 'Covered' means a test addresses the aspect, "
          "not that it passes.", {"analysis_id": {"type": "string"}}, ("analysis_id",)),
    _tool("get_coverage", "Technical coverage of the test repository",
          "With no change: the technical aspects every entry point and page should be tested for (inputs, rules, error answers) and the "
          "automated tests and written test cases that address each: covered, partial or missing.", _REPOS),
    _tool("get_api_inventory", "API automation inventory",
          "Every endpoint with its inputs (mandatory, type, rules), generated valid and invalid values, a test matrix with expected outcomes "
          "and the error statuses its handler answers with.", _REPOS),
    _tool("get_ui_inventory", "UI automation inventory",
          "Every page with its elements: a locator for each (CSS, XPath, Playwright; fragile ones flagged), mandatory flags, input rules, "
          "dropdown values, and the API calls the page makes.", _REPOS),
    _tool("check_test_case", "Is this test case enough?",
          "One test case as a query: the entry points it addresses, the technical aspects it covers and the ones it misses. Matching is static "
          "and textual.", {"title": {"type": "string"}, "entry_point": {"type": "string", "description": "e.g. 'POST /accounts/{id}/payments' or a page route"},
                           "steps": {"type": "string"}, "expected": {"type": "string"}, **_REPOS}),
    _tool("get_shared_components", "Shared components and rule gaps",
          "Endpoints and functions that several entry points depend on (a change to them needs the widest testing), and rules one consumer of a "
          "shared endpoint applies and another does not.", {"limit": {"type": "integer"}}),
    _tool("query_graph", "Ask the code graph",
          "A direct question about one component, with no change involved. question: 'consumers' (who depends on it), 'dependencies' (what it "
          "depends on), 'tests' (which tests exercise it) or 'covers' (what one test exercises). component: a node id, 'METHOD /path', or a "
          "function, test, service, table or topic name. status is ok, ambiguous (candidates listed: ask again with one node id) or not_found. "
          "Always report the answer's 'limits'.",
          {"question": {"type": "string", "enum": ["consumers", "dependencies", "tests", "covers"]}, "component": {"type": "string"},
           "repository": {"type": "string"}, "limit": {"type": "integer"}}, ("question", "component")),
]


class ToolError(Exception):
    pass


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg, tools: dict[str, Callable[[dict], dict]], server_version: str = "1") -> Optional[dict]:
    """One JSON-RPC message -> its response (None for a notification). `tools`: name -> callable(arguments) -> JSON result."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid JSON-RPC request")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:                                          # notifications (initialized, cancelled): nothing to answer
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                                                       "serverInfo": {"name": "tia", "title": "TIA - Technical Impact Analysis", "version": server_version},
                                                       "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        spec = next((t for t in TOOLS if t["name"] == name), None)
        if spec is None or name not in tools:
            return _error(mid, -32602, f"unknown tool '{name}' (TIA's MCP tools are read-only: {', '.join(t['name'] for t in TOOLS)})")
        allowed = set(spec["inputSchema"]["properties"])
        if not isinstance(args, dict) or set(args) - allowed or any(r not in args for r in spec["inputSchema"].get("required", [])):
            return _error(mid, -32602, f"invalid arguments for {name}: expected {sorted(allowed)}")
        try:
            out = tools[name](args)
        except ToolError as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                                                       "structuredContent": out, "isError": False}}
    return _error(mid, -32601, f"method '{method}' is not supported")
