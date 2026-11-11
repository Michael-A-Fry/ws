"""What leaves a walled class through ws's own shared stores, and the named cases of `ws walls check` that pin each flow.

Fixtures are the generic classes alpha and beta, built at run time under tmp_path with HOME and WS_HOME below it. The tracer
values are random and made when the test runs. Nothing reads the real HOME, a clock's zone or the network, and nothing runs
sudo or makes an account. The shared-store tests use the real `ws msg`, `ws memory`, `ws note` and `ws run` code on stand-in
sessions (the `World` of test_msg); the mutation tests use the `Host` of test_walls, a set-up host stood in for."""

from __future__ import annotations

import io
import json
import os
import secrets
import stat
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from test_msg import World, world  # noqa: F401  (the fixture is used by name)
from test_run import ws as run_ws  # noqa: F401  (a scratch WS_HOME with a fake claude, for the run record)
from test_walls import FLOW_NAMES, NAMES, box, host, kind, policy_file  # noqa: F401

from ws import cli, gate, mcp_server, memory, msg, notes, paths, policy, sandbox, walls, wallsettings
from ws.output import WsError


def tracer() -> str:
    """A value that is nowhere until a test puts it somewhere."""
    return "tracer" + secrets.token_hex(8)


def alpha_db(world: World) -> Path:  # noqa: F811
    """alpha's class store: under alpha's own repo, not under the shared state folder."""
    return world.root / "alpha" / ".ws" / "messages" / "alpha.db"


def alpha_world(world: World, allow=("sender1",)) -> World:  # noqa: F811
    """alpha and beta (walled), open (full), a sender in open on alpha's allowlist, and a session in each repo."""
    for name in ("alpha", "beta"):
        world.folder(f"{name}/src", git=False)
        (world.root / name / ".git").mkdir(exist_ok=True)
    world.folder("open/src")
    world.policy(
        f"""
        [classes.alpha]
        access = "walled"
        repos = ["{world.root}/alpha/**"]
        inbound = "allowlist"
        inbound_allow = {json.dumps(list(allow))}
        retention_days = 5
        [classes.beta]
        access = "walled"
        repos = ["{world.root}/beta/**"]
        [classes.open]
        access = "full"
        repos = ["{world.root}/open/**"]
        [gate]
        deny_commands = ["ws-tracer-probe*"]
        """
    )
    world.session("sender1", world.folder("open/src/one"))
    world.session("alpha1", world.folder("alpha/src/one"))
    world.session("beta1", world.folder("beta/src/one"))
    return world


# ---- the shared store is the account's, and the class store is the class's own ---------------------------------------------


def test_the_shared_store_is_private_to_the_account_and_the_class_store_is_the_class_own(world):  # noqa: F811
    alpha_world(world)
    body = tracer()
    sent = world.send("sender1", "alpha1", "note", body)
    shared = msg.store_path()
    assert stat.S_IMODE(shared.parent.stat().st_mode) == 0o700 and stat.S_IMODE(shared.stat().st_mode) == 0o600
    assert shared.parent.stat().st_uid == os.getuid() and shared.stat().st_uid == os.getuid()
    store = alpha_db(world)
    repo = (world.root / "alpha").resolve()
    assert store.resolve().is_relative_to(repo) and store.is_file()  # the class's own repo, not the shared folder
    world.who = "alpha1"  # a session of the class opens its own store and reads the body there
    got = msg.read(sent["id"], "alpha1")
    assert got["body"] == body and got["body_state"] == "present"
    assert not any(body.encode() in p.read_bytes() for p in shared.parent.glob("messages.db*"))


# ---- the tracer ------------------------------------------------------------------------------------------------------------


