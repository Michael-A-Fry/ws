"""Model role, `opencode`: any provider OpenCode's own config can reach (local models included).

    [roles.model]
    adapter = "opencode"
    default = "<provider>/<model>"   or default = "<model>" with provider = "<provider>"
    check_models = true              false skips asking `opencode models`

The provider's credentials stay in OpenCode's config or ws's broker; this role only names the pair. When `opencode` is
on PATH and the check is online, `opencode models` must list the pair. It prints a local catalogue and sends no key.
"""

from __future__ import annotations

import shutil
import subprocess

from ws.roles import FAIL, OK, WARN

ROLE, NAME = "model", "opencode"
TIMEOUT_S = 20.0


def pair(cfg: dict) -> tuple[str, str] | None:
    """(provider, model) from `default` ("provider/model"), or `default` plus `provider`. None if it names neither."""
    m, p = cfg.get("default"), cfg.get("provider")
    if not isinstance(m, str) or not m.strip():
        return None
    if isinstance(p, str) and p.strip():
        return p.strip(), m.removeprefix(f"{p.strip()}/").strip()
    provider, sep, model = m.partition("/")
    return (provider.strip(), model.strip()) if sep and provider.strip() and model.strip() else None


def list_models(exe: str, env) -> list[str] | None:
    """What `opencode models` prints, one id per line. None if it did not run. Replaced in tests."""
    try:
        p = subprocess.run(
            [exe, "models"], env=dict(env), capture_output=True, text=True, timeout=TIMEOUT_S, check=False,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()] if p.returncode == 0 else None


def check(cfg, osi, env, network):
    if not isinstance(cfg.get("default"), str) or not cfg["default"].strip():
        return FAIL, "no default model named"
    named = pair(cfg)
    if named is None:
        return FAIL, 'name the provider: default = "<provider>/<model>", or add provider = "<provider>"'
    ref = f"{named[0]}/{named[1]}"
    detail = f"default model {ref} via OpenCode (named; not probed yet)"
    if cfg.get("check_models", True) is False:
        return OK, detail
    if not network:
        return OK, f"{detail}; model list not checked (--offline)"
    exe = shutil.which("opencode", path=env.get("PATH"))
    if not exe:
        return OK, f"{detail}; `opencode` is not on PATH, so the model list was not checked"
    listed = list_models(exe, env)
    if listed is None:
        return WARN, f"{detail}; `opencode models` did not run"
    if ref not in listed:
        return FAIL, f"OpenCode does not list {ref}: check the provider is set up (`opencode models` shows what it has)"
    return OK, f"default model {ref} via OpenCode (listed)"


def steps(cfg, answers, env):
    """None: the model is in the profile (install writes it), and OpenCode's own config is not ws's to change."""
    return []
