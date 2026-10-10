"""Header + footer nav parity between the SSR pages and the SPA shell (KAN-294).

Every public SSR page (``/r/<slug>``, ``/browse``, ``/browse/tag/<slug>``, the
404) extends ``templates/public/base_public.html``, so its site header and
footer must carry the same links, in the same order, as the SPA shell's header
and footer. The canonical set lives in the cookbook repo at
``src/site-nav.json``; this repo is checked out there as the ``Backend/``
submodule.

Two layers, so the check can fail in both places it runs:

1. ``EXPECTED_HEADER`` / ``EXPECTED_FOOTER`` are literal copies of that
   manifest. A template edit that drifts fails here, in Backend CI. The footer's
   tag hub links (KAN-319) are rendered from ``services/tag_hubs.py`` for the
   hubs that are indexable; with every hub indexable they are the manifest's.
2. When this checkout sits inside the cookbook superproject (the cookbook
   ``backend-test`` job runs pytest with ``submodules: recursive``, and local
   dev checkouts look the same), the literals are also compared against
   ``../src/site-nav.json``. That half cannot run in standalone Backend CI,
   where the parent repo does not exist, and says so rather than passing.

A link is ``(href, label)``: ``label`` is the anchor's ``aria-label`` when it
has one (the brand, whose visible content includes a decorative emoji),
otherwise its visible text. Sign-in state is not part of the set: SSR cannot
know it, and the SPA's auth control is a button, not a link.
"""

import json
import sys
import uuid
from html.parser import HTMLParser
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from services.tag_hubs import MIN_INDEXABLE_RECIPES, TAG_HUBS  # noqa: E402

EXPECTED_HEADER = [
    ("/", "VeganGenius Chef home"),
    ("/generate", "Generator"),
    ("/kitchen", "My Kitchen"),
    ("/browse", "Browse"),
]
EXPECTED_FOOTER = [
    ("/browse", "Browse recipes"),
    ("/browse/tag/breakfast", "Breakfast"),
    ("/browse/tag/lunch", "Lunch"),
    ("/browse/tag/dinner", "Dinner"),
    ("/browse/tag/comfort-food", "Comfort food"),
    ("/browse/tag/pasta", "Pasta"),
    ("/browse/tag/mexican", "Mexican"),
    ("/browse/tag/sandwiches", "Sandwiches"),
    ("/browse/tag/tofu", "Tofu"),
    ("/browse/tag/high-protein", "High-protein"),
    ("/browse/tag/gluten-free", "Gluten-free"),
    ("/browse/tag/dessert", "Dessert"),
    ("/browse/tag/snacks", "Snacks"),
    ("/about", "About"),
    ("/privacy-policy", "Privacy Policy"),
]

# Backend/tests/<this file> -> parents[2] is the cookbook superproject root.
SUPERPROJECT_MANIFEST = Path(__file__).resolve().parents[2] / "src" / "site-nav.json"


class _ChromeLinks(HTMLParser):
    """Collect ``(href, label)`` for anchors inside the site header and footer.

    Scoped by class (``public-header`` / ``public-footer``) because the page
    bodies nest their own ``<header>`` elements (browse, tag hub, recipe
    panels), and those are content, not site chrome.
    """

    REGIONS = {"public-header": "header", "public-footer": "footer"}

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[str, list[tuple[str, str]]] = {"header": [], "footer": []}
        self._region: str | None = None
        self._region_tag: str | None = None
        self._depth = 0
        self._anchor: dict[str, str] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = {k: v or "" for k, v in attrs}
        if self._region is None:
            for cls in attr.get("class", "").split():
                if cls in self.REGIONS:
                    self._region = self.REGIONS[cls]
                    self._region_tag = tag
                    self._depth = 1
                    return
            return
        if tag == self._region_tag:
            self._depth += 1
        if tag == "a":
            self._anchor = attr
            self._text = []

    def handle_endtag(self, tag):
        if self._region is None:
            return
        if tag == "a" and self._anchor is not None:
            label = self._anchor.get("aria-label") or " ".join("".join(self._text).split())
            self.links[self._region].append((self._anchor.get("href", ""), label))
            self._anchor = None
        elif tag == self._region_tag:
            self._depth -= 1
            if self._depth == 0:
                self._region = None
                self._region_tag = None

    def handle_data(self, data):
        if self._anchor is not None:
            self._text.append(data)


