"""`ws walls`: init, edit and check. Every named case of `ws walls check` has a mutation test here: the same host passes,
the case's own setting is removed, and that case fails. Fixtures use generic names (alpha, beta, ws-alpha) built at run time
under tmp_path; nothing reads the real HOME, WS_HOME, /etc or the network, and no test runs sudo or creates an account."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ws import cli, doctor, gate, install, osinfo, paths, policy, sandbox, userconfig, walls, wallsettings
from ws.adapters import claude_code, opencode

NOW = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
NAMES = [
    "inbound-refused",
    "sealed-read-refused",
    "class-file-not-seen-by-other-class",
    "hooks-under-bypass",
    "hooks-under-auto",
    "project-load-check",
    "hooks-not-disabled",
    "sandbox-only-narrows",
    "per-repo-settings",
    "otel-off",
    "owner-and-review-date",
    "messaging-denied",
    "class-file-root-owned",
    "launch-write-block-and-token-deny",
    "remote-and-connectors-off",
    "headless-job-in-wall",
    "local-model-in-wall",
    "allowed-sender-delivered",
    "bodies-in-class-store",
    "walled-sends-fixed-kinds",
    "provider-allowlist",
    "class-push-refused",
    "memory-notes-refused",
    "walled-card-no-counts",
    "tracer-stays-in-class",
]
FLOW_NAMES = NAMES[17:]  # the cases about what leaves a walled class through ws's own stores: tests/test_walls_flows.py

POLICY = """
[classes.alpha]
access = "walled"
os_user = "ws-alpha"
repos = ["{box}/work/alpha/**"]
allowed_providers = ["anthropic"]
owner = "alpha lead"
review_date = 2027-06-01

