"""scripts/publish_audit.py — the KAN-329 publish-state audit and cutover.

Every row that is public today was published by a client that could set
``is_public`` itself, so the cutover trusts nothing it finds: it resets the
``generated`` label everywhere, gives it back only to rows a person approved
whose content has not changed since the listing and that pass the publish
eligibility rule, and unpublishes every other public row. The fixture here is
the matrix the plan names (approved and unchanged, approved but changed,
forged private, forged public, published after the listing, saved copy,
guest-owned, error row) plus the edges that decide whether the script is safe
to run twice, to dry-run, and to run while a worker still holds a row.
"""

import hashlib
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402
from scripts import publish_audit  # noqa: E402
from scripts.publish_audit import (  # noqa: E402
    BusyWorkersError,
    ManifestError,
    build_listing,
    eligibility_problems,
    fingerprint,
    manifest_skeleton,
    render_markdown,
    run_cutover,
    run_verify,
    validate_manifest,
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        WTF_CSRF_ENABLED=False,
    )
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def adam(app):
    user = User(email="adam@example.com", name="Adam")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def other(app):
    user = User(email="other@example.com", name="Other")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture(autouse=True)
def cache_calls(monkeypatch):
    """Record cache invalidation instead of talking to Valkey."""
    calls = {"recipe": [], "image": []}
    monkeypatch.setattr(
        publish_audit,
        "invalidate_recipe",
        lambda user_id, guest_session_id, recipe_id: calls["recipe"].append(
            (user_id, guest_session_id, recipe_id)
        ),
    )
    monkeypatch.setattr(
        publish_audit, "invalidate_image", lambda recipe_id: calls["image"].append(recipe_id)
    )
    return calls


def _text(name):
    return {
        "name": name,
        "description": f"{name} description",
        "ingredients": [{"name": "tofu", "amount": "1", "unit": "block"}],
        "instructions": [{"step": 1, "text": "Cook it."}],
        "notes": "Serve warm.",
        "ai_image_gcs": f"gs://bucket/recipes/{name}.png",
        "image_keywords": ["tofu"],
        "ai_metadata": {"model": "x"},
        "personalNotes": "mine",
    }


