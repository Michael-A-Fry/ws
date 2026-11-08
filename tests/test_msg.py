"""`ws msg`: the message store, who may send to whom, the walled-class rules, the caps, and the typed failures.

Every test sets HOME and WS_HOME below tmp_path. Who is calling comes from the process tree, so the tests stand in for it:
`World` gives each session a fake runner process and says which one the test is "under"; two tests use real processes.
Values that look like a secret are put together at run time.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

from ws import cli, mcp_server, msg, paths, session
from ws.output import WsError

BASE = 1_700_000_040.0  # a multiple of 60: the start of a rate window


class World:
    """Sessions with stamped runners, and a current caller. `session.resolve_caller` sees the caller's runner as an ancestor."""

    def __init__(self, root: Path, mp: pytest.MonkeyPatch):
        self.root, self.mp, self.procs, self.who = root, mp, {}, None
        mp.setattr(session, "_ancestors", self._chain)

    def _chain(self) -> list[session.Proc]:
        shell = session.Proc(5, "5", 4, "bash")
        return [shell, self.procs[self.who]] if self.who else [shell]

    def folder(self, name: str, git: bool = False) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        if git:
            (path / ".git").mkdir(exist_ok=True)
        return path

    def policy(self, text: str) -> None:
        (self.root / "wshome").mkdir(exist_ok=True)
        (self.root / "wshome" / "policy.toml").write_text(textwrap.dedent(text))

    def session(self, sid: str, cwd: Path, role: str = "worker", proc: session.Proc | None = None) -> None:
        pid = proc.pid if proc else 100 + len(self.procs)
        self.procs[sid] = proc or session.Proc(pid, f"t{pid}", 1, "claude")
        before, self.who = self.who, sid
        if role != "worker":
            self.mp.setenv(session.LAUNCH_ENV, session.prepare_launch(role))
        self.mp.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": sid, "cwd": str(cwd)})))
        assert cli.main(["session", "start", "--runner", "claude-code", *paths.pin_of().args()]) == 0
        self.mp.delenv(session.LAUNCH_ENV, raising=False)
        self.who = before
        assert session.read(sid).runner_pid == pid and session.read(sid).role == role

    def send(self, sender: str | None, to: str, kind: str = "note", body: str | None = "hello", **kw) -> dict:
        self.who = sender
        return msg.send(to, kind, body, **kw)

    def refused(self, sender: str, to: str, *args, kind: str = "refused", **kw) -> WsError:
        with pytest.raises(WsError) as e:
            self.send(sender, to, *args, **kw)
        assert e.value.kind == kind, e.value.message
        return e.value

    def count(self, table: str = "messages") -> int:
        """Rows in a table of the shared store; 0 when nothing has made the store yet."""
        return self.rows(f"SELECT COUNT(*) FROM {table}")[0][0] if msg.store_path().exists() else 0

    def rows(self, sql: str, db: Path | None = None) -> list[tuple]:
        conn = sqlite3.connect(db or msg.store_path())
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # the account's own folders are the test's (the real ones are the system's home, which a test must not touch)
    monkeypatch.setattr(
        session, "trusted_env", lambda: {"HOME": str(tmp_path / "home"), "WS_HOME": str(tmp_path / "wshome")}
    )
    for var in ("WS_RELEASE", "WS_RELEASE_FILE", "TMUX_PANE", session.LAUNCH_ENV):
        monkeypatch.delenv(var, raising=False)
    return World(tmp_path, monkeypatch)


@pytest.fixture
def plain(world):
    """Two sessions and a policy with no [classes] at all: the classless rules."""
    for sid in ("alpha", "bravo", "carol"):
        world.session(sid, world.folder(sid))
    return world


def classed(world, allow=("lead1", "work1", "work2", "wall1"), walled_inbound="lead-only", walled_allow=("work1",)):
    """A lead and two workers in a full class, and one session in a walled repo, under a policy with classes."""
    open_dir, walled = world.folder("open"), world.folder("walled", git=True)
    world.policy(
        f"""
        [classes.secret]
        access = "walled"
        repos = ["{walled}"]
        inbound = "{walled_inbound}"
        {f"inbound_allow = {json.dumps(list(walled_allow))}" if walled_inbound == "allowlist" else ""}
        retention_days = 5
        [classes.open]
        access = "full"
        repos = ["{open_dir}"]
        inbound = "allowlist"
        inbound_allow = {json.dumps(list(allow))}
        retention_days = 10
        """
    )
    world.session("lead1", world.folder("open/lead"), role="lead")
    world.session("work1", world.folder("open/w1"))
    world.session("work2", world.folder("open/w2"))
    world.session("wall1", world.folder("walled/src"))
    return world


def planted(prefix: str = "canary") -> str:
    return prefix + secrets.token_hex(8)


def class_db(world: Path | World, name: str = "secret") -> Path:
    root = world.root if isinstance(world, World) else world
    return root / "walled" / ".ws" / "messages" / f"{name}.db"


# --- C51 M8: the walled-class rules -----------------------------------------------------------------------------------


def test_walled_free_text_send_refused(world):  # (a)
    classed(world)
    for kind, body in (("note", "any words at all"), ("ask", "a question"), ("ack", "an ack with text")):
        world.refused("wall1", "lead1", kind, body)
    assert world.count() == 0
    sent = world.send("wall1", "lead1", "done", None)  # the fixed kind, no text
    assert sent["size"] == "S" and world.send("wall1", "lead1", "stop-and-hand-over:done", None)["hop"] == 0
    world.refused("wall1", "lead1", "ask:pending", "text")
    assert world.rows("SELECT body, sha256, has_body FROM messages") == [(None, None, 0)] * 2