[classes.beta]
access = "walled"
os_user = "ws-beta"
repos = ["{box}/work/beta/**"]
allowed_providers = ["anthropic"]
owner = "beta lead"
review_date = 2027-06-01
"""


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    for name in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "WS_SANDBOXED", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(wallsettings.WALL_MARKER, raising=False)
    (tmp_path / "wshome").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(walls, "_now", lambda: NOW)
    return tmp_path


def policy_file() -> Path:
    return policy.policy_path(dict(os.environ))


def kind() -> str:
    return osinfo.detect().kind


class Host:
    """A set-up host, stood in for: two classes with their accounts and class files, the generated settings, a clock."""

    def __init__(self, box: Path, monkeypatch):
        self.box, self.mp = box, monkeypatch
        self.text = POLICY.format(box=box)
        policy_file().write_text(self.text, encoding="utf-8")
        monkeypatch.setattr(
            wallsettings, "passwd", lambda name: types.SimpleNamespace(pw_dir=f"/home/{name}") if name else None
        )
        monkeypatch.setattr(wallsettings, "_is_root", lambda st: True)  # the test is not root: stand root in
        self.settings = paths.state_dir() / "gate" / "claude-code" / "settings.json"
        self.settings.parent.mkdir(parents=True)
        self.regenerate()
        self.pol = policy.load(policy_file())
        for name in ("alpha", "beta"):
            self.place(name)

    def regenerate(self) -> None:
        pol = policy.loads(self.text)
        out = self.settings.parent
        self.settings.write_text(claude_code.generate(pol, ["ws"], out, policy_file())["settings.json"])

    def place(self, name: str) -> Path:
        pol = policy.load(policy_file())
        cls = pol.classes[name]
        conf = wallsettings.launch_settings(cls, wallsettings.class_home(cls, kind()), kind(), pol)
        f = wallsettings.launch_file_path(name, kind())
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(conf), encoding="utf-8")
        f.chmod(0o644)
        f.parent.chmod(0o755)
        managed = wallsettings.class_file_path(
            name, kind()
        )  # the managed-settings file a bare `claude` run is bound by
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_text(
            wallsettings.class_file_text(cls, wallsettings.class_home(cls, kind()), kind()), encoding="utf-8"
        )
        managed.chmod(0o644)
        managed.parent.chmod(0o755)
        return f

    def edit_file(self, name: str, change, managed: bool = False) -> None:
        f = wallsettings.class_file_path(name, kind()) if managed else wallsettings.launch_file_path(name, kind())
        doc = json.loads(f.read_text())
        change(doc)
        f.write_text(json.dumps(doc), encoding="utf-8")

    def edit_policy(self, old: str, new: str) -> None:
        assert old in self.text
        self.text = self.text.replace(old, new, 1)
        policy_file().write_text(self.text, encoding="utf-8")

    def edit_settings(self, change) -> None:
        doc = json.loads(self.settings.read_text())
        change(doc)
        self.settings.write_text(json.dumps(doc))


@pytest.fixture
def host(box, monkeypatch):
    return Host(box, monkeypatch)


def results() -> dict[str, dict]:
    ctx, _, _ = walls._context(dict(os.environ), walls._now())
    return {c["name"]: c for c in walls.run_cases(ctx)}


def run(capsys, *argv):
    code = cli.main(["walls", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# ---- the registry --------------------------------------------------------------------------------------------------------


def test_the_cases_are_a_plain_table_of_name_check_and_setting_that_a_later_task_can_add_to():
    assert [c.name for c in walls.CASES] == NAMES
    assert all(callable(c.check) and c.setting and isinstance(c.name, str) for c in walls.CASES)
    first = walls.CASES[0]
    assert tuple(first)[:3] == (first.name, first.check, first.setting)  # (name, check function, setting it depends on)
    added = [*walls.CASES, walls.Case("extra", lambda ctx: walls.Outcome(True, "fine"), "a setting", host=False)]
    ctx, _, _ = walls._context(dict(os.environ), walls._now())
    assert walls.run_cases(ctx, added)[-1]["status"] == "pass"


def test_every_case_holds_on_a_host_that_is_set_up(host, capsys):
    got = results()
    assert {n: c["status"] for n, c in got.items()} == dict.fromkeys(NAMES, "pass"), {
        n: c["detail"] for n, c in got.items() if c["status"] != "pass"
    }
    code, out, _ = run(capsys, "check")
    assert code == 0 and "fail" not in out and "skip" not in out


def test_check_json_is_one_envelope_with_every_case(host, capsys):
    code, out, _ = run(capsys, "check", "--json")
    env = json.loads(out)
    assert code == 0 and env["kind"] == "walls.check" and env["status"] == "ok"
    assert [c["name"] for c in env["data"]["cases"]] == NAMES and env["data"]["set_up"] is True


# ---- a mutation for each named case: remove its setting and the case fails ------------------------------------------------


def drop(path: tuple[str, ...]):
    def change(doc: dict) -> None:
        at = doc
        for k in path[:-1]:
            at = at[k]
        at.pop(path[-1])

    return change


def without_rule(rule: str):
    return lambda doc: doc["permissions"]["deny"].remove(rule)


def stray_folders_are_beta(h) -> None:
    real = policy.class_of
    h.mp.setattr(
        policy,
        "class_of",
        lambda pol, path: pol.classes["beta"] if real(pol, path) is policy.UNCLASSIFIED else real(pol, path),
    )


def settings_without_hook(doc: dict) -> None:
    doc["hooks"]["PreToolUse"] = []


def other_hook(doc: dict) -> None:
    for g in doc["hooks"]["PreToolUse"]:
        g["matcher"] = "Bash|Read"  # the old list: a tool it does not name is not gated


def without_token(name: str):
    def change(doc: dict) -> None:
        cred = doc["sandbox"]["credentials"]
        cred["envVars"] = [e for e in cred["envVars"] if e["name"] != name]

    return change


def a_local_class(h, then) -> None:
    """Add a local_only class with its files in place (the host has none), then apply `then`."""
    h.edit_policy(
        "[classes.beta]",
        f'[classes.gamma]\naccess = "walled"\nos_user = "ws-gamma"\nrepos = ["{h.box}/work/gamma/**"]\n'
        'local_only = true\nallowed_hosts = ["localhost:11434"]\nowner = "gamma lead"\nreview_date = 2027-06-01\n\n'
        "[classes.beta]",
    )
    h.place("gamma")
    assert results()["local-model-in-wall"]["status"] == "pass"
    then(h)


MUTATIONS = [
    (
        "inbound-refused",
        "the file stops refusing cross-session messages",
        lambda h: h.edit_file("alpha", drop(("crossSessionInbound",))),
    ),
    (
        "inbound-refused",
        "the policy lets a named session in and the probe is on the list",
        lambda h: h.edit_policy(
            'owner = "alpha lead"', 'owner = "alpha lead"\ninbound = "allowlist"\ninbound_allow = ["ws-walls-probe"]'
        ),
    ),
    ("sealed-read-refused", "no Read rule for the home", lambda h: h.edit_file("alpha", without_rule("Read(~/**)"))),
    (
        "sealed-read-refused",
        "no Edit rule for the other class's repos",
        lambda h: h.edit_file("alpha", lambda d: d["permissions"]["deny"].remove(f"Edit(/{h.box}/work/beta/**)")),
    ),
    (
        "class-file-not-seen-by-other-class",
        "beta's file carries alpha's folder",
        lambda h: h.edit_file(
            "beta", lambda d: d["sandbox"]["filesystem"].setdefault("allowWrite", []).append(f"{h.box}/work/alpha")
        ),
    ),
    (
        "class-file-not-seen-by-other-class",
        "beta's launch file stops denying reads of alpha's folders",
        lambda h: h.edit_file("beta", drop(("sandbox", "filesystem", "denyRead"))),
    ),
    (
        "class-file-not-seen-by-other-class",
        "the launch's lookup puts a folder of beta in alpha",
        lambda h: h.mp.setattr(policy, "class_of", lambda pol, path: pol.classes["alpha"]),
    ),
    (
        "class-file-not-seen-by-other-class",
        "the launch's lookup gives a folder in no class a class's file",
        lambda h: stray_folders_are_beta(h),
    ),
    (
        "hooks-under-bypass",
        "the managed-settings file stops switching bypass off",
        lambda h: h.edit_file("alpha", drop(("permissions", "disableBypassPermissionsMode")), managed=True),
    ),
    (
        "hooks-under-bypass",
        "the gate hook is not in the generated settings",
        lambda h: h.edit_settings(settings_without_hook),
    ),
    (
        "hooks-under-bypass",
        "the preflight denies every call, so the probe's own rule is never what denies it",
        lambda h: h.mp.setattr(claude_code, "preflight", lambda payload: "denied before any rule"),
    ),
    (
        "hooks-under-auto",
        "the preflight denies every call, so the probe's own rule is never what denies it",
        lambda h: h.mp.setattr(claude_code, "preflight", lambda payload: "denied before any rule"),
    ),
    (
        "hooks-under-auto",
        "the gate hook is not in the generated settings",
        lambda h: h.edit_settings(settings_without_hook),
    ),
    ("hooks-under-auto", "the hook matches a list, not every tool", lambda h: h.edit_settings(other_hook)),
    (
        "project-load-check",
        "the file's marker is gone",
        lambda h: h.edit_file("alpha", drop(("env", wallsettings.WALL_MARKER))),
    ),
    (
        "project-load-check",
        "the project rule finds nothing",
        lambda h: h.mp.setattr(wallsettings, "project_problem", lambda starts, home: None),
    ),
    (
        "hooks-not-disabled",
        "the class file stops setting disableAllHooks false",
        lambda h: h.edit_file("alpha", drop(("disableAllHooks",))),
    ),
    (
        "hooks-not-disabled",
        "the class file lets hooks be switched off",
        lambda h: h.edit_file("beta", lambda d: d.update(disableAllHooks=True)),
    ),
    (
        "hooks-not-disabled",
        "the project rule finds nothing",
        lambda h: h.mp.setattr(wallsettings, "project_problem", lambda starts, home: None),
    ),
    (
        "hooks-not-disabled",
        "the project rule no longer sees a plugin or a switched-off hook",
        lambda h: h.mp.setattr(
            wallsettings,
            "_adds",
            lambda rel, conf: None if "enabledPlugins" in conf or "disableAllHooks" in conf else ("x", "y"),
        ),
    ),
    (
        "sandbox-only-narrows",
        "the class file stops setting enableWeakerNestedSandbox false",
        lambda h: h.edit_file("alpha", drop(("sandbox", "enableWeakerNestedSandbox"))),
    ),
    (
        "sandbox-only-narrows",
        "the class file turns enableWeakerNetworkIsolation on",
        lambda h: h.edit_file("beta", lambda d: d["sandbox"].update(enableWeakerNetworkIsolation=True)),
    ),
    (
        "sandbox-only-narrows",
        "the project rule finds nothing",
        lambda h: h.mp.setattr(wallsettings, "project_problem", lambda starts, home: None),
    ),
    (
        "sandbox-only-narrows",
        "the project rule no longer judges the sandbox table",
        lambda h: h.mp.setattr(wallsettings, "_sandbox_adds", lambda sb: None),
    ),
    (
        "per-repo-settings",
        "the narrowing is not applied",
        lambda h: h.mp.setattr(wallsettings, "narrowed", lambda conf, n: conf),
    ),
    (
        "per-repo-settings",
        "a table with an allow key is accepted",
        lambda h: h.mp.setattr(policy, "parse_repo_settings", lambda raw, repos, where: ()),
    ),
    (
        "otel-off",
        "prompt logging is on",
        lambda h: h.edit_file("alpha", lambda d: d["env"].update(OTEL_LOG_USER_PROMPTS="1")),
    ),
    ("otel-off", "tool details are not set", lambda h: h.edit_file("beta", drop(("env", "OTEL_LOG_TOOL_DETAILS")))),
    (
        "owner-and-review-date",
        "the review date has passed",
        lambda h: h.edit_policy("review_date = 2027-06-01", "review_date = 2026-11-12"),
    ),
    ("owner-and-review-date", "the owner is missing", lambda h: h.edit_policy('owner = "alpha lead"\n', "")),
    (
        "owner-and-review-date",
        "the review date is missing",
        lambda h: h.edit_policy("review_date = 2027-06-01\n\n[classes.beta]", "\n[classes.beta]"),
    ),
    ("messaging-denied", "ListAgents is allowed", lambda h: h.edit_file("alpha", without_rule("ListAgents"))),
    ("messaging-denied", "SendMessage is allowed", lambda h: h.edit_file("beta", without_rule("SendMessage"))),
    (
        "launch-write-block-and-token-deny",
        "the launch file stops blocking writes outside the working folders",
        lambda h: h.edit_file("alpha", drop(("permissions", wallsettings.WRITE_BLOCK_KEY))),
    ),
    (
        "launch-write-block-and-token-deny",
        "the launch file stops denying one sign-in token",
        lambda h: h.edit_file("beta", without_token("CLAUDE_CODE_OAUTH_TOKEN")),
    ),
    (
        "launch-write-block-and-token-deny",
        "the launch file has no credentials table",
        lambda h: h.edit_file("alpha", drop(("sandbox", "credentials"))),
    ),
    (
        "remote-and-connectors-off",
        "Remote Control is not switched off",
        lambda h: h.edit_file("alpha", drop((wallsettings.REMOTE_KEY,))),
    ),
    (
        "remote-and-connectors-off",
        "the claude.ai connectors are not switched off",
        lambda h: h.edit_file("beta", lambda d: d.update({wallsettings.CONNECTORS_KEY: False})),
    ),
    (
        "headless-job-in-wall",
        "a job is not made unattended",
        lambda h: h.mp.setattr(claude_code, "unattended", lambda settings: settings),
    ),
    (
        "headless-job-in-wall",
        "neither the generated settings nor the launch file block writes outside the working folders",
        lambda h: (
            h.edit_settings(drop(("permissions", wallsettings.WRITE_BLOCK_KEY))),
            h.edit_file("alpha", drop(("permissions", wallsettings.WRITE_BLOCK_KEY))),
        ),
    ),
    (
        "headless-job-in-wall",
        "the managed file lets shell commands write outside the class",
        lambda h: h.edit_file("alpha", lambda d: d["sandbox"]["filesystem"]["allowWrite"].append("/srv"), managed=True),
    ),
    (
        "local-model-in-wall",
        "a local_only class takes a hosted provider",
        lambda h: a_local_class(h, lambda h: h.mp.setattr(sandbox, "provider_problem", lambda cls, provider: None)),
    ),
    (
        "local-model-in-wall",
        "the local class's launch file can reach a hosted model",
        lambda h: a_local_class(
            h,
            lambda h: h.edit_file(
                "gamma", lambda d: d["sandbox"]["network"]["allowedDomains"].append("api.anthropic.com")
            ),
        ),
    ),
    (
        "local-model-in-wall",
        "the local class's launch file lacks the messaging refusal",
        lambda h: a_local_class(h, lambda h: h.edit_file("gamma", drop(("crossSessionInbound",)))),
    ),
    (
        "class-file-root-owned",
        "the file is writable by others",
        lambda h: wallsettings.launch_file_path("alpha", kind()).chmod(0o666),
    ),
    (
        "class-file-root-owned",
        "the file is not root's",
        lambda h: h.mp.setattr(wallsettings, "_is_root", lambda st: False),
    ),
]


@pytest.mark.parametrize(("case", "label", "mutate"), MUTATIONS, ids=[f"{c}: {label}" for c, label, _ in MUTATIONS])
def test_removing_a_cases_setting_makes_that_case_fail(host, case, label, mutate):
    assert results()[case]["status"] == "pass"  # the same host, before
    mutate(host)
    after = results()[case]
    assert after["status"] == "fail" and after["detail"], (label, after)
    # a case that could not run has not been shown to hold, but it is not the failure its setting names
    assert not after["detail"].startswith("the case could not run"), (label, after)


def test_every_named_case_has_a_mutation_test():
    assert {case for case, _, _ in MUTATIONS} | set(FLOW_NAMES) == set(NAMES)
    assert not {case for case, _, _ in MUTATIONS} & set(FLOW_NAMES)  # each has its mutation tests in one place


def test_a_failing_case_is_exit_1_with_a_typed_error_naming_the_next_step(host, capsys):
    host.edit_file("alpha", drop(("crossSessionInbound",)))
    code, out, _ = run(capsys, "check", "--json")
    err = json.loads(out)
    assert code == 1 and err["error_kind"] == "walls_check_failed" and err["exit_code"] == 1
    assert "inbound-refused" in err["message"] and "ws walls check" in err["hint"]
    failed = [c for c in err["details"]["cases"] if c["status"] == "fail"]
    assert [c["name"] for c in failed] == ["inbound-refused"]
    code, out, _ = run(capsys, "check")  # as text: each case on a line, then the typed error
    assert code == 1 and "fail  inbound-refused" in out


# ---- the probes are real: the gate denies under bypass and auto, and does not for the wrong reason -----------------------------


@pytest.mark.parametrize("mode", ["bypassPermissions", "auto"])
def test_the_gate_probe_is_a_denial_under_each_mode(host, mode):
    ctx, _, _ = walls._context(dict(os.environ), walls._now())
    assert walls._gate_denies(ctx, mode) is None
    payload = {"tool_name": "Bash", "tool_input": {"command": walls.PROBE_COMMAND}, "permission_mode": mode, "cwd": "/"}
    assert gate.check("claude-code", json.dumps(payload), policy.loads("")) is None  # the probe is denied by its policy


def test_a_gate_that_allows_the_probe_command_fails_the_hook_cases(host, monkeypatch):
    monkeypatch.setattr(gate, "check", lambda *a, **k: None)
    got = results()
    assert got["hooks-under-bypass"]["status"] == "fail" and got["hooks-under-auto"]["status"] == "fail"
    assert "did not deny" in got["hooks-under-auto"]["detail"]


# ---- the host is not set up --------------------------------------------------------------------------------------------


def test_with_no_walled_class_check_exits_1_names_the_next_command_and_skips_the_host_cases(box, capsys):
    policy_file().write_text("", encoding="utf-8")
    code, out, _ = run(capsys, "check", "--json")
    err = json.loads(out)
    assert code == 1 and err["error_kind"] == "walls_not_set_up" and err["exit_code"] == 1
    assert "ws walls init NAME" in err["hint"]
    status = {c["name"]: c["status"] for c in err["details"]["cases"]}
    assert (
        status["inbound-refused"] == "skip" and status["otel-off"] == "skip" and status["per-repo-settings"] == "pass"
    )
    assert "optional" in " ".join(err["details"]["setup"])  # walled classes are optional


def test_a_class_with_no_user_or_file_prints_the_setup_and_the_way_out_and_skips_the_host_cases(
    box, capsys, monkeypatch
):
    policy_file().write_text(POLICY.format(box=box), encoding="utf-8")
    monkeypatch.setattr(wallsettings, "passwd", lambda name: None)
    code, out, _ = run(capsys, "check", "--json")
    err = json.loads(out)
    assert code == 1 and err["error_kind"] == "walls_not_set_up"
    assert "ws install --class alpha" in err["hint"]
    setup = "\n".join(err["details"]["setup"])
    assert "ws install --class beta" in setup  # every class that is not in place
    assert (
        f"sudo rm -f {wallsettings.launch_file_path('alpha', kind())} " in setup
        and "userdel" in setup
        or "sysadminctl" in setup
    )
    assert "chmod 755" in setup
    assert {c["status"] for c in err["details"]["cases"] if c["name"] in ("inbound-refused", "messaging-denied")} == {
        "skip"
    }
    code, out, _ = run(capsys, "check")  # text mode prints the same commands, then the error
    assert code == 1 and "sudo rm" in out and "ws install --class alpha" in out


def test_a_user_in_place_but_no_class_file_is_not_set_up(box, capsys, monkeypatch):
    policy_file().write_text(POLICY.format(box=box), encoding="utf-8")
    monkeypatch.setattr(wallsettings, "passwd", lambda name: types.SimpleNamespace(pw_dir=f"/home/{name}"))
    code, out, _ = run(capsys, "check", "--json")
    assert code == 1 and json.loads(out)["error_kind"] == "walls_not_set_up"


def test_check_with_a_bad_flag_is_a_usage_error_exit_2(box, capsys):
    code, _, _err = run(capsys, "check", "--no-such-flag")
    assert code == 2


def test_an_invalid_policy_is_a_typed_error_and_nothing_is_run(box, capsys):
    policy_file().write_text("[classes.alpha]\naccess = 5\n", encoding="utf-8")
    code, out, _ = run(capsys, "check", "--json")
    assert code == 1 and json.loads(out)["error_kind"] == "bad_config"


def test_check_and_init_run_no_sudo_and_write_nothing_under_etc(host, monkeypatch, capsys):
    def no_process(*a, **k):
        raise AssertionError("ws walls must not start a process")

    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(os, "system", no_process)
    assert run(capsys, "check")[0] == 0
    assert run(capsys, "init", "gamma")[0] == 0
    assert not any(str(p).startswith("/etc") for p in (wallsettings.launch_file_path("alpha", kind()), policy_file()))


# ---- one clock for a review that is due -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "due"),
    [
        (datetime(2027, 5, 31, 23, 59, tzinfo=UTC), False),
        (datetime(2027, 6, 1, 0, 0, tzinfo=UTC), True),
        (datetime(2027, 6, 1, 11, 30, tzinfo=timezone(timedelta(hours=12))), False),  # 23:30 UTC the day before
        (datetime(2027, 6, 1, 12, 30, tzinfo=timezone(timedelta(hours=12))), True),  # 00:30 UTC
        (datetime(2027, 5, 31, 20, 0, tzinfo=timezone(timedelta(hours=-5))), True),  # 01:00 UTC on the 1st
    ],
)
def test_doctor_and_walls_check_judge_a_review_by_the_same_utc_clock(host, monkeypatch, now, due):
    cls = host.pol.classes["alpha"]
    assert cls.review_date == date(2027, 6, 1) and cls.review_due(now) is due
    monkeypatch.setattr(walls, "_now", lambda: now)
    ctx, _, _ = walls._context(dict(os.environ), now)
    case = walls._case_owner_review(ctx)
    ws017 = [c for c in doctor._review_checks(dict(os.environ), now) if c.detail.startswith("alpha")]
    assert case.ok is (not due) and (ws017[0].status == "warn") is due  # never apart


def test_a_class_with_no_review_date_has_no_ws017_line_and_a_naive_clock_is_refused(host):
    host.edit_policy("review_date = 2027-06-01", "")
    host.edit_policy("review_date = 2027-06-01", "")
    lines = doctor._review_checks(dict(os.environ), NOW)
    assert [c.detail for c in lines if c.detail.startswith("alpha")] == []
    with pytest.raises(ValueError):
        host.pol.classes["beta"].review_due(datetime(2027, 6, 1))


def test_ws017_says_who_owns_a_due_class_and_what_to_do(host):
    host.edit_policy("review_date = 2027-06-01", "review_date = 2026-11-12")
    (line,) = [c for c in doctor._review_checks(dict(os.environ), NOW) if c.detail.startswith("alpha")]
    assert line.id == "WS017" and line.status == "warn" and "2026-11-12" in line.detail and "alpha lead" in line.detail
    assert "review_date" in line.fix


# ---- init ----------------------------------------------------------------------------------------------------------------------


def test_init_prints_a_table_that_parses_with_the_whole_file_and_writes_nothing(box, capsys):
    code, out, _ = run(capsys, "init", "gamma")
    assert code == 0 and "[classes.gamma]" in out and not policy_file().exists()
    table = out.split("ws: nothing written")[0]
    cls = policy.loads(policy.default_toml() + "\n" + table).classes["gamma"]
    assert cls.access == "walled" and cls.os_user == "ws-gamma" and cls.inbound == "off" and cls.owner
    assert cls.review_date == (NOW + timedelta(days=walls.REVIEW_DAYS)).date()
    assert "ws install --class gamma" in out  # the next command


def test_init_write_makes_the_policy_with_the_prepare_dial_and_never_act(box, capsys):
    code, out, _ = run(capsys, "init", "--write", "gamma")
    assert code == 0 and policy_file().is_file()
    pol = policy.load(policy_file())
    assert pol.dial == "prepare" and "gamma" in pol.classes
    assert 'dial = "act"' not in policy_file().read_text() and 'dial = "act"' not in out
    assert not list(policy_file().parent.glob("policy.toml.bak-*"))  # nothing to back up


def test_init_write_into_an_existing_policy_backs_it_up_validates_the_whole_file_and_keeps_the_rest(host, capsys):
    before = policy_file().read_text()
    code, _out, _ = run(capsys, "init", "--write", "gamma")
    assert code == 0
    (backup,) = list(policy_file().parent.glob("policy.toml.bak-*"))
    assert backup.read_text() == before and oct(backup.stat().st_mode & 0o777) == "0o600"
    after = policy.load(policy_file())
    assert set(after.classes) == {"alpha", "beta", "gamma"} and policy_file().read_text().startswith(
        before.rstrip("\n")
    )
    assert after.dial == "prepare"


def test_init_write_never_writes_the_act_dial_even_when_the_person_set_it(host, capsys):
    host.text = 'dial = "act"\n' + host.text
    policy_file().write_text(host.text, encoding="utf-8")
    assert run(capsys, "init", "--write", "gamma")[0] == 0
    text = policy_file().read_text()
    assert text.count("act") == 1 and text.startswith('dial = "act"')  # theirs, untouched; ws added no second one


def test_init_write_leaves_the_file_as_it_was_when_the_new_whole_would_not_load(host, capsys, monkeypatch):
    before = policy_file().read_text()
    monkeypatch.setattr(
        walls, "class_table", lambda *a, **k: '[classes.gamma]\naccess = "walled"\nreview_date = "soon"\n'
    )
    code, out, _ = run(capsys, "init", "--write", "gamma", "--json")
    assert code == 1 and json.loads(out)["error_kind"] == "bad_config"
    assert policy_file().read_text() == before and not list(policy_file().parent.glob("policy.toml.bak-*"))


def test_init_write_is_an_atomic_replace_that_leaves_the_original_if_it_fails(host, capsys, monkeypatch):
    before = policy_file().read_text()
    real = install._write_atomic

    def fail_second(path, data, mode=None):
        if path == policy_file():
            raise OSError("disk full")
        real(path, data, mode)

    monkeypatch.setattr(install, "_write_atomic", fail_second)
    code, out, _ = run(capsys, "init", "--write", "gamma", "--json")
    assert code == 1 and json.loads(out)["error_kind"] == "io"
    assert policy_file().read_text() == before
    assert not [p for p in policy_file().parent.iterdir() if p.name.endswith(".tmp") or p.name.startswith(".policy")]


@pytest.mark.parametrize("name", ["", "a b", "../x", "x" * 40 + "!", "unclassified", "home/x"])
def test_init_refuses_a_name_that_is_not_a_class_name_with_a_usage_error(box, capsys, name):
    code, out, _ = run(capsys, "init", name, "--json")
    assert code == 2 and json.loads(out)["error_kind"] == "usage" and not policy_file().exists()


def test_init_refuses_a_class_that_is_already_there(host, capsys):
    code, out, _ = run(capsys, "init", "alpha", "--json")
    assert code == 2 and "already" in json.loads(out)["message"]


def test_init_names_a_macos_folder_on_macos(box):
    table = walls.class_table("gamma", "macos", NOW, "me")
    assert "/Users/Shared/work/gamma/**" in table and "/srv/" not in table
    assert "/srv/work/gamma/**" in walls.class_table("gamma", "linux", NOW, "me")


# ---- edit ----------------------------------------------------------------------------------------------------------------------


def editor(tmp_path: Path, body: str) -> str:
    script = tmp_path / "ed.py"
    script.write_text("import sys\np = sys.argv[1]\ntext = open(p).read()\n" + body, encoding="utf-8")
    return f"{sys.executable} {script}"


def test_edit_replaces_the_policy_with_a_valid_edit_and_backs_the_old_one_up(host, box, monkeypatch, capsys):
    before = policy_file().read_text()
    monkeypatch.setenv("EDITOR", editor(box, "open(p, 'w').write(text.replace('alpha lead', 'new lead'))\n"))
    monkeypatch.delenv("VISUAL", raising=False)
    code, _out, _ = run(capsys, "edit")
    assert code == 0 and "new lead" in policy_file().read_text()
    (backup,) = list(policy_file().parent.glob("policy.toml.bak-*"))
    assert backup.read_text() == before


def test_edit_refuses_an_invalid_file_leaves_the_original_and_keeps_the_edit(host, box, monkeypatch, capsys):
    before = policy_file().read_text()
    monkeypatch.setenv("EDITOR", editor(box, "open(p, 'w').write(text + '\\n[classes.zed]\\naccess = 5\\n')\n"))
    code, _out, err = run(capsys, "edit")  # `edit` keeps the terminal, so its errors are text on stderr
    assert code == 1 and "classes.zed" in err and "was not changed" in err
    assert policy_file().read_text() == before and not list(policy_file().parent.glob("policy.toml.bak-*"))
    kept = err.rsplit("kept at ", 1)[1].split()[0]
    assert "zed" in Path(kept).read_text()  # the person's work is not lost


def test_edit_with_no_change_or_no_editor_or_a_failing_editor_changes_nothing(host, box, monkeypatch, capsys):
    before = policy_file().read_text()
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    code, _out, err = run(capsys, "edit")
    assert code == 2 and "no editor is set" in err
    monkeypatch.setenv("EDITOR", editor(box, "pass\n"))
    assert run(capsys, "edit")[0] == 0 and not list(policy_file().parent.glob("policy.toml.bak-*"))
    monkeypatch.setenv("EDITOR", editor(box, "open(p, 'w').write('[gate]\\n')\nsys.exit(3)\n"))
    assert run(capsys, "edit")[0] == 1
    assert policy_file().read_text() == before


def test_edit_keeps_a_dial_the_person_sets_in_their_own_edit_including_act(host, box, monkeypatch, capsys):
    monkeypatch.setenv("EDITOR", editor(box, "open(p, 'w').write('dial = \"act\"\\n' + text)\n"))
    code, _out, _err = run(capsys, "edit")
    assert (
        code == 0 and policy.load(policy_file()).dial == "act"
    )  # their file, their dial: `init` is what never writes act


def test_edit_starts_from_the_default_policy_when_there_is_none(box, monkeypatch, capsys):
    monkeypatch.setenv("EDITOR", editor(box, "assert 'dial = \"prepare\"' in text\n"))
    assert run(capsys, "edit")[0] == 0 and not policy_file().exists()  # unchanged: nothing written


# ---- the policy write: one writer at a time, never over a change it did not read, never through a link ---------------------------


def temp_root(tmp_path, monkeypatch) -> Path:
    """A system temp root of the test's own, so a leftover `ws-walls-edit-*` copy is seen."""
    root = tmp_path / "systmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def copies(root: Path) -> list[Path]:
    return list(root.glob("ws-walls-edit-*"))


