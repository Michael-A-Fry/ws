"""`ws migrate`: plan, copy, check, record, rename, refresh. Everything lives under a tmp HOME; the environment is passed
in as a dict (or set on the process for the commands that read it), so nothing touches the real home."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ws import approvals, cli, gate, migrate, paths, policy
from ws.output import WsError

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    return h


@pytest.fixture
def env(home):
    return {"HOME": str(home)}


def write(path: Path, text: str = "x", mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


def make_old_home(home: Path) -> Path:
    """An earlier ws's `~/.ws` with a file or two of every kind."""
    old = home / ".ws"
    write(old / "policy.toml", "[gate]\n")
    write(old / "profile.toml", '[machine]\nserver = "none"\n')
    write(old / "secrets.toml", "")
    write(old / "jobs" / "daily.toml", 'runner = "x"\n')
    write(old / "jobs" / "units" / "ws-daily.service", "[Service]\n")
    write(old / "secrets" / "KEY", "not-a-real-value", 0o600)
    (old / "secrets").chmod(0o700)
    write(old / "logs" / "runs.jsonl", '{"schema":1}\n')
    write(old / "approvals" / "pending" / "a.json", "{}", 0o600)
    (old / "approvals").chmod(0o700)
    write(old / "sessions" / "s1.json", "{}")
    write(old / "ratings.jsonl", '{"schema":1}\n')
    write(old / "eval" / "tasks" / "t.toml", "id = 't'\n")
    write(old / "index" / "memory.sqlite", "not a database")
    write(old / "gate" / "claude-code" / "settings.json", "{}")
    write(old / "release", "r1")
    return old


def tree(root: Path) -> dict[str, tuple]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = ("link", os.readlink(p))
        elif p.is_dir():
            out[rel] = ("dir", stat.S_IMODE(p.stat().st_mode))
        elif stat.S_ISSOCK(p.lstat().st_mode):
            out[rel] = ("socket",)
        else:
            out[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest(), stat.S_IMODE(p.stat().st_mode))
    return out


def stub_refresh(calls):
    def refresh(e):
        calls.append(dict(e))
        return "0 changes"

    return refresh


# ---- the plan and the dry run -----------------------------------------------------------------------------------------


def test_a_dry_run_says_what_would_move_and_changes_nothing(home, env):
    old = make_old_home(home)
    before = tree(home)
    data = migrate.migrate(env, dry_run=True, now=NOW)
    assert data["state"] == "move" and data["dry_run"] is True
    assert data["renamed_to"] == ".ws.migrated-20261107"
    assert data["files"] == 14 and data["folders"]["config"] == str(home / ".config" / "ws")
    assert {"from": str(old / "policy.toml"), "to": str(home / ".config" / "ws" / "policy.toml")} in data["would_move"]
    assert tree(home) == before and not (home / ".config").exists()


def test_each_file_goes_to_the_folder_the_table_names(home, env):
    make_old_home(home)
    migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    cfg, state = home / ".config" / "ws", home / ".local" / "state" / "ws"
    data, cache = home / ".local" / "share" / "ws", home / ".cache" / "ws"
    for p in (
        cfg / "policy.toml", cfg / "profile.toml", cfg / "secrets.toml", cfg / "jobs" / "daily.toml",
        cfg / "jobs" / "units" / "ws-daily.service", cfg / "release",
        state / "logs" / "runs.jsonl", state / "approvals" / "pending" / "a.json", state / "sessions" / "s1.json",
        state / "ratings.jsonl", state / "gate" / "claude-code" / "settings.json",
        data / "secrets" / "KEY", data / "eval" / "tasks" / "t.toml",
        cache / "index" / "memory.sqlite",
    ):  # fmt: skip
        assert p.is_file(), p
    assert (cfg / "policy.toml").read_text(encoding="utf-8") == "[gate]\n"
    assert paths.layout(env).mode == "xdg"


# ---- the move --------------------------------------------------------------------------------------------------------


