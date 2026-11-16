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


def test_round_trip_add_list_answer_by_number_label_and_text(box, capsys):
    a = box.add(capsys, "--question", "Ship it?", "--option", "Yes|go now", "--option", "No", "--session", "bld")
    b = box.add(capsys, "--question", "Which?", "--option", "Red", "--option", "Blue")
    c = box.add(capsys, "--question", "Why?")
    code, out, _ = box.run(capsys, "list")
    assert code == 0 and [ln.split()[0] for ln in out.splitlines()] == [a, b, c]
    assert "Ship it?" in out
    assert box.run(capsys, "answer", a, "2")[1].strip() == "answered: No"
    assert box.run(capsys, "answer", b, "BLUE")[1].strip() == "answered: Blue"
    assert box.run(capsys, "answer", c, "because", "reasons")[1].strip() == "answered: because reasons"
    assert box.run(capsys, "list")[1].strip() == ""  # none open now
    code, out, _ = box.run(capsys, "list", "--all")
    assert [ln.split()[1] for ln in out.splitlines()] == ["answered"] * 3
    got = asks.load()
    assert got[a]["answer"]["option_index"] == 1 and got[b]["answer"]["option_index"] == 1
    assert got[c]["answer"]["option_index"] is None and got[a]["session"]["name"] == "bld"
    code, out, _ = box.run(capsys, "get", a)
    assert "1. Yes (Recommended) - go now" in out and "answer: No  (option 2, via cli)" in out
    assert box.run(capsys, "answer", a, "1")[0] == 1  # already answered


def test_an_unknown_id_is_a_usage_error(box, capsys):
    assert box.run(capsys, "get", "nope")[0] == 2
    assert box.run(capsys, "answer", "nope", "x")[0] == 2


def test_the_same_open_dedupe_key_returns_its_id_and_adds_nothing(box, capsys):
    a = box.add(capsys, "--question", "Q", "--dedupe-key", "k1")
    assert box.add(capsys, "--question", "Q again", "--dedupe-key", "k1") == a
    assert len(box.lines()) == 1
    box.run(capsys, "answer", a, "fine")
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
    ident = box.add(capsys, "--question", "ok", "--option", "x")
    code, _, err = box.run(capsys, "answer", ident, token)
    assert code == 1 and token not in err and token not in box.store.read_text()


def test_the_class_is_checked_again_when_an_ask_is_read(box, capsys):
    ident = box.add(capsys, "--question", "Readable now?", "--option", "Yes")
    assert "Readable now?" in box.run(capsys, "list")[1]
    box.policy(priv_access="walled", open_access="walled")  # the repo is walled after it was asked
    code, out, _ = box.run(capsys, "list")
    assert asks.OPAQUE in out and "Readable now?" not in out
    code, out, _ = box.run(capsys, "get", ident)
    assert asks.OPAQUE in out and "Readable now?" not in out and "Yes" not in out
    code, _, err = box.run(capsys, "answer", ident, "1")
    assert code == 1 and "Open the session" in err
    assert [e["event"] for e in box.lines()] == ["added"]  # nothing was answered


def test_a_redacted_ask_cannot_be_answered_here(box, capsys):
    ident = box.add(capsys, "--question", "secretproject?")
    assert box.run(capsys, "answer", ident, "yes")[0] == 1


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


def test_an_older_open_ask_can_be_answered_and_stays_one_ask(box, capsys):
    old_store(box)
    assert box.run(capsys, "answer", "a20261101T010500-cd34", "fine")[0] == 0
    assert asks.load()["a20261101T010500-cd34"]["answer"]["text"] == "fine"
    assert [e["event"] for e in box.lines()].count("added") == 2


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


