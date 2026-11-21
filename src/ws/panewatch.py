"""`ws watch panes`: tell the person when a tmux pane has sat on a dialog for minutes, so no session waits unseen.

    ws watch panes [--after-minutes N]

Why. A Claude Code dialog (a question to answer, a permission to give, the Remote Control status box) holds every key and
every queued message until someone presses one. A session behind one looks idle from outside, and two sat that way for
hours. `pane-keys` stops a session opening one by typing a slash command; this is the net under it for the rest.

Each run (a timer fires it every minute; see ws.roles.systemd and ws.roles.launchd) reads the last 20 lines of every pane
on the live tmux server and looks for a dialog's footer (`DIALOG_PATTERNS`: "Enter to select", "Esc to cancel", "Esc to
continue", "Enter to confirm", "Do you want to proceed?", case ignored). The profile's `[watch.panes]` table can add
patterns (`patterns = [...]`), set `after_minutes` (default 10), name the tmux socket (`live_socket`, else the one the
policy gives pane-keys or pane-close, else this user's default) and turn the dialog's text out of the push
(`show_text = false`). A small state file, `<state>/logs/panes-watch.json`, holds when each pane's dialog was first seen and a
fingerprint of it (its footer and first meaningful line), written whole or not at all. A dialog that changes is a new one.

What it does about one, in full.
  * After `after_minutes` on the same dialog it sends ONE push for that episode through the notify role (the alert cap
    applies; a push that failed is tried again next run), naming the window, its `@id`, how long it has been up and the
    dialog's first meaningful line. The episode ends when the dialog goes, and the next one is a new push.
  * The only key it ever sends is `Escape`, and only to a pane that shows the Remote Control status dialog (all of "Remote
    Control", "Disconnect this session" and "Esc to continue" present), once the dialog has been up a minute and is read
    again just before. Esc there only closes the box. It logs that (a line in `<state>/logs/alerts.jsonl`) and pushes once. It never
    presses anything on any other dialog: a question, a permission or a plan is the person's to answer, and a push is all
    it does about them. `press_escape` is the one place a key is sent, and a test pins that.

It runs on the machine it watches and reads only the screen text of panes, never a transcript. The push carries a line of a
dialog, which can be the question a session asks: `show_text = false` leaves it out for a machine where that must not leave
the box. No notify role means a log line only. Inert without tmux, and with no tmux server running it clears its state and
says nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ws import install, jobwatch, output, paths
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path
from ws.term import tmux

SCHEMA_VERSION = 1  # of the state file
DIALOG_PATTERNS = (
    "Enter to select",
    "Esc to cancel",
    "Esc to continue",
    "Enter to confirm",
    "Do you want to proceed?",
)
REMOTE_CONTROL_PARTS = ("Remote Control", "Disconnect this session", "Esc to continue")
TAIL_LINES = 20
DEFAULT_AFTER_MINUTES = 10
MAX_AFTER_MINUTES = 1440
ESCAPE_AFTER_S = 60  # the Remote Control box stays this long before Escape is pressed
SLACK_S = 5  # a timer fires a little early or late, so "a minute" is a minute less this
LINE_MAX = 100
_RULE = re.compile(r"^[\s─━═\-_=]{10,}$")  # a horizontal rule: where a dialog's box begins
_DECOR = str.maketrans("", "", "│┃║╭╮╰╯┌┐└┘├┤─━═❯›")  # box lines and the marker before a choice


@dataclass(frozen=True)
class PaneInfo:
    pane_id: str
    window_id: str
    window: str


@dataclass(frozen=True)
class Dialog:
    pattern: str  # the pattern of `DIALOG_PATTERNS` (or the profile's) that matched the footer
    first: str  # the dialog's first meaningful line
    remote_control: bool  # the Remote Control status box, whose Esc is a no-op

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.pattern}\n{self.first}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Config:
    after_minutes: int = DEFAULT_AFTER_MINUTES
    patterns: tuple[str, ...] = DIALOG_PATTERNS
    live_socket: str = ""  # "" = the policy's, else the default socket
    show_text: bool = True


@dataclass
class Report:
    """What one run saw: each pane on a dialog, and what was done about it."""

    dialogs: list[dict] = field(default_factory=list)
    pressed: list[str] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)


def state_path(env=None) -> Path:
    return paths.state_dir(env) / "logs" / "panes-watch.json"


# --- config ----------------------------------------------------------------------------------------------------------


def load_config(env=None) -> Config:
    """`[watch.panes]` of the profile: `after_minutes` (1 to 1440), `patterns` (more footer text), `live_socket`,
    `show_text`. A bad value is a typed error naming the key."""
    e = os.environ if env is None else env
    try:
        prof = load(profile_path(dict(e))) or {}
    except ProfileError as err:
        raise WsError("bad_config", str(err), "fix the profile", 1) from err
    watch = prof.get("watch") if isinstance(prof.get("watch"), dict) else {}
    table = watch.get("panes") if isinstance(watch.get("panes"), dict) else {}
    after = table.get("after_minutes", DEFAULT_AFTER_MINUTES)
    if isinstance(after, bool) or not isinstance(after, int) or not 1 <= after <= MAX_AFTER_MINUTES:
        raise WsError(
            "bad_config",
            f"[watch.panes] after_minutes must be a whole number from 1 to {MAX_AFTER_MINUTES}",
            "fix the profile",
            1,
        )
    extra = table.get("patterns", [])
    if not isinstance(extra, list) or not all(isinstance(p, str) and p.strip() for p in extra):
        raise WsError("bad_config", "[watch.panes] patterns must be a list of non-empty text", "fix the profile", 1)
    live = table.get("live_socket", "")
    if not isinstance(live, str) or "\0" in live:
        raise WsError("bad_config", "[watch.panes] live_socket must be text", "fix the profile", 1)
    show = table.get("show_text", True)
    if not isinstance(show, bool):
        raise WsError("bad_config", "[watch.panes] show_text must be true or false", "fix the profile", 1)
    return Config(after, (*DIALOG_PATTERNS, *dict.fromkeys(p.strip() for p in extra)), live, show)


def socket_path(cfg: Config, env) -> Path:
    """The live server's socket: the profile's `live_socket`, else the one a pane guard names in the policy, else the
    user's default. A bare name is under the tmux folder."""
    from ws import policy
    from ws.guards import pane_close

    live = cfg.live_socket
    if not live:
        try:
            pol = policy.load(policy.policy_path(dict(env)))
            for guard in ("pane-keys", "pane-close"):
                found = getattr(pol.guards.config.get(guard), "live_socket", "")
                if found:
                    live = found
                    break
        except policy.PolicyError:
            live = ""
    return pane_close._live_socket(pane_close.Config(live), dict(env))


