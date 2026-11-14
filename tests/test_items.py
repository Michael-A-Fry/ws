"""The item layer: kinds table, get/list, the rebuilt link index, the checked change path, views, the register fields
and the extractor eval. The change, front-matter and index trials run here and print their numbers (-s)."""

import json
import re
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ws import items, rating, runrecord

ROOT = Path(__file__).resolve().parents[1]
STORES = ROOT / "docs" / "reference" / "stores.md"
GUIDE = ROOT / "docs" / "reference" / "items.md"


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    reg = tmp_path / "TODO.md"
    reg.write_text("")
    return {"HOME": str(home), "WS_HOME": str(home), "WS_REGISTER": str(reg)}


def put_area(env, ident="a1", name="House", rank=1, people=()):
    d = items._areas_dir(env)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ident}.json").write_text(json.dumps({"name": name, "rank": rank, "people": list(people)}))


def put_register(env, text):
    Path(env["WS_REGISTER"]).write_text(text)


def put_run(env, run_id, session=None):
    now = datetime.now(UTC)
    rec = runrecord.new(
        run_id=run_id, job="j", runner="r", started_at=runrecord.iso(now), ended_at=runrecord.iso(now),
        duration_ms=1, status="ok", session_id=session,
    )  # fmt: skip
    runrecord.append(runrecord.runs_path(env), rec)


# --- the table ------------------------------------------------------------------------------------------------------------


def page_stores() -> set[str]:
    """The store names stores.md gives: the first cell of each table row under the log and record tables, plus the
    heading of the queue database."""
    text = STORES.read_text()
    names = set()
    for section in ("## JSON-lines logs", "## Record files"):
        block = text.split(section, 1)[1].split("\n## ", 1)[0]
        for line in block.splitlines():
            m = re.match(r"^\| ([^|]+) \|", line)
            if m and m.group(1).strip() not in ("Log", "Record") and not set(m.group(1)) <= set("-"):
                names.add(m.group(1).strip())
    names.add("The queue database")
    return names


def test_the_table_is_sound():
    assert items.check_table() == []


def test_every_store_on_the_stores_page_has_a_kinds_row():
    covered = {s for k in items.KINDS.values() for s in k.stores}
    page = page_stores()
    assert len(page) == 22  # counted first: 15 logs, 6 record files, the queue database
    assert page <= covered, sorted(page - covered)
    assert "The messages database" in covered  # not on the stores page yet


def test_every_reader_reads_an_empty_home(env):
    for name, k in items.KINDS.items():
        assert list_of(k.reader(env)) == [], name


def list_of(it):
    return [*it]


def test_a_planted_kind_with_no_reader_exits_1(monkeypatch, env):
    monkeypatch.setitem(items.KINDS, "ghost", replace(items.KINDS["run"], name="ghost", reader=None))
    assert items.main(["check"]) == 1
    assert [p.code for p in items.check(env)] == ["no-reader"]


def test_a_write_rule_outside_the_three_exits_1(monkeypatch):
    monkeypatch.setitem(items.KINDS, "run", replace(items.KINDS["run"], write="whenever"))
    assert items.main(["check"]) == 1
    monkeypatch.setitem(items.KINDS, "run", replace(items.KINDS["run"], write=""))
    assert [p.code for p in items.check_table()] == ["bad-write-rule"]


def test_every_row_declares_one_of_three_rules():
    assert {k.write for k in items.KINDS.values()} <= set(items.WRITE_RULES)
    assert {k.write for k in items.KINDS.values()} == set(items.WRITE_RULES)


def test_a_planted_kind_missing_from_a_registry_fails_the_gap_test():
    guide = GUIDE.read_text()
    assert items.registry_gaps(items.KINDS, guide) == {}
    planted = {**items.KINDS, "ghost": replace(items.KINDS["run"], name="ghost")}
    gaps = items.registry_gaps(planted, guide)
    assert gaps == {"LINK_SPEC": ["ghost"], "guide table": ["ghost"]}


