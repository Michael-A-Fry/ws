"""Notify role, `none`: ws sends no pushes."""

from __future__ import annotations

from ws.roles import OK

ROLE, NAME = "notify", "none"


def check(cfg, osi, env, network):
    return OK, "off (no pushes)"


def steps(cfg, answers, env):
    return []
