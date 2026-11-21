"""The pane-keys guard: a `tmux send-keys` (or a buffer paste) that types a slash command into a pane on the live server is
an approvable deny, however the text is split, decoded or hidden; test servers and ordinary typing are left alone."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from ws import gate, guards, policy
from ws.adapters import Call
from ws.guards import pane_keys
from ws.policy import PolicyError

CFG = pane_keys.Config(live_socket="live")
REAL_SHOW_BUFFER = pane_keys._show_buffer


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """A tmux folder of our own, and a `tmux` on PATH as far as the guard can tell (it never runs one here)."""
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("MSG", raising=False)
    real = shutil.which
    monkeypatch.setattr(
        pane_keys.shutil, "which", lambda name, *a, **k: "/usr/bin/tmux" if name == "tmux" else real(name)
    )
    monkeypatch.setattr(pane_keys, "_show_buffer", lambda sock, name, env: None)


def ask(command: str, cfg=CFG, cwd: str = "/w"):
    ctx = guards.Context("claude-code", "pre", "s1", cwd, {})
    return pane_keys.judge(Call("command", command), cfg, ctx)


def refused(command: str, *words: str, cfg=CFG) -> None:
    found = ask(command, cfg)
    assert found is not None, command
    assert found.action == "deny" and found.approvable, command
    assert "--remote-control" in found.reason and "ws msg send" in found.reason
    assert all(w in found.reason for w in words), found.reason


def allowed(command: str, cfg=CFG) -> None:
    assert ask(command, cfg) is None, command


# --- the case that happened ----------------------------------------------------------------------------------------


def test_the_incident_commands_are_refused():
    refused("tmux -L live send-keys -t %311 '/rename ws-private-lead' Enter", "slash")
    refused("tmux -L live send-keys -t %311 '/remote-control ws-private-lead' Enter")
    refused(
        "tmux -L live send-keys -t %311 '/rename x' Enter; tmux -L live send-keys -t %311 '/remote-control x' Enter"
    )


def test_a_server_that_cant_be_known_is_taken_to_be_the_live_one():
    refused(
        "cd /tmp && tmux -L other send-keys -t %3 '/rename x' Enter"
    )  # an earlier command: the flags can't be trusted
    refused("env tmux -L other send-keys -t %3 '/x' Enter")  # a wrapper
    refused("bash -c \"tmux -L other send-keys -t %3 '/x' Enter\"")  # a shell's text
    refused("for p in %3 %4; do tmux -L other send-keys -t $p '/x' Enter; done")  # a loop


def test_the_default_socket_is_the_live_one_when_the_policy_names_none(monkeypatch):
    plain = pane_keys.Config()
    refused("tmux send-keys -t %3 '/rename x' Enter", cfg=plain)
    refused("tmux -L default send-keys -t %3 '/rename x' Enter", cfg=plain)
    allowed("tmux -L test send-keys -t %3 '/rename x' Enter", cfg=plain)
    monkeypatch.setenv("TMUX", f"{os.environ['TMUX_TMPDIR']}/tmux-{os.getuid()}/live,1,0")  # the session's own server
    refused("tmux send-keys -t %3 '/rename x' Enter")


def test_a_clearly_different_test_socket_is_left_alone():
    allowed("tmux -L test send-keys -t %3 '/rename x' Enter")
    allowed("tmux -S /tmp/test.sock send-keys -t %3 '/rename x' Enter")
    allowed("tmux -L test set-buffer '/x' \\; paste-buffer -t %3")  # one tmux command list, one test server
    refused(
        "tmux -L test set-buffer '/x'; tmux -L test paste-buffer -t %3"
    )  # two commands: the second can't be trusted


# --- what counts as typing a slash -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "keys",
    [
        "-l '/rename x'",  # literal text
        "-l '/' 'rename x'",  # split across words
        "'/re' 'name' Space x Enter",  # text and key names put together
        "-t %3 -l '/model'",
        "-lt %3 '/model'",  # a cluster of options
        "-t%3 '/model'",
        "-- '/model'",
        "Enter '/model'",  # a line break first: the next line starts with a slash
        "'  /model'",  # blanks in front do not hide it
        "'hi' Enter '/model'",  # the second line
        "-H 2f 72 65",  # hex bytes
        "0x2f 'rename'",  # a code point
        "KP/ 'rename'",  # the keypad's slash
        "x BSpace '/rename'",
        "'hello' C-u '/rename'",  # the line cleared, then a slash
        "'/' C-a 'x'",  # the cursor moved: the slash may end up first
        "'rename' Home '/'",
    ],
)
def test_a_line_that_starts_with_a_slash_is_refused(keys):
    refused(f"tmux -L live send-keys {keys}")


def test_every_spelling_of_the_command_word_is_read():
    for word in ("send", "send-keys", "send-k"):
        refused(f"tmux -L live {word} -l '/rename x'")
    refused("tmux -L live 'send'-keys -l '/rename x'")  # quote-split


@pytest.mark.parametrize(
    "keys",
    [
        "'ls -la' Enter",
        "-t %3 'cd /tmp' Enter",  # a slash that is not at the start of a line
        "'echo hi' Enter 'echo /x' Enter",
        "-l 'see /etc/hosts'",
        "Enter",
        "C-c",
        "Up Enter",  # a cursor key with no slash
        "-X cancel",  # copy mode types nothing
        "-t %3 Escape",
        "'hello' Space 'there' Enter",
        "-l 'Enter /x'",  # literal: `Enter` is the word, and the line starts with E
    ],
)
def test_ordinary_typing_is_left_alone(keys):
    allowed(f"tmux -L live send-keys {keys}")


def test_literal_mode_does_not_decode_key_names():
    allowed("tmux -L live send-keys -l 'Enter' '/y'")  # one line, `Enter/y`: here Enter is a word, not a line break
    refused("tmux -L live send-keys '/y'")  # without -l the same words are keys and text: a slash line
    refused("tmux -L live send-keys -l $'a\\n/y'")  # a real newline in literal text starts a line


# --- text that is not settled ---------------------------------------------------------------------------------------


def test_a_line_that_starts_with_a_word_the_shell_has_not_settled_is_refused():
    refused('tmux -L live send-keys -t %3 "$BODY" Enter', "not settled")
    refused('tmux -L live send-keys -t %3 "$(cat /tmp/body)" Enter', "not settled")
    allowed(
        'tmux -L live send-keys -t "$PANE" "echo $BODY" Enter'
    )  # the target may be a variable; the line starts `echo`


def test_a_variable_that_the_environment_holds_is_read_as_its_value(monkeypatch):
    monkeypatch.setenv("MSG", "/rename x")
    refused('tmux -L live send-keys -t %3 "$MSG" Enter', "slash")


def test_a_tmux_command_word_that_is_not_plain_text_is_refused():
    refused("tmux -L live $sub -t %3 '/x' Enter", "not plain text")


# --- commands that carry commands --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "bash -c \"tmux -L test send-keys -t %3 '/x' Enter\"",
        "sh -c 'tmux send-keys -t %3 \"/x\" Enter'",
        "eval \"tmux -L test send-keys -t %3 '/x' Enter\"",
        "echo $(tmux -L test send-keys -t %3 '/x' Enter)",
        "tmux -L live run-shell \"tmux send-keys -t %3 '/x' Enter\"",
        "tmux -L live if-shell true \"send-keys -t %3 '/x' Enter\"",
        "tmux -L live bind-key x send-keys '/x' Enter",
        "tmux -L live new-window 'tmux send-keys -t %3 \"/x\" Enter'",
        "tmux -L live send-keys -t %3 \"tmux send-keys -t %4 '/x' Enter\" Enter",  # typed text that is itself a command
        "tmux -L live display-message hi \\; send-keys -t %3 '/x' Enter",  # after a `;`
    ],
)
def test_a_send_keys_held_in_text_is_refused(command):
    refused(command)


def test_a_command_list_keeps_the_socket_of_the_command_it_follows():
    allowed("tmux -L test display-message hi \\; send-keys -t %3 '/x' Enter")
    refused("tmux -L live send-keys -t %3 hello Enter \\; send-keys -t %3 '/x' Enter")
    refused("tmux -L live send-keys -t %3 hello Enter ';' send-keys -t %3 '/x' Enter")


def test_a_pipe_into_a_shell_with_tmux_in_the_line_is_refused():
    refused("echo \"tmux send-keys '/x' Enter\" | sh", "reads its commands from input")


def test_source_file_is_read(tmp_path):
    conf = tmp_path / "x.conf"
    conf.write_text("send-keys -t %3 '/x' Enter\n")
    refused(f"tmux -L live source-file {conf}")
    allowed(f"tmux -L test source-file {conf}")
    (tmp_path / "ok.conf").write_text("set -g status off\n")
    allowed(f"tmux -L live source-file {tmp_path / 'ok.conf'}")


# --- buffers ----------------------------------------------------------------------------------------------------------


def test_a_buffer_set_and_pasted_in_one_line_is_judged_on_its_text():
    refused("tmux -L live set-buffer '/rename x'; tmux -L live paste-buffer -t %3", "buffer")
    refused("tmux -L live set-buffer -b mine '/rename x' \\; paste-buffer -b mine -t %3")
    refused("tmux -L live setb $'hello\\n/rename x' && tmux -L live pasteb -t %3")
    allowed("tmux -L live set-buffer 'echo hi'; tmux -L live paste-buffer -t %3")
    allowed("tmux -L live set-buffer '/rename x'")  # never pasted here
    allowed("tmux -L live set-buffer -b a '/rename x'; tmux -L live set-buffer -b b ok; tmux -L live paste-buffer -b b")
    refused("tmux -L live set-buffer '/a'; tmux -L live set-buffer -a ' b'; tmux -L live paste-buffer")


def test_a_buffer_loaded_from_a_file_or_a_pipe_is_judged_on_its_text(tmp_path):
    body = tmp_path / "body.txt"
    body.write_text("/remote-control x\n")
    refused(f"tmux -L live load-buffer {body} && tmux -L live paste-buffer -t %3")
    body.write_text("hello\n")
    allowed(f"tmux -L live load-buffer {body} && tmux -L live paste-buffer -t %3")
    refused("printf '/rename x' | tmux -L live load-buffer - && tmux -L live paste-buffer -t %3")
    refused("echo '/rename x' | tmux -L live load-buffer -; tmux -L live paste-buffer -t %3")
    allowed("echo 'hello' | tmux -L live load-buffer -; tmux -L live paste-buffer -t %3")
    # relative paths are read from the session's folder; a missing or unreadable file is allowed (it can't be read)
    (tmp_path / "rel.txt").write_text("/x")
    assert ask("tmux -L live load-buffer rel.txt; tmux -L live paste-buffer", cwd=str(tmp_path)) is not None
    allowed("tmux -L live load-buffer /no/such/file; tmux -L live paste-buffer")
    allowed("tmux -L test load-buffer - ; tmux -L test paste-buffer")


def test_a_paste_of_a_buffer_this_line_did_not_fill_is_judged_on_what_the_server_holds(monkeypatch):
    seen = []

    def show(sock, name, env):
        seen.append((Path(sock).name, name))
        return "/rename x\n"

    monkeypatch.setattr(pane_keys, "_show_buffer", show)
    refused("tmux -L live paste-buffer -b mine -t %3", "buffer")
    assert seen == [("live", "mine")]
    allowed("tmux -L test paste-buffer -t %3")  # a test server: not asked
    monkeypatch.setattr(pane_keys, "_show_buffer", lambda *a: "hello\n")
    allowed("tmux -L live paste-buffer -t %3")


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_show_buffer_reads_a_real_server(monkeypatch):
    root = tempfile.mkdtemp(prefix="wsk", dir="/tmp")
    env = {**os.environ, "TMUX_TMPDIR": root, "TMUX": "", "TMUX_PANE": ""}
    try:
        subprocess.run(
            ["tmux", "-L", "live", "-f", "/dev/null", "new-session", "-d", "-s", "m", "sleep 60"], env=env, check=True
        )
        subprocess.run(["tmux", "-L", "live", "set-buffer", "-b", "mine", "/rename x"], env=env, check=True)
        sock = Path(root) / f"tmux-{os.getuid()}" / "live"
        assert REAL_SHOW_BUFFER(sock, "mine", env) == "/rename x"
        assert REAL_SHOW_BUFFER(sock, "nope", env) is None
    finally:
        subprocess.run(["tmux", "-L", "live", "kill-server"], env=env, capture_output=True, check=False)
        shutil.rmtree(root, ignore_errors=True)


# --- the shape of the guard ----------------------------------------------------------------------------------------------


def test_unrelated_commands_cost_nothing():
    for command in ("ls -la", "git commit -m 'send-keys /x'", "echo tmux send-keys '/x'", "grep send-keys notes.md"):
        allowed(command)


def test_inert_without_tmux(monkeypatch):
    monkeypatch.setattr(pane_keys.shutil, "which", lambda *a, **k: None)
    allowed("tmux -L live send-keys '/rename x' Enter")


def test_a_command_that_cant_be_read_is_refused_when_it_names_tmux():
    refused("tmux -L live send-keys '/x")  # an unterminated quote


def test_the_deny_can_be_approved_once_and_names_the_way_out():
    found = ask("tmux -L live send-keys '/rename x' Enter")
    assert found.approvable
    for words in ("--name X --remote-control X", "ws msg send", "cross-session messaging", "never as typed text"):
        assert words in found.reason


def test_the_config_names_one_key_and_loads_through_the_policy():
    assert pane_keys.config({}, "w") == pane_keys.Config()
    assert pane_keys.config({"live_socket": "ws"}, "w").live_socket == "ws"
    for bad in ({"live_socket": 3}, {"socket": "x"}):
        with pytest.raises(PolicyError):
            pane_keys.config(bad, "[guards.pane-keys]")
    assert (pane_keys.NAME, pane_keys.EVENTS, pane_keys.KINDS) == ("pane-keys", ("pre",), ("command",))
    pol = policy.loads('[guards]\non = ["pane-keys"]\n[guards.pane-keys]\nlive_socket = "live"\n')
    raw = (
        '{"session_id": "s1", "cwd": "/w", "tool_name": "Bash", "tool_input": '
        '{"command": "tmux -L live send-keys -t %3 \'/rename x\' Enter"}}'
    )
    reason = gate.check("claude-code", raw, pol)
    assert reason and reason.startswith("pane-keys: typing a slash command into a tmux pane is refused")
    assert gate.check("claude-code", raw.replace("/rename x", "ls"), pol) is None