def test_a_tracer_planted_in_the_class_reaches_no_shared_sink(world, run_ws, capsys, monkeypatch):  # noqa: F811
    alpha_world(world)
    value = tracer()
    repo_file = world.root / "alpha" / "src" / "one" / "finding.md"
    repo_file.write_text(f"# Finding\n{value}\n", encoding="utf-8")
    (world.root / "alpha" / "AGENTS.md").write_text(f"Rules for alpha. {value}\n", encoding="utf-8")
    world.send("sender1", "alpha1", "note", f"see {value}")
    # the plant is in the class first (a check that found nothing because nothing was written would prove nothing)
    assert world.rows("SELECT body FROM bodies", alpha_db(world)) == [(f"see {value}",)]
    assert value in repo_file.read_text(encoding="utf-8")
    # a sink the planted value could reach if the code carried it: a message to a session that is not walled
    control = tracer()
    world.session("lead1", world.folder("open/lead"), role="lead")
    world.session("open1", world.folder("open/two"))
    world.send("lead1", "open1", "note", control)
    assert control.encode() in b"".join(p.read_bytes() for p in msg.store_path().parent.glob("messages.db*"))

    # the gate's verdict log: a denied call from alpha's session, with the value in the command
    world.who = "alpha1"
    argv = ["--runner", "claude-code", *paths.pin_of().args()]
    call = {
        "tool_name": "Bash",
        "tool_input": {"command": f"ws-tracer-probe {value}"},
        "cwd": str(world.root / "alpha" / "src" / "one"),
        "session_id": "alpha1",
    }
    assert gate.run_hook("check", argv, json.dumps(call))[0] == gate.DENY
    # a run record, from a headless job run in the class's repo
    run_ws.job(prompt="ok")
    monkeypatch.chdir(world.root / "alpha" / "src" / "one")
    cli.main(["run", "daily"])
    capsys.readouterr()
    # a session's status output, text and JSON, from the class's repo
    outs = []
    for extra in ([], ["--json"]):
        assert cli.main(["status", *extra]) == 0
        outs.append(capsys.readouterr().out)
    assert "AGENTS.md" in outs[0]  # status saw the file that holds the value
    # the message feed a session reads
    feed = json.dumps(msg.watch("alpha1")) + json.dumps(msg.inbox("alpha1")) + json.dumps(msg.envelopes())

    state = paths.state_dir()
    sinks = {
        "the shared messages.db (and its journal)": b"".join(
            p.read_bytes() for p in msg.store_path().parent.glob("*") if p.is_file()
        ),
        "a run record": (state / "logs" / "runs.jsonl").read_bytes(),
        "the gate verdict log": (state / "logs" / gate.VERDICT_LOG).read_bytes(),
        "a session's status output": "".join(outs).encode(),
        "the message feed": feed.encode(),
    }
    assert all(sinks.values()), [name for name, data in sinks.items() if not data]  # every sink was written
    for name, data in sinks.items():
        assert value.encode() not in data, name
    # the two sinks that take text from a caller refuse a session of the class
    vault = world.root / "vault"
    vault.mkdir()
    for fn in (lambda: memory.add(value, "T"), lambda: notes.capture(value, vault)):
        with pytest.raises(WsError) as e:
            fn()
        assert e.value.kind == "refused"
    assert not list(vault.iterdir())


# ---- memory and notes ------------------------------------------------------------------------------------------------------


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """A WS_HOME with a notes folder and a memory repo, a policy with alpha and beta, and one folder in no class."""
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    from ws.profile import dumps, profile_path

    for name in ("vault", "mem", "work/alpha", "work/beta", "elsewhere"):
        (tmp_path / name).mkdir(parents=True)
    profile_path().parent.mkdir(parents=True)
    profile_path().write_text(
        dumps({"roles": {"notes": {"path": str(tmp_path / "vault")}, "memory": {"path": str(tmp_path / "mem")}}})
    )
    memory.init(tmp_path / "mem")
    policy_file().write_text(
        f'[classes.alpha]\naccess = "walled"\nrepos = ["{tmp_path}/work/alpha/**"]\n'
        f'[classes.beta]\naccess = "walled"\nrepos = ["{tmp_path}/work/beta/**"]\n',
        encoding="utf-8",
    )
    return tmp_path


