"""Secrets role, `env`: keys come from environment variables. No broker, so an agent can read them: for trying ws out."""

from __future__ import annotations

from ws.roles import OK

ROLE, NAME = "secrets", "env"


def check(cfg, osi, env, network):
    return OK, NAME


def steps(cfg, answers, env):
    return []