def test_replace_policy_refuses_when_the_file_is_not_the_bytes_it_was_built_from(host):
    before = policy_file().read_bytes()
    with pytest.raises(walls.WsError) as e:
        walls.replace_policy(policy_file(), "[gate]\n", NOW, b"what was read earlier")
    assert e.value.kind == "conflict" and "changed" in e.value.message
    assert policy_file().read_bytes() == before and not list(policy_file().parent.glob("policy.toml.bak-*"))


def test_edit_refuses_a_file_changed_while_the_editor_was_open_and_keeps_the_edit(host, box, monkeypatch, capsys):
    root = temp_root(box, monkeypatch)
    body = (
        f"open({str(policy_file())!r}, 'a').write('\\n# changed by something else\\n')\n"
        "open(p, 'w').write(text + '# mine\\n')\n"
    )
    monkeypatch.setenv("EDITOR", editor(box, body))
    code, _out, err = run(capsys, "edit")
    assert code == 1 and "changed while ws was working" in err
    assert "# changed by something else" in policy_file().read_text() and "# mine" not in policy_file().read_text()
    assert not list(policy_file().parent.glob("policy.toml.bak-*"))
    kept = Path(err.rsplit("kept at ", 1)[1].split()[0])
    assert "# mine" in kept.read_text() and copies(root) == [kept.parent]


