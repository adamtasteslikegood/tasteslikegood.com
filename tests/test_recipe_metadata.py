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
        "title": "Hearty Vegan Cornbread Recipe",
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
    assert row.seo_title == "Hearty Vegan Cornbread Recipe · Tasteslikegood.org"
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
    # One more message: a rule version asks for the model rewrite again, once a day.
    assert len(no_metadata_queue) == 2
    client.get(f"/r/{recipe.slug}")
    assert len(no_metadata_queue) == 2


def test_queue_is_throttled_and_never_raises(app, monkeypatch):
    recipe = _recipe()
    calls = []

    def fail(topic, data, timeout=None):
        calls.append((topic, timeout))
        raise RuntimeError("topic missing")

    monkeypatch.setattr(engine, "publish_message", fail)

    digest = engine.inputs_hash(engine.metadata_inputs(recipe))
    backoffs = []
    monkeypatch.setattr(engine, "safe_set", lambda key, _v, timeout=None: backoffs.append(timeout))
    engine.queue_metadata(recipe.id, digest)
    engine.queue_metadata(recipe.id, digest)

    # One bounded attempt, and the failure shortens the wait before the next one.
    assert calls == [("recipe-metadata", engine.QUEUE_PUBLISH_TIMEOUT_SECONDS)]
    assert backoffs == [engine.QUEUE_FAILED_BACKOFF_SECONDS]


def test_queue_is_skipped_when_the_cache_cannot_throttle(app, monkeypatch, no_metadata_queue):
    monkeypatch.setattr(engine, "safe_add", lambda _key, _value, timeout=None: False)

    recipe = _recipe()
    engine.queue_metadata(recipe.id, engine.inputs_hash(engine.metadata_inputs(recipe)))

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


def test_a_free_from_claim_must_be_one_of_the_recipes_tags():
    def answer(title):
        return json.dumps({"title": title, "description": "x" * 120 + "."})

    def inputs(description, tags):
        return {"name": "Cashew Curry", "description": description, "tags": tags}

    nut_free = answer("Nut-Free Curry Recipe")
    assert engine._parse_model_output(nut_free, inputs("A mild curry.", ["Nut Free"])) is not None
    assert engine._parse_model_output(nut_free, inputs("A mild, nut-free curry.", [])) is None
    assert engine._parse_model_output(nut_free, inputs("Cannot be called nut-free.", [])) is None
    # U+2011, a non-breaking hyphen.
    assert engine._parse_model_output(answer("Nut\u2011Free Curry Recipe"), inputs("", [])) is None
    assert (
        engine._parse_model_output(answer("Curry Recipe That Cures Colds"), inputs("", [])) is None
    )


def test_a_page_on_its_rule_version_asks_for_the_model_again_once_a_day(
    app, client, no_model, no_metadata_queue, monkeypatch
):
    recipe = _recipe()
    engine.build_metadata(recipe)

    client.get(f"/r/{recipe.slug}")
    client.get(f"/r/{recipe.slug}")

    assert no_metadata_queue == [("recipe-metadata", {"recipe_id": recipe.id})]

    _model_answers(monkeypatch, GOOD_ANSWER)
    engine.build_metadata(recipe)
    cache.clear()
    client.get(f"/r/{recipe.slug}")
    assert len(no_metadata_queue) == 1


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

    for transient in (408, 429):
        _model_answers(monkeypatch, ClientError(transient, {"error": {"message": "later"}}))
        assert _push(client, recipe.id).status_code == 500
    assert [v.source for v in _versions(recipe)] == ["rule"]


@pytest.mark.parametrize(
    "description",
    [
        "This creamy peanut and tofu curry is made without nuts or gluten and is full of flavor.",
        "This creamy peanut and tofu curry has no dairy and comes together with warm spices.",
        "This creamy peanut and tofu curry is ready in ten minutes and is full of warm spices.",
    ],
)
def test_reworded_absence_claims_and_spelled_out_times_are_rejected(description):
    inputs = {
        "name": "Peanut Tofu Curry",
        "description": "A creamy curry of peanuts, tofu and wheat noodles with warm spices.",
        "tags": ["dinner"],
    }
    answer = json.dumps({"title": "Peanut Tofu Curry Recipe", "description": description})

    assert engine._parse_model_output(answer, inputs) is None


