"""Search metadata for published recipes (KAN-388).

A pass that runs on the server after a recipe is public. It writes the page
title and meta description to ``recipe_metadata`` and never touches the
generation prompt or the generated recipe. Rules produce a safe baseline; a
model rewrite replaces it when its output passes the same limits.

Pages read the newest version whose inputs still match the recipe, and fall
back to plain truncation when there is none.
"""

import hashlib
import json
import logging
import re
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from config import DEFAULT_MODEL
from extensions import db
from models.recipe import Recipe
from models.recipe_metadata import RecipeMetadata
from services.gemini_service import attempt_generation, get_genai_client
from services.pubsub_service import publish_message
from utils.cache_utils import recipe_metadata_queued_key, safe_get, safe_set
from utils.log_sanitizer import sanitize_log_value

logger = logging.getLogger(__name__)

METADATA_TOPIC = "recipe-metadata"
# Part of the inputs hash: bump it when the rules or the prompt change, and
# every recipe's current version goes stale and is rebuilt.
ENGINE_VERSION = 1
# One queue message per recipe and content version in this window.
QUEUE_THROTTLE_SECONDS = 3600
# Queueing runs inside page renders and saves; it must not hold them up.
QUEUE_PUBLISH_TIMEOUT_SECONDS = 2

SITE_TITLE_SUFFIX = " · Tasteslikegood.org"
MAX_TITLE_LENGTH = 60
MAX_DESCRIPTION_LENGTH = 155
MIN_MODEL_DESCRIPTION_LENGTH = 70
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_CLAUSE_END = re.compile(r"[,;:](?=\s)")
_DANGLING_WORD = re.compile(r"\s+(?:and|or|with|then|plus|but)$", re.IGNORECASE)
_NUMBER = re.compile(r"\d+")
_FREE_FROM = re.compile(r"\b[a-z]+[- ]free\b")


def truncate_description(text: str) -> str:
    """``text`` cut to fit a result snippet: at the last sentence end, else a word."""
    text = " ".join(text.split())
    if len(text) <= MAX_DESCRIPTION_LENGTH:
        return text
    head = text[: MAX_DESCRIPTION_LENGTH + 1]
    ends = [m.end() for m in _SENTENCE_END.finditer(head) if m.end() <= MAX_DESCRIPTION_LENGTH]
    if ends and ends[-1] >= 60:
        return head[: ends[-1]]
    cut = head[: MAX_DESCRIPTION_LENGTH - 1].rsplit(" ", 1)[0].rstrip(" ,;:-–—")
    return f"{cut}…"


def rule_title(name: str) -> str:
    """The recipe name with "Recipe" and the site name, as far as 60 characters allow."""
    name = " ".join(name.split())
    with_keyword = name if "recipe" in name.lower() else f"{name} Recipe"
    for candidate in (
        f"{with_keyword}{SITE_TITLE_SUFFIX}",
        with_keyword,
        f"{name}{SITE_TITLE_SUFFIX}",
    ):
        if len(candidate) <= MAX_TITLE_LENGTH:
            return candidate
    return name


def rule_description(text: str) -> str:
    """Like ``truncate_description``, but a long first sentence ends at a clause, not "…"."""
    cut = truncate_description(text)
    if not cut.endswith("…"):
        return cut
    head = " ".join(text.split())[:MAX_DESCRIPTION_LENGTH]
    clause_ends = [m.start() for m in _CLAUSE_END.finditer(head) if m.start() >= 90]
    if not clause_ends:
        return cut
    return _DANGLING_WORD.sub("", head[: clause_ends[-1]].rstrip()) + "."


def metadata_inputs(recipe: Recipe) -> dict[str, Any]:
    data = recipe.data or {}
    description = data.get("description")
    tags = data.get("tags")
    return {
        "name": recipe.name or "",
        "description": description if isinstance(description, str) else "",
        "tags": (
            [t.strip() for t in tags if isinstance(t, str) and t.strip()]
            if isinstance(tags, list)
            else []
        ),
    }


