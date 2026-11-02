"""The OpenCode source: sessions read from its SQLite store (layout as seen in 1.19.4), the opaque rule, the typed rows
for a store that can't be read, the merge with ws's own session records, and the fields it never reads.

Every store is built at run time under `tmp_path` from the table layout OpenCode writes; no real data is copied.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3

import pytest

from home import NOW, SID, Box, iso
from ws.home import api
from ws.home.sources import SourceError, opencode

OC1 = "ses_aaaa00000001AAAAAAAAAAAAAA"
OC2 = "ses_bbbb00000002BBBBBBBBBBBBBB"
OC3 = "ses_cccc00000003CCCCCCCCCCCCCC"
SECRET = "sentinel-" + os.urandom(6).hex()  # text that must never reach the output

# the layout of 1.19.4's `session` and `message` tables (the columns, their order and defaults), written out here
SESSION_DDL = """CREATE TABLE session (
  id text PRIMARY KEY, project_id text NOT NULL, workspace_id text, parent_id text, slug text NOT NULL,
  directory text NOT NULL, path text, title text NOT NULL, version text NOT NULL, share_url text,
  summary_additions integer, summary_deletions integer, summary_files integer, summary_diffs text, metadata text,
  cost real DEFAULT 0 NOT NULL, tokens_input integer DEFAULT 0 NOT NULL, tokens_output integer DEFAULT 0 NOT NULL,
  tokens_reasoning integer DEFAULT 0 NOT NULL, tokens_cache_read integer DEFAULT 0 NOT NULL,
  tokens_cache_write integer DEFAULT 0 NOT NULL, revert text, permission text, agent text, model text,
  time_created integer NOT NULL, time_updated integer NOT NULL, time_compacting integer, time_archived integer)"""
MESSAGE_DDL = """CREATE TABLE message (
  id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL,
  data text NOT NULL)"""
PART_DDL = """CREATE TABLE part (
  id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL, time_created integer NOT NULL,
  time_updated integer NOT NULL, data text NOT NULL)"""


def ms(offset_s: float = 0.0) -> int:
    return int((NOW + offset_s) * 1000)


class Store:
    """An OpenCode store under a tmp data folder, with helpers to put sessions and messages in it."""

    def __init__(self, box: Box, wal: bool = False) -> None:
        self.box = box
        self.folder = box.home / "xdg-data" / "opencode"
        self.folder.mkdir(parents=True)
        self.path = self.folder / "opencode.db"
        box.environ["XDG_DATA_HOME"] = str(box.home / "xdg-data")
        self.db = sqlite3.connect(self.path)
        if wal:
            self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute(SESSION_DDL)
        self.db.execute(MESSAGE_DDL)
        self.n = 0

    def session(self, sid: str, cwd: str, title: str = "a title", updated: float = -30, created: float = -3600,
                parent: str | None = None, archived: int | None = None, cost: float = 0.0,
                tokens: tuple = (0, 0, 0, 0)) -> str:  # fmt: skip
        t_in, t_out, t_read, t_write = tokens
        self.db.execute(
            "INSERT INTO session (id, project_id, parent_id, slug, directory, title, version, cost, tokens_input, "
            "tokens_output, tokens_cache_read, tokens_cache_write, time_created, time_updated, time_archived) "
            "VALUES (?, 'global', ?, 'slug', ?, ?, '1.19.4', ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, parent, cwd, title, cost, t_in, t_out, t_read, t_write, ms(created), ms(updated), archived),
        )
        return sid

    def message(self, sid: str, role: str, at: float = -40, completed: float | None = None, **more) -> None:
        self.n += 1
        data = {"role": role, "time": {"created": ms(at)}, **more}
        if completed is not None:
            data["time"]["completed"] = ms(completed)
        self.db.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
            (f"msg_{self.n:04d}", sid, ms(at), ms(at), json.dumps(data)),
        )

    def done(self) -> Store:
        self.db.commit()
        self.db.close()
        return self

    def env(self, **kw) -> api.Env:
        return self.box.env(agents=[], **kw)


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def rows(snap):
    return {r.name: r for r in snap.sources}


def mine(snap):
    """The sessions this source gave (an opaque one has lost its runner, so it's found by source)."""
    return [s for s in snap.sessions if "opencode" in s.source.split("+")]


def make(box, **kw) -> Store:
    """A store with an open session that is mid-reply, an idle one with tokens and cost, and a finished one."""
    st = Store(box, **kw)
    st.session(OC1, "/work/app", "build the thing", updated=-20)
    st.message(OC1, "user", at=-60)
    st.message(OC1, "assistant", at=-50)  # no completion time: still being written
    st.session(OC2, "/work/other", "second", updated=-900, created=-7200, cost=0.42, tokens=(100, 50, 7, 3))
    st.message(OC2, "user", at=-950)
    st.message(OC2, "assistant", at=-940, completed=-900, cost=0.42)
    return st.done()


# --- the source and its contract -------------------------------------------------------------------------------------


def test_the_contract_is_sessions_only_and_read_after_the_live_list():
    from ws.home.sources import claude_code, ws_sessions

    assert (opencode.NAME, opencode.PROVIDES, opencode.RUNNER) == ("opencode", ("sessions",), "opencode")
    assert claude_code.ORDER < opencode.ORDER < ws_sessions.ORDER


def test_sessions_come_with_state_times_tokens_and_cost(box):
    make(box)
    snap = api.snapshot(box.env(agents=[]))
    assert rows(snap)["opencode"].ok and rows(snap)["opencode"].n == 2
    by = {s.id: s for s in mine(snap)}
    one, two = by[OC1], by[OC2]
    assert (one.name, one.cwd, one.repo, one.state) == ("build the thing", "/work/app", "app", "working")
    assert (one.started_at, one.last_seen) == (iso(-3600), iso(-20))
    assert (one.tokens, one.cost_usd, one.cost_basis) == (None, None, "not measured")  # nothing recorded
    assert (two.state, two.cost_usd, two.cost_basis) == ("idle", 0.42, "reported")
    assert two.tokens == {"input": 100, "output": 50, "cache_creation": 3, "cache_read": 7}
    assert (one.host, two.opaque, one.opaque) == ("testhost", False, False)
    assert [s.id for s in snap.sessions] == [OC1, OC2]  # working first


def test_a_zero_cost_beside_tokens_is_not_measured_not_free(box):
    st = Store(box)
    st.session(OC1, "/work/app", cost=0.0, tokens=(10, 5, 0, 0))
    st.done()
    (s,) = mine(api.snapshot(box.env(agents=[])))
    assert (s.cost_usd, s.cost_basis, s.tokens) == (None, "not measured", {
        "input": 10, "output": 5, "cache_creation": 0, "cache_read": 0})  # fmt: skip


def test_only_recent_top_level_sessions_that_are_not_archived_are_listed(box):
    st = Store(box)
    st.session(OC1, "/work/app", updated=-60)
    st.session("ses_old", "/work/app", updated=-(opencode.WINDOW_S + 60))
    st.session("ses_child", "/work/app", parent=OC1)
    st.session("ses_archived", "/work/app", archived=ms(-10))
    st.done()
    assert [s.id for s in mine(api.snapshot(box.env(agents=[])))] == [OC1]


def test_state_working_needs_an_unfinished_reply_and_a_recent_change(box):
    st = Store(box)
    st.session(OC1, "/work/a", updated=-20)  # a prompt sent, no reply yet
    st.message(OC1, "user", at=-25)
    st.session(OC2, "/work/b", updated=-(opencode.WORKING_S + 120))  # unfinished, but not touched for ages
    st.message(OC2, "assistant", at=-2000)
    st.session(OC3, "/work/c", updated=-20)  # finished
    st.message(OC3, "assistant", at=-40, completed=-20)
    st.session("ses_none", "/work/d", updated=-20)  # no messages yet
    st.done()
    states = {s.id: s.state for s in mine(api.snapshot(box.env(agents=[])))}
    assert states == {OC1: "working", OC2: "idle", OC3: "idle", "ses_none": "idle"}


def test_a_tool_call_waiting_for_a_yes_is_never_shown_as_an_ask(box):
    """OpenCode keeps a pending permission request in the running server, not in the store, so the store can only show
    a reply still in progress. That is `working`; the source makes no ask and no `waiting` state."""
    make(box)
    snap = api.snapshot(box.env(agents=[]))
    assert snap.asks == () and "waiting" not in {s.state for s in mine(snap)}


def test_a_message_that_is_not_json_leaves_that_state_unknown_and_the_source_ok(box):
    st = Store(box)
    st.session(OC1, "/work/app")
    st.db.execute("INSERT INTO message VALUES ('m1', ?, ?, ?, 'not json')", (OC1, ms(-40), ms(-40)))
    st.done()
    snap = api.snapshot(box.env(agents=[]))
    assert rows(snap)["opencode"].ok and [s.state for s in mine(snap)] == ["unknown"]


def test_a_store_in_wal_mode_is_read_without_its_side_files(box):
    make(box, wal=True)
    assert [f.name for f in make_files(box)] == ["opencode.db"]
    assert len(mine(api.snapshot(box.env(agents=[])))) == 2


def make_files(box):
    return sorted((box.home / "xdg-data" / "opencode").iterdir())


def test_a_newer_store_with_more_columns_and_tables_is_still_read(box):
    st = Store(box)
    st.db.execute("ALTER TABLE session ADD COLUMN something_new text")
    st.db.execute("CREATE TABLE brand_new (x text)")
    st.session(OC1, "/work/app")
    st.done()
    assert [s.id for s in mine(api.snapshot(box.env(agents=[])))] == [OC1]


# --- where the store is ----------------------------------------------------------------------------------------------


def test_the_default_folder_is_under_home_and_xdg_moves_it(box):
    box.environ.pop("XDG_DATA_HOME", None)
    env = box.env(agents=[])
    assert opencode.db_path(env) == box.home / ".local" / "share" / "opencode" / "opencode.db"
    assert env.path("opencode_dir") == box.home / ".local" / "share" / "opencode"
    box.environ["XDG_DATA_HOME"] = str(box.home / "elsewhere")
    assert opencode.db_path(box.env(agents=[])) == box.home / "elsewhere" / "opencode" / "opencode.db"
    box.environ["XDG_DATA_HOME"] = "relative/dir"  # the XDG rule: a relative value is ignored
    assert opencode.db_path(box.env(agents=[])).parent == box.home / ".local" / "share" / "opencode"


def test_opencode_db_names_a_file_or_a_name_in_the_data_folder(box):
    box.environ["XDG_DATA_HOME"] = str(box.home / "d")
    box.environ["OPENCODE_DB"] = str(box.home / "moved.db")
    assert opencode.db_path(box.env(agents=[])) == box.home / "moved.db"
    box.environ["OPENCODE_DB"] = "opencode-beta.db"
    assert opencode.db_path(box.env(agents=[])) == box.home / "d" / "opencode" / "opencode-beta.db"
    box.environ["OPENCODE_DB"] = ":memory:"
    assert opencode.db_path(box.env(agents=[])).name == "opencode.db"


def test_the_profile_can_name_the_folder(box):
    st = make(box)
    box.environ.pop("XDG_DATA_HOME")
    box.profile(f'[home.sources]\nopencode_dir = "{st.folder}"\n')
    assert len(mine(api.snapshot(box.env(agents=[])))) == 2


# --- the opaque rule -------------------------------------------------------------------------------------------------


def test_a_session_in_an_opaque_folder_keeps_only_the_kept_fields_and_its_title_is_never_read(box, monkeypatch):
    st = Store(box)
    st.session(OC1, str(box.home / "private" / "case"), title=SECRET + "-title", cost=9.0, tokens=(1, 2, 3, 4))
    st.session(OC2, "/work/open", title="visible", cost=1.5, tokens=(5, 6, 7, 8))
    st.done()
    box.profile(f'[home]\nopaque_paths = ["{box.home / "private"}"]\n')
    reads = spy_reads(monkeypatch)
    snap = api.snapshot(box.env(agents=[]))
    hidden = next(s for s in snap.sessions if s.opaque)
    open_one = next(s for s in snap.sessions if not s.opaque)
    assert hidden.opaque and (hidden.name, hidden.cwd, hidden.tokens, hidden.cost_usd) == ("", "", None, None)
    assert hidden.cost_basis == "hidden" and hidden.state in ("idle", "working", "unknown")
    assert (open_one.name, open_one.cost_usd) == ("visible", 1.5)
    assert SECRET not in json.dumps(snap.to_dict())
    # the title, cost and token columns were read for the open session's row only
    assert ("session", "title") in reads and ("session", "cost") in reads


def test_the_source_itself_never_gives_an_opaque_session_a_title_tokens_or_cost(box):
    st = Store(box)
    st.session(OC1, str(box.home / "private" / "case"), title="hidden title", cost=9.0, tokens=(1, 2, 3, 4))
    st.done()
    box.profile(f'[home]\nopaque_paths = ["{box.home / "private"}"]\n')
    env = box.env(agents=[])
    (s,) = opencode.read("sessions", env)
    assert s.opaque and (s.name, s.tokens, s.cost_usd) == ("", None, None) and s.cwd.endswith("private/case")


def test_a_session_with_no_folder_ws_can_place_is_opaque(box):
    st = Store(box)
    st.session(OC1, "")
    st.session(OC2, "relative/dir")
    st.done()
    snap = api.snapshot(box.env(agents=[]))
    assert [s.opaque for s in mine(snap)] == [True, True]
    assert {s.name for s in mine(snap)} == {""}


def test_a_link_into_an_opaque_folder_is_opaque_too(box):
    st = Store(box)
    (box.home / "private").mkdir()
    (box.home / "via").symlink_to(box.home / "private")
    st.session(OC1, str(box.home / "via"), title=SECRET)
    st.done()
    box.profile(f'[home]\nopaque_paths = ["{box.home / "private"}"]\n')
    snap = api.snapshot(box.env(agents=[]))
    assert [s.opaque for s in mine(snap)] == [True] and SECRET not in json.dumps(snap.to_dict())


# --- stores that can't be read: one typed row, no sessions -------------------------------------------------------------


def test_no_store_is_an_unavailable_row(box):
    snap = api.snapshot(box.env(agents=[]))
    row = rows(snap)["opencode"]
    assert (row.ok, row.kind) == (False, "unavailable") and "opencode.db" in row.why and mine(snap) == []


def test_a_file_that_is_not_sqlite_is_a_bad_format_row(box):
    folder = box.home / "xdg-data" / "opencode"
    folder.mkdir(parents=True)
    box.environ["XDG_DATA_HOME"] = str(box.home / "xdg-data")
    (folder / "opencode.db").write_bytes(os.urandom(4096))
    snap = api.snapshot(box.env(agents=[]))
    assert (rows(snap)["opencode"].ok, rows(snap)["opencode"].kind) == (False, "bad_format")
    assert mine(snap) == []


def test_an_empty_file_has_no_layout_so_it_is_a_bad_format_row(box):
    folder = box.home / "xdg-data" / "opencode"
    folder.mkdir(parents=True)
    box.environ["XDG_DATA_HOME"] = str(box.home / "xdg-data")
    (folder / "opencode.db").write_bytes(b"")
    assert rows(api.snapshot(box.env(agents=[])))["opencode"].kind == "bad_format"


@pytest.mark.parametrize("drop", ["directory", "time_updated", "tokens_cache_write", "parent_id"])
def test_a_store_missing_a_column_this_source_reads_is_a_bad_format_row_with_no_sessions(box, drop):
    st = Store(box)
    st.session(OC1, "/work/app")
    st.db.execute("DROP TABLE session")  # the same table, one column short
    st.db.execute(re.sub(rf"\b{drop} \w+( DEFAULT 0)?( NOT NULL)?,?\s*", "", SESSION_DDL, count=1))
    st.done()
    snap = api.snapshot(box.env(agents=[]))
    row = rows(snap)["opencode"]
    assert (row.ok, row.kind) == (False, "bad_format") and "session" in row.why and mine(snap) == []


def test_a_store_with_no_message_table_is_a_bad_format_row(box):
    st = Store(box)
    st.session(OC1, "/work/app")
    st.db.execute("DROP TABLE message")
    st.done()
    assert rows(api.snapshot(box.env(agents=[])))["opencode"].kind == "bad_format"


def test_a_locked_store_is_a_timeout_row(box, monkeypatch):
    st = make(box)
    monkeypatch.setattr(opencode, "BUSY_MS", 50)
    writer = sqlite3.connect(st.path, isolation_level=None)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        snap = api.snapshot(box.env(agents=[]))
    finally:
        writer.close()
    assert (rows(snap)["opencode"].ok, rows(snap)["opencode"].kind) == (False, "timeout") and mine(snap) == []


def test_a_pipe_or_folder_named_like_the_store_is_not_opened(box):
    folder = box.home / "xdg-data" / "opencode"
    folder.mkdir(parents=True)
    box.environ["XDG_DATA_HOME"] = str(box.home / "xdg-data")
    os.mkfifo(folder / "opencode.db")
    assert rows(api.snapshot(box.env(agents=[])))["opencode"].kind == "unavailable"
    os.unlink(folder / "opencode.db")
    (folder / "opencode.db").mkdir()
    assert rows(api.snapshot(box.env(agents=[])))["opencode"].kind == "unavailable"


def test_a_failure_inside_the_source_never_breaks_the_others(box, monkeypatch):
    make(box)
    box.ws_session(SID, cwd="/work/app")

    def boom(*a, **k):
        raise sqlite3.DatabaseError("planted")

    monkeypatch.setattr(opencode.sqlite3, "connect", boom)
    snap = api.snapshot(box.env(agents=[]))
    assert rows(snap)["opencode"].ok is False and rows(snap)["ws_sessions"].ok
    assert [s.id for s in snap.sessions] == [SID]


def test_a_read_that_fails_raises_a_typed_source_error(box):
    folder = box.home / "xdg-data" / "opencode"
    folder.mkdir(parents=True)
    box.environ["XDG_DATA_HOME"] = str(box.home / "xdg-data")
    (folder / "opencode.db").write_bytes(os.urandom(2048))
    with pytest.raises(SourceError) as e:
        opencode.read("sessions", box.env(agents=[]))
    assert e.value.kind == "bad_format"


# --- merging with ws's own record ------------------------------------------------------------------------------------


def test_a_session_ws_recorded_and_opencode_stored_is_one_session(box):
    st = Store(box)
    st.session(OC1, "/work/app", title="named by opencode", cost=0.3, tokens=(1, 2, 3, 4))
    st.done()
    box.ws_session(OC1, cwd="/work/app", runner="opencode")
    box.ws_session(SID, cwd="/work/app", runner="claude-code")
    snap = api.snapshot(box.env(agents=[]))
    got = [s for s in snap.sessions if s.id == OC1]
    assert len(got) == 1
    (s,) = got
    assert s.source == "opencode+ws_sessions" and s.name == "named by opencode" and s.cost_usd == 0.3
    assert s.runner == "opencode" and len(snap.sessions) == 2  # the Claude Code record under its own id stays apart


def test_the_same_id_under_another_runner_is_not_merged(box):
    st = Store(box)
    st.session(OC1, "/work/app")
    st.done()
    box.ws_session(OC1, cwd="/work/app", runner="claude-code")
    assert len(api.snapshot(box.env(agents=[])).sessions) == 2


def test_an_opaque_folder_in_either_part_hides_the_merged_session(box):
    st = Store(box)
    st.session(OC1, "/work/open", title=SECRET)
    st.done()
    box.ws_session(OC1, cwd=str(box.home / "private"), runner="opencode")
    box.profile(f'[home]\nopaque_paths = ["{box.home / "private"}"]\n')
    snap = api.snapshot(box.env(agents=[]))
    assert [s.opaque for s in snap.sessions] == [True] and SECRET not in json.dumps(snap.to_dict())


# --- what the source never opens or reads ----------------------------------------------------------------------------


def spy_reads(monkeypatch) -> set[tuple[str, str]]:
    """Every (table, column) SQLite is asked to read through a connection the source opens."""
    seen: set[tuple[str, str]] = set()
    real = sqlite3.connect

    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_READ:
            seen.add((arg1, arg2))
        return sqlite3.SQLITE_OK

    def connect(*a, **k):
        conn = real(*a, **k)
        conn.set_authorizer(authorizer)
        return conn

    monkeypatch.setattr(opencode.sqlite3, "connect", connect)
    return seen


def test_no_key_file_is_opened_and_the_store_is_opened_read_only(box, monkeypatch):
    st = make(box)
    for name in ("opencode.db.key", "token.key", "auth.json.key"):
        (st.folder / name).write_text("not-a-real-secret", encoding="utf-8")
    touched: list[str] = []
    real_open, real_os_open, real_connect = open, os.open, sqlite3.connect

    def spy_open(file, *a, **k):
        touched.append(str(file))
        return real_open(file, *a, **k)

    def spy_os_open(path, *a, **k):
        touched.append(str(path))
        return real_os_open(path, *a, **k)

    def spy_connect(target, *a, **k):
        touched.append(str(target))
        return real_connect(target, *a, **k)

    monkeypatch.setattr("builtins.open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(opencode.sqlite3, "connect", spy_connect)
    snap = api.snapshot(box.env(agents=[]))
    assert len(mine(snap)) == 2
    uris = [t for t in touched if "opencode.db" in t]
    assert uris and all(t.endswith("?mode=ro") for t in uris if t.startswith("file:"))
    assert [t for t in touched if ".key" in t] == []
    assert "not-a-real-secret" not in json.dumps(snap.to_dict())


def test_a_store_named_as_a_key_file_is_refused_unopened(box, monkeypatch):
    st = make(box)
    key = st.folder / "store.key"
    key.write_bytes(st.path.read_bytes())
    box.environ["OPENCODE_DB"] = str(key)
    opened: list[str] = []
    real = sqlite3.connect
    monkeypatch.setattr(opencode.sqlite3, "connect", lambda t, *a, **k: (opened.append(str(t)), real(t, *a, **k))[1])
    row = rows(api.snapshot(box.env(agents=[])))["opencode"]
    assert (row.ok, row.kind) == (False, "unavailable") and opened == []


def test_the_store_cannot_be_written_through_the_source(box):
    st = make(box)
    before = st.path.read_bytes()
    env = box.env(agents=[])
    opencode.read("sessions", env)
    assert st.path.read_bytes() == before
    conn = opencode._connect(st.path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM session")
    finally:
        conn.close()


def test_message_bodies_and_the_part_table_are_never_read(box, monkeypatch):
    st = Store(box)
    st.db.execute(PART_DDL)
    st.session(OC1, "/work/app", title="fine")
    st.message(OC1, "user", at=-60, text=SECRET, summary={"title": SECRET, "body": SECRET})
    st.message(OC1, "assistant", at=-50, completed=-40, error={"message": SECRET}, tokens={"input": 999})
    st.db.execute(
        "INSERT INTO part VALUES ('prt1', 'msg_0001', ?, ?, ?, ?)",
        (OC1, ms(-60), ms(-60), json.dumps({"type": "text", "text": SECRET})),
    )
    st.done()
    reads = spy_reads(monkeypatch)
    snap = api.snapshot(box.env(agents=[]))
    assert len(mine(snap)) == 1 and SECRET not in json.dumps(snap.to_dict())
    assert {t for t, _ in reads} == {"session", "message"}  # `part` is never touched
    # of a message, only the columns that say which session, when, and (inside `data`) its role and completion time
    assert {c for t, c in reads if t == "message"} <= {"id", "session_id", "time_created", "data"}


def test_a_store_without_a_part_table_reads_fine(box):
    make(box)  # the fixture has no `part` table at all
    assert len(mine(api.snapshot(box.env(agents=[])))) == 2


# --- the profile key ---------------------------------------------------------------------------------------------------


def test_opencode_dir_is_an_accepted_profile_key_and_an_unknown_one_is_still_refused(box):
    box.profile('[home.sources]\nopencode_dir = "~/oc"\n')
    cfg = api.load_config(box.environ, box.home)
    assert cfg.sources == {"opencode_dir": str(box.home / "oc")}
    assert "opencode_dir" in api.SOURCE_KEYS
    box.profile('[home.sources]\nopencode_folder = "~/oc"\n')
    with pytest.raises(api.WsError):
        api.load_config(box.environ, box.home)
