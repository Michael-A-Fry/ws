"""A guard's `mode = "log"`: where the OS wall is proven for the call, what the guard (or a rule the wall makes redundant)
would have refused is logged and allowed; everywhere else it is refused as before. The machine is faked at its edges
(the platform, `shutil.which`, the settings marker); no sandbox runs. Every folder, command and message is built at run
time under tmp_path, and nothing here reads the real HOME."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ws import approvals, doctor, gate, guards, jsonl, osinfo, paths, policy
from ws.adapters import claude_code

SENTENCE = "Make sure the branch is pushed before you report"


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "home" / ".ws"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    (tmp_path / "home" / ".ws").mkdir(parents=True)
    (tmp_path / "proj").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "main" / ".git" / "worktrees" / "wt").mkdir(parents=True)
    return tmp_path


def machine(monkeypatch, *, system="linux", binary="bwrap", marker="1"):
    """The edges the effective mode reads: the platform, which sandbox binary is on PATH, and ws's own settings marker."""
    monkeypatch.setattr(gate, "_platform", lambda: system)
    real = shutil.which

    def which(name, *a, **k):
        if name in ("bwrap", "sandbox-exec"):  # only the one the test names is on this machine
            return f"/opt/fake/{name}" if name == binary else None
        return real(name, *a, **k)

    monkeypatch.setattr(shutil, "which", which)
    if marker is None:
        monkeypatch.delenv(approvals.SANDBOX_MARKER, raising=False)
    else:
        monkeypatch.setenv(approvals.SANDBOX_MARKER, marker)


@pytest.fixture
def walled(box, monkeypatch):
    machine(monkeypatch)
    return box


def write_policy(box, mode: str = "log", extra: str = "") -> str:
    (box / "rel").mkdir(exist_ok=True)
    text = (
        f'[gate]\n{extra}\n[guards]\non = ["claims", "checkout-pin"]\n'
        f'[guards.claims]\nrelease_dir = "{box / "rel"}"\nmode = "{mode}"\n'
        f'[guards.checkout-pin]\npaths = ["{box / "main"}"]\nmode = "{mode}"\n'
    )
    f = box / "policy.toml"
    f.write_text(text)
    return str(f)


def bash(box, command: str, **tool_input) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command, **tool_input},
        "session_id": "sess-1",
        "cwd": str(box / "proj"),
    }


def cases(box) -> dict[str, tuple[dict, str]]:
    """The four neutral calls, each refused today for a reason the wall makes redundant (or, for the sentence, by a
    guard asked to log), and the kind each is logged under."""
    return {
        "loop": (bash(box, f'for f in $(ls {box / "bin"}); do "$f" --help; done'), "rule:unknown-command-word"),
        "ifs": (bash(box, 'IFS=: read -r a b <<< "x:y"; echo "$a"'), "rule:sets-ifs"),
        "sentence": (
            {
                "tool_name": "SendMessage",
                "tool_input": {"to": "lead", "message": f"v9.9 is live. {SENTENCE}"},
                "session_id": "sess-1",
                "cwd": str(box / "proj"),
            },
            "claims",
        ),
        "scratch": (bash(box, 'scratch=$(mktemp -p .); echo hi > "$scratch"'), "rule:unread-write"),
    }


def run(payload: dict, policy_file: str) -> tuple[int, str, str]:
    argv = ["--runner", "claude-code", "--policy", policy_file, *paths.pin_of().args()]
    return gate.run_check(gate.hook_args("check", argv), json.dumps(payload))


def lines() -> list[dict]:
    log = paths.state_dir() / "logs" / gate.VERDICT_LOG
    return jsonl.read(log) if log.exists() else []


def entries(line: dict, kind: str) -> list[dict]:
    return [g for g in line["guards"] if g["guard"] == kind]


