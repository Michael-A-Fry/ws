"""A person sends a `ws msg` from outside any session, and the session prepare step records who asked (hub chat 1b).

The registry stand-ins are test_msg's. A caller-less process is `world.who = None`: no stamped runner among its ancestors,
which is also what every ancestry escape (setsid, nohup, systemd-run --user, at, tmux new-window, cron) gives an agent.
None of those is proof of a person, so each must be refused without the per-boot token.
"""

from __future__ import annotations

import io
import json
import os
import sys

import pytest
from test_msg import World, classed, world  # noqa: F401, F811 - the fixture, imported for the same stand-ins

from ws import cli, msg, paths, policy, session
from ws.output import WsError

ON = "[msg]\nperson_send = true\n"
USER = session.PERSON + session.account_user()
ESCAPES = ("setsid -f", "nohup", "systemd-run --user", "at now", "tmux new-window", "cron")  # all: no runner ancestor
T0 = 1_700_000_040.0  # the start of a rate window


def token() -> None:
    session.init_person_token(session.trusted_env())


def alone(world: World, text: str = ON) -> World:
    world.policy(text)
    world.session("alpha", world.folder("alpha"))
    world.session("lead1", world.folder("lead1"), role="lead")
    return world


def person_send(world: World, to: str = "alpha", kind: str = "note", body: str | None = "hello", **kw) -> dict:
    world.who = None
    return msg.send(to, kind, body, allow_person=True, **kw)


def test_off_is_todays_behaviour(world):
    alone(world, "")  # no [msg] at all
    token()
    with pytest.raises(WsError) as e:
        person_send(world)
    assert e.value.kind == "refused" and "can't tell which session" in e.value.message
    assert world.count() == 0
    world.policy("[msg]\nperson_send = false\n")
    with pytest.raises(WsError):
        person_send(world)


def test_person_send_records_the_person(world):
    alone(world)
    token()
    out = person_send(world, kind="ask", body="status?")
    assert world.rows('SELECT "from", "to" FROM messages') == [(USER, "alpha")]
    assert out["hop"] == 0
    feed = msg.watch("alpha")
    assert [e["from"] for e in feed["events"]] == [USER]


@pytest.mark.parametrize("escape", ESCAPES)
def test_ancestry_escape_without_the_token_is_refused(world, escape):
    alone(world)  # switch on, but no token: inside the wall the secrets folder is hidden
    with pytest.raises(WsError) as e:
        person_send(world)
    assert e.value.kind == "refused", escape
    assert world.count() == 0


def test_planted_token_without_the_digest_is_refused(world):
    alone(world)
    path, digest = session._person_paths(session.trusted_env())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a" * 64 + "\n")
    os.chmod(path, 0o600)  # an agent that wrote its own token under an overlay of the hidden folder
    with pytest.raises(WsError):
        person_send(world)
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_text("0" * 64 + "\n")
    os.chmod(digest, 0o600)
    with pytest.raises(WsError):
        person_send(world)


def test_a_loose_token_is_refused(world):
    alone(world)
    token()
    path, _ = session._person_paths(session.trusted_env())
    os.chmod(path, 0o644)
    with pytest.raises(WsError):
        person_send(world)


def test_a_new_token_retires_the_old_one(world):
    alone(world)
    token()
    path, _ = session._person_paths(session.trusted_env())
    old = path.read_text()
    token()
    assert path.read_text() != old and session.person_proof()
    path.write_text(old)
    assert not session.person_proof()


def test_no_wall_in_policy_refuses(world):
    alone(world, "[gate]\nsandbox = false\n" + ON)
    token()
    with pytest.raises(WsError):
        person_send(world)


def test_the_resident_gate_child_is_never_a_person(world, monkeypatch):
    alone(world)
    token()
    monkeypatch.setattr(session, "_PEER", [None])  # macOS: bind_peer(None)
    with pytest.raises(WsError):
        person_send(world)


def test_a_session_still_sends_as_itself_even_with_the_token(world):
    alone(world)
    token()
    world.send("lead1", "alpha")
    assert world.rows('SELECT "from" FROM messages') == [("lead1",)]
    world.who = None
    with pytest.raises(WsError):  # the library call without allow_person (the mcp server) never takes a person path
        msg.send("alpha", "note", "x")


def test_a_walled_class_refuses_the_person_by_default_and_takes_a_named_one(world):
    classed(world)
    plain = (world.root / "wshome" / "policy.toml").read_text()
    world.policy(plain + ON)
    token()
    with pytest.raises(WsError) as e:
        person_send(world, "wall1", "note", "hello")
    assert e.value.kind == "refused"
    named = plain.replace('inbound_allow = ["lead1"]', f'inbound_allow = ["lead1", "{USER}"]')
    assert named != plain
    world.policy(named + ON)
    assert person_send(world, "wall1", "note", "hello")["class"] == "secret"
    with pytest.raises(WsError):  # the walled receiver still takes only its inbound kinds
        person_send(world, "wall1", "ack", "x")