def test_the_move_keeps_every_mode_and_the_secrets_stay_private(home, env):
    old = make_old_home(home)
    before = tree(old)
    migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    data = home / ".local" / "share" / "ws"
    assert stat.S_IMODE((data / "secrets" / "KEY").stat().st_mode) == 0o600
    assert stat.S_IMODE((data / "secrets").stat().st_mode) == 0o700
    state = home / ".local" / "state" / "ws"
    assert stat.S_IMODE((state / "approvals").stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "approvals" / "pending" / "a.json").stat().st_mode) == 0o600
    for d in (home / ".config" / "ws", state, data, home / ".cache" / "ws"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700  # ws's own folders start private
    kept = tree(home / ".ws.migrated-20261107")
    # the old folder is renamed with nothing touched or deleted, except the secrets store, which moved by rename
    assert kept == {k: v for k, v in before.items() if k != "secrets" and not k.startswith("secrets/")}


def test_the_old_folder_is_renamed_never_deleted_and_a_manifest_lists_what_moved(home, env):
    old = make_old_home(home)
    out = migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert not old.exists() and (home / ".ws.migrated-20261107").is_dir()
    assert out["state"] == "moved" and out["renamed_to"] == str(home / ".ws.migrated-20261107")
    manifest = home / ".local" / "state" / "ws" / "migrate-manifest.json"
    assert out["manifest"] == str(manifest) and stat.S_IMODE(manifest.stat().st_mode) == 0o600
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    assert doc["schema"] == 1 and doc["source"] == str(old) and doc["renamed_to"] == out["renamed_to"]
    key = next(m for m in doc["moved"] if m["to"].endswith("/secrets/KEY"))
    assert key["sha256"] == hashlib.sha256(b"not-a-real-value").hexdigest() and key["mode"] == "0600"


def test_a_name_ws_does_not_know_and_a_socket_stay_behind_and_are_listed(home, env):
    old = make_old_home(home)
    write(old / "my-notes.txt", "mine")
    (old / "run").mkdir()
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(old / "run" / "b.sock"))
    except OSError:
        pytest.skip("this system has no Unix sockets in this folder")
    finally:
        sock.close()
    out = migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert any("my-notes.txt" in line for line in out["left"]) and any("b.sock" in line for line in out["left"])
    assert (home / ".ws.migrated-20261107" / "my-notes.txt").read_text(encoding="utf-8") == "mine"
    assert not (home / ".config" / "ws" / "my-notes.txt").exists()
    assert (home / ".local" / "state" / "ws" / "run").is_dir() and not list(
        (home / ".local" / "state" / "ws" / "run").iterdir()
    )


def test_a_link_is_kept_as_a_link(home, env):
    old = make_old_home(home)
    (old / "jobs" / "latest.toml").symlink_to("daily.toml")
    migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    link = home / ".config" / "ws" / "jobs" / "latest.toml"
    assert link.is_symlink() and os.readlink(link) == "daily.toml"


def test_the_install_record_follows_the_files(home, env):
    old = make_old_home(home)
    other = home / "Notes"
    doc = {
        "schema": 1,
        "roots": [str(old), str(other)],
        "entries": [
            {"path": str(old / "policy.toml"), "kind": "file", "sha256": "ab", "created_by_ws": True},
            {"path": str(old / "gate"), "kind": "dir", "sha256": None, "created_by_ws": True},
            {"path": str(other / "_inbox"), "kind": "dir", "sha256": None, "created_by_ws": True},
            {"path": "/x/settings.json", "kind": "merge", "added": [{"value": f"--policy {old}/policy.toml"}]},
        ],
    }
    write(old / "install-manifest.json", json.dumps(doc))
    calls: list = []
    out = migrate.migrate(env, now=NOW, refresh=stub_refresh(calls))
    new = json.loads((home / ".local" / "state" / "ws" / "install-manifest.json").read_text(encoding="utf-8"))
    cfg, state = home / ".config" / "ws", home / ".local" / "state" / "ws"
    by_path = {e["path"]: e for e in new["entries"]}
    assert (
        by_path[str(cfg / "policy.toml")]["created_by_ws"] is True
        and by_path[str(cfg / "policy.toml")]["sha256"] == "ab"
    )
    assert (
        str(state / "gate") in by_path and str(old / "policy.toml") not in by_path and str(old / "gate") not in by_path
    )
    assert str(other / "_inbox") in by_path  # a folder that was never ws's own is left alone
    assert str(cfg) in new["roots"] and str(other) in new["roots"] and str(old) not in new["roots"]
    assert by_path["/x/settings.json"]["added"] == doc["entries"][3]["added"]
    assert calls and out["refreshed"] == "0 changes"  # the runners' hooks are refreshed because the policy path moved


