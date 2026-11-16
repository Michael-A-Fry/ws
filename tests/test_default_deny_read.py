"""The default `deny_read` that `ws gate init` writes: a project's `.env`, signed-in browser profiles and the GitHub CLI's
token file are unreadable to a session, and what sits beside them (`.env.example`, the CLI's other config) is not."""

from __future__ import annotations

from pathlib import Path

import pytest

from ws import policy


@pytest.fixture
def pol(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    return policy.default_policy(), tmp_path


@pytest.mark.parametrize(
    "rel",
    [
        "proj/.env",
        "proj/sub/dir/.env",
        "proj/.env.local",
        "proj/.env.production",
        "proj/.env.staging.local",
        "proj/.env.test",
        "proj/.envrc",
        ".config/google-chrome-beta/Default/Cookies",
        ".config/vivaldi/Default/Cookies",
        "snap/firefox/common/.mozilla/firefox/abc.default/logins.json",
        "snap/chromium/common/chromium/Default/Cookies",
        ".var/app/org.mozilla.firefox/config/x",
        "Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies",
        "Library/Application Support/Microsoft Edge/Default/Cookies",
        "Library/Application Support/Google/Chrome Beta/Default/Cookies",
        "Library/Application Support/Vivaldi/Default/Cookies",
        ".config/google-chrome/Default/Cookies",
        ".config/chromium/Default/Login Data",
        ".config/BraveSoftware/Brave-Browser/Default/Cookies",
        ".mozilla/firefox/abc.default/logins.json",
        "Library/Application Support/Google/Chrome/Default/Cookies",
        ".config/gh/hosts.yml",
        ".ssh/id_ed25519",
    ],
)
def test_these_are_denied(pol, rel):
    p, home = pol
    assert policy.is_denied_read(p, str(home / rel), cwd=str(home / "proj")), rel


@pytest.mark.parametrize("rel", ["proj/.env.example", "proj/README.md", ".config/gh/config.yml", ".config/git/config"])
def test_these_stay_readable(pol, rel):
    p, home = pol
    assert policy.is_denied_read(p, str(home / rel), cwd=str(home / "proj")) is None, rel


def test_the_template_ws_gate_init_writes_names_each_rule():
    text = policy.default_toml()
    for rule in (".env", "~/.config/google-chrome/**", "~/.mozilla/**", "~/.config/gh/hosts.yml"):
        assert f'"{rule}"' in text, rule


def test_the_known_limit_a_recursive_search_naming_only_a_folder_is_not_stopped_and_the_doc_says_so(pol):
    """A bare-name rule has no folder to hide (pinned): if this ever starts denying, drop the sentence in policy.md."""
    p, _ = pol
    assert policy._rule_root(".env") is None
    assert policy.is_denied_command(p, "cat proj/.env")  # naming the file is stopped
    assert policy.is_denied_command(p, "grep -r KEY .") is None  # naming a folder is not
    doc = (Path(__file__).parent.parent / "docs" / "reference" / "policy.md").read_text(encoding="utf-8")
    assert "grep -r KEY ." in doc and "can't hide it" in doc