def test_non_lead_message_to_walled_session_refused(world):  # (b)
    classed(world)
    world.refused("work1", "wall1")  # lead-only: a worker is not the lead
    assert world.send("lead1", "wall1", "note", "hello")["class"] == "secret"
    for kind in ("ack", "done", "counts", "ask:pending"):
        world.refused("lead1", "wall1", kind, None)  # only note, ask and stop-and-hand-over go in
    assert world.send("lead1", "wall1", "stop-and-hand-over", None)["id"]
    assert world.rows("SELECT COUNT(*) FROM messages") == [(2,)]


def test_walled_inbound_off_by_default_even_for_the_lead(world):
    classed(world, walled_inbound="off")
    err = world.refused("lead1", "wall1")
    assert "take no messages" in err.message


def test_walled_inbound_allowlist_names_who_may(world):
    classed(world, walled_inbound="allowlist")
    assert world.send("work1", "wall1")["class"] == "secret"  # named
    world.refused("work2", "wall1")  # not named
    err = world.refused("lead1", "wall1")  # and being the lead is no exception
    assert "named sessions" in err.message


def test_shared_db_holds_no_walled_body_canary(world):  # (c)
    classed(world)
    token = planted()
    sent = world.send("lead1", "wall1", "note", f"look at {token} please")
    plain_token = planted("open")
    world.send("lead1", "work1", "note", f"look at {plain_token} please")
    text = hashlib.sha256(f"look at {token} please".encode()).hexdigest()
    shared = [p for p in msg.store_path().parent.glob("messages.db*")]
    raw = b"".join(p.read_bytes() for p in shared)
    conn = sqlite3.connect(msg.store_path())
    dump = "\n".join(conn.iterdump())
    conn.close()
    for blob in (raw, dump.encode()):
        assert token.encode() not in blob and text.encode() not in blob  # no body, no hash of it
        assert plain_token.encode() in blob  # the non-walled message is there (the scan can see a body)
    assert world.rows(f"SELECT body, sha256, size_bytes, has_body FROM messages WHERE id={sent['id']}") == [
        (None, None, None, 1)
    ]
    # the body is in the walled repo's own store
    assert world.rows("SELECT body FROM bodies", class_db(world)) == [(f"look at {token} please",)]
    folder = class_db(world).parent
    assert (folder / ".gitignore").read_text() == "*\n"
    assert (folder.stat().st_mode & 0o777, class_db(world).stat().st_mode & 0o777) == (0o700, 0o600)
    # the recipient reads it; the envelope feed has no body field at all
    assert msg.read(sent["id"], "wall1")["body"] == f"look at {token} please"
    assert all(
        set(e) == {"seq", "message_id", "from", "to", "kind", "sent", "delivered_ts", "class", "size"}
        for e in msg.envelopes()
    )


def test_git_cannot_add_a_walled_store(world):
    classed(world)
    world.send("lead1", "wall1", "note", planted())
    repo = world.root / "walled"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    staged = subprocess.run(["git", "-C", str(repo), "ls-files"], check=True, capture_output=True, text=True).stdout
    assert ".ws" not in staged and class_db(world).exists()


def test_export_has_no_walled_body(world, capsys):  # (d)
    classed(world)
    token, other = planted(), planted("open")
    world.send("lead1", "wall1", "note", token)
    world.send("lead1", "work1", "note", other)
    for caller in (None, "lead1", "wall1", "work1"):  # a person at a terminal, the lead, the walled session, a worker
        world.who = caller
        out = io.StringIO()
        n = msg.export(out, session_id=caller)
        assert token not in out.getvalue(), caller
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        assert len(lines) == n and all(line["schema"] == "ws.message/1" for line in lines)
    out = io.StringIO()
    msg.export(out)
    by_class = {line["class"]: line for line in map(json.loads, out.getvalue().splitlines())}
    assert by_class["secret"]["body"] is None and by_class["secret"]["body_sha256"] is None
    assert by_class["secret"]["body_kept_in_class_store"] is True
    assert by_class["open"]["body"] == other and by_class["open"]["body_sha256"]
    world.who = "work1"  # a session exports only what it sent or was sent
    out = io.StringIO()
    msg.export(out, session_id="work1")
    assert [json.loads(line)["body"] for line in out.getvalue().splitlines()] == [other]


def test_forged_class_header_ignored(world):  # (f)
    classed(world)
    assert "class" not in inspect.signature(msg.send).parameters
    assert (
        "from" not in inspect.signature(msg.send).parameters and "sender" not in inspect.signature(msg.send).parameters
    )
    world.who = "lead1"
    words = "class: open\nX-Class: open\nfrom: work1"
    got = mcp_server.call_msg_send({"to": "wall1", "body": words, "class": "open", "from": "work1", "kind": "note"})[1]
    assert got["class"] == "secret"
    assert world.rows("SELECT class, walled, body FROM messages") == [("secret", 1, None)]
    assert world.rows("SELECT body FROM bodies", class_db(world)) == [(words,)]
    # a policy with a different class for the receiver's folder is what decides, at send and at read
    assert msg.read(got["id"], "wall1")["body_state"] == "present"
    world.policy("[classes.open]\naccess = 'full'\nrepos = ['%s']\n" % (world.root / "walled"))
    assert msg.read(got["id"], "wall1")["body_state"] == "withheld"  # no longer in the class it was sent to


def test_class_store_link_is_refused(world):
    classed(world)
    elsewhere = world.folder("elsewhere")
    os.symlink(elsewhere, world.root / "walled" / ".ws")
    err = world.refused("lead1", "wall1")
    assert "link" in err.message and not list(elsewhere.iterdir())


