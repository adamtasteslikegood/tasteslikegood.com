"""Shared pytest setup.

Disable the Datadog tracer for the test run. Application code opens manual
spans (KAN-268); with tracing on, the writer tries to flush them to a local
agent that does not exist and prints "failed to send, dropping N traces" at
exit. ``tracer.trace()`` still returns a usable span when disabled.

Setting ``DD_TRACE_ENABLED`` here would be too late: the ddtrace pytest plugin
imports ddtrace, and reads its config, before any conftest runs.
"""

import pytest
from ddtrace.trace import tracer

tracer.enabled = False


@pytest.fixture
def mark_generated():
    """Stamp a row the way the worker's text write does (KAN-329).

    Since the fix, a client cannot label a row ``generated`` and a new row is
    never created public, so a test that needs a publishable recipe creates
    it private through the normal path and then calls this. Writing the
    stamp through the ORM mirrors ``update_recipe_for_worker``; the row is
    then publishable via the ordinary ``is_public: true`` save.
    """
    from extensions import db
    from models.recipe import Recipe

    def _mark(recipe_id, status="ready"):
        row = db.session.get(Recipe, recipe_id)
        assert row is not None, recipe_id
        row.origin = "generated"
        row.status = status
        row.data = {**(row.data or {}), "origin": "generated"}
        db.session.commit()
        return row

    return _mark
