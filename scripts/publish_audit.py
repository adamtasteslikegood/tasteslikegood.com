"""Publish-state audit and cutover (KAN-329).

Until this fix any signed-in client could set ``is_public`` and ``origin`` on
its own rows, so no query can tell a recipe the worker generated from one a
client labelled. The cutover therefore trusts nothing it finds in the table:

1. ``list``     — one entry per public row, grouped by owner, with the full
                  public text, the media identity and a fingerprint over
                  both, for a person to read and decide keep / unpublish.
                  Writes ``<out>.jsonl``, ``<out>.md`` and a
                  ``<out>.manifest.json`` skeleton with empty decisions.
2. ``cutover``  — one transaction, rows locked, from the decided manifest:
                  reset the ``generated`` label on every row that has it;
                  give it back only to a ``keep`` row whose fingerprint still
                  matches and that passes the eligibility rule (signed-in
                  owner, not a saved copy, status ``ready``); unpublish every
                  other public row, including rows published after the
                  listing was taken; bump ``updated_at`` on every touched row.
                  Dry-run unless ``--apply``. Refuses an undecided manifest and
                  refuses to run while a worker holds a row.
3. ``verify``   — the re-check before the write pause is lifted: every public
                  row carries the label in the column and the blob, is
                  approved in the manifest with a matching fingerprint, and is
                  eligible. Exit 1 on any problem.

Nothing is deleted: a deleted published slug is retired for good (KAN-288).

Why a script and not SQL in Cloud SQL Studio: the fingerprint has one
implementation (here, tested on the fixture in ``tests/test_publish_audit.py``)
and the cutover must also drop the owner-scoped response cache and the image
cache in Valkey, which only something inside the VPC can reach.

Running it
----------

Locally against the dev database::

    uv run python scripts/publish_audit.py list --out /tmp/audit
    uv run python scripts/publish_audit.py cutover --manifest /tmp/audit.manifest.json
    uv run python scripts/publish_audit.py cutover --manifest ... --apply
    uv run python scripts/publish_audit.py verify  --manifest ...

On staging and production the script runs as the Cloud Run job
``flask-backend-publish-audit`` (deployed by the cookbook ``cloudbuild.yaml``
with the same env, secrets, VPC and Cloud SQL wiring as the migrate job, and
``python`` as its command). The subcommand is chosen per execution with an
args override, and ``--out`` / ``--manifest`` accept ``gs://`` paths in the
recipe-images bucket, which is private and read only by the app::

    gcloud run jobs execute flask-backend-publish-audit --region us-central1 --wait \
      --args=scripts/publish_audit.py,list,--out,gs://$BUCKET/audit/prod-$(date +%F)
    gsutil cp "gs://$BUCKET/audit/prod-*.md" "gs://$BUCKET/audit/prod-*.manifest.json" .
    # ... decide every row, then upload the manifest and:
    gcloud run jobs execute flask-backend-publish-audit --region us-central1 --wait \
      --args=scripts/publish_audit.py,cutover,--manifest,gs://$BUCKET/audit/prod.manifest.json
    gcloud run jobs execute ... --args=scripts/publish_audit.py,cutover,--manifest,...,--apply
    gcloud run jobs execute ... --args=scripts/publish_audit.py,verify,--manifest,...

The listing has to run on production *before* the release that deploys this
job, so its first run bootstraps the job by hand: build the Backend image
from the advisory branch into the private registry and create the job from
the image-repair job's description with ``--command=python``. The exact
commands are in the hotfix plan; the cloudbuild step keeps the job current
afterwards.

Canonical recipe text: once a generated row is locked (KAN-328) the client
API cannot change its text. The one approved exception is an ORM edit from a
``flask shell`` in the same job image: load the ``Recipe``, change ``data``,
commit — and then re-run ``list``/``verify`` so the saved fingerprint is
refreshed, because a text change on an approved row is by design a
fingerprint mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402
from utils.cache_utils import invalidate_recipe, invalidate_recipe_image  # noqa: E402
from utils.slug_utils import normalize_slug  # noqa: E402

# Re-exported under names the tests patch; the cutover reads these at call time.
invalidate_image = invalidate_recipe_image

MANIFEST_VERSION = 1
DECISIONS = ("keep", "unpublish")

# The public text and the media identity. ``ai_metadata``, ``personalNotes``,
# ``is_public``, ``origin`` and the timestamps are deliberately absent so a
# notes edit between the listing and the cutover does not fail an approved
# row; an image swap does, by design (D10).
TEXT_FIELDS = ("description", "ingredients", "instructions", "notes")
MEDIA_FIELDS = ("ai_image_gcs", "stock_image_url", "image_keywords")

BUSY_STATUSES = ("processing", "generating_image")


class ManifestError(ValueError):
    """The manifest cannot be acted on; nothing was changed."""

    def __init__(self, message: str, ids: list[str]):
        super().__init__(message)
        self.ids = ids


class BusyWorkersError(RuntimeError):
    """A worker still holds a row; the cutover must wait for the drain."""


# ── fingerprint and eligibility ───────────────────────────────────────


def _media_identity(data: dict[str, Any]) -> dict[str, Any]:
    media: dict[str, Any] = {field: data.get(field) for field in MEDIA_FIELDS}
    inline = data.get("ai_image_data")
    if isinstance(inline, str) and inline:
        media["ai_image_data_sha256"] = hashlib.sha256(inline.encode("utf-8")).hexdigest()
    return media


def _public_text(data: dict[str, Any]) -> dict[str, Any]:
    return {field: data.get(field) for field in TEXT_FIELDS}


def fingerprint(recipe: Recipe) -> str:
    """sha256 over the name column, the slug, the public text and the media."""
    data = recipe.data if isinstance(recipe.data, dict) else {}
    material = {
        "name": recipe.name,
        "slug": recipe.slug,
        "text": _public_text(data),
        "media": _media_identity(data),
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def eligibility_problems(recipe: Recipe) -> list[str]:
    """Why this row may not carry a public page; empty means it may."""
    problems: list[str] = []
    if recipe.user_id is None:
        problems.append("guest-owned")
    if recipe.source_slug is not None:
        problems.append("saved copy (source_slug)")
    if recipe.source_recipe_id is not None:
        problems.append("saved copy (source_recipe_id)")
    if recipe.status != "ready":
        problems.append(f"status {recipe.status}")
    return problems


# ── listing ───────────────────────────────────────────────────────────


def _owner_label(recipe: Recipe, email: Optional[str]) -> str:
    if recipe.user_id is not None:
        return f"user:{recipe.user_id}"
    return f"guest:{recipe.guest_session_id}"


def _entry(recipe: Recipe, email: Optional[str]) -> dict[str, Any]:
    data = recipe.data if isinstance(recipe.data, dict) else {}
    blob_origin = data.get("origin")
    blob_public = data.get("is_public")
    disagreements = []
    if blob_origin != recipe.origin:
        disagreements.append("origin")
    if blob_public is not recipe.is_public:
        disagreements.append("is_public")
    return {
        "id": recipe.id,
        "slug": recipe.slug,
        "slug_normalized": recipe.slug is None or normalize_slug(recipe.slug) == recipe.slug,
        "name": recipe.name,
        "owner": _owner_label(recipe, email),
        "owner_email": email,
        "origin": recipe.origin,
        "blob_origin": blob_origin,
        "is_public": recipe.is_public,
        "blob_is_public": blob_public,
        "disagreements": disagreements,
        "is_canonical": recipe.is_canonical,
        "status": recipe.status,
        "source_slug": recipe.source_slug,
        "source_recipe_id": recipe.source_recipe_id,
        "created_at": recipe.created_at.isoformat() if recipe.created_at else None,
        "updated_at": recipe.updated_at.isoformat() if recipe.updated_at else None,
        "media": _media_identity(data),
        "text": _public_text(data),
        "eligibility_problems": eligibility_problems(recipe),
        "fingerprint": fingerprint(recipe),
    }


def build_listing(session) -> list[dict[str, Any]]:
    """Every public row, grouped by owner (email, then guest), oldest first."""
    rows = (
        session.query(Recipe, User.email)
        .outerjoin(User, User.id == Recipe.user_id)
        .filter(Recipe.is_public.is_(True))
        .all()
    )
    entries = [_entry(recipe, email) for recipe, email in rows]
    entries.sort(
        key=lambda e: (
            e["owner_email"] is None,
            e["owner_email"] or "",
            e["owner"],
            e["created_at"] or "",
            e["id"],
        )
    )
    return entries


def manifest_skeleton(listing: list[dict[str, Any]]) -> dict[str, Any]:
    """The decision file: ids and fingerprints only, no owner emails."""
    return {
        "version": MANIFEST_VERSION,
        "rows": [
            {
                "id": e["id"],
                "slug": e["slug"],
                "name": e["name"],
                "fingerprint": e["fingerprint"],
                "decision": "",
            }
            for e in listing
        ],
    }


def _md_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _md_item(item: Any) -> str:
    """An ingredient or a step: objects are shown as compact JSON, strings as is."""
    return _md_cell(json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item)


def render_markdown(listing: list[dict[str, Any]]) -> str:
    """One section per owner; status and canonical flags up front."""
    out: list[str] = ["# Publish audit listing", ""]
    out.append(f"{len(listing)} public rows.")
    out.append("")
    current = object()
    for e in listing:
        owner = e["owner_email"] or e["owner"]
        if owner != current:
            current = owner
            out.append(f"## {owner}")
            out.append("")
        flags = []
        if e["is_canonical"]:
            flags.append("CANONICAL")
        if e["status"] != "ready":
            flags.append(f"STATUS {e['status']}")
        if e["disagreements"]:
            flags.append("column/blob disagree: " + ", ".join(e["disagreements"]))
        if not e["slug_normalized"]:
            flags.append("slug not normalized")
        out.append(f"### {e['name']}  —  `/r/{e['slug']}`")
        out.append("")
        out.append(f"- id `{e['id']}` · fingerprint `{e['fingerprint']}`")
        out.append(
            f"- origin column `{e['origin']}` / blob `{e['blob_origin']}` · "
            f"status `{e['status']}` · canonical `{e['is_canonical']}`"
        )
        if flags:
            out.append("- **" + " · ".join(flags) + "**")
        problems = e["eligibility_problems"]
        out.append("- eligibility: " + (", ".join(problems) if problems else "ok"))
        if e["source_slug"] or e["source_recipe_id"]:
            out.append(
                f"- source_slug `{e['source_slug']}` · source_recipe_id `{e['source_recipe_id']}`"
            )
        media = e["media"]
        out.append("- media: " + ", ".join(f"{k}={_md_cell(v)}" for k, v in media.items() if v))
        out.append(f"- created {e['created_at']} · updated {e['updated_at']}")
        out.append("")
        text = e["text"]
        out.append(f"> {_md_cell(text.get('description'))}")
        out.append("")
        out.append("Ingredients:")
        for item in text.get("ingredients") or []:
            out.append(f"- {_md_item(item)}")
        out.append("")
        out.append("Instructions:")
        for i, step in enumerate(text.get("instructions") or [], start=1):
            out.append(f"{i}. {_md_item(step)}")
        if text.get("notes"):
            out.append("")
            out.append(f"Notes: {_md_cell(text.get('notes'))}")
        out.append("")
    return "\n".join(out)


# ── manifest ──────────────────────────────────────────────────────────


def validate_manifest(session, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every row decided, every id known, no duplicates. Returns rows by id."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("rows"), list):
        raise ManifestError("manifest must be an object with a rows list", [])
    rows = manifest["rows"]
    by_id: dict[str, dict[str, Any]] = {}
    undecided: list[str] = []
    duplicates: list[str] = []
    unfingerprinted: list[str] = []
    for row in rows:
        rid = str(row.get("id") or "")
        if not rid:
            raise ManifestError("manifest row without an id", [])
        if rid in by_id:
            duplicates.append(rid)
            continue
        decision = row.get("decision")
        if decision not in DECISIONS:
            undecided.append(rid)
        elif decision == "keep" and not row.get("fingerprint"):
            unfingerprinted.append(rid)
        by_id[rid] = row
    if duplicates:
        raise ManifestError(f"duplicate manifest ids: {', '.join(duplicates)}", duplicates)
    if undecided:
        raise ManifestError(
            "undecided manifest rows (decision must be keep or unpublish): " + ", ".join(undecided),
            undecided,
        )
    if unfingerprinted:
        raise ManifestError(
            "keep rows without a fingerprint: " + ", ".join(unfingerprinted), unfingerprinted
        )
    known = {rid for (rid,) in session.query(Recipe.id).filter(Recipe.id.in_(list(by_id))).all()}
    unknown = [rid for rid in by_id if rid not in known]
    if unknown:
        raise ManifestError(f"manifest ids not in the database: {', '.join(unknown)}", unknown)
    return by_id


# ── cutover ───────────────────────────────────────────────────────────


def _set_blob(recipe: Recipe, *, origin: Optional[str], is_public: bool) -> None:
    data = dict(recipe.data) if isinstance(recipe.data, dict) else {}
    if origin is None:
        data.pop("origin", None)
    else:
        data["origin"] = origin
    data["is_public"] = is_public
    recipe.data = data


def run_cutover(
    session,
    manifest: dict[str, Any],
    *,
    apply: bool = False,
    allow_busy: bool = False,
) -> dict[str, Any]:
    """Reset, restore the approved set, default-deny the rest. One transaction."""
    by_id = validate_manifest(session, manifest)

    busy = (
        session.query(Recipe.id)
        .filter(Recipe.status.in_(BUSY_STATUSES), Recipe.worker_claim_token.isnot(None))
        .count()
    )
    if busy and not allow_busy:
        raise BusyWorkersError(f"{busy} row(s) still held by a worker; wait for the drain")

    # The lock set comes from the table, not the manifest, so a row published
    # after the listing is in it and lands in the default deny.
    rows: list[Recipe] = (
        session.query(Recipe)
        .filter(
            db.or_(
                Recipe.origin == "generated",
                Recipe.is_public.is_(True),
                Recipe.id.in_(list(by_id)),
            )
        )
        .with_for_update()
        .all()
    )
    now = datetime.utcnow()
    reset: list[str] = []
    restored: list[str] = []
    second_look: list[dict[str, Any]] = []
    unpublished: list[str] = []
    touched: list[str] = []

    for recipe in rows:
        before = (
            recipe.origin,
            recipe.is_public,
            (recipe.data or {}).get("origin"),
            (recipe.data or {}).get("is_public"),
        )
        approved = False
        decided = by_id.get(recipe.id)
        if decided is not None and decided["decision"] == "keep":
            reasons = []
            if fingerprint(recipe) != decided["fingerprint"]:
                reasons.append("content changed since the listing")
            reasons.extend(eligibility_problems(recipe))
            if reasons:
                second_look.append({"id": recipe.id, "reasons": reasons})
            else:
                approved = True

        if recipe.origin == "generated" and not approved:
            reset.append(recipe.id)
        if approved:
            restored.append(recipe.id)

        new_origin = "generated" if approved else None
        new_public = bool(recipe.is_public and approved)
        if recipe.is_public and not approved:
            unpublished.append(recipe.id)

        recipe.origin = new_origin
        recipe.is_public = new_public
        _set_blob(recipe, origin=new_origin, is_public=new_public)
        after = (
            recipe.origin,
            recipe.is_public,
            recipe.data.get("origin"),
            recipe.data.get("is_public"),
        )
        if after != before:
            # Explicit, not onupdate: a queued image write or a patch holding
            # an older timestamp must lose to this row (:885, :1368).
            recipe.updated_at = now
            touched.append(recipe.id)

    report = {
        "applied": apply,
        "reset": reset,
        "restored": restored,
        "second_look": second_look,
        "unpublished": unpublished,
        "touched": touched,
    }
    if not apply:
        session.rollback()
        return report

    owners = {r.id: (r.user_id, r.guest_session_id) for r in rows}
    session.commit()
    for rid in touched:
        user_id, guest_session_id = owners[rid]
        invalidate_recipe(user_id, guest_session_id, rid)
    for rid in unpublished:
        invalidate_image(rid)
    return report


# ── verify ────────────────────────────────────────────────────────────


def run_verify(session, manifest: dict[str, Any]) -> list[str]:
    """Problems with the public set after the cutover; empty means clean."""
    by_id = validate_manifest(session, manifest)
    problems: list[str] = []
    for recipe in session.query(Recipe).filter(Recipe.is_public.is_(True)).all():
        data = recipe.data if isinstance(recipe.data, dict) else {}
        decided = by_id.get(recipe.id)
        if decided is None or decided["decision"] != "keep":
            problems.append(f"{recipe.id}: public but not approved in the manifest")
        elif fingerprint(recipe) != decided["fingerprint"]:
            problems.append(f"{recipe.id}: content changed since the listing")
        if recipe.origin != "generated":
            problems.append(f"{recipe.id}: origin column is {recipe.origin}")
        if data.get("origin") != "generated":
            problems.append(f"{recipe.id}: blob origin is {data.get('origin')}")
        if data.get("is_public") is not True:
            problems.append(f"{recipe.id}: blob is_public is {data.get('is_public')}")
        for reason in eligibility_problems(recipe):
            problems.append(f"{recipe.id}: {reason}")
    return problems


# ── IO (local paths and gs://) ────────────────────────────────────────


def _read_text(path: str) -> str:
    if path.startswith("gs://"):
        from google.cloud import storage

        bucket_name, _, name = path[len("gs://") :].partition("/")
        return str(storage.Client().bucket(bucket_name).blob(name).download_as_text())
    return Path(path).read_text(encoding="utf-8")


def _write_text(path: str, text: str) -> None:
    if path.startswith("gs://"):
        from google.cloud import storage

        bucket_name, _, name = path[len("gs://") :].partition("/")
        storage.Client().bucket(bucket_name).blob(name).upload_from_string(text)
        return
    Path(path).write_text(text, encoding="utf-8")


def _load_manifest(path: str) -> dict[str, Any]:
    manifest = json.loads(_read_text(path))
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object", [])
    return manifest


def _print_report(report: dict[str, Any]) -> None:
    label = "APPLIED" if report["applied"] else "DRY RUN"
    print(f"[{label}] reset {len(report['reset'])} label(s)")
    print(f"[{label}] restored {len(report['restored'])} approved row(s)")
    for rid in report["restored"]:
        print(f"  RESTORE   {rid}")
    print(f"[{label}] second look {len(report['second_look'])} row(s)")
    for entry in report["second_look"]:
        print(f"  SECOND    {entry['id']} — {'; '.join(entry['reasons'])}")
    print(f"[{label}] unpublished {len(report['unpublished'])} row(s)")
    for rid in report["unpublished"]:
        print(f"  UNPUBLISH {rid}")
    print(f"[{label}] touched {len(report['touched'])} row(s)")


def main(argv: Optional[list[str]] = None, app=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser(
        "list", help="write the listing, the markdown and the manifest skeleton"
    )
    p_list.add_argument(
        "--out", required=True, help="path prefix (local or gs://) for the three files"
    )

    p_cut = sub.add_parser(
        "cutover", help="reset, restore the approved rows, default-deny the rest"
    )
    p_cut.add_argument("--manifest", required=True, help="decided manifest (local or gs://)")
    p_cut.add_argument("--apply", action="store_true", help="commit; without it the run rolls back")
    p_cut.add_argument(
        "--allow-busy",
        action="store_true",
        help="proceed although a worker holds a row (only after an abandoned claim is confirmed)",
    )

    p_ver = sub.add_parser(
        "verify", help="check every public row against the manifest and the rule"
    )
    p_ver.add_argument("--manifest", required=True)

    args = parser.parse_args(argv)
    app = app or create_app()
    with app.app_context():
        try:
            if args.command == "list":
                listing = build_listing(db.session)
                _write_text(
                    f"{args.out}.jsonl", "".join(json.dumps(e, default=str) + "\n" for e in listing)
                )
                _write_text(f"{args.out}.md", render_markdown(listing))
                _write_text(
                    f"{args.out}.manifest.json", json.dumps(manifest_skeleton(listing), indent=2)
                )
                print(
                    f"{len(listing)} public row(s) written to {args.out}.{{jsonl,md,manifest.json}}"
                )
                return 0
            manifest = _load_manifest(args.manifest)
            if args.command == "cutover":
                report = run_cutover(
                    db.session, manifest, apply=args.apply, allow_busy=args.allow_busy
                )
                _print_report(report)
                return 0
            problems = run_verify(db.session, manifest)
            for problem in problems:
                print(f"PROBLEM {problem}")
            print(f"verify: {len(problems)} problem(s)")
            return 1 if problems else 0
        except ManifestError as exc:
            print(f"manifest refused: {exc}", file=sys.stderr)
            return 2
        except BusyWorkersError as exc:
            print(f"cutover refused: {exc}", file=sys.stderr)
            return 3


if __name__ == "__main__":
    raise SystemExit(main())
