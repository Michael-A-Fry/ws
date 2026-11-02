"""The home page areas: they read records and nothing else, each says what it shows when empty, an opaque item says to
open the session, a status is a glyph and a word, a cost is `not measured` until it is, and every text is escaped."""

from __future__ import annotations

import ast
import builtins
import importlib
import re
from dataclasses import replace
from pathlib import Path

import pytest

from ws.home import areas, page
from ws.home.areas import _fmt
from ws.home.records import Ask, EvalArm, Plugin, Session, Snapshot, SourceRow

SRC = Path(__file__).resolve().parents[2] / "src" / "ws" / "home"
BAD = "<script>alert(1)</script>"
TS = "2026-11-02T12:00:00.000Z"

# What an area or the page may import: the records, the shared UI, the areas themselves and plain text helpers.
ALLOWED = (
    "__future__", "html", "dataclasses", "types", "datetime", "ws.home.records", "ws.ui", "ws.home.areas", "ws.brand",
)  # fmt: skip
FORBIDDEN_NAMES = {"open", "os", "pathlib", "subprocess", "sources", "eval", "runrecord", "api", "openers", "server"}


def snap(**kw) -> Snapshot:
    base = {"ts": TS, "host": "testhost"}
    base.update(kw)
    return Snapshot(**base)


def session(**kw) -> Session:
    base = {"id": "11111111-aaaa-bbbb-cccc-000000000001", "source": "claude_code", "name": "builder", "host": "testhost",
            "tmux": "ws:@1.%1", "cwd": "/work/app", "repo": "app", "state": "working", "last_seen": TS}  # fmt: skip
    base.update(kw)
    return Session(**base)


def arm(**kw) -> EvalArm:
    base = {"arm": "haiku", "job": "eval-haiku", "trials": 10, "solved": 9, "pass_rate": 0.9,
            "pass_hat_k": {"k": 3, "value": 0.8}, "time_per_solved": {"p50_s": 12.0, "p90_s": 20.0},
            "tokens_per_solved": 90_000.0, "cost_per_solved_usd": 0.004,
            "trend": ({"batch": "b1", "ts": TS, "n": 5, "pass_rate": 0.8}, {"batch": "b2", "ts": TS, "n": 5, "pass_rate": 1.0}),
            "noise": {"points": 3.0, "wins_needed": 8}}  # fmt: skip
    base.update(kw)
    return EvalArm(**base)


def view(**kw) -> areas.View:
    live = kw.pop("live", False)
    return areas.View(snap(**kw), live)


# --- the slots -------------------------------------------------------------------------------------------------------


def test_every_slot_has_a_module_and_a_contract():
    ids = [s.id for s in areas.SLOTS]
    assert ids == ["composer", "lead", "needs_you", "sessions", "evals", "plugins", "terminal", "quiet"]
    assert [s.order for s in areas.SLOTS] == sorted(s.order for s in areas.SLOTS)
    on_disk = {p.stem for p in (SRC / "areas").glob("*.py") if not p.stem.startswith("_") and p.stem != "__init__"}
    assert on_disk == set(ids)  # a module with no slot, or a slot with no module, is a mistake
    for slot in areas.SLOTS:
        mod = importlib.import_module(f"ws.home.areas.{slot.id}")
        assert callable(mod.render) and mod.ID == slot.id
        for field in (slot.title, slot.reads, slot.empty, slot.opaque, slot.actions):
            assert field.strip()
        assert isinstance(slot.built, bool)


def test_plugins_and_terminal_are_slots_that_are_not_built_and_draw_nothing():
    unbuilt = {s.id for s in areas.SLOTS if not s.built}
    assert unbuilt == {"plugins", "terminal"}
    v = view(plugins=(Plugin("memory", on=True, built=True),), sessions=(session(),))
    for sid in unbuilt:
        assert areas.MODULES[sid].render(v) == ""
    assert [s.id for s in areas.built()] == ["composer", "lead", "needs_you", "sessions", "evals", "quiet"]