def test_memory_and_notes_refuse_walled_content_to_another_class(stores, monkeypatch):
    vault, mem = stores / "vault", stores / "mem"
    value = tracer()
    (stores / "work" / "alpha" / "plan.md").write_text(f"# Plan\n{value}\n", encoding="utf-8")
    (vault / "copied.md").write_text(f"---\nclass: alpha\n---\n{value} copied out of alpha\n", encoding="utf-8")
    (vault / "mine.md").write_text(f"{value} nobody's class\n", encoding="utf-8")
    roots = [("memory", mem), ("notes", vault), ("notes", stores / "work" / "alpha")]
    monkeypatch.setattr(memory, "configured_roots", lambda env=None: roots)

    def names() -> set[str]:
        return {Path(h.path).name for h in memory.search(value)}

    monkeypatch.chdir(stores / "work" / "alpha")
    assert names() == {"plan.md", "copied.md", "mine.md"}  # the class sees its own, and the search is not blind
    for refused in (
        lambda: memory.add(f"alpha: {value}", "T"),
        lambda: notes.capture(f"alpha: {value}", vault),
        lambda: mcp_server.call_add({"text": value}),  # the MCP tools call these functions, so they inherit the refusal
        lambda: mcp_server.call_note({"text": value}),
    ):
        with pytest.raises(WsError) as e:
            refused()
        assert e.value.kind == "refused" and e.value.hint
    assert not (vault / "_inbox").exists() and not list(mem.glob("*/*.md"))
    monkeypatch.chdir(stores / "work" / "beta")
    assert names() == {"mine.md"}  # beta sees neither a file in alpha's repos nor one marked as alpha's
    found = mcp_server.call_search({"query": value})[1]["results"]
    assert {Path(r["path"]).name for r in found} == {"mine.md"}  # the MCP search is the same search
    policy_file().write_text(  # alpha lets beta read it
        f'[classes.alpha]\naccess = "walled"\nreadable_by = ["beta"]\nrepos = ["{stores}/work/alpha/**"]\n'
        f'[classes.beta]\naccess = "walled"\nrepos = ["{stores}/work/beta/**"]\n',
        encoding="utf-8",
    )
    assert names() == {"plan.md", "copied.md", "mine.md"}
    policy_file().write_text('[classes.alpha]\naccess = "nonsense"\n', encoding="utf-8")  # it will not load: refused
    monkeypatch.chdir(stores / "elsewhere")
    for refused in (lambda: memory.add("x", "T"), lambda: notes.capture("x", vault), lambda: names()):
        with pytest.raises(WsError) as e:
            refused()
        assert e.value.kind == "refused"


def test_with_no_classes_in_the_policy_nothing_changes(stores, monkeypatch):
    policy_file().write_text("", encoding="utf-8")
    monkeypatch.chdir(stores / "work" / "alpha")
    assert memory.add("a fact", "Fact").exists() and notes.capture("a note", stores / "vault").exists()
    assert [Path(h.path).name for h in memory.search("fact")] == ["fact.md"]


# ---- the card --------------------------------------------------------------------------------------------------------------