def test_refs():
    assert items.parse_ref("run:abc") == ("run", "abc")
    for bad in ("run", "run:", ":x", "Run:x", "run: x", 5):
        with pytest.raises(items.ItemError) as e:
            items.parse_ref(bad)
        assert e.value.kind == "bad-ref"
    with pytest.raises(items.ItemError) as e:
        items.get("nope:1")
    assert e.value.kind == "unknown-kind"


# --- get and list ---------------------------------------------------------------------------------------------------------


def test_get_and_list_go_through_the_readers(env):
    put_run(env, "r1", "s1")
    put_run(env, "r2", "s2")
    put_run(env, "r3", "s1")
    assert items.get("run:r2", env)["session_id"] == "s2"
    assert items.get("run:zz", env) is None
    got = items.list("run", {"session_id": "s1"}, "-run_id", env=env)
    assert [r["id"] for r in got] == ["r3", "r1"]
    groups = items.list("run", group="session_id", env=env)
    assert {k: len(v) for k, v in groups.items()} == {"s1": 2, "s2": 1}
    assert len(items.list("run", env=env, limit=2)) == 2
    assert len(items.list("run", env=env, limit=10**6)) == 3  # capped at 300, not at the store


def test_a_log_reader_drops_what_it_is_not_asked_to_keep(tmp_path):
    from ws import jsonl

    p = tmp_path / ("calls." + "jsonl")
    jsonl.append(p, jsonl.stamped({"runner": "r", "payload": {"command": "SECRET"}}, 1))
    rows = list_of(items._log(lambda e: tmp_path / "calls", ("schema", "ts", "runner"))({}))
    assert len(rows) == 1 and "payload" not in rows[0] and re.fullmatch(r"h[0-9a-f]{12}", rows[0]["id"])


# --- views ----------------------------------------------------------------------------------------------------------------


def test_a_planted_view_renders_with_no_python_change(env):
    put_run(env, "r1", "s1")
    put_run(env, "r2", "s2")
    d = items._views_dir(env)
    d.mkdir(parents=True)
    (d / "mine.toml").write_text('kind = "run"\nversion = 1\nsort = "-run_id"\n[where]\nsession_id = "s2"\n')
    assert [r["id"] for r in items.render_view("mine", env)] == ["r2"]
    assert items.check(env) == []
    (d / "bad.toml").write_text('kind = "run"\nversion = 2\n')
    with pytest.raises(items.ItemError) as e:
        items.render_view("bad", env)
    assert e.value.kind == "bad-view"
    assert [p.code for p in items.check(env)] == ["bad-view"]


# --- the register and areas -----------------------------------------------------------------------------------------------


ROWS = """# Register
- [ ] C1 First thing {area=a1 due=2026-11-20 every=7d who=sam chase_on=2026-11-18}
- [x] C2 Second {needs=C1 area=a1}
- [ ] a plain task with no id
"""


def test_the_register_parser_reads_the_fields(env):
    put_register(env, ROWS)
    rows = {r["id"]: r for r in items.list("row", env=env)}
    assert set(rows) == {"C1", "C2"}
    assert rows["C1"]["due"] == "2026-11-20" and rows["C1"]["every"] == "7d" and rows["C1"]["who"] == "sam"
    assert rows["C1"]["chase_on"] == "2026-11-18" and rows["C2"]["done"] is True and rows["C2"]["needs"] == "C1"


def test_a_planted_item_whose_area_is_not_in_the_table_exits_1(env, monkeypatch):
    put_register(env, ROWS)
    put_area(env, "a1")
    assert items.check(env) == []
    put_register(env, ROWS + "- [ ] C3 Orphan {area=a9}\n")
    problems = items.check(env)
    assert [(p.code, p.where) for p in problems] == [("dangling", "row:C3")]
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert items.main(["check"]) == 1