def test_the_page_draws_the_box_then_pinned_then_needs_you_then_sessions_and_evals_on_their_own_page():
    s = snap(
        lead=session(lead=True, name="Boss"),
        asks=(Ask(id="a1", source="asks_jsonl", kind="question", title="Which one?"),),
        sessions=(session(lead=True, name="Boss"), session(id="s2", name="second")),
        evals=(arm(),),
    )
    out = page.render(s)
    out = out[out.index('<main class="col"') :]  # the sidebar names sessions too
    steps = ('id="composer"', 'data-grp="pin"', "Boss", 'data-grp="need"', "Which one?", 'data-grp="work"', "second")
    marks = [out.index(m) for m in steps]
    assert marks == sorted(marks)
    assert out.index('id="v-home"') < out.index('id="v-evals"') and "Evaluation" in out.split('id="v-evals"')[1]
    assert out.index("Evaluation") > out.index("second")  # the arms are on the Evals page, below the list


# --- reading only through the API --------------------------------------------------------------------------------------


def _imports(path: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names += [base] + [f"{base}.{a.name}" for a in node.names]
    return names


def _files():
    return [*sorted((SRC / "areas").glob("*.py")), SRC / "page.py"]


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_every_area_reads_only_through_the_api(path):
    for name in _imports(path):
        assert name in ("", "ws.home") or name.startswith(ALLOWED), f"{path.name} imports {name}"
        assert not set(name.split(".")) & FORBIDDEN_NAMES, f"{path.name} imports {name}"
    text = path.read_text(encoding="utf-8")
    calls = {n.func.id for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"open", "eval", "exec", "__import__"}, path.name


def test_a_page_renders_from_a_fake_snapshot_with_every_source_and_the_file_system_failing(monkeypatch):
    from ws.home.sources import all_sources

    def boom(*a, **k):
        raise AssertionError("an area reached for something other than its records")

    for mod in all_sources():
        monkeypatch.setattr(mod, "read", boom)
        monkeypatch.setattr(mod, "available", boom)
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    monkeypatch.setattr(Path, "iterdir", boom)
    out = page.render(snap(sessions=(session(),), evals=(arm(),), asks=(Ask(id="a", source="s", kind="question"),)))
    assert "builder" in out and "haiku" in out


# --- each area's words -----------------------------------------------------------------------------------------------


def test_each_area_has_its_empty_state():
    v = view()
    assert "Pin a lead: set [home] lead in your profile" in areas.MODULES["lead"].render(v)
    assert areas.MODULES["needs_you"].EMPTY in areas.MODULES["needs_you"].render(v)
    assert "No sessions found" in areas.MODULES["sessions"].render(v)
    assert "ws eval run" in areas.MODULES["evals"].render(v)


def test_a_status_is_a_glyph_and_a_word_never_colour_alone():
    out = areas.MODULES["sessions"].render(
        view(sessions=tuple(session(id=f"s{i}", state=st) for i, st in enumerate(("working", "idle", "unknown"))))
    )
    for glyph, word in (("●", "working"), ("○", "idle")):
        assert re.search(
            rf'<span class="g" aria-hidden="true">{re.escape(glyph)}</span><span class="w">{word}</span>', out
        )
    ask = areas.MODULES["needs_you"].render(view(asks=(Ask(id="a", source="s", kind="permission", title="Run it?"),)))
    assert '<span class="g" aria-hidden="true">◆</span><span class="w">needs you</span>' in ask
    done = areas.MODULES["sessions"].render(view(sessions=(session(id="d", state="ended"),)))
    assert '<span class="g" aria-hidden="true">✓</span><span class="w">done</span>' in done


def test_a_cost_or_token_count_that_is_not_known_says_so():
    out = areas.MODULES["sessions"].render(view(sessions=(session(tokens=None, cost_usd=None),)))
    assert out.count("not measured") == 2  # in the row's body, which opens in place
    known = areas.MODULES["sessions"].render(
        view(sessions=(session(tokens={"input": 1000, "output": 2000}, cost_usd=12.5, cost_basis="otel"),))
    )
    assert "3k tokens" in known and "US$12.50" in known and "not measured" not in known


def test_idle_and_done_today_are_folded_done_is_capped_and_the_lead_is_not_listed_twice():
    done = tuple(session(id=f"e{i}", name=f"old{i}", state="ended", tmux="w:@1") for i in range(20))
    stale = session(id="old", name="ancient", state="ended", last_seen="2026-10-01T00:00:00.000Z")
    lead = session(id="L", name="Boss", lead=True)
    idle = session(id="i", name="napper", state="idle")
    out = areas.MODULES["sessions"].render(view(lead=lead, sessions=(lead, session(id="live"), idle, stale, *done)))
    assert 'data-grp="idle"' in out and 'data-grp="done"' in out and out.count('<details class="grp"') == 2
    assert '<em class="num">20</em>' in out and "and 15 more" in out
    assert 'href="#v-history"' in out  # "and N more" goes to History
    assert "Boss" not in out and "ancient" not in out
    assert out.count('class="r done"') == _fmt_shown()


def runs(job: str, n: int, **kw) -> tuple:
    return tuple(
        session(id=f"{job}-{i}", name=job, state="ended", tmux="", repo=job, last_seen=f"2026-11-02T11:{59 - i:02d}:00.000Z", **kw)
        for i in range(n)
    )  # fmt: skip


def test_headless_runs_fold_into_one_row_per_job_and_count_as_one_each():
    sessions = (*runs("triage-replay", 30), *runs("nightly", 2), session(id="live", state="working"))
    v = view(sessions=sessions)
    out = areas.MODULES["sessions"].render(v)
    assert out.count('class="r done"') == 2  # a row per job, never one per run
    assert "triage-replay" in out and "30 runs today" in out and "2 runs today" in out
    assert out.count("run-") == 0 and "triage-replay-7" not in out
    assert '<em class="num">2</em>' in out
    assert "1 working · 0 idle · 2 done today" in page._summary(v.snap)  # sessions, and each job as one


def test_an_ended_session_that_had_a_pane_is_a_done_row_not_a_job(box=None):
    sessions = (*runs("triage-replay", 5), session(id="mine", name="builder", state="ended", tmux="w:@2"))
    out = areas.MODULES["sessions"].render(view(sessions=sessions))
    assert out.count('class="r done"') == 2 and "builder" in out and "5 runs today" in out


def test_a_job_row_with_one_run_says_run_and_the_age_is_the_newest(box=None):
    out = areas.MODULES["sessions"].render(view(sessions=runs("solo", 1)))
    assert "1 run today" in out and "1 runs" not in out and ">1m<" in out


def test_done_today_shows_five_rows_when_opened_then_a_link_to_history():
    sessions = tuple(session(id=f"d{i}", name=f"w{i}", state="ended", tmux="w:@1") for i in range(9))
    out = areas.MODULES["sessions"].render(view(sessions=sessions))
    assert out.count('class="r done"') == 5 and "and 4 more ›" in out
    assert '<details class="grp" data-grp="done"' in out


def _fmt_shown() -> int:
    return areas.sessions.DONE_SHOWN


def test_needs_you_shows_the_recommended_option_first_and_says_where_it_is_answered():
    ask = Ask(
        id="a1", source="asks_jsonl", kind="question", session_name="builder", title="Pick one", decision="the plan",
        options=({"label": "Tidy", "description": "small"}, {"label": "Rewrite", "description": ""}),
        created="2026-11-02T10:00:00.000Z", answer_in="app",
    )  # fmt: skip
    out = areas.MODULES["needs_you"].render(view(asks=(ask,)))
    assert "asks_jsonl" in out and "builder" in out and ">2h<" in out and "Answer in the app" in out
    assert out.index("Tidy") < out.index("Rewrite") and out.count("recommended") == 1 and "Decides: the plan" in out
    assert "data-choice" not in out  # no server behind this one: it can't be answered here


def test_a_question_the_page_can_answer_has_the_form_and_the_first_option_is_the_only_filled_one():
    ask = Ask(id="a1", source="asks_jsonl", kind="question", title="Pick one", options=(
        {"label": "Tidy", "description": "small"}, {"label": "Rewrite", "description": ""}, {"label": "Stop"}))  # fmt: skip
    hub = areas.Hub(answerable=frozenset({"a1"}), answer_set=True)
    out = areas.MODULES["needs_you"].render(areas.View(snap(asks=(ask,)), True, hub))
    assert out.count('class="opt rec"') == 1 and out.index('class="opt rec"') < out.index('data-choice="Rewrite"')
    assert (
        "Something else…" in out
        and 'data-act="not-now"' in out
        and 'data-act="undo"' in out
        and '<details class="r need"' in out
    )
    assert " open>" in out  # the first answerable ask opens


def test_an_ask_the_page_is_not_set_up_to_answer_says_what_to_set():
    ask = Ask(id="a1", source="asks_jsonl", kind="question", title="Pick one", options=({"label": "Tidy"},))
    out = areas.MODULES["needs_you"].render(areas.View(snap(asks=(ask,)), True, areas.Hub()))
    assert "Set [home.answer] command to answer here" in out and "data-choice" not in out


def test_an_opaque_ask_and_session_say_to_open_the_session_and_show_nothing_hidden():
    ask = Ask(id="a1", source="asks_jsonl", kind="question", repo="vault", tmux="ws:2", opaque=True)
    sess = session(opaque=True, name="", cwd="", repo="vault", tokens=None, cost_usd=None, cost_basis="hidden")
    hub = areas.Hub(answerable=frozenset({"a1"}), answer_set=True)  # even if a page said so, an opaque ask has no form
    out = page.render(snap(asks=(ask,), sessions=(sess,)), live=True, hub=hub)
    assert out.count("Open the session to see this") >= 3  # the row's line and its body, for each
    assert "Hidden session" in out and "terminal only" in out and "not measured" not in out.split("Evals")[0]
    body = out.split("<script")[0]  # the page's own script mentions data-choice
    assert "/work/app" not in body and "builder" not in body and "data-choice" not in body


def test_an_arm_shows_each_figure_and_a_trend_and_the_noise_band():
    out = areas.MODULES["evals"].render(view(evals=(arm(), arm(arm="sonnet", pass_rate=0.97, trials=30, solved=29))))
    for text in ("90%", "9 of 10 trials solved", "pass^3", "0.80", "12.0 s p50", "20.0 s p90", "90k", "US$0.0040"):
        assert text in out, text
    assert "2 batches, latest 100%" in out and "<svg" in out
    assert "A gap under 3 points is noise; a head-to-head needs 8 wins." in out
    assert "sonnet and haiku are 7.0 points apart: outside the noise band" in out
    tie = areas.MODULES["evals"].render(view(evals=(arm(), arm(arm="b", pass_rate=0.92))))
    assert "inside the noise band" in tie


def test_a_metric_with_no_data_says_why():
    bare = arm(
        pass_rate=None, pass_hat_k={"k": None, "value": None}, time_per_solved={"p50_s": None, "p90_s": None},
        tokens_per_solved=None, cost_per_solved_usd=None, trend=(),
        not_measured={"success_rate": "no trials", "pass_hat_k": "needs 3 trials per task", "cost_per_solved": "no cost recorded"},
    )  # fmt: skip
    out = areas.MODULES["evals"].render(view(evals=(bare,)))
    assert out.count("not measured") >= 4
    for why in ("no trials", "needs 3 trials per task", "no cost recorded", "no data yet"):
        assert why in out
    assert "0 batches" in out


# --- escaping, and what the page may hold -----------------------------------------------------------------------------


def test_every_text_from_a_record_is_escaped():
    ask = Ask(
        id="a", source=BAD, kind="question", session_name=BAD, title=BAD, tmux="ws:1", repo=BAD,
        options=({"label": BAD, "description": BAD}, {"label": "b", "description": ""}),
    )  # fmt: skip
    sess = session(name=BAD, cwd=BAD, tmux=BAD, host=BAD, waiting_for=BAD, state="waiting")
    out = page.render(
        snap(
            host=BAD, asks=(ask,), sessions=(sess,), lead=replace(sess, lead=True), evals=(arm(arm=BAD, job=BAD),),
            sources=(SourceRow(name=BAD, ok=False, kind="failed", why=BAD),),
        )
    )  # fmt: skip
    assert BAD not in out and "&lt;script&gt;" in out
    assert out.count("<script") == 1  # the page's own script, and no other


def test_the_page_has_no_inline_style_and_no_outside_resource():
    out = page.render(snap(sessions=(session(),), evals=(arm(),)), "abc123xyz", live=True)
    assert " style=" not in out and "<link" not in out and "<img" not in out
    assert not re.search(r"(src|href)=\"https?://(?!example)", out)
    assert out.count('nonce="abc123xyz"') == 2  # the stylesheet and the script


def test_the_page_is_the_same_file_without_a_server_and_shows_the_line_to_paste():
    post = {"opener": "terminal", "label": "Switch to it", "post": True, "text": "tmux switch-client -t ws:1"}
    s = snap(sessions=(session(opens=(post,)),))
    static = page.render(s, live=False)
    live = page.render(s, live=True)
    assert "data-opener" not in static and "tmux switch-client -t ws:1" in static
    assert 'data-opener="terminal"' in live and 'data-kind="session"' in live and "data-opener" in live
    assert "/api/open" in live and "/api/open" not in static


def test_a_link_is_drawn_only_for_a_safe_scheme():
    good = {"opener": "claude_app", "label": "Open in Claude", "href": "https://claude.ai/code/x"}
    bad = {"opener": "browser", "label": "Open in browser", "href": "javascript:alert(1)"}
    out = areas.MODULES["sessions"].render(view(sessions=(session(opens=(good, bad)),)))
    assert 'href="https://claude.ai/code/x"' in out and 'rel="noopener noreferrer"' in out
    assert "javascript:" not in out


def test_the_sources_list_is_folded_and_says_which_need_a_look():
    rows = (
        SourceRow(name="ws_runs", ok=True, n=3),
        SourceRow(name="collie", ok=False, kind="unavailable", why="not built: waiting for the trial's read format"),
        SourceRow(name="transcripts", ok=False, kind="unreadable", why="can't read it"),
    )
    out = page.render(snap(sources=rows))
    assert "<details>" in out and "1 need a look" in out
    assert "not built: waiting" in out and "3 records" in out and "unreadable" in out


def test_ages_counts_and_money_read_plainly():
    assert _fmt.age("2026-11-02T11:59:30Z", TS) == "just now"
    assert _fmt.age("2026-11-02T11:55:00Z", TS) == "5 min ago"
    assert _fmt.age("2026-10-30T12:00:00Z", TS) == "3 d ago"
    assert _fmt.age(None, TS) == "" and _fmt.age("junk", TS) == ""
    assert [_fmt.count(n) for n in (950, 12_300, 4_100_000, 2_500_000_000)] == ["950", "12.3k", "4.1M", "2.5G"]
    assert _fmt.usd(None) == "not measured" and _fmt.usd(0.0052) == "US$0.0052" and _fmt.usd(12) == "US$12.00"
    assert _fmt.percent(1.0) == "100%" and _fmt.percent(0.9795) == "98%" and _fmt.percent(0.935) == "93.5%"


# --- the Open button ---------------------------------------------------------------------------------------------------

TERM_POST = {"opener": "terminal", "label": "Switch to it", "post": True, "text": "tmux switch-client -t ws:1"}
TERM_TEXT = {"opener": "terminal", "label": "Attach", "text": "tmux attach -t ws:1"}
CMD_POST = {"opener": "command", "label": "Open with ws-term", "post": True, "text": "ws-term ws:1"}
APP = {"opener": "claude_app", "label": "Open in Claude", "href": "https://claude.ai/code/x"}


def _cell(opens, live=True, rec=None):
    return _fmt.open_cell(view(live=live), rec or session(opens=tuple(opens)), "session")


def _visible(html_text: str) -> str:
    """The text a person reads: tags and attribute values removed."""
    return re.sub(r"<[^>]*>", "", html_text)


def test_the_open_button_is_labelled_and_the_command_sits_behind_a_copy_control():
    out = _cell([TERM_POST])
    assert 'data-opener="terminal"' in out and "Open →" in _visible(out)
    assert 'data-copy="tmux switch-client -t ws:1"' in out and ">Copy<" in out
    assert "tmux" not in _visible(out)  # the raw command is never a visible label


def test_without_a_server_the_open_button_copies_the_command():
    for o in (TERM_TEXT, TERM_POST):
        out = _cell([o], live=False)
        assert "data-opener" not in out and "data-copy=" in out and "Open →" in _visible(out)
        assert "tmux" not in _visible(out)


def test_a_record_with_a_claude_app_link_gets_a_second_link():
    out = _cell([TERM_POST, APP])
    assert '<a class="act" href="https://claude.ai/code/x"' in out and ">Claude app</a>" in out
    assert "Open in Claude" not in out


def test_the_phone_opener_is_the_command_opener_when_one_is_set():
    out = _cell([TERM_POST, CMD_POST])
    desk, phone = out.split('<span class="o-phone">')
    assert 'data-opener="terminal"' in desk and 'data-opener="command"' in phone
    assert 'data-opener="terminal"' not in phone and "terminal only" not in out


def test_without_a_command_opener_the_phone_gets_a_disabled_button_that_says_why():
    out = _cell([TERM_POST])
    phone = out.split('<span class="o-phone">')[1]
    assert "<button" in phone and " disabled" in phone and 'title="terminal only"' in phone
    assert "terminal only</span>" in phone and "data-opener" not in phone


def test_a_record_with_nothing_to_open_it_with_draws_no_button():
    assert _cell([]) == ""
    assert "Open →" not in areas.MODULES["sessions"].render(view(sessions=(session(),)))


# --- the shell: places, the box, the menus ------------------------------------------------------------------------------


def test_every_place_in_the_sidebar_leads_somewhere_and_the_unbuilt_ones_say_what_is_coming():
    out = page.render(snap(sessions=(session(),), evals=(arm(),)))
    links = set(re.findall(r'href="#(v-[a-z]+)"', out))
    ids = set(re.findall(r'<section class="pg" id="(v-[a-z]+)"', out))
    assert links and links <= ids  # no link to a page that isn't there
    for place in ("search", "scheduled", "history", "skills", "plugins", "connections"):
        page_html = out.split(f'id="v-{place}"')[1].split("</section>")[0]
        assert "Coming next" in page_html and 'href="#v-home"' in page_html
    for label in ("New session", "Search", "Home", "Scheduled", "History", "Skills", "Plugins", "Connections", "Evals"):
        assert f">{label}" in out or f"</svg>{label}" in out, label


def test_the_view_menu_and_settings_hold_what_the_plan_names():
    out = page.render(snap())
    for text in ("Need", "Workspace", "Model", "Machine", "Comfortable", "Compact", "Pinned", "Done today",
                 "Save as view", "System", "Light", "Dark", "Sans", "Mono", "Indigo", "Teal", "Violet"):  # fmt: skip
        assert text in out, text
    assert 'data-store="view" data-k="group"' in out and 'data-store="look" data-k="theme"' in out


def test_the_list_carries_what_the_script_needs_to_regroup_it():
    s = session(id="s1", repo="app", host="other", runner="claude-code")
    out = areas.MODULES["sessions"].render(view(sessions=(s,)))
    assert 'data-ws="app"' in out and 'data-machine="other"' in out and 'data-model="claude-code"' in out
    assert 'data-need="work"' in out


def test_a_row_says_what_it_is_doing_where_it_runs_and_how_old_it_is():
    out = areas.MODULES["sessions"].render(view(sessions=(session(last_seen="2026-11-02T11:48:00.000Z"),)))
    assert (
        ">12m<" in out and "Working" in out and "this machine" in out and "ws:@1.%1" in out and "claude-code" not in out
    )
    other = areas.MODULES["sessions"].render(view(sessions=(session(host="laptop", runner="opencode"),)))
    assert ">laptop<" in other and ">opencode<" in other


def test_a_session_with_an_ask_shows_as_the_ask_and_waiting_with_no_ask_is_still_needs_you():
    s = session(id="s1", state="waiting", waiting_for="a reply")
    waiting = session(id="s2", state="waiting", tmux="", waiting_for="a reply", name="lonely")
    ask = Ask(id="a1", source="asks_jsonl", kind="question", session_id="s1", title="Which?")
    v = view(sessions=(s, waiting), asks=(ask,))
    assert 'class="r work"' not in areas.MODULES["sessions"].render(v)
    out = areas.MODULES["needs_you"].render(v)
    assert out.count('class="r need"') == 2 and "Waiting for: a reply" in out and "lonely" in out


def test_the_quiet_line_says_the_last_eval_verdict_in_words():
    tie = view(evals=(arm(), arm(arm="sonnet", pass_rate=0.92)))
    assert "sonnet and haiku are within noise" in areas.MODULES["quiet"].render(tie)
    clear = view(evals=(arm(), arm(arm="sonnet", pass_rate=0.5, solved=5)))
    assert "haiku leads sonnet by 40 points" in areas.MODULES["quiet"].render(clear)
    assert "no eval trials yet" in areas.MODULES["quiet"].render(view())
    ended = view(sessions=(session(id="e1", state="ended"), session(id="e2", state="ended", tmux="")))
    assert "1 ended session window" in areas.MODULES["quiet"].render(ended)


def test_the_machine_filter_is_there_only_when_more_than_one_machine_is_seen():
    one = page.render(snap(sessions=(session(),)), hub=areas.Hub(machines=("testhost",)))
    two = page.render(snap(sessions=(session(),)), hub=areas.Hub(machines=("testhost", "laptop")))
    assert 'id="machine"' not in one and 'id="machine"' in two and ">laptop<" in two
