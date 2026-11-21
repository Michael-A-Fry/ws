"""Switching the walls on for two walled classes on one host: what `ws install --class` prints for each, and what each
class's launch file holds against the other and against messages in, over the local socket and the bridge."""

from __future__ import annotations

import argparse
import json
import os

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

SID = "0b9d2a4e-6c1f-4f7a-9b3e-2d5c8e1a7f40"
GOOD = {"v": 1, "kind": "done", "class": "alpha", "session": SID, "at": "2026-11-21T00:00:00Z"}


@pytest.fixture
def spool(tmp_path, monkeypatch, box):
    """A 3730 spool this account owns, in this account's own group standing in for ws-cards, the two-walled policy, and
    accounts by name: `who` is this process's account, and a card file's owner is read from `owners` (no chown here)."""
    from ws import cards, msg, session

    folder = tmp_path / "spool"
    folder.mkdir()
    folder.chmod(0o3730)
    pol = policy.loads(TWO_WALLED)
    state = {"who": "ws-alpha", "cwd": "/srv/work/alpha/one", "owners": {}, "sid": SID}
    monkeypatch.setattr(msg, "_policy", lambda: pol)
    monkeypatch.setattr(msg, "caller", lambda: session.Record(state["sid"], "t", cwd=state["cwd"]))
    monkeypatch.setattr(cards, "_group_gid", lambda: folder.stat().st_gid)

    def account(uid=None):
        return state["who"] if uid is None else state["owners"].get("next", state["who"])

    monkeypatch.setattr(cards, "_account", account)
    state["folder"] = folder
    return state


def _plant(folder, name, data):
    (folder / name).write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())


def _as_lead(spool, owner="ws-alpha"):
    spool["who"], spool["owners"]["next"] = "user", owner


def test_a_walled_account_drops_a_card_and_the_lead_takes_it(spool):
    from ws import cards

    old = os.umask(0o077)  # the card is 0640 whatever the umask
    try:
        made = cards.drop("counts", folder=spool["folder"])
    finally:
        os.umask(old)
    st = (spool["folder"] / made["card"]).stat()
    assert made["class"] == "alpha" and st.st_mode & 0o777 == 0o640 and st.st_gid == spool["folder"].stat().st_gid
    _as_lead(spool)
    got = cards.take(folder=spool["folder"])
    assert [(c["class"], c["kind"], c["session"]) for c in got["cards"]] == [("alpha", "counts", SID)]
    assert got["rejected"] == {} and list(spool["folder"].iterdir()) == []


@pytest.mark.parametrize("kind", ["note", "ask", "stop-and-hand-over"])
def test_a_card_is_a_fixed_kind_only(spool, kind):
    from ws import cards
    from ws.output import WsError

    with pytest.raises(WsError) as e:
        cards.drop(kind, folder=spool["folder"])
    assert e.value.kind == "bad_input" and list(spool["folder"].iterdir()) == []


def test_only_a_walled_class_on_its_own_account_with_a_uuid_session_drops(spool):
    from ws import cards
    from ws.output import WsError

    spool["who"] = "ws-beta"  # alpha's folder, beta's account
    with pytest.raises(WsError) as e:
        cards.drop("done", folder=spool["folder"])
    assert e.value.kind == "refused" and "ws-alpha" in e.value.message
    spool["who"], spool["sid"] = "ws-alpha", "PLANTED.exfil.text_here"  # an id that could carry text
    with pytest.raises(WsError) as e:
        cards.drop("done", folder=spool["folder"])
    assert e.value.kind == "refused" and "UUID" in e.value.message
    spool["who"], spool["cwd"], spool["sid"] = "user", "/srv/elsewhere", SID  # no class
    with pytest.raises(WsError) as e:
        cards.drop("done", folder=spool["folder"])
    assert e.value.kind == "refused"
    assert list(spool["folder"].iterdir()) == []


@pytest.mark.parametrize("mode", [0o1730, 0o3770, 0o3750, 0o3731, 0o2730, 0o3777, 0o0730])
def test_a_spool_in_any_mode_but_3730_is_refused(spool, mode):
    from ws import cards
    from ws.output import WsError

    spool["folder"].chmod(mode)
    for call in (lambda: cards.drop("done", folder=spool["folder"]), lambda: cards.take(folder=spool["folder"])):
        with pytest.raises(WsError) as e:
            call()
        assert e.value.kind == "refused" and "3730" in e.value.hint


