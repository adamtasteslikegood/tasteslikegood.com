"""Tests for the recipe metadata engine (KAN-388)."""

import base64
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from blueprints import worker_api_bp as worker_module  # noqa: E402
from extensions import cache, db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.recipe_metadata import RecipeMetadata  # noqa: E402
from services import recipe_metadata as engine  # noqa: E402

LONG_SENTENCE = (
    "A hearty, artisan vegan cornbread crafted with a heritage blend of three organic "
    "flours, sweet organic creamed corn, and baked in a sizzling cast iron skillet for a "
    "golden, crisp crust."
)
GOOD_ANSWER = json.dumps(
    {
        "title": "Three-Grain Vegan Cornbread Recipe",
        "description": (
            "A hearty vegan cornbread made with three organic flours and creamed corn, "
            "baked in a cast iron skillet for a golden, crisp crust."
        ),
    }
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    monkeypatch.setattr(worker_module, "PUBSUB_AUTH_OPTIONAL", True)
    app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        WTF_CSRF_ENABLED=False,
    )
    with app.app_context():
        db.create_all()
        cache.clear()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def no_model(monkeypatch):
    monkeypatch.setattr(engine, "get_genai_client", lambda _credentials: None)


def _model_answers(monkeypatch, answer):
    monkeypatch.setattr(engine, "get_genai_client", lambda _credentials: object())

    def generate(_client, _model, _prompt):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(engine, "attempt_generation", generate)


def _recipe(name="Vegan Polenta Fries", description=LONG_SENTENCE, is_public=True):
    recipe = Recipe(
        id=str(uuid.uuid4()),
        name=name,
        slug=f"r-{uuid.uuid4().hex[:8]}",
        is_public=is_public,
        data={"name": name, "description": description, "tags": ["vegan", "cornbread"]},
    )
    db.session.add(recipe)
    db.session.commit()
    return recipe


def _push(client, recipe_id):
    data = base64.b64encode(json.dumps({"recipe_id": recipe_id}).encode()).decode()
    return client.post("/api/worker/metadata", json={"message": {"data": data}})


def _versions(recipe):
    return (
        RecipeMetadata.query.filter_by(recipe_id=recipe.id).order_by(RecipeMetadata.version).all()
    )


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Wild Plum Syrup", "Wild Plum Syrup Recipe · Tasteslikegood.org"),
        # 45 characters: "Recipe" fits, the site name does not.
        (
            "Vegan Baked Ziti with Roasted Summer Squashes",
            "Vegan Baked Ziti with Roasted Summer Squashes Recipe",
        ),
        (
            "Grandma's Recipe for Lentil Soup",
            "Grandma's Recipe for Lentil Soup · Tasteslikegood.org",
        ),
        (
            "Vegan Double Chocolate Chip Cookies with Baked-On Chocolate Drizzle",
            "Vegan Double Chocolate Chip Cookies with Baked-On Chocolate Drizzle",
        ),
    ],
)
def test_rule_title_adds_recipe_and_site_name_within_the_limit(name, expected):
    title = engine.rule_title(name)

    assert title == expected
    assert len(title) <= engine.MAX_TITLE_LENGTH or title == name


def test_rule_description_ends_a_long_sentence_at_a_clause():
    description = engine.rule_description(LONG_SENTENCE)

    assert description == (
        "A hearty, artisan vegan cornbread crafted with a heritage blend of three organic "
        "flours, sweet organic creamed corn."
    )
    assert len(description) <= engine.MAX_DESCRIPTION_LENGTH


def test_rule_description_keeps_whole_sentences_and_short_text():
    two = "A quick weeknight curry with chickpeas and spinach. " + "It reheats well. " * 12

    assert engine.rule_description("Short and sweet.") == "Short and sweet."
    assert engine.rule_description(two).endswith("It reheats well.")
    assert "…" not in engine.rule_description(two)


def test_build_without_a_model_saves_the_rule_version(app, no_model):
    recipe = _recipe()

    row = engine.build_metadata(recipe)

    assert (row.source, row.version, row.model) == ("rule", 1, None)
    assert row.seo_title == "Vegan Polenta Fries Recipe · Tasteslikegood.org"
    assert row.inputs["name"] == "Vegan Polenta Fries"
    # A second run with unchanged content writes nothing.
    engine.build_metadata(recipe)
    assert len(_versions(recipe)) == 1


