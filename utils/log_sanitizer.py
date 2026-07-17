"""Helpers for keeping untrusted values from forging structured log lines."""

import re

_LINE_BREAKING_CHARACTER = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def _escape_character(match):
    codepoint = ord(match.group(0))
    if codepoint <= 0xFF:
        return f"\\x{codepoint:02x}"
    return f"\\u{codepoint:04x}"


def sanitize_log_value(value, max_length=512):
    """Escape control characters and cap untrusted values before logging."""
    text = str(value)
    escaped = _LINE_BREAKING_CHARACTER.sub(_escape_character, text)
    # Runtime no-op (the regex above already escaped every newline variant),
    # but CodeQL's py/log-injection taint tracking only recognizes
    # str.replace-based newline stripping as a sanitizer, so flows through
    # this helper are otherwise still reported (e.g. alerts on
    # worker_api_bp/db_recipe_repository call sites that already sanitize).
    escaped = escaped.replace("\r\n", "").replace("\r", "").replace("\n", "")
    return escaped[:max_length]
