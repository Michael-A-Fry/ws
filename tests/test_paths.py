"""ws.paths: the four XDG folders, the single-folder override, the old `~/.ws`, and the guard that nothing else spells a
ws path. Environments are passed in as dicts, so no test reads the real HOME or the real WS_HOME."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from ws import doctor, gate, paths, policy
from ws.output import WsError

SRC = Path(__file__).resolve().parent.parent / "src" / "ws"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    return h


def env_of(home, **extra):
    return {"HOME": str(home), **extra}


# ---- the layouts ------------------------------------------------------------------------------------------------------


def test_with_nothing_set_the_four_folders_are_the_xdg_defaults(home):
    lay = paths.layout(env_of(home))
    assert lay.mode == "xdg"
    assert lay.config == home / ".config" / "ws"
    assert lay.state == home / ".local" / "state" / "ws"
    assert lay.data == home / ".local" / "share" / "ws"
    assert lay.cache == home / ".cache" / "ws"
    assert paths.config_dir(env_of(home)) == lay.config and paths.cache_dir(env_of(home)) == lay.cache
    assert paths.state_dir(env_of(home)) == lay.state and paths.data_dir(env_of(home)) == lay.data
    assert paths.probe(env_of(home)).state == "fresh"


def test_an_absolute_xdg_variable_moves_its_folder_and_a_relative_one_is_ignored(home, tmp_path):
    env = env_of(
        home,
        XDG_CONFIG_HOME=str(tmp_path / "cfg"),
        XDG_STATE_HOME="relative/state",
        XDG_DATA_HOME=str(tmp_path / "dat with space"),
        XDG_CACHE_HOME="",
    )
    lay = paths.layout(env)
    assert lay.config == tmp_path / "cfg" / "ws"
    assert lay.state == home / ".local" / "state" / "ws"  # relative: ignored, as the specification says
    assert lay.data == tmp_path / "dat with space" / "ws"
    assert lay.cache == home / ".cache" / "ws"  # empty: unset


def test_ws_home_holds_all_four_folders_in_the_layout_ws_always_had(tmp_path, home):
    root = tmp_path / "one folder"
    lay = paths.layout(env_of(home, WS_HOME=str(root)))
    assert lay.mode == "single" and lay.distinct() == (root,)
    assert {lay.config, lay.state, lay.data, lay.cache} == {root}
    assert (
        paths.layout(env_of(home, WS_HOME="~/ws-here")).config == home / "ws-here"
    )  # a tilde is the HOME of that environment


def test_ws_home_beats_a_leftover_old_folder_and_the_xdg_folders(tmp_path, home):
    (home / ".ws").mkdir()
    (home / ".config" / "ws").mkdir(parents=True)
    (home / ".config" / "ws" / "policy.toml").write_text("", encoding="utf-8")
    assert paths.layout(env_of(home, WS_HOME=str(tmp_path / "w"))).config == tmp_path / "w"
    assert paths.probe(env_of(home, WS_HOME=str(tmp_path / "w"))).state == "single"


def test_one_folder_can_be_named_on_its_own(tmp_path, home):
    env = env_of(home, WS_STATE_DIR=str(tmp_path / "st"))
    lay = paths.layout(env)
    assert lay.state == tmp_path / "st" and lay.config == home / ".config" / "ws"
    both = paths.layout(env_of(home, WS_HOME=str(tmp_path / "w"), WS_DATA_DIR=str(tmp_path / "d")))
    assert (
        both.data == tmp_path / "d" and both.config == both.state == both.cache == tmp_path / "w"
    )  # most specific wins


def test_a_relative_folder_variable_is_refused_not_guessed(home):
    with pytest.raises(WsError) as e:
        paths.layout(env_of(home, WS_CONFIG_DIR="here"))
    assert e.value.kind == "bad_config" and "WS_CONFIG_DIR" in e.value.message


def test_a_relative_home_is_refused(home):
    with pytest.raises(WsError) as e:
        paths.layout({"HOME": "relative"})
    assert e.value.kind == "bad_config"


# ---- an existing install: ~/.ws -----------------------------------------------------------------------------------------


def test_an_old_folder_alone_keeps_being_used_and_nothing_is_made(home):
    (home / ".ws").mkdir()
    lay = paths.layout(env_of(home))
    assert lay.mode == "legacy" and lay.distinct() == (home / ".ws",)
    assert paths.probe(env_of(home)).state == "legacy"
    assert not (home / ".config").exists() and not (home / ".local").exists()  # looking changes nothing


def test_an_old_folder_and_ws_entries_in_the_xdg_folders_is_a_typed_error_naming_both(home):
    (home / ".ws").mkdir()
    (home / ".local" / "state" / "ws" / "logs").mkdir(parents=True)
    with pytest.raises(WsError) as e:
        paths.layout(env_of(home))
    assert e.value.kind == "ambiguous_layout"
    assert str(home / ".ws") in e.value.message and str(home / ".local" / "state" / "ws" / "logs") in e.value.message
    assert "ws migrate" in e.value.hint
    for fn in (paths.config_dir, paths.state_dir, paths.data_dir, paths.cache_dir, paths.roots):
        with pytest.raises(WsError):
            fn(env_of(home))
    pr = paths.probe(env_of(home))  # the doctor and migrate can still look
    assert pr.state == "both" and pr.owned == (home / ".local" / "state" / "ws" / "logs",)


def test_entries_in_the_xdg_folders_that_are_not_ws_s_do_not_make_the_layout_ambiguous(home):
    (home / ".ws").mkdir()
    (home / ".config" / "ws").mkdir(parents=True)
    (home / ".config" / "ws" / "something-else").write_text("x", encoding="utf-8")
    (home / ".config" / "ws" / "secrets").mkdir()  # a name that belongs in the data folder, not here
    assert paths.layout(env_of(home)).mode == "legacy"


def test_the_cache_folder_alone_does_not_count_as_the_xdg_layout(home):
    (home / ".ws").mkdir()
    (home / ".cache" / "ws" / "index").mkdir(parents=True)
    assert paths.layout(env_of(home)).mode == "legacy"


def test_a_file_where_the_old_folder_would_be_is_refused(home):
    (home / ".ws").write_text("not a folder", encoding="utf-8")
    with pytest.raises(WsError) as e:
        paths.layout(env_of(home))
    assert e.value.kind == "bad_config"


def test_after_the_old_folder_is_renamed_the_xdg_folders_are_used(home):
    (home / ".ws.migrated-20261107").mkdir()
    (home / ".config" / "ws").mkdir(parents=True)
    (home / ".config" / "ws" / "policy.toml").write_text("", encoding="utf-8")
    assert paths.layout(env_of(home)).mode == "xdg"
    assert paths.probe(env_of(home)).state == "xdg"


# ---- the table ------------------------------------------------------------------------------------------------------


def test_every_name_belongs_to_one_folder_and_the_control_names_are_all_in_the_table():
    seen = [n for names in paths.NAMES.values() for n in names]
    assert len(seen) == len(set(seen))
    assert set(paths.CONTROL_NAMES) <= set(seen)
    assert set(paths.NAMES) == set(paths.KINDS)
    assert paths.kind_of("policy.toml") == "config" and paths.kind_of("logs") == "state"
    assert paths.kind_of("secrets") == "data" and paths.kind_of("index") == "cache"
    assert paths.kind_of("nonsense") is None


def test_path_of_puts_each_name_under_its_folder_and_refuses_an_unknown_one(home):
    lay = paths.layout(env_of(home))
    for kind, names in paths.NAMES.items():
        for n in names:
            assert lay.path_of(n) == lay.dir(kind) / n
    with pytest.raises(WsError):
        lay.path_of("nonsense")


def test_the_work_folder_is_beside_ws_s_folders_and_not_in_one(home, tmp_path):
    env = env_of(home)
    assert paths.work_dir(env) == home / ".local" / "state" / "ws-work"
    assert paths.old_work_dir(env) == home / ".local" / "state" / "ws" / "work"
    assert not any(paths.work_dir(env).is_relative_to(r) for r in paths.roots(env))
    moved = env_of(home, XDG_STATE_HOME=str(tmp_path / "s"))
    assert paths.work_dir(moved) == tmp_path / "s" / "ws-work"


def test_env_for_gives_a_child_the_same_folders(home, tmp_path):
    xdg = paths.env_for(env_of(home))
    assert set(xdg) == set(paths.ENV_DIRS.values())
    child = {"HOME": str(tmp_path / "elsewhere"), **xdg}  # a different HOME, as inside the wall
    got = paths.layout(child)
    assert got.mode == "single" and [got.dir(k) for k in paths.KINDS] == [
        paths.layout(env_of(home)).dir(k) for k in paths.KINDS
    ]
    assert paths.env_for(env_of(home, WS_HOME=str(tmp_path / "w"))) == {"WS_HOME": str(tmp_path / "w")}


def test_env_for_pins_the_old_folder_explicitly(home):
    (home / ".ws").mkdir()
    assert paths.env_for(env_of(home)) == {"WS_HOME": str(home / ".ws")}


def test_describe_says_where_the_layout_comes_from(home, tmp_path):
    assert paths.describe(env_of(home, WS_HOME=str(tmp_path))) == "$WS_HOME"
    assert "XDG" in paths.describe(env_of(home))
    (home / ".ws").mkdir()
    assert "ws migrate" in paths.describe(env_of(home))


# ---- the guard: nothing else spells a ws path -------------------------------------------------------------------------

# `.ws` as a folder name on its own (not `.ws-release`, `.ws-tmp`, `foo.ws`). The repo-level folder `.ws/` that
# `ws globals` reads (overrides, manifest) is a different thing and lives in globals.py.
_LITERAL = re.compile(r"(?<![\w.-])\.ws(?![\w-])")
_ALLOWED = {"paths.py", "globals.py", "migrate.py"}  # migrate.py names the old folder in its help text


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def test_no_module_but_paths_spells_the_old_folder_or_reads_the_layout_variables():
    offenders = []
    for py in sorted(SRC.rglob("*.py")):
        if py.name in _ALLOWED:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            is_text = isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
            if is_text and _LITERAL.search(node.value):
                offenders.append(f"{py.relative_to(SRC)}:{node.lineno}: {node.value[:60]!r}")
    assert offenders == [], "build ws paths with ws.paths, not by hand:\n" + "\n".join(offenders)


def test_no_module_reads_ws_home_straight_from_the_environment_but_the_ones_that_name_it():
    # the modules that may name the variables: paths (the owner), the policy and shell reader (a command word's `$WS_HOME`),
    # the sandbox and the schedulers/canary/run (hand them to a child), secrets (the walled broker's own folder) and
    # the doctor and config text
    allowed = {"paths.py", "policy.py", "shellwords.py", "sandbox.py", "run.py", "canary.py", "secrets.py", "gate.py"}
    offenders = []
    for py in sorted(SRC.rglob("*.py")):
        if py.name in allowed:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "WS_HOME" and id(node) not in docs:
                offenders.append(f"{py.relative_to(SRC)}:{node.lineno}")
    assert offenders == [], "ask ws.paths for a folder instead of reading WS_HOME:\n" + "\n".join(offenders)


# ---- the gate and the wall protect each folder, in both layouts --------------------------------------------------------


def _hook(tool, **tool_input):
    return json.dumps({"tool_name": tool, "tool_input": tool_input})


def _layouts(request, tmp_path, monkeypatch):
    h = tmp_path / "gate-home"
    h.mkdir()
    for v in (
        "WS_HOME",
        "WS_CONFIG_DIR",
        "WS_STATE_DIR",
        "WS_DATA_DIR",
        "WS_CACHE_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    ):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(h))
    if request.param == "legacy":
        (h / ".ws").mkdir()
    return h


@pytest.fixture(params=["xdg", "legacy", "single", "custom"])
def layout_home(request, tmp_path, monkeypatch):
    h = _layouts(request, tmp_path, monkeypatch)
    if request.param == "single":
        monkeypatch.setenv("WS_HOME", str(tmp_path / "one"))
    if request.param == "custom":
        for kind, var in paths.ENV_DIRS.items():
            monkeypatch.setenv(var, str(tmp_path / f"c-{kind}"))
    return h


def test_the_gate_denies_a_write_into_each_ws_folder_in_every_layout(layout_home):
    pol = policy.default_policy()
    protect = gate._protect(paths.config_dir() / "policy.toml")
    folders = paths.roots()
    assert len(folders) == (1 if len(set(paths.layout().distinct())) == 1 else 4)
    for folder in folders:
        for call in (
            _hook("Write", file_path=str(folder / "new.txt")),
            _hook("Edit", file_path=str(folder / "sub" / "x.toml")),
            _hook("Bash", command=f"echo x > {folder}/new.txt"),
            _hook("Bash", command=f"cp /etc/hostname {folder}/copy"),
        ):
            assert gate.check("claude-code", call, pol, protect), (str(folder), call)


def test_the_gate_denies_reading_the_secrets_store_in_every_layout(layout_home):
    pol = policy.default_policy()
    protect = gate._protect(paths.config_dir() / "policy.toml")
    store = paths.data_dir() / "secrets"
    for call in (
        _hook("Read", file_path=str(store / "KEY")),
        _hook("Bash", command=f"cat {store}/KEY"),
        _hook("Grep", pattern="x", path=str(store)),
    ):
        assert gate.check("claude-code", call, pol, protect), call


def test_a_write_to_the_layout_not_in_use_is_denied_too(tmp_path, monkeypatch):
    h = tmp_path / "other"
    h.mkdir()
    for v in (
        "WS_HOME",
        "WS_CONFIG_DIR",
        "WS_STATE_DIR",
        "WS_DATA_DIR",
        "WS_CACHE_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(h))
    (h / ".ws").mkdir()  # in use: the old folder
    protect = gate._protect(paths.config_dir() / "policy.toml")
    pol = policy.default_policy()
    # planting a policy in the XDG folders would make the layout ambiguous and stop ws, so it is refused
    call = _hook("Write", file_path=str(h / ".config" / "ws" / "policy.toml"))
    assert gate.check("claude-code", call, pol, protect)


def test_the_secrets_store_is_denied_for_reading_whichever_layout_is_in_use(layout_home):
    rules = policy.secret_read_rules()
    assert any(r.startswith(str(paths.data_dir() / "secrets")) for r in rules)


def test_protected_paths_never_raise_even_when_the_layout_is_ambiguous(home):
    (home / ".ws").mkdir()
    (home / ".local" / "state" / "ws" / "logs").mkdir(parents=True)
    env = env_of(home)
    roots = paths.protected_roots(env)
    assert home / ".ws" in roots and home / ".config" / "ws" in roots and home / ".local" / "share" / "ws" in roots
    assert str(home / ".ws" / "policy.toml") in paths.control_paths(env)
    assert home / ".ws" / "secrets" in paths.secret_dirs(
        env
    ) and home / ".local" / "share" / "ws" / "secrets" in paths.secret_dirs(env)


# ---- the doctor ------------------------------------------------------------------------------------------------------


def test_doctor_ws010_is_ok_for_the_xdg_folders_and_for_a_folder_you_named(home, tmp_path):
    c = doctor._layout_check(env_of(home))
    assert (c.id, c.status) == ("WS010", "ok") and str(home / ".config" / "ws") in c.detail
    one = doctor._layout_check(env_of(home, WS_HOME=str(tmp_path / "w")))
    assert one.status == "ok" and str(tmp_path / "w") in one.detail


def test_doctor_ws010_warns_while_the_files_are_in_the_old_folder(home):
    (home / ".ws").mkdir()
    c = doctor._layout_check(env_of(home))
    assert c.status == "warn" and str(home / ".ws") in c.detail and "ws migrate" in c.fix


def test_doctor_ws010_fails_and_names_both_when_both_exist(home):
    (home / ".ws").mkdir()
    (home / ".config" / "ws").mkdir(parents=True)
    (home / ".config" / "ws" / "profile.toml").write_text("", encoding="utf-8")
    c = doctor._layout_check(env_of(home))
    assert (
        c.status == "fail"
        and str(home / ".ws") in c.detail
        and str(home / ".config" / "ws" / "profile.toml") in c.detail
    )
