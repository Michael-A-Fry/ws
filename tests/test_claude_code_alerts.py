"""The phone-alert hooks in the Claude Code settings: Notification and Stop run `ws notify alert`."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from ws import guards, paths, policy
from ws.adapters import claude_code

PIN = paths.Pin(Path("/h"), Path("/c"), Path("/s"), Path("/d"), Path("/k"))
PINS = shlex.join(PIN.args())
ALERT = f"ws notify alert --runner claude-code {PINS} || true"


def hooks(toml: str = "") -> dict:
    files = claude_code.generate(policy.loads(toml), ["ws"], Path("/x/out"), Path("/x/policy.toml"), pin=PIN)
    return json.loads(files["settings.json"])["hooks"]


def commands(groups: list[dict]) -> list[str]:
    return [h["command"] for g in groups for h in g["hooks"]]


def test_the_settings_carry_the_alert_on_notification_and_stop():
    h = hooks()
    assert commands(h["Notification"]) == [ALERT]
    assert commands(h["Stop"]) == [ALERT]
    assert h["Notification"][0]["hooks"][0]["type"] == "command"


def test_the_alert_runs_beside_a_guard_hook_on_stop():
    h = hooks('[guards]\non = ["spawn-cap"]\n[guards.spawn-cap]\nmax_agents = 2\n')
    assert len(commands(h["Stop"])) == 1  # only the alert: spawn-cap does not run on Stop
    h = hooks('[guards]\non = ["claims"]\n')
    stop = commands(h["Stop"])
    assert len(stop) == 2 and "gate event" in stop[0] and stop[1] == ALERT
    assert all(c.endswith("|| true") for c in stop)


def test_the_alert_command_follows_the_ws_command_given():
    files = claude_code.generate(policy.loads(""), ["uv", "run", "ws"], Path("/x/out"), Path("/x/policy.toml"), pin=PIN)
    h = json.loads(files["settings.json"])["hooks"]
    assert commands(h["Notification"]) == [f"uv run ws notify alert --runner claude-code {PINS} || true"]


@pytest.mark.parametrize("event", ["Notification", "Stop"])
def test_an_alert_hook_is_short_and_never_blocks(event):
    (hook,) = [x for g in hooks()[event] for x in g["hooks"] if "notify alert" in x["command"]]
    assert hook["timeout"] <= 5 and "exit 2" not in hook["command"]


# --- the message wake: a Stop hook that waits in the background and wakes an idle session with its exit 2 ----------------


def with_inbox(monkeypatch, on: str = "inbox") -> policy.Policy:
    """A policy with the guard `inbox` on. The guard itself is another task's, so a stand-in with the same name is
    registered for the policy to load (the pattern of tests/test_guards.py `fake_guard`)."""
    mod = SimpleNamespace(
        NAME="inbox",
        EVENTS=("pre",),
        KINDS=("command",),
        config=lambda table, where: guards.known_keys(table, where),
        judge=lambda call, cfg, ctx: None,
    )
    real = guards.all_guards
    monkeypatch.setattr(guards, "all_guards", lambda: {**real(), "inbox": mod})
    return policy.loads(f'[guards]\non = ["{on}"]\n')


def wake_hooks(pol: policy.Policy) -> list[dict]:
    files = claude_code.generate(pol, ["ws"], Path("/x/out"), Path("/x/policy.toml"), pin=PIN)
    return [
        x for g in json.loads(files["settings.json"])["hooks"]["Stop"] for x in g["hooks"] if "msg wait" in x["command"]
    ]


def test_message_wake_hook_is_on_stop_with_inbox_on(monkeypatch):
    (hook,) = wake_hooks(with_inbox(monkeypatch))
    assert hook["command"] == f"ws msg wait --session-hook {PINS}"  # exactly this; the pin is the install's layout
    assert hook["asyncRewake"] is True and hook["type"] == "command"
    assert not hook["command"].rstrip().endswith("|| true") and "exit" not in hook["command"]  # its exit 2 must get out
    assert hook["timeout"] == claude_code.WAKE_TIMEOUT and hook["timeout"] >= 3600  # an idle session waits a long time
    assert "onFailure" not in hook  # a background hook: nothing it does can hold up a call
    ours = hook_commands_of_stop(monkeypatch)
    assert ours[0] == ALERT and ours[-1] == hook["command"]  # the alert still runs, and keeps its `|| true`


def hook_commands_of_stop(monkeypatch) -> list[str]:
    files = claude_code.generate(with_inbox(monkeypatch), ["ws"], Path("/x/out"), Path("/x/policy.toml"), pin=PIN)
    return commands(json.loads(files["settings.json"])["hooks"]["Stop"])


def test_message_wake_hook_is_left_out_when_messages_are_off(monkeypatch):
    assert wake_hooks(policy.loads("")) == []  # no [guards]: messages are off
    on = with_inbox(monkeypatch)  # the guard exists, so a policy could turn it on
    off = policy.loads('[guards]\non = ["record-calls"]\n')  # other guards on, `inbox` not among them
    assert "inbox" in on.guards.on and "inbox" not in off.guards.on
    assert wake_hooks(off) == []
    stop = commands(
        json.loads(claude_code.generate(off, ["ws"], Path("/x/out"), Path("/x/policy.toml"), pin=PIN)["settings.json"])[
            "hooks"
        ]["Stop"]
    )
    assert ALERT in stop and not any("msg wait" in c for c in stop)


def test_the_wake_hook_is_ws_own_for_the_merge_and_the_upgrade(monkeypatch):
    from ws import userconfig

    (hook,) = wake_hooks(with_inbox(monkeypatch))
    assert userconfig._WS_HOOK.search(hook["command"])
    assert not userconfig._WS_HOOK.search("echo msg wait")  # a person's own command is not mistaken for it
    group = {"hooks": [hook]}
    assert userconfig._is_ws_group(group)