def test_no_refresh_when_nothing_was_installed(home, env):
    make_old_home(home)
    calls: list = []
    out = migrate.migrate(env, now=NOW, refresh=stub_refresh(calls))
    assert calls == [] and "refreshed" not in out


def test_a_failed_refresh_leaves_the_move_done_and_says_what_to_run(home, env):
    old = make_old_home(home)
    write(old / "install-manifest.json", json.dumps({"schema": 1, "roots": [], "entries": {}}))

    def boom(e):
        raise WsError("failed", "the runner's config will not read", "fix it")

    out = migrate.migrate(env, now=NOW, refresh=boom)
    assert out["state"] == "moved" and "will not read" in out["refresh_error"]
    assert "ws install" in migrate.render(out)
    assert (home / ".config" / "ws" / "policy.toml").is_file()


def test_timers_are_switched_off_for_the_move_and_on_again_after_it(home, env):
    old = make_old_home(home)
    write(old / "jobs" / "units" / "ws-daily.timer", "[Timer]\n")
    seen = []

    def off(files, e):
        seen.append(("off", [f.name for f in files], old.is_dir()))
        return []

    def on(e):
        seen.append(("on", paths.layout(e).mode, old.exists()))
        return []

    out = migrate.migrate(env, now=NOW, refresh=stub_refresh([]), deactivate=off, reschedule=on)
    assert seen == [("off", ["ws-daily.timer"], True), ("on", "xdg", False)]
    assert out["rescheduled"] == 1 and "next" not in out
    assert "on again" in migrate.render(out)


# ---- refusals and repeats ---------------------------------------------------------------------------------------------


def test_a_target_that_exists_and_differs_refuses_the_whole_move_and_changes_nothing(home, env):
    old = make_old_home(home)
    write(home / ".config" / "ws" / "policy.toml", "[gate]\n# a different one\n")
    before = tree(home)
    with pytest.raises(WsError) as e:
        migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert e.value.kind == "refused" and "differs" in e.value.message and "policy.toml" in e.value.message
    assert old.is_dir() and tree(home) == before and not (home / ".ws.migrated-20261107").exists()
    assert not (home / ".local").exists()  # not even the other folders were made


def test_a_target_of_the_wrong_kind_is_refused(home, env):
    make_old_home(home)
    (home / ".config" / "ws" / "jobs").mkdir(parents=True)
    (home / ".config" / "ws" / "jobs" / "daily.toml").mkdir()  # a folder where a file goes
    with pytest.raises(WsError) as e:
        migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert e.value.kind == "refused"


def test_a_second_run_says_it_is_done_and_changes_nothing(home, env):
    make_old_home(home)
    migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    before = tree(home)
    again = migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert again["state"] == "done" and "already migrated" in again["message"] and tree(home) == before


def test_an_interrupted_run_picks_up_where_it_stopped(home, env):
    make_old_home(home)
    cfg = home / ".config" / "ws"
    write(cfg / "policy.toml", "[gate]\n", 0o644)  # a file an earlier, stopped run had already put in place
    write(cfg / ".profile.toml.migrate-tmp", "half a copy")  # and the temporary file it died with
    out = migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert out["state"] == "moved" and out["already_there"] >= 1
    assert (cfg / "profile.toml").read_text(encoding="utf-8") == '[machine]\nserver = "none"\n'
    assert not (cfg / ".profile.toml.migrate-tmp").exists()


