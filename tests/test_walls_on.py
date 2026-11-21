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


# --- ws cards: a fixed-kind card from a walled class's account to the lead's, across the OS wall ----------------------


@pytest.fixture
def spool(tmp_path, monkeypatch, box):
    """A 1730 spool this account owns, the two-walled policy, and accounts by name: `who` is this process's account, and
    a card file's owner is read from `owners` (the test can't chown)."""
    from ws import cards, msg, session

    folder = tmp_path / "spool"
    folder.mkdir()
    folder.chmod(0o1730)
    pol = policy.loads(TWO_WALLED)
    state = {"who": "ws-alpha", "cwd": "/srv/work/alpha/one", "owners": {}}
    monkeypatch.setattr(msg, "_policy", lambda: pol)
    monkeypatch.setattr(msg, "caller", lambda: session.Record("s-alpha", "t", cwd=state["cwd"]))

    def account(uid=None):
        return state["who"] if uid is None else state["owners"].get("next", state["who"])

    monkeypatch.setattr(cards, "_account", account)
    state["folder"] = folder
    return state


def test_a_walled_account_drops_a_card_and_the_lead_takes_it(spool):
    from ws import cards

    made = cards.drop("counts", folder=spool["folder"])
    assert made["class"] == "alpha" and (spool["folder"] / made["card"]).stat().st_mode & 0o777 == 0o640
    spool["who"] = "user"  # the lead's account takes; the file's owner stays ws-alpha
    spool["owners"]["next"] = "ws-alpha"
    got = cards.take(folder=spool["folder"])
    assert [(c["class"], c["kind"], c["session"]) for c in got["cards"]] == [("alpha", "counts", "s-alpha")]
    assert got["rejected"] == [] and list(spool["folder"].iterdir()) == []


@pytest.mark.parametrize("kind", ["note", "ask", "stop-and-hand-over"])
def test_a_card_is_a_fixed_kind_only(spool, kind):
    from ws import cards
    from ws.output import WsError

    with pytest.raises(WsError) as e:
        cards.drop(kind, folder=spool["folder"])
    assert e.value.kind == "bad_input" and list(spool["folder"].iterdir()) == []


def test_only_a_walled_class_on_its_own_account_drops(spool):
    from ws import cards
    from ws.output import WsError

    spool["who"] = "ws-beta"  # alpha's folder, beta's account
    with pytest.raises(WsError) as e:
        cards.drop("done", folder=spool["folder"])
    assert e.value.kind == "refused" and "ws-alpha" in e.value.message
    spool["who"], spool["cwd"] = "user", "/srv/elsewhere"  # no class
    with pytest.raises(WsError) as e:
        cards.drop("done", folder=spool["folder"])
    assert e.value.kind == "refused"
    assert list(spool["folder"].iterdir()) == []


@pytest.mark.parametrize("mode", [0o0730, 0o1737, 0o1734, 0o1777])
def test_a_spool_others_can_reach_or_without_the_sticky_bit_is_refused(spool, mode):
    from ws import cards
    from ws.output import WsError

    spool["folder"].chmod(mode)
    for call in (lambda: cards.drop("done", folder=spool["folder"]), lambda: cards.take(folder=spool["folder"])):
        with pytest.raises(WsError) as e:
            call()
        assert e.value.kind == "refused" and "1730" in e.value.hint


def test_the_lead_takes_only_from_a_spool_it_owns(spool, monkeypatch):
    from ws import cards
    from ws.output import WsError

    monkeypatch.setattr(cards.os, "getuid", lambda: 4242424)
    with pytest.raises(WsError) as e:
        cards.take(folder=spool["folder"])
    assert e.value.kind == "refused" and "owned" in e.value.message


def _plant(folder, name, data):
    (folder / name).write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())


GOOD = {"v": 1, "kind": "done", "class": "alpha", "session": "s-alpha", "at": "2026-11-21T00:00:00Z"}


@pytest.mark.parametrize(
    ("card", "owner", "why"),
    [
        ({**GOOD, "class": "beta"}, "ws-alpha", "its owner is class 'alpha'"),  # a forged class
        (GOOD, "user", "no walled class"),  # dropped by an account that is no class's
        ({**GOOD, "body": "PLANTED-7f3"}, "ws-alpha", "not a card"),  # a body smuggled in
        ({**GOOD, "kind": "note"}, "ws-alpha", "not a fixed kind"),
        ({**GOOD, "session": "a b"}, "ws-alpha", "no session id"),
        (b"\xff not json", "ws-alpha", "not JSON"),
        (b"{" + b" " * 600 + b"}", "ws-alpha", "bigger than"),
    ],
)
def test_a_card_that_is_not_what_its_owner_may_send_is_rejected_and_removed(spool, card, owner, why):
    from ws import cards

    _plant(spool["folder"], "1-0123456789abcdef.card", card)
    spool["who"], spool["owners"]["next"] = "user", owner
    got = cards.take(folder=spool["folder"])
    assert got["cards"] == [] and len(got["rejected"]) == 1 and why in got["rejected"][0]["reason"]
    assert "PLANTED" not in json.dumps(got)  # never the card's own text
    assert list(spool["folder"].iterdir()) == []


def test_a_link_and_a_bad_name_are_rejected_and_a_fresh_dot_file_is_left(spool, tmp_path):
    from ws import cards

    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(GOOD))
    (spool["folder"] / "2-0123456789abcdef.card").symlink_to(target)
    _plant(spool["folder"], "notacard.txt", GOOD)
    _plant(spool["folder"], ".3-0123456789abcdef.card.tmp", b"half")
    spool["who"], spool["owners"]["next"] = "user", "ws-alpha"
    got = cards.take(folder=spool["folder"])
    assert got["cards"] == [] and sorted(r["reason"] for r in got["rejected"]) == [
        "a link, or not readable",
        "not a card name",
    ]
    assert target.exists() and [p.name for p in spool["folder"].iterdir()] == [".3-0123456789abcdef.card.tmp"]


# --- lead-only on a walled class: the lead gets in, no other session does (DEC-052) --------------------------------


def test_lead_only_lets_the_lead_into_each_walled_class_and_no_other_session(box, tmp_path):
    from ws import msg, session
    from ws.output import WsError

    a, b, home = tmp_path / "alpha", tmp_path / "beta", tmp_path / "lead"
    for d in (a, b, home):
        d.mkdir()
    text = TWO_WALLED.replace('"/srv/work/alpha/one/**", "/srv/work/alpha/two/**"', f'"{a}/**"')
    text = text.replace('"/srv/work/beta/three/**"', f'"{b}/**"')
    pol = policy.loads(text + f'\n[classes.platform]\naccess = "full"\nrepos = ["{home}/**"]\n')
    lead = session.Record("lead1", "t", cwd=str(home), role="lead")
    in_a = session.Record("wall-a", "t", cwd=str(a))
    in_b = session.Record("wall-b", "t", cwd=str(b))
    shared = tmp_path / "shared"
    for target in (in_a, in_b):
        assert msg.scratch_delivery(shared, pol, lead, target, "note", "from the lead")["id"]
    for sender, target in ((in_a, in_b), (in_b, in_a), (session.Record("w1", "t", cwd=str(home)), in_a)):
        with pytest.raises(WsError) as e:
            msg.scratch_delivery(shared, pol, sender, target, "ack" if sender.cwd != str(home) else "note", None)
        assert e.value.kind == "refused"