def test_init_write_refuses_a_file_changed_after_it_was_read(host, monkeypatch, capsys):
    real = walls.read_policy
    reads: list[int] = []

    def read_then_change(path):
        raw = real(path)
        reads.append(1)
        if len(reads) == 1:  # the first read is `init`'s; the person's editor saves between it and the write
            path.write_text(raw.decode() + "\n# someone else\n", encoding="utf-8")
        return raw

    monkeypatch.setattr(walls, "read_policy", read_then_change)
    code, out, _ = run(capsys, "init", "--write", "gamma", "--json")
    assert code == 1 and json.loads(out)["error_kind"] == "conflict"
    assert "# someone else" in policy_file().read_text() and "gamma" not in policy.load(policy_file()).classes


def test_the_policy_write_takes_the_lock_another_ws_run_would_take(host, monkeypatch):
    monkeypatch.setattr(userconfig, "_LOCK_WAIT", 0.2)
    with userconfig.locked(policy_file()), pytest.raises(walls.WsError) as e:
        walls.replace_policy(policy_file(), host.text + "\n# x\n", NOW, policy_file().read_bytes())
    assert e.value.kind == "conflict" and "lock" in e.value.message
    assert policy_file().read_text() == host.text
    walls.replace_policy(policy_file(), host.text + "\n# x\n", NOW, policy_file().read_bytes())  # free again
    assert policy_file().read_text().endswith("# x\n")


