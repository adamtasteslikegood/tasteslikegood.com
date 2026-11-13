"""Reading a JSON request body in the write routes (KAN-426)."""

from typing import Any

from flask import jsonify, request
from flask.typing import ResponseReturnValue

JSON_OBJECT_REQUIRED_ERROR = "Request body must be a JSON object"


def json_object_body() -> tuple[dict[str, Any], ResponseReturnValue | None]:
    """The request body as a JSON object, or the 400 to send back.

    ``get_json()`` raises for a malformed body or a non-JSON content type, and a
    route's broad ``except Exception`` turned that into a 500. ``silent`` makes
    both ``None`` instead. No ``force``: the body must still be sent as
    application/json, which a browser cannot do across origins without a
    preflight.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {}, (jsonify({"error": JSON_OBJECT_REQUIRED_ERROR}), 400)
    return data, None
