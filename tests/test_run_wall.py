"""`ws run` puts a headless job inside the same wall `ws gate launch` puts a session in: a Claude Code job gets a settings
copy made for its folder (the project's locked paths, the home folder hidden, a walled class's launch file), the load
paths it holds are there while it runs, and a job the wall can't be put round is refused as a record, before it starts.

The runner is the fake `claude` of tests/test_run.py; the class account is stood in for by patching `real_user`."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_run import FIXTURES
from test_run import ws as run_ws  # noqa: F401  (a scratch WS_HOME with a fake claude on PATH)

from ws import cli, policy, run, sandbox, wallsettings
from ws.output import WsError


@pytest.fixture
def ws(run_ws):  # noqa: F811 (a fixture takes the other as its argument)
    return run_ws


def settings_of(ws) -> dict:
    (rec,) = ws.records()
    return json.loads((ws.home / "runs" / rec["run_id"] / "gate" / "settings.json").read_text(encoding="utf-8"))


def project(tmp_path, name: str = "app") -> Path:
    folder = tmp_path / "work" / name
    folder.mkdir(parents=True)
    return folder


def class_policy(ws, tmp_path, user: str = "") -> None:
    owner = f'os_user = "{user}"\n' if user else ""
    (ws.home / "policy.toml").write_text(
        f'[classes.work]\naccess = "walled"\n{owner}repos = ["{tmp_path}/work/**"]\nallowed_providers = ["anthropic"]\n',
        encoding="utf-8",
    )


def test_a_claude_job_gets_a_settings_copy_naming_the_projects_locked_paths(ws, tmp_path):
    app = project(tmp_path)
    ws.job(extra=f'cwd = "{app}"')
    assert cli.main(["run", "daily"]) == 0
    fs = settings_of(ws)["sandbox"]["filesystem"]
    for rel in sandbox.claude_denied():
        assert f"{app.resolve()}/{rel}" in fs["denyWrite"]
    assert str((tmp_path / "userhome").resolve()) in fs["denyRead"]  # the home folder is hidden from the job
    saw = ws.saw()  # and the runner is started with that copy
    assert saw["argv"][saw["argv"].index("--settings") + 1].endswith("/gate/settings.json")


def test_a_claude_jobs_load_paths_are_there_while_it_runs_and_gone_after(ws, tmp_path, monkeypatch):
    app = project(tmp_path)
    ws.job(extra=f'cwd = "{app}"')
    during = {}
    real = run._spawn

    def spy(argv, env, cwd, *a, **k):
        during.update({n: (cwd / n).exists() for n in (".claude", ".mcp.json", ".opencode")})
        return real(argv, env, cwd, *a, **k)

    monkeypatch.setattr(run, "_spawn", spy)
    assert cli.main(["run", "daily"]) == 0
    assert during == {".claude": True, ".mcp.json": True, ".opencode": False}
    assert not (app / ".claude").exists() and not (app / ".mcp.json").exists()


def test_a_job_with_no_cwd_runs_in_its_default_folder_with_the_same_wall(ws):
    ws.job()
    assert cli.main(["run", "daily"]) == 0
    (rec,) = ws.records()
    deny = settings_of(ws)["sandbox"]["filesystem"]["denyWrite"]
    assert f"{os.path.realpath(rec['cwd'])}/.claude" in deny
    assert not (Path(rec["cwd"]) / ".claude").exists()


def test_settings_without_a_sandbox_block_are_the_runs_as_they_were(ws, tmp_path):
    (ws.home / "policy.toml").write_text("[gate]\nsandbox = false\n", encoding="utf-8")
    app = project(tmp_path)
    ws.job(extra=f'cwd = "{app}"')
    assert cli.main(["run", "daily"]) == 0
    assert "sandbox" not in settings_of(ws)
    assert not (app / ".claude").exists()


def test_a_job_whose_folder_is_the_home_folder_or_above_it_is_refused_as_a_record(ws, tmp_path, capsys):
    for i, folder in enumerate((tmp_path / "userhome", tmp_path)):
        folder.mkdir(exist_ok=True)
        ws.job(name=f"j{i}", extra=f'cwd = "{folder}"')
        assert cli.main(["run", f"j{i}"]) == 3
    recs = ws.records()
    assert [r["stop_reason"] for r in recs] == ["refused:wall", "refused:wall"]
    assert all("project folder" in r["error"] for r in recs)
    assert not (ws.home / "fake-saw.json").exists()  # the runner never started


def test_dry_run_lists_the_wall_refusal(ws, tmp_path, capsys):
    (tmp_path / "userhome").mkdir()
    ws.job(extra=f'cwd = "{tmp_path / "userhome"}"')
    assert cli.main(["run", "daily", "--dry-run"]) == 3
    assert "project folder" in json.loads(capsys.readouterr().out)["would_refuse"]
    assert ws.records() == []


def test_a_class_account_runs_jobs_in_its_class_folders_only(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "real_user", lambda: "classacct")
    (ws.home / "policy.toml").write_text(
        f'[classes.work]\naccess = "walled"\nos_user = "classacct"\nrepos = ["{tmp_path}/work/**"]\n'
        f'allowed_providers = ["anthropic"]\n[classes.other]\naccess = "full"\nrepos = ["{tmp_path}/other/**"]\n'
        'allowed_providers = ["anthropic"]\n',
        encoding="utf-8",
    )
    (tmp_path / "other" / "repo").mkdir(parents=True)
    ws.job(extra=f'cwd = "{tmp_path}/other/repo"')
    assert cli.main(["run", "daily"]) == 3
    (rec,) = ws.records()
    assert rec["stop_reason"] == "refused:wall" and "class 'other'" in rec["error"] and "class 'work'" in rec["error"]
    assert "classacct" not in rec["error"]  # the class is named, not the account
    assert not (ws.home / "fake-saw.json").exists()


def test_a_job_with_no_cwd_is_in_no_class_for_the_class_account_check(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "real_user", lambda: "classacct")
    class_policy(ws, tmp_path, "classacct")
    ws.job()
    job = run.jobs.load("daily", os.environ)
    pol = run.policy.load()
    why = run._wall_problem(pol, job, run.adapters.get(job.runner), Path(tmp_path))
    assert why and "class 'work'" in why and "no class" in why and "classacct" not in why


def test_a_job_in_a_walled_class_folder_needs_the_class_account(ws, tmp_path, monkeypatch):
    app = project(tmp_path)
    monkeypatch.setattr(sandbox, "real_user", lambda: "someone-else")
    class_policy(ws, tmp_path, "classacct")
    ws.job(extra=f'cwd = "{app}"')
    assert cli.main(["run", "daily"]) == 3
    (rec,) = ws.records()
    assert rec["stop_reason"] == "refused:wall" and "class 'work'" in rec["error"] and "classacct" not in rec["error"]


def test_a_walled_class_job_needs_its_launch_file(ws, tmp_path, monkeypatch):
    app = project(tmp_path)
    monkeypatch.setattr(sandbox, "real_user", lambda: "classacct")
    monkeypatch.setattr(wallsettings, "launch_dir", lambda kind: str(tmp_path / "no-such-folder"))
    class_policy(ws, tmp_path, "classacct")
    ws.job(extra=f'cwd = "{app}"')
    assert cli.main(["run", "daily"]) == 3
    (rec,) = ws.records()
    assert rec["stop_reason"] == "refused:wall" and "launch file" in rec["error"]


def launch_file_in_place(ws, tmp_path, monkeypatch) -> None:
    """The class's launch file as `ws install --class` renders it; the test is not root, so root ownership is stood in for."""
    pol = policy.load(ws.home / "policy.toml")
    cls = pol.classes["work"]
    monkeypatch.setattr(wallsettings, "launch_dir", lambda kind: str(tmp_path / "launch"))
    doc = wallsettings.launch_settings(cls, str(tmp_path / "userhome"), "linux", pol)
    file = wallsettings.launch_file_path(cls.name, "linux")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(doc), encoding="utf-8")
    file.chmod(0o644)
    file.parent.chmod(0o755)
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: True)