def chrome_links(body: str) -> dict[str, list[tuple[str, str]]]:
    parser = _ChromeLinks()
    parser.feed(body)
    return parser.links


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
        db.session.add(
            Recipe(
                id=str(uuid.uuid4()),
                name="Nav Parity Chili",
                slug="nav-parity-chili",
                is_public=True,
                data={
                    "name": "Nav Parity Chili",
                    "description": "A chili for the nav parity test.",
                    "tags": ["dinner"],
                    "ingredients": [{"name": "beans", "quantity": 1, "unit": "cup"}],
                    "instructions": ["Simmer."],
                },
            )
        )
        # Every hub indexable, so the footer carries the full canonical set.
        for hub in TAG_HUBS:
            for index in range(MIN_INDEXABLE_RECIPES):
                name = f"{hub.slug} parity {index}"
                db.session.add(
                    Recipe(
                        id=str(uuid.uuid4()),
                        name=name,
                        slug=name.replace(" ", "-"),
                        is_public=True,
                        data={"name": name, "tags": [sorted(hub.aliases)[0]]},
                    )
                )
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/browse", 200),
        ("/r/nav-parity-chili", 200),
        ("/browse/tag/dinner", 200),
        ("/r/no-such-recipe", 404),
    ],
)
def test_ssr_chrome_renders_the_canonical_link_set(client, path, status):
    resp = client.get(path)
    assert resp.status_code == status
    links = chrome_links(resp.get_data(as_text=True))
    assert links["header"] == EXPECTED_HEADER
    assert links["footer"] == EXPECTED_FOOTER


def test_ssr_chrome_has_no_js_only_controls(client):
    """The old header had a "My Kitchen" <button> that only a script could
    act on. SSR pages stay minimal-JS: every nav entry is a real link."""
    body = client.get("/browse").get_data(as_text=True)
    assert "data-open-kitchen" not in body
    assert 'id="spa-modal"' not in body


def test_footer_literals_are_the_defined_hubs():
    """KAN-319: the hub entries in ``EXPECTED_FOOTER`` are every hub
    services/tag_hubs.py defines, in its order, under its short label. A hub
    added, renamed or removed there must change the manifest with it."""
    prefix = "/browse/tag/"
    hub_links = [link for link in EXPECTED_FOOTER if link[0].startswith(prefix)]
    assert hub_links == [(f"{prefix}{hub.slug}", hub.label) for hub in TAG_HUBS]


def test_footer_leaves_out_a_thin_hub(app, client):
    """A hub below ``MIN_INDEXABLE_RECIPES`` is noindex and gets no link
    anywhere, the footer included."""
    with app.app_context():
        thin = Recipe.query.filter(Recipe.slug.like("tofu-parity-%")).all()
        for recipe in thin[1:]:
            db.session.delete(recipe)
        db.session.commit()
    footer = chrome_links(client.get("/browse").get_data(as_text=True))["footer"]
    assert footer == [link for link in EXPECTED_FOOTER if link[0] != "/browse/tag/tofu"]


def test_literals_match_the_cookbook_manifest():
    if not SUPERPROJECT_MANIFEST.is_file():
        pytest.skip(
            "standalone Backend checkout: the cookbook superproject's "
            "src/site-nav.json is not present; the cookbook backend-test job "
            "runs this comparison against the pinned Backend"
        )
    manifest = json.loads(SUPERPROJECT_MANIFEST.read_text(encoding="utf-8"))
    as_pairs = {
        region: [(link["href"], link["label"]) for link in manifest[region]]
        for region in ("header", "footer")
    }
    assert as_pairs["header"] == EXPECTED_HEADER
    assert as_pairs["footer"] == EXPECTED_FOOTER
