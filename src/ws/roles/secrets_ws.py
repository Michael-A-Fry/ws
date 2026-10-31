"""Secrets role, `ws-secret`: values in ws's own store, used only through the broker (see ws.secrets and ws.broker)."""

from __future__ import annotations

import os

from ws import broker
from ws import secrets as wsecrets
from ws.roles import FAIL, OK, WARN

ROLE, NAME = "secrets", "ws-secret"


def check(cfg, osi, env, network):
    store, sock = wsecrets.Store(wsecrets.store_dir(env, cfg)), wsecrets.socket_path(env, cfg)
    try:
        problems, owner = wsecrets.health(wsecrets.registry_path(env), store)
    except PermissionError:  # the store belongs to another user: only the broker can vouch for it
        problems, owner = None, None
    try:
        answer, down = broker.status(sock), ""
    except broker.BrokerError as e:
        answer, down = None, str(e)
    if problems is None:
        if answer is None:
            return FAIL, f"the store belongs to another user and the broker is not answering: {down}"
        problems = answer.get("problems", ["the broker did not report on its store"])
        owner = answer.get("store_owner_uid")
    if problems:
        return FAIL, "; ".join(problems)
    if owner == os.geteuid():
        tail = "" if answer else f"; broker not answering ({down})"
        return WARN, f"same-user boundary: run `ws secret wall` for the one sudo step{tail}"
    if answer is None:
        return FAIL, f"store is walled but the broker is not answering: {down}"
    cp = answer.get("code_problem")
    if cp is None or cp:  # a broker that runs code another user can edit hands that user the keys
        why = cp or "it did not say where it runs from"
        return WARN, f"store is walled, but the broker's own code is not locked down: {why}"
    return OK, f"ws-secret: store owned by {wsecrets.owner_name(owner)}, broker answering"


def steps(cfg, answers, env):
    """None: the registry and the values are the person's to add (`ws secret set`), and a wall needs sudo."""
    return []