def test_both_folders_present_and_equal_finishes_the_move(home, env):
    make_old_home(home)
    migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    (home / ".ws.migrated-20261107").rename(home / ".ws")  # as if the final rename had not happened
    assert paths.probe(env).state == "both"
    out = migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert out["state"] == "moved" and not (home / ".ws").exists()


def test_a_copy_that_does_not_match_stops_before_anything_is_renamed(home, env, monkeypatch):
    old = make_old_home(home)

    def corrupt(item):
        item.dst.write_text("not what was there", encoding="utf-8")

    monkeypatch.setattr(migrate, "_copy_file", corrupt)
    with pytest.raises(WsError) as e:
        migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert e.value.kind == "failed" and "did not match" in e.value.message
    assert old.is_dir() and not (home / ".ws.migrated-20261107").exists()
    assert (old / "policy.toml").read_text(encoding="utf-8") == "[gate]\n"


def test_a_name_taken_for_the_renamed_folder_gets_a_number(home, env):
    make_old_home(home)
    (home / ".ws.migrated-20261107").mkdir()
    out = migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert out["renamed_to"] == str(home / ".ws.migrated-20261107-2")


def test_nothing_to_migrate_on_a_new_machine_or_when_a_folder_is_named(home, env, tmp_path):
    assert migrate.migrate(env, now=NOW)["state"] == "nothing"
    named = {**env, "WS_HOME": str(tmp_path / "mine")}
    make_old_home(home)
    out = migrate.migrate(named, now=NOW)
    assert out["state"] == "single" and (home / ".ws").is_dir() and not (home / ".config").exists()


def test_a_file_where_the_old_folder_would_be_is_refused(home, env):
    (home / ".ws").write_text("not a folder", encoding="utf-8")
    with pytest.raises(WsError) as e:
        migrate.migrate(env, now=NOW)
    assert e.value.kind == "bad_config"


# ---- the secrets store leaves no copy behind ---------------------------------------------------------------------------


def files_holding(root: Path, value: bytes) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and not p.is_symlink() and value in p.read_bytes()]


def test_the_secrets_store_is_renamed_so_the_old_folder_keeps_no_copy(home, env):
    old = make_old_home(home)
    inode = os.stat(old / "secrets" / "KEY").st_ino
    migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    kept = home / ".ws.migrated-20261107"
    new = home / ".local" / "share" / "ws" / "secrets"
    assert not (kept / "secrets").exists() and files_holding(kept, b"not-a-real-value") == []
    assert (new / "KEY").read_text(encoding="utf-8") == "not-a-real-value"
    assert os.stat(new / "KEY").st_ino == inode  # the same file, not a copy
    assert stat.S_IMODE((new / "KEY").stat().st_mode) == 0o600 and stat.S_IMODE(new.stat().st_mode) == 0o700


def test_across_filesystems_the_store_is_copied_checked_and_the_old_copy_removed(home, env, monkeypatch):
    old = make_old_home(home)
    write(old / "secrets" / "sub" / "TOKEN", "another-not-a-real-value", 0o600)
    (old / "secrets" / "sub").chmod(0o700)
    real = os.rename

    def rename(src, dst):
        if Path(src).name == "secrets":
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
        return real(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    out = migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    kept = home / ".ws.migrated-20261107"
    new = home / ".local" / "share" / "ws" / "secrets"
    assert out["state"] == "moved" and not (kept / "secrets").exists()
    assert files_holding(kept, b"not-a-real-value") == []
    assert (new / "sub" / "TOKEN").read_text(encoding="utf-8") == "another-not-a-real-value"
    for p, mode in ((new, 0o700), (new / "sub", 0o700), (new / "KEY", 0o600), (new / "sub" / "TOKEN", 0o600)):
        assert stat.S_IMODE(p.stat().st_mode) == mode, p


def test_a_store_that_arrives_wrong_is_not_removed_from_the_old_folder(home, env, monkeypatch):
    old = make_old_home(home)

    def cross_device(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))

    monkeypatch.setattr(os, "rename", cross_device)
    real = migrate._copy_file

    def corrupt(item):
        real(item)
        if item.src.name == "KEY":
            item.dst.write_text("not what was there", encoding="utf-8")

    monkeypatch.setattr(migrate, "_copy_file", corrupt)
    with pytest.raises(WsError) as e:
        migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert e.value.kind == "failed" and (old / "secrets" / "KEY").read_text(encoding="utf-8") == "not-a-real-value"
    assert old.is_dir() and not (home / ".ws.migrated-20261107").exists()


