"""ws.triage: rules first, one small model call per thread, checks before a draft, nothing sent. A fixture mailbox and a
stub model stand in for the network and the runner, so no test reads a real HOME, a real mailbox or a model."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import os
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

from ws import joblock, jobs, mail, paths, policy, run, triage
from ws.minters import imap

NOW = datetime(2026, 11, 23, 5, 0, tzinfo=UTC)
USER = "me@example.com"
RULES = triage.parse_rules(
    """
default = "ask-model"

[[rule]]
id = "bulk"
label = "ws/low"
bulk = true

[[rule]]
id = "bank"
label = "ws/fyi"
domain = "bank.example"

[[rule]]
id = "sub-receipt"
label = "ws/fyi"
subject = "^receipt\\\\b"
"""
)


def raw(sender="Sam T <sam@example.org>", subject="Hello", body="Can you send the report?", **headers) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, USER, subject
    m["Date"] = "Sun, 22 Nov 2026 08:15:00 +1300"
    m["Message-ID"] = headers.pop("mid", f"<{abs(hash((sender, subject, body))) % 10**9}@example.org>")
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m.as_bytes()


class Route:
    """A mailbox of fixtures: uid -> raw bytes. Records every label and draft; can be told to refuse them."""

    def __init__(self, name="a", uidvalidity=7, user=USER):
        self.name, self.uv, self.owner = name, uidvalidity, user
        self.msgs: dict[int, bytes] = {}
        self.labels: list[tuple[str, str]] = []
        self.drafts: list[tuple[str, str]] = []
        self.searches: list[tuple[date, str | None]] = []
        self.total_extra = 0
        self.fail_label = False

    def add(self, uid: int, data: bytes) -> int:
        self.msgs[uid] = data
        return uid

    def search(self, since, query, after=None):
        """As the broker: the newest page, or with `after` the oldest page above that uid."""
        self.searches.append((since, query))
        if after is None:
            uids = sorted(self.msgs, reverse=True)
        else:
            uids = sorted(u for u in self.msgs if u > after)
        return self.uv, uids[: imap.MAX_RESULTS], len(uids) + self.total_extra

    def ref(self, uidvalidity, uid):
        return mail.make_uid(self.name, uidvalidity, uid)

    def fetch(self, ref):
        _, uid = mail.split_uid(ref, self.name)
        return triage.Raw(ref, self.msgs[uid], None, False)

    def user(self):
        return self.owner

    def label(self, ref, label):
        if self.fail_label:
            raise mail.MailError("broker_unavailable", "down")
        self.labels.append((ref, label))

    def draft(self, ref, body):
        self.drafts.append((ref, body))


class Model:
    """A stub model: answers from a queue (a dict or a raw string), and counts what it was given."""

    def __init__(self, *answers, ok=True, tokens=(100, 20), cost=0.001):
        self.answers, self.ok, self.tokens, self.cost = list(answers), ok, tokens, cost
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, instructions, data, budget):
        self.calls.append((instructions, data, budget))
        a = self.answers.pop(0) if self.answers else reply(True, "default", "Hi Sam, thanks for this. I will check.")
        text = a if isinstance(a, str) else json.dumps(a)
        return triage.Reply(self.ok, text, *self.tokens, self.cost, None if self.ok else "the model run ended error")


def reply(needs, reason="r", draft=None):
    return {"needs_reply": needs, "reason": reason, "draft": draft}


def config(tmp_path, **kw) -> triage.Config:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Answer with the JSON object.", encoding="utf-8")
    rules = tmp_path / "rules.toml"
    rules.write_text('default = "ask-model"\n', encoding="utf-8")
    cfg = triage.parse_config(
        {"mailboxes": ["a"], "rules": str(rules), "prompt_file": str(prompt), "summary_dir": str(tmp_path / "ctx")},
        "inbox",
        tmp_path,
        {"HOME": str(tmp_path)},
    )
    return _replace(cfg, **kw)


def _replace(cfg, **kw):
    """The settings with fields changed; `model={...}` changes fields of the model settings."""
    model = kw.pop("model", None)
    out = dataclasses.replace(cfg, **kw)
    return dataclasses.replace(out, model=dataclasses.replace(out.model, **model)) if model else out


def go(tmp_path, route, model, rules=RULES, cfg=None, dry=False, now=NOW, routes=None):
    cfg = cfg or config(tmp_path)
    return triage.execute(
        cfg, rules, routes or {"a": route}, model,
        state_file=tmp_path / "state" / "inbox.json", log=tmp_path / "logs" / "triage.jsonl", now=now, dry_run=dry,
    )  # fmt: skip


# ---------------------------------------------------------------- rules


def test_the_first_rule_that_matches_wins_and_its_id_is_the_answer():
    f = lambda **k: triage.Facts(**{"mailbox": "a", "sender": "x@example.org", "subject": "hi", "bulk": False, **k})
    assert triage.decide(RULES, f(bulk=True, sender="x@bank.example")) == ("ws/low", "bulk")  # bulk comes first
    assert triage.decide(RULES, f(sender="noreply@mail.bank.example")) == ("ws/fyi", "bank")  # a sub-domain
    assert triage.decide(RULES, f(sender="evilbank.example@example.org")) == (triage.ASK_MODEL, "default")
    assert triage.decide(RULES, f(subject="Receipt for your order")) == ("ws/fyi", "sub-receipt")
    assert triage.decide(triage.Rules((), "ws/low"), f()) == ("ws/low", "default")


def test_bulk_mail_is_found_by_its_headers():
    parse = lambda b: __import__("email").message_from_bytes(b)
    assert triage.is_bulk(parse(raw(List_Unsubscribe="<mailto:u@example.org>")))
    assert triage.is_bulk(parse(raw(List_Id="<news.example.org>")))
    assert triage.is_bulk(parse(raw(Precedence="bulk")))
    assert triage.is_bulk(parse(raw(Auto_Submitted="auto-generated")))
    assert not triage.is_bulk(parse(raw(Auto_Submitted="no")))
    assert not triage.is_bulk(parse(raw()))


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ('[[rule]]\nid = "x"\nlabel = "ws/trash"\nbulk = true\n', "label must be one of"),
        ('[[rule]]\nid = "x"\nlabel = "ws/low"\n', "needs at least one of"),
        ('[[rule]]\nid = "X y"\nlabel = "ws/low"\nbulk = true\n', "id must be"),
        (
            '[[rule]]\nid = "x"\nlabel = "ws/low"\nbulk = true\n[[rule]]\nid = "x"\nlabel = "ws/low"\nbulk = true\n',
            "used twice",
        ),
        ('[[rule]]\nid = "x"\nlabel = "ws/low"\nsubject = "("\n', "not a valid pattern"),
        ('[[rule]]\nid = "x"\nlabel = "ws/low"\nbulk = true\nbody = "y"\n', "unknown key"),
        ('default = "ws/trash"\n', "default must be"),
        ("what = 1\n", "unknown key"),
    ],
)
def test_a_bad_rules_file_is_refused_whole(text, match):
    with pytest.raises(triage.TriageError, match=match):
        triage.parse_rules(text)


# ---------------------------------------------------------------- the model's answer


def test_the_answer_must_parse_to_the_fixed_schema():
    ok = triage.parse_verdict('```json\n{"needs_reply": true, "reason": "asks", "draft": "Hi."}\n```')
    assert ok == triage.Verdict(True, "asks", "Hi.")
    assert triage.parse_verdict('{"needs_reply": false, "reason": "fyi", "draft": "ignored"}').draft is None
    for bad in (
        "no object here",
        "{not json}",
        '{"needs_reply": "yes", "reason": "r", "draft": null}',
        '{"needs_reply": true, "reason": "r", "draft": null}',
        '{"needs_reply": false, "reason": "r"}',
        '{"needs_reply": false, "reason": "r", "draft": null, "extra": 1}',
        json.dumps({"needs_reply": False, "reason": "x" * 201, "draft": None}),
        '{"needs_reply": false, "reason": 5, "draft": null}',
    ):
        with pytest.raises(triage.SchemaError):
            triage.parse_verdict(bad)


# ---------------------------------------------------------------- the checks on a draft

THREAD = (
    "From: Sam <sam@example.org>\nSubject: Quote\nDate: Sun, 22 Nov 2026 03:15:00 +0000\n\n"
    "The quote is $1,250. Can you do Monday 14 Dec at 3pm? See https://example.org/quote for the plan."
)
GOOD = "Kia ora Sam,\nThanks for the quote. Monday 14th December at 3:00pm works for me. Cheers, Alex"


def test_a_plain_draft_that_repeats_the_threads_own_facts_passes():
    assert triage.check_draft(GOOD, THREAD) is None
    assert triage.check_draft("Hi Sam. The $1250 is fine. See https://example.org/quote.", THREAD) is None


@pytest.mark.parametrize(
    ("draft", "why"),
    [
        ("Kia ora Sam,\nThanks. Friday 18 December works for me.", "date"),  # a date the thread never named
        ("Kia ora Sam,\nThanks. I can do Thursday.", "date"),  # a weekday is a commitment too
        ("Kia ora Sam,\nThanks. 3pm works. Or 4pm.", "time"),
        ("Kia ora Sam,\nThat is fine. Say 17:45 on Monday 14 Dec.", "time"),
        ("Kia ora Sam,\nThanks. I can do it for $900.", "amount"),
        ("Kia ora Sam,\nThanks. I'll pay the $1,250 tomorrow.", "payment"),
        ("Kia ora Sam,\nThanks. The payment has been sent.", "payment"),
        ("Kia ora Sam,\nThanks. See https://evil.example.net/x for more.", "link"),
        ("Kia ora Sam,\nThanks. Email me at sam@example.net please.", "link"),
        ("Kia ora Sam,\nThanks. I can do it on the 15th.", "date"),  # a bare ordinal
        ("Kia ora Sam,\nThanks. See you at 4 on Monday.", "time"),  # an hour with no am or pm
        ("Kia ora Sam,\nThanks. The plan is up on quotes.example now.", "link"),  # any domain-shaped word
        ("Kia ora Sam,\nThanks for the quote. Consider it paid.", "payment"),
        ("Kia ora Sam,\nThanks for the quote. The payment is done.", "payment"),
        ("Kia ora Sam,\nThanks for the quote. I paid it this morning.", "payment"),
        ("Dear Sam,\nThanks for the quote. It looks good.", "Dear"),
        ("Thanks.", "length"),
        ("One. Two. Three. Four. Five. Six. Seven.", "length"),
    ],
)
def test_a_draft_that_invents_or_promises_is_refused_with_its_reason(draft, why):
    got = triage.check_draft(draft, THREAD)
    assert got is not None and why in got


def test_a_bare_ordinal_an_hour_or_a_domain_the_thread_named_passes():
    ok = "Kia ora Sam,\nThanks. The 14th at 3 works, as on example.org."
    assert triage.check_draft(ok, THREAD) is None


def test_a_header_date_is_not_an_offer_of_that_date():
    thread = "Date: Sun, 22 Nov 2026 08:15:00 +1300\nMessage-ID: <a@example.org>\n\nCan we talk?"
    assert triage.check_draft("Hi Sam. Sure thing. 8:15 is fine.", thread) is not None


def test_a_thread_that_asks_for_a_time_is_found():
    assert triage.asks_for_time("Are you free next week? When can we meet?")
    assert triage.asks_for_time("Can we reschedule?")
    assert not triage.asks_for_time("Thanks for the report.")


# ---------------------------------------------------------------- the pipeline on a fixture mailbox


def test_nothing_new_is_no_model_call_and_the_next_run_after_a_run_is_the_same(tmp_path):
    r, m = Route(), Model()
    rec = go(tmp_path, r, m)
    assert (rec["threads_seen"], rec["model_calls"], rec["errors"]) == (0, 0, []) and m.calls == []
    r.add(1, raw(List_Unsubscribe="<u>"))
    rec = go(tmp_path, r, m)
    assert rec["threads_seen"] == 1 and rec["rule_labelled"] == 1 and m.calls == []
    rec = go(tmp_path, r, m)  # the same mailbox again: the uid is behind the state now
    assert rec["threads_seen"] == 0 and r.labels == [("a:7-1", "ws/low")]


def test_rules_label_the_bulk_and_the_model_sees_only_the_rest(tmp_path):
    r = Route()
    r.add(1, raw(sender="deals@example.org", List_Unsubscribe="<u>"))
    r.add(2, raw(sender="alerts@bank.example", subject="Statement"))
    r.add(3, raw(sender="Sam T <sam@example.org>", subject="Report", body="Can you send the report by Friday?"))
    r.add(4, raw(sender="Kim <kim@example.org>", subject="Photos", body="Here are the photos from the weekend."))
    r.add(5, raw(sender="Me <me@example.com>", subject="Sent copy", body="x"))  # a message from the mailbox itself
    m = Model(
        reply(True, "asks for the report", "Kia ora Sam,\nI will send the report by Friday. Cheers, Alex"),
        reply(False, "photos, nothing to answer"),
    )
    rec = go(tmp_path, r, m)
    assert (rec["threads_seen"], rec["rule_labelled"], rec["threads_asked"], rec["model_calls"]) == (5, 2, 2, 2)
    assert len(m.calls) == 2
    assert sorted(r.labels) == [
        ("a:7-1", "ws/low"), ("a:7-2", "ws/fyi"), ("a:7-3", "ws/needs-reply"), ("a:7-4", "ws/fyi"),
    ]  # fmt: skip
    assert [d["rule"] for d in rec["decisions"]] == ["bulk", "bank", "default", "default", "builtin:self"]
    assert rec["drafts"] == 1 and r.drafts[0][0] == "a:7-3" and rec["refused"] == []
    assert rec["tokens_in"] == 200 and rec["tokens_out"] == 40 and rec["cost_usd"] == 0.002


def test_what_the_model_sees_is_the_instructions_the_voice_and_the_thread_as_data(tmp_path):
    r, m = Route(), Model(reply(False))
    r.add(1, raw(subject="Question", body="Can you help?"))
    voice = tmp_path / "voice.md"
    voice.write_text("Opens with Kia ora.\x00", encoding="utf-8")
    cfg = _replace(config(tmp_path), voice=triage.mail.clean(voice.read_text()).strip())
    go(tmp_path, r, m, cfg=cfg)
    instructions, data, budget = m.calls[0]
    assert "Answer with the JSON object." in instructions and "Opens with Kia ora." in instructions
    assert "Can you help?" in data and "untrusted content" in data and 0 < budget <= cfg.model.budget_usd


def test_a_thread_is_one_call_for_its_newest_message(tmp_path):
    r, m = Route(), Model(reply(True, "r", "Kia ora Sam,\nThanks, got it. Cheers, Alex"))
    r.add(1, raw(subject="Plan", body="First point.", mid="<a@example.org>"))
    r.add(2, raw(subject="Re: Plan", body="Second point.", mid="<b@example.org>", References="<a@example.org>"))
    r.add(
        3,
        raw(
            subject="Re: Plan", body="Third point.", mid="<c@example.org>", References="<a@example.org> <b@example.org>"
        ),
    )
    rec = go(tmp_path, r, m)
    assert rec["threads_seen"] == 3 and rec["threads_asked"] == 1 and len(m.calls) == 1
    data = m.calls[0][1]
    assert data.index("First point.") < data.index("Second point.") < data.index("Third point.")
    assert r.drafts[0][0] == "a:7-3"  # the reply goes against the newest message


def test_a_long_thread_is_cut_to_the_newest_messages_inside_the_cap(tmp_path):
    big = "word " * 400
    msgs = [
        triage.Message(i, f"a:7-{i}", "s@example.org", "S", "Re: T", False, "t", f"<<<m{i}>>> {big}")
        for i in (1, 2, 3, 4)
    ]
    out = triage.thread_data(msgs, 600)  # 600 tokens is 2400 characters: one message of about 2000 fits, not two
    assert "<<<m4>>>" in out and "<<<m3>>>" not in out
    alone = triage.thread_data(msgs[:1], 100)  # a single message over the cap is cut, never dropped
    assert alone.startswith("<<<m1>>>") and alone.endswith("[ws: message cut short]") and len(alone) < 500


def test_a_draft_with_an_invented_date_is_refused_flagged_and_not_saved(tmp_path):
    r = Route()
    r.add(1, raw(subject="Catch up", body="Do you want to catch up some time? When are you free?"))
    m = Model(
        reply(True, "wants to meet", "Kia ora Sam,\nGreat idea. How about Friday 18 December at 2pm? Cheers, Alex")
    )
    rec = go(tmp_path, r, m)
    assert r.drafts == [] and rec["drafts"] == 0
    assert rec["refused"] == [{"mailbox": "a", "uid": 1, "reason": "names a date not in the thread"}]
    assert r.labels == [("a:7-1", "ws/needs-reply")]  # it still needs a reply: the label says so
    why = [f["why"] for f in rec["flagged"]]
    assert triage.SCHEDULING_FLAG in why and any("draft refused" in w for w in why)


def test_a_bad_answer_is_retried_twice_then_flagged_and_never_drafted(tmp_path):
    r, m = Route(), Model("sorry, I cannot", "{}", '{"needs_reply": true}')
    r.add(1, raw())
    rec = go(tmp_path, r, m)
    assert len(m.calls) == 3 and rec["model_calls"] == 3 and r.drafts == [] and r.labels == []
    assert "Your last answer was refused" in m.calls[1][0] and "Your last answer was refused" not in m.calls[0][0]
    assert [f["why"] for f in rec["flagged"]] == ["the model's answer did not parse"]


def test_a_retry_that_parses_is_used(tmp_path):
    r, m = Route(), Model("nope", reply(False, "fine"))
    r.add(1, raw())
    rec = go(tmp_path, r, m)
    assert rec["model_calls"] == 2 and r.labels == [("a:7-1", "ws/fyi")] and rec["flagged"] == []


def test_a_dry_run_makes_no_label_no_draft_no_state_and_no_summary_but_does_call_the_model(tmp_path):
    r = Route()
    r.add(1, raw(List_Unsubscribe="<u>"))
    r.add(2, raw(subject="Report", body="Can you send the report?"))
    r.label = r.draft = lambda *a: pytest.fail("a dry run must not label or draft")  # type: ignore[assignment]
    m = Model(reply(True, "asks", "Kia ora Sam,\nI will send it soon. Cheers, Alex"))
    rec = go(tmp_path, r, m, dry=True)
    assert rec["dry_run"] is True and rec["model_calls"] == 1 and rec["drafts"] == 1 and rec["rule_labelled"] == 1
    assert not (tmp_path / "state").exists() and not (tmp_path / "logs").exists() and not (tmp_path / "ctx").exists()


def test_a_real_run_writes_state_a_record_and_the_summary_the_morning_note_reads(tmp_path):
    r = Route()
    r.add(1, raw(List_Unsubscribe="<u>"))
    r.add(2, raw(subject="Catch up", body="Can we catch up? When are you free?"))
    m = Model(reply(True, "asks", "Kia ora Sam,\nYes, let's do that. Cheers, Alex"))
    go(tmp_path, r, m)
    state = json.loads((tmp_path / "state" / "inbox.json").read_text())
    assert state["mailboxes"]["a"]["last_uid"] == 2 and state["mailboxes"]["a"]["uidvalidity"] == 7
    (line,) = [json.loads(x) for x in (tmp_path / "logs" / "triage.jsonl").read_text().splitlines()]
    assert line["schema"] == 1 and line["ts"] and line["threads_seen"] == 2 and line["cost_usd"] == 0.001
    assert "Sam" not in json.dumps(line) and "catch up" not in json.dumps(line).lower()  # ids and counts, no text
    day = f"{NOW.astimezone():%Y-%m-%d}"
    text = (tmp_path / "ctx" / f"summary-{day}.md").read_text()
    assert "2 threads" in text and "Drafts created: 1" in text and "1 low priority" in text and "1 need a reply" in text
    assert "Needs a reply:" in text and triage.SCHEDULING_FLAG in text and "Draft saved." in text


def test_the_days_summary_adds_up_across_runs_and_starts_again_on_a_new_day(tmp_path):
    r, m = Route(), Model(reply(False), reply(False), reply(False))
    r.add(1, raw(subject="One"))
    go(tmp_path, r, m)
    r.add(2, raw(subject="Two"))
    go(tmp_path, r, m, now=NOW.replace(hour=6))
    text = (tmp_path / "ctx" / f"summary-{NOW.astimezone():%Y-%m-%d}.md").read_text()
    assert "2 threads" in text and "2 for your information" in text and "token" not in text
    go(tmp_path, r, m, now=NOW.replace(day=24))  # nothing new, next day: a fresh summary that says so
    nxt = (tmp_path / "ctx" / f"summary-{NOW.replace(day=24).astimezone():%Y-%m-%d}.md").read_text()
    assert nxt.startswith("No new mail.") and "0 threads" in nxt


def test_a_quiet_run_leaves_the_days_summary_alone_once_there_is_one(tmp_path):
    r, m = Route(), Model(reply(False))
    r.add(1, raw())
    go(tmp_path, r, m)
    f = tmp_path / "ctx" / f"summary-{NOW.astimezone():%Y-%m-%d}.md"
    before = f.read_text()
    go(tmp_path, r, m, now=NOW.replace(hour=7))
    assert f.read_text() == before


def test_only_mail_after_the_last_uid_is_read_and_a_changed_uidvalidity_starts_again(tmp_path):
    r, m = Route(), Model(reply(False), reply(False), reply(False))
    r.add(1, raw(subject="One"))
    go(tmp_path, r, m)
    r.add(2, raw(subject="Two"))
    rec = go(tmp_path, r, m, now=NOW.replace(hour=6))
    assert rec["threads_seen"] == 1 and r.labels[-1][0] == "a:7-2"
    r.uv = 8  # the folder was rebuilt: its uids mean something else now
    rec = go(tmp_path, r, m, now=NOW.replace(hour=7))
    assert rec["threads_seen"] == 2 and {"mailbox": "a", "kind": "uidvalidity_changed"} in rec["notes"]
    assert rec["errors"] == []  # a rebuilt folder is a note, not a failed run


def test_the_search_goes_back_a_day_from_the_last_run_and_the_first_run_by_the_window(tmp_path):
    r, m = Route(), Model()
    go(tmp_path, r, m)
    go(tmp_path, r, m, now=NOW.replace(day=25))
    first, second = (s[0] for s in r.searches)
    today = NOW.astimezone().date()
    assert first == today - timedelta(days=1)  # the first run: the window
    assert second == today  # the mailbox was empty, so there is nothing from the day before to find


def test_a_first_run_window_of_zero_remembers_where_the_mailbox_is_and_reads_nothing(tmp_path):
    r, m = Route(), Model()
    r.add(1, raw())
    r.add(2, raw(subject="Two"))
    cfg = _replace(config(tmp_path), first_run_days=0)
    rec = go(tmp_path, r, m, cfg=cfg)
    assert rec["threads_seen"] == 0 and r.labels == [] and m.calls == []
    r.add(3, raw(subject="Three"))
    m.answers = [reply(False)]
    rec = go(tmp_path, r, m, cfg=cfg, now=NOW.replace(hour=6))
    assert rec["threads_seen"] == 1 and r.labels == [("a:7-3", "ws/fyi")]


def test_a_search_query_reaches_the_mailbox_so_what_is_not_ours_is_never_opened(tmp_path):
    r = Route()
    cfg = _replace(config(tmp_path), query={"a": "-from:work.example"})
    go(tmp_path, r, Model(), cfg=cfg)
    assert r.searches[0][1] == "-from:work.example"


# ---------------------------------------------------------------- caps, failures, boundaries


def test_a_cap_holds_the_rest_for_the_next_run_and_nothing_is_called_twice(tmp_path):
    r = Route()
    for i in (1, 2, 3):
        r.add(i, raw(subject=f"Subject {i}", body=f"Body {i}?", mid=f"<{i}@example.org>"))
    m = Model(reply(False), reply(False), reply(False))
    cfg = _replace(config(tmp_path), model={"max_calls": 2})
    rec = go(tmp_path, r, m, cfg=cfg)
    assert rec["model_calls"] == 2 and rec["deferred_threads"] == 1 and len(r.labels) == 2
    assert json.loads((tmp_path / "state" / "inbox.json").read_text())["mailboxes"]["a"]["pending"] == [3]
    rec = go(tmp_path, r, m, cfg=cfg, now=NOW.replace(hour=6))
    assert rec["threads_seen"] == 1 and rec["model_calls"] == 1 and len(r.labels) == 3 and len(m.calls) == 3
    assert json.loads((tmp_path / "state" / "inbox.json").read_text())["mailboxes"]["a"]["pending"] == []


@pytest.mark.parametrize(
    ("model", "why"),
    [({"run_tokens": 100}, "run_tokens"), ({"budget_usd": 0.001}, "budget_usd")],
)
def test_each_cap_of_a_run_stops_further_calls(tmp_path, model, why):
    r = Route()
    r.add(1, raw(subject="One", mid="<1@example.org>"))
    r.add(2, raw(subject="Two", mid="<2@example.org>"))
    m = Model(reply(False), reply(False))
    rec = go(tmp_path, r, m, cfg=_replace(config(tmp_path), model=model))
    assert len(m.calls) == 1 and rec["deferred_threads"] == 1


def test_a_failed_runner_stops_the_calls_and_holds_the_threads(tmp_path):
    r, m = Route(), Model(ok=False)
    r.add(1, raw(subject="One", mid="<1@example.org>"))
    r.add(2, raw(subject="Two", mid="<2@example.org>"))
    rec = go(tmp_path, r, m)
    assert len(m.calls) == 1 and rec["deferred_threads"] == 2 and r.labels == [] and r.drafts == []
    assert json.loads((tmp_path / "state" / "inbox.json").read_text())["mailboxes"]["a"]["pending"] == [1, 2]


def test_a_label_the_broker_refuses_stops_the_mailbox_and_the_message_is_tried_again(tmp_path):
    r = Route()
    r.add(1, raw(List_Unsubscribe="<u>"))
    r.fail_label = True
    rec = go(tmp_path, r, Model())
    assert rec["errors"] == [{"mailbox": "a", "kind": "broker_unavailable"}]
    assert json.loads((tmp_path / "state" / "inbox.json").read_text())["mailboxes"]["a"]["last_uid"] == 0
    r.fail_label = False
    rec = go(tmp_path, r, Model(), now=NOW.replace(hour=6))
    assert rec["rule_labelled"] == 1 and r.labels == [("a:7-1", "ws/low")]


def test_more_mail_than_one_search_page_is_reported_not_hidden(tmp_path):
    r = Route()
    r.total_extra = 30
    r.add(1, raw(List_Unsubscribe="<u>"))
    rec = go(tmp_path, r, Model())
    assert {"mailbox": "a", "kind": "more_than_one_search_page", "total": 31} in rec["notes"] and rec["errors"] == []


def test_the_draft_limit_of_a_run_flags_the_rest(tmp_path):
    r = Route()
    for i in (1, 2, 3):
        r.add(i, raw(subject=f"S{i}", mid=f"<{i}@example.org>"))
    m = Model(*[reply(True, "r", "Kia ora,\nThanks for this. I will look. Cheers, Alex")] * 3)
    rec = go(tmp_path, r, m, cfg=_replace(config(tmp_path), max_drafts=2))
    assert rec["drafts"] == 2 and any("draft limit" in f["why"] for f in rec["flagged"])


def test_a_mailbox_in_a_class_without_full_access_is_refused_before_any_mail_is_read():
    def pol(access):
        return (
            policy.loads(f'[classes.work]\naccess = "{access}"\nrepos = ["~/code/work"]\nowner = "me"\n')
            if access
            else policy.default_policy()
        )

    cfg = triage.Config("j", ("a",), {}, {"a": "work"}, Path("r"), "p", "", None, 1, 5, triage.ModelConfig())
    for access in ("walled", "counts-only", None):
        with pytest.raises(triage.TriageError) as e:
            triage.refuse_walled(cfg, pol(access))
        assert e.value.exit_code == triage.EXIT_REFUSED and "'a'" in e.value.message
    triage.refuse_walled(_replace(cfg, classes={}), pol(None))  # no class named: nothing to refuse


# ---------------------------------------------------------------- settings


def test_settings_are_checked_and_the_model_is_config(tmp_path):
    cfg = config(tmp_path)
    assert (cfg.model.runner, cfg.model.model, cfg.max_drafts, cfg.first_run_days) == ("claude-code", "haiku", 15, 1)
    prompt, rules = tmp_path / "prompt.md", tmp_path / "rules.toml"
    base = {"mailboxes": ["a"], "rules": str(rules), "prompt_file": str(prompt)}
    env = {"HOME": str(tmp_path)}
    local = triage.parse_config({**base, "model": {"runner": "opencode", "model": "ollama/x"}}, "j", tmp_path, env)
    assert local.model.runner == "opencode" and local.model.model == "ollama/x"
    api = {"runner": "opencode", "model": "p/m", "secrets": [{"name": "key-a", "host": "API.Example.org"}]}
    got = triage.parse_config({**base, "model": api}, "j", tmp_path, env)
    assert got.model.secrets == (jobs.SecretUse("key-a", "api.example.org", "run"),)
    for bad, match in (
        ({**base, "mailboxes": []}, "mailboxes must be"),
        ({**base, "mailboxes": ["a", "a"]}, "listed twice"),
        ({**base, "mailbox": {"b": {}}}, "unknown key 'b'"),
        ({**base, "mailbox": {"a": {"query": "xé"}}}, "printable ASCII"),
        ({**base, "mailbox": {"a": {"folder": "x"}}}, "takes query and class only"),
        ({**base, "model": {"budget_usd": 0}}, "budget_usd"),
        ({**base, "model": {"max_calls": 1.5}}, "max_calls"),
        ({**base, "model": {"who": 1}}, "unknown key 'who'"),
        ({**base, "model": {"secrets": [{"name": "k"}]}}, "a secret needs a secret name and a host"),
        ({**base, "model": {"secrets": [{"name": "k", "host": "https://x.example", "extra": 1}]}}, "secrets must be"),
        ({**base, "nope": 1}, "unknown key 'nope'"),
        ({"mailboxes": ["a"]}, "rules and prompt_file are required"),
        ({**base, "prompt_file": str(tmp_path / "missing.md")}, "cannot be read"),
        ({**base, "voice": "~/missing.md"}, "voice"),
    ):
        with pytest.raises(triage.TriageError, match=match):
            triage.parse_config(bad, "j", tmp_path, env)


# ---------------------------------------------------------------- the runner path and the mail route


def test_the_model_call_is_a_ws_run_of_an_in_memory_job_and_its_answer_is_read_back(tmp_path, monkeypatch):
    env = {"WS_HOME": str(tmp_path / "wsh"), "HOME": str(tmp_path)}
    monkeypatch.setenv("WS_HOME", env["WS_HOME"])
    seen = {}

    def fake_run_job(args, job, fields=None, hook=None, queued=None):
        seen["job"], seen["input"] = job, (Path(job.cwd) / "thread.md").read_text()
        out = paths.state_dir(env) / "runs" / "RID"
        out.mkdir(parents=True)
        (out / "stdout.txt").write_text(json.dumps({"type": "result", "result": '{"needs_reply": false}'}))
        hook(
            {"run_id": "RID", "status": "ok", "tokens": {"input": 10, "cache_read": 90, "output": 7}, "cost_usd": 0.002}
        )
        print("ws: run RID ok")  # the run's own report must not reach our output
        return 0

    monkeypatch.setattr(run, "run_job", fake_run_job)
    cfg = _replace(config(tmp_path), model={"runner_options": {}})
    got = triage.RunnerModel(cfg, tmp_path / "work", env)("Be brief.", "THE THREAD", 0.05)
    assert (
        got.ok
        and got.text == '{"needs_reply": false}'
        and (got.tokens_in, got.tokens_out, got.cost_usd) == (100, 7, 0.002)
    )
    j = seen["job"]
    assert (j.runner, j.model, j.prompt, j.input, j.max_turns, j.bare, j.budget_usd) == (
        "claude-code",
        "haiku",
        "Be brief.",
        "thread.md",
        1,
        False,  # Claude Code's --bare ignores the subscription login, so a run with no key is not bare
        0.05,
    )
    assert j.sources == ("mail",) and j.name == "inbox-model" and seen["input"] == "THE THREAD" and j.secrets == ()
    assert oct((tmp_path / "work" / "thread.md").stat().st_mode & 0o777) == "0o600"


def test_a_model_run_that_did_not_end_ok_is_an_error_with_the_status_not_the_text(tmp_path, monkeypatch):
    def fake_run_job(args, job, fields=None, hook=None, queued=None):
        hook({"run_id": "R2", "status": "refused", "stop_reason": "refused:cap", "tokens": {}, "cost_usd": None})
        return 3

    monkeypatch.setattr(run, "run_job", fake_run_job)
    got = triage.RunnerModel(config(tmp_path), tmp_path / "work", {"WS_HOME": str(tmp_path)})("i", "d", 0.05)
    assert not got.ok and got.error == "the model run ended refused (refused:cap)" and got.cost_usd == 0.0


def test_a_claude_code_run_with_no_key_is_not_bare_because_bare_ignores_the_subscription_login():
    base = triage.ModelConfig()
    key = (jobs.SecretUse("k", "api.example.org"),)
    assert triage.is_bare(base) is False  # claude-code, no key: the subscription login
    assert triage.is_bare(dataclasses.replace(base, secrets=key)) is True  # a key is leased: bare is fine
    assert triage.is_bare(dataclasses.replace(base, runner="opencode")) is True
    assert triage.is_bare(dataclasses.replace(base, bare=True)) is True  # the person's word wins
    assert triage.is_bare(dataclasses.replace(base, secrets=key, bare=False)) is False


def test_answer_text_reads_each_runners_output():
    assert triage.answer_text("claude-code", json.dumps({"type": "result", "result": "hi"})) == "hi"
    assert triage.answer_text("claude-code", "not json") == ""
    stream = "\n".join(
        json.dumps(e)
        for e in ({"type": "text", "part": {"text": "a"}}, {"type": "text", "part": {"text": "b"}}, {"type": "x"})
    )
    assert triage.answer_text("opencode", stream) == "ab"


class FakeMail(mail.Mail):
    """ws.mail.Mail with the broker call replaced: search, fetch, label and draft are answered from a table."""

    def __init__(self):
        super().__init__({"a": mail.Mailbox("a", "mail-a", "gmail")})
        self.sent: list[tuple[str, dict]] = []

    def _call(self, box, cmd, args):
        self.sent.append((cmd, args))
        if cmd == "search":
            return {"uidvalidity": 7, "uids": [9, 4], "total": 2}
        if cmd == "fetch":
            return {"uidvalidity": 7, "uid": args["uid"], "raw": base64.b64encode(raw()).decode(), "thread": "T1"}
        return {}

    def _user(self, box):
        return USER


def test_the_mail_adapter_calls_ws_mail_and_reads_the_whole_message(monkeypatch):
    fm = FakeMail()
    a = triage.MailAdapter(fm, "a")
    assert a.search(date(2026, 11, 22), "-label:x", None) == (7, [9, 4], 2)
    assert fm.sent[0][1] == {"folder": "INBOX", "unseen": False, "query": "-label:x", "since": "2026-11-22"}
    a.search(date(2026, 11, 22), None, 3)
    assert fm.sent[1][1] == {"folder": "INBOX", "unseen": False, "after_uid": 3, "since": "2026-11-22"}
    got = a.fetch(a.ref(7, 9))
    assert got.ref == "a:7-9" and got.thread == "T1" and b"Subject: Hello" in got.raw and a.user() == USER
    a.label("a:7-9", "ws/low")
    assert fm.sent[-1] == ("label", {"folder": "INBOX", "uid": 9, "uidvalidity": 7, "label": "ws/low"})
    with pytest.raises(mail.MailError, match="not a ws label"):
        a.label("a:7-9", "ws/trash")  # ws.mail's own allowlist still stands behind this module


def test_a_message_reads_to_a_sender_a_subject_a_bulk_flag_and_a_thread_key():
    m = triage.read_message(
        "a", triage.Raw("a:7-5", raw(sender='"Sam T" <SAM@Example.org>', Precedence="bulk"), None, False), 5
    )
    assert (m.sender, m.name, m.subject, m.bulk, m.uid) == ("sam@example.org", "Sam T", "Hello", True, 5)
    assert m.thread.startswith("<") and "untrusted content" in m.text
    g = triage.read_message("a", triage.Raw("a:7-5", raw(), "T9", False), 5)
    assert g.thread == "T9"  # the provider's own thread id wins


# ---------------------------------------------------------------- the command


def write_job_config(tmp_path):
    (tmp_path / "prompt.md").write_text("Answer with the JSON object.", encoding="utf-8")
    (tmp_path / "rules.toml").write_text('[[rule]]\nid = "bulk"\nlabel = "ws/low"\nbulk = true\n', encoding="utf-8")
    cfg = tmp_path / "triage.toml"
    cfg.write_text('[triage]\nmailboxes = ["a"]\nrules = "rules.toml"\nprompt_file = "prompt.md"\n', encoding="utf-8")
    return cfg


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wsh"))
    return tmp_path


def args(**kw):
    return argparse.Namespace(**{"job": None, "config": None, "dry_run": True, "json": False, **kw})


def test_the_command_runs_a_config_file_through_the_fixture_route(home, monkeypatch, capsys):
    route = Route()
    route.add(1, raw(List_Unsubscribe="<u>"))
    monkeypatch.setattr(
        mail.Mail, "from_profile", classmethod(lambda cls, env=None, run=None: type("M", (), {"boxes": {"a": 1}})())
    )
    monkeypatch.setattr(triage, "MailAdapter", lambda client, name: route)
    no_model = lambda *a: pytest.fail("the only mail is bulk: no model call")
    monkeypatch.setattr(triage, "RunnerModel", lambda *a, **k: no_model)
    assert triage.cmd_run(args(config=str(write_job_config(home)))) == 0
    out = capsys.readouterr().out
    assert "(dry run)" in out and "1 new, 1 labelled by rule" in out and route.labels == []  # a dry run labels nothing


def test_the_command_says_what_is_wrong_with_the_settings_a_mailbox_or_the_job(home, monkeypatch):
    for bad in (args(config=str(home / "nope.toml")), args(job="nojob")):
        with pytest.raises(triage.TriageError) as e:
            triage.cmd_run(bad)
        assert e.value.exit_code == triage.EXIT_USAGE
    monkeypatch.setattr(
        mail.Mail, "from_profile", classmethod(lambda cls, env=None, run=None: type("M", (), {"boxes": {}})())
    )
    with pytest.raises(triage.TriageError, match="no mailbox 'a'") as e:
        triage.cmd_run(args(config=str(write_job_config(home))))
    assert e.value.kind == "unknown_mailbox"


def test_two_runs_of_one_job_do_not_overlap(home, monkeypatch, capsys):
    monkeypatch.setattr(
        mail.Mail, "from_profile", classmethod(lambda cls, env=None, run=None: type("M", (), {"boxes": {"a": 1}})())
    )
    cfg = write_job_config(home)
    with joblock.hold("triage-triage", dict(os.environ)) as lock:
        assert lock is not None
        assert triage.cmd_run(args(config=str(cfg))) == 0
    assert "already running" in capsys.readouterr().out


def test_triage_is_a_registered_command_that_needs_a_job_or_a_config(capsys):
    from ws import cli

    assert cli.main(["triage", "run"]) == 2
    assert "one of the arguments --job --config is required" in capsys.readouterr().err


# ---------------------------------------------------------------- review fixes (C701a)


class Killed(BaseException):
    """What a timeout's kill looks like from inside: the run stops where it is."""