def test_each_neutral_case_is_allowed_and_logged_once_inside_the_wall(walled):
    pol = write_policy(walled)
    for name, (payload, kind) in cases(walled).items():
        before = len(lines())
        code, _, err = run(payload, pol)
        assert code == 0, (name, err)
        fresh = lines()[before:]
        assert len(fresh) == 1, name  # one line for the hook run, not one for the call and one more
        (line,) = fresh
        assert line["decision"] == "allow" and line["layer"] is None and line["session"] == "sess-1"
        assert line["tool"] == payload["tool_name"]
        (hit,) = entries(line, kind)
        assert hit["result"] == "would-deny" and hit["count"] == 1 and isinstance(hit["ms"], float), name
        assert not [g for g in line["guards"] if g["result"] == "deny" or "gap" in g], name


def test_each_case_still_denies_with_bubblewrap_faked_missing(box, monkeypatch):
    machine(monkeypatch, binary="not-bwrap")
    pol = write_policy(box)
    for name, (payload, kind) in cases(box).items():
        before = len(lines())
        code, _, err = run(payload, pol)
        assert code == gate.DENY, (name, err)
        (line,) = lines()[before:]
        assert line["decision"] == "deny"
        assert not [g for g in line["guards"] if g["result"] == "would-deny"]
        assert any(g.get("gap") == "no-sandbox-binary" for g in line["guards"]), (name, line)  # and it says why


@pytest.mark.parametrize("marker", ["0", None, "2", ""])
def test_each_case_still_denies_with_the_sandbox_off(box, monkeypatch, marker):
    machine(monkeypatch, marker=marker)  # `0` is ws's own mark for settings written without the sandbox block
    pol = write_policy(box)
    for name, (payload, _) in cases(box).items():
        before = len(lines())
        code, _, err = run(payload, pol)
        assert code == gate.DENY, (name, err)
        (line,) = lines()[before:]
        assert any(g.get("gap") == "sandbox-off" for g in line["guards"]), (name, line)


@pytest.mark.parametrize(
    "system, binary, gap",
    [
        ("win32", "bwrap", "native-windows"),
        ("darwin", "bwrap", "no-sandbox-binary"),  # bwrap is not macOS's wall
        ("linux", "sandbox-exec", "no-sandbox-binary"),  # nor sandbox-exec Linux's
    ],
)
def test_the_platform_and_the_binary_are_each_needed(box, monkeypatch, system, binary, gap):
    machine(monkeypatch, system=system, binary=binary)
    pol = write_policy(box)
    code, _, _ = run(cases(box)["sentence"][0], pol)
    assert code == gate.DENY
    assert any(g.get("gap") == gap for g in lines()[-1]["guards"])


def test_macos_with_sandbox_exec_is_a_wall(box, monkeypatch):
    machine(monkeypatch, system="darwin", binary="sandbox-exec")
    assert run(cases(box)["sentence"][0], write_policy(box))[0] == 0


def test_a_call_marked_to_run_outside_the_sandbox_enforces(walled):
    pol = write_policy(walled)
    for value in (True, "true", 1):
        payload = cases(walled)["loop"][0]
        payload["tool_input"]["dangerouslyDisableSandbox"] = value
        code, _, _ = run(payload, pol)
        assert code == gate.DENY, value
        assert any(g.get("gap") == "unsandboxed-call" for g in lines()[-1]["guards"])
    payload = cases(walled)["loop"][0]
    payload["tool_input"]["dangerouslyDisableSandbox"] = False  # said so, and it is not
    assert run(payload, pol)[0] == 0


