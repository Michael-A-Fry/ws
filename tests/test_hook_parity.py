"""docs/reference/hook-parity.toml (shown as hook-parity.md) maps each hook of a hand-built set to the ws rule or guard that does the same job. Each
`covered` row has a test here that sends the same sample call to the ws side, through the real hook entry, and checks that
it is stopped (and that its harmless neighbour is not). The last test keeps the page and these tests in step."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ws import cli, gate, guards, paths, policy
from ws.adapters import claude_code, opencode

ROOT0 = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("gen_hook_parity", ROOT0 / "scripts" / "gen_hook_parity.py")
gen = importlib.util.module_from_spec(_spec)
sys.modules["gen_hook_parity"] = gen
_spec.loader.exec_module(gen)

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "reference" / "hook-parity.md"
DATA = ROOT / "docs" / "reference" / "hook-parity.toml"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "w"))
    (tmp_path / "w").mkdir()
    return tmp_path


def call(tool: str, cwd="/", session="s1", **tool_input) -> str:
    return json.dumps({"tool_name": tool, "tool_input": tool_input, "session_id": session, "cwd": str(cwd)})


def hook(box, policy_text: str | None, payload: str, *protect: str) -> int:
    """The exit code of `ws gate check` for a payload under this policy (None: the built-in one)."""
    argv = ["--runner", "claude-code", *paths.pin_of().args()]
    if policy_text is not None:
        file = box / "w" / "policy.toml"
        file.write_text(policy_text)
        argv += ["--policy", str(file)]
    for p in protect:
        argv += ["--protect", p]
    return gate.run_hook("check", argv, payload)[0]


def verdict(pol: policy.Policy, payload: str, state: Path | None = None) -> str:
    reason = gate.check("claude-code", payload, pol, home=state)
    return "allow" if reason is None else "ask" if isinstance(reason, gate.Ask) else "deny"


# --- row 1, 2: shell commands and credential reads ---------------------------------------------------------------


def test_parity_irreversible_shell_commands(box):
    for command in ("rm -rf /usr", "git push --force origin main", "mkfs.ext4 /dev/sda1", "git push origin main -f"):
        assert hook(box, None, call("Bash", command=command)) == gate.DENY, command
    for command in ("ls -la", "git push origin work", "rm -rf build", "git status"):
        assert hook(box, None, call("Bash", command=command)) == 0, command


def test_parity_credential_reads(box):
    denied = (
        call("Bash", command="cat ~/.ssh/id_rsa"),
        call("Bash", command="cat ~/.aws/credentials | head -3"),
        call("Read", file_path="~/.aws/credentials"),
        call("Grep", pattern="BEGIN", path="~/.gnupg"),
        call("Glob", pattern="~/.ssh/*"),
    )
    for payload in denied:
        assert hook(box, None, payload) == gate.DENY, payload
    for payload in (call("Read", file_path="/etc/hostname"), call("Bash", command="cat README.md")):
        assert hook(box, None, payload) == 0, payload


# --- row 4: sealed folders ---------------------------------------------------------------------------------------


def test_parity_sealed_folders(box):
    text = '[gate]\ndeny_read = ["~/sealed/**"]\n'
    sealed = (
        call("Read", file_path="~/sealed/a.txt"),
        call("Bash", command="cat ~/sealed/a.txt"),
        call("Bash", command="grep -r x ~/sealed"),
        call("Grep", pattern="x", path="~/sealed"),
    )
    for payload in sealed:
        assert hook(box, text, payload) == gate.DENY, payload
    assert hook(box, text, call("Read", file_path="~/open/a.txt")) == 0
    # and the runner's own layers carry the folder too, with no hook at all
    pol = policy.loads(text)
    conf = json.loads(claude_code.generate(pol, ["ws"], box / "out", box / "w" / "policy.toml")["settings.json"])
    assert "Read(~/sealed/**)" in conf["permissions"]["deny"]
    assert any(p.endswith(("/sealed/**", "sealed")) for p in conf["sandbox"]["filesystem"]["denyRead"])


# --- row 5: the files that hold the guardrails -------------------------------------------------------------------


def test_parity_guardrail_files(box):
    policy_file = box / "w" / "policy.toml"
    out = box / "gate-out"
    protect = (str(out),)
    for payload in (
        call("Edit", file_path=str(policy_file), old_string="a", new_string="b"),
        call("Write", file_path=str(policy_file), content="[gate]\n"),
        call("Write", file_path=str(out / "settings.json"), content="{}"),
        call("Bash", command=f"echo x >> {policy_file}"),
        call("Bash", command=f"rm {policy_file}"),
    ):
        assert hook(box, "", payload, *protect) == gate.DENY, payload
    for payload in (
        call("Write", file_path=str(box / "project" / "notes.md"), content="x"),
        call("Bash", command="ls"),
    ):
        assert hook(box, "", payload, *protect) == 0, payload


# --- row 6: staging ----------------------------------------------------------------------------------------------


def test_parity_bulk_staging():
    pol = policy.loads('[guards]\non = ["bulk-staging"]\n')
    for command in ("git add -A", "git add .", "git commit -am wip", "git commit -a -m wip", "git add --all"):
        assert verdict(pol, call("Bash", command=command)) != "allow", command
    for command in ("git add src/a.py", "git commit -m 'git add -A'", "git add -u src/", "git status"):
        assert verdict(pol, call("Bash", command=command)) == "allow", command


# --- row 7: claims -----------------------------------------------------------------------------------------------


def git(cwd, *args) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def commit_with_readable_sha(repo, name: str, text: str) -> str:
    """Commit `text` to `name` and return its short sha, retrying with a new message until the sha has a digit and a
    letter a to f, which is all the claims guard reads as a commit (a hash is random, so one in ~100 would not)."""
    for n in range(50):
        (repo / name).write_text(text)
        git(repo, "add", name)
        git(repo, "commit", "-q", "--allow-empty", "-m", f"c{n}")
        sha = git(repo, "rev-parse", "--short=10", "HEAD")
        if any(c.isdigit() for c in sha) and any(c in "abcdef" for c in sha):
            return sha
        git(repo, "reset", "-q", "--hard", "HEAD~1")
    raise AssertionError("no commit with a readable sha in 50 tries")


@needs_git
def test_parity_claims(box):
    repo = box / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "seed").write_text("0")
    git(repo, "add", "seed")
    git(repo, "commit", "-q", "-m", "seed")
    on = commit_with_readable_sha(repo, "a", "1")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-q", "-b", "work")
    off = commit_with_readable_sha(repo, "a", "2")
    pol = policy.loads('[guards]\non = ["claims"]\n')

    def say(text: str) -> str:
        payload = call("SendMessage", cwd=repo, to="lead", message=text)
        return verdict(pol, payload)

    assert say(f"merged {off} into main") == "deny"
    assert say(f"{off} is on main") == "deny"
    assert say(f"merged {on} into main") == "allow"
    assert say(f"committed {off} on a branch, not merged") == "allow"


@needs_git
@pytest.mark.parametrize("how", ["timeout", "budget", "no-start"])
def test_claims_deny_when_git_cannot_answer_in_time(box, monkeypatch, how):
    """The WSL2 red: a slow git used up the claims budget and the guard let the message through as "can't tell". The
    gate fails closed (ADR 0017), so a claim it can't reach git to check is denied, whatever stopped git."""
    from ws.guards import claims

    repo = box / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "seed").write_text("0")
    git(repo, "add", "seed")
    git(repo, "commit", "-q", "-m", "seed")
    on = commit_with_readable_sha(repo, "a", "1")  # the guard reads only an id with a digit and a letter a to f
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    pol = policy.loads('[guards]\non = ["claims"]\n')

    def say(text: str) -> str:
        return verdict(pol, call("SendMessage", cwd=repo, to="lead", message=text))

    def broken(argv, **k):
        if how == "timeout":
            raise subprocess.TimeoutExpired(argv, k["timeout"])
        raise PermissionError("git would not start")

    assert say(f"merged {on} into main") == "allow"  # git answers: a true claim
    real_budget = claims.Budget
    with monkeypatch.context() as m:
        if how == "budget":
            m.setattr(claims, "Budget", lambda: real_budget(0.0))
        else:
            m.setattr(claims.subprocess, "run", broken)
        assert say(f"merged {on} into main") == "deny"  # the same claim, git not reachable in time
        assert say(f"{on} is on main") == "deny"
        assert say("the ed25519 key is on main") == "allow"  # the true negatives never needed git
        assert say(f"committed {on} on a branch, not merged") == "allow"


