"""Open in the terminal: `tmux switch-client -t T` when the server runs inside tmux, else the line to attach by hand."""

from __future__ import annotations

import shlex

from ws.home.openers import _tmux

NAME = "terminal"
ORDER = 30
OPAQUE_OK = True  # it needs only the tmux target, which an opaque record keeps


def available(ctx):
    return True, ""


def argv(rec, ctx):
    target = getattr(rec, "tmux", "")
    return ["tmux", "switch-client", "-t", target] if _tmux.valid(target) else None


def action(rec, ctx):
    target = getattr(rec, "tmux", "")
    if not _tmux.valid(target):
        return None
    if ctx.environ.get("TMUX"):
        return {"label": "Switch to it", "post": True, "text": f"tmux switch-client -t {shlex.quote(target)}"}
    return {"label": "Attach", "text": f"tmux attach -t {shlex.quote(target)}"}
