"""Open in a browser: the web link the record carries (`rc_url`), as a plain link that works on a phone too."""

from __future__ import annotations

NAME = "browser"
ORDER = 20
OPAQUE_OK = False  # a web link is the record's own address; an opaque record carries none


def available(ctx):
    return True, ""


def action(rec, ctx):
    url = getattr(rec, "rc_url", "")
    if url.lower().startswith(("http://", "https://")):
        return {"label": "Open in browser", "href": url}
    return None