def test_a_planted_dangling_needs_exits_1(env):
    put_area(env, "a1")
    put_register(env, "- [ ] C1 x {needs=C77 area=a1}\n")
    assert [(p.code, p.where) for p in items.check(env)] == [("dangling", "row:C1")]


def test_a_rename_changes_no_item_id(env):
    put_area(env, "a1", "House", 2)
    put_register(env, ROWS)
    before = (items.all_links(env), sorted(r["id"] for r in items.list("row", env=env)))
    _, rev = items.read_state("area:a1", env)
    res = items.change("area:a1", {"name": "Home and garden"}, rev, "person:sam", env)
    assert res.outcome == "ok" and res.state["name"] == "Home and garden" and res.state["changed_by"] == "person:sam"
    after = (items.all_links(env), sorted(r["id"] for r in items.list("row", env=env)))
    assert before == after
    assert items.check(env) == []


def test_an_id_that_holds_an_area_name_is_a_fault(env):
    put_area(env, "a1", "ho")
    put_register(env, "- [ ] ho-1 x\n")
    assert "id-has-area-name" in [p.code for p in items.check(env)]


def test_a_rank_change_by_an_agent_is_refused(env):
    put_area(env, "a1", rank=3, people=["sam"])
    _, rev = items.read_state("area:a1", env)
    for delta in ({"rank": 1}, {"people": []}):
        res = items.change("area:a1", delta, rev, "agent:worker-7", env)
        assert res.outcome == "refused" and res.problems
    assert items.read_state("area:a1", env)[0]["rank"] == 3
    assert items.change("area:a1", {"rank": 1}, rev, "person:sam", env).outcome == "ok"
    assert (
        items.change("area:a1", {"name": "Garden"}, items.read_state("area:a1", env)[1], "agent:w", env).outcome == "ok"
    )


def test_change_validates_and_is_gated_only(env):
    put_area(env, "a1")
    _, rev = items.read_state("area:a1", env)
    res = items.change("area:a1", {"rank": -4}, rev, "person:sam", env)
    assert res.outcome == "invalid" and items.read_state("area:a1", env)[1] == rev
    assert items.change("area:a2", {"rank": 1}, rev, "person:sam", env).outcome == "missing"
    with pytest.raises(items.ItemError) as e:
        items.change("run:r1", {"x": 1}, rev, "person:sam", env)
    assert e.value.kind == "not-gated"
    with pytest.raises(items.ItemError):
        items.change("area:a1", {"rank": 1}, rev, "", env)


# --- the link index -------------------------------------------------------------------------------------------------------


def plant_world(env, n=8):
    put_area(env, "a1")
    lines = []
    for i in range(n):
        put_run(env, f"r{i}", f"s{i % 3}")
        rating.append(rating.ratings_path(env), rating.new(f"r{i}", "up"))
        lines.append(f"- [ ] C{i} item {{area=a1 needs=C{i - 1}}}" if i else "- [ ] C0 item {area=a1}")
    put_register(env, "\n".join(lines) + "\n")


def test_deleting_the_index_and_rebuilding_gives_the_same_table(env):
    plant_world(env)
    db = items.index_path(env)
    n = items.rebuild_index(env)
    first = items.index_rows(db)
    assert n == len(first) > 0
    db.unlink()
    items.rebuild_index(env)
    assert items.index_rows(db) == first
    assert items.index_rows(db) == items.all_links(env)  # the live table, straight from the stores
    assert {r[1] for r in first} <= set(items.RELS)


def test_a_rel_outside_the_seven_is_a_typed_error(env, monkeypatch):
    put_run(env, "r1")
    monkeypatch.setitem(items.LINKS, "run", lambda r: [("likes", "run:x")])
    with pytest.raises(items.ItemError) as e:
        items.all_links(env)
    assert e.value.kind == "bad-field"


# --- change under two writers (trial) ---------------------------------------------------------------------------------------