def test_a_walled_class_job_gets_the_launch_file_in_its_settings_copy(ws, tmp_path, monkeypatch):
    app = project(tmp_path)
    monkeypatch.setattr(sandbox, "real_user", lambda: "classacct")
    class_policy(ws, tmp_path, "classacct")
    launch_file_in_place(ws, tmp_path, monkeypatch)
    ws.job(extra=f'cwd = "{app}"')
    assert cli.main(["run", "daily"]) == 0
    conf = settings_of(ws)
    assert conf["env"][wallsettings.WALL_MARKER] == "1"  # the class's launch file is merged in
    assert f"{app.resolve()}/.mcp.json" in conf["sandbox"]["filesystem"]["denyWrite"]


def test_a_walled_class_job_in_a_project_that_starts_an_mcp_server_is_refused(ws, tmp_path, monkeypatch):
    app = project(tmp_path)
    (app / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "y"}}}), encoding="utf-8")
    monkeypatch.setattr(sandbox, "real_user", lambda: "classacct")
    class_policy(ws, tmp_path, "classacct")
    launch_file_in_place(ws, tmp_path, monkeypatch)
    ws.job(extra=f'cwd = "{app}"')
    assert cli.main(["run", "daily"]) == 3
    (rec,) = ws.records()
    assert rec["stop_reason"] == "refused:wall" and ".mcp.json" in rec["error"]
    assert not (ws.home / "fake-saw.json").exists()


