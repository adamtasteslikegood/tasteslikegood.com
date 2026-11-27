"""The site's own author name on public recipes (KAN-434).

Recipes owned by the site account (user id 1, Adam) show
``"Tasteslikegood - {suffix}"`` instead of the Google account name. Everyone
else keeps their own ``user.name``.

The name is derived here, never stored, so the Google sign-in that rewrites
``user.name`` (``services.oauth_flow.upsert_authenticated_user``) cannot
revert it.

This is the one place that decides who the brand author is and what they are
called. Anything that shows a recipe's author (the public byline, Recipe
JSON-LD, and the recipe-feedback "From the author" section) should call
``author_display_name`` rather than read ``user.name`` or test ``user_id == 1``.

Both values come from the environment at call time so tests and staging can
override them:

- ``BRAND_AUTHOR_USER_IDS``: comma-separated user ids, default ``"1"``.
- ``BRAND_AUTHOR_SUFFIX``: default ``"classic"``. Set it empty for plain
  ``"Tasteslikegood"``.
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

BRAND_AUTHOR_NAME = "Tasteslikegood"
DEFAULT_BRAND_AUTHOR_USER_IDS = "1"
DEFAULT_BRAND_AUTHOR_SUFFIX = "classic"


def brand_author_user_ids() -> frozenset[int]:
    """The user ids whose recipes carry the brand name."""
    raw = os.getenv("BRAND_AUTHOR_USER_IDS", DEFAULT_BRAND_AUTHOR_USER_IDS)
    ids = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdigit():
            # A typo in the env var must not take down every public page.
            logger.warning("BRAND_AUTHOR_USER_IDS: skipping non-numeric id %r", token)
            continue
        ids.add(int(token))
    return frozenset(ids)


def is_brand_author(user_id: int | None) -> bool:
    """True when ``user_id`` is the site's own account."""
    return user_id is not None and user_id in brand_author_user_ids()


def brand_author_name() -> str:
    """``"Tasteslikegood - {suffix}"``, or ``"Tasteslikegood"`` with no suffix."""
    suffix = os.getenv("BRAND_AUTHOR_SUFFIX", DEFAULT_BRAND_AUTHOR_SUFFIX).strip()
    return f"{BRAND_AUTHOR_NAME} - {suffix}" if suffix else BRAND_AUTHOR_NAME


def author_display_name(user: Any) -> str | None:
    """The name to show for ``user`` as a recipe author, or ``None`` for none."""
    if user is None:
        return None
    if is_brand_author(getattr(user, "id", None)):
        return brand_author_name()
    name: str | None = getattr(user, "name", None) or None
    return name