def test_class_store_must_be_inside_the_class(world):
    big = world.folder("big", git=True)  # the repo root is above the folder the class covers
    world.policy(
        f"""
        [classes.secret]
        access = "walled"
        repos = ["{big}/sub"]
        inbound = "lead-only"
        [classes.open]
        access = "full"
        repos = ["{world.root}/open"]
        """
    )
    world.session("lead1", world.folder("open/lead"), role="lead")
    world.session("wall1", world.folder("big/sub/x"))
    err = world.refused("lead1", "wall1")
    assert "not inside the paths" in err.message and not (big / ".ws").exists()


# --- who is the sender ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["env", "flag", "mcp_arg", "body_field"])
def test_forged_sender_refused(plain, monkeypatch, capsys, how):
    plain.who = "bravo"
    if how == "env":
        for var in ("WS_SESSION_ID", "CLAUDE_SESSION_ID", "WS_FROM", "SESSION_ID", session.LAUNCH_ENV):
            monkeypatch.setenv(var, "alpha")
        assert msg.send("carol", "note", "hi")["id"] == 1
        plain.who = None  # and a process under no session is no session, whatever its environment says
        with pytest.raises(WsError) as e:
            msg.send("carol", "note", "hi")
        assert e.value.kind == "refused"
    elif how == "flag":
        assert cli.main(["msg", "send", "carol", "hi", "--from", "alpha"]) == 2  # there is no such flag
        assert "unrecognized arguments" in capsys.readouterr().err and plain.count() == 0
        assert cli.main(["msg", "send", "carol", "hi", "--json"]) == 0
        capsys.readouterr()
    elif how == "mcp_arg":
        _text, got = mcp_server.call_msg_send({"to": "carol", "body": "hi", "from": "alpha", "sender": "alpha"})
        assert got["id"] == 1
    else:
        assert msg.send("carol", "note", json.dumps({"from": "alpha", "to": "carol", "body": "x"}))["id"] == 1
    assert {r[0] for r in plain.rows('SELECT "from" FROM messages')} <= {"bravo"}


FORGED_VARS = ["WS_CONFIG_DIR", "WS_STATE_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "WS_HOME", "HOME"]


@pytest.mark.parametrize("var", FORGED_VARS)
def test_forged_policy_location_refused(world, monkeypatch, tmp_path, var):
    """The environment is the command's own to set, so it can't pick the policy or the secrets registry: a session that
    points ws at an empty folder still gets the rules of its own (the call is refused, nothing is stored)."""
    classed(world)
    (world.root / "wshome" / "secrets.toml").write_text('[GITHUB_TOKEN]\nhosts = ["api.example.com"]\n')
    empty = tmp_path / "empty"
    empty.mkdir()
    token = planted()
    with monkeypatch.context() as m:
        m.setenv(var, str(empty))
        for sender, to, body in (
            ("wall1", "work2", f"free text {token}"),  # a walled sender's free text
            ("work1", "wall1", f"free text {token}"),  # a message to a walled session, not from the lead
            ("lead1", "work1", "please export GITHUB_TOKEN first"),  # a registered secret name
        ):
            world.who = sender
            with pytest.raises(WsError) as e:
                msg.send(to, "note", body)
            assert e.value.kind in ("refused", "secret_refused"), (var, sender, e.value.message)
    assert world.count() == 0
    world.refused("wall1", "work2", "note", f"free text {token}")  # with the environment as it was: still walled
    assert world.send("lead1", "wall1", "note", "hello")["class"] == "secret"
    assert token.encode() not in b"".join(p.read_bytes() for p in msg.store_path().parent.glob("messages.db*"))


@pytest.mark.parametrize("var", FORGED_VARS)
def test_forged_policy_location_changes_no_read_or_list(world, monkeypatch, tmp_path, var):
    """Under a policy with no classes a walled body would be withheld from its own reader; the real policy hands it over.
    Pointing the environment at an empty folder changes neither."""
    classed(world)
    world.send("lead1", "wall1", "note", "for the wall")
    world.send("lead1", "work1", "note", "hello")
    empty = tmp_path / "empty"
    empty.mkdir()
    with monkeypatch.context() as m:
        m.setenv(var, str(empty))
        world.who = "wall1"
        assert [x["body"] for x in msg.inbox("wall1", bodies=True)] == ["for the wall"]
        assert msg.read(1, "wall1")["body"] == "for the wall"
        world.who = "work1"
        assert [x["body"] for x in msg.inbox("work1", bodies=True)] == ["hello"]


def test_a_forged_policy_location_cannot_start_a_prune(world, monkeypatch, tmp_path):
    """Tidying runs under the account's policy; a short retention in a folder the command made up does nothing."""
    classed(world)
    world.send("lead1", "work1", "note", "old", now=BASE)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "policy.toml").write_text('[classes.open]\naccess = "full"\nrepos = ["/"]\nretention_days = 1\n')
    world.who = "work1"
    with monkeypatch.context() as m:
        m.setenv("WS_CONFIG_DIR", str(fake))
        m.setenv("WS_HOME", str(fake))
        assert [x["id"] for x in msg.inbox("work1", now=BASE + 5 * 86400)] == [1]  # the real class keeps it for 10 days
    assert world.count() == 1