# --- row 8: session context ---------------------------------------------------------------------------------------


@needs_git
def test_parity_session_context(box):
    repo = box / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    pol = policy.loads('[guards]\non = ["session-context"]\n[guards.session-context]\nwarn_api_key = false\n')
    start = json.dumps({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(repo)})
    code, out, _ = gate.event("claude-code", start, pol, home=box / "state")
    assert code == 0 and "STATE.md" not in out
    (repo / "STATE.md").write_text("where it is\n")
    code, out, _ = gate.event("claude-code", start, pol, home=box / "state")
    assert code == 0 and "STATE.md" in out


# --- row 9: skill gate -------------------------------------------------------------------------------------------


def test_parity_skill_gate(box):
    pol = policy.loads(
        '[guards]\non = ["skill-gate"]\n[[guards.skill-gate]]\nskill = "taste-gate"\npaths = ["**/*.css"]\n'
    )
    state = box / "state"
    css = call("Write", cwd=box, file_path=str(box / "site" / "a.css"), content="a {}")
    assert verdict(pol, css, state) == "ask"  # the skill hasn't been loaded: a person may let it through once
    assert verdict(pol, call("Write", cwd=box, file_path=str(box / "site" / "a.html"), content="x"), state) == "allow"
    assert verdict(pol, call("Skill", cwd=box, skill="taste-gate"), state) == "allow"  # it is loaded now
    assert verdict(pol, css, state) == "allow"


