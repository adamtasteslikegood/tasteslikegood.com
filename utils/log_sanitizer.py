"""Helpers for keeping untrusted values from forging structured log lines."""

import re

_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_log_value(value, max_length=512):
    """Escape control characters and cap untrusted values before logging."""
    text = str(value)
    escaped = _CONTROL_CHARACTER.sub(
        lambda match: f"\\x{ord(match.group(0)):02x}",
        text,
    )
    return escaped[:max_length]
