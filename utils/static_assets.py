"""Versioned, cacheable URLs for Flask's own static files (KAN-362).

``/static/css/recipe-site.css`` kept one URL across releases, so Flask could
only answer it ``Cache-Control: no-cache`` and every public page revalidated
its stylesheets and script. ``url_for('static', ...)`` now carries the file's
content hash as ``?v=``, so a changed file gets a new URL and a URL that
carries the current hash can be cached for a year.
"""

import hashlib
import os
from functools import lru_cache
from typing import Any

from flask import Flask, Response, request

IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


@lru_cache(maxsize=256)
def _digest(path: str, mtime_ns: int, size: int) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()[:12]


@lru_cache(maxsize=4)
def _static_files(folder: str) -> dict[str, str]:
    """URL filename -> absolute path, for every file in the static folder.

    Built by walking the folder once per process, so a requested filename is
    only ever used as a dictionary key and never joined into a path.
    """
    root = os.path.realpath(folder)
    files: dict[str, str] = {}
    for directory, _subdirs, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            files[os.path.relpath(path, root).replace(os.sep, "/")] = path
    return files


def static_version(app: Flask, filename: str) -> str | None:
    """Content hash of a static file, or None when the folder has no such file."""
    folder = app.static_folder
    if not folder:
        return None
    path = _static_files(folder).get(filename)
    if path is None:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return _digest(path, stat.st_mtime_ns, stat.st_size)


def register_static_versioning(app: Flask) -> None:
    @app.url_defaults
    def add_static_version(endpoint: str, values: dict[str, Any]) -> None:
        if endpoint != "static" or "v" in values:
            return
        filename = values.get("filename")
        if isinstance(filename, str):
            version = static_version(app, filename)
            if version:
                values["v"] = version

    @app.after_request
    def cache_versioned_static(response: Response) -> Response:
        # Only a URL naming the file's current hash is immutable. A stale or
        # made-up ``v`` keeps Flask's revalidating default, so an old page can
        # never pin old CSS under a URL a new page also uses.
        if request.endpoint != "static" or response.status_code != 200:
            return response
        filename = (request.view_args or {}).get("filename")
        version = request.args.get("v")
        if isinstance(filename, str) and version and version == static_version(app, filename):
            response.headers["Cache-Control"] = IMMUTABLE_CACHE_CONTROL
        return response