def test_build_with_a_valid_model_answer_adds_a_model_version(app, monkeypatch):
    recipe = _recipe()
    _model_answers(monkeypatch, GOOD_ANSWER)

    row = engine.build_metadata(recipe)

    assert [v.source for v in _versions(recipe)] == ["rule", "model"]
    assert row.seo_title == "Three-Grain Vegan Cornbread Recipe · Tasteslikegood.org"
    assert row.meta_description.endswith("crisp crust.")
    engine.build_metadata(recipe)
    assert len(_versions(recipe)) == 2


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        json.dumps({"title": "Three-Grain Vegan Cornbread", "description": "x" * 120 + "."}),
        json.dumps({"title": "Cornbread Recipe " + "x" * 60, "description": "x" * 120 + "."}),
        json.dumps({"title": "Cornbread Recipe", "description": "Too short."}),
        json.dumps({"title": "Cornbread Recipe", "description": "x" * 120 + "…"}),
        # "30" is in neither the name, the description nor the tags.
        json.dumps({"title": "30-Minute Cornbread Recipe", "description": "x" * 120 + "."}),
        # The recipe never says it is gluten-free.
        json.dumps({"title": "Gluten-Free Cornbread Recipe", "description": "x" * 120 + "."}),
        json.dumps(
            {"title": "Cornbread Recipe", "description": "Safe for allergies " + "x" * 110 + "."}
        ),
    ],
)
def test_an_invalid_model_answer_keeps_the_rule_version(app, monkeypatch, answer):
    recipe = _recipe()
    _model_answers(monkeypatch, answer)

    row = engine.build_metadata(recipe)

    assert row.source == "rule"
    assert [v.source for v in _versions(recipe)] == ["rule"]


def test_an_edited_recipe_gets_a_new_version_and_a_persons_version_is_kept(app, no_model):
    recipe = _recipe()
    engine.build_metadata(recipe)
    recipe.name = "Crispy Polenta Fries"
    db.session.commit()

    assert engine.metadata_for_page(recipe) is None
    engine.build_metadata(recipe)
    assert [v.version for v in _versions(recipe)] == [1, 2]
    assert engine.metadata_for_page(recipe).seo_title.startswith("Crispy Polenta Fries Recipe")

    db.session.add(
        RecipeMetadata(
            recipe_id=recipe.id,
            version=3,
            seo_title="Hand-written title",
            meta_description="Hand-written description.",
            source="human",
            inputs_hash="",
            inputs={},
        )
    )
    db.session.commit()
    engine.build_metadata(recipe)
    assert engine.metadata_for_page(recipe).seo_title == "Hand-written title"
    assert len(_versions(recipe)) == 3


def test_page_uses_metadata_and_falls_back_without_it(app, client, no_model, no_metadata_queue):
    recipe = _recipe()

    before = client.get(f"/r/{recipe.slug}").get_data(as_text=True)
    assert "<title>Vegan Polenta Fries · Tasteslikegood.org</title>" in before
    assert no_metadata_queue == [("recipe-metadata", {"recipe_id": recipe.id})]

    engine.build_metadata(recipe)
    after = client.get(f"/r/{recipe.slug}").get_data(as_text=True)
    assert "<title>Vegan Polenta Fries Recipe · Tasteslikegood.org</title>" in after
    assert 'content="A hearty, artisan vegan cornbread' in after
    assert "sweet organic creamed corn." in after
    # Built and current: nothing more is queued, and the throttle held before that.
    assert len(no_metadata_queue) == 1


def test_queue_is_throttled_and_never_raises(app, monkeypatch):
    recipe = _recipe()
    calls = []

    def fail(topic, data, timeout=None):
        calls.append((topic, timeout))
        raise RuntimeError("topic missing")

    monkeypatch.setattr(engine, "publish_message", fail)

    engine.queue_metadata(recipe)
    engine.queue_metadata(recipe)

    # One bounded attempt: a queue outage must not hold a page for minutes.
    assert calls == [("recipe-metadata", engine.QUEUE_PUBLISH_TIMEOUT_SECONDS)]


