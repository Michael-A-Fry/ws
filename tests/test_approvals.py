"""Bound, single-use approvals (ws.approvals) and how the gate uses them: bound to the exact call, used once, expire in
10 minutes, refuse an edited record, and only a person at a terminal can grant."""

from __future__ import annotations

import io
import json
import os
import stat

import pytest

from ws import approvals, cli, gate, policy
from ws.approvals import ApprovalError, Bound

NOW = 1_000_000.0
CALL = Bound("claude-code", "command", "terraform apply", "/work", "sess-1")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "approvals"


def ask(root, bound=CALL, now=NOW):
    return approvals.require(root, bound, now)


def ident(bound=CALL) -> str:
    return bound.digest()


def approve(root, bound=CALL, now=NOW):
    assert ask(root, bound, now), "the first try must be refused"
    return approvals.grant(root, ident(bound)[:12], now + 5)


# --- bound -----------------------------------------------------------------------------------------------------


def test_the_first_try_is_refused_with_an_id_and_a_pending_record(root):
    why = ask(root)
    assert why and f"ws approve {ident()[:12]}" in why and "10 minutes" in why
    rec = json.loads((root / "pending" / f"{ident()}.json").read_text())
    assert rec["bound"] == CALL.__dict__ and rec["id"] == ident()


def test_a_refused_call_stays_refused_until_a_person_grants_it(root):
    ask(root)
    assert ask(root, now=NOW + 30)  # asking again does not approve itself


def test_the_grant_covers_only_the_identical_call(root):
    approve(root)
    for other in (
        Bound("opencode", "command", "terraform apply", "/work", "sess-1"),  # another runner
        Bound("claude-code", "command", "terraform apply ", "/work", "sess-1"),  # one byte more
        Bound("claude-code", "command", "terraform apply", "/work/sub", "sess-1"),  # another folder
        Bound("claude-code", "command", "terraform apply", "/work", "sess-2"),  # another session
        Bound("claude-code", "read", "terraform apply", "/work", "sess-1"),  # another kind
    ):
        assert ask(root, other, NOW + 10), other
    assert ask(root, CALL, NOW + 20) is None  # and the real one is still waiting


def test_the_id_is_a_sha256_of_the_four_fields_and_kind(root):
    assert len(ident()) == 64 and ident() != ident(Bound("claude-code", "command", "terraform apply", "/work", "x"))


# --- single use ------------------------------------------------------------------------------------------------


def test_an_approval_is_used_once(root):
    approve(root)
    assert ask(root, now=NOW + 10) is None
    again = ask(root, now=NOW + 11)
    assert again and "ws approve" in again


def test_two_racing_calls_cannot_both_use_one_approval(root, monkeypatch):
    approve(root)
    real = os.rename
    seen = []

    def rename_then_race(src, dst):
        real(src, dst)
        seen.append(approvals.require(root, CALL, NOW + 10))  # the other call arrives just after the claim

    monkeypatch.setattr(os, "rename", rename_then_race)
    assert approvals.require(root, CALL, NOW + 10) is None
    assert seen and seen[0] is not None


# --- expiry ----------------------------------------------------------------------------------------------------


def test_a_pending_request_expires_after_10_minutes(root):
    ask(root)
    with pytest.raises(ApprovalError, match="no pending|expired"):
        approvals.grant(root, ident()[:12], NOW + approvals.TTL + 1)


def test_a_granted_approval_expires_10_minutes_after_the_grant(root):
    approve(root)  # granted at NOW + 5
    why = ask(root, now=NOW + 5 + approvals.TTL + 1)
    assert why and "expired" in why
    assert ask(root, now=NOW + 5 + approvals.TTL + 2)  # and it is gone, not kept for later


def test_the_last_second_still_counts(root):
    approve(root)
    assert ask(root, now=NOW + 5 + approvals.TTL) is None


def test_expired_files_are_swept(root):
    ask(root)
    other = Bound("claude-code", "command", "other", "/work", "sess-1")
    ask(root, other, NOW + approvals.TTL + 60)
    assert [p.name for p in (root / "pending").glob("*.json")] == [f"{ident(other)}.json"]


# --- tamper ----------------------------------------------------------------------------------------------------


def edit(path, fn):
    rec = json.loads(path.read_text())
    fn(rec)
    path.write_text(json.dumps(rec))