def test_a_spool_outside_the_group_is_refused(spool, monkeypatch):
    from ws import cards
    from ws.output import WsError

    monkeypatch.setattr(cards, "_group_gid", lambda: None)  # no ws-cards group on the host
    with pytest.raises(WsError) as e:
        cards.drop("done", folder=spool["folder"])
    assert e.value.kind == "refused" and cards.GROUP in e.value.message


def test_the_lead_takes_only_from_a_spool_it_owns(spool, monkeypatch):
    from ws import cards
    from ws.output import WsError

    monkeypatch.setattr(cards.os, "getuid", lambda: 4242424)
    with pytest.raises(WsError) as e:
        cards.take(folder=spool["folder"])
    assert e.value.kind == "refused" and "owned" in e.value.message


@pytest.mark.parametrize(
    ("card", "owner", "code"),
    [
        ({**GOOD, "class": "beta"}, "ws-alpha", "class"),  # a forged class
        (GOOD, "user", "owner"),  # dropped by an account that is no class's
        ({**GOOD, "body": "PLANTED-7f3"}, "ws-alpha", "shape"),  # a body smuggled in
        ({**GOOD, "kind": "note"}, "ws-alpha", "kind"),
        ({**GOOD, "session": "s-alpha"}, "ws-alpha", "session"),  # not a UUID
        ({**GOOD, "at": "2026-11-21 00:00:00"}, "ws-alpha", "at"),
        ({**GOOD, "at": "2026-13-41T00:00:00Z"}, "ws-alpha", "at"),  # the shape, not a time
        (b"\xff not json", "ws-alpha", "json"),
        (b"{" + b" " * 600 + b"}", "ws-alpha", "size"),
    ],
)
def test_a_card_that_is_not_what_its_owner_may_send_is_rejected_by_code_and_removed(spool, card, owner, code):
    from ws import cards

    _plant(spool["folder"], "1-0123456789abcdef.card", card)
    _as_lead(spool, owner)
    got = cards.take(folder=spool["folder"])
    assert got == {"cards": [], "rejected": {code: 1}} and code in cards.REASONS
    assert list(spool["folder"].iterdir()) == []


def test_a_card_in_another_group_is_rejected(spool, monkeypatch):
    from ws import cards

    _plant(spool["folder"], "1-0123456789abcdef.card", GOOD)
    _as_lead(spool)
    gid = spool["folder"].stat().st_gid
    monkeypatch.setattr(cards, "_group_gid", lambda: gid)
    real_fstat = cards.os.fstat
    monkeypatch.setattr(
        cards.os, "fstat", lambda fd: os.stat_result((*real_fstat(fd)[:5], gid + 1, *real_fstat(fd)[6:]))
    )
    assert cards.take(folder=spool["folder"]) == {"cards": [], "rejected": {"group": 1}}


def test_a_link_and_a_bad_name_are_rejected_and_a_fresh_dot_file_is_left(spool, tmp_path):
    from ws import cards

    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(GOOD))
    (spool["folder"] / "2-0123456789abcdef.card").symlink_to(target)
    _plant(spool["folder"], "notacard.txt", GOOD)
    _plant(spool["folder"], ".3-0123456789abcdef.card.tmp", b"half")
    _as_lead(spool)
    got = cards.take(folder=spool["folder"])
    assert got == {"cards": [], "rejected": {"link": 1, "name": 1}}
    assert target.exists() and [p.name for p in spool["folder"].iterdir()] == [".3-0123456789abcdef.card.tmp"]