def test_a_forged_sandbox_marker_does_not_turn_log_on(box, monkeypatch):
    """A project's settings can put WS_SANDBOXED=1 (or a PATH with a `bwrap` of its own) in the session's environment
    while the sandbox is off: the call is then enforced, and a project that opens the sandbox is refused outright."""
    machine(monkeypatch, marker="1")
    pol = write_policy(box)
    settings = box / "proj" / ".claude" / "settings.json"
    settings.parent.mkdir()
    for forged in ({"WS_SANDBOXED": "1"}, {approvals.ENV_MARKER: "1"}, {"PATH": str(box / "bin")}):
        settings.write_text(json.dumps({"env": forged}))
        for name, (payload, _) in cases(box).items():
            before = len(lines())
            code, _, err = run(payload, pol)
            assert code == gate.DENY, (forged, name, err)
            (line,) = lines()[before:]
            assert any(g.get("gap") == "preflight" for g in line["guards"]), (forged, name)
    settings.write_text(json.dumps({"sandbox": {"enabled": False}, "env": {"WS_SANDBOXED": "1"}}))
    code, _, err = run(cases(box)["ifs"][0], pol)
    assert code == gate.DENY and "sandbox" in err  # the preflight refuses every call of such a project
    assert lines()[-1]["layer"] == "preflight"
    local = box / "proj" / ".claude" / "settings.local.json"  # the other project file, and an ancestor folder
    settings.unlink()
    local.write_text(json.dumps({"env": {"WS_SANDBOXED": "1"}}))
    assert run(cases(box)["ifs"][0], pol)[0] == gate.DENY
    local.unlink()
    (box / ".claude").mkdir()
    (box / ".claude" / "settings.json").write_text(json.dumps({"env": {"WS_SANDBOXED": "1"}}))
    assert run(cases(box)["ifs"][0], pol)[0] == gate.DENY
    (box / ".claude" / "settings.json").unlink()
    assert run(cases(box)["ifs"][0], pol)[0] == 0  # and with no project file making the claim, it is the wall


def test_a_replay_never_proves_the_wall(walled):
    """`ws gate replay` judges as a session with no wall would, so a recorded call answers the same on any machine, and a
    payload replayed on this one can't borrow the wall it happens to have."""
    pol = policy.load(Path(write_policy(walled)))
    lines_in = [json.dumps(payload) for payload, _ in cases(walled).values()]
    assert [r["result"] for r in gate.replay("claude-code", lines_in, pol)] == ["deny"] * 4
    assert [run(payload, write_policy(walled))[0] for payload, _ in cases(walled).values()] == [0] * 4  # the live hook


def test_the_persons_own_settings_may_carry_the_marker(box, monkeypatch):
    """`~/.claude/settings.json` is where ws writes the marker: the folder a project walk skips is the home itself."""
    machine(monkeypatch)
    (box / "home" / ".claude").mkdir()
    (box / "home" / ".claude" / "settings.json").write_text(json.dumps({"env": {"WS_SANDBOXED": "1"}}))
    assert run(cases(box)["ifs"][0], write_policy(box))[0] == 0


def test_a_would_deny_line_holds_no_command_path_or_text(walled):
    pol = write_policy(walled)
    for payload, _ in cases(walled).values():
        assert run(payload, pol)[0] == 0
    raw = (paths.state_dir() / "logs" / gate.VERDICT_LOG).read_text()
    assert len(raw.splitlines()) == 4
    for planted in (SENTENCE, "v9.9", "IFS", "mktemp", "scratch", "out.txt", "--help", str(walled), "release", "x:y"):
        assert planted not in raw, planted
    allowed = {"schema", "ts", "runner", "session", "event", "decision", "layer", "guards", "ms", "via", "tool"}
    for line in map(json.loads, raw.splitlines()):
        assert set(line) <= allowed
        assert all(set(g) <= {"guard", "result", "ms", "count", "gap"} for g in line["guards"])
        assert line["tool"] in ("Bash", "SendMessage")


def test_log_mode_consumes_no_approval(walled):
    """checkout-pin's deny is one a person may approve once. In log mode it is logged, nothing is asked, and a grant that
    is waiting for the same call is still there afterwards."""
    root = paths.state_dir() / "approvals"
    raw = json.dumps(bash(walled, "git pull") | {"cwd": str(walled / "main")})
    enforcing = policy.load(Path(write_policy(walled, "enforce")))
    logging = policy.load(Path(write_policy(walled, "log")))

    def check(pol):
        return gate.check("claude-code", raw, pol, approval_root=root, home=paths.state_dir())

    assert "ws approve" in check(enforcing)  # asked: a pending record is written
    ((ident, _, _),) = approvals.pending(root)
    approvals.grant(root, ident[:12])
    assert check(logging) is None  # allowed, the grant not used up
    assert approvals.pending(root) == []
    assert check(enforcing) is None  # the grant was still there: this is its one use
    assert "ws approve" in check(enforcing)  # and now it is gone
    approvals.grant(root, approvals.pending(root)[0][0][:12])
    verdicts: list[dict] = []
    assert (
        gate.check("claude-code", raw, logging, approval_root=root, home=paths.state_dir(), verdicts=verdicts) is None
    )
    assert [v["result"] for v in verdicts if v["guard"] == "checkout-pin"] == ["would-deny"]
    assert len(approvals.pending(root)) == 0 and (root / "granted").exists() and list((root / "granted").iterdir())