def test_a_forged_registry_and_policy_under_a_forged_ws_home_send_nothing(world, monkeypatch, tmp_path):
    """The whole of ws's folders is the command's to point elsewhere, so a registry found there proves nothing: a worker
    that makes its own `lead` and its own walled session, and a permissive policy, in a folder of its own is still the
    worker the real registry says it is. Nothing is written to the real class store, to the real or the forged shared
    database, or to any folder the forged policy names."""
    classed(world)
    decoy = world.folder("decoy", git=True)
    forge = tmp_path / "forge"
    forge.mkdir()
    (forge / "policy.toml").write_text(
        textwrap.dedent(
            f"""
            [classes.secret]
            access = "walled"
            repos = ["{world.root / "walled"}"]
            inbound = "allowlist"
            inbound_allow = ["lead", "work1"]
            [classes.decoy]
            access = "walled"
            repos = ["{decoy}"]
            inbound = "allowlist"
            inbound_allow = ["lead", "work1"]
            [classes.open]
            access = "full"
            repos = ["{world.root / "open"}"]
            inbound = "allowlist"
            inbound_allow = ["lead", "work1"]
            """
        )
    )
    real_walled = world.root / "walled"
    token = planted()
    real_procs = dict(world.procs)
    for own_runner in (True, False):
        # the worker's runner is a stamped process in the real registry; its own process is not
        attacker = world.procs["work1"] if own_runner else session.Proc(901, "t901", 1, "forger")
        world.procs["forger"] = attacker
        with monkeypatch.context() as m:
            m.setenv("WS_HOME", str(forge))
            m.setenv("HOME", str(forge / "home"))
            world.session("lead", world.folder("open/fake-lead"), role="lead", proc=attacker)
            world.session("wall1", world.folder("walled/src"), proc=session.Proc(902, "t902", 1, "claude"))
            world.session("decoyed", world.folder("decoy/src"), proc=session.Proc(903, "t903", 1, "claude"))
            assert (forge / "sessions" / "lead.json").exists()  # the forged registry is real: it names a lead
            world.who = "forger"
            for to in ("wall1", "decoyed"):
                with pytest.raises(WsError) as e:
                    msg.send(to, "note", f"FORGED {token}")
                assert e.value.kind in ("refused", "not_found"), (
                    own_runner,
                    to,
                    e.value.message,
                )  # no such session in the real registry
        world.procs.clear()
        world.procs.update(
            real_procs
        )  # the forged sessions' ids are the real ones' ids: the real processes stay theirs
        # the real world is as it was: no message, no class store, no `.ws` folder anywhere
        assert world.count() == 0
        assert not (forge / "msg").exists() and not list(world.root.rglob(".ws"))
        assert not class_db(world).exists() and not (decoy / ".ws").exists() and not (real_walled / ".ws").exists()
    # and the real lead's order still lands, undisturbed by the forged one
    assert world.send("lead1", "wall1", "note", "REAL ORDER")["id"] == 1
    world.who = "wall1"
    got = msg.read(1, "wall1")
    assert (got["body"], got["from"]) == ("REAL ORDER", "lead1")
    assert token.encode() not in b"".join(p.read_bytes() for p in msg.store_path().parent.glob("messages.db*"))


def test_nothing_in_msg_takes_an_environment():
    """No function of the public API can be handed an environment, and the layout answers to `session.trusted_env` only."""
    for name in ("send", "inbox", "sent", "read", "ack", "mark_delivered", "envelopes", "export", "prune", "caller"):
        assert "env" not in inspect.signature(getattr(msg, name)).parameters, name
    assert not inspect.signature(msg.store_path).parameters
    assert not inspect.signature(session.resolve_caller).parameters
    source = inspect.getsource(msg)
    assert "merged_environ" not in source and "os.environ" not in source and "policy_path" not in source


def test_orphaned_process_cannot_send(plain):
    """A process that double-forks is owned by init, not by the runner: it is no session, so it cannot send. The same code
    as a plain child of the runner can."""
    me = session.proc_info(os.getpid())
    plain.session("alpha", plain.folder("alpha"), proc=me)  # the test process stands in for the runner of "alpha"
    assert session.read("alpha").runner_start == me.start
    out = plain.root / "out.txt"
    script = textwrap.dedent(
        """
        import os, sys, time
        out, orphan = sys.argv[1], sys.argv[2] == "orphan"
        if orphan:
            if os.fork():
                os._exit(0)
            os.setsid()
            if os.fork():
                os._exit(0)
            time.sleep(0.3)  # the parents are gone: this process belongs to init
        import json
        from ws import msg, session
        env = json.loads(sys.argv[3])
        session.trusted_env = lambda: env  # the test's folders, as the fixture has them in this process
        try:
            res = "sent %d" % msg.send("bravo", "note", "hi")["id"]
        except Exception as e:
            res = type(e).__name__ + ":" + getattr(e, "kind", "")
        open(out, "w").write(res)
        """
    )
    env = {**os.environ}
    folders = json.dumps(session.trusted_env())
    subprocess.run([sys.executable, "-c", script, str(out), "child", folders], env=env, check=True, timeout=60)
    assert out.read_text() == "sent 1"  # a child of the runner: its sender is alpha
    out.unlink()
    subprocess.run([sys.executable, "-c", script, str(out), "orphan", folders], env=env, check=True, timeout=60)
    deadline = time.monotonic() + 15
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert out.read_text() == "WsError:refused"
    assert plain.rows("SELECT COUNT(*) FROM messages") == [(1,)]


def test_unknown_recipient_not_found(plain):
    assert plain.refused("alpha", "nobody", kind="not_found")
    plain.refused("alpha", "bad id!", kind="bad_input")


# --- replay, keys and threads ---------------------------------------------------------------------------------------


def test_replay_returns_first_id(plain):
    first = plain.send("alpha", "bravo", body="one", key="job-1")
    again = plain.send("alpha", "bravo", body="different words", key="job-1")
    assert (again["id"], again["replayed"], first["replayed"]) == (first["id"], True, False)
    assert plain.rows("SELECT COUNT(*), MIN(body) FROM messages") == [(1, "one")]
    other = plain.send("bravo", "alpha", body="x", key="job-1")  # the key is the sender's own
    assert other["id"] != first["id"]
    # the key is not stored as given
    assert plain.rows("SELECT idem FROM messages WHERE id=1")[0][0] == hashlib.sha256(b"alpha\0job-1").hexdigest()
    plain.refused("alpha", "bravo", key="has space", kind="bad_input")


