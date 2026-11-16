"""`ws ask`: the queue of questions a session leaves for its person, and the wall around what it may hold.

Every test runs in a scratch HOME with a policy of two classes (`open` is full, `priv` is walled) and a terms file.
The store is the file `ws home` reads, so one test reads it back through that source.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import threading
import time
import tokenize
from pathlib import Path

import pytest

from ws import asks, cli, paths

TERMS = "# one term per line\nsecretproject\n=ACME\n"


class Box:
    def __init__(self, tmp: Path, mp):
        self.home = tmp / "home"
        self.open = self.home / "code" / "open" / "app"
        self.priv = self.home / "code" / "priv" / "vault"
        self.loose = self.home / "code" / "loose"
        for d in (self.open, self.priv, self.loose):
            d.mkdir(parents=True)
        mp.setenv("HOME", str(self.home))
        mp.setenv("WS_HOME", str(self.home / ".ws"))
        mp.setenv("WS_ASK_TERMS", str(tmp / "terms.txt"))
        mp.delenv("WS_SESSION_NAME", raising=False)
        mp.delenv("WS_OUTPUT", raising=False)
        self.terms = tmp / "terms.txt"
        self.terms.write_text(TERMS)
        self.policy(priv_access="walled")
        self.store = self.home / ".claude-usage" / "asks" / "asks.jsonl"
        self.mp = mp

    def policy(self, priv_access: str, open_access: str = "full") -> None:
        cfg = paths.config_dir()
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "policy.toml").write_text(
            f'[classes.open]\naccess = "{open_access}"\nrepos = ["{self.home}/code/open/**", "{self.home}/code/open/app"]\n\n'
            f'[classes.priv]\naccess = "{priv_access}"\nrepos = ["{self.home}/code/priv/**"]\n'
        )

    def run(self, capsys, *argv, cwd: Path | None = None, stdin: str = ""):
        self.mp.chdir(cwd or self.open)
        self.mp.setattr(sys, "stdin", io.StringIO(stdin))
        code = cli.main(["ask", *argv])
        out = capsys.readouterr()
        return code, out.out, out.err

    def add(self, capsys, *argv, cwd: Path | None = None) -> str:
        code, out, err = self.run(capsys, "add", *argv, cwd=cwd)
        assert code == 0, err
        return out.strip()

    def lines(self) -> list[dict]:
        return [json.loads(x) for x in self.store.read_text().splitlines() if x.strip()]


def redacted(box, capsys, *flags) -> bool:
    ident = box.add(capsys, *flags)
    return asks.load()[ident]["redacted"]


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


def answered(ident: str, text: str = "fine") -> None:
    """An answer as another writer leaves it: `ws ask answer` is not in this change."""
    reply = {"text": text, "option_index": None, "via": "test", "ts": 1}
    asks._write(None, {"event": "answered", "id": ident, "answer": reply})


def test_round_trip_add_list_get(box, capsys):
    a = box.add(capsys, "--question", "Ship it?", "--option", "Yes|go now", "--option", "No", "--session", "bld")
    b = box.add(capsys, "--question", "Which?", "--option", "Red", "--option", "Blue")
    c = box.add(capsys, "--question", "Why?")
    code, out, _ = box.run(capsys, "list")
    assert code == 0 and [ln.split()[0] for ln in out.splitlines()] == [a, b, c]
    assert "Ship it?" in out
    answered(a)
    assert [ln.split()[0] for ln in box.run(capsys, "list")[1].splitlines()] == [b, c]  # an answered ask is not open
    code, out, _ = box.run(capsys, "list", "--all")
    assert [ln.split()[1] for ln in out.splitlines()] == ["answered", "open", "open"]
    assert asks.load()[a]["session"]["name"] == "bld"
    code, out, _ = box.run(capsys, "get", a)
    assert "1. Yes (Recommended) - go now" in out and "answer: fine  (free text, via test)" in out


def test_an_unknown_id_is_a_usage_error(box, capsys):
    assert box.run(capsys, "get", "nope")[0] == 2


def test_the_same_open_dedupe_key_returns_its_id_and_adds_nothing(box, capsys):
    a = box.add(capsys, "--question", "Q", "--dedupe-key", "k1")
    assert box.add(capsys, "--question", "Q again", "--dedupe-key", "k1") == a
    assert len(box.lines()) == 1
    answered(a)
    assert box.add(capsys, "--question", "Q", "--dedupe-key", "k1") != a  # an answered ask no longer dedupes


def test_add_reads_one_json_object_from_stdin(box, capsys):
    body = json.dumps({"question": "From stdin?", "options": [{"label": "A"}, "B"], "session": "s1", "repo": "app"})
    code, out, _ = box.run(capsys, "add", "--stdin", stdin=body)
    assert code == 0
    ask = asks.load()[out.strip()]
    assert ask["question"] == "From stdin?" and [o["label"] for o in ask["options"]] == ["A", "B"]
    assert box.run(capsys, "add", "--stdin", stdin="[1]")[0] == 2
    assert box.run(capsys, "add", "--stdin", stdin="not json")[0] == 2


def test_json_output_is_one_envelope(box, capsys):
    code, out, _ = box.run(capsys, "add", "--question", "Q?", "--json")
    env = json.loads(out)
    assert code == 0 and env["data"]["created"] is True and env["data"]["redacted"] is False


# --- the wall --------------------------------------------------------------------------------------------------------


def test_the_class_is_the_strictest_of_the_repo_name_and_the_working_folder(box, capsys):
    # a walled working folder naming an open repo is still redacted
    a = box.add(capsys, "--question", "plain", "--repo", "app", cwd=box.priv)
    assert (
        asks.load()[a]["redacted"] is True and asks.load()[a]["flows"] == "counts"
    )  # the walled folder wins over the open name
    # a name that is no class and no folder here is the strictest, even from an open folder
    u = box.add(capsys, "--question", "plain", "--repo", "nowhere", cwd=box.open)
    assert asks.load()[u]["redacted"] is True and asks.load()[u]["flows"] == "off"
    # an open working folder naming a walled repo is redacted too
    b = box.add(capsys, "--question", "plain", "--repo", "vault", cwd=box.open)
    assert asks.load()[b]["redacted"] is True
    # open and open stays plain
    c = box.add(capsys, "--question", "plain", "--repo", "app", cwd=box.open)
    assert asks.load()[c]["redacted"] is False and asks.load()[c]["question"] == "plain"


def test_a_path_in_no_class_is_the_strictest_when_the_policy_has_classes(box, capsys):
    a = box.add(capsys, "--question", "plain", cwd=box.loose)
    assert asks.load()[a]["redacted"] is True


def test_a_policy_with_no_classes_classifies_nothing_so_asks_are_plain(box, capsys):
    (paths.config_dir() / "policy.toml").write_text("")
    a = box.add(capsys, "--question", "plain", cwd=box.loose)
    assert asks.load()[a]["redacted"] is False


@pytest.mark.parametrize("text", ["about SecretProject today", "the ACME deal"])
def test_a_term_hit_redacts_in_any_text_field(box, capsys, text):
    for flags in (
        ["--question", text],
        ["--question", "ok", "--decision", text],
        ["--question", "ok", "--option", text],
    ):
        ident = box.add(capsys, *flags)
        assert asks.load()[ident]["redacted"] is True


def test_an_exact_case_term_is_a_whole_word_only(box, capsys):
    ident = box.add(capsys, "--question", "the acme and ACMEs and ACMEcorp")
    assert asks.load()[ident]["redacted"] is False  # ACME needs its exact case, standing alone


def test_a_missing_or_unreadable_terms_file_fails_safe_to_redacted(box, capsys):
    box.terms.unlink()
    assert redacted(box, capsys, "--question", "plain") is True
    box.terms.mkdir()  # a folder where the file should be: unreadable
    assert redacted(box, capsys, "--question", "plain two") is True
    box.terms.rmdir()
    box.terms.write_text("# no terms at all\n")  # readable and empty: nothing to trip on
    assert redacted(box, capsys, "--question", "plain three") is False


def test_a_redacted_ask_never_stores_its_text_and_stores_the_key_hashed(box, capsys):
    box.add(
        capsys, "--question", "uses secretproject", "--option", "secretproject yes|detail-text", "--decision", "d secretproject",
        "--dedupe-key", "my-key-123", "--session", "bld",
    )  # fmt: skip
    raw = box.store.read_text()
    for needle in ("secretproject", "my-key-123", "detail-text"):
        assert needle not in raw
    (ev,) = box.lines()
    assert ev["question"] == asks.OPAQUE and ev["options"] == [] and ev["decision"] == ""
    assert ev["dedupe_key"].startswith("sha256:") and ev["redacted"] is True
    # the hashed key still dedupes the plain key
    again = box.add(capsys, "--question", "uses secretproject", "--dedupe-key", "my-key-123")
    assert again == ev["id"] and len(box.lines()) == 1


def test_a_token_shaped_secret_is_refused_and_stored_nowhere(box, capsys):
    token = "ghp_" + "a1B2c3D4" * 5
    for flags in (
        ["--question", f"use {token}"],
        ["--question", "ok", "--option", token],
        ["--question", "ok", "--dedupe-key", token],
    ):
        code, _, err = box.run(capsys, "add", *flags)
        assert code == 1 and token not in err and "secret" in err
    assert not box.store.exists()


def test_the_class_is_checked_again_when_an_ask_is_read(box, capsys):
    ident = box.add(capsys, "--question", "Readable now?", "--option", "Yes")
    assert "Readable now?" in box.run(capsys, "list")[1]
    box.policy(priv_access="walled", open_access="walled")  # the repo is walled after it was asked
    out = box.run(capsys, "list")[1]
    assert asks.OPAQUE in out and "Readable now?" not in out
    out = box.run(capsys, "get", ident)[1]
    assert asks.OPAQUE in out and "Readable now?" not in out and "Yes" not in out


def test_the_folder_is_private_and_so_is_the_file(box, capsys):
    box.add(capsys, "--question", "Q")
    assert stat.S_IMODE(os.stat(box.store.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(box.store).st_mode) == 0o600


def test_a_folder_made_wider_earlier_is_narrowed(box, capsys):
    box.store.parent.mkdir(parents=True, mode=0o755)
    os.chmod(box.store.parent, 0o755)
    box.add(capsys, "--question", "Q")
    assert stat.S_IMODE(os.stat(box.store.parent).st_mode) == 0o700


# --- the one store ---------------------------------------------------------------------------------------------------


def test_ws_home_reads_what_ask_wrote(box, capsys):
    from ws.home import api
    from ws.home.sources import asks_jsonl

    box.add(capsys, "--question", "Ship it?", "--option", "Yes|go", "--option", "No", "--session", "bld")
    (got,) = asks_jsonl.read("asks", api.Env.make({"HOME": str(box.home), "WS_HOME": str(box.home / ".ws")}))
    assert got.title == "Ship it?" and [o["label"] for o in got.options] == ["Yes", "No"] and got.session_name == "bld"


def test_lines_the_opencode_shim_wrote_still_fold(box):
    box.store.parent.mkdir(parents=True)
    old = [
        {"schema": 1, "event": "added", "id": "p1", "ts": 1760000000000, "repo": "app", "question": "Allow?", "options": [],
         "source": "permission", "session": {"name": "oc"}, "decision": "run ls", "redacted": False},
        {"schema": 1, "event": "added", "id": "p2", "ts": 1760000001000, "repo": "app", "question": "Allow 2?", "redacted": False},
        {"schema": 1, "event": "answered", "id": "p2", "ts": 1760000002000, "decision": "allow"},
    ]  # fmt: skip
    box.store.write_text("\n" + "\n\n".join(json.dumps(x) for x in old) + "\n")
    got = asks.load()
    assert (
        got["p1"]["status"] == "open" and got["p2"]["status"] == "answered" and got["p2"]["answer"]["text"] == "allow"
    )
    assert asks.add({"question": "new"}, str(box.open))[1] is True
    assert set(asks.load()) >= {"p1", "p2"}


def test_two_writers_at_once_lose_no_line(box, capsys):
    errs = []

    def one(n):
        try:
            asks.add({"question": f"q{n}"}, str(box.open))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=one, args=(n,)) for n in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(asks.load()) == 12
    assert all(json.loads(x) for x in box.store.read_text().split("\n") if x.strip())


def test_every_line_has_a_numeric_ts_and_an_rfc_3339_at(box, capsys):
    before = int(time.time() * 1000)
    box.add(capsys, "--question", "Q")
    (ev,) = box.lines()
    assert isinstance(ev["ts"], int) and before <= ev["ts"] and ev["schema"] == asks.SCHEMA
    assert ev["at"].endswith("Z") and "T" in ev["at"]


def test_cli_py_does_not_know_about_asks():
    """The command is found by its `COMMAND =` line; nothing in cli.py names the module."""
    text = (Path(__file__).resolve().parents[1] / "src" / "ws" / "cli.py").read_text(encoding="utf-8")
    names = {t.string for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.NAME}
    assert "asks" not in names and "ws.asks" not in text


# --- the older program's rows and lock; two writers at once ----------------------------------------------------------

OLD_ROWS = [
    {"v": 1, "ts": "2026-11-01T01:02:03Z", "event": "added", "id": "a20261101T010203-ab12", "repo": "app",
     "session": {"name": "bld", "tmux": None}, "question": "Ship it?", "options": [{"label": "Yes", "description": ""}],
     "free_text_allowed": True, "decision": "", "source": "session", "dedupe_key": None, "flows": "full",
     "redacted": False},
    {"v": 1, "ts": "2026-11-01T01:05:00Z", "event": "added", "id": "a20261101T010500-cd34", "repo": "app",
     "session": {"name": "bld", "tmux": None}, "question": "Left open?", "options": [], "free_text_allowed": True,
     "decision": "", "source": "session", "dedupe_key": None, "flows": "full", "redacted": False},
    {"v": 1, "ts": "2026-11-01T01:06:00Z", "event": "answered", "id": "a20261101T010203-ab12",
     "answer": {"text": "Yes", "option_index": 0, "via": "page", "ts": "2026-11-01T01:06:00Z"}},
]  # fmt: skip


def old_store(box) -> None:
    box.store.parent.mkdir(parents=True)
    box.store.write_text("".join(json.dumps(r) + "\n" for r in OLD_ROWS))


def test_the_older_programs_rows_read_with_their_times(box, capsys):
    old_store(box)
    got = asks.load()
    assert got["a20261101T010203-ab12"]["status"] == "answered" and "v" not in got["a20261101T010203-ab12"]
    assert (
        got["a20261101T010500-cd34"]["ts"] == 1793495100000 and asks.age_text(got["a20261101T010500-cd34"]["ts"]) != "?"
    )
    code, out, _ = box.run(capsys, "list")
    assert code == 0 and "Left open?" in out and "  ?  " not in out


def test_a_write_takes_the_older_programs_asks_lock(box, capsys, monkeypatch):
    import fcntl

    box.store.parent.mkdir(parents=True)
    monkeypatch.setattr(asks, "LOCK_WAIT_S", 0.2)
    fd = os.open(box.store.parent / "asks.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, _, err = box.run(capsys, "add", "--question", "Q")
        assert code == 1 and "asks store" in err
    finally:
        os.close(fd)
    assert box.run(capsys, "add", "--question", "Q")[0] == 0  # and it goes ahead once the lock is let go
    assert stat.S_IMODE((box.store.parent / "asks.lock").stat().st_mode) == 0o600


def slow_between_check_and_write(monkeypatch):
    """Both writers get past their check before either writes, unless the lock keeps them apart."""
    real = asks._now

    def slow():
        time.sleep(0.05)
        return real()

    monkeypatch.setattr(asks, "_now", slow)


def test_two_adds_with_one_key_make_one_ask(box, capsys, monkeypatch):
    slow_between_check_and_write(monkeypatch)
    ids, errs = [], []

    def one():
        try:
            ids.append(asks.add({"question": "Q", "dedupe_key": "k"}, str(box.open)))
        except asks.WsError as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=one) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert len({i[0] for i in ids}) == 1 and sum(1 for i in ids if i[1]) == 1
    assert [e["event"] for e in box.lines()] == ["added"]
