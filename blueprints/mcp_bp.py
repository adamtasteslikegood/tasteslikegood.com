"""MCP endpoint over the published recipes (KAN-399).

- POST /mcp   One JSON-RPC message (or a batch) in, one JSON response out

The Streamable HTTP transport of the Model Context Protocol, in its smallest
form: no session, no event stream, no server-to-client messages. The tools are
the agent tools (``services.agent_tools``), called through the same access
check as ``POST /api/agent/tools/<name>``.

A caller here is never signed in. The site session cookie is not read, so the
endpoint offers the tools that need no account and no others, whatever the
generation switches say.
"""

import json
import os
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request
from flask.typing import ResponseReturnValue
from jsonschema import Draft7Validator

from blueprints.agent_api_bp import NO_BODY, read_json_body, run_tool
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


# Protocol version 2025-03-26 lets a client send several messages as one JSON
# array; 2025-06-18 took that out. The body size limit already bounds a batch.
BATCH_VERSION = "2025-03-26"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

Reply = dict[str, Any] | None


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _refuse(code: int, message: str, status: int) -> ResponseReturnValue:
    """A refusal of the whole HTTP request: an error with no id."""
    return jsonify(_error(None, code, message)), status


def _tools() -> list[AgentTool]:
    return [tool for tool in enabled_tools(current_app.config) if not tool.requires_sign_in]


def _site(url: str) -> tuple[str, str, int | None]:
    """Scheme, host and port of a URL, with the scheme's default port filled in."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    return scheme, (parts.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme)


def _foreign_origin() -> bool:
    """True for a browser page on another site.

    The transport requires the check, against DNS rebinding: a page that
    points its own name at this server sends that name as Origin and as Host,
    so the Host of the request proves nothing and is not consulted. Allowed
    are the site's configured address and a loopback page (local development).
    A program sends no Origin and is let through; nothing here depends on a
    cookie either way.
    """
    origin = request.headers.get("Origin")
    if origin is None:
        return False
    site = _site(origin)
    if site[1] in LOOPBACK_HOSTS:
        return False
    configured = os.environ.get("FRONTEND_URL", "").strip()
    return not configured or site != _site(configured)


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


def _call_tool(message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return _error(message_id, INVALID_PARAMS, "name and an arguments object are required")
    # Not offered here means unknown here: a tool that needs sign-in is not
    # reachable through this endpoint even by name.
    tool = next((tool for tool in _tools() if tool.name == name), None)
    if tool is None:
        return _error(message_id, INVALID_PARAMS, f"Unknown tool: {name}")
    # Arguments that do not fit the schema the tool list published are the
    # caller's mistake, reported as one, before the tool runs.
    problem = next(Draft7Validator(tool.input_schema).iter_errors(arguments), None)
    if problem is not None:
        return _error(message_id, INVALID_PARAMS, f"Invalid arguments: {problem.message}")

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


def _answer(message: Any, *, in_batch: bool = False) -> Reply:
    """The reply to one message, or None when it asks for none."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message")

    method = message.get("method")
    if not isinstance(method, str):
        # A response from the client. This server asks the client nothing, so
        # there is nothing to match it to; accept it as the transport says.
        if "result" in message or "error" in message:
            return None
        return _error(None, INVALID_REQUEST, "method is required")
    if "id" not in message:
        # A notification (notifications/initialized, cancelled, ...).
        return None
    message_id = message["id"]
    if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
        return _error(None, INVALID_REQUEST, "id must be a string or an integer")

    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(message_id, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        if in_batch:
            return _error(message_id, INVALID_REQUEST, "initialize must not be sent in a batch")
        return _result(message_id, _initialize(params))
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, _list_tools())
    if method == "tools/call":
        return _call_tool(message_id, params)
    return _error(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")


def _malformed(reply: Reply) -> bool:
    """True for a refusal that names no request: it has no id to answer to."""
    return reply is not None and "error" in reply and reply["id"] is None


@mcp_bp.route("/mcp", methods=["POST"])
def mcp() -> ResponseReturnValue:
    """Answer one request body. GET and DELETE get 405: no stream, no session."""
    if _foreign_origin():
        return _refuse(INVALID_REQUEST, "Origin not allowed", 403)
    version = request.headers.get("MCP-Protocol-Version")
    if version is not None and version not in PROTOCOL_VERSIONS:
        return _refuse(INVALID_REQUEST, "Unsupported MCP-Protocol-Version", 400)

    body, refusal = read_json_body()
    if refusal is not None:
        status = current_app.make_response(refusal).status_code
        # 400 is a real JSON parse failure; 413/415 are transport-level
        # refusals that JSON-RPC classifies as invalid-request, not parse.
        code = PARSE_ERROR if status == 400 else INVALID_REQUEST
        return _refuse(code, "The body must be a JSON-RPC message", status)
    if body is NO_BODY:
        return _refuse(PARSE_ERROR, "The body must be a JSON-RPC message", 400)

    if isinstance(body, list):
        # A header naming the newer version rules a batch out; without one the
        # transport says to assume the older version, which allows it.
        if version not in (None, BATCH_VERSION) or not body:
            return _refuse(INVALID_REQUEST, "A batch is not accepted here", 400)
        replies = [reply for message in body if (reply := _answer(message, in_batch=True))]
        return (jsonify(replies), 200) if replies else ("", 202)

    reply = _answer(body)
    if reply is None:
        return "", 202
    return jsonify(reply), 400 if _malformed(reply) else 200
