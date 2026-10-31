"""The PII gate catches a private IP, a tailnet host, an e-mail and a denylisted name, and never prints the match.
Planted values are built at run time so this file passes the gate itself."""

import os
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "scripts" / "pii_gate.py"
IP = ".".join(["192", "168", "7", "21"])
TAILNET = "box." + "tail" + "abc123" + ".ts" + ".net"
MAIL = "someone" + "@" + "corp-mail.co.nz"
NAME = "Zebedee " + "Quintrell"


def run(tmp_path, text, denylist=NAME, extra_env=None):
    repo = tmp_path / "r"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "f.md").write_text(text)
    env = {**os.environ, "WS_PII_DENYLIST": denylist, **(extra_env or {})}
    env.pop("WS_PII_DENYLIST_FILE", None)
    if not denylist:
        env.pop("WS_PII_DENYLIST")
        env["WS_PII_DENYLIST_FILE"] = str(tmp_path / "none")
    return subprocess.run(
        [sys.executable, str(GATE), "f.md"], cwd=repo, env=env, capture_output=True, text=True, check=False
    )


def test_clean_file_passes(tmp_path):
    assert run(tmp_path, "nothing private here, see 127.0.0.1 and 203.0.113.5\n").returncode == 0


def test_each_planted_value_fails_without_echoing_it(tmp_path):
    for value, rule in (
        (IP, "private IP"),
        (TAILNET, "tailnet host"),
        (MAIL, "e-mail address"),
        (NAME.lower(), "denylist term #1"),
    ):
        r = run(tmp_path, f"line one\nsee {value} here\n")
        assert r.returncode == 1, value
        assert f"f.md:2: {rule}" in r.stdout
        assert value.lower() not in (r.stdout + r.stderr).lower()


def test_no_denylist_refuses(tmp_path):
    assert run(tmp_path, "clean\n", denylist="").returncode == 2


def test_no_denylist_optional_for_forks(tmp_path):
    r = run(tmp_path, "clean\n", denylist="", extra_env={"WS_PII_DENYLIST_OPTIONAL": "1"})
    assert r.returncode == 0


def test_allow_file_skips_a_false_positive(tmp_path):
    (tmp_path / "r").mkdir()
    v = ".".join(["10", "1", "2", "3"])
    (tmp_path / "r" / ".pii-allow").write_text("version " + v.replace(".", r"\.") + "\n")
    assert run(tmp_path, f"version {v} of the tool\n").returncode == 0