def test_queue_is_skipped_when_the_cache_cannot_throttle(app, monkeypatch, no_metadata_queue):
    monkeypatch.setattr(engine, "safe_add", lambda _key, _value, timeout=None: False)

    engine.queue_metadata(_recipe())

    assert no_metadata_queue == []


@pytest.mark.parametrize("meanwhile", ["human", "edited"])
def test_a_model_answer_does_not_replace_what_was_written_during_the_call(
    app, monkeypatch, meanwhile
):
    recipe = _recipe()
    monkeypatch.setattr(engine, "get_genai_client", lambda _credentials: object())

    def generate(_client, _model, _prompt):
        if meanwhile == "human":
            db.session.add(
                RecipeMetadata(
                    recipe_id=recipe.id,
                    version=2,
                    seo_title="Hand-written title",
                    meta_description="Hand-written description.",
                    source="human",
                    inputs_hash="",
                    inputs={},
                )
            )
        else:
            recipe.name = "Crispy Polenta Fries"
        db.session.commit()
        return GOOD_ANSWER

    monkeypatch.setattr(engine, "attempt_generation", generate)

    engine.build_metadata(recipe)

    assert "model" not in [v.source for v in _versions(recipe)]


def test_worker_builds_for_public_recipes_only(app, client, no_model):
    public, private = _recipe(), _recipe(is_public=False)

    assert _push(client, public.id).status_code == 200
    assert _push(client, private.id).status_code == 200
    assert _push(client, "missing").status_code == 200

    assert len(_versions(public)) == 1
    assert _versions(private) == []


def test_worker_asks_for_redelivery_when_the_model_call_fails(app, client, monkeypatch):
    recipe = _recipe()
    _model_answers(monkeypatch, RuntimeError("upstream timeout"))

    assert _push(client, recipe.id).status_code == 500
    # The rule version is saved before the model is tried.
    assert [v.source for v in _versions(recipe)] == ["rule"]

    _model_answers(monkeypatch, GOOD_ANSWER)
    assert _push(client, recipe.id).status_code == 200
    assert [v.source for v in _versions(recipe)] == ["rule", "model"]


def test_a_free_from_claim_needs_the_recipe_to_state_it_without_negation():
    answer = json.dumps({"title": "Nut-Free Curry Recipe", "description": "x" * 120 + "."})

    def inputs(description):
        return {"name": "Cashew Curry", "description": description, "tags": []}

    assert engine._parse_model_output(answer, inputs("A mild, nut-free curry.")) is not None
    assert engine._parse_model_output(answer, inputs("Not nut-free: has cashews.")) is None


def test_a_late_stale_build_does_not_hide_newer_metadata(app, no_model):
    recipe = _recipe()
    engine.build_metadata(recipe)
    current = engine.metadata_for_page(recipe)
    # A build from content the recipe no longer has, saved afterwards.
    db.session.add(
        RecipeMetadata(
            recipe_id=recipe.id,
            version=2,
            seo_title="Stale",
            meta_description="Stale.",
            source="rule",
            inputs_hash="older-content",
            inputs={},
        )
    )
    db.session.commit()

    assert engine.metadata_for_page(recipe).id == current.id


def test_a_rule_version_saved_after_a_model_version_does_not_replace_it(app, monkeypatch):
    recipe = _recipe()
    _model_answers(monkeypatch, GOOD_ANSWER)
    model_row = engine.build_metadata(recipe)
    db.session.add(
        RecipeMetadata(
            recipe_id=recipe.id,
            version=3,
            seo_title="Late rule",
            meta_description="Late rule.",
            source="rule",
            inputs_hash=model_row.inputs_hash,
            inputs={},
        )
    )
    db.session.commit()

    assert engine.metadata_for_page(recipe).id == model_row.id


def test_worker_does_not_ask_for_redelivery_when_the_model_refuses_the_request(
    app, client, monkeypatch
):
    from google.genai.errors import ClientError

    recipe = _recipe()
    _model_answers(monkeypatch, ClientError(400, {"error": {"message": "bad model"}}))
    assert _push(client, recipe.id).status_code == 200

    _model_answers(monkeypatch, ClientError(429, {"error": {"message": "slow down"}}))
    assert _push(client, recipe.id).status_code == 500
    assert [v.source for v in _versions(recipe)] == ["rule"]