# --- row 10: spawn cap -------------------------------------------------------------------------------------------


def test_parity_spawn_cap(box):
    pol = policy.loads('[guards]\non = ["spawn-cap"]\n[guards.spawn-cap]\nmax_agents = 1\n')
    state = box / "state"
    agent = {"subagent_type": "worker", "prompt": "go", "run_in_background": True}
    assert verdict(pol, call("Agent", cwd=box, **agent), state) == "allow"
    started = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "tool_input": agent,
            "session_id": "s1",
            "cwd": str(box),
        }
    )
    gate.event("claude-code", started, pol, home=state)  # the first agent is running
    assert verdict(pol, call("Agent", cwd=box, **agent), state) == "deny"


# --- row 12: tests are not weakened -------------------------------------------------------------------------------


def test_parity_test_weakening(box):
    (box / "tests").mkdir()
    test_file = box / "tests" / "test_x.py"
    test_file.write_text("import pytest\n\n\ndef test_a():\n    assert 1 + 1 == 2\n")
    pol = policy.loads('[guards]\non = ["test-weakening"]\n')
    skip = call(
        "Edit",
        cwd=box,
        file_path=str(test_file),
        old_string="def test_a():",
        new_string="@pytest.mark.skip\ndef test_a():",
    )
    assert verdict(pol, skip) == "ask"  # a person may allow it once
    more = call(
        "Edit",
        cwd=box,
        file_path=str(test_file),
        old_string="    assert 1 + 1 == 2\n",
        new_string="    assert 1 + 1 == 2\n    assert 2 + 2 == 4\n",
    )
    assert verdict(pol, more) == "allow"


# --- row 15: the phone alert -------------------------------------------------------------------------------------


def test_parity_phone_alert(box, monkeypatch, capsys):
    pol = policy.default_policy()
    conf = json.loads(claude_code.generate(pol, ["ws"], box / "out", box / "w" / "policy.toml")["settings.json"])
    for name in ("Notification", "Stop"):
        commands = [h["command"] for g in conf["hooks"][name] for h in g["hooks"]]
        assert any("notify alert --runner claude-code" in c and c.endswith("|| true") for c in commands), name
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "Notification", "message": "x"})))
    assert cli.main(["notify", "alert", "--runner", "claude-code", *paths.pin_of().args()]) == 0  # no channel: silent


# --- launch from the main checkout, and config-guard ---------------------------------------------------------------


@needs_git
def test_parity_launch_from_main(box, monkeypatch):
    from test_guard_launch_config import real_repos

    main, wt = real_repos(box)
    on = '[guards]\non = ["guard-launch"]\n'

    def go(where, *a, **k):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(where))  # the folder the session started in
        return hook(box, on, call(*a, cwd=where, **k))

    assert go(wt, "Workflow", name="w") == 2  # a workflow from a linked worktree
    assert go(wt, "Agent", prompt="p", isolation="worktree") == 2
    assert go(main, "Workflow", name="w") == 0
    assert go(wt, "Agent", prompt="p") == 0  # not isolated: allowed
    (box / "plain").mkdir()
    (box / "plain" / ".git").mkdir()  # a planted .git folder is not a repository
    assert go(box / "plain", "Workflow", name="w") == 2


