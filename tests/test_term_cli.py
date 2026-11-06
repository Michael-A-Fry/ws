"""`ws term`'s command line: where it listens (and the warning), the usage exit code, and the help and docs wording for
the two settings that reach beyond the session."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ws import cli as ws_cli
from ws.output import WsError
from ws.term import cli

DOC = (Path(__file__).parent.parent / "docs" / "web-terminal.md").read_text()
NAMED = "198.51.100.4"  # from the documentation range: it names no real network


def term_help() -> str:
    parser = ws_cli.build_parser()
    action = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    return re.sub(r"\s+", " ", action.choices["term"].format_help())


# -- where it listens


def test_no_bind_means_loopback_only():
    assert cli.bind_address(None) == "127.0.0.1" == cli.LOOPBACK


def test_a_named_address_is_accepted_as_written():
    assert cli.bind_address(NAMED) == NAMED
    assert cli.bind_address(f"  {NAMED} ") == NAMED


def test_the_loopback_address_named_outright_is_still_loopback():
    assert cli.bind_address("127.0.0.1") == cli.LOOPBACK
    assert cli.bind_warning(cli.bind_address("127.0.0.1")) is None


@pytest.mark.parametrize("text", ["0.0.0.0", "::", " 0.0.0.0 "])
def test_a_wildcard_is_refused(text):
    with pytest.raises(WsError) as e:
        cli.bind_address(text)
    assert e.value.kind == "usage" and e.value.exit_code == 2 and e.value.hint


@pytest.mark.parametrize("text", ["", "localhost", "example.com", "300.1.1.1", "2001:db8::1", "1.2.3"])
def test_anything_that_is_not_one_ipv4_address_is_a_usage_error(text):
    with pytest.raises(WsError) as e:
        cli.bind_address(text)
    assert e.value.kind == "usage" and e.value.exit_code == 2 and e.value.hint


def test_a_bad_bind_exits_2_not_1(capsys):
    assert ws_cli.main(["term", "--bind", "0.0.0.0"]) == 2
    assert "every interface" in capsys.readouterr().err
    assert ws_cli.main(["term", "--bind", "not-an-address"]) == 2


def test_loopback_has_no_warning_and_a_named_address_has_one_that_says_who_can_reach_it():
    assert cli.bind_warning("127.0.0.1") is None
    text = cli.bind_warning(NAMED)
    assert text is not None and NAMED in text
    assert "not just this machine" in text and "run commands as you" in text


# -- the settings that reach beyond the session


def test_the_tmux_server_options_help_says_it_changes_the_server_for_every_session():
    text = term_help()
    assert "--tmux-server-options" in text
    assert "every session" in text and "escape-time" in text and "extended-keys" in text


def test_the_docs_say_the_tmux_server_options_change_every_session_on_the_server():
    para = DOC.split("`--tmux-server-options`", 1)[1][:400]
    assert "every session" in re.sub(r"\s+", " ", para)


def test_the_sync_output_variable_is_documented_with_its_scope():
    flat = re.sub(r"\s+", " ", DOC)
    assert cli.SYNC_ENV == {"CLAUDE_CODE_FORCE_SYNC_OUTPUT": "1"}
    assert "CLAUDE_CODE_FORCE_SYNC_OUTPUT" in flat and "--no-sync-output" in flat
    assert "other programs ignore it" in flat
    assert "ignore it" in term_help()


def test_the_docs_give_the_ping_and_silence_limits_the_server_uses():
    from ws.term import app

    flat = re.sub(r"\s+", " ", DOC)
    assert f"every {app.PING_SECONDS:.0f} seconds" in flat
    assert f"more than {app.PING_SECONDS + app.PONG_SECONDS:.0f}" in flat