def test_the_rules_that_stay_denies_stay_denies_in_every_mode(walled):
    """The walk goes on past a refusal the wall makes redundant, so a hard refusal later in the same line still wins."""
    pol = write_policy(walled, extra='deny_commands = ["make deploy"]\ndeny_read = ["~/.ssh/**"]')
    home = walled / "home"
    for command in (
        "IFS=: ; make deploy",  # deny_commands
        "$PROGRAM --go; make deploy",
        f"IFS=: ; cat {home}/.ssh/id_rsa",  # deny_read
        f"$PROGRAM --go; cat {home}/.ssh/id_rsa",
        f"cat < {home}/.ssh/id_rsa; $PROGRAM",
        f"IFS=: ; echo hi > {paths.state_dir()}/policy.toml",  # a protected write
        f"$PROGRAM --go; echo hi > {paths.state_dir()}/policy.toml",
    ):
        code, _, err = run(bash(walled, command), pol)
        assert code == gate.DENY, command
        assert "unknown" not in err and "IFS" not in err or "deny" in err, command
        line = lines()[-1]
        assert line["decision"] == "deny" and line["layer"] == "rule:command"
        assert not [g for g in line["guards"] if g["result"] == "would-deny"], command


def test_a_protected_write_through_a_part_ws_cannot_read_stays_a_deny_in_a_proven_wall(walled):
    """A write whose path holds a variable could land on a file ws protects: that is the protected-write rule, not a
    scratch file, so the wall does not stand in for it."""
    pol = write_policy(walled)
    name = "V" + "ZQ"  # built at run time
    state = paths.state_dir()
    for command in (
        f"echo hi > ${{{name}}}/policy.toml",
        f"echo hi > ${{{name}}}{state}/policy.toml",
        f"echo hi > ${name}{state}/policy.toml",
    ):
        code, _, err = run(bash(walled, command), pol)
        assert code == gate.DENY, command
        line = lines()[-1]
        assert line["decision"] == "deny" and line["layer"] == "rule:command", command
        assert not [g for g in line["guards"] if g["result"] == "would-deny"], command
    # the scratch write inside the project is still the one case let through
    code, _, err = run(bash(walled, 'scratch=$(mktemp -p .); echo hi > "$scratch"'), pol)
    assert code == 0, err


def test_a_line_that_sets_the_separator_is_allowed_only_when_nothing_in_it_can_split(walled):
    """The field separator decides how an unquoted expansion splits into words, so ws cannot say which program such a
    line runs: a deny_commands or ask_commands rule would be bypassed. A line with no unquoted expansion has nothing to
    split."""
    pol = write_policy(walled, extra='deny_commands = ["make deploy"]\nask_commands = ["git push*"]')
    var = "IF" + "S"  # built at run time
    sep = "Z"
    splits = (
        f"{var}={sep}; c=make{sep}deploy; $c",
        f"{var}={sep}; c=make{sep}deploy; ${{c}} --now",
        f"{var}={sep}; c=make{sep}deploy; `echo $c`",
        f'{var}={sep}; c=make{sep}deploy; "$(echo $c)"',
        f"c=make{sep}deploy; {var}={sep}; $c",
        f"{var}={sep}; c=git{sep}push; $c",  # asked, so never run unasked
        f"bash -c '{var}={sep}; c=make{sep}deploy; $c'",  # a nested shell reads the line itself
        f'bash -c "{var}={sep}; c=make{sep}deploy; \\$c"',
        f"{var}={sep}; $c <<EOT\nit's\nEOT\necho 'x'",
        f"{var}={sep}; echo a # it's\n$c; echo 'x'",
    )
    for command in splits:
        code, _, err = run(bash(walled, command), pol)
        assert code == gate.DENY, command
        assert not [g for g in lines()[-1]["guards"] if g["result"] == "would-deny"], command
    # nothing to split: the ordinary neutral line is let through
    code, _, err = run(bash(walled, f'{var}=: read -r a b <<< "x:y"; echo "$a" \'$b\''), pol)
    assert code == 0, err