def _row(
    name,
    *,
    owner=None,
    guest=None,
    public=True,
    origin="generated",
    status="ready",
    slug=None,
    source_slug=None,
    source_recipe_id=None,
    blob_origin="same",
    blob_public="same",
    claim_token=None,
    canonical=False,
    data=None,
):
    """Add one row. The blob mirrors the columns unless a test says otherwise."""
    rid = str(uuid.uuid4())
    blob = dict(data or _text(name))
    blob["id"] = rid
    if blob_origin == "same":
        if origin is not None:
            blob["origin"] = origin
    elif blob_origin is not None:
        blob["origin"] = blob_origin
    if blob_public == "same":
        blob["is_public"] = public
    elif blob_public is not None:
        blob["is_public"] = blob_public
    row = Recipe(
        id=rid,
        user_id=owner.id if owner else None,
        guest_session_id=guest,
        name=name,
        slug=slug if slug is not None else (name.lower().replace(" ", "-") if public else None),
        is_public=public,
        is_canonical=canonical,
        origin=origin,
        status=status,
        source_slug=source_slug,
        source_recipe_id=source_recipe_id,
        worker_claim_token=claim_token,
        data=blob,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _manifest(*decisions):
    """decisions: (row, "keep"|"unpublish"|<override fingerprint>) pairs."""
    rows = []
    for row, decision in decisions:
        fp = fingerprint(row)
        if decision not in ("keep", "unpublish"):
            fp, decision = decision, "keep"
        rows.append({"id": row.id, "slug": row.slug, "fingerprint": fp, "decision": decision})
    return {"version": 1, "rows": rows}


def _get(rid):
    db.session.expire_all()
    return db.session.get(Recipe, rid)


# ── fingerprint ───────────────────────────────────────────────────────


def test_fingerprint_ignores_notes_metadata_flags_and_timestamps(app, adam):
    row = _row("Stable", owner=adam)
    before = fingerprint(row)
    row.data = {
        **row.data,
        "personalNotes": "edited later",
        "ai_metadata": {"model": "y"},
        "is_public": False,
    }
    row.updated_at = datetime(2026, 2, 2)
    db.session.commit()
    assert fingerprint(_get(row.id)) == before


@pytest.mark.parametrize(
    "field, value",
    [
        ("description", "new words"),
        ("ingredients", [{"name": "seitan", "amount": "2", "unit": "cups"}]),
        ("instructions", [{"step": 1, "text": "Different."}]),
        ("notes", "Serve cold."),
        ("tags", ["dinner", "anything a client typed"]),
        ("servings", "4 (or whatever a client typed)"),
        ("prepTime", 10),
        ("cookTime", 25),
        ("ai_image_gcs", "gs://bucket/recipes/swapped.png"),
        ("stock_image_url", "https://images.example/other.jpg"),
        ("image_keywords", ["seitan"]),
    ],
)
def test_fingerprint_changes_with_public_text_and_media(app, adam, field, value):
    row = _row("Changing", owner=adam)
    before = fingerprint(row)
    row.data = {**row.data, field: value}
    db.session.commit()
    assert fingerprint(_get(row.id)) != before


def test_fingerprint_binds_name_column_slug_and_inline_image_bytes(app, adam):
    row = _row("Named", owner=adam)
    base = fingerprint(row)
    row.name = "Renamed"
    db.session.commit()
    renamed = fingerprint(_get(row.id))
    assert renamed != base
    row = _get(row.id)
    row.slug = "renamed"
    db.session.commit()
    reslugged = fingerprint(_get(row.id))
    assert reslugged != renamed
    row = _get(row.id)
    row.data = {**row.data, "ai_image_data": "aGVsbG8="}
    db.session.commit()
    with_bytes = fingerprint(_get(row.id))
    assert with_bytes != reslugged
    # The bytes themselves are hashed, never carried into the listing.
    listing = build_listing(db.session)
    (entry,) = [e for e in listing if e["id"] == row.id]
    assert entry["media"]["ai_image_data_sha256"] == hashlib.sha256(b"aGVsbG8=").hexdigest()
    assert "ai_image_data" not in entry["media"]


# ── eligibility ───────────────────────────────────────────────────────


def test_eligibility_names_every_failed_check(app, adam, other):
    ok = _row("Fine", owner=adam)
    assert eligibility_problems(ok) == []
    guest = _row("Guest", guest="g-1")
    assert eligibility_problems(guest) == ["guest-owned"]
    # Another account's copy: a copy keys on the source id (KAN-221), so the
    # same owner could not hold both rows.
    copy = _row("Copy", owner=other, source_slug="fine", source_recipe_id=ok.id)
    assert eligibility_problems(copy) == [
        "saved copy (source_slug)",
        "saved copy (source_recipe_id)",
    ]
    err = _row("Broken", owner=adam, status="error")
    assert eligibility_problems(err) == ["status error"]


# ── listing ───────────────────────────────────────────────────────────


def test_listing_covers_public_rows_only_grouped_by_owner(app, adam, other):
    b = _row("Bravo", owner=other)
    a = _row("Alpha", owner=adam)
    _row("Hidden", owner=adam, public=False)
    g = _row("Guesty", guest="g-9", origin=None)

    listing = build_listing(db.session)

    assert [e["id"] for e in listing] == [a.id, b.id, g.id]
    assert listing[0]["owner_email"] == "adam@example.com"
    assert listing[2]["owner_email"] is None
    assert listing[2]["owner"] == "guest:g-9"


def test_listing_carries_the_full_text_and_flags_disagreement(app, adam):
    row = _row("Flagged", owner=adam, blob_origin="manual", blob_public=False)
    forged = _row("Forged", owner=adam, origin=None, status="generating_image")

    listing = build_listing(db.session)
    by_id = {e["id"]: e for e in listing}

    entry = by_id[row.id]
    assert entry["origin"] == "generated"
    assert entry["blob_origin"] == "manual"
    assert entry["is_public"] is True
    assert entry["blob_is_public"] is False
    assert entry["disagreements"] == ["origin", "is_public"]
    assert entry["text"]["ingredients"] == [{"name": "tofu", "amount": "1", "unit": "block"}]
    assert entry["text"]["instructions"] == [{"step": 1, "text": "Cook it."}]
    assert entry["text"]["notes"] == "Serve warm."
    assert "personalNotes" not in entry["text"]
    assert entry["eligibility_problems"] == []
    assert entry["slug_normalized"] is True
    assert entry["fingerprint"] == fingerprint(row)

    assert by_id[forged.id]["origin"] is None
    assert by_id[forged.id]["eligibility_problems"] == ["status generating_image"]


def test_listing_flags_an_unsanitized_slug(app, adam):
    row = _row("Odd", owner=adam, slug="Odd Slug!")
    (entry,) = build_listing(db.session)
    assert entry["id"] == row.id
    assert entry["slug_normalized"] is False


def test_manifest_skeleton_has_no_decisions_and_no_emails(app, adam):
    row = _row("Skel", owner=adam)
    skeleton = manifest_skeleton(build_listing(db.session))
    assert skeleton["rows"] == [
        {
            "id": row.id,
            "slug": row.slug,
            "name": "Skel",
            "fingerprint": fingerprint(row),
            "decision": "",
        }
    ]
    assert "adam@example.com" not in str(skeleton)


def test_markdown_puts_status_and_canonical_in_front_of_the_reader(app, adam):
    _row("Canon", owner=adam, canonical=True, status="generating_image")
    text = render_markdown(build_listing(db.session))
    assert "adam@example.com" in text
    assert "generating_image" in text
    assert "canonical" in text.lower()
    assert "status generating_image" in text


def test_markdown_shows_every_field_the_public_page_renders(app, adam):
    # tags reach the page, JSON-LD keywords and the pin text; servings and the
    # times reach JSON-LD. A reviewer has to see them to bless them.
    data = {**_text("Tagged"), "tags": ["dinner", "planted | tag"], "servings": "4", "prepTime": 10}
    _row("Tagged", owner=adam, data=data)
    (entry,) = build_listing(db.session)
    assert entry["text"]["tags"] == ["dinner", "planted | tag"]
    text = render_markdown([entry])
    assert "Tags: dinner, planted \\| tag" in text
    assert "servings 4" in text
    assert "prep 10" in text


# ── manifest validation ───────────────────────────────────────────────


def test_undecided_manifest_row_is_refused_before_anything_changes(app, adam):
    keep = _row("Keep", owner=adam)
    blank = _row("Blank", owner=adam)
    manifest = _manifest((keep, "keep"))
    manifest["rows"].append({"id": blank.id, "fingerprint": fingerprint(blank), "decision": ""})

    with pytest.raises(ManifestError) as exc:
        run_cutover(db.session, manifest, apply=True)
    assert blank.id in exc.value.ids
    assert "undecided" in str(exc.value)
    assert _get(keep.id).origin == "generated"
    assert _get(blank.id).is_public is True


@pytest.mark.parametrize("bad", ["", "KEEP", "maybe", None])
def test_manifest_decision_must_be_keep_or_unpublish(app, adam, bad):
    row = _row("Row", owner=adam)
    manifest = {
        "version": 1,
        "rows": [{"id": row.id, "fingerprint": fingerprint(row), "decision": bad}],
    }
    with pytest.raises(ManifestError):
        validate_manifest(db.session, manifest)


def test_manifest_refuses_unknown_and_duplicate_ids(app, adam):
    row = _row("Row", owner=adam)
    dup = _manifest((row, "keep"), (row, "keep"))
    with pytest.raises(ManifestError) as exc:
        validate_manifest(db.session, dup)
    assert "duplicate" in str(exc.value)

    unknown = {"version": 1, "rows": [{"id": "nope", "fingerprint": "x", "decision": "keep"}]}
    with pytest.raises(ManifestError) as exc:
        validate_manifest(db.session, unknown)
    assert "nope" in exc.value.ids


def test_manifest_row_needs_a_fingerprint_to_keep(app, adam):
    row = _row("Row", owner=adam)
    manifest = {"version": 1, "rows": [{"id": row.id, "decision": "keep"}]}
    with pytest.raises(ManifestError):
        validate_manifest(db.session, manifest)
    # An unpublish decision needs no fingerprint: nothing is being blessed.
    manifest = {"version": 1, "rows": [{"id": row.id, "decision": "unpublish"}]}
    assert set(validate_manifest(db.session, manifest)) == {row.id}


# ── cutover ───────────────────────────────────────────────────────────


def test_cutover_matrix(app, adam, other, cache_calls):
    approved = _row("Approved", owner=adam)
    changed = _row("Changed", owner=adam)
    forged_private = _row("Forged Private", owner=other, public=False)
    forged_public = _row("Forged Public", owner=other)
    after_listing = _row("Late", owner=other)
    saved_copy = _row("Copy", owner=other, source_slug="approved", source_recipe_id=approved.id)
    guest_row = _row("Guest", guest="g-1")
    error_row = _row("Broken", owner=adam, status="error")
    manual_blessed = _row("Manual", owner=adam, origin="manual")
    notes_edited = _row("Noted", owner=adam)

    manifest = _manifest(
        (approved, "keep"),
        (changed, "keep"),
        (forged_public, "unpublish"),
        (saved_copy, "keep"),
        (guest_row, "keep"),
        (error_row, "keep"),
        (manual_blessed, "keep"),
        (notes_edited, "keep"),
    )
    # Between the listing and the cutover: text changed on one approved row,
    # only personal notes on another.
    changed.data = {**changed.data, "description": "rewritten"}
    notes_edited.data = {**notes_edited.data, "personalNotes": "later"}
    db.session.commit()

    report = run_cutover(db.session, manifest, apply=True)

    assert report["applied"] is True
    assert set(report["restored"]) == {approved.id, manual_blessed.id, notes_edited.id}
    assert {e["id"]: e["reasons"] for e in report["second_look"]} == {
        changed.id: ["content changed since the listing"],
        saved_copy.id: ["saved copy (source_slug)", "saved copy (source_recipe_id)"],
        guest_row.id: ["guest-owned"],
        error_row.id: ["status error"],
    }
    assert set(report["unpublished"]) == {
        changed.id,
        forged_public.id,
        after_listing.id,
        saved_copy.id,
        guest_row.id,
        error_row.id,
    }
    assert set(report["reset"]) >= {
        forged_private.id,
        changed.id,
        forged_public.id,
        after_listing.id,
    }

    for rid in (approved.id, manual_blessed.id, notes_edited.id):
        row = _get(rid)
        assert row.is_public is True
        assert row.origin == "generated"
        assert row.data["origin"] == "generated"
        assert row.data["is_public"] is True
    for rid in report["unpublished"]:
        row = _get(rid)
        assert row.is_public is False
        assert row.data["is_public"] is False
        assert row.origin is None
        assert "origin" not in row.data
    fp = _get(forged_private.id)
    assert fp.origin is None and "origin" not in fp.data and fp.is_public is False
    # Nothing is deleted, slugs stay where they were (KAN-288).
    assert _get(forged_public.id).slug == "forged-public"
    assert db.session.query(Recipe).count() == 10

    # Every touched row moves its timestamp forward so a stale worker or
    # patch write cannot restore the old blob.
    for rid in report["touched"]:
        assert _get(rid).updated_at > datetime(2026, 1, 1)
    # An approved row that already carried the label and the flag is not
    # touched at all: its timestamp stays, which is what makes a second run
    # a no-op. The one whose label was forged is.
    assert approved.id not in report["touched"]
    assert manual_blessed.id in report["touched"]
    assert _get(approved.id).updated_at == datetime(2026, 1, 1)

    # Cache: the owner-scoped recipe key for every touched row, the image key
    # for every unpublished one.
    expected_keys = {
        (r.user_id, r.guest_session_id, r.id)
        for r in (db.session.get(Recipe, rid) for rid in report["touched"])
    }
    assert set(cache_calls["recipe"]) == expected_keys
    assert len(cache_calls["recipe"]) == len(expected_keys)
    assert set(cache_calls["image"]) == set(report["unpublished"])
    assert len(cache_calls["image"]) == len(report["unpublished"])


def test_cutover_is_dry_run_by_default(app, adam, cache_calls):
    approved = _row("Approved", owner=adam)
    stray = _row("Stray", owner=adam)
    manifest = _manifest((approved, "keep"))

    report = run_cutover(db.session, manifest)

    assert report["applied"] is False
    assert set(report["unpublished"]) == {stray.id}
    assert _get(stray.id).is_public is True
    assert _get(stray.id).origin == "generated"
    assert _get(stray.id).updated_at == datetime(2026, 1, 1)
    assert cache_calls == {"recipe": [], "image": []}


def test_cutover_refuses_while_a_worker_holds_a_row(app, adam):
    approved = _row("Approved", owner=adam)
    _row("Busy", owner=adam, public=False, status="processing", claim_token="tok")
    manifest = _manifest((approved, "keep"))

    with pytest.raises(BusyWorkersError):
        run_cutover(db.session, manifest, apply=True)
    assert _get(approved.id).origin == "generated"

    report = run_cutover(db.session, manifest, apply=True, allow_busy=True)
    assert report["applied"] is True


def test_cutover_twice_is_a_no_op_the_second_time(app, adam, other):
    approved = _row("Approved", owner=adam)
    stray = _row("Stray", owner=other)
    manifest = _manifest((approved, "keep"))

    first = run_cutover(db.session, manifest, apply=True)
    assert set(first["unpublished"]) == {stray.id}
    stamp = _get(approved.id).updated_at

    second = run_cutover(db.session, manifest, apply=True)
    assert second["unpublished"] == []
    assert second["second_look"] == []
    assert second["restored"] == [approved.id]
    assert second["touched"] == []
    assert _get(approved.id).updated_at == stamp
    assert run_verify(db.session, manifest) == []


def test_cutover_pops_the_blob_label_instead_of_nulling_it(app, adam):
    row = _row("Reset", owner=adam, public=False)
    run_cutover(db.session, {"version": 1, "rows": []}, apply=True)
    data = _get(row.id).data
    assert "origin" not in data
    assert data["is_public"] is False


# ── verify ────────────────────────────────────────────────────────────


def test_verify_names_every_public_row_that_fails_the_rule(app, adam, other):
    good = _row("Good", owner=adam)
    no_label = _row("Unlabelled", owner=adam, origin=None)
    blob_drift = _row("Drift", owner=adam, blob_origin=None)
    not_listed = _row("Unlisted", owner=other)
    changed = _row("Changed", owner=adam)
    manifest = _manifest(
        (good, "keep"), (no_label, "keep"), (blob_drift, "keep"), (changed, "keep")
    )
    changed.data = {**changed.data, "description": "after"}
    db.session.commit()

    problems = run_verify(db.session, manifest)

    text = "\n".join(problems)
    assert good.id not in text
    assert f"{no_label.id}: origin column is None" in text
    assert f"{blob_drift.id}: blob origin is None" in text
    assert f"{not_listed.id}: public but not approved in the manifest" in text
    assert f"{changed.id}: content changed since the listing" in text


def test_verify_is_clean_after_an_applied_cutover(app, adam, other):
    approved = _row("Approved", owner=adam)
    _row("Stray", owner=other)
    _row("Changed", owner=adam, origin="manual")
    manifest = _manifest((approved, "keep"))
    assert run_verify(db.session, manifest) != []
    run_cutover(db.session, manifest, apply=True)
    assert run_verify(db.session, manifest) == []


# ── CLI ───────────────────────────────────────────────────────────────


def test_cli_cutover_without_apply_exits_zero_and_changes_nothing(app, adam, tmp_path, capsys):
    import json

    approved = _row("Approved", owner=adam)
    stray = _row("Stray", owner=adam)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest((approved, "keep"))))

    code = publish_audit.main(["cutover", "--manifest", str(path)], app=app)

    assert code == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert stray.id in out
    assert _get(stray.id).is_public is True