def test_a_run_killed_after_one_draft_never_drafts_that_mail_again(tmp_path):
    r = Route()
    for i in (1, 2):
        r.add(i, raw(subject=f"S{i}", mid=f"<{i}@example.org>"))
    saved = []

    def draft(ref, body):
        if saved:
            raise Killed
        saved.append(ref)

    r.draft = draft
    good = reply(True, "r", "Kia ora,\nThanks for this. I will look. Cheers, Alex")
    with pytest.raises(Killed):
        go(tmp_path, r, Model(good, good))
    assert saved == ["a:7-1"]
    r.draft = lambda ref, body: saved.append(ref)
    rec = go(tmp_path, r, Model(good, good), now=NOW.replace(hour=6))
    assert saved == ["a:7-1", "a:7-2"] and rec["drafts"] == 1


def test_a_label_that_fails_on_a_model_thread_is_tried_again_next_run(tmp_path):
    r = Route()
    r.add(1, raw())
    r.fail_label = True
    rec = go(tmp_path, r, Model(reply(False)))
    assert rec["errors"] == [{"mailbox": "a", "kind": "broker_unavailable"}] and r.labels == []
    r.fail_label = False
    m = Model(reply(False))
    go(tmp_path, r, m, now=NOW.replace(hour=6))
    assert len(m.calls) == 1 and r.labels == [("a:7-1", triage.LABEL_FYI)]


