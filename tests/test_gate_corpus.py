"""The gate replay corpus (ws.gatecorpus, `ws gate replay --metrics|--save|--diff`) and the corpus shipped in
tests/gate_corpus: the maths on a hand-made matrix, the loader, the ratchet on `known` rows, the decision diff, the
scratch folders, both runners' payload shapes, and the shipped files as a whole."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from ws import cli, gate, gatecorpus, policy
from ws.output import WsError

CORPUS = sorted((Path(__file__).parent / "gate_corpus").glob("*.jsonl"))
RUNNERS = ("claude-code", "opencode")

# The ordinary calls the gate still refuses, by source. A fix shows up as a FIXED line until the row is unmarked, and
# the number here goes down with it; a new known row has to raise it on purpose.
KNOWN_BASELINE = {"oracle": 2, "parity": 3, "regression": 4, "control": 0}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def row(id_, value, expect, kind="command", source="control", **extra):
    return {
        "id": id_,
        "payload": {"kind": kind, "value": value},
        "expect": expect,
        "source": source,
        "why": "a planted row",
        **extra,
    }


def write(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(["gate", "replay", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# --- the maths --------------------------------------------------------------------------------------------------


def judged(expect: str, got: str, source: str = "oracle", known: bool = False, ms: float = 1.0) -> dict:
    return {
        "id": f"{expect}-{got}-{source}",
        "source": source,
        "expect": expect,
        "result": got,
        "known": known,
        "differs": expect != got,
        "ms": ms,
    }


def matrix_rows() -> list[dict]:
    """10 expected allow (7 allow, 2 deny, 1 ask), 8 expected deny (5 deny, 2 ask, 1 allow), 2 expected ask (1 ask,
    1 allow). The oracle holds 14 of them and parity the other 6."""
    spec = [
        ("allow", "allow", 7, "oracle"),
        ("allow", "deny", 2, "oracle"),
        ("allow", "ask", 1, "parity"),
        ("deny", "deny", 5, "oracle"),
        ("deny", "ask", 2, "parity"),
        ("deny", "allow", 1, "parity"),
        ("ask", "ask", 1, "parity"),
        ("ask", "allow", 1, "parity"),
    ]
    return [judged(e, g, s, ms=float(i)) for i, (e, g, n, s) in enumerate(spec, 1) for _ in range(n)]


def test_wilson_interval_matches_known_values():
    lo, hi = gatecorpus.wilson(3, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.1078, 0.6032)
    assert gatecorpus.wilson(0, 20)[0] == 0.0 and gatecorpus.wilson(20, 20)[1] == 1.0
    assert gatecorpus.wilson(0, 0) is None
    assert round(gatecorpus.wilson(50, 100)[0], 4) == 0.4038


def test_metrics_on_a_hand_made_matrix():
    m = gatecorpus.metrics(matrix_rows())
    assert m["confusion"] == {
        "allow": {"allow": 7, "deny": 2, "ask": 1},
        "deny": {"allow": 1, "deny": 5, "ask": 2},
        "ask": {"allow": 1, "deny": 0, "ask": 1},
    }
    # a false block is an expected allow that was denied or held: 3 of 10
    assert (m["false_block"]["k"], m["false_block"]["n"], m["false_block"]["rate"]) == (3, 10, 0.3)
    assert [round(x, 4) for x in m["false_block"]["ci"]] == [0.1078, 0.6032]
    # a miss is an expected deny or ask that was allowed: 2 of 10
    assert (m["miss"]["k"], m["miss"]["n"]) == (2, 10)
    # softened is an expected deny that came back as an ask: 2 of 8
    assert (m["softened"]["k"], m["softened"]["n"], m["softened"]["rate"]) == (2, 8, 0.25)
    assert m["by_source"]["parity"]["rows"] == 6 and m["by_source"]["oracle"]["rows"] == 14
    assert m["by_source"]["oracle"]["false_block"]["k"] == 2 and m["by_source"]["oracle"]["miss"]["n"] == 5
    assert m["by_source"]["oracle"]["differ"] == 2 and m["by_source"]["parity"]["differ"] == 5


def test_latency_percentiles_are_read_from_the_rows():
    rows = [judged("allow", "allow", ms=float(i)) for i in range(1, 101)]
    lat = gatecorpus.metrics(rows)["latency_ms"]
    assert lat["n"] == 100 and lat["p50"] == pytest.approx(50.5) and lat["p95"] == pytest.approx(95.05)


def test_the_report_prints_the_matrix_then_the_rates_then_the_sources_then_the_latency():
    text = "\n".join(gatecorpus.report_lines(gatecorpus.metrics(matrix_rows())))
    order = [text.index(s) for s in ("confusion", "false blocks", "misses", "softened", "source", "check() latency")]
    assert order == sorted(order)
    assert "3 of 10" in text and "30.0%" in text and "95% CI 10.8% to 60.3%" in text


def test_failures_split_the_unexpected_from_the_fixed():
    rows = [
        judged("allow", "deny"),
        judged("allow", "deny", known=True),
        judged("deny", "deny", known=True),
        judged("deny", "deny"),
    ]
    bad, fixed = gatecorpus.failures(rows)
    assert [r["known"] for r in bad] == [False] and [r["known"] for r in fixed] == [True]


# --- the loader and the placeholders ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad, words",
    [
        ({"extra": 1}, "unknown key 'extra'"),
        ({"id": "has space"}, "has a character outside"),
        ({"expect": "maybe"}, "expect must be one of"),
        ({"source": "elsewhere"}, "source must be one of"),
        ({"why": " "}, "why must be non-empty"),
        ({"payload": {"kind": "command"}}, "needs a kind"),
        ({"payload": {"kind": "teleport", "value": "x"}}, "needs a kind"),
        ({"known": "yes"}, "known must be true or false"),
        ({"guards": ["no-such-guard"]}, "unknown guard"),
    ],
)
def test_a_bad_row_is_refused_with_its_file_and_line(tmp_path, bad, words):
    f = write(tmp_path / "c.jsonl", [row("ok", "ls", "allow"), {**row("bad", "ls", "allow"), **bad}])
    with pytest.raises(WsError) as e:
        gatecorpus.load([f])
    assert "c.jsonl:2" in e.value.message and words in e.value.message and e.value.kind == "bad_input"


def test_a_repeated_id_across_files_is_refused(tmp_path):
    a = write(tmp_path / "a.jsonl", [row("same", "ls", "allow")])
    b = write(tmp_path / "b.jsonl", [row("same", "pwd", "allow")])
    with pytest.raises(WsError, match="'same' is used twice"):
        gatecorpus.load([a, b])


def test_fill_replaces_the_two_placeholders_and_leaves_the_shells_own_braces():
    text = "cat {HOME}/x ${HOME}/y {CWD}/z $HOME ~/w {HOMES} {a,b}"
    assert gatecorpus.fill(text, "/h", "/c") == "cat /h/x ${HOME}/y /c/z $HOME ~/w {HOMES} {a,b}"
    assert gatecorpus.fill({"a": ["{CWD}", 1, None]}, "/h", "/c") == {"a": ["/c", 1, None]}


def test_a_neutral_call_is_written_in_each_runners_own_shape():
    r = gatecorpus.Row("x", {"kind": "command", "value": "ls {CWD}"}, "allow", "control", "w")
    assert gatecorpus.render(r, "claude-code", "/h", "/c") == {
        "tool_name": "Bash",
        "tool_input": {"command": "ls /c"},
        "cwd": "/c",
        "session_id": "replay",
    }
    assert gatecorpus.render(r, "opencode", "/h", "/c") == {"tool": "bash", "args": {"command": "ls /c"}}
    w = gatecorpus.Row("w", {"kind": "write", "value": "{HOME}/a", "content": "x"}, "allow", "control", "w")
    assert gatecorpus.render(w, "claude-code", "/h", "/c")["tool_input"] == {"file_path": "/h/a", "content": "x"}
    assert gatecorpus.render(w, "opencode", "/h", "/c")["args"] == {"filePath": "/h/a", "content": "x"}
    rd = gatecorpus.Row("r", {"kind": "read", "value": "~/a"}, "allow", "control", "w")
    assert gatecorpus.render(rd, "claude-code", "/h", "/c")["tool_input"] == {"file_path": "~/a"}
    assert gatecorpus.render(rd, "opencode", "/h", "/c")["args"] == {"filePath": "~/a"}
    s = gatecorpus.Row("s", {"kind": "search", "value": "{CWD}"}, "allow", "control", "w")
    assert gatecorpus.render(s, "claude-code", "/h", "/c")["tool_input"]["path"] == "/c"
    assert gatecorpus.render(s, "opencode", "/h", "/c")["args"]["path"] == "/c"


def test_a_row_with_its_own_payload_belongs_to_its_runner_alone():
    raw = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    r = gatecorpus.Row("x", raw, "allow", "recorded", "w", runner="claude-code")
    assert gatecorpus.render(r, "claude-code", "/h", "/c") == raw
    assert gatecorpus.render(r, "opencode", "/h", "/c") is None
    neutral = gatecorpus.Row("y", {"kind": "command", "value": "ls"}, "allow", "control", "w")
    with pytest.raises(WsError, match="no way to write a neutral call"):
        gatecorpus.render(neutral, "some-other-runner", "/h", "/c")


# --- replay: the exit code, the ratchet, fail closed ------------------------------------------------------------


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_mislabelled_row_exits_1_and_names_its_id(home, capsys, runner):
    f = write(
        home / "c.jsonl",
        [row("fine", "ls", "allow"), row("mislabelled-ls", "ls", "deny"), row("stops", "rm -rf /", "deny")],
    )
    code, out, _ = run([str(f), "--runner", runner, "--metrics"], capsys)
    assert code == 1
    assert "FAIL  mislabelled-ls: expected deny, got allow" in out
    assert "FAIL  fine" not in out and "FAIL  stops" not in out


@pytest.mark.parametrize("runner", RUNNERS)
def test_both_runners_judge_the_same_neutral_rows_the_same_way(home, capsys, runner):
    rows = [
        row("a", "ls", "allow"),
        row("b", "git push --force", "deny"),
        row("c", "~/.ssh/id_ed25519", "deny", kind="read"),
        row("d", "{HOME}/.ssh/id_ed25519", "deny", kind="read"),
        row("e", "{CWD}/notes.md", "allow", kind="write"),
        row("f", "{HOME}", "deny", kind="search"),
        row("g", "{CWD}", "allow", kind="search"),
        row("h", "cat $HOME/.ssh/id_ed25519", "deny"),
    ]
    code, out, _ = run([str(write(home / "c.jsonl", rows)), "--runner", runner], capsys)
    assert code == 0, out
    assert "8 rows: 3 allow, 5 deny, 0 ask; 0 differ" in out


def test_a_known_row_is_counted_but_does_not_fail(home, capsys):
    f = write(home / "c.jsonl", [row("gap", "ls", "deny", known=True), row("ok", "ls", "allow")])
    code, out, _ = run([str(f), "--runner", "claude-code", "--metrics"], capsys)
    assert code == 0 and "2 rows (1 known)" in out


def test_a_known_row_that_starts_passing_fails_until_it_is_unmarked(home, capsys):
    f = write(home / "c.jsonl", [row("was-a-gap", "ls", "allow", known=True)])
    code, out, _ = run([str(f), "--runner", "claude-code"], capsys)
    assert code == 1 and 'FIXED was-a-gap: now allow as expected; remove its "known" mark' in out
    write(f, [row("was-a-gap", "ls", "allow")])
    assert run([str(f), "--runner", "claude-code"], capsys)[0] == 0


def test_a_row_that_cannot_be_judged_is_a_deny(home, capsys):
    broken = {"tool_name": "Bash", "tool_input": {}}  # no command: the live hook denies it
    f = write(
        home / "c.jsonl",
        [row("unreadable", "", "deny", source="recorded") | {"payload": broken, "runner": "claude-code"}],
    )
    code, out, _ = run([str(f), "--runner", "claude-code", "--json"], capsys)
    data = json.loads(out)["data"]
    assert code == 0 and data["results"][0]["result"] == "deny" and data["results"][0]["kind"] == "error"
    assert "ws: 0 rows" in run([str(f), "--runner", "opencode"], capsys)[1]  # another runner's row is left out


def test_json_carries_the_metrics_and_the_results(home, capsys):
    f = write(home / "c.jsonl", [row("a", "ls", "allow"), row("b", "rm -rf /", "deny")])
    code, out, _ = run([str(f), "--runner", "claude-code", "--metrics", "--json"], capsys)
    data = json.loads(out)["data"]
    assert code == 0 and data["rows"] == 2 and data["metrics"]["confusion"]["deny"]["deny"] == 1
    assert data["results"][1]["kind"] == "rule:command" and data["results"][0]["kind"] is None
    assert data["metrics"]["latency_ms"]["n"] == 2


def test_a_corpus_runs_without_a_flag_and_a_plain_file_still_takes_one_file(home, capsys):
    corpus = write(home / "c.jsonl", [row("a", "ls", "allow")])
    plain = home / "p.jsonl"
    plain.write_text(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}) + "\n")
    assert run([str(corpus), "--runner", "claude-code"], capsys)[0] == 0
    code, _, err = run([str(plain), str(plain), "--runner", "claude-code"], capsys)
    assert code == 1 and "give one file of payloads" in err


# --- the scratch folders ----------------------------------------------------------------------------------------


def test_placeholders_never_leave_the_scratch_folders(home, monkeypatch):
    seen = []
    real = gate.check

    def spy(runner, raw, pol_, protect=(), *a, **kw):
        seen.append((raw, kw.get("home"), os.environ["HOME"], os.environ["WS_HOME"]))
        return real(runner, raw, pol_, protect, *a, **kw)

    monkeypatch.setattr(gate, "check", spy)
    before = {k: os.environ[k] for k in ("HOME", "WS_HOME")}
    rows = gatecorpus.parse(
        "\n".join(
            json.dumps(r)
            for r in [
                row("a", "cat {HOME}/x {CWD}/y ${HOME}/z", "allow"),
                row("b", "{CWD}/p", "allow", kind="write"),
            ]
        ),
        "c",
    )
    for runner in RUNNERS:
        seen.clear()
        gate.replay_corpus(runner, rows)
        assert len(seen) == 2
        for raw, scratch, env_home, env_ws in seen:
            assert "{HOME}" not in raw.replace("${HOME}", "") and "{CWD}" not in raw
            assert str(home / "home") not in raw and str(home / "wshome") not in raw
            assert env_home == str(scratch) and env_ws == str(scratch / ".ws")
            assert not Path(scratch).exists()  # removed after the run
            assert str(Path(scratch).parent) in raw
        assert "${HOME}/z" in seen[0][0]
    assert {k: os.environ[k] for k in ("HOME", "WS_HOME")} == before


def test_a_corpus_run_does_not_read_the_persons_policy(home, capsys):
    (home / "wshome").mkdir()
    (home / "wshome" / "policy.toml").write_text('[gate]\ndeny_commands = ["ls*"]\n')
    f = write(home / "c.jsonl", [row("ls", "ls", "allow")])
    assert run([str(f), "--runner", "claude-code"], capsys)[0] == 0
    assert run([str(f), "--runner", "claude-code", "--policy", str(home / "wshome" / "policy.toml")], capsys)[0] == 1


def test_a_row_can_switch_a_guard_on_for_itself(home, capsys):
    f = write(
        home / "c.jsonl",
        [
            row("off", "git add -A", "allow"),
            row("on", "git add -A", "deny", guards=["bulk-staging"]),
            row("off-again", "git add -A", "allow"),
        ],
    )
    assert run([str(f), "--runner", "claude-code"], capsys)[0] == 0


# --- save and diff ----------------------------------------------------------------------------------------------


def test_save_then_diff_with_no_change_is_clean(home, capsys):
    f = write(home / "c.jsonl", [row("a", "ls", "allow"), row("b", "rm -rf /", "deny")])
    saved = home / "old.json"
    assert run([str(f), "--runner", "claude-code", "--save", str(saved)], capsys)[0] == 0
    data = json.loads(saved.read_text())
    assert data["schema"] == "ws.gate-decisions/1" and data["runner"] == "claude-code"
    assert data["decisions"] == {
        "a": {"decision": "allow", "kind": "none"},
        "b": {"decision": "deny", "kind": "rule:command"},
    }
    code, out, _ = run([str(f), "--runner", "claude-code", "--diff", str(saved)], capsys)
    assert code == 0 and "0 changed, 0 not listed" in out


def test_a_one_rule_policy_edit_shows_in_the_diff_and_passes_only_when_expected(home, capsys):
    pol_file = home / "edited.toml"
    pol_file.write_text(policy.default_toml().replace("deny_commands = [", 'deny_commands = ["echo planted*", ', 1))
    before = write(
        home / "before.jsonl", [row("planted", "echo planted", "deny", known=True), row("ls", "ls", "allow")]
    )
    after = write(home / "after.jsonl", [row("planted", "echo planted", "deny"), row("ls", "ls", "allow")])
    old = home / "old.json"
    assert run([str(before), "--runner", "claude-code", "--save", str(old)], capsys)[0] == 0
    # the edit is not listed: the changed id is named and the run fails
    code, out, _ = run([str(after), "--runner", "claude-code", "--policy", str(pol_file), "--diff", str(old)], capsys)
    assert code == 1
    assert "changed planted: allow -> deny  [none -> rule:command]  (not listed in --expect-changes)" in out
    assert "1 changed, 1 not listed" in out and "changed ls" not in out
    # the edit is listed with its arrow: it passes
    expected = home / "expected.txt"
    expected.write_text("# the new deny rule\nplanted: allow -> deny\n")
    args = [str(after), "--runner", "claude-code", "--policy", str(pol_file), "--diff", str(old)]
    code, out, _ = run([*args, "--expect-changes", str(expected)], capsys)
    assert code == 0 and "1 changed, 0 not listed" in out
    # a bare id is enough, and a wrong arrow is not
    expected.write_text("planted\n")
    assert run([*args, "--expect-changes", str(expected)], capsys)[0] == 0
    expected.write_text("planted: deny -> allow\n")
    assert run([*args, "--expect-changes", str(expected)], capsys)[0] == 1


def test_a_listed_change_that_did_not_happen_fails(home, capsys):
    f = write(home / "c.jsonl", [row("a", "ls", "allow")])
    old = home / "old.json"
    run([str(f), "--runner", "claude-code", "--save", str(old)], capsys)
    expected = home / "expected.txt"
    expected.write_text("a: allow -> deny\n")
    code, out, _ = run(
        [str(f), "--runner", "claude-code", "--diff", str(old), "--expect-changes", str(expected)], capsys
    )
    assert code == 1 and "listed but unchanged: a" in out


def test_rows_added_or_removed_are_reported_and_do_not_fail(home, capsys):
    old = home / "old.json"
    run(
        [
            str(write(home / "a.jsonl", [row("a", "ls", "allow"), row("gone", "pwd", "allow")])),
            "--runner",
            "opencode",
            "--save",
            str(old),
        ],
        capsys,
    )
    new = write(home / "b.jsonl", [row("a", "ls", "allow"), row("fresh", "pwd", "allow")])
    code, out, _ = run([str(new), "--runner", "opencode", "--diff", str(old)], capsys)
    assert code == 0 and "rows added 1, removed 1" in out


def test_diff_refuses_decisions_saved_for_another_runner_or_by_something_else(home, capsys):
    f = write(home / "c.jsonl", [row("a", "ls", "allow")])
    old = home / "old.json"
    run([str(f), "--runner", "claude-code", "--save", str(old)], capsys)
    code, _, err = run([str(f), "--runner", "opencode", "--diff", str(old)], capsys)
    assert code == 1 and "saved for claude-code, not opencode" in err
    old.write_text("[]")
    assert run([str(f), "--runner", "claude-code", "--diff", str(old)], capsys)[0] == 1
    assert run([str(f), "--runner", "claude-code", "--expect-changes", str(old)], capsys)[0] == 2


def test_diff_json_lists_the_changes(home, capsys):
    f = write(home / "c.jsonl", [row("a", "ls", "allow")])
    old = home / "old.json"
    old.write_text(
        json.dumps(
            {
                "schema": "ws.gate-decisions/1",
                "runner": "claude-code",
                "decisions": {"a": {"decision": "deny", "kind": "rule:command"}},
            }
        )
    )
    code, out, _ = run([str(f), "--runner", "claude-code", "--diff", str(old), "--json"], capsys)
    d = json.loads(out)["data"]["diff"]
    assert code == 1 and d["unexpected"] == ["a"] and d["changed"][0]["to"] == {"decision": "allow", "kind": "none"}


# --- the shipped corpus -----------------------------------------------------------------------------------------


def shipped_rows() -> list[gatecorpus.Row]:
    return gatecorpus.load(CORPUS)


def test_the_shipped_corpus_is_big_enough_and_has_every_source():
    rows = shipped_rows()
    assert len(rows) >= 300
    assert sum(r.expect == "allow" for r in rows) / len(rows) >= 0.35  # false blocks stay measurable
    assert {r.source for r in rows} >= {"oracle", "parity", "regression", "control"}
    assert all(r.why.strip() for r in rows) and len({r.id for r in rows}) == len(rows)
    assert {r.expect for r in rows} == {"allow", "deny", "ask"}


def test_the_shipped_corpus_names_no_real_path_host_or_person():
    text = "\n".join(p.read_text() for p in CORPUS)
    assert not re.search(r"/(home|Users|root)/|\\\\Users\\\\", text)
    assert not re.search(r"[\w.+-]+@(?!example\.(com|org)\b)[\w-]+\.[a-z]{2,}", text)
    assert not re.search(
        r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        text.replace("127.0.0.1", "").replace("0.0.0.0", "").replace("192.0.2.10", ""),
    )


def test_the_shipped_corpus_ratchet_pins_every_known_row():
    known = {}
    for r in shipped_rows():
        if r.known:
            known[r.source] = known.get(r.source, 0) + 1
    assert {s: known.get(s, 0) for s in KNOWN_BASELINE} == KNOWN_BASELINE, (
        "a new known row needs a reason and a raised number here; a fixed one lowers it"
    )


def test_a_shipped_known_row_is_only_ever_a_call_that_should_be_let_through():
    # A shipped corpus is public. A known row that should be stopped would publish a call the released gate lets
    # through, so those rows live in a private corpus until the fix ships.
    held = [r.id for r in shipped_rows() if r.known and r.expect != "allow"]
    assert held == [], f"known rows that expect deny or ask belong in a private corpus: {held}"


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_shipped_corpus_holds_on_each_runner(home, capsys, runner):
    code, out, _ = run([*map(str, CORPUS), "--runner", runner, "--metrics"], capsys)
    assert code == 0, out
    assert "false blocks" in out and "check() latency" in out


def test_both_runners_agree_on_every_shipped_row():
    rows = shipped_rows()
    claude = {r["id"]: r["result"] for r in gate.replay_corpus("claude-code", rows)}
    opencode = {r["id"]: r["result"] for r in gate.replay_corpus("opencode", rows)}
    assert claude == opencode