def granted(root):
    return root / "granted" / f"{ident()}.json"


def test_a_granted_record_with_a_changed_command_is_refused_and_removed(root):
    approve(root)
    edit(granted(root), lambda r: r["bound"].update(value="rm -rf ~"))
    why = ask(root, now=NOW + 10)
    assert why and "changed" in why
    assert not granted(root).exists()


def test_a_granted_record_with_another_id_is_refused(root):
    approve(root)
    edit(granted(root), lambda r: r.update(id="0" * 64))
    assert "changed" in ask(root, now=NOW + 10)


@pytest.mark.parametrize(
    "fn",
    [
        lambda r: r.update(granted=NOW + 99999),  # stretched into the future
        lambda r: r.update(created=NOW + 50, granted=NOW),  # granted before it was asked
        lambda r: r.update(granted="soon"),
        lambda r: r.pop("bound"),
    ],
)
def test_a_granted_record_with_impossible_fields_is_refused(root, fn):
    approve(root)
    edit(granted(root), fn)
    assert ask(root, now=NOW + 10)
    assert not granted(root).exists()


def test_a_pending_record_that_was_edited_cannot_be_granted(root):
    ask(root)
    edit(root / "pending" / f"{ident()}.json", lambda r: r["bound"].update(value="something else"))
    with pytest.raises(ApprovalError, match="changed"):
        approvals.grant(root, ident()[:12], NOW + 1)
    assert not (root / "granted").exists() or not list((root / "granted").glob("*.json"))


def test_a_pending_record_is_not_a_grant(root):
    ask(root)
    (root / "granted").mkdir()
    os.rename(root / "pending" / f"{ident()}.json", granted(root))  # a session moving its own request across
    assert "never granted" in ask(root, now=NOW + 1)


def test_an_unreadable_record_is_refused(root):
    approve(root)
    granted(root).write_text("{not json")
    assert ask(root, now=NOW + 10)


def test_files_are_private(root):
    ask(root)
    f = root / "pending" / f"{ident()}.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700


# --- granting --------------------------------------------------------------------------------------------------


def test_grant_needs_a_plain_unambiguous_id(root):
    ask(root)
    for bad in ("", "xyz", "abc", "../../etc", ident()[:7]):
        with pytest.raises(ApprovalError):
            approvals.grant(root, bad, NOW + 1)
    # two pending ids that share a prefix are told to give more digits
    (root / "pending" / f"{ident()[:8]}{'0' * 8}.json").write_text("{}")
    (root / "pending" / f"{ident()[:8]}{'1' * 8}.json").write_text("{}")
    with pytest.raises(ApprovalError, match="more digits"):
        approvals.grant(root, ident()[:8], NOW + 1)


def test_the_person_sees_the_call_with_escapes_shown_not_run():
    bound = Bound("opencode", "command", "echo hi\x1b[2J\nrm x", "/w", "s")
    text = approvals.describe(bound)
    assert "\x1b" not in text and "\\u001b" in text and "\\n" in text


# --- who may approve -------------------------------------------------------------------------------------------


def test_a_gated_session_or_a_pipe_cannot_approve():
    assert "gated session" in approvals.refuse_reason({approvals.ENV_MARKER: "1"}, True)
    assert "terminal" in approvals.refuse_reason({}, False)
    assert approvals.refuse_reason({}, True) is None


def test_ws_approve_refuses_without_a_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    ask(tmp_path / "approvals")
    assert cli.main(["approve", ident()[:12]]) == 1  # pytest's stdin is not a terminal
    assert "terminal" in capsys.readouterr().err
    assert not (tmp_path / "approvals" / "granted").exists()


def test_ws_approve_refuses_inside_a_gated_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv(approvals.ENV_MARKER, "1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    assert cli.main(["approve", "abcdef12", "--yes"]) == 1
    assert "gated session" in capsys.readouterr().err
    assert cli.main(["approve", "--list"]) == 1


def test_ws_approve_grants_at_a_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.delenv(approvals.ENV_MARKER, raising=False)
    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True, "read": lambda self: ""})())
    approvals.require(tmp_path / "approvals", CALL)  # real clock: the person is approving now
    assert cli.main(["approve", "--list"]) == 0
    assert ident()[:12] in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    assert cli.main(["approve", ident()[:12]]) == 1  # said no
    assert not (tmp_path / "approvals" / "granted").exists()
    monkeypatch.setattr("builtins.input", lambda _="": "y")
    assert cli.main(["approve", ident()[:12]]) == 0
    assert approvals.require(tmp_path / "approvals", CALL) is None  # the session's retry runs


