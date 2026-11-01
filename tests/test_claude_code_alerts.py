"""The phone-alert hooks in the Claude Code settings: Notification and Stop run `ws notify alert`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ws import policy
from ws.adapters import claude_code

ALERT = "ws notify alert --runner claude-code || true"


def hooks(toml: str = "") -> dict:
    files = claude_code.generate(policy.loads(toml), ["ws"], Path("/x/out"), Path("/x/policy.toml"))
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
    files = claude_code.generate(policy.loads(""), ["uv", "run", "ws"], Path("/x/out"), Path("/x/policy.toml"))
    h = json.loads(files["settings.json"])["hooks"]
    assert commands(h["Notification"]) == ["uv run ws notify alert --runner claude-code || true"]


@pytest.mark.parametrize("event", ["Notification", "Stop"])
def test_an_alert_hook_is_short_and_never_blocks(event):
    (hook,) = [x for g in hooks()[event] for x in g["hooks"] if "notify alert" in x["command"]]
    assert hook["timeout"] <= 5 and "exit 2" not in hook["command"]
