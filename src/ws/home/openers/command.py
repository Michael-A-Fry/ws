"""Open with your own program: an argument list from the profile's `[home.open]`, e.g. `command: ws-term {tmux}`.

The words are split the way a shell would split them, but nothing runs through a shell: the server starts the program
with each word as one argument. `{tmux}`, `{cwd}` and `{session_id}` are replaced by the record's own values. A record
with no value for a word the template uses (or a value that starts with `-`) is offered nothing.
"""

from __future__ import annotations

import shlex

from ws.home.openers import _tmux

NAME = "command"
ORDER = 40
OPAQUE_OK = True  # an opaque record has no folder, so a template that needs `{cwd}` offers nothing for it


def _words(arg: str) -> list[str]:
    try:
        return shlex.split(arg)
    except ValueError:
        return []


def available(ctx):
    if _words(ctx.arg):
        return True, ""
    return False, "no program set: write `command: <program> {tmux}` in [home.open]"


def argv(rec, ctx):
    words = _words(ctx.arg)
    if not words:
        return None
    values = {
        "{tmux}": getattr(rec, "tmux", ""),
        "{cwd}": getattr(rec, "cwd", ""),
        "{session_id}": getattr(rec, "session_id", None) or ("" if hasattr(rec, "kind") else getattr(rec, "id", "")),
    }
    out: list[str] = []
    for word in words:
        for key, val in values.items():
            if key in word:
                if not val or val.startswith("-") or "\0" in val or (key == "{tmux}" and not _tmux.valid(val)):
                    return None
                word = word.replace(key, val)
        out.append(word)
    return out


def action(rec, ctx):
    line = argv(rec, ctx)
    if line is None:
        return None
    return {"label": f"Open with {line[0]}", "post": True, "text": shlex.join(line)}
