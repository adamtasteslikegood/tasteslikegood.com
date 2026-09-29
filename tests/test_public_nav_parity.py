"""Header + footer nav parity between the SSR pages and the SPA shell (KAN-294).

Every public SSR page (``/r/<slug>``, ``/browse``, ``/browse/tag/<slug>``, the
404) extends ``templates/public/base_public.html``, so its site header and
footer must carry the same links, in the same order, as the SPA shell's header
and footer. The canonical set lives in the cookbook repo at
``src/site-nav.json``; this repo is checked out there as the ``Backend/``
submodule.

Two layers, so the check can fail in both places it runs:

1. ``EXPECTED_HEADER`` / ``EXPECTED_FOOTER`` are literal copies of that
   manifest. A template edit that drifts fails here, in Backend CI.
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

EXPECTED_HEADER = [
    ("/", "VeganGenius Chef home"),
    ("/generate", "Generator"),
    ("/kitchen", "My Kitchen"),
    ("/browse", "Browse"),
]
EXPECTED_FOOTER = [
    ("/browse", "Browse recipes"),
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