def test_mail_past_one_search_page_is_read_on_the_next_run_not_skipped(tmp_path):
    r = Route()
    n = imap.MAX_RESULTS + 10
    for i in range(1, n + 1):
        r.add(i, raw(List_Unsubscribe="<u>", subject=f"S{i}"))
    first = go(tmp_path, r, Model())
    assert first["rule_labelled"] == imap.MAX_RESULTS and r.labels[0] == ("a:7-1", "ws/low")
    st = json.loads((tmp_path / "state" / "inbox.json").read_text())["mailboxes"]["a"]
    assert st["last_uid"] == imap.MAX_RESULTS and "since" in st
    go(tmp_path, r, Model(), now=NOW + timedelta(days=3))  # the window stays put until the older mail is read
    assert sorted(int(ref.split("-")[1]) for ref, _ in r.labels) == list(range(1, n + 1))
    assert r.searches[-1][0] == r.searches[0][0]
    assert "since" not in json.loads((tmp_path / "state" / "inbox.json").read_text())["mailboxes"]["a"]


def test_a_mailbox_with_no_class_is_refused_once_the_policy_has_classes():
    pol = policy.loads('[classes.home]\naccess = "full"\nrepos = ["~/code/home"]\nowner = "me"\n')
    cfg = triage.Config("j", ("a", "b"), {}, {"a": "home"}, Path("r"), "p", "", None, 1, 5, triage.ModelConfig())
    with pytest.raises(triage.TriageError) as e:
        triage.refuse_walled(cfg, pol)
    assert e.value.exit_code == triage.EXIT_REFUSED and "'b'" in e.value.message
    triage.refuse_walled(_replace(cfg, classes={"a": "home", "b": "home"}), pol)
    triage.refuse_walled(_replace(cfg, classes={}), policy.default_policy())  # no classes table: as before