def test_cli_undecided_manifest_exits_two(app, adam, tmp_path, capsys):
    import json

    row = _row("Row", owner=adam)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 1, "rows": [{"id": row.id, "decision": ""}]}))

    code = publish_audit.main(["cutover", "--manifest", str(path), "--apply"], app=app)

    assert code == 2
    assert row.id in capsys.readouterr().err
    assert _get(row.id).is_public is True


def test_cli_verify_exit_code_follows_the_problems(app, adam, other, tmp_path):
    import json

    approved = _row("Approved", owner=adam)
    _row("Stray", owner=other)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest((approved, "keep"))))

    assert publish_audit.main(["verify", "--manifest", str(path)], app=app) == 1
    assert publish_audit.main(["cutover", "--manifest", str(path), "--apply"], app=app) == 0
    assert publish_audit.main(["verify", "--manifest", str(path)], app=app) == 0


def test_cli_list_writes_jsonl_markdown_and_manifest(app, adam, tmp_path):
    import json

    row = _row("Listed", owner=adam)
    out = tmp_path / "listing"

    assert publish_audit.main(["list", "--out", str(out)], app=app) == 0

    lines = (out.with_suffix(".jsonl")).read_text().splitlines()
    assert json.loads(lines[0])["id"] == row.id
    assert "adam@example.com" in out.with_suffix(".md").read_text()
    skeleton = json.loads(out.with_suffix(".manifest.json").read_text())
    assert skeleton["rows"][0]["decision"] == ""
    assert "adam@example.com" not in out.with_suffix(".manifest.json").read_text()