def hook(tool, **tool_input):
    return json.dumps({"tool_name": tool, "tool_input": tool_input})


def test_a_session_can_neither_read_nor_write_what_the_move_left_in_the_old_folder(cli_home):
    make_old_home(cli_home)
    migrate.migrate({"HOME": str(cli_home)}, now=NOW, refresh=stub_refresh([]))
    kept = cli_home / ".ws.migrated-20261107"
    again = cli_home / ".ws.migrated-20261107-2"  # a second move on the same day gets a number
    again.mkdir()
    pol = policy.default_policy()
    protect = gate._protect(paths.config_dir() / "policy.toml")
    for folder in (kept, again):
        for call in (
            hook("Read", file_path=str(folder / "profile.toml")),
            hook("Bash", command=f"cat {folder}/logs/runs.jsonl"),
            hook("Grep", pattern="x", path=str(folder)),
            hook("Write", file_path=str(folder / "new.txt")),
            hook("Edit", file_path=str(folder / "policy.toml")),
            hook("Bash", command=f"echo x > {folder}/sessions/a.json"),
        ):
            assert gate.check("claude-code", call, pol, protect), (str(folder), call)
    other = cli_home / ".ws-notes" / "a.txt"  # a name that only looks alike is nobody's concern
    assert not gate.check("claude-code", hook("Read", file_path=str(other)), pol, protect)


def test_the_wall_and_the_runners_rules_name_the_folders_a_move_left(cli_home):
    make_old_home(cli_home)
    migrate.migrate({"HOME": str(cli_home)}, now=NOW, refresh=stub_refresh([]))
    kept = cli_home / ".ws.migrated-20261107"
    assert kept in paths.protected_roots() and f"{kept}/**" in policy.secret_read_rules()
    assert str(kept) in gate._protect(paths.config_dir() / "policy.toml")


# ---- timers --------------------------------------------------------------------------------------------------------


def old_home_with_a_timer(home):
    old = make_old_home(home)
    write(old / "jobs" / "units" / "ws-daily.timer", "[Timer]\n")
    return old


def test_a_timer_that_will_not_switch_off_stops_the_move_before_it_starts(home, env):
    old_home_with_a_timer(home)
    before = tree(home)
    calls = []
    with pytest.raises(WsError) as e:
        migrate.migrate(
            env,
            now=NOW,
            refresh=stub_refresh([]),
            deactivate=lambda files, e2: ["systemctl said no"],
            reschedule=lambda e2: calls.append("on") or [],
        )
    assert e.value.kind == "refused" and "ws schedule apply" in e.value.hint
    assert tree(home) == before and calls == []


def test_timers_that_will_not_start_again_are_named_and_the_command_exits_1(home, env):
    old_home_with_a_timer(home)
    out = migrate.migrate(
        env, now=NOW, refresh=stub_refresh([]), deactivate=lambda files, e2: [], reschedule=lambda e2: ["no bus"]
    )
    assert out["state"] == "moved" and out["next"] == "ws schedule apply" and out["schedule_error"] == "no bus"
    assert "ws schedule apply" in migrate.render(out)