def test_two_answers_to_one_ask_only_one_succeeds(box, capsys, monkeypatch):
    ident = box.add(capsys, "--question", "Q", "--option", "Yes", "--option", "No")
    slow_between_check_and_write(monkeypatch)
    won, lost = [], []

    def one(n):
        try:
            won.append(asks.answer(ident, str(n)))
        except asks.WsError as e:
            lost.append(e.kind)

    ts = [threading.Thread(target=one, args=(1 + n % 2,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(won) == 1 and lost == ["not_open"] * 7
    assert [e["event"] for e in box.lines()] == ["added", "answered"]


# --- wait, done, withdraw ----------------------------------------------------------------------------------------------


def test_wait_returns_the_answer_when_it_comes(box, capsys, monkeypatch):
    monkeypatch.setattr(asks, "POLL_S", 0.02)
    ident = box.add(capsys, "--question", "Q", "--option", "Yes", "--option", "No")
    threading.Timer(0.2, lambda: asks.answer(ident, "2")).start()
    code, out, _ = box.run(capsys, "wait", ident, "--timeout", "10")
    assert code == 0 and out.strip() == "No"


def test_wait_times_out_with_exit_3_and_still_open(box, capsys, monkeypatch):
    monkeypatch.setattr(asks, "POLL_S", 0.02)
    ident = box.add(capsys, "--question", "Q")
    code, out, _ = box.run(capsys, "wait", ident, "--timeout", "0.1")
    assert code == 3 and out.strip() == "still open"


@pytest.mark.parametrize("verb", ["withdraw", "done"])
def test_wait_on_a_closed_ask_with_no_answer_is_exit_4(box, capsys, verb):
    ident = box.add(capsys, "--question", "Q")
    box.run(capsys, verb, ident)
    code, out, _ = box.run(capsys, "wait", ident, "--timeout", "1")
    assert code == 4 and out.strip() == ("withdrawn" if verb == "withdraw" else "done")


def test_wait_with_an_unknown_id_is_2_and_a_negative_timeout_is_2(box, capsys):
    assert box.run(capsys, "wait", "nope")[0] == 2
    ident = box.add(capsys, "--question", "Q")
    assert box.run(capsys, "wait", ident, "--timeout", "-1")[0] == 2


def test_wait_sees_an_answer_already_there(box, capsys):
    ident = box.add(capsys, "--question", "Q")
    box.run(capsys, "answer", ident, "yes")
    code, out, _ = box.run(capsys, "wait", ident, "--timeout", "0")
    assert code == 0 and out.strip() == "yes"


def test_withdraw_closes_an_open_ask_and_is_a_no_op_after(box, capsys):
    ident = box.add(capsys, "--question", "Q")
    assert box.run(capsys, "withdraw", ident)[1].strip() == "withdrawn"
    assert box.run(capsys, "list")[1].strip() == ""
    assert box.run(capsys, "withdraw", ident)[1].strip() == "withdrawn"
    assert box.run(capsys, "done", ident)[1].strip() == "withdrawn"  # a withdrawn ask is not done
    assert [e["event"] for e in box.lines()] == ["added", "withdrawn"]
    assert box.run(capsys, "answer", ident, "x")[0] == 1  # and it can no longer be answered


def test_done_closes_open_and_answered_asks_and_is_a_no_op_after(box, capsys):
    a = box.add(capsys, "--question", "A")
    b = box.add(capsys, "--question", "B")
    box.run(capsys, "answer", b, "yes")
    assert box.run(capsys, "done", a)[1].strip() == "done"
    assert box.run(capsys, "done", b)[1].strip() == "done"
    assert box.run(capsys, "done", b)[1].strip() == "done"
    assert box.run(capsys, "withdraw", b)[1].strip() == "done"
    assert [e["event"] for e in box.lines()] == ["added", "added", "answered", "done", "done"]
    assert asks.load()[b]["answer"]["text"] == "yes"  # the answer stays on a done ask


def test_done_and_withdraw_on_an_unknown_id_are_2(box, capsys):
    assert box.run(capsys, "done", "nope")[0] == 2
    assert box.run(capsys, "withdraw", "nope")[0] == 2


def test_ws_home_stops_showing_a_done_ask(box, capsys):
    from ws.home import api
    from ws.home.sources import asks_jsonl

    ident = box.add(capsys, "--question", "Q")
    env = api.Env.make({"HOME": str(box.home), "WS_HOME": str(box.home / ".ws")})
    assert [a.id for a in asks_jsonl.read("asks", env)] == [ident]
    box.run(capsys, "done", ident)
    assert asks_jsonl.read("asks", env) == []


# --- import ----------------------------------------------------------------------------------------------------------

OLD = [
    {"v": 1, "ts": "2026-11-01T01:02:03Z", "event": "added", "id": "a20261101T010203-ab12", "repo": "app",
     "session": {"name": "bld", "tmux": "w:1"}, "rc_url": None, "question": "Ship it?",
     "options": [{"label": "Yes", "description": "go"}, {"label": "No", "description": ""}],
     "free_text_allowed": True, "decision": "the release", "source": "session", "dedupe_key": "k", "flows": "full",
     "redacted": False},
    {"v": 1, "ts": "2026-11-01T01:05:00Z", "event": "answered", "id": "a20261101T010203-ab12",
     "answer": {"text": "Yes", "option_index": 0, "via": "page", "ts": "2026-11-01T01:05:00Z"}},
    {"v": 1, "ts": "2026-11-02T00:00:00Z", "event": "added", "id": "a20261102T000000-cd34", "repo": "vault",
     "session": {"name": "", "tmux": None}, "rc_url": None, "question": "Open the session to see this ask.",
     "options": [], "free_text_allowed": False, "decision": "", "source": "session", "dedupe_key": "sha256:abc",
     "flows": "counts", "redacted": True},
    {"v": 1, "ts": "2026-11-03T00:00:00Z", "event": "added", "id": "a20261103T000000-ef56", "repo": "app",
     "session": {"name": "x", "tmux": None}, "rc_url": None, "question": "Left open?", "options": [],
     "free_text_allowed": True, "decision": "", "source": "session", "dedupe_key": None, "flows": "full",
     "redacted": False},
    {"v": 1, "ts": "2026-11-03T00:10:00Z", "event": "withdrawn", "id": "a20261103T000000-ef56"},
]  # fmt: skip


def old_file(tmp_path, extra: str = "") -> Path:
    p = tmp_path / "old-asks.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in OLD) + "\n" + extra)
    return p


def test_import_converts_the_old_rows_into_events(box, capsys, tmp_path):
    code, out, _ = box.run(capsys, "import", str(old_file(tmp_path)))
    assert code == 0 and "imported 5 event(s); 0 already there; 0 skipped" in out
    got = asks.load()
    first = got["a20261101T010203-ab12"]
    assert first["status"] == "answered" and first["answer"]["text"] == "Yes" and first["answer"]["via"] == "page"
    assert first["ts"] == 1793494923000 and first["at"] == "2026-11-01T01:02:03.000Z" and "v" not in first
    assert got["a20261103T000000-ef56"]["status"] == "withdrawn"
    assert [e["schema"] for e in box.lines()] == [asks.SCHEMA] * 5


def test_import_twice_adds_nothing_the_second_time(box, capsys, tmp_path):
    src = old_file(tmp_path)
    box.run(capsys, "import", str(src))
    count = len(box.lines())
    code, out, _ = box.run(capsys, "import", str(src))
    assert code == 0 and "imported 0 event(s); 5 already there" in out
    assert len(box.lines()) == count == 5


def test_a_redacted_row_stays_redacted_and_gains_no_text(box, capsys, tmp_path):
    box.run(capsys, "import", str(old_file(tmp_path)))
    ask = asks.load()["a20261102T000000-cd34"]
    assert ask["redacted"] is True and ask["question"] == asks.OPAQUE and ask["options"] == []
    shown = asks.view(ask)
    assert shown["opaque"] is True and shown["dedupe_key"] is None
    assert box.run(capsys, "answer", ask["id"], "yes")[0] == 1


def test_a_row_without_an_explicit_redacted_false_is_imported_redacted(box, capsys, tmp_path):
    row = {k: v for k, v in OLD[3].items() if k != "redacted"} | {"id": "a20261104T000000-aa11"}
    p = tmp_path / "o.jsonl"
    p.write_text(json.dumps(row) + "\n")
    box.run(capsys, "import", str(p))
    assert asks.load()["a20261104T000000-aa11"]["redacted"] is True


def test_dry_run_writes_nothing_and_says_what_it_would_do(box, capsys, tmp_path):
    code, out, _ = box.run(capsys, "import", str(old_file(tmp_path)), "--dry-run")
    assert code == 0 and "would import 5 event(s)" in out
    assert not box.store.exists() and not box.store.parent.exists()


def test_import_skips_bad_rows_and_closings_of_unknown_asks(box, capsys, tmp_path):
    junk = 'not json\n{"v": 2, "event": "added", "id": "zz", "ts": "2026-11-01T00:00:00Z"}\n'
    junk += json.dumps({"v": 1, "ts": "2026-11-01T00:00:00Z", "event": "done", "id": "ghost"}) + "\n"
    code, out, _ = box.run(capsys, "import", str(old_file(tmp_path, junk)))
    assert code == 0 and "imported 5 event(s); 0 already there; 3 skipped" in out


def test_import_of_a_missing_file_is_a_usage_error(box, capsys, tmp_path):
    assert box.run(capsys, "import", str(tmp_path / "none.jsonl"))[0] == 2


def test_imported_asks_are_what_ws_home_reads(box, capsys, tmp_path):
    from ws.home import api
    from ws.home.sources import asks_jsonl

    box.run(capsys, "import", str(old_file(tmp_path)))
    got = asks_jsonl.read("asks", api.Env.make({"HOME": str(box.home), "WS_HOME": str(box.home / ".ws")}))
    assert [a.id for a in got] == ["a20261102T000000-cd34"]  # the redacted one is the only one still open


def test_ask_help_exits_0(box, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["ask", "--help"])
    assert e.value.code == 0
    assert "import" in capsys.readouterr().out


def test_notify_is_a_hook_that_does_nothing_by_default(box, capsys):
    assert asks.NOTIFY_HOOK is None


def test_the_notify_hook_runs_once_for_a_new_ask_and_gets_no_text_of_a_redacted_one(box, capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(asks, "NOTIFY_HOOK", seen.append)
    box.add(capsys, "--question", "plain", "--dedupe-key", "k")
    box.add(capsys, "--question", "plain", "--dedupe-key", "k")  # the same open ask: no second call
    box.add(capsys, "--question", "uses secretproject")
    assert len(seen) == 2 and seen[1]["question"] == asks.OPAQUE


def test_a_redacted_row_comes_in_with_no_text_at_all(box, capsys, tmp_path):
    row = {k: v for k, v in OLD[3].items() if k != "redacted"} | {"id": "a20261104T000000-bb22"}
    row |= {"question": "leaky-question", "decision": "leaky-decision", "dedupe_key": "leaky-key", "cwd": "/leaky/dir"}
    row |= {"options": [{"label": "leaky-label", "description": ""}], "session": {"name": "leaky-name", "tmux": None}}
    p = tmp_path / "o.jsonl"
    p.write_text(json.dumps(row) + "\n")
    box.run(capsys, "import", str(p))
    stored = box.store.read_text()
    assert "leaky" not in stored and asks.OPAQUE in stored and "sha256:" in stored


def test_an_opaque_view_drops_the_folder_and_a_redacted_ask_keeps_no_session_name(box, capsys):
    ident = box.add(capsys, "--question", "uses secretproject", "--session", "who", cwd=box.open)
    assert box.lines()[0]["session"]["name"] == "" and box.lines()[0]["cwd"] == ""
    ok = box.add(capsys, "--question", "plain", "--session", "who", cwd=box.open)
    box.policy(priv_access="walled", open_access="walled")
    assert asks.view(asks.get(ok))["cwd"] == "" and asks.view(asks.get(ident))["cwd"] == ""


def test_reading_uses_the_strictest_of_the_name_and_the_folder_as_adding_does(box, capsys):
    ident = box.add(capsys, "--question", "plain", "--repo", "app", cwd=box.open)
    assert asks.view(asks.get(ident))["opaque"] is False
    box.policy(priv_access="walled", open_access="walled")  # the folder is walled now; the name still says open
    assert asks.view(asks.get(ident))["opaque"] is True


def answered(ident: str, text: str) -> None:
    """An answer as another writer leaves it: `ws ask answer` refuses an opaque ask, so write the event directly."""
    asks._write(
        None, {"event": "answered", "id": ident, "answer": {"text": text, "option_index": None, "via": "test", "ts": 1}}
    )


def test_wait_does_not_print_the_answer_to_an_opaque_ask(box, capsys):
    ident = box.add(capsys, "--question", "uses secretproject")  # stored redacted
    answered(ident, "planted-answer-text")
    code, out, err = box.run(capsys, "wait", ident, "--timeout", "1")
    assert code == 0 and "planted-answer-text" not in out + err and out.strip() == "(hidden)"
    code, out, _ = box.run(capsys, "wait", ident, "--timeout", "1", "--json")
    assert code == 0 and "planted-answer-text" not in out


def test_wait_does_not_print_the_answer_once_the_repo_is_walled(box, capsys):
    ident = box.add(capsys, "--question", "plain")
    answered(ident, "planted-answer-text")
    box.policy(priv_access="walled", open_access="walled")
    assert "planted-answer-text" not in box.run(capsys, "wait", ident, "--timeout", "1")[1]


# --- review follow-ups: pick, terms in answers, the import lock, old answers, nan ----------------------------------------


def test_an_answer_with_a_superscript_digit_is_free_text_not_a_crash(box, capsys):
    ident = box.add(capsys, "--question", "Q", "--option", "Yes", "--option", "No")
    code, out, _ = box.run(capsys, "answer", ident, "²")
    assert code == 0 and out.strip() == "answered: ²" and asks.load()[ident]["answer"]["option_index"] is None


def test_options_that_are_not_labels_do_not_crash_an_answer(box, capsys):
    ident = box.add(capsys, "--question", "Q")
    asks._write(None, {"event": "added", "id": "odd1", "repo": "app", "cwd": str(box.open), "question": "Q", "flows": "full",
                       "options": ["plain-string", {"description": "no label"}, None], "redacted": False})  # fmt: skip
    code, out, _ = box.run(capsys, "answer", "odd1", "1")
    assert code == 0 and out.strip() == "answered: 1" and ident


def test_a_label_beats_a_number(box, capsys):
    ident = box.add(capsys, "--question", "Q", "--option", "2", "--option", "1")
    box.run(capsys, "answer", ident, "2")
    assert asks.load()[ident]["answer"]["option_index"] == 0  # the option labelled "2", not the second one


def test_a_free_text_answer_with_a_sensitive_term_is_refused_and_not_stored(box, capsys):
    ident = box.add(capsys, "--question", "Q")
    code, _, err = box.run(capsys, "answer", ident, "about secretproject")
    assert code == 1 and "secretproject" not in err and "secretproject" not in box.store.read_text()
    assert [e["event"] for e in box.lines()] == ["added"]
    assert box.run(capsys, "answer", ident, "fine")[0] == 0  # a plain answer still goes in


def hold_lock(box):
    import fcntl

    box.store.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(box.store.parent / "asks.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_import_done_and_withdraw_wait_for_the_asks_lock(box, capsys, monkeypatch, tmp_path):
    ident = box.add(capsys, "--question", "Q")
    monkeypatch.setattr(asks, "LOCK_WAIT_S", 0.2)
    fd = hold_lock(box)
    try:
        assert box.run(capsys, "import", str(old_file(tmp_path)))[0] == 1
        assert box.run(capsys, "done", ident)[0] == 1
        assert box.run(capsys, "withdraw", ident)[0] == 1
        assert box.run(capsys, "import", str(old_file(tmp_path)), "--dry-run")[0] == 0  # a dry run takes no lock
    finally:
        os.close(fd)
    assert [e["event"] for e in box.lines()] == ["added"]


def test_an_old_answer_to_a_redacted_ask_comes_in_with_no_text(box, capsys, tmp_path):
    rows = [OLD[2], {"v": 1, "ts": "2026-11-02T00:05:00Z", "event": "answered", "id": OLD[2]["id"],
                     "answer": {"text": "leaky-answer", "option_index": None, "via": "page", "ts": "2026-11-02T00:05:00Z"}}]  # fmt: skip
    p = tmp_path / "o.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    code = box.run(capsys, "import", str(p))[0]
    assert code == 0 and "leaky-answer" not in box.store.read_text()
    assert box.run(capsys, "wait", OLD[2]["id"], "--timeout", "1")[1].strip() == "(hidden)"


def test_an_old_row_with_options_that_are_not_labels_is_skipped(box, capsys, tmp_path):
    row = OLD[0] | {"options": ["just-a-string"]}
    p = tmp_path / "o.jsonl"
    p.write_text(json.dumps(row) + "\n")
    assert "1 skipped" in box.run(capsys, "import", str(p))[1]


def test_wait_with_a_timeout_of_nan_or_inf_is_a_usage_error(box, capsys):
    ident = box.add(capsys, "--question", "Q")
    assert box.run(capsys, "wait", ident, "--timeout", "nan")[0] == 2
    assert box.run(capsys, "wait", ident, "--timeout", "inf")[0] == 2
