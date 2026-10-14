"""MCP endpoint over the published recipes (KAN-399).

- POST /mcp   One JSON-RPC message in, one JSON response out

The Streamable HTTP transport of the Model Context Protocol, in its smallest
form: no session, no event stream, no server-to-client messages. The tools are
the agent tools (``services.agent_tools``), called through the same access
check as ``POST /api/agent/tools/<name>``.

A caller here is never signed in. The site session cookie is not read, so the
endpoint offers the tools that need no account and no others, whatever the
generation switches say.
"""

import json
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request
from flask.typing import ResponseReturnValue

from blueprints.agent_api_bp import read_arguments, run_tool
from blueprints.public_bp import _public_base_url
from services.agent_tools import ATTRIBUTION, AgentTool, enabled_tools

mcp_bp = Blueprint("mcp", __name__)

# Newest first. A client that asks for one of these gets it back; any other
# request is answered with the newest.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
SERVER_INFO = {
    "name": "tasteslikegood-recipes",
    "title": "Tasteslikegood.org recipes",
    "version": "1.0.0",
}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def _result(message_id: Any, result: dict[str, Any]) -> ResponseReturnValue:
    return jsonify({"jsonrpc": "2.0", "id": message_id, "result": result})


def _rpc_error(message_id: Any, code: int, message: str, status: int = 200) -> ResponseReturnValue:
    body = {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}
    return jsonify(body), status


def _tools() -> list[AgentTool]:
    return [tool for tool in enabled_tools(current_app.config) if not tool.requires_sign_in]


def _canonical_netloc(scheme: str, netloc: str) -> str:
    """Lowercase netloc with the scheme's default port stripped."""
    netloc = netloc.lower()
    default = {"http": ":80", "https": ":443"}.get(scheme.lower())
    if default and netloc.endswith(default):
        return netloc[: -len(default)]
    return netloc


def _foreign_origin() -> bool:
    """True for a browser page on another site.

    The transport requires the check. A program sends no Origin and is let
    through; nothing here depends on a cookie either way.
    """
    origin = request.headers.get("Origin")
    if origin is None:
        return False
    origin_parts = urlsplit(origin)
    public_parts = urlsplit(_public_base_url())
    # Default ports differ between the Origin header (bare host) and
    # request.host when a proxy forwards an explicit :80/:443; strip both.
    allowed = {
        _canonical_netloc(public_parts.scheme, public_parts.netloc),
        _canonical_netloc(request.scheme, request.host),
    }
    return _canonical_netloc(origin_parts.scheme, origin_parts.netloc) not in allowed


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {}},
        "serverInfo": SERVER_INFO,
        "instructions": (
            "Search and read the published vegan recipes on Tasteslikegood.org. " + ATTRIBUTION
        ),
    }


def _list_tools() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in _tools()
        ]
    }


def _call_tool(message_id: Any, params: dict[str, Any]) -> ResponseReturnValue:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return _rpc_error(message_id, INVALID_PARAMS, "name and an arguments object are required")
    # Not offered here means unknown here: a tool that needs sign-in is not
    # reachable through this endpoint even by name.
    if name not in {tool.name for tool in _tools()}:
        return _rpc_error(message_id, INVALID_PARAMS, f"Unknown tool: {name}")

    response = current_app.make_response(run_tool(name, arguments, None))
    payload = response.get_json(silent=True)
    if response.status_code >= 400:
        message = payload.get("error") if isinstance(payload, dict) else None
        return _result(
            message_id,
            {"content": [{"type": "text", "text": message or "The tool failed"}], "isError": True},
        )
    return _result(
        message_id,
        {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": payload,
            "isError": False,
        },
    )


@mcp_bp.route("/mcp", methods=["POST"])
def mcp() -> ResponseReturnValue:
    """Answer one JSON-RPC message. GET and DELETE get 405: no stream, no session."""
    if _foreign_origin():
        return _rpc_error(None, INVALID_REQUEST, "Origin not allowed", 403)
    version = request.headers.get("MCP-Protocol-Version")
    if version is not None and version not in PROTOCOL_VERSIONS:
        return _rpc_error(None, INVALID_REQUEST, "Unsupported MCP-Protocol-Version", 400)

    message, refusal = read_arguments()
    if refusal is not None:
        status = current_app.make_response(refusal).status_code
        # 400 is a real JSON parse failure; 413/415 are transport-level
        # refusals that JSON-RPC classifies as invalid-request, not parse.
        code = PARSE_ERROR if status == 400 else INVALID_REQUEST
        return _rpc_error(None, code, "The body must be one JSON-RPC message", status)
    if message.get("jsonrpc") != "2.0":
        return _rpc_error(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message", 400)

    method = message.get("method")
    message_id = message.get("id")
    if not isinstance(method, str):
        # A response from the client. This server asks the client nothing, so
        # there is nothing to match it to; accept it as the transport says.
        if "result" in message or "error" in message:
            return "", 202
        return _rpc_error(None, INVALID_REQUEST, "method is required", 400)
    if "id" not in message:
        # A notification (notifications/initialized, cancelled, ...).
        return "", 202
    if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
        return _rpc_error(None, INVALID_REQUEST, "id must be a string or an integer", 400)

    params = message.get("params", {})
    if not isinstance(params, dict):
        return _rpc_error(message_id, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        return _result(message_id, _initialize(params))
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, _list_tools())
    if method == "tools/call":
        return _call_tool(message_id, params)
    return _rpc_error(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")