def test_no_timer_file_means_the_scheduler_is_never_asked(home, env):
    make_old_home(home)  # only a .service file in jobs/units
    asked = []
    out = migrate.migrate(
        env,
        now=NOW,
        refresh=stub_refresh([]),
        deactivate=lambda files, e2: asked.append(files) or [],
        reschedule=lambda e2: asked.append("on") or [],
    )
    assert out["state"] == "moved" and asked == [] and "rescheduled" not in out


def test_the_machines_scheduler_is_switched_off_through_the_runner(home, env):
    old_home_with_a_timer(home)
    argvs = []

    def runner(argv):
        argvs.append(argv)
        return 0, ""

    migrate.migrate(env, now=NOW, refresh=stub_refresh([]), reschedule=lambda e2: [], runner=runner)
    assert ["systemctl", "--user", "disable", "--now", "ws-daily.timer", "ws-daily.service"] in argvs


def test_the_command_keeps_the_timers_off_only_while_the_files_move(cli_home, capsys, monkeypatch):
    old = old_home_with_a_timer(cli_home)
    seen = []
    monkeypatch.setattr(migrate, "_deactivate", lambda files, e, runner=None: seen.append(("off", old.is_dir())) or [])
    monkeypatch.setattr(migrate, "_reschedule", lambda e, runner=None: seen.append(("on", old.exists())) or [])
    monkeypatch.setattr(migrate, "_refresh", lambda e: "0 changes")
    assert cli.main(["migrate"]) == 0
    assert seen == [("off", True), ("on", False)]
    assert "on again" in capsys.readouterr().out


# ---- the move won't start under a live broker or session, and its temporary files are its own ---------------------------


