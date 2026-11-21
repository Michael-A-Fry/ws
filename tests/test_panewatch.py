"""`ws watch panes`: a fake tmux with screens, a fake ntfy sender and a clock the test sets. What it pushes, when, how often,
and the one key it may ever press."""

from __future__ import annotations

import json
import os

import pytest

from ws import cli, jobwatch, panewatch, paths
from ws.output import WsError
from ws.profile import dumps, profile_path
from ws.roles import ntfy

MIN = 60.0
ASK = """\
 ────────────────────────────────────────────────────────────────
  Which approach should I take?

  ❯ 1. Rebuild the index
    2. Leave it

 Enter to select · ↑/↓ to navigate · Esc to cancel
"""
REMOTE = """\
 ────────────────────────────────────────────────────────────────
   Remote Control
   This session is available in the Claude mobile app and at
   https://claude.ai/code/session_x
     Disconnect this session
     Show QR code   Scan with your phone to open this session
   ❯ Continue
   Enter to select · Esc to continue
"""
PLAIN = "some output\nmore output\n❯ \n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "ws"
    home.mkdir()
    e = {"WS_HOME": str(home), "HOME": str(tmp_path)}
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    return e


def profile(env, notify=True, panes: dict | None = None, **notify_extra):
    doc: dict = {}
    if notify:
        doc["roles"] = {"notify": {"adapter": "ntfy", "url": "https://ntfy.example.org/t", **notify_extra}}
    if panes is not None:
        doc["watch"] = {"panes": panes}
    p = profile_path(env)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps(doc), encoding="utf-8")


class Sender:
    def __init__(self, status: int = 200):
        self.sent: list[str] = []
        self.status = status

    def __call__(self, url, body, headers):
        self.sent.append(body.decode() if isinstance(body, bytes) else body)
        return self.status


class Tmux:
    """A tmux server with named windows whose screens a test sets; it records every command."""

    def __init__(self):
        self.screens: dict[str, str] = {}
        self.windows: dict[str, tuple[str, str]] = {}  # pane -> (window id, window name)
        self.calls: list[list[str]] = []
        self.up = True

    def add(self, pane: str, window_id: str, name: str, screen: str = PLAIN) -> None:
        self.screens[pane] = screen
        self.windows[pane] = (window_id, name)

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if not self.up:
            return 1, "no server running"
        if argv[0] == "list-panes":
            rows = [f"{p}\t{w}\t0\t{n}" for p, (w, n) in self.windows.items()]
            return 0, "\n".join(rows) + "\n"
        if argv[0] == "capture-pane":
            return 0, self.screens.get(argv[argv.index("-t") + 1], "")
        if argv[0] == "send-keys":
            pane = argv[argv.index("-t") + 1]
            if argv[-1] == "Escape" and "Remote Control" in self.screens.get(pane, ""):
                self.screens[pane] = PLAIN  # Esc closes the box
            return 0, ""
        return 1, "unknown"

    def keys(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "send-keys"]


def watch(env, t, now, sender, **kw):
    return panewatch.run_once(env, run=t, now=now, sender=sender, **kw)


# --- reading a pane --------------------------------------------------------------------------------------------------


def test_a_question_footer_is_a_dialog_and_its_first_line_is_the_question():
    d = panewatch.find_dialog(ASK.splitlines())
    assert d is not None and d.first == "Which approach should I take?" and not d.remote_control
    assert d.pattern == "Enter to select"


@pytest.mark.parametrize(
    "footer",
    [
        "Enter to select",
        "Esc to cancel",
        "Esc to continue",
        "Enter to confirm",
        "Do you want to proceed?",
        "ESC TO CANCEL",
    ],
)
def test_every_footer_pattern_is_found_whatever_its_case(footer):
    assert panewatch.find_dialog(["Some title", "", f"  {footer} · more"]) is not None


def test_ordinary_output_is_not_a_dialog():
    assert panewatch.find_dialog(PLAIN.splitlines()) is None
    assert panewatch.find_dialog([]) is None


def test_the_remote_control_box_needs_all_three_parts():
    assert panewatch.find_dialog(REMOTE.splitlines()).remote_control
    for part in panewatch.REMOTE_CONTROL_PARTS:
        assert (
            not panewatch.find_dialog(REMOTE.replace(part, "x").splitlines()).remote_control
            or part == "Esc to continue"
        )
    assert not panewatch.find_dialog(REMOTE.replace("Disconnect this session", "Leave").splitlines()).remote_control
    assert not panewatch.find_dialog(REMOTE.replace("Remote Control", "Mobile").splitlines()).remote_control


