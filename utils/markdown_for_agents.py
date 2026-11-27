"""Markdown for agents (KAN-397).

A GET for a public page that sends ``Accept: text/markdown`` gets the page as
Markdown. Everything else gets the HTML it always did. The negotiation and the
conversion are md4a's WSGI middleware; this module only decides which paths it
sees and makes every response on those paths say that it varies by ``Accept``.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from md4a.wsgi import MarkdownForAgentsWSGIMiddleware

StartResponse = Callable[..., Any]
WSGIApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]


def _is_public_page(path: str) -> bool:
    """The server-rendered public pages: a recipe, the browse index, a hub."""
    return path.startswith("/r/") or path == "/browse" or path.startswith("/browse/")


def _with_vary_accept(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The headers, with ``Accept`` among the ``Vary`` values."""
    tokens = [
        token.strip().lower()
        for name, value in headers
        if name.lower() == "vary"
        for token in value.split(",")
    ]
    if "accept" in tokens or "*" in tokens:
        return headers
    return [*headers, ("Vary", "Accept")]


class PublicPagesMarkdown:
    """Offer the public pages as Markdown; pass every other request straight on.

    Nothing is cached: each Markdown response is converted from the HTML the
    application has just rendered, so it is as fresh as the page, it carries
    that response's own headers, and no memory is held between requests.
    """

    def __init__(self, app: WSGIApp) -> None:
        self.app = app
        self.markdown = MarkdownForAgentsWSGIMiddleware(
            app,
            cache_responses=False,
            # Convert the page content, not the site header and footer around
            # it. Inside it, drop what only a browser needs: the breadcrumb and
            # the browse page's label (the eyebrow), the sort and tag form, and
            # the template's step numbers, which the Markdown list repeats.
            content_selector="main",
            strip_selectors=(".public-eyebrow", ".public-browse-filter", ".public-step-number"),
            # KAN-356 (R16): cooks' feedback notes are user-written text. Escape
            # stray Markdown across the page, and render each note (marked
            # ``data-md="plain"`` in the template) as literal text, so a note
            # can never become a live link, image or heading for an agent.
            escape_misc=True,
            literal_selectors=('[data-md="plain"]',),
        )

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        if not _is_public_page(environ.get("PATH_INFO", "")):
            return self.app(environ, start_response)

        # The same URL now has two representations, so a cache must key on
        # Accept for the HTML one too, not only for the Markdown one.
        def start_varying(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            if exc_info is not None:
                return start_response(status, _with_vary_accept(headers), exc_info)
            return start_response(status, _with_vary_accept(headers))

        return self.markdown(environ, start_varying)
