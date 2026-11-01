"""Open in the Claude app: only the Remote Control link the source gave for this session, never one built from parts."""

from __future__ import annotations

from urllib.parse import urlsplit

NAME = "claude_app"
ORDER = 10
OPAQUE_OK = False


def available(ctx):
    return True, ""


def _is_claude(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme == "claude":
        return True
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "claude.ai" or host.endswith(".claude.ai"))


def action(rec, ctx):
    url = getattr(rec, "rc_url", "")
    return {"label": "Open in Claude", "href": url} if url and _is_claude(url) else None