@pytest.mark.parametrize("command", [["init", "--write", "gamma"], ["edit"]])
def test_a_policy_that_is_a_link_is_refused_and_the_file_behind_it_is_not_touched(
    host, box, monkeypatch, capsys, command
):
    real = box / "elsewhere.toml"
    real.write_text(host.text, encoding="utf-8")
    policy_file().unlink()
    policy_file().symlink_to(real)
    monkeypatch.setenv("EDITOR", editor(box, "open(p, 'w').write(text + '# mine\\n')\n"))
    code, _out, err = run(capsys, *command)
    assert code == 1 and "is a link" in err
    assert real.read_text() == host.text and policy_file().is_symlink()
    assert not list(box.glob("**/policy.toml.bak-*")) and not list(box.glob("elsewhere.toml.bak-*"))


def test_backup_names_are_unique_a_second_backup_in_the_same_second_keeps_the_first(host):
    first = policy_file().read_bytes()
    one = walls.replace_policy(policy_file(), host.text + "\n# one\n", NOW, first)
    second_base = policy_file().read_bytes()
    two = walls.replace_policy(policy_file(), host.text + "\n# two\n", NOW, second_base)
    assert one != two and one.read_bytes() == first and two.read_bytes() == second_base
    assert {p.name for p in policy_file().parent.glob("policy.toml.bak-*")} == {one.name, two.name}


def test_a_backup_name_that_is_taken_is_never_overwritten_not_even_through_a_link(host, box):
    stamp = walls._stamp(NOW)
    taken = policy_file().with_name(f"policy.toml.bak-{stamp}")
    taken.write_text("someone's own copy", encoding="utf-8")
    target = box / "victim.txt"
    target.write_text("untouched", encoding="utf-8")
    policy_file().with_name(f"policy.toml.bak-{stamp}-1").symlink_to(target)
    backup = walls.replace_policy(policy_file(), host.text + "\n# x\n", NOW, policy_file().read_bytes())
    assert taken.read_text() == "someone's own copy" and target.read_text() == "untouched"
    assert backup.name == f"policy.toml.bak-{stamp}-2" and backup.read_text() == host.text


def test_a_policy_that_cannot_be_read_is_a_typed_error_not_a_traceback(host, box, monkeypatch, capsys):
    monkeypatch.setenv("EDITOR", editor(box, "pass\n"))
    policy_file().write_bytes(b"\xff\xfe not utf-8")
    code, _out, err = run(capsys, "edit")
    assert code == 1 and "not UTF-8" in err and "Traceback" not in err
    policy_file().unlink()
    policy_file().mkdir()
    code, _out, err = run(capsys, "edit")
    assert code == 1 and "not a plain file" in err and "Traceback" not in err
    code, out, _ = run(capsys, "init", "--write", "gamma", "--json")
    assert code == 1 and json.loads(out)["error_kind"] in ("refused", "bad_config")