def test_an_absence_phrase_in_the_recipes_name_is_allowed():
    inputs = {"name": "No-Bake Vegan Cheesecake", "description": "A chilled dessert.", "tags": []}
    answer = json.dumps(
        {
            "title": "No-Bake Vegan Cheesecake Recipe",
            "description": "This no-bake vegan cheesecake is a chilled dessert with a smooth, "
            "creamy filling on a crisp base.",
        }
    )

    assert engine._parse_model_output(answer, inputs) is not None


def test_a_second_build_does_not_call_the_model_while_the_first_holds_the_claim(app, monkeypatch):
    recipe = _recipe()
    calls = []
    monkeypatch.setattr(engine, "get_genai_client", lambda _credentials: object())

    def overlapping(_client, _model, _prompt):
        calls.append(1)
        # A second delivery arrives while this call is running: it is told to come back.
        with pytest.raises(engine.MetadataBuildBusy):
            engine.build_metadata(db.session.get(Recipe, recipe.id))
        return GOOD_ANSWER

    monkeypatch.setattr(engine, "attempt_generation", overlapping)

    assert engine.build_metadata(recipe).source == "model"
    assert calls == [1]


def test_a_failed_model_call_releases_the_claim_for_the_redelivery(app, monkeypatch):
    recipe = _recipe()
    monkeypatch.setattr(engine, "get_genai_client", lambda _credentials: object())

    def fail(_client, _model, _prompt):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(engine, "attempt_generation", fail)
    with pytest.raises(RuntimeError):
        engine.build_metadata(recipe)

    monkeypatch.setattr(engine, "attempt_generation", lambda _c, _m, _p: GOOD_ANSWER)
    assert engine.build_metadata(recipe).source == "model"


def test_deleting_a_recipe_deletes_its_metadata(app, no_model):
    recipe = _recipe()
    engine.build_metadata(recipe)
    recipe.is_public = False
    db.session.delete(recipe)
    db.session.commit()

    assert RecipeMetadata.query.count() == 0


def test_page_queueing_runs_off_the_request_thread(app, monkeypatch):
    import threading

    recipe = _recipe()
    seen = threading.Event()
    threads = []

    def record(_recipe_id, _digest, model_retry=False):
        threads.append(threading.current_thread().name)
        seen.set()

    monkeypatch.undo()  # the real background runner, not the inline test one
    monkeypatch.setattr(engine, "queue_metadata", record)

    assert engine.metadata_for_page(recipe) is None
    assert seen.wait(5)
    assert threads[0].startswith("recipe-metadata-queue")


def test_safe_add_is_one_atomic_command_on_redis(app, monkeypatch):
    from cachelib.serializers import RedisSerializer

    from utils import cache_utils

    commands = []

    class Client:
        def set(self, name, value, nx=False, ex=None):
            commands.append((name, nx, ex))
            return True

    class Backend:
        _write_client = Client()
        serializer = RedisSerializer()

        def _get_prefix(self):
            return ""

    class Cache:
        cache = Backend()

    monkeypatch.setattr(cache_utils, "cache", Cache())

    assert cache_utils.safe_add("vgc:meta:queued:x:y", 1, timeout=60) is True
    assert commands == [("vgc:meta:queued:x:y", True, 60)]


def test_safe_compare_delete_is_one_atomic_command_on_redis(app, monkeypatch):
    from cachelib.serializers import RedisSerializer

    from utils import cache_utils

    commands = []

    class Client:
        def eval(self, script, numkeys, name, value):
            commands.append((script, numkeys, name, value))
            return 1

    class Backend:
        _write_client = Client()
        serializer = RedisSerializer()

        def _get_prefix(self):
            return "prefix:"

    class Cache:
        cache = Backend()

    monkeypatch.setattr(cache_utils, "cache", Cache())

    assert cache_utils.safe_compare_delete("claim", "owner-token") is True
    assert len(commands) == 1
    script, numkeys, name, value = commands[0]
    assert "get" in script.lower() and "del" in script.lower()
    assert numkeys == 1
    assert name == "prefix:claim"
    assert value == Backend.serializer.dumps("owner-token")