def test_a_spelling_of_the_separator_the_rename_would_miss_is_still_read_whole(walled):
    """A full-width letter or a backslash-newline inside the name is folded before the line is judged; the line read
    after the rename is folded the same way, so a deny_commands rule still sees what follows."""
    pol = write_policy(walled, extra='deny_commands = ["git push*"]')
    for command in (
        "\uff29\uff26\uff33=, ; git push origin",  # full-width letters
        "I\\\nFS=, ; git push origin",  # backslash-newline inside the name
        "\uff29\uff26\uff33=, ; echo hi; git push origin",
    ):
        code, _, err = run(bash(walled, command), pol)
        assert code == gate.DENY, command
        assert "deny_commands" in err, (command, err)
        assert not [g for g in lines()[-1]["guards"] if g["result"] == "would-deny"], command


def class_lines(box) -> dict[str, list[str]]:
    """For each refusal the wall makes redundant (policy.WALL_REDUNDANT), lines that also run something one of the rules
    the wall does not enforce would refuse. Names are built at run time."""
    word = "W" + "ZQ"  # a variable no one can read
    var = "IF" + "S"
    home = box / "home"
    state = paths.state_dir()
    return {
        policy.UNKNOWN_WORD: [
            f"{word}=$(echo git); ${word} push --force origin topic",  # deny_commands
            f"${word} push origin topic",  # ask_commands
            '"$(command -v git)" push origin topic',
            'x=$(mktemp -u); "$x" push --force origin topic',
            f"${word} --go; cat {home}/.ssh/id_rsa",  # deny_read
            f"${word} --go; echo hi > {state}/policy.toml",  # a protected write
        ],
        policy.SETS_IFS: [
            f"{var}=: ; ${word} push --force origin topic",
            f"{var}=: ; git push --force origin topic",
            f"{var}=: ; git push origin topic",
            f"{var}=: ; cat {home}/.ssh/id_rsa",
            f"{var}=: ; echo hi > {state}/policy.toml",
        ],
        policy.UNREAD_WRITE: [
            'x=$(mktemp -p .); echo hi > "$x"; git push --force origin topic',
            'x=$(mktemp -p .); echo hi > "$x"; git push origin topic',
            f'x=$(mktemp -p .); echo hi > "$x"; cat {home}/.ssh/id_rsa',
            f'x=$(mktemp -p .); echo hi > "$x"; echo hi > {state}/policy.toml',
            f'x=$(mktemp -p .); echo hi > "$x"; echo hi > $x{state}/policy.toml',
        ],
    }


@pytest.mark.parametrize("kind", policy.WALL_REDUNDANT)
def test_every_refusal_the_wall_makes_redundant_leaves_the_rules_it_does_not_enforce(walled, kind):
    """The wall holds where a line writes and what it reads, not which program runs: deny_commands and ask_commands are
    still ws's to enforce, so a line whose program word ws cannot read is a deny where such a rule is set."""
    rules = 'deny_commands = ["git push --force*"]\nask_commands = ["git push*"]\ndeny_read = ["~/.ssh/**"]'
    pol = write_policy(walled, extra=rules)
    for command in class_lines(walled)[kind]:
        code, _, _ = run(bash(walled, command), pol)
        assert code == gate.DENY, (kind, command)
        assert lines()[-1]["decision"] == "deny" and lines()[-1]["layer"], (
            kind,
            command,
        )  # the line says it was refused