def test_parity_config_guard(box, monkeypatch):
    (box / "g").mkdir()
    (box / "mine").mkdir()
    on = f'[guards]\non = ["config-guard"]\n[guards.config-guard]\npaths = ["{box / "g"}"]\nowners = ["{box / "mine"}"]\n'
    edit = {"file_path": str(box / "g" / "settings.json"), "content": "x"}
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(box))
    assert hook(box, on, call("Write", cwd=box, **edit)) == 2  # not the owner's folder
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(box / "mine"))
    assert hook(box, on, call("Write", cwd=box / "mine", **edit)) == 0
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(box))
    assert hook(box, on, call("Write", cwd=box / "mine", **edit)) == 2  # the call moved itself into the owner's folder


@needs_git
def test_parity_commit_wall(box, monkeypatch):
    """The commit wall refuses a staged line or a message that matches a pattern the person supplied, and a line the
    exceptions file names goes through (the fuller cases are in tests/test_commit_wall.py)."""
    from ws import commit_wall

    monkeypatch.setenv("WS_HOME", str(box / "w"))
    plant = "zz" + "planted" + "-name"
    folder = commit_wall.folder()
    folder.mkdir(parents=True)
    (folder / "patterns.toml").write_text(f'[[pattern]]\nname = "plant"\nterms = ["{plant}"]\n')
    repo = box / "wallrepo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text(f"a line with {plant}\n")
    git(repo, "add", "f.txt")
    assert cli.main(["commit-wall", "check", "--repo", str(repo)]) == 1
    (folder / "exceptions.toml").write_text('[[allow]]\npattern = "plant"\npath = "f.txt"\n')
    assert cli.main(["commit-wall", "check", "--repo", str(repo)]) == 0
    msg = box / "msg"
    msg.write_text(f"subject {plant}\n")
    assert cli.main(["commit-wall", "check", "--repo", str(repo), "--stage", "commit-msg", str(msg)]) == 1


# --- wave 2b: force-push, env files, scheduled branch moves, syntax -----------------------------------------------


PUSH_DENIED = (
    "git push --force origin main",
    "git push origin :trunk",
    "git push origin ':trunk'",
    'git push origin ":trunk"',
    "git push origin refs/heads/x :trunk",
    "git push -d origin trunk",
    "git push origin -d trunk",
    "git push origin trunk -d",
    "git push --delete origin trunk",
    "git push origin --delete trunk",
    "git push -qd origin trunk",  # combined flags: -d among others
    "git push -dq origin trunk",
    "git push --prune origin 'refs/heads/*:refs/heads/*'",
    "git push --mirror origin",
    "git -C repo push origin :trunk",
    "git -c a=b push origin :trunk",
    "git --no-pager push -d origin trunk",
    "/usr/bin/git push origin :trunk",
    "command git push origin :trunk",
    "env git push origin :trunk",
    "sudo git push origin :trunk",
    "bash -c 'git push origin :trunk'",
    "cd /tmp && git push origin :trunk",
    "true; git push origin :trunk",
    "git push origin -d trunk 2>&1",
)
PUSH_ALLOWED = (
    "git push origin trunk",
    "git push origin HEAD:work",
    "git push origin trunk:trunk",
    "git push -u origin feature",
    "git push --dry-run origin trunk",
    "git push origin v1.0 --tags",
    "git push origin -o ci.skip trunk",
    "git push origin --follow-tags trunk",
    "git branch -d topic",
    'git push origin "refs/heads/a:refs/heads/b"',
    'git push origin trunk; echo ":done"',
    "git push --set-upstream origin dev",
    "git push origin feature-d",
    "git push origin -- trunk",
    'git push origin trunk -o "msg :x"',
    "git push origin trunk 2>&1 | grep -v ' -d '",
    "git push origin trunk && git branch -d trunk",
    "git push origin trunk -o 'ci.message=fix :x'",
    "git push origin -o -d trunk",  # the value of -o is not a flag
    "git push --push-option -d origin trunk",
    "git status",
)


def test_parity_force_push_main(box):
    """Every forcing or deleting push spelling is refused, whatever it pushes to; an ordinary push is free. A remote ref
    deleted through the host's own API (`gh api -X DELETE`) is a network call this list doesn't read."""
    for command in PUSH_DENIED:
        assert hook(box, None, call("Bash", command=command)) == gate.DENY, command
    for command in PUSH_ALLOWED:
        assert hook(box, None, call("Bash", command=command)) == 0, command


