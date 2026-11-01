"""The claims guard (ws.guards.claims): which sentences are claims, the check against the ref, and the log on stop."""

from __future__ import annotations

import json
import os
import re
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
        env={**os.environ, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"},
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
    off = git(d, "rev-parse", "HEAD")
    for sha in (on[:10], off[:10]):  # fixed dates give fixed ids; each must look like a commit id to the guard
        assert re.search(r"\d", sha) and re.search(r"[a-f]", sha), sha
    return d, on[:10], off[:10]


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
    assert (rec["schema"], rec["ts"]) == (1, "1970-01-01T00:00:00.000Z")
    assert [(c["word"], c["commit"], c["on_ref"]) for c in rec["claims"]] == [
        ("merged into main", on, True),
        ("pushed to main", off, False),
    ]


def test_stop_with_no_claim_or_no_message_writes_nothing(home):
    for payload in ({}, {"last_assistant_message": "all finished"}, {"last_assistant_message": 7}):
        ctx = Context("claude-code", "stop", "s1", str(home), payload)
        assert cl.judge(None, cl.config({}, "w"), ctx) is None
    assert not (home / "wshome" / "logs" / "claims.jsonl").exists()


# --- release claims ----------------------------------------------------------------------------------------------


def releases(home, current="v0.2", link=True):
    """A release folder holding v0.2 and v0.3, with `current` pointing at `current` (or not a link)."""
    root = home / "rel"
    for name in ("v0.2", "v0.2.1", "v0.3", "r2026-11-02.1"):
        (root / name).mkdir(parents=True, exist_ok=True)
    if current and link:
        (root / "current").symlink_to(root / current)
    elif current:
        (root / "current").mkdir()
    return root


def release_cfg(root, **extra):
    return cl.config({"release_dir": str(root), **extra}, "[guards.claims]")


def test_release_claims_are_off_until_a_release_dir_is_set(home):
    assert cl.config({}, "w").release_dir == "" and cl.config({}, "w").release_tags == cl.DEFAULT_TAGS
    assert judge("v9.9 is live", home) is None


@pytest.mark.parametrize(
    "text",
    ["v0.2 is live", "We promoted v0.2 to live.", "live now: v0.2", "v0.2 is now live", "V0.2 went live", "live: v0.2"],
)
def test_the_promoted_release_may_be_called_live(home, text):
    assert judge(text, home, release_cfg(releases(home))) is None


@pytest.mark.parametrize(
    "text",
    [
        "v0.3 is live",
        "promoted v0.3 to live",
        "live now: v0.3",
        "v0.2 is live, v0.3 is live too",
        "v0.2 and v0.3 are live",
        "v0.2 is live; v0.3 is live",
        "v0.2 is live but v0.3 is live as well",
        "The live release is v0.3.",
        "v0.2.1 is live",
    ],
)
def test_another_release_called_live_is_denied(home, text):
    found = judge(text, home, release_cfg(releases(home)))
    assert found.action == "deny" and "current points at v0.2" in found.reason


@pytest.mark.parametrize(
    "text",
    [
        "v0.3 goes live after the next promote",
        "v0.3 will be live tomorrow",
        "once v0.3 is live we stop",
        "when v0.3 is live, tell me",
        "v0.3 isn't live",
        "v0.3 is not live yet",
        "never promoted v0.3 to live",
        "the words `v0.3 is live` are a quote",
        'say "v0.3 is live" when it is',
        "v0.3 is staged, v0.2 is live",
        "v0.3 is in the folder. Nothing is live.",
        "it is live",
        "v0.3 is ready",
    ],
)
def test_negation_the_future_quotes_and_unnamed_releases_are_not_claims(home, text):
    assert judge(text, home, release_cfg(releases(home))) is None


def test_the_claim_is_denied_when_current_is_missing_not_a_link_or_dangling(home):
    root = releases(home, current=None)
    cfg = release_cfg(root)
    assert "is missing" in judge("v0.2 is live", home, cfg).reason
    (root / "current").mkdir()
    assert judge("v0.2 is live", home, cfg).action == "deny"  # a folder is not a link
    (root / "current").rmdir()
    (root / "current").symlink_to(root / "v0.9")
    assert judge("v0.2 is live", home, cfg).action == "deny"  # points at nothing
    (root / "current").unlink()
    (root / "current").write_text("v0.2")
    assert judge("v0.2 is live", home, cfg).action == "deny"  # a file is not a link


def test_a_release_folder_that_does_not_exist_denies_a_live_claim_and_nothing_else(home):
    cfg = release_cfg(home / "nowhere")
    assert judge("v0.2 is live", home, cfg).action == "deny"
    assert judge("nothing to say", home, cfg) is None


def test_the_target_may_be_a_relative_link_and_may_leave_off_the_v(home):
    root = releases(home, current=None)
    (root / "0.3").mkdir()
    (root / "current").symlink_to("0.3/")
    assert judge("v0.3 is live", home, release_cfg(root)) is None
    assert judge("v0.2 is live", home, release_cfg(root)).action == "deny"


def test_release_tags_are_the_shapes_the_policy_names(home):
    root = releases(home, current="r2026-11-02.1")
    cfg = release_cfg(root, release_tags=[r"r\d{4}-\d\d-\d\d\.\d+"])
    assert judge("r2026-11-02.1 is live", home, cfg) is None
    assert judge("r2026-11-03.1 is live", home, cfg).action == "deny"
    assert judge("v0.3 is live", home, cfg) is None  # not a tag shape this policy names


def test_a_tilde_in_release_dir_is_this_homes(home):
    releases(home / "home")
    cfg = cl.config({"release_dir": "~/rel"}, "w")
    assert judge("v0.2 is live", home, cfg) is None
    assert judge("v0.3 is live", home, cfg).action == "deny"


@needs_git
def test_release_and_commit_claims_are_judged_together(repo, home):
    d, on, off = repo
    cfg = release_cfg(releases(home))
    found = judge(f"{off} is on main and v0.3 is live", d, cfg)
    assert found.action == "deny" and off in found.reason and "v0.3" in found.reason
    assert judge(f"{on} is on main and v0.2 is live", d, cfg) is None


def test_the_release_check_reads_a_link_and_writes_nothing(home):
    root = releases(home)
    before = sorted(str(p) for p in home.rglob("*"))
    judge("v0.3 is live", home, release_cfg(root))
    assert sorted(str(p) for p in home.rglob("*")) == before


@pytest.mark.parametrize(
    "table, key",
    [
        ({"release_dir": 5}, "release_dir"),
        ({"release_dir": "relative/dir"}, "release_dir"),
        ({"release_tags": []}, "release_tags"),
        ({"release_tags": "v1"}, "release_tags"),
        ({"release_tags": ["("]}, "release_tags"),
        ({"release_tags": ["v?"]}, "release_tags"),  # can match nothing
        ({"release_tags": [r"\b"]}, "release_tags"),  # a zero-width hit
        ({"release_tags": ["(?=v)"]}, "release_tags"),
        ({"release_tags": [r"(?:v\d+)?"]}, "release_tags"),
        ({"release_tags": [r"v\d+", ""]}, "release_tags"),
        ({"release_tags": [r"(\d+)+"]}, "release_tags"),  # a group that repeats, repeated
        ({"release_tags": [r"v(?:\d+)*x"]}, "release_tags"),
        ({"release_tags": [r"v([0-9]+\.?)+"]}, "release_tags"),
        ({"release_tags": ["(?i)v1"]}, "release_tags"),  # an inline flag only works at the start of a pattern
        ({"release_tags": ["v" + "1" * 300]}, "release_tags"),
    ],
)
def test_a_bad_release_key_names_it(table, key):
    with pytest.raises(policy.PolicyError, match=key):
        cl.config(table, "[guards.claims]")


def test_the_gate_denies_a_wrong_live_claim_from_a_policy(home):
    root = releases(home)
    pol = policy.loads(f'[guards]\non = ["claims"]\n[guards.claims]\nrelease_dir = "{root}"\n')
    assert pol.guards.config["claims"].release_dir == str(root)
    raw = json.dumps(
        {
            "tool_name": "SendMessage",
            "tool_input": {"to": "lead", "message": "v0.3 is live"},
            "session_id": "s1",
            "cwd": str(home),
        }
    )
    reason = gate.check("claude-code", raw, pol, home=home / "wshome")
    assert reason and "v0.3" in reason and "v0.2" in reason


def test_release_tags_with_a_fixed_part_in_a_repeated_group_are_fine():
    for tag in (*cl.DEFAULT_TAGS, "v[0-9]+[.][0-9]+([.][0-9]+)*", r"r\d{4}-\d\d-\d\d\.\d+", r"v(\d+)"):
        assert cl.config({"release_tags": [tag]}, "w").release_tags == (tag,)


def test_a_long_run_of_spaces_is_read_in_linear_time():
    import time

    for text in ("v0.2" + " " * 19990 + "x", "v0.2 is live" + " " * 19990 + "x", "v0.2 is live;" + " " * 19990 + "x"):
        t = time.perf_counter()
        cl.release_claims(text, cl.DEFAULT_TAGS)
        assert time.perf_counter() - t < 0.5


def test_one_stray_quote_does_not_hide_a_later_claim():
    assert cl.release_claims('He typed "v0.3 then: v0.4 is live', cl.DEFAULT_TAGS) == ["v0.3", "v0.4"]
    assert cl.release_claims("It's `v0.4 is live", cl.DEFAULT_TAGS) == ["v0.4"]
    assert cl.release_claims("He said \u201cv0.4 is live", cl.DEFAULT_TAGS) == ["v0.4"]


def test_a_claim_between_a_matched_pair_of_quotes_is_still_quoted():
    for text in ('She wrote "v0.4 is live" there', "see `v0.4 is live`", "\u201cv0.4 is live\u201d, she said"):
        assert cl.release_claims(text, cl.DEFAULT_TAGS) == []