def interleave(env, how):
    """Two writers each add one field to area a1, having both read it first. Returns the number of changes lost."""
    put_area(env, "a1", rank=5)
    doc, rev = items.read_state("area:a1", env)
    if how == "bare":
        items.bare_write("area:a1", {"x": 1}, env, read=dict(doc))
        items.bare_write("area:a1", {"y": 1}, env, read=dict(doc))
    else:
        a = items.change("area:a1", {"x": 1}, rev, "agent:a", env)
        b = items.change("area:a1", {"y": 1}, rev, "agent:b", env)
        assert (a.outcome, b.outcome) == ("ok", "conflict") and b.state["x"] == 1  # the loser gets the fresh state
        again = items.change("area:a1", {"y": 1}, b.rev, "agent:b", env)
        assert again.outcome == "ok"
    final = items.read_state("area:a1", env)[0]
    return sum(1 for f in ("x", "y") if f not in final)


def test_ta3_100_two_writer_interleavings(tmp_path, capsys):
    lost_checked = lost_bare = 0
    for i in range(100):
        e = {"HOME": str(tmp_path / f"c{i}"), "WS_HOME": str(tmp_path / f"c{i}")}
        lost_checked += interleave(e, "checked")
        e = {"HOME": str(tmp_path / f"b{i}"), "WS_HOME": str(tmp_path / f"b{i}")}
        lost_bare += interleave(e, "bare")
    assert lost_checked == 0 and lost_bare >= 1
    with capsys.disabled():
        print(f"\nchange interleavings: checked lost {lost_checked} of 100, bare lost {lost_bare} of 100")