def test_a_scratch_write_never_hides_a_later_refusal_in_the_same_command(walled):
    """One command with an unreadable redirect (a soft refusal the wall covers) and then a plain one: the plain one
    decides, whichever word comes first."""
    home = walled / "home"
    state = paths.state_dir()
    pol = write_policy(walled, extra='deny_read = ["~/.ssh/**"]')
    scratch = "x=$(mktemp -p .); "
    for command in (
        scratch + f'echo x > "$x" > {state}/policy.toml',  # a protected write after the scratch redirect
        scratch + f'echo x > "$x" > $x{state}/policy.toml',  # the same, through a word with an unread part
        scratch + f'cat > "$x" < {home}/.ssh/id_rsa',  # a deny_read path read after the scratch redirect
        scratch + f'cat > "$x" < $x{home}/.ssh/id_rsa',
        scratch + f'echo x > "$x" > {home}/.ssh/id_rsa',  # a deny_read path written after it
        scratch + 'cat > "$x" < "$y"',  # a read of a word that could be any file
    ):
        code, _, _ = run(bash(walled, command), pol)
        assert code == gate.DENY, command
        assert lines()[-1]["decision"] == "deny", command
    code, _, err = run(bash(walled, scratch + 'echo x > "$x" > "$x"'), pol)  # scratch files only
    assert code == 0, err


def test_an_unread_program_word_is_let_through_in_a_proven_wall_only_where_no_command_rule_is_set(walled):
    word = "W" + "ZQ"
    command = f"{word}=$(echo git); ${word} push origin topic"
    for extra in ("", 'deny_read = ["~/.ssh/**"]'):  # no deny_commands, no ask_commands
        code, _, err = run(bash(walled, command), write_policy(walled, extra=extra))
        assert code == 0, err
    for extra in ('deny_commands = ["make deploy"]', 'ask_commands = ["make deploy"]'):
        code, _, err = run(bash(walled, command), write_policy(walled, extra=extra))
        assert code == gate.DENY, extra


def test_an_unread_program_word_stays_a_deny_where_a_command_guard_enforces(walled):
    """A guard in enforce mode reads the program word as the rules do: one ws cannot read could be git."""
    word = "W" + "ZQ"
    pol = walled / "enforce.toml"
    pol.write_text('[gate]\n[guards]\non = ["bulk-staging"]\n')
    for command in (f"${word} git add -A", '"$(cat /dev/null)git" add -A', f"{word}=$(echo git); ${word} add -A"):
        code, _, _ = run(bash(walled, command), str(pol))
        assert code == gate.DENY, command
    pol.write_text('[gate]\n[guards]\non = ["bulk-staging"]\n[guards.bulk-staging]\nmode = "log"\n')
    code, _, err = run(bash(walled, f"${word} git add -A"), str(pol))
    assert code == 0, err  # a guard asked to log, in a proven wall, only counts it


def test_a_log_that_cannot_be_written_never_changes_the_decision(walled, monkeypatch):
    pol = write_policy(walled)

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(gate.jsonl, "append", broken)
    assert run(cases(walled)["ifs"][0], pol)[0] == 0
    assert run(cases(walled)["sentence"][0], pol)[0] == 0
    monkeypatch.setenv(approvals.SANDBOX_MARKER, "0")
    assert run(cases(walled)["ifs"][0], pol)[0] == gate.DENY


def test_the_line_is_written_with_a_cap_and_rotates_like_every_other_log(walled, monkeypatch):
    assert isinstance(gate.VERDICT_MAX_BYTES, int) and 0 < gate.VERDICT_MAX_BYTES <= jsonl.DEFAULT_MAX_BYTES
    seen = {}
    real = jsonl.append

    def spy(path, rec, **kw):
        seen.update(kw)
        return real(path, rec, **kw)

    monkeypatch.setattr(gate.jsonl, "append", spy)
    pol = write_policy(walled)
    assert run(cases(walled)["ifs"][0], pol)[0] == 0
    assert seen["max_bytes"] == gate.VERDICT_MAX_BYTES
    monkeypatch.setattr(gate, "VERDICT_MAX_BYTES", 700)
    for _ in range(12):
        assert run(cases(walled)["ifs"][0], pol)[0] == 0
    log = paths.state_dir() / "logs" / gate.VERDICT_LOG
    assert log.with_name(log.name + ".1").exists()  # rotated
    kept = [
        f for f in (log, *(log.with_name(f"{log.name}.{n}") for n in range(1, jsonl.DEFAULT_KEEP + 1))) if f.exists()
    ]
    assert len(kept) > 1 and len(jsonl.read(log)) == sum(
        len(f.read_text().splitlines()) for f in kept
    )  # a reader spans them