def test_background_jobs_run_on_a_daemon_thread_and_a_full_line_drops_new_ones(app, monkeypatch):
    import queue as queue_module
    import threading

    monkeypatch.undo()  # the real background runner
    release = threading.Event()
    started = threading.Event()
    ran = []
    monkeypatch.setattr(engine, "_background_jobs", queue_module.Queue(maxsize=1))
    monkeypatch.setattr(engine, "_background_thread", None)

    def blocked():
        started.set()
        release.wait(5)

    engine._run_in_background(blocked)
    assert started.wait(5)
    # A daemon thread is not joined at exit, so a blocked job cannot hold the worker.
    assert engine._background_thread.daemon
    engine._run_in_background(lambda: ran.append("waiting"))
    engine._run_in_background(lambda: ran.append("dropped"))
    release.set()
    engine._background_thread.join(0.5)

    assert ran == ["waiting"]


def test_a_failed_save_after_the_model_call_releases_the_claim(app, monkeypatch):
    recipe = _recipe()
    _model_answers(monkeypatch, GOOD_ANSWER)
    real_refresh = db.session.refresh
    monkeypatch.setattr(
        db.session, "refresh", lambda _obj: (_ for _ in ()).throw(RuntimeError("database down"))
    )
    with pytest.raises(RuntimeError):
        engine.build_metadata(recipe)

    monkeypatch.setattr(db.session, "refresh", real_refresh)
    assert engine.build_metadata(recipe).source == "model"


def test_the_model_call_runs_with_no_database_connection_checked_out(app, monkeypatch):
    recipe = _recipe()
    monkeypatch.setattr(engine, "get_genai_client", lambda _credentials: object())
    in_transaction = []

    def generate(_client, _model, _prompt):
        in_transaction.append(db.session().in_transaction())
        return GOOD_ANSWER

    monkeypatch.setattr(engine, "attempt_generation", generate)
    engine.build_metadata(recipe)

    assert in_transaction == [False]


@pytest.mark.parametrize(
    "title, description",
    [
        (
            "Peanut Tofu Curry Recipe",
            "This creamy peanut curry contains neither nuts nor gluten and makes a cozy dinner "
            "with warm spices.",
        ),
        (
            "Peanut Tofu Curry Recipe",
            "This creamy peanut curry is a dairyless dinner of tofu and noodles with warm spices "
            "in every bite.",
        ),
        (
            "Chocolate Cake Recipe",
            "A creamy curry of peanuts, tofu and wheat noodles with warm spices for dinner any "
            "night of the week.",
        ),
        (
            "Peanut Tofu Curry Recipe\ud800",
            "A creamy curry of peanuts, tofu and wheat noodles with warm spices for dinner any "
            "night of the week.",
        ),
        (
            "Peanut Tofu Curry Recipe\u0000",
            "A creamy curry of peanuts, tofu and wheat noodles with warm spices for dinner any "
            "night of the week.",
        ),
    ],
)
def test_negation_unrelated_titles_and_control_characters_are_rejected(title, description):
    inputs = {
        "name": "Peanut Tofu Curry",
        "description": "A creamy curry of peanuts, tofu and wheat noodles with warm spices.",
        "tags": ["dinner"],
    }
    answer = json.dumps({"title": title, "description": description})

    assert engine._parse_model_output(answer, inputs) is None


def test_worker_asks_for_redelivery_when_the_build_is_claimed_or_the_cache_is_down(
    app, client, monkeypatch
):
    recipe = _recipe()
    _model_answers(monkeypatch, GOOD_ANSWER)
    monkeypatch.setattr(engine, "safe_add", lambda _key, _value, timeout=None: False)

    assert _push(client, recipe.id).status_code == 503
    assert [row.source for row in _versions(recipe)] == ["rule"]


def test_a_worker_does_not_release_a_claim_it_no_longer_owns(app, monkeypatch):
    recipe = _recipe()
    monkeypatch.setattr(engine, "get_genai_client", lambda _credentials: object())
    claim = engine.recipe_metadata_building_key(
        recipe.id, engine.inputs_hash(engine.metadata_inputs(recipe))
    )

    def expire_then_fail(_client, _model, _prompt):
        cache.set(claim, "another-worker", timeout=600)
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(engine, "attempt_generation", expire_then_fail)
    with pytest.raises(RuntimeError):
        engine.build_metadata(recipe)

    assert cache.get(claim) == "another-worker"