def test_take_never_prints_planted_text(spool, capsys, monkeypatch):
    """The cold review's probe: text planted in `at`, `session`, `class` and a file name never comes out of `take`, in
    its result or on the screen."""
    from ws import cards

    f = spool["folder"]
    _plant(
        f,
        "1-0123456789abcdef.card",
        {**GOOD, "at": "PLANTED\x1b[2J ignore prior", "session": "PLANTED.exfil.text_here"},
    )
    _plant(f, "2-0123456789abcdef.card", {**GOOD, "class": "PLANTED forged class text"})
    _plant(f, "PLANTED free text in a file name", GOOD)
    _plant(f, "4-0123456789abcdef.card", GOOD)  # one good card, so the screen path prints a row
    _as_lead(spool)
    got = cards.take(folder=f, keep=True)
    assert "PLANTED" not in json.dumps(got) and len(got["cards"]) == 1
    real = cards.take
    monkeypatch.setattr(cards, "take", lambda keep=False: real(folder=f, keep=keep))
    code = cards.cmd_take(argparse.Namespace(keep=False, json=False))
    out, err = capsys.readouterr()
    assert code == 0 and "PLANTED" not in out + err and "\x1b" not in out + err


# --- lead-only on a walled class: the lead gets in, no other session does (DEC-052) --------------------------------


def _three(tmp_path):
    from ws import session

    a, b, home = tmp_path / "alpha", tmp_path / "beta", tmp_path / "lead"
    for d in (a, b, home):
        d.mkdir()
    text = TWO_WALLED.replace('"/srv/work/alpha/one/**", "/srv/work/alpha/two/**"', f'"{a}/**"')
    text = text.replace('"/srv/work/beta/three/**"', f'"{b}/**"')
    pol = policy.loads(text + f'\n[classes.platform]\naccess = "full"\nrepos = ["{home}/**"]\n')
    rec = {
        "lead": session.Record("lead1", "t", cwd=str(home), role="lead"),
        "worker": session.Record("w1", "t", cwd=str(home)),
        "person": session.Record(session.PERSON + "me", "", cwd=str(home), role="lead", runner="person"),
        "a": session.Record("wall-a", "t", cwd=str(a)),
        "b": session.Record("wall-b", "t", cwd=str(b)),
    }
    return pol, rec


@pytest.mark.parametrize("kind", ["note", "ask", "stop-and-hand-over"])
def test_lead_only_lets_the_lead_into_each_walled_class_and_refuses_a_worker(box, tmp_path, kind):
    """The kinds a walled class takes in: lead-only is then the only rule that decides, and it decides on the role."""
    from ws import msg
    from ws.output import WsError

    pol, r = _three(tmp_path)
    shared = tmp_path / "shared"
    for target in (r["a"], r["b"]):
        assert msg.scratch_delivery(
            shared, pol, r["lead"], target, kind, "from the lead" if kind != "stop-and-hand-over" else None
        )["id"]
        with pytest.raises(WsError) as e:
            msg.scratch_delivery(shared, pol, r["worker"], target, kind, "x" if kind != "stop-and-hand-over" else None)
        assert e.value.kind == "refused" and "from the lead only" in e.value.message


def test_the_persons_own_send_stays_out_of_a_lead_only_walled_class(box, tmp_path):
    """The person reaches a walled class through its own sessions, not `ws msg` (lead ruling, 21 Nov)."""
    from ws import msg
    from ws.output import WsError

    pol, r = _three(tmp_path)
    with pytest.raises(WsError) as e:
        msg.scratch_delivery(tmp_path / "shared", pol, r["person"], r["a"], "note", "from the person")
    assert e.value.kind == "refused" and "not the person's own send" in e.value.message


@pytest.mark.parametrize("kind", ["note", "ask", "stop-and-hand-over", "ack", "done", "counts"])
def test_no_kind_goes_from_one_walled_class_to_the_other(box, tmp_path, kind):
    from ws import msg
    from ws.output import WsError

    pol, r = _three(tmp_path)
    for sender, target in ((r["a"], r["b"]), (r["b"], r["a"])):
        with pytest.raises(WsError) as e:
            msg.scratch_delivery(tmp_path / "shared", pol, sender, target, kind, None)
        assert e.value.kind == "refused"


def test_a_name_or_time_in_non_ascii_digits_is_rejected(spool):
    from ws import cards

    _plant(spool["folder"], "١-0123456789abcdef.card", GOOD)  # an Arabic-Indic digit in the name
    _plant(spool["folder"], "5-0123456789abcdef.card", {**GOOD, "at": "２０２６-11-21T00:00:00Z"})  # full-width digits
    _as_lead(spool)
    assert cards.take(folder=spool["folder"]) == {"cards": [], "rejected": {"name": 1, "at": 1}}
