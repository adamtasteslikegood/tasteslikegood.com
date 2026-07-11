"""Shared admin bearer-token check for privileged HTTP endpoints."""

import os

from flask import jsonify, request


def require_admin():
    """Check for admin bearer token (ADMIN_API_TOKEN). Returns error response or None.

    Fails closed: when ADMIN_API_TOKEN is unset, every request is rejected.
    """
    admin_key = os.environ.get("ADMIN_API_TOKEN", "")
    auth_header = request.headers.get("Authorization", "")
    if not admin_key or not auth_header.startswith("Bearer ") or auth_header[7:] != admin_key:
        return jsonify({"error": "Unauthorized — admin token required"}), 403
    return None