def test_ta3_real_threads_lose_nothing(tmp_path, capsys):
    lost = 0
    for i in range(100):
        e = {"HOME": str(tmp_path / f"t{i}"), "WS_HOME": str(tmp_path / f"t{i}")}
        put_area(e, "a1")
        _, rev = items.read_state("area:a1", e)
        barrier, results = threading.Barrier(2), {}

        def writer(field, e=e, rev=rev, barrier=barrier, results=results):
            barrier.wait()
            r = items.change("area:a1", {field: 1}, rev, f"agent:{field}", e)
            while r.outcome == "conflict":
                r = items.change("area:a1", {field: 1}, r.rev, f"agent:{field}", e)
            results[field] = r.outcome

        ts = [threading.Thread(target=writer, args=(f,)) for f in ("x", "y")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        final = items.read_state("area:a1", e)[0]
        lost += sum(1 for f in ("x", "y") if f not in final)
    assert lost == 0
    with capsys.disabled():
        print(f"\nchange threads: lost {lost} of 100")


def test_ta3_single_writer_cost_and_false_conflicts(env, capsys):
    put_area(env, "a1", rank=1)
    checked, bare, conflicts = [], [], 0
    for i in range(50):
        _doc, rev = items.read_state("area:a1", env)
        t = time.perf_counter()
        res = items.change("area:a1", {"note": str(i)}, rev, "person:sam", env)
        checked.append((time.perf_counter() - t) * 1000)
        conflicts += res.outcome == "conflict"
        assert res.outcome == "ok"
        t = time.perf_counter()
        items.bare_write("area:a1", {"note": str(i)}, env)
        bare.append((time.perf_counter() - t) * 1000)
    p95 = lambda xs: sorted(xs)[int(len(xs) * 0.95) - 1]
    assert conflicts == 0
    with capsys.disabled():
        print(
            f"\nchange single writer: false conflicts {conflicts} of 50; p95 checked {p95(checked):.2f} ms, "
            f"bare {p95(bare):.2f} ms, extra {p95(checked) - p95(bare):.2f} ms"
        )


# --- front-matter and index numbers ---------------------------------------------------------------------------------------------------


def test_ta1_parser_counts(capsys):
    src = ROOT / "src" / "ws"
    text = {p: p.read_text(errors="replace") for p in src.rglob("*.py") if p.name != "items.py"}
    fm = sum(len(re.findall(r"frontmatter\.(?:parse|loads?)\(", t)) for t in text.values())
    reg = sum(len(re.findall(r"\\\[\[ xX\]\\\]|\[ xX\]", t)) for t in text.values())
    with capsys.disabled():
        print(f"\nfront-matter parse calls outside items.py: {fm}; register matchers outside items.py: {reg}")
    assert reg < 20 and fm < 40  # a ceiling to catch a runaway, set well above the count the trial printed


def test_ta2_twenty_cross_kind_questions(env, capsys):
    plant_world(env, 10)
    db = items.index_path(env)
    t = time.perf_counter()
    items.rebuild_index(env)
    rebuild_s = time.perf_counter() - t
    qs = []  # (question, the index's answer, the true answer)
    for i in range(10):
        qs.append((f"which session ran r{i}", set(items.linked(f"run:r{i}", db, "parent")), {f"session:s{i % 3}"}))
        qs.append(
            (
                f"what rates r{i}",
                {x for x in items.linked(f"run:r{i}", db, "about", inbound=True) if x.startswith("rating:")},
                {f"rating:r{i}@" + "*"},
            )
        )
    right = 0
    for _q, got, want in qs:
        right += (got == want) or (
            len(got) == len(want) == 1
            and next(iter(want)).endswith("*")
            and next(iter(got)).startswith(next(iter(want))[:-1])
        )
    # the grep arm: every store file's lines that hold the id; right only if exactly one distinct file-kind answers
    blob = {p: p.read_text(errors="ignore") for p in Path(env["WS_HOME"]).rglob("*.jsonl")}
    grep_right = 0
    for i in range(10):
        hits = {p.name for p, t_ in blob.items() if f'"{f"r{i}"}"' in t_}
        grep_right += len(hits) == 1  # a bare grep of the id cannot say which of the two files is the answer
        hits2 = {p.name for p, t_ in blob.items() if f"r{i}" in t_}
        grep_right += len(hits2) == 1
    assert right == 20 and rebuild_s < 5
    with capsys.disabled():
        print(
            f"\nindex answered {right} of {len(qs)}; grep arm {grep_right} of 20; rebuild {rebuild_s:.3f} s "
            "(recall scoring: not run, the item layer feeds no recall path yet)"
        )


# --- the extractor eval ---------------------------------------------------------------------------------------------------

SEEDED = [
    ("- [ ] Add the retry guard\n- [x] Wrote the doc\n", ["Add the retry guard"]),
    (
        "Notes.\nTODO: rotate the broker log\nfollow-up: tell the team about the wall",
        ["rotate the broker log", "tell the team about the wall"],
    ),
    ("We must pin the action SHAs before the tag.", ["pin the action SHAs before the tag"]),
    (
        "* Next step: write the migration\n* need to check the index rebuild time",
        ["write the migration", "check the index rebuild time"],
    ),
    ("Nothing is promised in this paragraph.", []),
    ("- [ ] Ship it\n- [ ] Tell Sam", ["Ship it", "Tell Sam"]),
]


def test_the_extractor_finds_the_seeded_set(capsys):
    r = items.recall(SEEDED)
    with capsys.disabled():
        print(
            f"\nextractor recall {r['found']}/{r['planted']} = {r['recall']:.2f}; missed {r['missed']}; extra {r['extra']}"
        )
    assert r["planted"] == 8 and r["recall"] >= 0.9
    rows = items.ledger_rows("TODO: a thing", "notes.md")
    assert rows[0]["text"] == "a thing" and rows == items.ledger_rows("TODO: a thing", "notes.md")


def test_change_refuses_a_path_traversal_id(env, tmp_path):
    put_area(env)
    outside = tmp_path / "home" / "x.json"
    outside.write_text('{"name": "X", "rank": 1}')
    for ident in ("../../x", "..", "a/b", "a..b", "x y"):
        with pytest.raises(items.ItemError) as e:
            items.change(f"area:{ident}", {"rank": 2}, "r", "person:m", env)
        assert e.value.kind == "bad-ref"
    assert json.loads(outside.read_text()) == {"name": "X", "rank": 1}


def test_item_path_stays_in_areas_dir(env, tmp_path):
    d = items._areas_dir(env)
    d.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (d / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(items.ItemError):
        items._item_path(items.KINDS["area"], "link/../x", env)
    (d / "sneak.json").symlink_to(tmp_path / "elsewhere" / "t.json")
    with pytest.raises(items.ItemError) as e:
        items._item_path(items.KINDS["area"], "sneak", env)
    assert e.value.kind == "bad-ref"
    assert items._item_path(items.KINDS["area"], "ok-1.v2", env).parent == d


def test_change_on_corrupt_or_vanished_file_is_a_result(env):
    put_area(env)
    p = items._areas_dir(env) / "a1.json"
    rev = items._rev(p.read_bytes())

    def corrupt():
        p.write_text("{not json")

    r = items.change("area:a1", {"rank": 2}, rev, "person:m", env, _between=corrupt)
    assert r.outcome == "invalid"

    def vanish():
        p.unlink()

    put_area(env, "a2")
    p2 = items._areas_dir(env) / "a2.json"
    rev2 = items._rev(p2.read_bytes())
    r = items.change("area:a2", {"rank": 2}, rev2, "person:m", env, _between=p2.unlink)
    assert r.outcome == "missing"


def test_messages_honours_env_root(env, tmp_path):
    from ws import msg, paths

    def plant(root, mid):
        e = {"HOME": str(root), "WS_HOME": str(root)}
        db = paths.state_dir(e) / "msg" / "messages.db"
        conn = msg._open(create=True, path=db)
        conn.close()
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            'INSERT INTO envelope(message_id, "from", "to", kind, sent, size) VALUES (?, "a", "b", "note", "t", "1")',
            (mid,),
        )
        conn.commit()
        conn.close()
        return e

    e1 = plant(tmp_path / "r1", 11)
    e2 = plant(tmp_path / "r2", 22)
    assert [m["id"] for m in items._messages(e1)] == [11]
    assert [m["id"] for m in items._messages(e2)] == [22]


def test_change_busy_after_three_lock_timeouts(env, monkeypatch):
    put_area(env)
    p = items._areas_dir(env) / "a1.json"
    rev = items._rev(p.read_bytes())

    class Held:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise TimeoutError

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(items, "_Lock", Held)
    r = items.change("area:a1", {"rank": 2}, rev, "person:m", env)
    assert r.outcome == "busy"
    assert json.loads(p.read_text())["rank"] == 1


def test_a_bare_person_actor_is_not_a_person(env):
    put_area(env)
    k = replace(items.KINDS["area"], human_only=("rank",))
    p = items._areas_dir(env) / "a1.json"
    for actor in ("person:", "person:  "):
        r = items.change("area:a1", {"rank": 2}, items._rev(p.read_bytes()), actor, env, {"area": k})
        assert r.outcome == "refused"


def test_write_atomic_keeps_the_file_mode(env):
    put_area(env)
    p = items._areas_dir(env) / "a1.json"
    p.chmod(0o640)
    items._write_atomic(p, b"{}")
    assert p.stat().st_mode & 0o777 == 0o640


def test_rebuild_failure_leaves_no_temp_file(env, tmp_path, monkeypatch):
    db = tmp_path / "idx" / "links.db"
    monkeypatch.setattr(items, "all_links", lambda *a, **k: [("a:1", "r")])  # wrong arity: the insert fails
    with pytest.raises(sqlite3.Error):
        items.rebuild_index(env, db=db)
    assert list(db.parent.glob("*.tmp")) == []


def test_main_turns_a_store_error_into_exit_1(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(items, "check", boom)
    assert items.main(["check"]) == 1
    assert "disk gone" in capsys.readouterr().err