# ---- OpenCode jobs: the runner has no sandbox of its own, so ws puts it in its wall ----------------------------------------------

OPENCODE = f"#!{sys.executable}\nimport sys\nprint('9.9.9') if '--version' in sys.argv else sys.stdin.read()\n"


class Started:
    """What `_spawn` was asked to start, in place of starting it: the wall's command line is checked, not run."""

    def __init__(self):
        self.argv, self.env, self.cwd, self.pass_fds, self.wrap, self.db = [], {}, None, (), {}, None


@pytest.fixture
def started(ws, monkeypatch):
    """A fake `opencode` on PATH, the real wall builder told this host has bubblewrap that starts, and a `_spawn` that
    records what it was given and leaves the runner's session database where the runner would."""
    exe = ws.bin / "opencode"
    exe.write_text(OPENCODE, encoding="utf-8")
    exe.chmod(0o755)
    seen = Started()
    real_wrap = sandbox.wrap

    def wrap(argv, *a, **k):
        seen.wrap = {**k, "protect": a[2]}
        host = {"system": "linux", "machine": "x86_64", "which": lambda n: f"/usr/bin/{n}", "probe": lambda: True}
        return real_wrap(argv, *a, **host, **k)

    def spawn(argv, env, cwd, timeout_s, stdin_path, out_dir, grace_s=None, pass_fds=()):
        seen.argv, seen.env, seen.cwd, seen.pass_fds = argv, env, cwd, pass_fds
        (out_dir / "stdout.txt").write_text((FIXTURES / "opencode-events-ok.jsonl").read_text(encoding="utf-8"))
        (out_dir / "stderr.txt").write_text("")
        if env.get("XDG_DATA_HOME"):
            seen.db = Path(env["XDG_DATA_HOME"]) / "opencode" / "opencode.db"
            seen.db.parent.mkdir(parents=True, exist_ok=True)
            seen.db.write_text("")
        for fd in pass_fds:
            os.close(fd)
        return 0, False, False

    monkeypatch.setattr(sandbox, "wrap", wrap)
    monkeypatch.setattr(run, "_spawn", spawn)
    return seen


def opencode_job(ws, extra: str = "", model: str = "stub/m") -> None:
    (ws.home / "jobs" / "oc.toml").write_text(
        f'runner = "opencode"\nmodel = "{model}"\nprompt = "p"\ntimeout_s = 30\n{extra}\n[runner_options]\noffline = true\n',
        encoding="utf-8",
    )