def test_the_matcher_denies_ws_approve_as_a_shell_command():
    pol = policy.loads("")
    for cmd in ("ws approve abcdef12", "uv run ws approve abcdef12 --yes", "ws approve --list"):
        assert policy.is_denied_command(pol, cmd), cmd


def test_approvals_are_a_control_path(tmp_path):
    assert "approvals" in policy.CONTROL_NAMES
    reason = policy.is_denied_command(
        policy.loads(""),
        f"echo x > {tmp_path}/approvals/granted/a.json",
        protect=tuple(policy.control_paths(tmp_path)),
    )
    assert reason


# --- in the gate -----------------------------------------------------------------------------------------------

ASK = policy.loads('[gate]\nask_commands = ["terraform apply*"]\ndeny_commands = ["rm -* /"]\n')


def payload(command, session="sess-1", cwd="/work"):
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "session_id": session, "cwd": cwd})


def test_the_gate_asks_then_runs_the_same_call_once(root):
    first = gate.check("claude-code", payload("terraform apply -auto-approve"), ASK, approval_root=root)
    assert first and "ws approve" in first
    bound = Bound("claude-code", "command", "terraform apply -auto-approve", "/work", "sess-1")
    approvals.grant(root, bound.digest()[:12])
    assert gate.check("claude-code", payload("terraform apply -auto-approve"), ASK, approval_root=root) is None
    assert gate.check("claude-code", payload("terraform apply -auto-approve"), ASK, approval_root=root)


def test_the_gate_binds_the_session_and_folder(root):
    gate.check("claude-code", payload("terraform apply"), ASK, approval_root=root)
    approvals.grant(root, Bound("claude-code", "command", "terraform apply", "/work", "sess-1").digest()[:12])
    assert gate.check("claude-code", payload("terraform apply", session="other"), ASK, approval_root=root)
    assert gate.check("claude-code", payload("terraform apply", cwd="/elsewhere"), ASK, approval_root=root)
    assert gate.check("claude-code", payload("terraform apply"), ASK, approval_root=root) is None


def test_a_hidden_ask_inside_a_compound_command_still_asks(root):
    assert gate.check("claude-code", payload("echo hi && bash -c 'terraform apply'"), ASK, approval_root=root)


def test_a_command_that_is_not_asked_about_runs_and_leaves_no_record(root):
    assert gate.check("claude-code", payload("terraform plan"), ASK, approval_root=root) is None
    assert not root.exists()


def test_a_denied_command_is_never_offered_for_approval(root):
    why = gate.check("claude-code", payload("rm -rf /"), ASK, approval_root=root)
    assert why and "deny_commands" in why and not root.exists()


def test_without_an_approvals_folder_an_ask_is_a_deny():
    assert "approval" in gate.check("claude-code", payload("terraform apply"), ASK)


def test_ask_commands_and_allow_hosts_load_and_validate():
    ip = "203.0.113" + ".7:443"  # split so the PII gate does not flag a documentation address
    p = policy.loads(f'[gate]\nask_commands = ["git push*"]\nallow_hosts = ["github.com", "*.npmjs.org", "{ip}"]\n')
    assert p.gate.ask_commands == ("git push*",) and len(p.gate.allow_hosts) == 3
    for bad in ('["https://x.com/"]', '["a b"]', '["*"]', '[""]'):
        with pytest.raises(policy.PolicyError, match="allow_hosts"):
            policy.loads(f"[gate]\nallow_hosts = {bad}\n")
    assert policy.loads(policy.default_toml()).gate.ask_commands == ()


# --- fix round: exact grant, FIFOs, claims ---------------------------------------------------------------------


def test_the_person_grants_exactly_what_was_shown_even_if_the_folder_changes_after(root):
    ask(root)
    found = approvals._find(root, ident()[:12], NOW + 1)
    pend = root / "pending"
    # an inotify-quick swap: another record with the same prefix appears, and the shown one is rewritten
    evil = Bound("claude-code", "command", "rm -rf ~", "/work", "sess-1")
    (pend / f"{ident()[:12]}{'f' * 52}.json").write_text(json.dumps({"id": "x"}))
    edit(pend / f"{ident()}.json", lambda r: r["bound"].update(value=evil.value))
    assert approvals.grant_found(root, found, NOW + 2) == ident()
    assert ask(root, CALL, NOW + 3) is None  # the call that was shown runs
    assert ask(root, evil, NOW + 3)  # and the one swapped in does not