def test_the_temp_copy_of_an_edit_is_removed_on_every_path_but_a_refused_edit(host, box, monkeypatch, capsys):
    root = temp_root(box, monkeypatch)
    cases = [
        ("open(p, 'w').write(text.replace('alpha lead', 'new lead'))\n", 0),  # applied
        ("pass\n", 0),  # no change
        ("open(p, 'w').write('[gate]\\n')\nsys.exit(3)\n", 1),  # the editor failed
    ]
    for body, want in cases:
        monkeypatch.setenv("EDITOR", editor(box, body))
        assert run(capsys, "edit")[0] == want
        assert copies(root) == []
    monkeypatch.setenv("EDITOR", "/nonexistent/editor-ws-walls")  # the editor does not start
    assert run(capsys, "edit")[0] == 2 and copies(root) == []
    monkeypatch.setenv(
        "EDITOR", editor(box, "open(p, 'wb').write(b'\\xff\\xfe')\n")
    )  # an edit that cannot be read back
    code, _out, err = run(capsys, "edit")
    assert code == 1 and len(copies(root)) == 1 and str(copies(root)[0]) in err
    for c in copies(root):
        for f in c.iterdir():
            f.unlink()
        c.rmdir()
    monkeypatch.setenv("EDITOR", editor(box, "open(p, 'w').write(text + '\\n[classes.zed]\\naccess = 5\\n')\n"))
    code, _out, err = run(capsys, "edit")  # a refused edit: kept, and the hint names it
    assert code == 1 and len(copies(root)) == 1 and str(copies(root)[0] / "policy.toml") in err


# ---- the cases test what they name ---------------------------------------------------------------------------------------------


def test_the_other_class_case_asks_the_launch_own_lookup_about_a_folder_of_each_class_and_one_in_none(
    host, monkeypatch
):
    asked: list[str] = []
    real = policy.class_of
    monkeypatch.setattr(policy, "class_of", lambda pol, path: asked.append(str(path)) or real(pol, path))
    assert results()["class-file-not-seen-by-other-class"]["status"] == "pass"
    assert any(p.startswith(f"{host.box}/work/beta") for p in asked)  # a path in beta
    stray = [p for p in asked if not p.startswith(str(host.box))]
    assert stray and real(host.pol, stray[0]) is policy.UNCLASSIFIED  # a path in no class


def test_the_other_class_case_checks_the_launch_copy_built_per_project_not_the_generic_base(host, monkeypatch):
    (host.box / "work" / "beta" / "shared").mkdir(parents=True)
    (host.box / "work" / "alpha").mkdir(parents=True)
    host.text = f'[gate]\nallow_read = ["{host.box}/work/beta/shared"]\n' + host.text
    policy_file().write_text(host.text, encoding="utf-8")
    for name in ("alpha", "beta"):
        host.place(name)
    got = results()["class-file-not-seen-by-other-class"]
    assert got["status"] == "pass", got["detail"]  # the entry is dropped from alpha's copy, as the launch drops it
    monkeypatch.setattr(wallsettings, "read_denied", lambda doc: [])  # a launch that applied the denial too late
    got = results()["class-file-not-seen-by-other-class"]
    assert got["status"] == "fail" and f"{host.box}/work/beta" in got["detail"]


def test_the_hook_probe_runs_with_no_project_dir_and_no_walled_marker_and_puts_them_back(host, monkeypatch):
    project = host.box / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text('{"sandbox": {"enabled": false}}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setenv(wallsettings.WALL_MARKER, "1")
    seen: list[tuple] = []
    real = gate.check

    def spy(runner, raw, pol, *a, **k):
        env = (os.environ.get("CLAUDE_PROJECT_DIR"), os.environ.get(wallsettings.WALL_MARKER))
        seen.append((*env, json.loads(raw)["cwd"]))
        return real(runner, raw, pol, *a, **k)

    monkeypatch.setattr(gate, "check", spy)
    got = results()
    assert got["hooks-under-bypass"]["status"] == got["hooks-under-auto"]["status"] == "pass"
    assert seen and all(p is None and m is None for p, m, _ in seen)
    assert all(Path(cwd).is_relative_to(paths.state_dir()) for _, _, cwd in seen)  # a folder of ws's own
    assert os.environ["CLAUDE_PROJECT_DIR"] == str(project) and os.environ[wallsettings.WALL_MARKER] == "1"


def test_the_project_load_probe_does_not_read_the_system_temp_root(host, box, monkeypatch):
    root = temp_root(box, monkeypatch)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "y"}}}), encoding="utf-8")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text('{"disableAllHooks": true}', encoding="utf-8")
    got = results()
    assert got["project-load-check"]["status"] == "pass" and got["hooks-not-disabled"]["status"] == "pass", got
    assert {p.name for p in root.iterdir()} == {".mcp.json", ".claude"}  # and nothing was left in it


def test_doctor_shows_a_ws017_warn_row_for_a_class_past_its_review_date(host):
    host.edit_policy("review_date = 2027-06-01", "review_date = 2020-01-01")
    checks = doctor.run_checks(osinfo.detect(), host.box / "p.toml", dict(os.environ), network=False)
    rows = [c for c in checks if c.id == "WS017" and c.detail.startswith("alpha")]
    assert [c.status for c in rows] == ["warn"] and "2020-01-01" in rows[0].detail


# ---- walls.py uses only public names of the modules it imports -----------------------------------------------------------------


def test_walls_py_uses_no_private_name_of_another_module():
    import ast

    tree = ast.parse(Path(walls.__file__).read_text(encoding="utf-8"))
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    modules = {(a.asname or a.name).split(".")[0] for n in imports for a in n.names}
    private = [
        f"line {n.lineno}: {n.value.id}.{n.attr}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id in modules
        and n.attr.startswith("_")
        and not n.attr.startswith("__")
    ]
    private += [
        f"line {n.lineno}: from {n.module} import {a.name}"
        for n in imports
        if isinstance(n, ast.ImportFrom)
        for a in n.names
        if a.name.startswith("_") and n.module != "__future__"
    ]
    assert private == []
    promoted = [
        (policy, "NAME_PATTERN"),
        (policy, "CLASS_RESERVED"),
        (policy, "parse_repo_settings"),
        (install, "write_atomic"),
    ]
    assert all(hasattr(module, name) for module, name in promoted)


# ---- the new class-table keys read the way every consumer reads them -----------------------------------------------------------


def test_walls_is_found_by_scanning_and_cli_py_has_no_line_for_it():
    src = Path(walls.__file__).read_text(encoding="utf-8")
    assert '\nCOMMAND = "walls"\n' in src and "def register(sub)" in src
    assert "walls" not in (Path(cli.__file__).read_text(encoding="utf-8"))


def test_ws_install_class_text_points_at_check_and_prints_the_way_out(host, capsys):
    code, out, _ = run_install(capsys)
    assert code == 0
    assert "ws walls check" in out and f"sudo rm -f {wallsettings.launch_file_path('alpha', kind())} " in out
    assert "optional" in out


def run_install(capsys):
    code = cli.main(["install", "--class", "alpha"])
    return code, capsys.readouterr().out, ""


# ---- one class file path for the launch, the doctor, the check and the printed text ----------------------------------------------


def test_every_reader_of_the_class_file_uses_the_one_path_and_the_one_shape(host, monkeypatch):
    f = wallsettings.launch_file_path("alpha", kind())
    cls = host.pol.classes["alpha"]
    assert wallsettings.load_launch_file(cls, kind()) == json.loads(f.read_text())  # the launch
    rows = {r.part: r for r in wallsettings.class_rows(host.pol, dict(os.environ), kind()) if r.cls == "alpha"}
    assert (
        str(wallsettings.class_file_path("alpha", kind())) in rows["file present and root-owned"].detail
    )  # WS014: the managed file
    text = wallsettings.install_text(cls, dict(os.environ), kind(), host.pol)
    assert (
        str(f) in text and json.dumps(json.loads(f.read_text()), indent=2).splitlines()[1] in text
    )  # the printed text
    ctx, ready, _ = walls._context(dict(os.environ), NOW)
    assert ctx.docs["alpha"] == json.loads(f.read_text()) and [c.name for c in ready] == ["alpha", "beta"]  # the check
    moved = f.parent.parent / "elsewhere"
    monkeypatch.setattr(wallsettings, "launch_dir", lambda k: str(moved))  # one function decides: all four follow it
    assert wallsettings.launch_file_path("alpha", kind()) == moved / f.name
    assert wallsettings.launch_file_found("alpha", kind()) is None
    ctx, ready, missing = walls._context(dict(os.environ), NOW)
    assert ready == [] and len(missing) == 2