def test_only_the_last_twenty_lines_are_read():
    t = Tmux()
    t.add("%1", "@1", "w", "Esc to cancel\n" + "\n".join(f"line {i}" for i in range(30)) + "\n\n\n")
    assert panewatch.tail(t, "%1") == [f"line {i}" for i in range(10, 30)]
    t.screens["%1"] = "a\nb\n\n   \n"
    assert panewatch.tail(t, "%1") == ["a", "b"]


# --- one push for an episode -----------------------------------------------------------------------------------------


def test_a_dialog_gets_one_push_after_ten_minutes_naming_the_window_and_the_line(env):
    profile(env)
    t, s = Tmux(), Sender()
    t.add("%3", "@7", "ws-private-lead", ASK)
    start = 1_000_000.0
    for minutes in (0, 1, 5, 9):
        assert watch(env, t, start + minutes * MIN, s).pushed == []
    assert s.sent == []
    report = watch(env, t, start + 10 * MIN, s)
    assert report.pushed == ["%3"] and len(s.sent) == 1
    for word in ("ws-private-lead", "@7", "10 min", "Which approach should I take?"):
        assert word in s.sent[0]
    for minutes in (11, 12, 60, 180):  # still there: no second push
        watch(env, t, start + minutes * MIN, s)
    assert len(s.sent) == 1
    assert t.keys() == []  # never a key on a question


def test_the_episode_ends_when_the_dialog_goes_and_a_new_one_is_a_new_push(env):
    profile(env)
    t, s = Tmux(), Sender()
    t.add("%3", "@7", "w", ASK)
    start = 5_000_000.0
    watch(env, t, start, s)
    watch(env, t, start + 11 * MIN, s)
    assert len(s.sent) == 1 and panewatch.read_state(env)
    t.screens["%3"] = PLAIN
    watch(env, t, start + 12 * MIN, s)
    assert panewatch.read_state(env) == {}
    t.screens["%3"] = ASK
    watch(env, t, start + 20 * MIN, s)
    assert len(s.sent) == 1  # the new dialog's clock starts now
    watch(env, t, start + 31 * MIN, s)
    assert len(s.sent) == 2


def test_a_different_dialog_in_the_same_pane_restarts_the_clock(env):
    profile(env)
    t, s = Tmux(), Sender()
    t.add("%3", "@7", "w", ASK)
    start = 9_000_000.0
    watch(env, t, start, s)
    t.screens["%3"] = ASK.replace("Which approach should I take?", "Which file should I edit?")
    watch(env, t, start + 9 * MIN, s)
    watch(env, t, start + 12 * MIN, s)  # 12 min since the first, 3 since the second
    assert s.sent == []
    watch(env, t, start + 19 * MIN, s)
    assert len(s.sent) == 1 and "Which file should I edit?" in s.sent[0]


def test_each_pane_has_its_own_clock(env):
    profile(env)
    t, s = Tmux(), Sender()
    t.add("%1", "@1", "old", ASK)
    start = 2_000_000.0
    watch(env, t, start, s)
    t.add("%2", "@2", "new", ASK.replace("Which approach", "Which other approach"))
    report = watch(env, t, start + 10 * MIN, s)
    assert report.pushed == ["%1"] and "old" in s.sent[0]


def test_after_minutes_and_more_patterns_come_from_the_profile(env):
    profile(env, panes={"after_minutes": 3, "patterns": ["Press any key"]})
    t, s = Tmux(), Sender()
    t.add("%1", "@1", "w", "Menu\n\nPress any key to go on\n")
    start = 3_000_000.0
    watch(env, t, start, s)
    assert watch(env, t, start + 2 * MIN, s).pushed == []
    assert watch(env, t, start + 3 * MIN, s).pushed == ["%1"]
    cfg = panewatch.load_config(env)
    assert cfg.after_minutes == 3 and cfg.patterns[: len(panewatch.DIALOG_PATTERNS)] == panewatch.DIALOG_PATTERNS
    assert cfg.patterns[-1] == "Press any key"


@pytest.mark.parametrize(
    "bad",
    [{"after_minutes": 0}, {"after_minutes": "5"}, {"after_minutes": True}, {"after_minutes": 5000}, {"patterns": "x"}]
    + [{"patterns": [""]}, {"live_socket": 3}, {"show_text": "no"}],
)
def test_a_bad_table_is_a_typed_error(env, bad):
    profile(env, panes=bad)
    with pytest.raises(WsError) as e:
        panewatch.load_config(env)
    assert e.value.kind == "bad_config" and "[watch.panes]" in e.value.message


def test_show_text_false_leaves_the_dialog_out_of_the_push(env):
    profile(env, panes={"show_text": False})
    t, s = Tmux(), Sender()
    t.add("%3", "@7", "w", ASK)
    watch(env, t, 0.0, s)
    watch(env, t, 11 * MIN, s)
    assert len(s.sent) == 1 and "Which approach" not in s.sent[0] and "@7" in s.sent[0]


