"""Model role, `anthropic`: Claude models over Anthropic's API.

    [roles.model]
    adapter = "anthropic"            optional: this is the default
    default = "<model id>"
    key = "anthropic"                the secret's name in secrets.toml (default "anthropic")

The key is only ever named. A run leases it from the broker (job `[[secrets]]`, host api.anthropic.com), so this check
looks at the registry, never at the store, and makes no network call. `ws models probe` is phase 5.
"""

from __future__ import annotations

from ws import secrets as wsecrets
from ws.roles import FAIL, OK, WARN

ROLE, NAME = "model", "anthropic"
HOST = "api.anthropic.com"
DEFAULT_KEY = "anthropic"


def check(cfg, osi, env, network):
    m = cfg.get("default")
    if not isinstance(m, str) or not m:
        return FAIL, "no default model named"
    name = cfg.get("key", DEFAULT_KEY)
    if not isinstance(name, str) or not wsecrets.NAME_RE.match(name):
        return FAIL, "key must be the name of a secret (letters, digits, - and _)"
    detail = f"default model {m} (named; not probed yet)"
    try:
        entry = wsecrets.load_registry(wsecrets.registry_path(env)).get(name)
    except wsecrets.SecretsError as e:
        return WARN, f"{detail}; the secrets registry does not read: {e}"
    if entry is None:
        return WARN, f"{detail}; no [{name}] in the secrets registry, so a run cannot lease a key yet"
    if HOST not in entry.hosts:
        return WARN, f"{detail}; [{name}] is not allowed at {HOST}"
    return OK, f"{detail}; key {name} leased through the broker"


def steps(cfg, answers, env):
    """None: install already records the model in the profile, and the key is the person's to add."""
    return []
