from datetime import datetime

from extensions import db


class RecipeMetadata(db.Model):  # type: ignore[name-defined]
    """Search metadata written about a recipe after it is published (KAN-388).

    Kept out of ``Recipe.data``: the app saves that blob from the browser's
    copy, so a server-side edit there could be overwritten by the next save,
    and the generator's output stays as generated.

    Rows are append-only versions; the highest ``version`` for a recipe is the
    one in use. ``inputs`` and ``inputs_hash`` record what a version was built
    from, so a later edit to the recipe makes it stale instead of wrong.
    ``scores`` is empty until something grades a version.
    """

    __tablename__ = "recipe_metadata"
    __table_args__ = (
        db.UniqueConstraint("recipe_id", "version", name="uq_recipe_metadata_recipe_version"),
    )

    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(
        db.String(36),
        db.ForeignKey("recipe.id", name="fk_recipe_metadata_recipe_id", ondelete="CASCADE"),
        nullable=False,
    )
    version = db.Column(db.Integer, nullable=False)
    seo_title = db.Column(db.String(200), nullable=False)
    meta_description = db.Column(db.String(400), nullable=False)
    # Who wrote this version: "rule", "model" or "human".
    source = db.Column(db.String(16), nullable=False)
    model = db.Column(db.String(64), nullable=True)
    inputs_hash = db.Column(db.String(64), nullable=False)
    inputs = db.Column(db.JSON, nullable=False)
    scores = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