# ---- local models: an OpenCode session with a loopback provider, started by `ws gate launch` in a class ---------------------------


class Ran(list):
    seen: dict


@pytest.fixture
def started(monkeypatch):
    ran = Ran()
    seen: dict = {}

    def wrap(argv, *a, **k):
        seen.update(k)
        return sandbox.Wrapped(["wall", *argv])

    monkeypatch.setattr(sandbox, "wrap", wrap)
    monkeypatch.setattr(sandbox, "run_with_broker", lambda cmd, e, fds, root: ran.append(("wall", cmd, e)) or 0)
    monkeypatch.setattr(sandbox, "run_plain", lambda cmd, e: ran.append(("plain", cmd, e)) or 0)
    ran.seen = seen
    return ran


LOCAL = """
[gate]
deny_tools = ["mcp__x__y"]

[classes.alpha]
access = "walled"
os_user = "ws-alpha"
repos = ["{box}/work/alpha/**"]
allowed_providers = ["local"]
local_only = true
allowed_hosts = ["localhost:11434", "api.example.com"]
"""


def place_class_file(pol, name: str) -> Path:
    """A class's launch file as `ws install --class` renders it, where the launcher reads it (the caller stands root in)."""
    cls = pol.classes[name]
    f = wallsettings.launch_file_path(name, kind())
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(wallsettings.launch_settings(cls, wallsettings.class_home(cls, kind()), kind(), pol)))
    f.chmod(0o644)
    f.parent.chmod(0o755)
    return f


def test_a_local_opencode_session_in_a_class_gets_the_same_file_write_network_and_mcp_rules_as_a_claude_session(
    box, monkeypatch, started
):
    app = box / "work" / "alpha" / "app"
    app.mkdir(parents=True)
    monkeypatch.chdir(app)
    text = LOCAL.format(box=box)
    policy_file().write_text(text, encoding="utf-8")
    pol = policy.loads(text)
    cls = pol.classes["alpha"]
    monkeypatch.setattr(sandbox, "real_user", lambda: "ws-alpha")
    monkeypatch.setattr(wallsettings, "_is_root", lambda st: True)
    place_class_file(pol, "alpha")  # a walled class's OpenCode session reads its launch file, as a Claude one does
    monkeypatch.setenv("PATH", str(box))  # no runner on it: the wall and the launcher are stood in for
    # the Claude session: its class file holds writes to the class's repos and scratch, and loopback-only hosts
    file = wallsettings.class_settings(cls, wallsettings.class_home(cls, kind()), kind())
    allow_write = set(file["sandbox"]["filesystem"]["allowWrite"])
    assert file["sandbox"]["network"]["allowedDomains"] == ["localhost:11434"]
    # the OpenCode session, started the same way
    assert cli.main(["gate", "launch", "--runner", "opencode", "--provider", "local", "--", "opencode"]) == 0
    seen = started.seen
    writable = [str(Path(p).resolve()) for p in seen["writable"]]
    scratch = [p for p in writable if "ws-wall-" in p]
    assert str(app.resolve()) in writable and len(scratch) == 1  # the project and the private scratch
    assert all(p == str(app.resolve()) or p in scratch for p in writable)  # nothing else is writable
    assert str(app.resolve()).startswith(tuple(allow_write))  # the project is inside the class file's repos
    _, _, env = started[0]
    plan = sandbox.egress_plan(pol, cls, "local")
    assert [h for h in plan.domains()] == ["localhost:11434"]  # loopback only under local_only
    assert seen["net"] == (int(env["HTTPS_PROXY"].rsplit(":", 1)[1]), 11434)
    # MCP: the same deny reaches both runners' generated settings, from the one policy
    claude_deny = json.loads(claude_code.generate(pol, ["ws"], box / "o", policy_file())["settings.json"])[
        "permissions"
    ]["deny"]
    oc = json.loads(opencode.generate(pol, ["ws"], box / "o", policy_file())[opencode.CONFIG])["permission"]
    assert "mcp__x__y" in claude_deny and oc["x_y"] == "deny"


# ---- C360: the launch file holds the write block and the token deny; the managed file is as 0.1.9 wrote it ------------------


def test_the_launch_file_blocks_writes_and_denies_the_four_tokens_and_the_managed_file_has_neither():
    pol = policy.loads(POLICY.format(box="/b"))
    cls = pol.classes["alpha"]
    home = wallsettings.class_home(cls, "linux")
    doc = wallsettings.launch_settings(cls, home, "linux", pol)
    assert doc["permissions"][wallsettings.WRITE_BLOCK_KEY] is True
    names = [e["name"] for e in doc["sandbox"]["credentials"]["envVars"] if e["mode"] == "deny"]
    assert names == list(wallsettings.TOKEN_VARS) == list(claude_code.TOKEN_VARS) and len(names) == 4
    managed = wallsettings.class_settings(cls, home, "linux")  # binds every account: none of the two
    assert wallsettings.WRITE_BLOCK_KEY not in managed["permissions"] and "credentials" not in managed["sandbox"]


@pytest.mark.parametrize(
    "change",
    [drop(("permissions", wallsettings.WRITE_BLOCK_KEY)), without_token("ANTHROPIC_API_KEY")],
    ids=["no write block", "a token not denied"],
)
def test_a_planted_class_file_missing_the_write_block_or_a_token_deny_fails_walls_check(host, capsys, change):
    assert run(capsys, "check")[0] == 0
    host.edit_file("alpha", change)
    code, out, err = run(capsys, "check")
    assert code == 1 and "launch-write-block-and-token-deny" in err
    assert "fail  launch-write-block-and-token-deny" in out


# ---- `ws walls canary`: every case on a planted world ---------------------------------------------------------------------

REQUIRED = ("inbound message", "headless job", "canary token", "non-class remote", "G3 ", "G4 ", "G5 ", "G7 ", "G8 ",
            "G10 ", "G11 ", "G14 ", "G16 ")  # fmt: skip


def test_canary_exits_0_on_a_host_with_no_class_set_up(box, capsys):
    code, out, _ = run(capsys, "canary")
    assert code == 0, out
    assert "fail" not in out and out.strip().endswith(f"covering {len(walls.COVERS)} items")


def test_canary_counts_at_least_13_items_each_with_a_case_it_runs(box, capsys):
    items = list(walls.COVERS)
    assert len(items) >= 13
    for want in REQUIRED:
        assert any(want in item for item in items), want
    run_names = {c.name for c in walls.canary_cases()}
    assert set(walls.COVERS.values()) <= run_names
    assert run_names == set(NAMES) - set(walls.HOST_ONLY)
    code, out, _ = run(capsys, "canary", "--json")
    env = json.loads(out)
    data = env["data"]
    assert (
        code == 0
        and env["kind"] == "walls.canary"
        and len(data["covers"]) == len(items)
        and {c["status"] for c in data["covers"]} == {"pass"}
    )


def test_canary_reads_no_class_file_in_place_and_plants_only_its_own_classes(box, monkeypatch, capsys):
    monkeypatch.setattr(wallsettings, "class_file_found", lambda name: pytest.fail("the canary read a class file"))
    monkeypatch.setattr(wallsettings, "launch_file_found", lambda name, k: pytest.fail("the canary read a launch file"))
    assert run(capsys, "canary")[0] == 0
    assert set(policy.loads(walls.canary_policy(Path("/r"), NOW)).classes) == set(walls.CANARY_CLASSES)


def _without(where: str, *key: str):
    real = getattr(wallsettings, where)

    def patched(*a, **k):
        doc = real(*a, **k)
        drop(key)(doc)
        return doc

    return patched