# ---------------------------------------------------------------- the summary names the threads that matter (C701)

ASK = "Kia ora Sam,\nThanks, I will look at it. Cheers, Alex"
CHECK_RULES = triage.parse_rules(
    '[[rule]]\nid = "security"\nlabel = "check"\nsubject = "security alert"\n'
    '[[rule]]\nid = "bulk"\nlabel = "ws/low"\nbulk = true\n'
)


def summary(tmp_path, now=NOW) -> str:
    return (tmp_path / "ctx" / f"summary-{now.astimezone():%Y-%m-%d}.md").read_text()


def test_the_second_run_of_a_day_keeps_the_first_runs_needs_reply_line(tmp_path):
    r = Route()
    r.add(1, raw(sender="Sam T <sam@example.org>", subject="Quote for the deck", mid="<q1@example.org>"))
    go(tmp_path, r, Model(reply(True, "asks for a quote by Friday", ASK)))
    r.add(2, raw(sender="Kim <kim@example.org>", subject="Photos", mid="<p1@example.org>"))
    go(tmp_path, r, Model(reply(False)), now=NOW.replace(hour=6))
    text = summary(tmp_path, NOW.replace(hour=6))
    assert "Needs a reply:\n- a: Sam T, “Quote for the deck”: asks for a quote by Friday. Draft saved.\n" in text
    assert "2 threads" in text and "Drafts created: 1" in text