def test_the_effective_mode_is_computed_for_the_payload_not_the_machine_alone(walled, monkeypatch):
    """The same machine gives log to one call and enforce to the next."""
    pol = write_policy(walled)
    ok = cases(walled)["ifs"][0]
    off = cases(walled)["ifs"][0]
    off["tool_input"]["dangerouslyDisableSandbox"] = True
    assert [run(ok, pol)[0], run(off, pol)[0], run(ok, pol)[0]] == [0, gate.DENY, 0]


def test_mode_enforce_is_the_default_and_changes_nothing(walled):
    for text in (write_policy(walled, "enforce"), write_policy(walled, "enforce").replace('mode = "enforce"\n', "")):
        for name, (payload, _) in cases(walled).items():
            if name in ("loop", "ifs", "scratch"):
                continue  # a rule the wall makes redundant is allowed by the wall, with or without a guard in log mode
            assert run(payload, text)[0] == gate.DENY, name


# --- WS018 -------------------------------------------------------------------------------------------------------


def env_of(box) -> dict:
    return {"HOME": str(box / "home"), "WS_HOME": str(box / "home" / ".ws")}


def test_ws018_warns_when_log_cannot_apply(box, monkeypatch):
    env = env_of(box)
    write_policy(box)
    paths_file = box / "home" / ".ws" / "policy.toml"
    paths_file.write_text((box / "policy.toml").read_text())
    machine(monkeypatch, binary="not-bwrap")
    (check,) = doctor._mode_checks(env)
    assert (check.id, check.status) == ("WS018", doctor.WARN)
    assert "claims" in check.detail and "checkout-pin" in check.detail and "no-sandbox-binary" in check.detail
    assert str(box) not in check.detail and str(box) not in check.fix  # never a path
    machine(monkeypatch, system="win32")
    (check,) = doctor._mode_checks(env)
    assert check.status == doctor.WARN and "native-windows" in check.detail
    machine(monkeypatch)  # a machine where the wall can apply: ok, and it names the guards
    (check,) = doctor._mode_checks(env)
    assert check.status == doctor.OK and "claims" in check.detail
    paths_file.write_text(paths_file.read_text().replace('mode = "log"', 'mode = "enforce"'))  # no guard asks for log
    assert doctor._mode_checks(env) == []
    paths_file.write_text("[guards\n")  # a policy that does not load: WS012 says so, this says nothing
    assert doctor._mode_checks(env) == []


def test_ws018_reads_the_sandbox_switch_in_the_policy(box, monkeypatch):
    machine(monkeypatch)
    paths_file = box / "home" / ".ws" / "policy.toml"
    paths_file.write_text('[gate]\nsandbox = false\n[guards]\non = ["claims"]\n[guards.claims]\nmode = "log"\n')
    (check,) = doctor._mode_checks(env_of(box))
    assert check.status == doctor.WARN and "sandbox-off" in check.detail


def test_the_default_policy_warns_on_a_machine_with_no_wall(box, monkeypatch):
    """The template ships syntax-check in log, so a machine without the wall hears about it once."""
    machine(monkeypatch, binary="not-bwrap")
    (check,) = doctor._mode_checks(env_of(box))
    assert check.status == doctor.WARN and "syntax-check" in check.detail
    ids = [c.id for c in doctor.run_checks(osinfo.detect(), box / "profile.toml", env_of(box), network=False)]
    assert "WS018" in ids


def test_the_guard_modes_helper_names_who_asked(box):
    pol = policy.load(Path(write_policy(box)))
    assert guards.requested_log(pol) == ["checkout-pin", "claims"]
    assert guards.requested_log(policy.loads("")) == []
    assert claude_code.MATCHER == "*"