def listening(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    return srv


def test_it_refuses_while_the_broker_answers_and_changes_nothing(home, env):
    old = make_old_home(home)
    srv = listening(old / "run" / "broker.sock")
    before = tree(home)
    try:
        with pytest.raises(WsError) as e:
            migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    finally:
        srv.close()
    assert e.value.kind == "refused" and "broker.sock" in e.value.message
    assert tree(home) == before and not (home / ".config").exists()


def test_a_socket_nothing_listens_on_is_a_leftover_and_does_not_stop_the_move(home, env):
    old = make_old_home(home)
    listening(old / "run" / "broker.sock").close()  # the file stays, the broker is gone
    out = migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert out["state"] == "moved"


@pytest.mark.parametrize("marker", [approvals.ENV_MARKER, approvals.SANDBOX_MARKER])
def test_it_refuses_inside_a_gated_ws_session_but_a_dry_run_still_plans(home, env, marker):
    make_old_home(home)
    inside = {**env, marker: "1"}
    with pytest.raises(WsError) as e:
        migrate.migrate(inside, now=NOW, refresh=stub_refresh([]))
    assert e.value.kind == "refused" and "gated ws session" in e.value.message
    assert not (home / ".config").exists()
    assert migrate.migrate(inside, dry_run=True, now=NOW)["state"] == "move"


def test_a_planted_name_for_a_temporary_file_is_never_written_through(home, env, tmp_path):
    make_old_home(home)
    victim = write(tmp_path / "victim.txt", "precious")
    other = write(tmp_path / "other.txt", "also precious")
    cfg, state = home / ".config" / "ws", home / ".local" / "state" / "ws"
    cfg.mkdir(parents=True)
    state.mkdir(parents=True)
    os.link(victim, cfg / ".policy.toml.migrate-tmp")  # a second name for a file of the person's
    os.link(victim, state / ".migrate-manifest.json.migrate-tmp")
    os.symlink(other, cfg / ".profile.toml.migrate-tmp")
    out = migrate.migrate(env, now=NOW, refresh=stub_refresh([]))
    assert out["state"] == "moved"
    assert victim.read_text(encoding="utf-8") == "precious" and other.read_text(encoding="utf-8") == "also precious"
    assert (cfg / "policy.toml").read_text(encoding="utf-8") == "[gate]\n"
    assert json.loads((state / "migrate-manifest.json").read_text(encoding="utf-8"))["schema"] == migrate.SCHEMA


# ---- the command ------------------------------------------------------------------------------------------------------


@pytest.fixture
def cli_home(home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    for v in ("WS_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.chdir(home)
    return home


def test_the_command_prints_the_plan_for_a_dry_run(cli_home, capsys):
    make_old_home(cli_home)
    assert cli.main(["migrate", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert (
        "would move 14 file(s)" in out and str(cli_home / ".config" / "ws") in out and "dry run: nothing changed" in out
    )
    assert (cli_home / ".ws").is_dir()


def test_the_command_gives_one_envelope_under_json(cli_home, capsys):
    make_old_home(cli_home)
    assert cli.main(["migrate", "--json"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["kind"] == "migrate" and env["status"] == "ok" and env["data"]["state"] == "moved"
    assert (cli_home / ".config" / "ws" / "policy.toml").is_file()
    assert cli.main(["migrate", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "done"


def test_the_command_refuses_with_a_typed_error(cli_home, capsys):
    make_old_home(cli_home)
    write(cli_home / ".config" / "ws" / "policy.toml", "different")
    assert cli.main(["migrate"]) == 1
    err = capsys.readouterr().err
    assert "differs" in err and "hint" in err


def test_doctor_names_the_move_before_and_is_quiet_after(cli_home, capsys):
    make_old_home(cli_home)
    assert cli.main(["doctor", "--offline", "--json"]) in (0, 1)
    checks = {c["id"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["WS010"]["status"] == "warn" and "ws migrate" in checks["WS010"]["fix"]
    assert cli.main(["migrate", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["doctor", "--offline", "--json"]) in (0, 1)
    checks = {c["id"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["WS010"]["status"] == "ok"


def test_doctor_still_runs_and_fails_ws010_when_both_folders_exist(cli_home, capsys):
    make_old_home(cli_home)
    write(cli_home / ".config" / "ws" / "profile.toml", "")
    code = cli.main(["doctor", "--offline", "--json"])
    checks = {c["id"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert code == 1 and checks["WS010"]["status"] == "fail"
    assert str(cli_home / ".ws") in checks["WS010"]["detail"]


# ---- the whole round trip, with the real install -------------------------------------------------------------------------

FULL = ["--yes", "--runner", "claude-code", "--model", "some-model", "--cap", "20", "--stays-on", "yes"]


def test_install_then_migrate_then_install_again_changes_nothing_and_uninstall_still_cleans_up(cli_home, capsys):
    (cli_home / ".ws").mkdir()  # an earlier ws's folder: ws keeps using it until `ws migrate`
    notes = cli_home / "Notes"
    assert cli.main(["install", *FULL, "--notes", str(notes)]) == 0
    capsys.readouterr()
    assert paths.layout().mode == "legacy" and (cli_home / ".ws" / "policy.toml").is_file()
    settings = cli_home / ".claude" / "settings.json"
    assert str(cli_home / ".ws" / "policy.toml") in settings.read_text(encoding="utf-8")

    assert cli.main(["migrate"]) == 0
    out = capsys.readouterr().out
    assert "refreshed the runner hooks" in out and "could not refresh" not in out
    assert not (cli_home / ".ws").exists() and (cli_home / ".config" / "ws" / "policy.toml").is_file()
    text = settings.read_text(encoding="utf-8")
    assert (
        str(cli_home / ".config" / "ws" / "policy.toml") in text and str(cli_home / ".ws" / "policy.toml") not in text
    )
    assert (cli_home / ".local" / "state" / "ws" / "gate" / "claude-code" / "settings.json").is_file()

    assert cli.main(["install", *FULL, "--notes", str(notes)]) == 0
    assert "0 changes" in capsys.readouterr().out

    assert cli.main(["uninstall", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "left 0" in out, out
    assert not (cli_home / ".config" / "ws" / "policy.toml").exists()
    assert str(cli_home / ".config" / "ws") not in (settings.read_text(encoding="utf-8") if settings.exists() else "")