def test_walled_sender_cannot_set_key_or_thread_text(world):  # I4
    classed(world)
    for extra in ({"key": "k1"}, {"thread": "free-text-thread"}, {"key": "k", "thread": "t"}):
        err = world.refused("wall1", "lead1", "done", None, **extra)
        assert "can't set" in err.message
    world.refused("wall1", "lead1", "done", "a body")
    # nor can anyone put a thread name on a message to a walled class; a reply stays in its parent's thread
    world.refused("lead1", "wall1", "note", "x", thread="named")
    first = world.send("lead1", "wall1", "note", "x")
    answer = world.send("wall1", "lead1", "done", None, reply_to=first["id"])
    assert answer["thread_id"] == first["thread_id"] == str(first["id"]) and answer["hop"] == 1
    world.refused("wall1", "lead1", "done", None, reply_to=9999, kind="not_found")  # only an existing id
    assert world.rows("SELECT COUNT(*) FROM messages WHERE idem IS NOT NULL") == [(0,)]


def test_threads_and_replies_stay_together(plain):
    top = plain.send("alpha", "bravo", body="start", thread="deploy-7")
    reply = plain.send("bravo", "alpha", body="ok", reply_to=top["id"])
    assert reply["thread_id"] == "deploy-7" and reply["hop"] == 1
    plain.refused("bravo", "alpha", reply_to=top["id"], thread="another", kind="bad_input")
    plain.refused("carol", "alpha", reply_to=top["id"], kind="not_found")  # not a message carol sent or was sent


# --- caps -------------------------------------------------------------------------------------------------------------