def test_the_person_has_a_rate_cap_of_its_own(world):
    alone(world)
    token()
    for _ in range(msg.PERSON_RATE_PER_MINUTE):
        person_send(world, now=T0)
    with pytest.raises(WsError) as e:
        person_send(world, now=T0)
    assert e.value.kind == "rate_limited"
    assert world.send("lead1", "alpha", now=T0)["id"]  # a session's own count is separate


def test_secret_scan_and_caps_still_apply(world):
    alone(world)
    token()
    with pytest.raises(WsError) as e:
        person_send(world, body="key " + "AKIA" + "ABCDEFGHIJKLMNOP")
    assert e.value.kind == "secret_refused"
    with pytest.raises(WsError) as e:
        person_send(world, body="x" * (msg.BODY_MAX_BYTES + 1))
    assert e.value.kind == "too_large"


def test_cli_send_from_stdin_as_the_person(world, monkeypatch):
    alone(world)
    token()
    world.who = None
    monkeypatch.setattr(sys, "stdin", io.StringIO("from the hub"))
    assert cli.main(["msg", "send", "alpha", "-"]) == 0
    assert world.rows('SELECT "from", body FROM messages') == [(USER, "from the hub")]


def test_person_token_command_is_refused_inside_a_session(world):
    alone(world)
    args = type("A", (), {"json": False})()
    world.who = "alpha"
    with pytest.raises(WsError):
        msg.cmd_person_token(args)
    world.who = None
    assert msg.cmd_person_token(args) == 0 and session.person_proof()


def test_inbound_allow_takes_a_person_and_msg_keys_are_checked():
    ok = policy.loads(
        '[classes.c]\naccess="walled"\nrepos=["/x"]\ninbound="allowlist"\ninbound_allow=["person:me.x"]\n'
    )
    assert ok.classes["c"].inbound_allow == ("person:me.x",)
    with pytest.raises(policy.PolicyError):
        policy.loads('[msg]\nperson_send = "yes"\n')
    with pytest.raises(policy.PolicyError):
        policy.loads("[msg]\nsender = 'x'\n")
    assert policy.loads("").msg.person_send is False


# --- who asked ---------------------------------------------------------------------------------------------------------


def start(world: World, sid: str, launch: str | None = None) -> session.Record:
    before = world.who
    if launch:
        world.mp.setenv(session.LAUNCH_ENV, launch)
    world.mp.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": sid, "cwd": str(world.folder(sid))})))
    world.procs.setdefault(sid, session.Proc(300 + len(world.procs), "t", 1, "claude"))
    world.who = sid
    assert cli.main(["session", "start", "--runner", "claude-code", *paths.pin_of().args()]) == 0
    world.mp.delenv(session.LAUNCH_ENV, raising=False)
    world.who = before
    return session.read(sid)


def test_requested_by_a_session(world):
    alone(world)
    world.who = "lead1"
    got = session.asked_by()
    assert got == "lead1"
    rec = start(world, "child", session.prepare_launch("worker", requested_by=got))
    assert rec.requested_by == "lead1"
    assert json.loads(session._record_path(session.trusted_env(), "child").read_text())["requested_by"] == "lead1"


def test_requested_by_the_person_needs_the_proof(world):
    alone(world)
    world.who = None
    assert session.asked_by() == "unknown"  # switch on, no token
    token()
    assert session.asked_by() == USER
    world.policy("")
    assert session.asked_by() == "unknown"  # switch off
    world.policy(ON)
    rec = start(world, "kid", session.prepare_launch("worker", requested_by=session.asked_by()))
    assert rec.requested_by == USER


def test_prepare_prints_who_asked_and_a_resume_keeps_it(world, capsys):
    alone(world)
    token()
    world.who = None
    assert cli.main(["--json", "session", "prepare", "--role", "worker"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["requested_by"] == USER
    first = start(world, "kid2", session.prepare_launch("worker", requested_by=USER))
    again = start(world, "kid2")  # a resumed start, no launch token
    assert first.requested_by == again.requested_by == USER


def test_an_old_record_reads_as_unknown_and_show_prints_it(world, capsys):
    alone(world)
    rec = session.read("alpha")
    assert rec.requested_by == "unknown"
    assert "requested_by" not in rec.to_dict()
    assert cli.main(["session", "show", "alpha"]) == 0
    assert "requested_by: unknown" in capsys.readouterr().out


def test_a_forged_requested_by_in_the_sessions_file_is_not_believed(world):
    alone(world)
    path = session._record_path(session.trusted_env(), "alpha")
    data = json.loads(path.read_text())
    data["requested_by"] = USER  # the sessions folder is writable from inside the wall; the identity folder is not
    path.write_text(json.dumps(data))
    assert session.read("alpha").requested_by == "unknown"