# --- the push can fail, be capped or have nowhere to go ------------------------------------------------------------------


def test_a_failed_send_is_tried_again_and_a_missing_role_is_not(env):
    profile(env)
    t = Tmux()
    t.add("%3", "@7", "w", ASK)
    bad = Sender(status=500)
    watch(env, t, 0.0, bad)
    assert watch(env, t, 11 * MIN, bad).pushed == []  # not sent: not marked
    good = Sender()
    assert watch(env, t, 12 * MIN, good).pushed == []  # ntfy pauses alerts for 5 minutes after a failed send
    (paths.state_dir(env) / ntfy.LAST_FILE).unlink()  # the five minutes have passed (ntfy keeps real time)
    assert watch(env, t, 17 * MIN, good).pushed == ["%3"] and len(good.sent) == 1
    # no notify role: a log line, and it does not try again
    profile(env, notify=False)
    t2 = Tmux()
    t2.add("%4", "@8", "x", ASK)
    first = watch(env, t2, 200 * MIN, good)
    later = watch(env, t2, 212 * MIN, good)
    assert first.pushed == [] and later.pushed == ["%4"] and len(good.sent) == 1
    assert watch(env, t2, 213 * MIN, good).pushed == []
    events = [json.loads(x) for x in jobwatch.alerts_path(env).read_text().splitlines()]
    assert [e["event"] for e in events] == ["dialog-stuck"] * 4 and events[-1]["pushed"] is False


def test_the_daily_cap_is_respected(env):
    profile(env, alerts_per_day=1)
    t, s = Tmux(), Sender()
    t.add("%1", "@1", "a", ASK)
    t.add("%2", "@2", "b", ASK.replace("Which approach", "Which other approach"))
    watch(env, t, 0.0, s)
    watch(env, t, 11 * MIN, s)
    assert len(s.sent) == 1  # the second hit the cap: nothing sent, and it is tried again, never forced
    assert len(panewatch.read_state(env)) == 2
    watch(env, t, 12 * MIN, s)
    assert len(s.sent) == 1


# --- the one key ---------------------------------------------------------------------------------------------------------


def test_escape_goes_only_to_the_remote_control_box_after_a_minute_and_once(env):
    profile(env)
    t, s = Tmux(), Sender()
    t.add("%5", "@9", "ws-lead", REMOTE)
    start = 7_000_000.0
    assert watch(env, t, start, s).pressed == [] and t.keys() == []
    assert watch(env, t, start + 30, s).pressed == [] and t.keys() == []
    report = watch(env, t, start + 60, s)
    assert report.pressed == ["%5"]
    assert t.keys() == [["send-keys", "-t", "%5", "Escape"]]
    assert len(s.sent) == 1 and "ws-lead" in s.sent[0] and "@9" in s.sent[0] and "Remote Control" in s.sent[0]
    events = [json.loads(x) for x in jobwatch.alerts_path(env).read_text().splitlines()]
    assert events[0]["event"] == "escape-sent" and events[0]["pane"] == "%5" and events[0]["pushed"] is True
    watch(env, t, start + 120, s)  # the box is gone: the episode is over, nothing more is sent
    assert len(t.keys()) == 1 and len(s.sent) == 1 and panewatch.read_state(env) == {}


def test_escape_is_pressed_once_for_one_episode_even_if_the_box_stays(env):
    profile(env)

    class Stubborn(Tmux):
        def __call__(self, argv):
            if argv[0] == "send-keys":
                self.calls.append(argv)
                return 0, ""  # Esc does nothing
            return super().__call__(argv)

    t, s = Stubborn(), Sender()
    t.add("%5", "@9", "w", REMOTE)
    for minutes in range(15):
        watch(env, t, minutes * MIN, s)
    assert len(t.keys()) == 1
    assert len(s.sent) == 2  # the Esc notice, and the ten-minute push that says it is still there


def test_the_screen_is_read_again_before_escape(env):
    profile(env)

    class Gone(Tmux):
        reads = 0

        def __call__(self, argv):
            if argv[0] == "capture-pane":
                Gone.reads += 1
                if Gone.reads > 1:  # the person closed the box between the two reads
                    self.screens["%5"] = PLAIN
            return super().__call__(argv)

    t, s = Gone(), Sender()
    t.add("%5", "@9", "w", REMOTE)
    watch(env, t, 0.0, s)
    Gone.reads = 0
    assert watch(env, t, 61.0, s).pressed == [] and t.keys() == []