def test_a_thread_seen_again_is_one_line_and_one_the_owner_answered_is_marked_done(tmp_path):
    r = Route()
    r.add(1, raw(subject="Quote", mid="<q1@example.org>"))
    go(tmp_path, r, Model(reply(True, "asks for a quote", ASK)))
    r.add(
        2, raw(sender="Me <me@example.com>", subject="Re: Quote", mid="<q2@example.org>", References="<q1@example.org>")
    )
    go(tmp_path, r, Model(), now=NOW.replace(hour=6))
    text = summary(tmp_path, NOW.replace(hour=6))
    assert "Needs a reply:" not in text and text.count("Quote") == 1
    assert "\nDone:\n- a: Sam T, “Quote”: asks for a quote. Draft saved." in text
    r.add(3, raw(subject="Re: Quote", mid="<q3@example.org>", References="<q1@example.org>"))  # he writes back again
    go(tmp_path, r, Model(reply(True, "now asks for a price", ASK)), now=NOW.replace(hour=7))
    text = summary(tmp_path, NOW.replace(hour=7))
    assert "Done:" not in text and text.count("Quote") == 1 and "now asks for a price" in text


def test_a_check_rule_gives_a_summary_line_adds_fyi_and_makes_no_model_call(tmp_path):
    r = Route()
    r.add(1, raw(sender="Google <no-reply@accounts.google.com>", subject="Security alert", mid="<s1@example.org>"))
    m = Model()
    rec = go(tmp_path, r, m, rules=CHECK_RULES)
    assert m.calls == [] and rec["model_calls"] == 0 and r.labels == [("a:7-1", "ws/fyi")] and r.drafts == []
    assert rec["labels"] == {"ws/fyi": 1} and rec["decisions"][0]["rule"] == "security"
    assert "Worth a look:\n- a: Google, “Security alert”: matched rule security.\n" in summary(tmp_path)