def test_build_releases_its_claim_with_atomic_compare_delete(app, monkeypatch):
    recipe = _recipe()
    monkeypatch.setattr(engine, "get_genai_client", lambda _credentials: object())
    released = []
    monkeypatch.setattr(
        engine,
        "safe_compare_delete",
        lambda key, token: released.append((key, token)),
        raising=False,
    )
    monkeypatch.setattr(
        engine,
        "safe_get",
        lambda _key: pytest.fail("claim release must not use a separate GET"),
        raising=False,
    )
    monkeypatch.setattr(
        engine,
        "safe_delete",
        lambda _key: pytest.fail("claim release must not use a separate DELETE"),
        raising=False,
    )
    monkeypatch.setattr(
        engine,
        "attempt_generation",
        lambda _client, _model, _prompt: (_ for _ in ()).throw(RuntimeError("model down")),
    )

    with pytest.raises(RuntimeError, match="model down"):
        engine.build_metadata(recipe)

    assert len(released) == 1
    assert released[0][0].startswith("vgc:meta:building:")
    assert len(released[0][1]) == 32


def test_a_failed_metadata_check_never_fails_the_save(app, monkeypatch):
    recipe = _recipe()

    def down(_recipe_id, _digest):
        raise RuntimeError("database down")

    monkeypatch.setattr(engine, "_versions", down)

    engine.queue_if_missing(recipe)


BAD_ANSWER = json.dumps({"title": "Cornbread", "description": "Too short."})


def _counting_model(monkeypatch, answer):
    calls = []
    monkeypatch.setattr(engine, "get_genai_client", lambda _credentials: object())

    def generate(_client, _model, _prompt):
        calls.append(1)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(engine, "attempt_generation", generate)
    return calls


def test_content_whose_model_answer_was_rejected_is_not_sent_to_the_model_again(app, monkeypatch):
    # KAN-393
    recipe = _recipe()
    calls = _counting_model(monkeypatch, BAD_ANSWER)

    first = engine.build_metadata(recipe)
    again = engine.build_metadata(db.session.get(Recipe, recipe.id))

    assert calls == [1]
    assert first.source == again.source == "rule"
    assert [v.source for v in _versions(recipe)] == ["rule"]
    assert _versions(recipe)[0].scores["model_rejections"] == engine.MAX_MODEL_REJECTIONS


def test_a_model_outage_is_not_counted_as_a_rejection(app, monkeypatch):
    recipe = _recipe()
    calls = _counting_model(monkeypatch, RuntimeError("model unavailable"))

    for _ in range(2):
        with pytest.raises(RuntimeError):
            engine.build_metadata(db.session.get(Recipe, recipe.id))

    assert calls == [1, 1]
    assert not _versions(recipe)[0].scores
    monkeypatch.setattr(engine, "attempt_generation", lambda _c, _m, _p: GOOD_ANSWER)
    assert engine.build_metadata(db.session.get(Recipe, recipe.id)).source == "model"


def test_changed_content_gets_a_fresh_model_attempt_after_a_rejection(app, monkeypatch):
    recipe = _recipe()
    calls = _counting_model(monkeypatch, BAD_ANSWER)
    engine.build_metadata(recipe)

    recipe = db.session.get(Recipe, recipe.id)
    recipe.data = {**recipe.data, "description": LONG_SENTENCE + " Serve it warm."}
    db.session.commit()
    monkeypatch.setattr(
        engine, "attempt_generation", lambda _c, _m, _p: calls.append(1) or GOOD_ANSWER
    )

    assert engine.build_metadata(recipe).source == "model"
    assert calls == [1, 1]


def test_a_page_whose_model_answer_was_rejected_does_not_queue_a_retry(
    app, client, no_metadata_queue, monkeypatch
):
    recipe = _recipe()
    _counting_model(monkeypatch, BAD_ANSWER)
    engine.build_metadata(recipe)

    client.get(f"/r/{recipe.slug}")
    cache.clear()
    client.get(f"/r/{recipe.slug}")

    assert no_metadata_queue == []


def test_worker_acknowledges_a_delivery_for_rejected_content_without_waiting_on_a_claim(
    app, client, monkeypatch
):
    recipe = _recipe()
    calls = _counting_model(monkeypatch, BAD_ANSWER)

    assert _push(client, recipe.id).status_code == 200
    # The rejected build released its claim, and a delivery queued before the
    # rejection was recorded is finished work even when no claim can be made.
    assert (
        cache.get(
            engine.recipe_metadata_building_key(
                recipe.id, engine.inputs_hash(engine.metadata_inputs(recipe))
            )
        )
        is None
    )
    monkeypatch.setattr(engine, "safe_add", lambda _key, _value, timeout=None: False)
    assert _push(client, recipe.id).status_code == 200
    assert calls == [1]