@pytest.mark.parametrize(
    "screen",
    [ASK, REMOTE.replace("Disconnect this session", "Disconnect"), REMOTE.replace("Esc to continue", "Esc to cancel")]
    + ["Do you want to proceed?\n  ❯ 1. Yes\n    2. No\n\n Enter to confirm · Esc to cancel\n"],
)
def test_no_key_is_ever_sent_to_any_other_dialog(env, screen):
    profile(env)
    t, s = Tmux(), Sender()
    t.add("%1", "@1", "w", screen)
    for minutes in (0, 1, 2, 10, 30, 600):
        watch(env, t, minutes * MIN, s)
    assert t.keys() == []
    assert len(s.sent) == 1  # the one push for the episode


def test_send_keys_has_one_call_site_and_it_sends_escape():
    import inspect

    src = inspect.getsource(panewatch)
    assert src.count('"send-keys"') == 1
    assert 'run(["send-keys", "-t", pane_id, "Escape"])' in src


# --- the server and the state --------------------------------------------------------------------------------------------


def test_no_server_clears_the_state_and_says_nothing(env):
    profile(env)
    t, s = Tmux(), Sender()
    t.add("%3", "@7", "w", ASK)
    watch(env, t, 0.0, s)
    assert panewatch.read_state(env)
    t.up = False
    report = watch(env, t, 60.0, s)
    assert report.dialogs == [] and panewatch.read_state(env) == {} and s.sent == []


def test_a_damaged_state_file_starts_again(env):
    profile(env)
    panewatch.state_path(env).parent.mkdir(parents=True, exist_ok=True)
    panewatch.state_path(env).write_text("{not json")
    t, s = Tmux(), Sender()
    t.add("%3", "@7", "w", ASK)
    watch(env, t, 0.0, s)
    st = json.loads(panewatch.state_path(env).read_text())
    assert st["schema"] == 1 and set(st["panes"]) == {"%3"} and st["panes"]["%3"]["first_seen"] == 0.0
    assert oct(os.stat(panewatch.state_path(env)).st_mode & 0o777) == "0o600"
    assert not [p for p in panewatch.state_path(env).parent.iterdir() if p.name.startswith(".")]  # no temp file left


def test_a_pane_that_is_gone_leaves_the_state(env):
    profile(env)
    t, s = Tmux(), Sender()
    t.add("%3", "@7", "w", ASK)
    watch(env, t, 0.0, s)
    del t.windows["%3"]
    watch(env, t, 60.0, s)
    assert panewatch.read_state(env) == {}


def test_a_dead_pane_is_skipped():
    t = Tmux()
    t.add("%3", "@7", "w", ASK)
    rows = "%3\t@7\t1\tw\n%4\t@8\t0\tlive\n"
    assert panewatch.list_panes(lambda argv: (0, rows)) == [panewatch.PaneInfo("%4", "@8", "live")]
    assert panewatch.list_panes(lambda argv: (1, "")) is None


def test_the_live_socket_is_the_profiles_then_the_policy_then_the_default(env, tmp_path):
    env = {**env, "TMUX_TMPDIR": str(tmp_path / "tmux")}
    profile(env)
    base = tmp_path / "tmux" / f"tmux-{os.getuid()}"
    assert panewatch.socket_path(panewatch.Config(), env) == base / "default"
    assert panewatch.socket_path(panewatch.Config(live_socket="rc"), env) == base / "rc"
    (tmp_path / "ws" / "policy.toml").write_text(
        '[guards]\non = ["pane-keys"]\n[guards.pane-keys]\nlive_socket = "fromguard"\n'
    )
    assert panewatch.socket_path(panewatch.Config(), env) == base / "fromguard"
    assert panewatch.socket_path(panewatch.Config(live_socket="rc"), env) == base / "rc"


# --- the command ---------------------------------------------------------------------------------------------------------


def test_the_command_reports_a_dialog_and_exits_zero(env, monkeypatch, capsys):
    profile(env)
    t = Tmux()
    t.add("%3", "@7", "ws-lead", ASK)
    monkeypatch.setattr(panewatch.shutil, "which", lambda name, *a, **k: "/usr/bin/tmux")
    monkeypatch.setattr(panewatch.tmux, "runner", lambda prefix=None, env=None: t)
    assert cli.main(["watch", "panes"]) == 0
    assert "ws-lead (@7) on a dialog for 1 min" in capsys.readouterr().out
    assert cli.main(["--json", "watch", "panes", "--after-minutes", "1"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["dialogs"][0]["window"] == "ws-lead"
    assert cli.main(["watch", "panes", "--after-minutes", "0"]) == 2


def test_inert_without_tmux(env, monkeypatch, capsys):
    profile(env)
    monkeypatch.setattr(panewatch.shutil, "which", lambda *a, **k: None)
    assert cli.main(["watch", "panes"]) == 0
    assert capsys.readouterr().out == ""