def test_the_summary_has_no_tokens_or_money_and_the_run_log_keeps_them(tmp_path):
    r = Route()
    r.add(1, raw())
    go(tmp_path, r, Model(reply(True, "asks", ASK)))
    text = summary(tmp_path)
    assert not any(w in text.lower() for w in ("token", "us$", "cost", "model", "ws/", "rules decided"))
    (line,) = [json.loads(x) for x in (tmp_path / "logs" / "triage.jsonl").read_text().splitlines()]
    assert line["tokens_in"] == 100 and line["cost_usd"] == 0.001 and line["model_calls"] == 1
    day = json.loads((tmp_path / "state" / "inbox.json").read_text())["day"]
    assert not {"tokens_in", "tokens_out", "cost_usd", "model_calls"} & set(day)


def test_a_subject_or_reason_with_a_link_or_a_control_character_is_written_cleanly(tmp_path):
    r = Route()
    evil = (
        "Invoice [click here](https://evil.example/x) <https://evil.example/y> \u202eexe.pdf\u0007 www.evil.example/z"
    )
    r.add(1, raw(sender="Pat \u202eX <pat@example.org>", subject=evil, mid="<e1@example.org>"))
    r.add(2, raw(subject="x" * 300, mid="<e2@example.org>"))
    reason = "see [the plan](http://evil.example)\u200b and `run it` ![i](http://evil.example/i.png) " + "y" * 200
    go(tmp_path, r, Model(reply(True, "okay", ASK), reply(True, reason[:200], ASK)))
    text = summary(tmp_path)
    assert "evil.example" not in text and "](" not in text and "http" not in text
    assert not any(ch in text for ch in "\u202e\u0007\u200b`<>")
    assert "Invoice click here" in text and "see the plan" in text
    lines = [x for x in text.splitlines() if x.startswith("- ")]
    assert len(lines) == 2 and all(len(x) < 300 for x in lines)
    assert "x" * 81 not in text and "…" in text  # a subject is cut to about 80 characters