def test_an_opencode_job_starts_inside_the_wall_with_the_project_locked(ws, started, tmp_path):
    app = project(tmp_path)
    opencode_job(ws, f'cwd = "{app}"')
    assert cli.main(["run", "oc"]) == 0
    argv = started.argv
    assert argv[0] == "bwrap" and argv[argv.index("--") + 1] == str(ws.bin / "opencode")
    real = str(app.resolve())
    assert argv[argv.index(f"{real}/.claude") - 1] == "--ro-bind"  # the placeholders are there for the wall to hold
    assert argv[argv.index(f"{real}/.opencode") - 1] == "--ro-bind"
    assert started.wrap["writable"][0] == real and argv[argv.index(real) - 1] == "--bind"
    assert len(started.pass_fds) == 1  # the seccomp program, handed to bubblewrap
    assert not (app / ".claude").exists() and not (app / ".opencode").exists()  # and gone once the run has ended
    assert not list((ws.home / "wall" / "projects").glob("*.json"))


def test_an_opencode_job_gets_private_homes_and_a_private_tmp_in_its_environment(ws, started, tmp_path):
    app = project(tmp_path)
    opencode_job(ws, f'cwd = "{app}"')
    assert cli.main(["run", "oc"]) == 0
    env = started.env
    base = (ws.home / "wall" / "opencode").resolve()
    key = sandbox.project_key(str(app.resolve()))
    assert env["XDG_DATA_HOME"] == f"{base}/data/{key}" and env["XDG_STATE_HOME"] == f"{base}/state"
    assert env["XDG_CACHE_HOME"] == f"{base}/cache/{key}"
    assert "ws-wall-" in env["TMPDIR"] and not Path(env["TMPDIR"]).exists()  # taken down with the wall
    assert (
        env["WS_GATED_SESSION"] == "1" and "OPENCODE_CONFIG_CONTENT" in env
    )  # the job's own variables are still there
    assert any(
        p.endswith("/gate") and "/runs/" in p for p in started.wrap["protect"]
    )  # the generated folder is read-only


def test_an_opencode_jobs_transcript_is_found_in_its_private_data_folder(ws, started, tmp_path):
    app = project(tmp_path)
    opencode_job(ws, f'cwd = "{app}"')
    assert cli.main(["run", "oc"]) == 0
    (rec,) = ws.records()
    assert started.db is not None and rec["transcript_path"] == str(started.db)
    assert str(started.db).startswith(str((ws.home / "wall").resolve()))


def test_an_opencode_job_with_no_cwd_may_write_its_default_folder(ws, started):
    opencode_job(ws)
    assert cli.main(["run", "oc"]) == 0
    (rec,) = ws.records()
    assert os.path.realpath(rec["cwd"]) == started.wrap["writable"][0]  # the first writable folder is the project


def test_an_opencode_job_is_refused_as_a_record_when_the_wall_cannot_be_built(ws, started, tmp_path, monkeypatch):
    def none(argv, *a, **k):
        raise sandbox.SandboxError("bubblewrap (bwrap) is not installed; install it")

    app = project(tmp_path)
    opencode_job(ws, f'cwd = "{app}"')
    monkeypatch.setattr(sandbox, "wrap", none)
    assert cli.main(["run", "oc"]) == 3
    (rec,) = ws.records()
    assert rec["status"] == "refused" and rec["stop_reason"] == "refused:wall" and "bwrap" in rec["error"]
    assert started.argv == []  # the runner never started
    assert not (app / ".claude").exists() and not list((ws.home / "wall" / "projects").glob("*.json"))


def test_an_opencode_job_runs_bare_when_the_policy_has_the_sandbox_off(ws, started, tmp_path):
    (ws.home / "policy.toml").write_text("[gate]\nsandbox = false\n", encoding="utf-8")
    app = project(tmp_path)
    opencode_job(ws, f'cwd = "{app}"')
    assert cli.main(["run", "oc"]) == 0
    assert started.argv[0] == str(ws.bin / "opencode") and started.pass_fds == () and "XDG_DATA_HOME" not in started.env


