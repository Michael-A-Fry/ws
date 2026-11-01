"""The claims guard (ws.guards.claims): which sentences are claims, the check against the ref, and the log on stop."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from ws import cli, gate, guards, policy
from ws.adapters import claude_code
from ws.guards import Context
from ws.guards import claims as cl

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
W = cl.DEFAULT_WORDS


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def git(cwd, *args) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(home):
    """A repo whose `origin/main` holds commit `on` and not commit `off`."""
    d = home / "repo"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    (d / "a").write_text("1")
    git(d, "add", "a")
    git(d, "commit", "-q", "-m", "first")
    on = git(d, "rev-parse", "HEAD")
    git(d, "update-ref", "refs/remotes/origin/main", on)
    git(d, "checkout", "-q", "-b", "work")
    (d / "a").write_text("2")
    git(d, "commit", "-q", "-am", "second")
    return d, on[:10], git(d, "rev-parse", "HEAD")[:10]


def message(text: str, cwd, event="pre", **extra):
    payload = {
        "tool_name": "SendMessage",
        "tool_input": {"to": "lead", "message": text},
        "session_id": "s1",
        "cwd": str(cwd),
    }
    return claude_code.parse(payload), Context("claude-code", event, "s1", str(cwd), {**payload, **extra})


def judge(text, cwd, cfg=None):
    call, ctx = message(text, cwd)
    return cl.judge(call, cfg or cl.config({}, "w"), ctx)


# --- reading claims ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expect",
    [
        ("merged into main abc1234", [("merged into main", "abc1234")]),
        ("Merged abc1234 into main.", [("Merged into main", "abc1234")]),  # a commit may sit inside the phrase
        ("abc1234 is on main", [("on main", "abc1234")]),
        ("abc1234 on main", [("on main", "abc1234")]),  # the claim ends the sentence: not "quoted" by nothing
        ("merged to main: abc1234", [("merged to main", "abc1234")]),
        (
            "pushed to main 0123abcdef0123abcdef0123abcdef0123abcdef",
            [("pushed to main", "0123abcdef0123abcdef0123abcdef0123abcdef")],
        ),
        (
            "they merged into main abc1234 and def5678",
            [("merged into main", "abc1234"), ("merged into main", "def5678")],
        ),
        ("merged into main abc1234 and again abc1234", [("merged into main", "abc1234")]),
        ("It was pushed to main. See abc1234 for the diff.", []),  # two sentences
        ("merged into main abc123", []),  # six characters
        ("merged into main 1234567 rows", []),  # no hex letter
        ("merged into main deadbeef", []),  # no digit
        ("merged into main ABC1234", []),  # git prints lower case
        ("the pushed-branch abc1234 on main-street", []),  # not the words
        ("unmerged into main abc1234", []),
        ("merged abc1234", []),  # merged where? only main is checked
        ("pushed abc1234", []),
    ],
)
def test_what_counts_as_a_claim(text, expect):
    assert cl.find_claims(text, W) == expect


@pytest.mark.parametrize(
    "text",
    [
        "the words `merged into main` abc1234",
        'the words "merged into main" abc1234',
        "the words 'pushed to main' abc1234",
        "I have not merged into main abc1234",
        "abc1234 is not on main",
        "it isn't pushed to main abc1234",
        "never merged into main abc1234",
        "it will be merged into main: abc1234",
        "it'll be on main abc1234",
        "once it's merged into main abc1234",
        "to be pushed to main abc1234",
        "not yet really merged into main abc1234",
    ],
)
def test_quotes_negation_and_the_future_are_not_claims(text):
    assert cl.find_claims(text, W) == []


@pytest.mark.parametrize(
    "text",
    [
        "abc1234 is on main and pushed to origin/work",
        "merged abc1234 into main, then pushed to origin/feature-x",
        "abc1234 is on origin/work and on main",
        "pushed abc1234 to work, so it is on main soon",
        "merged abc1234 into release/1.2 and then on main",
    ],
)
def test_a_sentence_that_names_another_branch_is_not_a_claim(text):
    assert cl.find_claims(text, W) == []


def test_naming_main_or_the_ref_itself_is_still_a_claim():
    assert cl.find_claims("merged abc1234 into main from origin/main", W)
    assert cl.find_claims("abc1234 is on main", W, "origin/work")
    assert cl.find_claims("landed abc1234 on origin/work", ("landed",), "origin/work")
    assert cl.find_claims("merged abc1234 into main", W, "origin/work")


def test_a_negation_more_than_four_words_back_does_not_cancel_the_claim():
    assert cl.find_claims("no problem at all, and then one two three four merged into main abc1234", W)


def test_custom_words_replace_the_defaults():
    assert cl.find_claims("landed abc1234", ("landed",)) == [("landed", "abc1234")]
    assert cl.find_claims("merged into main abc1234", ("landed",)) == []


@pytest.mark.parametrize(
    "table, key",
    [
        ({"word": ["x"]}, "word"),
        ({"words": []}, "words"),
        ({"words": "merged"}, "words"),
        ({"ref": "-x"}, "ref"),
        ({"ref": ""}, "ref"),
        ({"ref": "a b"}, "ref"),
        ({"ref": 3}, "ref"),
    ],
)
def test_a_bad_table_names_the_key(table, key):
    with pytest.raises(policy.PolicyError, match=key):
        cl.config(table, "[guards.claims]")


# --- the check against the ref -----------------------------------------------------------------------------------


@needs_git
def test_a_commit_not_on_the_ref_is_denied_and_one_on_it_is_allowed(repo):
    d, on, off = repo
    denied = judge(f"merged {off} into main", d)
    assert denied.action == "deny" and off in denied.reason and "origin/main" in denied.reason and not denied.approvable
    assert judge(f"merged {on} into main", d) is None
    assert judge(f"all done. I merged {on} into main, then pushed {on} to main.", d) is None


@needs_git
def test_one_wrong_commit_among_right_ones_is_denied(repo):
    d, on, off = repo
    assert judge(f"merged into main: {on} and {off}", d).action == "deny"


@needs_git
def test_a_commit_this_repository_doesnt_have_is_denied(repo):
    d, _, _ = repo
    assert judge("merged abc1234 into main", d).action == "deny"


@needs_git
def test_a_message_with_no_claim_is_allowed_without_asking_git(repo, monkeypatch):
    d, _, off = repo
    monkeypatch.setattr(cl, "on_ref", lambda *a: pytest.fail("git was asked"))
    assert judge(f"committed {off} on a branch", d) is None
    assert judge("merged, thanks", d) is None


@needs_git
def test_the_ref_is_the_one_the_policy_names(repo):
    d, on, off = repo
    git(d, "update-ref", "refs/remotes/origin/work", "HEAD")
    assert judge(f"merged {off} into main", d, cl.config({"ref": "origin/work"}, "w")) is None
    assert judge(f"merged {on} into main", d, cl.config({"ref": "origin/nope"}, "w")).action == "note"


@needs_git
def test_a_folder_that_is_not_a_repository_is_a_note(home):
    d = home / "plain"
    d.mkdir()
    f = judge("merged abc1234 into main", d)
    assert f.action == "note" and "isn't a git repository" in f.reason


@needs_git
def test_the_deny_reason_says_to_fetch_when_the_ref_may_be_stale(repo):
    d, _, off = repo
    reason = judge(f"merged {off} into main", d).reason
    assert "origin/main may be out of date" in reason and "`git fetch`" in reason


@needs_git
def test_a_claim_about_another_branch_is_allowed_without_asking_git(repo, monkeypatch):
    d, _, off = repo
    monkeypatch.setattr(cl, "on_ref", lambda *a: pytest.fail("git was asked"))
    assert judge(f"pushed {off} to origin/work, so it is on main", d) is None


@needs_git
def test_git_runs_with_lazy_fetch_off_and_a_bare_environment(repo, monkeypatch):
    d, on, _ = repo
    seen = []
    real = subprocess.run

    def spy(*a, **k):
        seen.append(k["env"])
        return real(*a, **k)

    monkeypatch.setattr(cl.subprocess, "run", spy)
    monkeypatch.setenv("GIT_DIR", "/nowhere")
    assert cl.on_ref(str(d), on, "origin/main") is True
    assert seen and all(e["GIT_NO_LAZY_FETCH"] == "1" and "GIT_DIR" not in e for e in seen)


def test_git_time_is_one_budget_for_the_whole_hook(tmp_path, monkeypatch):
    now = [0.0]
    budget = cl.Budget(8.0, clock=lambda: now[0])
    calls = []

    def fake_run(argv, **k):
        calls.append(k["timeout"])
        now[0] += 5.0  # each git call takes five seconds
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cl.subprocess, "run", fake_run)
    note = cl.on_ref(str(tmp_path), "abc1234", "origin/main", budget)
    assert calls == [8.0, 3.0]  # the second call gets only what is left
    assert isinstance(note, str) and "took more than 8 seconds" in note  # and the third never starts


def test_a_timeout_in_git_lets_the_message_through_with_a_note(home, monkeypatch):
    d = home / "d"
    d.mkdir()

    def slow(argv, **k):
        raise subprocess.TimeoutExpired(argv, k["timeout"])

    monkeypatch.setattr(cl.subprocess, "run", slow)
    f = judge("merged abc1234 into main", d)
    assert f.action == "note" and "claims not checked" in f.reason


def test_a_missing_folder_or_no_git_is_a_note(home, monkeypatch):
    assert judge("merged abc1234 into main", home / "nowhere").action == "note"
    d = home / "d"
    d.mkdir()
    monkeypatch.setenv("PATH", str(home / "empty"))
    f = judge("merged abc1234 into main", d)
    assert f.action == "note" and "git can't run" in f.reason


# --- through the gate and replay ---------------------------------------------------------------------------------


def _payload(text, cwd):
    return {
        "tool_name": "SendMessage",
        "session_id": "s1",
        "cwd": str(cwd),
        "tool_input": {"to": "lead", "message": text},
    }


@needs_git
def test_the_gate_denies_a_false_claim_and_lets_a_true_one_through(repo):
    d, on, off = repo
    pol = policy.loads('[guards]\non = ["claims"]\n')
    reason = gate.check("claude-code", json.dumps(_payload(f"merged {off} into main", d)), pol)
    assert reason.startswith("claims: ") and off in reason
    assert gate.check("claude-code", json.dumps(_payload(f"merged {on} into main", d)), pol) is None


@needs_git
def test_replay_of_a_send_message_matches_the_ref(repo, home, capsys):
    d, on, off = repo
    pol = home / "policy.toml"
    pol.write_text('[guards]\non = ["claims"]\n')
    log = home / "calls.jsonl"
    log.write_text(
        json.dumps({"payload": _payload(f"merged {off} into main", d), "expect": "deny"})
        + "\n"
        + json.dumps({"payload": _payload(f"merged {on} into main", d), "expect": "allow"})
        + "\n"
    )
    assert cli.main(["gate", "replay", str(log), "--runner", "claude-code", "--policy", str(pol)]) == 0


def test_claims_reads_messages_and_stops_and_opencode_does_not_run_it():
    mod = guards.get("claims")
    assert mod.EVENTS == ("pre", "stop") and mod.KINDS == ("message",)
    from ws.adapters import opencode

    assert "claims" in opencode.UNSUPPORTED_GUARDS


# --- the log on stop ---------------------------------------------------------------------------------------------


@needs_git
def test_stop_logs_the_claims_in_the_last_message_and_stops_nothing(repo, home):
    d, on, off = repo
    ctx = Context(
        "claude-code",
        "stop",
        "s1",
        str(d),
        {"last_assistant_message": f"merged {on} into main. Also pushed {off} to main."},
        clock=lambda: 0.0,
    )
    assert cl.judge(None, cl.config({}, "w"), ctx) is None
    lines = (home / "wshome" / "logs" / "claims.jsonl").read_text().splitlines()
    rec = json.loads(lines[-1])
    assert rec["session"] == "s1" and rec["ref"] == "origin/main" and rec["at"].startswith("1970-01-01")
    assert [(c["word"], c["commit"], c["on_ref"]) for c in rec["claims"]] == [
        ("merged into main", on, True),
        ("pushed to main", off, False),
    ]


def test_stop_with_no_claim_or_no_message_writes_nothing(home):
    for payload in ({}, {"last_assistant_message": "all finished"}, {"last_assistant_message": 7}):
        ctx = Context("claude-code", "stop", "s1", str(home), payload)
        assert cl.judge(None, cl.config({}, "w"), ctx) is None
    assert not (home / "wshome" / "logs" / "claims.jsonl").exists()
