"""Switching the walls on for two walled classes on one host: what `ws install --class` prints for each, and what each
class's launch file holds against the other and against messages in, over the local socket and the bridge."""

from __future__ import annotations

import json

import pytest

from ws import cli, osinfo, policy, wallsettings

TWO_WALLED = """
[classes.alpha]
access = "walled"
os_user = "ws-alpha"
repos = ["/srv/work/alpha/one/**", "/srv/work/alpha/two/**"]
inbound = "lead-only"
owner = "someone"
review_date = 2099-01-01

[classes.beta]
access = "walled"
os_user = "ws-beta"
repos = ["/srv/work/beta/three/**"]
inbound = "lead-only"
owner = "someone"
review_date = 2099-01-01
"""


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.chdir(home)
    return home


def _policy(box, text: str = TWO_WALLED) -> None:
    pol = box / ".ws" / "policy.toml"
    pol.parent.mkdir(exist_ok=True)
    pol.write_text(text, encoding="utf-8")


def _warnings(box, capsys, name: str) -> list[str]:
    code = cli.main(["install", "--class", name, "--json"])
    data = json.loads(capsys.readouterr().out)["data"]
    assert code == 0 and data["writes"] is False
    return data.get("warnings", [])


def _launch(box, name: str) -> dict:
    pol = policy.loads(TWO_WALLED)
    return wallsettings.launch_settings(pol.classes[name], str(box), osinfo.detect().kind, pol, {"HOME": str(box)})


@pytest.mark.parametrize("name", ["alpha", "beta"])
def test_two_walled_classes_are_named_as_the_untested_shape(box, capsys, name):
    _policy(box)
    found = [w for w in _warnings(box, capsys, name) if "classes with an os_user" in w]
    assert len(found) == 1 and "alpha, beta" in found[0] and "one class per machine" in found[0]


def test_one_walled_class_has_no_shape_warning(box, capsys):
    _policy(box, TWO_WALLED.split("[classes.beta]")[0])
    assert not [w for w in _warnings(box, capsys, "alpha") if "classes with an os_user" in w]


def test_the_managed_file_is_said_to_bind_your_own_sessions(box, capsys):
    _policy(box)
    found = [w for w in _warnings(box, capsys, "alpha") if "binds every account" in w]
    assert len(found) == 1 and "yours included" in found[0] and "no host at all" in found[0]


def test_a_repo_under_your_own_home_is_named(box, capsys):
    _policy(box, TWO_WALLED.replace("/srv/work/alpha/two/**", f"{box}/code/two/**"))
    found = [w for w in _warnings(box, capsys, "alpha") if "under your own home" in w]
    assert len(found) == 1 and f"{box}/code/two" in found[0]
    assert not [w for w in _warnings(box, capsys, "beta") if "under your own home" in w]


def test_warnings_go_to_stderr_in_text_mode(box, capsys):
    _policy(box)
    code = cli.main(["install", "--class", "beta"])
    out, err = capsys.readouterr()
    assert code == 0 and "sudo" in out and "ws: warning:" not in out
    assert err.count("ws: warning:") >= 2


@pytest.mark.parametrize(("name", "other"), [("alpha", "/srv/work/beta/three"), ("beta", "/srv/work/alpha/one")])
def test_each_launch_file_refuses_messages_on_both_routes_and_seals_the_other_class(box, name, other):
    doc = _launch(box, name)
    assert doc["crossSessionInbound"] == "refuse"  # the local socket route
    assert doc["disableRemoteControl"] is True  # the bridge route
    assert doc["isolatePeerMachines"] is True
    deny = doc["permissions"]["deny"]
    assert "SendMessage" in deny and "ListAgents" in deny
    assert f"Read(/{other}/**)" in deny and f"Edit(/{other}/**)" in deny
    assert other in doc["sandbox"]["filesystem"]["denyRead"]


def test_neither_launch_file_names_its_own_repos_as_sealed(box):
    for name, own in (("alpha", "/srv/work/alpha/one"), ("beta", "/srv/work/beta/three")):
        doc = _launch(box, name)
        assert own not in doc["sandbox"]["filesystem"]["denyRead"]
        assert f"Read(/{own}/**)" not in doc["permissions"]["deny"]