# --- reading panes ---------------------------------------------------------------------------------------------------


def list_panes(run: tmux.Run) -> list[PaneInfo] | None:
    """Every live pane on the server, or None when the server doesn't answer."""
    code, out = run(["list-panes", "-a", "-F", "#{pane_id}\t#{window_id}\t#{pane_dead}\t#{window_name}"])
    if code != 0:
        return None
    found = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4 and parts[0].startswith("%") and parts[1].startswith("@") and parts[2] != "1":
            found.append(PaneInfo(parts[0], parts[1], parts[3]))
    return found


def tail(run: tmux.Run, pane_id: str) -> list[str] | None:
    """The last `TAIL_LINES` lines of the pane's screen with the blank ones at the foot cut off, or None."""
    code, out = run(["capture-pane", "-p", "-J", "-t", pane_id])
    if code != 0:
        return None
    lines = [ln.rstrip() for ln in out.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-TAIL_LINES:]


def find_dialog(lines: list[str], patterns: tuple[str, ...] = DIALOG_PATTERNS) -> Dialog | None:
    """The dialog the lines end in, or None. It is the last line that holds a footer pattern; its text runs from the
    rule above it (else the eight lines before) down to that footer, and the first line of that with a word in it, once
    box and marker characters are taken out, is what the push quotes."""
    low = [p.lower() for p in patterns]
    at = next((i for i in range(len(lines) - 1, -1, -1) if any(p in lines[i].lower() for p in low)), None)
    if at is None:
        return None
    pattern = next(p for p, lp in zip(patterns, low, strict=True) if lp in lines[at].lower())
    start = next((i + 1 for i in range(at - 1, -1, -1) if _RULE.match(lines[i])), max(0, at - 8))
    first = ""
    for line in lines[start : at + 1]:
        text = line.translate(_DECOR).strip()
        if text and re.search(r"\w", text):
            first = text
            break
    whole = "\n".join(lines).lower()
    rc = all(part.lower() in whole for part in REMOTE_CONTROL_PARTS)
    return Dialog(pattern, first[:LINE_MAX], rc)


def press_escape(run: tmux.Run, pane_id: str) -> bool:
    """Press Escape in one pane. The only key this module ever sends, and the only call that sends one."""
    code, _ = run(["send-keys", "-t", pane_id, "Escape"])
    return code == 0


# --- state -----------------------------------------------------------------------------------------------------------


def read_state(env) -> dict[str, dict]:
    try:
        doc = json.loads(state_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    panes = doc.get("panes") if isinstance(doc, dict) else None
    return {k: v for k, v in panes.items() if isinstance(v, dict)} if isinstance(panes, dict) else {}


def write_state(env, panes: dict[str, dict]) -> None:
    path = state_path(env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    install.write_atomic(path, json.dumps({"schema": SCHEMA_VERSION, "panes": panes}).encode(), 0o600)


def _log(env, rec: dict) -> None:
    """One line in the scheduler's alerts log (`<state>/logs/alerts.jsonl`, kind "not-automatic"), beside the job alerts:
    one place to look for what ws told the person without being asked."""
    jobwatch._write_line(env, rec)


# --- one run ---------------------------------------------------------------------------------------------------------


def _minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))


