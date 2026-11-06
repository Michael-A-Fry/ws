"""The tmux side: options gated by version and scope, the browser as the one size owner, and a real tmux if there is one."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ws.term import tmux

POLICY = json.loads((Path(__file__).parent / "fixtures" / "term" / "tmux_size_policy.json").read_text())


class FakeTmux:
    """A `run` that records every command and answers like a tmux of a given version."""

    def __init__(self, version="tmux 3.3a", *, server_up=False, session_exists=False, status="on"):
        self.version, self.server_up, self.exists, self.status = version, server_up, session_exists, status
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv == ["-V"]:
            return 0, self.version + "\n"
        if argv[0] == "list-sessions":
            return (0, "work: 1 windows\n") if self.server_up else (1, "no server running")
        if argv[0] == "has-session":
            return (0, "") if self.exists else (1, "can't find session")
        if argv[:3] == ["show-options", "-A", "-v"]:
            return 0, self.status + "\n"
        if argv[0] == "new-session":
            return 0, "@3\n"
        return 0, ""

    def sets(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "set-option"]


# -- versions and options


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("tmux 3.3a", (3, 3)),
        ("tmux 3.5", (3, 5)),
        ("tmux 2.9a", (2, 9)),
        ("tmux next-3.6", (3, 6)),
        ("tmux master", (999, 0)),
    ]
    + [("tmux", (0, 0)), ("", (0, 0)), ("something else 10.4", (10, 4))],
)
def test_parse_version(text, want):
    assert tmux.parse_version(text) == want


@pytest.mark.parametrize("case", POLICY["options_by_version"], ids=lambda c: c["version"])
def test_options_follow_the_tmux_version_and_scope(case):
    version = tmux.parse_version(case["version"])
    got = tmux.managed_options(version, server_options=True)
    for scope in ("session", "window", "server"):
        assert [o.name for o in got if o.scope == scope] == case[scope], (case["version"], scope)
    without = tmux.managed_options(version, server_options=False)
    assert [o.name for o in without if o.scope == "server"] == []


def test_the_managed_values_are_the_measured_ones():
    by = {o.name: o.value for o in tmux.OPTIONS}
    assert by == {"window-size": "manual", "allow-passthrough": "on", "escape-time": "10", "extended-keys": "on"}


@pytest.mark.parametrize("name", ["", "a b", "a:b", "a.b", "a" * 65, "x;y", "-t", "-x", "it's", "é"])
def test_a_session_name_that_could_be_a_target_or_a_flag_is_refused(name):
    with pytest.raises(ValueError, match="session name"):
        tmux.Tmux(name)


# -- prepare


def test_a_new_session_gets_its_own_options_by_name_and_is_sized():
    fake = FakeTmux()
    t = tmux.Tmux("work", run=fake)
    attach = t.prepare(100, 30)
    assert attach == ["tmux", "attach-session", "-t", "=work"]
    assert ["new-session", "-d", "-P", "-F", "#{window_id}", "-s", "work", "-x", "100", "-y", "30"] in fake.calls
    assert fake.sets() == [
        ["set-option", "-t", "=work:", "window-size", "manual"],
        ["set-option", "-w", "-t", "@3", "allow-passthrough", "on"],
    ]
    assert fake.calls[-1] == ["resize-window", "-t", "@3", "-x", "100", "-y", "29"]
    assert [o.name for o in t.skipped] == ["escape-time", "extended-keys"] and not t.adopted


def test_a_session_that_already_exists_is_attached_as_it_is():
    fake = FakeTmux(server_up=True, session_exists=True)
    t = tmux.Tmux("work", run=fake)
    assert t.prepare(100, 30) == ["tmux", "attach-session", "-t", "=work"]
    assert t.adopted
    assert [c[0] for c in fake.calls] == ["-V", "has-session"], "no new session, no option, no resize"
    t.resize(60, 20)
    assert [c[0] for c in fake.calls] == ["-V", "has-session"], "a later resize leaves its size policy alone"


def test_the_server_wide_options_are_set_only_when_the_person_asks():
    fake = FakeTmux(server_up=True)
    tmux.Tmux("work", run=fake).prepare(100, 30)
    assert all(c[1] != "-s" for c in fake.sets()), "not set unless asked, even on a server ws just started"
    fake = FakeTmux()
    t = tmux.Tmux("work", run=fake, server_options=True)
    t.prepare(100, 30)
    assert ["set-option", "-s", "escape-time", "10"] in fake.sets()
    assert ["set-option", "-s", "extended-keys", "on"] in fake.sets() and t.skipped == []


def test_options_this_tmux_doesnt_have_are_never_set():
    fake = FakeTmux("tmux 2.8")
    tmux.Tmux("work", run=fake, server_options=True).prepare(100, 30)
    assert [c[-2] for c in fake.sets()] == ["escape-time"]
    fake = FakeTmux("tmux 3.2")
    tmux.Tmux("work", run=fake).prepare(100, 30)
    assert [c[-2] for c in fake.sets()] == ["window-size"], "no allow-passthrough before tmux 3.3"


@pytest.mark.parametrize("version", ["tmux 1.9", "tmux 2.8", "tmux 3.1c", "tmux 3.3a", "tmux 3.5", "tmux next-3.6"])
@pytest.mark.parametrize("server_options", [False, True])
@pytest.mark.parametrize("exists", [False, True])
def test_the_builder_never_emits_a_global_option_or_aggressive_resize(version, server_options, exists):
    fake = FakeTmux(version, session_exists=exists, server_up=exists)
    t = tmux.Tmux("work", run=fake, server_options=server_options)
    t.prepare(100, 30)
    t.resize(80, 24)
    for call in fake.calls:
        assert "-g" not in call and not any(a.startswith("-g") for a in call), call
        assert "aggressive-resize" not in call, call
        assert "source-file" not in call and call[0] not in ("kill-server", "kill-session"), call
    assert "aggressive-resize" not in {o.name for o in tmux.OPTIONS}
    for call in fake.sets():
        # every option is set on the session or window ws made (or, only on request, the server), never unscoped
        assert call[1] in ("-t", "-w", "-s"), call


@pytest.mark.parametrize("case", POLICY["cases"], ids=lambda c: f"{c['version']}-status-{c['status']}-{c['rows']}")
def test_the_window_is_the_browsers_grid_less_the_status_line(case):
    fake = FakeTmux(case["version"], status=case["status"])
    tmux.Tmux("w", run=fake).prepare(case["cols"], case["rows"])
    assert fake.calls[-1] == ["resize-window", "-t", "@3", "-x", str(case["window"][0]), "-y", str(case["window"][1])]


def test_each_later_resize_from_the_page_resizes_the_window():
    fake = FakeTmux()
    t = tmux.Tmux("w", run=fake)
    t.prepare(100, 30)
    t.resize(60, 20)
    assert fake.calls[-1] == ["resize-window", "-t", "@3", "-x", "60", "-y", "19"]


def test_a_failing_command_ws_needs_is_a_typed_error():
    def run(argv):
        return (0, "tmux 3.3a") if argv == ["-V"] else (1, "boom")

    with pytest.raises(tmux.TmuxError, match="boom"):
        tmux.Tmux("w", run=run).prepare(80, 24)


def test_run_tmux_reports_a_missing_binary_as_a_failure(monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert tmux.run_tmux(["-V"])[0] == 127


# -- a real tmux, on a private socket

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")


@pytest.fixture
def private(tmp_path, monkeypatch):
    """A private tmux server (its own socket, no config file) that is killed afterwards."""
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    prefix = ["-L", "ws-" + os.urandom(4).hex(), "-f", "/dev/null"]
    yield prefix
    subprocess.run(["tmux", *prefix, "kill-server"], capture_output=True, check=False, timeout=10)


def globals_of(run) -> dict[str, str]:
    """Every global option, from `show -g`, `show -gw` and `show -gs` (all options at every scope), as text."""
    return {flags: run(["show-options", *flags.split()])[1] for flags in ("-g", "-gw", "-gs")}


@needs_tmux
def test_with_a_real_tmux_the_window_follows_the_browser_and_two_clients_dont_fight(private):
    run = tmux.runner(private)
    t = tmux.Tmux("ws-test", run=run, prefix=private)
    t.prepare(100, 30)

    def show(fmt: str) -> str:
        return run(["display-message", "-p", "-t", "=ws-test:", fmt])[1].strip()

    assert show("#{window_width}x#{window_height}") == "100x29"
    assert run(["show-options", "-v", "-t", "=ws-test:", "window-size"])[1].strip() == "manual"
    t.resize(60, 20)
    assert show("#{window_width}x#{window_height}") == "60x19"
    t.resize(140, 40)
    assert show("#{window_width}x#{window_height}") == "140x39", "the size follows the owner, whoever types"
    assert run(["show-options", "-v", "-w", "-t", "=ws-test:", "allow-passthrough"])[1].strip() == "on"


@needs_tmux
def test_a_ws_term_session_leaves_every_global_option_byte_identical(private):
    from ws.term import bridge

    run = tmux.runner(private)
    assert run(["new-session", "-d", "-s", "mine"])[0] == 0  # the person's own session, and so the server
    before = globals_of(run)
    assert before["-g"] and before["-gw"], "the comparison has something to compare"
    t = tmux.Tmux("ws-test", run=run, prefix=private)
    child = bridge.open_pty(t.prepare(100, 30), bridge.child_env(os.environ), 100, 30)
    try:
        for _ in range(100):
            if run(["list-clients", "-t", "=ws-test"])[1].strip():
                break
            time.sleep(0.05)
        assert run(["list-clients", "-t", "=ws-test"])[1].strip(), "the client attached"
        assert globals_of(run) == before, "while the session runs"
    finally:
        child.close()
    run(["kill-session", "-t", "=ws-test"])
    assert globals_of(run) == before, "after it stops"
    assert "aggressive-resize on" not in run(["show-options", "-gw"])[1]


@needs_tmux
def test_a_session_that_was_already_there_keeps_all_its_options(private):
    run = tmux.runner(private)
    assert run(["new-session", "-d", "-s", "theirs", "-x", "90", "-y", "30"])[0] == 0
    session_opts = run(["show-options", "-t", "=theirs:"])[1]
    window_opts = run(["show-options", "-w", "-t", "=theirs:"])[1]
    size = run(["display-message", "-p", "-t", "=theirs:", "#{window_width}x#{window_height}"])[1]
    t = tmux.Tmux("theirs", run=run, prefix=private)
    assert t.prepare(120, 40) == ["tmux", *private, "attach-session", "-t", "=theirs"]
    t.resize(50, 10)
    assert t.adopted
    assert run(["show-options", "-t", "=theirs:"])[1] == session_opts
    assert run(["show-options", "-w", "-t", "=theirs:"])[1] == window_opts
    assert run(["display-message", "-p", "-t", "=theirs:", "#{window_width}x#{window_height}"])[1] == size