def test_size_cap(plain):
    plain.send("alpha", "bravo", body="x" * msg.BODY_MAX_BYTES)
    plain.refused("alpha", "bravo", body="x" * (msg.BODY_MAX_BYTES + 1), kind="too_large")
    plain.refused("alpha", "bravo", body="é" * (msg.BODY_MAX_BYTES // 2 + 1), kind="too_large")  # bytes, not characters
    assert plain.rows("SELECT COUNT(*) FROM messages") == [(1,)]
    assert {msg._bucket(n) for n in (0, 1024)} == {"S"} and msg._bucket(1025) == msg._bucket(4096) == "M"
    assert msg._bucket(4097) == "L"


def test_rate_cap_per_sender(plain):
    for i in range(msg.RATE_PER_MINUTE):
        plain.send("alpha", "bravo", body=f"m{i}", now=BASE + i)
    err = plain.refused("alpha", "bravo", body="one too many", now=BASE + 59, kind="rate_limited")
    assert str(msg.RATE_PER_MINUTE) in err.message
    plain.send("bravo", "alpha", body="a different sender", now=BASE + 59)  # the cap is per sender
    plain.send("alpha", "bravo", body="the next window", now=BASE + 60)  # one fixed window, then it starts again
    assert plain.rows("SELECT COUNT(*) FROM messages") == [(msg.RATE_PER_MINUTE + 2,)]


def test_hop_limit(plain):
    last, sender, receiver = plain.send("alpha", "bravo", body="0")["id"], "bravo", "alpha"
    for hop in range(1, msg.HOP_MAX + 1):
        got = plain.send(sender, receiver, body=str(hop), reply_to=last)
        assert got["hop"] == hop
        last, sender, receiver = got["id"], receiver, sender
    err = plain.refused(sender, receiver, body="one deeper", reply_to=last, kind="hop_limit")
    assert str(msg.HOP_MAX) in err.message


def test_secret_name_refused(plain):
    (plain.root / "wshome").mkdir(exist_ok=True)
    (plain.root / "wshome" / "secrets.toml").write_text('[GITHUB_TOKEN]\nhosts = ["api.example.com"]\n')
    err = plain.refused("alpha", "bravo", body="please export GITHUB_TOKEN first", kind="secret_refused")
    assert "GITHUB_TOKEN" in err.message
    plain.refused("alpha", "bravo", body="x", thread="GITHUB_TOKEN", kind="secret_refused")
    plain.send("alpha", "bravo", body="GITHUB_TOKEN_2 and my-GITHUB_TOKEN are other words")
    (plain.root / "wshome" / "secrets.toml").write_text("not = = toml")
    plain.refused("alpha", "bravo", body="fine words")  # a registry that will not load refuses the send


SCAN_CASES = {
    "an AWS access key id": lambda: "AKIA" + "Q" * 16,
    "a GitHub token": lambda: "gh" + "p_" + "a1" * 20,
    "an Anthropic key": lambda: "sk-" + "ant-" + "x" * 30,
    "an OpenAI-style key": lambda: "sk-" + "y" * 40,
    "a Slack token": lambda: "xox" + "b-" + "1234567890-abcdefghij",
    "a Google API key": lambda: "AIza" + "S" * 35,
    "a private key block": lambda: "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "a JSON web token": lambda: "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12,
    "a bearer token": lambda: "Authorization: Bearer " + "z" * 30,
    "a password in a web address": lambda: "postgres" + "://user:" + "pw123@dbhost/x",
    "a key or password with its value": lambda: "api_key = " + "k" * 20,
}


@pytest.mark.parametrize("what", sorted(SCAN_CASES))
def test_secret_scan_refuses(plain, what):
    text = f"see this: {SCAN_CASES[what]()} ok"
    err = plain.refused("alpha", "bravo", body=text, kind="secret_refused")
    assert what in err.message and text not in err.message  # it says what it found, never the text
    assert plain.count() == 0


def test_secret_scan_leaves_ordinary_words_and_cannot_be_made_slow(plain):
    plain.send("alpha", "bravo", body="the token budget and the key points; password policy; Bearer of bad news")
    hostile = [
        "a" * 16000,
        "sk-" + "a" * 16000,
        "://" * 5000,
        "key" + " " * 3 + "=" + "b" * 16000,
        "-----BEGIN " + "AB " * 4000,
        "eyJ" + "a" * 8000 + "." + "eyJ" + "b" * 8000,
        "Bearer " + "c" * 15000,
        "AKIA" * 4000,
        "a:" * 8000 + "@",
    ]
    start = time.monotonic()
    for text in hostile:
        msg.scan(text)
    assert time.monotonic() - start < 3  # each rule has bounded repeats and no group repeats another


# --- failures ---------------------------------------------------------------------------------------------------------


def counts(world) -> tuple:
    return tuple(world.rows(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("messages", "recipients", "envelope"))


def test_store_full_is_typed(plain, monkeypatch):
    monkeypatch.setattr(msg, "STORE_MAX_BYTES", 1)  # as small as it goes: 64 pages
    err = None
    for i in range(500):
        try:
            plain.send("alpha", "bravo", body=f"{i} " + "word " * 1600, now=BASE + 60 * i)
        except WsError as e:
            err = e
            break
    assert err is not None and (err.kind, err.details["reason"]) == ("unavailable", "full")
    assert "size cap" in err.hint
    messages, recipients, envelope = counts(plain)
    assert messages == recipients == envelope and messages > 3  # the failed send left nothing behind
    assert len(msg.inbox("bravo", limit=msg.LIST_MAX, now=BASE + 3600)) == messages  # and reading still works


def test_store_locked_is_typed(plain, monkeypatch):
    plain.send("alpha", "bravo")
    monkeypatch.setattr(msg, "BUSY_MS", 100)
    blocker = sqlite3.connect(msg.store_path(), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        err = plain.refused("alpha", "bravo", kind="unavailable")
        assert err.details["reason"] == "locked"
        assert [m["id"] for m in msg.inbox("bravo")] == [1]  # a reader is not blocked
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert plain.send("alpha", "bravo")["id"] == 2
    assert counts(plain) == (2, 2, 2)


def test_store_readonly_is_typed(plain, monkeypatch):
    plain.send("alpha", "bravo")
    monkeypatch.setattr(msg, "_OPEN_MODE", "ro")  # what the wall does to the state folder
    err = plain.refused("alpha", "bravo", kind="unavailable")
    assert err.details["reason"] == "read-only" and "wall" in err.hint
    assert len(msg.inbox("bravo")) == 1  # it can still be read
    assert counts(plain) == (1, 1, 1)


def test_unopenable_store_is_typed(plain):
    (plain.root / "wshome" / "msg").parent.mkdir(exist_ok=True)
    (plain.root / "wshome" / "msg").write_text("a file where the folder should be")
    err = plain.refused("alpha", "bravo", kind="unavailable")
    assert err.details["reason"] == "unopenable"
    assert msg.inbox("bravo") == []  # nothing to read where there is no store


def test_damaged_store_is_typed(plain):
    plain.send("alpha", "bravo")
    for p in msg.store_path().parent.glob("messages.db*"):
        p.unlink()
    msg.store_path().write_bytes(b"this is not a database" * 400)
    err = plain.refused("alpha", "bravo", kind="unavailable")
    assert err.details["reason"] == "damaged"


def test_body_and_envelope_are_one_write(world):
    classed(world)
    world.send("lead1", "work1", body="first")
    world.send("lead1", "wall1", body="walled first")
    base = counts(world)
    shared = sqlite3.connect(msg.store_path())
    shared.execute("CREATE TRIGGER no_envelope BEFORE INSERT ON envelope BEGIN SELECT RAISE(ABORT, 'x'); END")
    shared.commit()
    shared.close()
    for to in ("work1", "wall1"):  # the envelope fails after the message (and the walled body) went in
        world.refused("lead1", to, body="second", kind="unavailable")
    assert counts(world) == base
    assert world.rows("SELECT COUNT(*) FROM bodies", class_db(world)) == [(1,)]  # no body without its envelope
    shared = sqlite3.connect(msg.store_path())
    shared.execute("DROP TRIGGER no_envelope")
    shared.commit()
    shared.close()
    walled = sqlite3.connect(class_db(world))
    walled.execute("CREATE TRIGGER no_body BEFORE INSERT ON bodies BEGIN SELECT RAISE(ABORT, 'x'); END")
    walled.commit()
    walled.close()
    world.refused("lead1", "wall1", body="third", kind="unavailable")  # the body fails: no envelope either
    assert counts(world) == base


# --- retention and deletion -------------------------------------------------------------------------------------------


def test_retention_per_class(world):
    classed(world)
    world.send("lead1", "work1", body="open class: 10 days", now=BASE)
    world.send("lead1", "wall1", body="secret class: 5 days", now=BASE)
    day = 86400
    assert msg.prune(now=BASE + 3 * day, force=True) == {"ran": True, "expired": 0, "gone": 0}
    assert msg.prune(now=BASE + 6 * day, force=True)["expired"] == 1  # the walled one, past its 5 days
    assert world.rows("SELECT class FROM messages") == [("open",)]
    assert world.rows("SELECT COUNT(*) FROM bodies", class_db(world)) == [(0,)]  # its body went too
    assert counts(world) == (1, 1, 1)
    assert msg.prune(now=BASE + 11 * day, force=True)["expired"] == 1
    assert counts(world) == (0, 0, 0)


def test_prune_runs_at_most_hourly(world):
    classed(world)
    world.send("lead1", "work1", body="x", now=BASE)  # a send prunes first, so the clock starts here
    assert msg.prune(now=BASE + 600)["ran"] is False
    assert msg.prune(now=BASE + 3599)["ran"] is False
    assert msg.prune(now=BASE + 3600)["ran"] is True
    assert msg.prune(now=BASE + 3601)["ran"] is False
    world.policy("this is not toml = =")  # without a policy retention can't be known: nothing is pruned
    with pytest.raises(WsError):
        msg.prune(now=BASE + 99999, force=True)
    assert counts(world) == (1, 1, 1)


def test_housekeeping_rides_on_the_next_send_after_an_hour(world):
    classed(world)
    world.send("lead1", "work1", body="old", now=BASE)
    world.send("lead1", "work1", body="late", now=BASE + 11 * 86400)
    assert world.rows("SELECT body FROM messages") == [("late",)]


def test_deleted_walled_repo_loses_its_rows(world):
    classed(world)
    world.send("lead1", "wall1", body=planted())
    world.send("lead1", "wall1", "stop-and-hand-over", None)
    world.send("wall1", "lead1", "done", None)
    world.send("lead1", "work1", body="stays")
    assert counts(world) == (4, 4, 4)
    shutil.rmtree(class_db(world).parent)  # the store gone but the repo still there: not "gone", only its body
    assert msg.prune(now=BASE, force=True)["gone"] == 0
    assert msg.read(1, "wall1")["body_state"] == "gone"
    shutil.rmtree(world.root / "walled")  # the repo deleted
    got = msg.prune(now=BASE, force=True)
    assert got["gone"] == 2  # the two messages kept in its store; the done sent from it went to lead1's full class
    assert world.rows("SELECT body FROM messages ORDER BY id") == [(None,), ("stays",)]
    assert counts(world) == (2, 2, 2) and [e["class"] for e in msg.envelopes()] == ["open", "open"]


def test_a_repo_that_cannot_be_seen_is_not_a_repo_that_is_gone(world, monkeypatch):
    classed(world)
    world.send("lead1", "wall1", body=planted())

    def denied(path):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(msg, "os", types.SimpleNamespace(stat=denied))
    assert msg._repo_gone(str(world.root / "walled")) is False


# --- reading ----------------------------------------------------------------------------------------------------------


def test_read_only_by_recipient(plain):
    mid = plain.send("alpha", "bravo", body="for bravo only")["id"]
    for who in ("alpha", "carol"):  # not even the sender, and not a third session
        with pytest.raises(WsError) as e:
            msg.read(mid, who)
        assert e.value.kind == "not_found"
    with pytest.raises(WsError) as e:
        msg.read(9999, "bravo")
    assert e.value.kind == "not_found"  # the same answer: ids can't be probed
    got = msg.read(mid, "bravo")
    assert (got["body"], got["body_state"], got["from"]) == ("for bravo only", "present", "alpha")
    assert got["read_ts"] and msg.inbox("bravo", unread_only=True) == []
    assert [m["body"] for m in msg.inbox("bravo", bodies=True)] == ["for bravo only"]
    assert "body" not in msg.inbox("bravo")[0] and "body" not in msg.sent("alpha")[0]
    assert msg.ack(mid, "bravo")["ack_ts"] and msg.ack(mid, "bravo")["ack_ts"] == msg.ack(mid, "bravo")["ack_ts"]
    with pytest.raises(WsError):
        msg.ack(mid, "alpha")


def test_walled_body_only_to_a_caller_in_that_class(world):
    classed(world)
    world.session("wall2", world.folder("walled/other"))  # the same class, another session
    sent = world.send("lead1", "wall1", body=planted())
    with pytest.raises(WsError):
        msg.read(sent["id"], "wall2")  # addressed to wall1
    assert msg.read(sent["id"], "wall1")["body_state"] == "present"
    # wall1 moves to a folder in another class: the same id no longer gets it the body
    world.session("wall1", world.folder("open/moved"))
    assert msg.read(sent["id"], "wall1")["body_state"] == "withheld"
    assert msg.inbox("wall1", bodies=True)[0]["body"] is None


def test_mark_delivered_and_the_envelope_feed(plain):
    ids = [plain.send("alpha", "bravo", body=str(i))["id"] for i in range(3)]
    assert msg.mark_delivered(ids[:2], "bravo", now=BASE) == 2
    assert msg.mark_delivered(ids, "bravo", now=BASE + 5) == 1  # a delivery is stamped once
    feed = msg.envelopes()
    assert [e["delivered_ts"] is not None for e in feed] == [True, True, True]
    assert [e["seq"] for e in msg.envelopes(after=feed[0]["seq"])] == [e["seq"] for e in feed[1:]]
    assert len(msg.envelopes(limit=2)) == 2


def test_lists_are_bounded(plain):
    for bad in (0, -1, msg.LIST_MAX + 1, True, "5"):
        with pytest.raises(WsError) as e:
            msg.inbox("bravo", limit=bad)
        assert e.value.kind == "bad_input"
    for i in range(5):
        plain.send("alpha", "bravo", body=str(i))
    assert [m["id"] for m in msg.inbox("bravo", limit=2)] == [1, 2]
    assert [m["id"] for m in msg.inbox("bravo", after=4)] == [5]
    out = io.StringIO()
    assert msg.export(out, limit=3) == 3
    with pytest.raises(WsError):
        msg.export(out, limit=msg.EXPORT_MAX + 1)


# --- the classless rules, and a policy that will not load --------------------------------------------------------------


def test_no_classes_policy_uses_the_classless_rules(plain):
    assert session.read("alpha").class_ is None  # None is "not known", and must not be taken for walled
    got = plain.send("alpha", "bravo", "note", "plain words")
    assert got["class"] is None and plain.rows("SELECT body, sha256, walled, class FROM messages") == [
        ("plain words", hashlib.sha256(b"plain words").hexdigest(), 0, None)
    ]
    assert plain.send("bravo", "alpha", "done", None)["id"]  # any session to any, any kind
    plain.policy("[budget.purposes]\nx = 1\n")  # a policy file that names no classes
    assert plain.send("carol", "alpha", "ask", "a question")["id"] and msg.read(3, "alpha")["body"] == "a question"
    assert not (plain.root / "alpha" / ".ws").exists()  # nothing was made in any repo


def test_unloadable_policy_refuses(plain):
    plain.send("alpha", "bravo")
    plain.policy("this is not toml = = =")
    err = plain.refused("alpha", "bravo")
    assert "policy" in err.message
    with pytest.raises(WsError) as e:
        msg.read(1, "bravo")
    assert e.value.kind == "refused"
    with pytest.raises(WsError):
        msg.inbox("bravo", bodies=True)
    assert counts(plain) == (1, 1, 1)


def test_session_registry_that_will_not_read_is_no_caller(plain, monkeypatch):
    for f in (plain.root / "wshome" / "sessions").glob("*.json"):
        f.write_text("{broken")
    err = plain.refused("alpha", "bravo")
    assert "which session" in err.message


# --- the commands -----------------------------------------------------------------------------------------------------


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_json_envelope_for_every_msg_command(plain, capsys, tmp_path, monkeypatch):
    plain.who = None
    code, out, _ = run(capsys, "msg", "send", "bravo", "hi", "--json")  # no session: a typed error, still one envelope
    env = json.loads(out)
    assert (code, env["kind"], env["status"], env["error_kind"]) == (1, "msg.send", "error", "refused")
    assert env["hint"] and env["schema"] == "ws.cli/1"

    plain.who = "alpha"
    code, out, _ = run(capsys, "msg", "send", "bravo", "hello bravo", "--ack-required", "--key", "k1", "--json")
    env = json.loads(out)
    assert (code, env["kind"], env["status"]) == (0, "msg.send", "ok")
    assert env["data"]["id"] == 1 and env["data"]["replayed"] is False and env["data"]["size"] == "S"
    assert json.loads(run(capsys, "msg", "send", "bravo", "x", "--key", "k1", "--json")[1])["data"]["replayed"]

    code, out, _ = run(capsys, "msg", "list", "--sent", "--json")
    env = json.loads(out)
    assert (code, env["kind"]) == (0, "msg.list") and env["data"]["messages"][0]["ack_required"] is True
    assert "body" not in env["data"]["messages"][0]

    plain.who = "bravo"
    code, out, _ = run(capsys, "msg", "list", "--unread", "--json")
    assert json.loads(out)["data"]["messages"][0]["to"] == "bravo"
    code, out, _ = run(capsys, "msg", "read", "1", "--ack", "--json")
    env = json.loads(out)
    assert (code, env["kind"], env["data"]["body"]) == (0, "msg.read", "hello bravo") and env["data"]["ack_ts"]
    code, out, _ = run(capsys, "msg", "read", "77", "--json")
    assert json.loads(out)["error_kind"] == "not_found" and code == 1

    path = tmp_path / "out dir" / "export.jsonl"
    path.parent.mkdir()
    code, out, _ = run(capsys, "msg", "export", "--out", str(path), "--json")
    env = json.loads(out)
    assert (code, env["kind"], env["data"]) == (0, "msg.export", {"written": 1, "path": str(path)})
    assert json.loads(path.read_text())["schema"] == "ws.message/1" and oct(path.stat().st_mode & 0o777) == "0o600"
    plain.who = None  # no session (a person's shell, or an orphaned process): no export, whoever asks
    code, out, err = run(
        capsys, "msg", "export", "--json"
    )  # to standard output it is raw lines: the error is on stderr
    assert (code, out) == (1, "") and "can't tell which session" in err
    second = path.parent / "second.jsonl"
    code, out, _ = run(capsys, "msg", "export", "--out", str(second), "--json")
    assert (code, json.loads(out)["error_kind"]) == (1, "refused") and not second.exists()
    plain.who = "alpha"
    code, out, _ = run(capsys, "msg", "export", "--json")  # a session: its own messages as JSON lines, no envelope
    assert code == 0 and [json.loads(line)["schema"] for line in out.splitlines()] == ["ws.message/1"]
    code, out, _ = run(capsys, "msg", "export", "--out", str(path.parent / "no" / "such" / "x"), "--json")
    assert json.loads(out)["error_kind"] == "io"


def test_text_output_and_stdin(plain, capsys, monkeypatch):
    plain.who = "alpha"
    monkeypatch.setattr(sys, "stdin", io.StringIO("from standard input"))
    code, out, _ = run(capsys, "msg", "send", "bravo", "-")
    assert (code, out) == (0, "sent message 1 to bravo\n")
    plain.who = "bravo"
    code, out, _ = run(capsys, "msg", "list")
    assert code == 0 and out.split()[0] == "1" and "alpha -> bravo" in out and out.rstrip().endswith("new")
    escape = "\x1b[31m red \x1b[0m"
    plain.send("alpha", "bravo", body=escape)
    plain.who = "bravo"
    code, out, _ = run(capsys, "msg", "read", "2")
    assert "\x1b" not in out and "untrusted" in out  # text from another session never drives the terminal
    code, out, err = run(capsys, "msg", "list", "--after", "99")
    assert (code, out, err) == (0, "", "ws: no messages\n")


def test_shared_store_is_private(plain):
    plain.send("alpha", "bravo")
    folder = msg.store_path().parent
    assert (folder.stat().st_mode & 0o777, msg.store_path().stat().st_mode & 0o777) == (0o700, 0o600)
    assert msg.store_path() == paths.state_dir() / "msg" / "messages.db"
    assert sqlite3.connect(msg.store_path()).execute("PRAGMA journal_mode").fetchone() == ("wal",)