def test_an_opencode_job_whose_folder_is_the_home_folder_is_refused_as_a_record(ws, started, tmp_path):
    (tmp_path / "userhome").mkdir()
    opencode_job(ws, f'cwd = "{tmp_path / "userhome"}"')
    assert cli.main(["run", "oc"]) == 3
    (rec,) = ws.records()
    assert rec["stop_reason"] == "refused:wall" and "project folder" in rec["error"] and started.argv == []


def test_an_opencode_job_in_a_class_has_one_proxy_and_the_class_list(ws, started, tmp_path, monkeypatch):
    app = project(tmp_path)
    (ws.home / "policy.toml").write_text(
        f'[classes.work]\naccess = "walled"\nrepos = ["{tmp_path}/work/**"]\nallowed_providers = ["ollama"]\n'
        'local_only = true\nallowed_hosts = ["localhost:11434"]\n',
        encoding="utf-8",
    )
    opencode_job(ws, f'cwd = "{app}"', model="ollama/m")
    proxies = []
    real = sandbox.EgressProxy

    class Counting(real):
        def __init__(self, *a, **k):
            proxies.append(self)
            super().__init__(*a, **k)

    monkeypatch.setattr(sandbox, "EgressProxy", Counting)
    assert cli.main(["run", "oc"]) == 0
    assert len(proxies) == 1
    assert started.env["HTTPS_PROXY"].endswith(f"@127.0.0.1:{proxies[0].port}")
    assert started.wrap["net"] == (proxies[0].port, 11434)  # the proxy and the local model, nothing else


def test_an_opencode_job_in_a_class_with_the_sandbox_off_is_refused(ws, started, tmp_path):
    app = project(tmp_path)
    (ws.home / "policy.toml").write_text(
        f'[gate]\nsandbox = false\n[classes.work]\naccess = "walled"\nrepos = ["{tmp_path}/work/**"]\n'
        'allowed_providers = ["ollama"]\n',
        encoding="utf-8",
    )
    opencode_job(ws, f'cwd = "{app}"', model="ollama/m")
    assert cli.main(["run", "oc"]) == 3
    (rec,) = ws.records()
    assert rec["stop_reason"] == "refused:egress" and "sandbox is off" in rec["error"] and started.argv == []


def test_an_opencode_job_in_a_walled_class_with_a_project_mcp_server_is_refused(ws, started, tmp_path, monkeypatch):
    app = project(tmp_path)
    (app / "opencode.json").write_text(json.dumps({"mcp": {"x": {"command": ["y"]}}}), encoding="utf-8")
    monkeypatch.setattr(sandbox, "real_user", lambda: "classacct")
    class_policy(ws, tmp_path, "classacct")
    launch_file_in_place(ws, tmp_path, monkeypatch)
    opencode_job(ws, f'cwd = "{app}"', model="anthropic/m")
    assert cli.main(["run", "oc", "--dry-run"]) == 3  # listed before anything is reserved
    assert cli.main(["run", "oc"]) == 3
    (rec,) = ws.records()
    assert rec["stop_reason"] == "refused:wall" and "opencode.json" in rec["error"] and started.argv == []


def test_an_opencode_job_in_a_walled_class_hides_the_other_classes_folders(ws, started, tmp_path, monkeypatch):
    app = project(tmp_path)
    (tmp_path / "other" / "repo").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "real_user", lambda: "classacct")
    monkeypatch.setattr(wallsettings, "passwd", lambda name: SimpleNamespace(pw_dir=f"/home/{name}") if name else None)
    (ws.home / "policy.toml").write_text(
        f'[classes.work]\naccess = "walled"\nos_user = "classacct"\nrepos = ["{tmp_path}/work/**"]\n'
        f'allowed_providers = ["anthropic"]\n[classes.other]\naccess = "walled"\nos_user = "otheracct"\n'
        f'repos = ["{tmp_path}/other/**"]\nallowed_providers = ["anthropic"]\n',
        encoding="utf-8",
    )
    launch_file_in_place(ws, tmp_path, monkeypatch)
    opencode_job(ws, f'cwd = "{app}"', model="anthropic/m")
    assert cli.main(["run", "oc"]) == 0
    other = str((tmp_path / "other").resolve())
    assert started.argv[started.argv.index(other) - 1] == "--tmpfs"
    assert started.wrap["extra_hidden"] == [other]