def test_no_body_text_reaches_the_summary(tmp_path):
    r = Route()
    r.add(1, raw(subject="Plans", body="The secret passphrase is plum-orchard-42.", mid="<b1@example.org>"))
    go(tmp_path, r, Model(reply(True, "asks about plans", ASK)))
    assert "plum-orchard" not in summary(tmp_path)


def test_a_dry_run_carries_the_summary_it_would_write_and_writes_nothing(tmp_path):
    r = Route()
    r.add(1, raw(sender="Google <no-reply@accounts.google.com>", subject="Security alert", mid="<s1@example.org>"))
    rec = go(tmp_path, r, Model(), rules=CHECK_RULES, dry=True)
    assert rec["summary"].startswith("Inbox triage ") and "matched rule security" in rec["summary"]
    assert not (tmp_path / "ctx").exists() and not (tmp_path / "state").exists()


def test_the_days_lines_are_read_tolerantly_deduped_and_capped_keeping_the_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(triage, "MAX_ITEMS", 20)
    f = tmp_path / "state" / "inbox.json"
    f.parent.mkdir()
    day = f"{NOW.astimezone():%Y-%m-%d}"
    old = {"key": "a|t0", "mailbox": "a", "kind": "check", "uid": 0, "who": "Old", "subject": "Kept", "why": "w"}
    f.write_text(json.dumps({"schema": 1, "mailboxes": {}, "day": {
        "date": day, "threads": 3, "labels": "junk", "drafts": "x", "flags": ["Sam, “Quote”: scheduling", 7],
        "items": [old, old, "junk", {"no": "key"}, {"key": 5}]}}))  # fmt: skip
    r = Route()
    for i in range(1, 31):
        r.add(i, raw(subject=f"S{i}", mid=f"<t{i}@example.org>"))
    cfg = _replace(config(tmp_path), max_drafts=500, model={"max_calls": 500, "run_tokens": 10**9, "budget_usd": 100.0})
    go(tmp_path, r, Model(*[reply(True, "r", ASK)] * 200), cfg=cfg)
    st = json.loads(f.read_text())["day"]
    keys = [i["key"] for i in st["items"]]
    assert len(keys) == len(set(keys)) == triage.MAX_ITEMS and keys[0] == "text|Sam, “Quote”: scheduling"
    assert (
        "a|t0" in keys
        and st["more"] > 0
        and "S1" in summary(tmp_path)
        and f"And {st['more']} more" in summary(tmp_path)
    )


def test_the_rules_file_accepts_check_and_the_default_may_be_check():
    assert triage.parse_rules('[[rule]]\nid = "x"\nlabel = "check"\nsubject = "a"\n').rules[0].label == "check"
    assert triage.parse_rules('default = "check"\n').default == "check"
    assert triage.ws_label("check") == "ws/fyi" and triage.ws_label("ws/low") == "ws/low"