def test_a_colliding_prefix_is_ambiguous_not_a_swap(root):
    ask(root)
    (root / "pending" / f"{ident()[:12]}{'e' * 52}.json").write_text("{}")
    with pytest.raises(ApprovalError, match="more digits"):
        approvals._find(root, ident()[:12], NOW + 1)
    assert approvals._find(root, ident(), NOW + 1).bound == CALL  # the full id still works


def test_a_fifo_or_a_link_in_place_of_a_record_is_refused_without_hanging(root):
    ask(root)
    pend = root / "pending"
    os.mkfifo(pend / f"{'a' * 64}.json")
    os.symlink("/etc/hostname", pend / f"{'b' * 64}.json")
    assert [i for i, *_ in approvals.pending(root, NOW + 1)] == [ident()]
    ask(root, CALL, NOW + 2)  # the sweep reads every file in the folder: it must not block on the FIFO
    for name in ("a", "b"):
        with pytest.raises(ApprovalError):
            approvals._load(pend / f"{name * 64}.json", NOW)


def test_what_the_person_is_shown_has_every_field_escaped():
    bound = Bound("open\x1b[2Jcode", "com\nmand", "echo\x07 hi", "/w\r", "s\x00")
    text = approvals.describe(bound)
    assert not any(c in text for c in "\x1b\x07\r\x00") and text.count("\n") == 5
    assert "\\u001b" in text and "\\u0007" in text


def test_the_list_escapes_what_the_session_wrote(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.delenv(approvals.ENV_MARKER, raising=False)
    approvals.require(tmp_path / "approvals", Bound("op\x1bencode", "command", "x\ry", "/w", "s"))
    assert cli.main(["approve", "--list"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out


def test_inside_the_wall_a_grant_is_peeked_not_used_and_comes_back_as_a_claim(root):
    approve(root)
    got = approvals.require(root, CALL, NOW + 10, consume=False)
    assert got == approvals.Claim(ident())
    assert granted(root).exists()  # still there: only the launcher uses it up
    assert approvals.consume_id(root, ident(), NOW + 11) is None
    assert approvals.require(root, CALL, NOW + 12, consume=False) != approvals.Claim(ident())


def test_peeking_asks_when_there_is_no_valid_grant(root):
    why = approvals.require(root, CALL, NOW, consume=False)
    assert isinstance(why, str) and "ws approve" in why
    approve(root, now=NOW + 1)
    edit(granted(root), lambda r: r["bound"].update(value="other"))
    assert isinstance(approvals.require(root, CALL, NOW + 20, consume=False), str)


def test_the_gate_check_exits_4_with_the_claim_inside_the_wall(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv(approvals.FD_ENV, "5")
    (tmp_path / "policy.toml").write_text('[gate]\nask_commands = ["terraform apply*"]\n')
    raw = payload("terraform apply")
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert cli.main(["gate", "check", "--runner", "claude-code"]) == gate.DENY  # first try: asks
    bound = Bound("claude-code", "command", "terraform apply", "/work", "sess-1")
    approvals.grant(tmp_path / "approvals", bound.digest()[:12])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert cli.main(["gate", "check", "--runner", "claude-code"]) == gate.CLAIM
    assert json.loads(capsys.readouterr().out) == {"claim": bound.digest()}
    assert (tmp_path / "approvals" / "granted" / f"{bound.digest()}.json").exists()


def test_a_refusal_can_say_what_stopped_the_call(root):
    text = approvals.require(root, CALL, NOW, why_asked="bulk-staging: too wide")
    assert text.startswith("bulk-staging: too wide: needs a person's approval: in a terminal")
    assert ask(root).startswith("needs a person's approval")  # without it, the old text


def test_a_command_that_needs_approval_with_no_folder_to_ask_through_is_an_ask():
    p = policy.loads('[gate]\nask_commands = ["terraform apply*"]\n')
    raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": "terraform apply"}})
    reason = gate.check("claude-code", raw, p)
    assert isinstance(reason, gate.Ask) and "needs a person's approval" in reason