def test_a_walled_card_has_no_count_field(world, capsys):  # noqa: F811
    """The card is the body-free envelope row `ws msg watch` and the viewer show for a walled message."""
    alpha_world(world)
    world.send("sender1", "alpha1", "note", tracer())
    world.send("sender1", "alpha1", "ask", tracer())
    world.who = "alpha1"
    events = msg.watch("alpha1")["events"]
    assert len(events) == 2
    for card in [*events, *msg.envelopes()]:
        assert tuple(card) == msg.ENVELOPE_KEYS  # these keys and no others
        assert not [k for k in card if "count" in k.lower()]
        assert {k for k, v in card.items() if isinstance(v, int | float)} <= {"seq", "message_id"}
        assert card["size"] in ("S", "M", "L") and card["class"] == "alpha"
    assert cli.main(["msg", "watch", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [tuple(e) for e in printed["data"]["events"]] == [msg.ENVELOPE_KEYS] * 2
    assert "count" not in json.dumps(printed["data"]["events"]).lower()
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main(["msg", "watch"]) == 0
    assert "count" not in buf.getvalue().lower()
    assert [r[1] for r in world.rows("PRAGMA table_info(envelope)")] == list(
        msg.ENVELOPE_KEYS
    )  # the table gained nothing


# ---- a push to a remote outside the class ---------------------------------------------------------------------------------------


def test_a_push_to_a_host_outside_the_class_is_refused(host):  # noqa: F811
    host.edit_policy(
        'allowed_providers = ["anthropic"]\nowner = "alpha lead"',
        'allowed_providers = ["anthropic"]\nallowed_hosts = ["git.example.org"]\nowner = "alpha lead"',
    )
    host.place("alpha")
    pol = policy.load(policy_file())
    cls = pol.classes["alpha"]
    doc = json.loads(wallsettings.launch_file_path("alpha", kind()).read_text())
    net = doc["sandbox"]["network"]
    assert net["strictAllowlist"] is True and net["allowedDomains"] == sandbox.class_domains(cls)
    assert "git.example.org" in net["allowedDomains"]
    plan = sandbox.egress_plan(pol, cls, "")
    assert plan.decide("git.example.org", 443).allow  # a remote the class lists
    assert not plan.decide(walls.PROBE_REMOTE, 443).allow  # one it does not
    assert not any(sandbox.host_matches(walls.PROBE_REMOTE, 443, e) for e in net["allowedDomains"])
    assert "refused" in sandbox.explain_host(pol, cls, walls.PROBE_REMOTE)
    got = {c["name"]: c for c in walls.run_cases(walls._context(dict(os.environ), walls._now())[0])}
    assert got["class-push-refused"]["status"] == "pass", got["class-push-refused"]


# ---- the named cases ---------------------------------------------------------------------------------------------------------


def test_walls_check_names_at_least_13_cases(host, capsys):  # noqa: F811
    assert cli.main(["walls", "check", "--json"]) == 0
    names = [c["name"] for c in json.loads(capsys.readouterr().out)["data"]["cases"]]
    assert len(names) >= 13 and len(set(names)) == len(names)
    wanted = {
        "inbound-refused",
        "tracer-stays-in-class",
        "class-push-refused",
        "bodies-in-class-store",
        "walled-sends-fixed-kinds",
        "provider-allowlist",
        "memory-notes-refused",
        "walled-card-no-counts",
        "per-repo-settings",
        "otel-off",
        "owner-and-review-date",
        "messaging-denied",
        "allowed-sender-delivered",
    }
    assert wanted <= set(names) and names == NAMES


def test_a_case_that_needs_no_class_file_runs_without_a_class_user(box):  # noqa: F811
    policy_file().write_text("", encoding="utf-8")
    ctx, _, _ = walls._context(dict(os.environ), walls._now())
    got = {c["name"]: c["status"] for c in walls.run_cases(ctx)}
    for case in walls.CASES:
        assert got[case.name] == ("skip" if case.host else "pass"), (case.name, got[case.name])
    assert {c.name for c in walls.CASES if not c.host} >= {"tracer-stays-in-class", "allowed-sender-delivered"}


def test_each_new_row_names_its_setting_and_the_class_file_it_reads_if_any():
    new = [c for c in walls.CASES if c.name in FLOW_NAMES]
    assert [c.name for c in new] == FLOW_NAMES
    assert all(c.setting.strip() for c in new)
    assert {c.name for c in new if c.host} == {
        "provider-allowlist",
        "class-push-refused",
    }  # the rest need no class file


def append_domain(host_name: str, domain: str):
    return lambda h: h.edit_file(host_name, lambda d: d["sandbox"]["network"]["allowedDomains"].append(domain))


def leak_into_envelope(h) -> None:
    real = msg._store_message

    def leaky(conn, parties, content, labels, clock):
        out = real(conn, parties, content, labels, clock)
        conn.execute("UPDATE envelope SET kind=kind || ? WHERE message_id=?", (content[2], out["id"]))
        return out

    h.mp.setattr(msg, "_store_message", leaky)


def open_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)


def with_count(h) -> None:
    real = msg._envelope_rows
    h.mp.setattr(msg, "_envelope_rows", lambda *a: [{**r, "count": 3} for r in real(*a)])


MUTATIONS = [
    (
        "allowed-sender-delivered",
        "admit lets every sender in",
        lambda h: h.mp.setattr(msg, "admit", lambda *a, **k: None),
    ),
    (
        "allowed-sender-delivered",
        "a refusal leaves no log row",
        lambda h: h.mp.setattr(msg, "_record_refusal", lambda *a, **k: None),
    ),
    ("allowed-sender-delivered", "no class is walled", lambda h: h.mp.setattr(msg, "_walled", lambda cls: False)),
    (
        "bodies-in-class-store",
        "the body is kept in the shared store",
        lambda h: h.mp.setattr(msg, "_walled", lambda cls: False),
    ),
    (
        "bodies-in-class-store",
        "the shared folder is open to others",
        lambda h: h.mp.setattr(msg, "_private_folder", open_folder),
    ),
    (
        "walled-sends-fixed-kinds",
        "a free-text kind is a fixed kind",
        lambda h: h.mp.setattr(msg, "FIXED_KINDS", (*msg.FIXED_KINDS, "note")),
    ),
    (
        "walled-sends-fixed-kinds",
        "a walled sender is not held to them",
        lambda h: h.mp.setattr(msg, "_walled", lambda c: False),
    ),
    (
        "provider-allowlist",
        "the launch file lists a provider the class does not name",
        append_domain("alpha", "api.openai.com"),
    ),
    (
        "provider-allowlist",
        "the class's list now holds more than the launch file",
        lambda h: h.mp.setattr(
            sandbox, "class_domains", lambda cls: [*sandbox.PROVIDER_HOSTS["anthropic"], "extra.example.org"]
        ),
    ),
    (
        "class-push-refused",
        "the launch file stops refusing a host that is not listed",
        lambda h: h.edit_file("alpha", lambda d: d["sandbox"]["network"].pop("strictAllowlist")),
    ),
    ("class-push-refused", "the push host is on the list", append_domain("beta", walls.PROBE_REMOTE)),
    (
        "class-push-refused",
        "every host matches",
        lambda h: h.mp.setattr(sandbox, "host_matches", lambda name, port, entry: True),
    ),
    ("memory-notes-refused", "no class is walled", lambda h: h.mp.setattr(notes, "is_walled", lambda cls: False)),
    (
        "memory-notes-refused",
        "a search shows every file",
        lambda h: h.mp.setattr(memory, "readable_by_caller", lambda *a, **k: None),
    ),
    (
        "memory-notes-refused",
        "the policy is never read",
        lambda h: h.mp.setattr(notes, "load_policy", lambda env=None: policy.default_policy()),
    ),
    ("walled-card-no-counts", "the card has a count", with_count),
    (
        "walled-card-no-counts",
        "the envelope gains a key",
        lambda h: h.mp.setattr(msg, "ENVELOPE_KEYS", (*msg.ENVELOPE_KEYS, "count")),
    ),
    ("tracer-stays-in-class", "the value reaches the envelope", leak_into_envelope),
    (
        "tracer-stays-in-class",
        "a walled caller may add and capture",
        lambda h: h.mp.setattr(notes, "is_walled", lambda cls: False),
    ),
]


def one(name: str) -> dict:
    ctx, _, _ = walls._context(dict(os.environ), walls._now())
    (got,) = walls.run_cases(ctx, [c for c in walls.CASES if c.name == name])
    return got


@pytest.mark.parametrize(("case", "label", "mutate"), MUTATIONS, ids=[f"{c}: {label}" for c, label, _ in MUTATIONS])
def test_each_new_case_fails_when_its_setting_is_removed(host, case, label, mutate):  # noqa: F811
    assert one(case)["status"] == "pass"  # the same host, before
    mutate(host)
    after = one(case)
    assert after["status"] == "fail" and after["detail"], (label, after)
    assert not after["detail"].startswith("the case could not run"), (label, after)


def test_every_new_row_has_a_mutation_test():
    assert {case for case, _, _ in MUTATIONS} == set(FLOW_NAMES)