ENV_DENIED = (
    "echo KEY=1 > .env",
    'echo KEY=1 > ".env"',
    "echo KEY=1 > '.env'",
    "echo KEY=1 > .env 2>/dev/null",  # a trailing redirect
    "echo KEY=1 >.env; ls",
    "echo KEY=1 > ./.env",
    "echo KEY=1 >> app/.env",
    "echo KEY=1 > /srv/app/.env",
    "echo KEY=1 > $PWD/.env",
    "F=.env; echo KEY=1 > $F",  # a variable set on the line
    'echo KEY=1 > .e""nv',
    "echo KEY=1 1> .env",
    "echo KEY=1 &> .env",
    "echo KEY=1 >| .env",
    ">.env",
    "echo x 2>&1 >.env",
    "{ echo x; } > .env",
    "(echo x) > .env",
    "exec 3> .env",
    "cat > .env <<'X'\nKEY=1\nX",  # a heredoc
    "cat > .env <<X\nKEY=1\nX",
    "cat <<X > .env\nKEY=1\nX",
    "tee .env <<X\nKEY=1\nX",
    "printf x | tee .env",
    "printf x | tee -a .env",
    "printf x | tee .env > /dev/null",
    "printf x | sudo tee .env",
    'printf x | tee ".env"',
    "printf x | tee .env.local >/dev/null",
    "printf x | tee .env.production",
    "printf x | tee .env out.txt",  # a second operand
    "printf x | tee out.txt .env",
    "cp /tmp/x .env",
    "mv /tmp/x .env",
    "sed -i s/a/b/ .env",
    "dd of=.env",
    "bash -c 'echo x > .env'",
    "echo x > .env.test",
    "echo x > .envrc",
    "echo x > $UNSET_NAME",  # a name the reader cannot know could be any file
    'echo x > "$UNSET_DIR/$UNSET_NAME"',
    "echo x > .e*",  # the shell expands a glob or a brace into a name the reader never sees
    "echo x > .en?",
    "cp /tmp/x .e[n]v",
    "cp x .{y,env}",  # the reader expands a brace itself; the last name is the one written
    "cp ../other/.env .",  # a copy into a folder writes folder/<the source's name>
    "cp /tmp/.env ./",
    "cp ../o/.env app/",
    "cp -t . ../o/.env",
    "cp -t app ../o/.env",
    "mv ../o/.env .",
    "rm .env",  # new: removing it is refused too, as `rm` writes its operands
    "sort -o .env in.txt",
    "sort --output=.env in.txt",
    "awk -i inplace '{print}' .env",
    "tar xf a.tar .env",
    "split in.txt .env.",
    "ed .env",
)
ENV_ALLOWED = (
    "cat .env.example",
    "echo KEY=1 > .env.example",
    "echo KEY=1 > .env.sample",
    "grep KEY .env",
    "grep KEY .env > out.txt",
    "make 2>&1 | grep .env",
    "ls > files.txt && cat .env",
    "source .env",
    "diff .env .env.example > d.txt",
    "echo x > notes.env",
    "cat x | tee build.log",
    "cat .env | tee copy.txt",
    "cat x | tee teeth.envy",
    "printf x | tee log.txt | grep .env",
    "make 2>&1 | tee build.log | grep -c .env",
    "python app.py 2>/dev/null --config prod.env",
    "docker compose --env-file .env up 2>&1",
    "echo x > .env2",
    "ls -a",
    "grep KEY notes.txt > out.txt",
)


def test_parity_env_file_write(box):
    """A write to `.env`, or to `.env.NAME` other than a template's, is refused: read from the lexed redirect targets and
    the operands of a writing command by file name, so quoting, a heredoc, a variable on the line, a trailing redirect or
    a second `tee` operand change nothing. `notes.env` and an option's value `prod.env` are not writes to it. Not covered:
    an interpreter that opens the file itself (`python3 -c "open('.env', 'w')"`)."""
    # The default policy also refuses to READ `.env` (deny_read), so a command like `grep KEY .env` is refused for that
    # reason, not this one. The free cases are about the write rule alone: they run under a policy with no read rules.
    no_read_rules = "[gate]\ndeny_read = []\n"
    for command in ENV_DENIED:
        assert hook(box, None, call("Bash", command=command)) == gate.DENY, command
        assert hook(box, no_read_rules, call("Bash", command=command)) == gate.DENY, command  # the write rule alone
    for command in ENV_ALLOWED:
        assert hook(box, no_read_rules, call("Bash", command=command)) == 0, command
    # and the default policy still refuses the read of the secret file, by its own rule. It is passed as text: a policy
    # file written by the calls above is what a call with no policy would read from this test's home.
    assert hook(box, policy.default_toml(), call("Bash", command="grep KEY .env")) == gate.DENY


