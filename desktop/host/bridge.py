"""Resolve the frontend URL and inject backend connection details.

The built TypeScript frontend reads ``port`` and ``token`` from the
page query string, so the host appends them to whichever URL it loads:
- ``CODEWOOD_GUI_URL`` env override (e.g. a running Vite dev server)
- the bundled ``frontend/index.html`` inside a frozen build
- the locally built ``desktop/frontend/dist/index.html`` in development
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlencode


def _frontend_base() -> str:
    override = os.environ.get("CODEWOOD_GUI_URL", "").strip()
    if override:
        return override

    if getattr(sys, "frozen", False):
        index = Path(getattr(sys, "_MEIPASS", ".")) / "frontend" / "index.html"
    else:
        index = Path(__file__).resolve().parents[1] / "frontend" / "dist" / "index.html"

    if not index.exists():
        raise FileNotFoundError(
            f"Frontend build not found: {index}. Run the frontend build first."
        )
    return index.as_uri()


def resolve_frontend_url(port: int, token: str) -> str:
    base = _frontend_base()
    params = urlencode({"port": str(port), "token": token})
    # Pass connection details in the URL hash fragment rather than a query
    # string: WebView2/Edge fails to resolve a file:// URL that carries a
    # ``?query`` (it looks for a file literally named "index.html?..."),
    # but the fragment is ignored for file lookups and still readable from
    # ``location.hash`` in the frontend.
    separator = "&" if "#" in base else "#"
    return f"{base}{separator}{params}"
