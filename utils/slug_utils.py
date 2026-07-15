"""Shared route-safe slug normalization."""

import re
import unicodedata


def normalize_slug(value: str) -> str:
    """Return a lowercase ASCII slug without leading or trailing separators."""
    ascii_value = (
        unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    )
    normalized = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower().strip())
    return normalized.strip("-")