def run_once(
    env=None,
    cfg: Config | None = None,
    run: tmux.Run | None = None,
    now: float | None = None,
    sender=None,
) -> Report:
    """Look at every pane once; push, and press Escape on the Remote Control box, as the module docstring says."""
    e = os.environ if env is None else env
    cfg = cfg or load_config(e)
    now = time.time() if now is None else now
    report = Report()
    if run is None:
        if shutil.which("tmux") is None:
            return report
        clean = {k: v for k, v in e.items() if k not in ("TMUX", "TMUX_PANE")}
        run = tmux.runner(["-S", str(socket_path(cfg, e))], clean)
    panes = list_panes(run)
    if panes is None:  # no server: every episode is over
        if read_state(e):
            write_state(e, {})
        return report
    before, after = read_state(e), {}
    for pane in panes:
        lines = tail(run, pane.pane_id)
        dialog = find_dialog(lines, cfg.patterns) if lines else None
        if dialog is None:
            continue
        old = before.get(pane.pane_id, {})
        same = old.get("fp") == dialog.fingerprint and isinstance(old.get("first_seen"), (int, float))
        entry = {**old} if same else {"first_seen": now, "fp": dialog.fingerprint}
        entry.update(window=pane.window, window_id=pane.window_id)
        age = now - entry["first_seen"]
        after[pane.pane_id] = entry
        label = f"{pane.window} ({pane.window_id})"
        shown = {"pane": pane.pane_id, "window": pane.window, "window_id": pane.window_id, "minutes": _minutes(age)}
        report.dialogs.append({**shown, "remote_control": dialog.remote_control, "first": dialog.first})

        if dialog.remote_control and not entry.get("escaped") and age >= ESCAPE_AFTER_S - SLACK_S:
            again = tail(run, pane.pane_id)
            still = find_dialog(again, cfg.patterns) if again else None
            if still is not None and still.remote_control and press_escape(run, pane.pane_id):
                entry["escaped"] = True
                report.pressed.append(pane.pane_id)
                sent = jobwatch.push(
                    e,
                    f"{label}: the Remote Control box had been up {_minutes(age)} min, so Esc was pressed to close it",
                    "a session sat on a status box; Esc closed it",
                    sender,
                    title="ws panes",
                )
                _log(e, {"event": "escape-sent", **shown, "pushed": sent is True})
        if age >= cfg.after_minutes * 60 - SLACK_S and not entry.get("pushed"):
            quote = f": {dialog.first}" if cfg.show_text and dialog.first else ""
            text = f"{label} has been on a dialog for {_minutes(age)} min{quote}"
            sent = jobwatch.push(e, text, "a session has been on a dialog for a while", sender, title="ws panes")
            _log(e, {"event": "dialog-stuck", **shown, "pushed": sent is True})
            if sent is not False:  # a failed send is tried again next run; no notify role is not retried
                entry["pushed"] = True
                report.pushed.append(pane.pane_id)
    if after != before:
        write_state(e, after)
    return report


# --- command ---------------------------------------------------------------------------------------------------------


def cmd_panes(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.after_minutes is not None:
        if not 1 <= args.after_minutes <= MAX_AFTER_MINUTES:
            raise WsError(
                "bad_usage", f"--after-minutes must be from 1 to {MAX_AFTER_MINUTES}", "give whole minutes", 2
            )
        cfg = Config(args.after_minutes, cfg.patterns, cfg.live_socket, cfg.show_text)
    report = run_once(cfg=cfg)
    for d in report.dialogs:
        did = " (Esc pressed)" if d["pane"] in report.pressed else " (pushed)" if d["pane"] in report.pushed else ""
        print(f"ws: {d['window']} ({d['window_id']}) on a dialog for {d['minutes']} min{did}")
    if getattr(args, "json", False):
        output.result({"dialogs": report.dialogs, "pressed": report.pressed, "pushed": report.pushed})
    return 0


def register(ws: argparse._SubParsersAction) -> None:
    """The `panes` sub-command of `ws watch` (jobwatch.register calls this)."""
    p = ws.add_parser(
        "panes", help="push when a tmux pane has sat on a dialog for minutes; press Esc only on the Remote Control box"
    )
    p.add_argument(
        "--after-minutes",
        type=int,
        metavar="N",
        help=f"minutes on a dialog before the push (default {DEFAULT_AFTER_MINUTES})",
    )
    p.set_defaults(func=cmd_panes)