def inputs_hash(inputs: dict[str, Any]) -> str:
    payload = json.dumps({"engine": ENGINE_VERSION, "inputs": inputs}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _latest(recipe_id: str) -> Optional[RecipeMetadata]:
    row: Optional[RecipeMetadata] = (
        RecipeMetadata.query.filter_by(recipe_id=recipe_id)
        .order_by(RecipeMetadata.version.desc())
        .first()
    )
    return row


def _current(recipe: Recipe) -> Optional[RecipeMetadata]:
    """The version a page may use: a person's, or one built from today's content."""
    latest = _latest(recipe.id)
    if latest is None:
        return None
    if latest.source == "human" or latest.inputs_hash == inputs_hash(metadata_inputs(recipe)):
        return latest
    return None


def queue_metadata(recipe: Recipe) -> None:
    """Ask the worker to build ``recipe``'s metadata. Never raises.

    Throttled per recipe and content version, so a page that renders often or a
    recipe that saves often queues once, and a queue outage costs one failed
    publish per window rather than one per request.
    """
    try:
        key = recipe_metadata_queued_key(recipe.id, inputs_hash(metadata_inputs(recipe)))
        if safe_get(key):
            return
        safe_set(key, 1, timeout=QUEUE_THROTTLE_SECONDS)
        if not safe_get(key):
            # No working cache means no throttle; skip rather than publish per request.
            return
        publish_message(
            METADATA_TOPIC, {"recipe_id": recipe.id}, timeout=QUEUE_PUBLISH_TIMEOUT_SECONDS
        )
    except Exception as e:
        logger.warning(
            "Could not queue metadata for recipe %s: %s",
            sanitize_log_value(recipe.id),
            sanitize_log_value(e),
        )


def metadata_for_page(recipe: Recipe) -> Optional[RecipeMetadata]:
    """Current metadata for a public page, queueing a build when there is none."""
    current = _current(recipe)
    if current is None:
        queue_metadata(recipe)
    return current


def queue_if_missing(recipe: Recipe) -> None:
    """After a save: queue a build when a public recipe has no current metadata."""
    try:
        if recipe.is_public and _current(recipe) is None:
            queue_metadata(recipe)
    except Exception as e:
        db.session.rollback()
        logger.warning(
            "Could not check metadata for recipe %s: %s",
            sanitize_log_value(recipe.id),
            sanitize_log_value(e),
        )


def _model_prompt(inputs: dict[str, Any]) -> str:
    return (
        "Write the search-result title and meta description for this vegan recipe page.\n"
        f"Recipe name: {inputs['name']}\n"
        f"Recipe description: {inputs['description']}\n"
        f"Tags: {', '.join(inputs['tags'])}\n"
        "Rules:\n"
        f"- title: at most {MAX_TITLE_LENGTH - len(SITE_TITLE_SUFFIX)} characters if you can, "
        f"never more than {MAX_TITLE_LENGTH}. It names the dish plainly and contains the word "
        '"Recipe". No site name, no quotes, no emoji.\n'
        f"- description: {MIN_MODEL_DESCRIPTION_LENGTH + 40} to {MAX_DESCRIPTION_LENGTH} "
        "characters, complete sentences ending in a full stop. Say what the dish is and what "
        "makes it worth cooking. Do not end with an ellipsis.\n"
        "- Use only facts stated above. Do not add times, quantities, ingredients or claims "
        "that are not there. Make no dietary, allergen or health claim beyond what is stated.\n"
        'Return only a JSON object: {"title": "...", "description": "..."}'
    )


def _parse_model_output(text: str, inputs: dict[str, Any]) -> Optional[tuple[str, str]]:
    """(title, description) when the model's answer passes every limit, else None."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    title, description = parsed.get("title"), parsed.get("description")
    if not isinstance(title, str) or not isinstance(description, str):
        return None
    title = " ".join(title.split())
    description = " ".join(description.split())
    if not title or len(title) > MAX_TITLE_LENGTH or "recipe" not in title.lower():
        return None
    if "tasteslikegood" in title.lower():
        return None
    if not MIN_MODEL_DESCRIPTION_LENGTH <= len(description) <= MAX_DESCRIPTION_LENGTH:
        return None
    if description[-1] not in ".!?" or "…" in description or description.endswith("..."):
        return None
    # A number the recipe never mentioned is an invented time or quantity.
    source_text = " ".join([inputs["name"], inputs["description"], *inputs["tags"]])
    known = set(_NUMBER.findall(source_text))
    if any(n not in known for n in _NUMBER.findall(f"{title} {description}")):
        return None
    # Likewise a "nut-free" or "gluten free" the recipe never claimed.
    stated = {c.replace("-", " ") for c in _FREE_FROM.findall(source_text.lower())}
    claimed = _FREE_FROM.findall(f"{title} {description}".lower())
    if any(c.replace("-", " ") not in stated for c in claimed):
        return None
    if len(f"{title}{SITE_TITLE_SUFFIX}") <= MAX_TITLE_LENGTH:
        title = f"{title}{SITE_TITLE_SUFFIX}"
    return title, description


def build_metadata(recipe: Recipe, *, use_model: bool = True) -> Optional[RecipeMetadata]:
    """Write a new metadata version for ``recipe`` when its current one is missing or weaker.

    The rule output is always saved first when it is new, so a failed model
    call still leaves the page better than truncation. A model call that raises
    is re-raised after that save, so the queue retries it; an answer that fails
    validation is not retried.
    """
    inputs = metadata_inputs(recipe)
    digest = inputs_hash(inputs)
    latest = _latest(recipe.id)
    fresh = latest is not None and latest.inputs_hash == digest
    if latest is not None and (latest.source == "human" or (fresh and latest.source == "model")):
        return latest

    def save(title: str, description: str, source: str, model: Optional[str]) -> RecipeMetadata:
        newest = _latest(recipe.id)
        row = RecipeMetadata(
            recipe_id=recipe.id,
            version=(newest.version + 1) if newest else 1,
            seo_title=title,
            meta_description=description,
            source=source,
            model=model,
            inputs_hash=digest,
            inputs=inputs,
        )
        db.session.add(row)
        try:
            db.session.commit()
        except IntegrityError:
            # Another delivery of the same message wrote this version first.
            db.session.rollback()
            return _latest(recipe.id) or row
        return row

    if not fresh:
        latest = save(
            rule_title(inputs["name"]),
            rule_description(inputs["description"]) if inputs["description"] else "",
            "rule",
            None,
        )
    if not use_model:
        return latest
    client = get_genai_client(None)
    if client is None:
        return latest
    # End the read transaction: the model call can take minutes and must not
    # hold a pooled database connection while it does.
    db.session.commit()
    answer = _parse_model_output(
        attempt_generation(client, DEFAULT_MODEL, _model_prompt(inputs)), inputs
    )
    if answer is None:
        logger.info(
            "Model metadata for recipe %s failed validation; keeping the rule version",
            sanitize_log_value(recipe.id),
        )
        return latest
    # The model call takes seconds. Whatever was written meanwhile wins: a
    # person's version, or a build from content the recipe has since moved to.
    db.session.refresh(recipe)
    newest = _latest(recipe.id)
    if inputs_hash(metadata_inputs(recipe)) != digest or (
        newest is not None and (newest.source != "rule" or newest.inputs_hash != digest)
    ):
        return newest
    return save(answer[0], answer[1], "model", DEFAULT_MODEL)
