"""The scanned commands: a top-level module of the installed `ws` package that sets `COMMAND = "name"` is found by its
source text and its `register(sub)` is called, after the built-ins. A name that is taken is refused; the working folder
is never scanned."""

from __future__ import annotations

import importlib
import json
import sys

import pytest

import ws
from ws import cli
from ws.output import WsError

# The commands the installed package itself adds, in module-name order; a planted one comes after them.
SHIPPED = [("msg", "msg"), ("node", "node"), ("queue", "queue"), ("usage", "usage")]

PLANTED = """\
COMMAND = "{name}"


def register(sub):
    p = sub.add_parser("{name}", help="a planted command")
    p.set_defaults(func=lambda args: print("{name} ran") or 0, raw=True)
"""


@pytest.fixture
def plant(tmp_path, monkeypatch):
    """Write modules into a folder that is added to the end of `ws.__path__`, as a temp copy of the package would be."""
    extra = tmp_path / "pkg"
    extra.mkdir()
    monkeypatch.setattr(ws, "__path__", [*ws.__path__, str(extra)])
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    made: list[str] = []

    def write(module: str, text: str | None = None, **fmt) -> None:
        (extra / f"{module}.py").write_text(text if text is not None else PLANTED.format(**fmt))
        monkeypatch.delitem(sys.modules, f"ws.{module}", raising=False)
        made.append(f"ws.{module}")
        importlib.invalidate_caches()

    yield write
    for name in made:
        sys.modules.pop(name, None)


def names(parser=None) -> list[str]:
    return list(cli._subcommands(parser or cli.build_parser()))


def test_the_installed_package_adds_only_its_own_scanned_commands():
    assert cli.scanned_commands() == SHIPPED  # a module that wants one sets COMMAND; these four do


def test_a_planted_module_is_in_help_and_runs(plant, capsys):
    plant("zzplanted", name="planted-one")
    assert cli.scanned_commands() == [*SHIPPED, ("planted-one", "zzplanted")]
    parser = cli.build_parser()
    assert "planted-one" in parser.format_help()
    assert names(parser)[-1] == "planted-one"  # after every built-in
    assert cli.main(["planted-one"]) == 0
    assert capsys.readouterr().out == "planted-one ran\n"
    assert cli.main(["help", "--json"]) == 0
    listed = [c["name"] for c in json.loads(capsys.readouterr().out)["data"]["commands"]]
    assert "planted-one" in listed and "doctor" in listed


def test_scanned_commands_are_added_in_module_name_order(plant):
    plant("zz_b", name="beta")
    plant("zz_a", name="alpha")
    assert cli.scanned_commands() == [*SHIPPED, ("alpha", "zz_a"), ("beta", "zz_b")]
    got = names()
    assert got.index("alpha") < got.index("beta") and got.index("version") < got.index("alpha")


def test_a_name_that_is_a_built_in_is_refused_with_both_named(plant):
    plant("zz_doctor", name="doctor")
    with pytest.raises(WsError) as e:
        cli.build_parser()
    assert e.value.kind == "command_conflict"
    assert "ws.zz_doctor" in e.value.message and "`doctor`" in e.value.message and "built-in" in e.value.message


def test_two_scanned_modules_with_one_name_are_refused_with_both_named(plant):
    plant("zz_first", name="twin")
    plant("zz_second", name="twin")
    with pytest.raises(WsError) as e:
        cli.build_parser()
    assert e.value.kind == "command_conflict" and "ws.zz_first" in e.value.message and "ws.zz_second" in e.value.message


def test_a_collision_is_a_typed_error_from_main(plant, capsys):
    plant("zz_doctor", name="doctor")
    assert cli.main(["doctor", "--json"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["status"] == "error" and env["error_kind"] == "command_conflict"


def test_command_without_register_is_a_typed_error(plant):
    plant("zz_bare", 'COMMAND = "bare"\n')
    with pytest.raises(WsError) as e:
        cli.build_parser()
    assert e.value.kind == "command_module" and "ws.zz_bare" in e.value.message and "register" in e.value.message


def test_register_that_does_not_add_the_command_is_a_typed_error(plant):
    plant("zz_wrong", 'COMMAND = "wanted"\n\n\ndef register(sub):\n    sub.add_parser("other")\n')
    with pytest.raises(WsError) as e:
        cli.build_parser()
    assert e.value.kind == "command_module" and "wanted" in e.value.message


def test_a_module_that_cannot_be_loaded_names_itself(plant):
    plant("zz_broken", 'COMMAND = "broken"\nraise RuntimeError("no")\n')
    with pytest.raises(WsError) as e:
        cli.build_parser()
    assert e.value.kind == "command_module" and "ws.zz_broken" in e.value.message


def test_only_a_matching_module_is_imported(plant):
    # these would blow up if they were imported; none sets COMMAND at the top level in the plain form
    plant("zz_plain", "raise RuntimeError('imported')\n")
    plant("zz_indented", 'if True:\n    COMMAND = "indented"\nraise RuntimeError("imported")\n')
    plant("zz_text", '"""\nCOMMAND = "in-a-docstring" is a line of text; it is not at the start of a line.\n"""\n')
    plant("zz_caps", 'COMMAND = "Caps"\nraise RuntimeError("imported")\n')
    plant("zz_other", 'COMMANDS = "x"\nCOMMAND_NAME = "x"\nraise RuntimeError("imported")\n')
    plant("_private", 'COMMAND = "private"\nraise RuntimeError("imported")\n')
    plant(
        "zz_ok", 'COMMAND: str = "typed"  # the annotated form\n\n\ndef register(sub):\n    sub.add_parser("typed")\n'
    )
    assert cli.scanned_commands() == [*SHIPPED, ("typed", "zz_ok")]
    assert "typed" in names()


def test_the_working_folder_is_never_scanned(tmp_path, monkeypatch, plant):
    here = tmp_path / "cwd"
    (here / "ws").mkdir(parents=True)
    (here / "ws" / "__init__.py").write_text("")
    (here / "ws" / "evil.py").write_text(PLANTED.format(name="evil"))
    (here / "evil.py").write_text(PLANTED.format(name="evil"))
    monkeypatch.chdir(here)
    monkeypatch.syspath_prepend(str(here))
    assert cli.scanned_commands() == SHIPPED
    assert "evil" not in names()
    assert str(here) not in map(str, ws.__path__)


def test_the_scan_reads_text_only_and_imports_nothing(plant, monkeypatch):
    plant("zz_text_only", name="textual")
    seen = []
    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda n, *a: seen.append(n) or real(n, *a))
    assert cli.scanned_commands() == [*SHIPPED, ("textual", "zz_text_only")]
    assert seen == []


def test_help_json_and_the_fallback_see_scanned_commands_and_no_built_in_is_shadowed(plant, capsys, monkeypatch):
    from ws import extcmd

    plant("zz_seen", name="seen")
    parser = cli.build_parser()
    flags = [o for a in parser._actions for o in a.option_strings]
    # a scanned command is a command like any other: no external program or fallback takes its word
    assert extcmd.plan(["seen"], *_split(["seen"]), cli._subcommands(parser), flags) is None
    assert cli.main(["help", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert "seen" not in [e if isinstance(e, str) else e.get("name") for e in data["external"]]


def _split(raw):
    from ws import output

    return output.split_global(raw)
