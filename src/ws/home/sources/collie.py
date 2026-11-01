"""A slot for the phone front end's sessions and asks: not built until its read format is known."""

from __future__ import annotations

from ws.home.sources import UNAVAILABLE, SourceError

NAME = "collie"
PROVIDES = ("sessions", "asks")
ORDER = 95
WHY = "not built: waiting for the trial's read format"


def available(env):
    return False, WHY


def read(kind, env):
    raise SourceError(UNAVAILABLE, WHY)