CANARY_MUTATIONS = [
    ("inbound-refused", "launch_settings", ("crossSessionInbound",)),
    ("messaging-denied", "launch_settings", ("permissions", "deny")),
    ("class-push-refused", "launch_settings", ("sandbox", "network", "strictAllowlist")),
    ("otel-off", "launch_settings", ("env",)),
    ("remote-and-connectors-off", "launch_settings", (wallsettings.CONNECTORS_KEY,)),
    ("launch-write-block-and-token-deny", "launch_settings", ("permissions", wallsettings.WRITE_BLOCK_KEY)),
    ("launch-write-block-and-token-deny", "launch_settings", ("sandbox", "credentials")),
    ("local-model-in-wall", "launch_settings", (wallsettings.REMOTE_KEY,)),
    ("headless-job-in-wall", "class_settings", ("sandbox", "filesystem", "allowWrite")),
    ("hooks-under-bypass", "class_settings", ("permissions", wallsettings.BYPASS_KEY)),
]


@pytest.mark.parametrize(
    ("case", "where", "key"), CANARY_MUTATIONS, ids=[f"{c} via {w}" for c, w, _ in CANARY_MUTATIONS]
)
def test_canary_fails_when_the_wall_stops_writing_a_setting(box, monkeypatch, capsys, case, where, key):
    monkeypatch.setattr(wallsettings, where, _without(where, *key))
    code, out, err = run(capsys, "canary")
    assert code == 1 and case in err and f"fail  {case}" in out


def test_canary_fails_when_a_local_only_class_takes_a_hosted_model(box, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "provider_problem", lambda cls, provider: None)
    code, out, _ = run(capsys, "canary")
    assert code == 1 and "fail  local-model-in-wall" in out


# ---- C213 / rr-r2-3: a local_only class refuses a hosted model where the call is made, with one record --------------------

from test_run import ws as run_ws  # noqa: F401  (a scratch WS_HOME with a fake claude, for `ws run`)


def test_a_local_only_class_refuses_a_job_on_a_hosted_provider_and_records_exactly_one_refusal(run_ws, tmp_path):  # noqa: F811
    work = tmp_path / "work"
    work.mkdir()
    (run_ws.home / "policy.toml").write_text(
        f'[classes.work]\naccess = "walled"\nrepos = ["{work}/**"]\nlocal_only = true\n', encoding="utf-8"
    )
    run_ws.job(extra=f'cwd = "{work}"')  # the fake claude: Anthropic's hosted models
    assert cli.main(["run", "daily"]) == 3
    (rec,) = run_ws.records()  # exactly one line, and it is the refusal
    assert rec["status"] == "refused" and rec["stop_reason"] == "refused:egress"
    assert "class 'work'" in rec["error"] and "anthropic" not in rec["error"]
    assert not (run_ws.home / "fake-saw.json").exists()  # the runner never started


# ---- pa-10: the doctor warns when the policy names no data class ---------------------------------------------------------


def test_doctor_warns_when_the_policy_has_no_data_classes(box):
    policy_file().write_text('[gate]\ndeny_commands = ["x*"]\n', encoding="utf-8")
    (check,) = doctor._classes_check(dict(os.environ))
    assert (check.id, check.role, check.status) == ("WS020", "data-classes", "warn") and check.fix


def test_doctor_is_ok_about_data_classes_when_the_policy_names_some(host):
    (check,) = doctor._classes_check(dict(os.environ))
    assert (check.id, check.status) == ("WS020", "ok") and "2 data class(es)" in check.detail


def test_ws020_is_in_the_doctor_reference():
    page = (Path(__file__).resolve().parents[1] / "docs" / "reference" / "doctor-checks.md").read_text(encoding="utf-8")
    assert "| WS020 | data-classes |" in page


# ---- pa-11: an alert from a walled class's folder is the plain line ------------------------------------------------------


class _Sender:
    def __init__(self):
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((body, headers))
        return 200


@pytest.mark.parametrize("mode", ["title", "full"])
def test_an_alert_from_a_walled_folder_sends_no_folder_name_whatever_alert_text_says(host, mode):
    from ws.roles import ntfy

    folder = host.box / "work" / "alpha" / "secret-case-folder"
    folder.mkdir(parents=True)
    cfg = {"adapter": "ntfy", "url": "https://ntfy.example.org/t", "alert_text": mode}
    payload = {"hook_event_name": "Notification", "cwd": str(folder), "message": "about secret-case-folder"}
    sender = _Sender()
    ntfy.alert(cfg, dict(os.environ), payload, "claude-code", now=NOW, sender=sender)
    ((body, headers),) = sender.calls
    text = body.decode() + json.dumps(headers)
    assert "secret-case-folder" not in text and "alpha" not in text and "a session needs you" in body.decode()


def test_an_alert_from_a_folder_in_no_class_keeps_its_folder_name(host):
    from ws.roles import ntfy

    folder = host.box / "elsewhere" / "my-app"
    folder.mkdir(parents=True)
    cfg = {"adapter": "ntfy", "url": "https://ntfy.example.org/t"}
    sender = _Sender()
    ntfy.alert(
        cfg, dict(os.environ), {"hook_event_name": "Notification", "cwd": str(folder)}, "c", now=NOW, sender=sender
    )
    assert "my-app" in sender.calls[0][0].decode()


def test_an_alert_is_plain_when_the_policy_does_not_load(box):
    from ws.roles import ntfy

    policy_file().write_text("[gate\n", encoding="utf-8")
    assert ntfy.walled_folder(str(box), dict(os.environ)) is True
    assert ntfy.walled_folder("", dict(os.environ)) is False


# ---- ls-10: an allowlist planted inside the repo is ignored --------------------------------------------------------------


def test_an_allowlist_edit_planted_inside_a_class_repo_is_ignored(host):
    """The lists that say what a class session may do (the policy, the class files) live outside the repo. A policy file
    and a settings allow rule planted in the class's repo change neither the policy ws loads nor the launch copy, and the
    settings file is refused at the start."""
    pol_before = policy.load(policy_file())
    ctx, _, _ = walls._context(dict(os.environ), walls._now())
    root = host.box / "work" / "alpha"
    root.mkdir(parents=True)
    before = walls._launch_copy(ctx, str(root), ctx.docs["alpha"])
    (root / ".ws").mkdir()
    (root / ".ws" / "policy.toml").write_text('[gate]\nallow_write = ["/"]\n', encoding="utf-8")
    (root / "policy.toml").write_text('[gate]\nallow_write = ["/"]\n', encoding="utf-8")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash(*)"]}}', encoding="utf-8")
    with walls._chdir(root):
        assert policy.policy_path(dict(os.environ)) == policy_file()
        assert policy.load(policy_file()) == pol_before
        ctx, _, _ = walls._context(dict(os.environ), walls._now())
        after = walls._launch_copy(ctx, str(root), ctx.docs["alpha"])
    fs_a, fs_b = after["sandbox"]["filesystem"], before["sandbox"]["filesystem"]
    for key in ("allowWrite", "allowRead"):  # nothing more is open
        assert fs_a.get(key) == fs_b.get(key), key
    assert set(fs_a.get("denyWrite", [])) >= set(fs_b.get("denyWrite", []))  # a planted config file is only locked
    assert after["sandbox"]["network"] == before["sandbox"]["network"]
    assert after["permissions"] == before["permissions"] and "allow" not in after["permissions"]
    refusal = wallsettings.project_problem([str(root)], host.box / "home")
    assert refusal is not None and refusal.kind == "project_load_refused" and "settings.json" in refusal.hint


# ---- rr-r2-3: no class key is parsed and read by nothing -----------------------------------------------------------------


def test_every_class_key_the_policy_parses_is_read_outside_the_parser():
    import dataclasses

    src = Path(walls.__file__).resolve().parent
    readers = "".join(
        p.read_text(encoding="utf-8")
        for p in src.rglob("*.py")
        if p.name != "policy.py" and "__pycache__" not in p.parts
    )
    unread = [f.name for f in dataclasses.fields(policy.Class) if f.name != "name" and f".{f.name}" not in readers]
    assert unread == [], f"parsed but read by no module: {unread}"