def test_spawn_keeps_the_descriptors_it_is_given_open_in_the_runner_and_closes_them_here(tmp_path):
    r, w = os.pipe()
    out = tmp_path / "out"
    out.mkdir()
    code, _, _ = run._spawn(
        [sys.executable, "-c", "import os, sys; os.fstat(int(sys.argv[1])); print('open')", str(r)],
        {}, tmp_path, 20, None, out, None, (r,),
    )  # fmt: skip
    os.close(w)
    assert code == 0 and (out / "stdout.txt").read_text().strip() == "open"
    with pytest.raises(OSError):
        os.fstat(r)


def test_a_failing_clean_up_step_still_writes_the_record(ws, tmp_path, monkeypatch, capsys):
    app = project(tmp_path)
    ws.job(extra=f'cwd = "{app}"')

    def broken(home, folder):
        raise OSError("disk went away")

    monkeypatch.setattr(sandbox, "leave_project", broken)
    assert cli.main(["run", "daily"]) == 0
    (rec,) = ws.records()
    assert rec["status"] == "ok" and rec["ended_at"]
    assert "could not let go of the project (OSError)" in capsys.readouterr().err


def test_a_wall_whose_project_cannot_be_let_go_of_still_removes_its_tmp(ws, started, tmp_path, monkeypatch, capsys):
    app = project(tmp_path)
    opencode_job(ws, f'cwd = "{app}"')

    def broken(home, folder):
        raise OSError("disk went away")

    monkeypatch.setattr(sandbox, "leave_project", broken)
    assert cli.main(["run", "oc"]) == 0
    (rec,) = ws.records()
    assert rec["status"] == "ok"
    assert not Path(started.env["TMPDIR"]).exists()
    assert "could not let go of the project" in capsys.readouterr().err


def test_a_refusal_raised_while_the_wall_is_built_is_recorded_as_a_wall_refusal(ws, started, tmp_path, monkeypatch):
    app = project(tmp_path)
    opencode_job(ws, f'cwd = "{app}"')

    def refuse(*a, **k):
        raise WsError("refused", "the project holds a server the class does not allow", exit_code=3)

    monkeypatch.setattr(sandbox, "walled", refuse)
    assert cli.main(["run", "oc"]) == 3
    (rec,) = ws.records()
    assert rec["status"] == "refused" and rec["stop_reason"] == "refused:wall" and "does not allow" in rec["error"]
    assert started.argv == []


def test_spawn_closes_the_descriptors_when_the_prompt_file_cannot_be_opened(tmp_path):
    r, w = os.pipe()
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(OSError):
        run._spawn(["true"], {}, tmp_path, 20, tmp_path / "missing-prompt", out, None, (r,))
    os.close(w)
    with pytest.raises(OSError):
        os.fstat(r)


def test_a_claude_job_denies_a_call_instead_of_waiting_on_a_prompt(ws, tmp_path):
    """No one is there to answer: the job's settings carry denyInsteadOfPrompting, with the file blocks kept."""
    ws.job(extra=f'cwd = "{project(tmp_path)}"')
    assert cli.main(["run", "daily"]) == 0
    perms = settings_of(ws)["permissions"]
    assert perms["denyInsteadOfPrompting"] is True
    assert (
        perms["blockWritesOutsideWorkingDirectories"] is True and perms["blockReadsOutsideWorkingDirectories"] is True
    )
    assert settings_of(ws)["sandbox"]["credentials"]["envVars"]  # the tokens stay out of the job's shell commands


def test_a_job_with_the_sandbox_off_still_does_not_wait_on_a_prompt(ws, tmp_path):
    (ws.home / "policy.toml").write_text("[gate]\nsandbox = false\n", encoding="utf-8")
    ws.job(extra=f'cwd = "{project(tmp_path)}"')
    assert cli.main(["run", "daily"]) == 0
    assert settings_of(ws)["permissions"]["denyInsteadOfPrompting"] is True