@needs_git
def test_parity_scheduled_branch_move(box, monkeypatch):
    """The checkout-pin guard holds a main checkout that has linked worktrees in place, whoever runs the command (a
    timer's job included), so a job can't move its branch."""
    from test_guard_launch_config import real_repos

    main, _ = real_repos(box)
    on = '[guards]\non = ["checkout-pin"]\n'
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(main))
    for command in ("git checkout side", "git switch -c other", "git reset --hard HEAD~1", "git pull"):
        assert hook(box, on, call("Bash", cwd=main, command=command)) == gate.DENY, command
    for command in ("git status", "git log -1", "git add f"):
        assert hook(box, on, call("Bash", cwd=main, command=command)) == 0, command


def test_parity_syntax_check(box):
    pol = policy.loads('[guards]\non = ["syntax-check"]\n')
    conf = json.loads(claude_code.generate(pol, ["ws"], box / "out", box / "w" / "policy.toml")["settings.json"])
    pre = conf["hooks"].get("PreToolUse", [])
    assert not any("syntax-check" in h["command"] for g in pre for h in g["hooks"])
    post = [h["command"] for g in conf["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert post  # only after the call ran, so a finding reaches the model and can't block the call
    (box / "bad.py").write_text("def f(:\n")
    (box / "good.py").write_text("def f():\n    return 1\n")
    guard = guards.get("syntax-check")
    ctx = guards.Context("claude-code", "post", "s1", str(box), {})

    def judged(name):
        raw = json.loads(call("Write", cwd=box, file_path=str(box / name), content="x"))
        return guard.judge(claude_code.parse(raw), guard.config({}, "w"), ctx)

    found = judged("bad.py")
    assert found is not None and found.action == "deny"
    assert judged("good.py") is None


# --- the page and these tests agree ------------------------------------------------------------------------------


def table() -> list[dict]:
    return gen.load()


def test_every_hook_row_is_covered_by_a_test_or_says_why_not():
    rows = table()
    assert len({r["id"] for r in rows}) == len(rows), "duplicate row id"
    mine = {n for n in globals() if n.startswith("test_parity_")}
    named = set()
    for r in rows:
        assert r["what"] and r["ws"], r["id"]
        assert r["status"] in gen.STATUSES and r["fail"] in gen.FAILS, r["id"]
        if r["status"] == "covered":
            for name in r["test"].split(", "):
                assert name in mine, f"row {r['id']} names {name}, which is not in tests/test_hook_parity.py"
                named.add(name)
        else:
            assert r["test"] == "none", r["id"]
            if r["status"] == "planned":
                assert r.get("wave") in ("1", "2a", "2b", "3"), r["id"]
            if r["status"] == "dropped":
                assert "reason" in r["ws"] or "no " in r["ws"], r["id"]
    assert named == mine, f"tests with no row on the page: {sorted(mine - named)}"


def test_a_hook_in_an_inventory_with_no_row_is_found():
    assert gen.missing(["dummy-hook"]) == ["dummy-hook"]  # planted: a hook the table does not know is red
    assert gen.missing([table()[0]["id"]]) == []
    assert gen.missing([], rows=[]) == []


def test_the_page_is_what_the_script_writes_from_the_table():
    assert PAGE.read_text(encoding="utf-8") == gen.render()
    assert PAGE.read_text(encoding="utf-8").startswith("<!-- GENERATED")


def test_the_page_names_no_person_host_or_private_setup():
    text = PAGE.read_text(encoding="utf-8") + DATA.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text) and "@" not in text
    assert "/home/" not in text and "/Users/" not in text


def test_generated_runner_configs_say_do_not_edit(box):
    pol = policy.default_policy()
    made = claude_code.generate(pol, ["ws"], box / "out", box / "w" / "policy.toml")
    made.update(opencode.generate(pol, ["ws"], box / "out", box / "w" / "policy.toml"))
    for name, text in made.items():
        if name == ".gitignore":
            continue
        assert "DO NOT EDIT" in text.splitlines()[0] or "DO NOT EDIT" in json.loads(text).get("_generated", ""), name
