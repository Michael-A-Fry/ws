"""Notify role, ntfy: `alert_on` decides which session events alert. "needs-you" alerts on a Notification and on a Stop
whose last message asks something; "every-stop" (and no key) alerts on every Stop. The sender is a fake."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ws.roles import ntfy

URL = "https://ntfy.example.org/my-topic"
CFG = {"adapter": "ntfy", "url": URL}
NEEDS = {**CFG, "alert_on": "needs-you"}
NOON = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


class Sender:
    def __init__(self):
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((body, headers))
        return 200


def stop(message=None, **extra):
    payload = {"hook_event_name": "Stop", "cwd": "/work/payroll-app", **extra}
    if message is not None:
        payload["last_assistant_message"] = message
    return payload


def sent(cfg, env, payload):
    s = Sender()
    ntfy.alert(cfg, env, payload, "claude-code", now=NOON, sender=s)
    return s.calls


QUESTIONS = [
    "Done with the migration. Should I run it on the staging copy too?",
    "Which table do you want first?",
    "Shall I go ahead?\n",
    "Shall I go ahead?   ",
    "Do you want me to continue **with the second batch**?",
    'Is "orders" the right name?"',
    "Ready to merge (want me to open the PR?)",
    "Is that right？",
    "All three options are in the plan.\n\nWhich one do you want?",
    "I found two ways to fix it. Pick one and I'll carry on.",
    "Two options: a flat file or a table. Your call.",
    "Both work. It's up to you.",
    "I need your decision before I touch the schema.",
    "The change is staged.\n\nReply with yes to apply it.",
    "Let me know which branch to use.",
    "Choose A or B.",
    "Needs your approval.",
]
FINISHED = [
    "Done. All 214 tests pass.",
    "Finished the refactor and pushed the branch.",
    "Why it failed: the fixture reused a path? That is fixed now.\n\nAll green.",
    "I asked myself which approach was safer, then wrote the test first.\n\nDone.",
    "Let me know if you need anything else.",
    "The report is in docs/report.md.\n\n```\nwhat is this?\n```",
    "",
    "   \n",
]


@pytest.mark.parametrize("text", QUESTIONS)
def test_a_last_message_that_asks_something_needs_you(text):
    assert ntfy.asks(text), text


@pytest.mark.parametrize("text", FINISHED)
def test_a_plain_finish_asks_nothing(text):
    assert not ntfy.asks(text), text


def test_only_text_can_ask():
    assert not ntfy.asks(None) and not ntfy.asks(["Which one?"]) and not ntfy.asks({"text": "Which one?"})


def test_a_long_message_is_judged_by_its_end():
    assert ntfy.asks("x" * 100_000 + " Shall I go on?")
    assert not ntfy.asks("Shall I go on? " + "x" * 100_000)


# ---- the filter -----------------------------------------------------------------------------------------------------


def test_a_notification_always_needs_you(env):
    n = {"hook_event_name": "Notification", "cwd": "/work/payroll-app", "message": "Claude needs your permission"}
    [(body, headers)] = sent(NEEDS, env, n)
    assert body == b"payroll-app needs you" and headers["Priority"] == "high"


def test_a_notification_that_needs_nothing_is_quiet(env):
    n = {"hook_event_name": "Notification", "cwd": "/w/app", "notification_type": "auth_success"}
    assert sent(NEEDS, env, n) == []
    n["notification_type"] = "permission_prompt"
    assert len(sent(NEEDS, env, n)) == 1
    n["notification_type"] = "idle_prompt"
    assert len(sent(NEEDS, env, n)) == 1


def test_a_stop_that_ends_asking_alerts_as_a_call_to_you(env):
    [(body, headers)] = sent(NEEDS, env, stop("Tests pass. Should I open the PR?"))
    assert body == b"payroll-app needs you" and headers["Priority"] == "high"


def test_a_plain_finished_turn_sends_nothing(env):
    assert sent(NEEDS, env, stop("Done. All tests pass.")) == []
    assert ntfy.alert(NEEDS, env, stop("Done."), "claude-code", now=NOON, sender=Sender()) is None
    assert not ntfy.counter_path(env).exists()  # nothing sent, nothing counted


def test_a_stop_with_no_last_message_sends_nothing_under_needs_you(env):
    assert sent(NEEDS, env, stop()) == []
    assert sent(NEEDS, env, stop(None, transcript_path="/no/such/file.jsonl")) == []
    assert sent(NEEDS, env, stop(["Should I?"])) == []  # not text


def test_a_runner_event_with_no_message_does_not_ask(env):
    assert sent(NEEDS, env, {"event": "session.idle", "cwd": "/w/app"}) == []


def test_every_stop_is_the_behaviour_of_a_config_with_no_key(env):
    for cfg in (CFG, {**CFG, "alert_on": "every-stop"}):
        [(body, headers)] = sent(cfg, env, stop("Done. All tests pass."))
        assert body == b"payroll-app finished" and "Priority" not in headers
    assert len(sent(CFG, env, stop())) == 1


def test_a_question_under_every_stop_still_says_finished(env):
    [(body, _)] = sent({**CFG, "alert_on": "every-stop"}, env, stop("Should I go on?"))
    assert body == b"payroll-app finished"


def test_alert_on_is_checked(env):
    assert ntfy.alert_on(CFG) == "every-stop" and ntfy.alert_on(NEEDS) == "needs-you"
    for bad in ("always", "", "needs_you", 1, True, None, ["needs-you"]):
        with pytest.raises(ntfy.NotifyError, match="alert_on"):
            ntfy.alert_on({**CFG, "alert_on": bad})
    with pytest.raises(ntfy.NotifyError, match="alert_on"):
        ntfy.alert({**CFG, "alert_on": "always"}, env, stop("Done."), "claude-code", now=NOON, sender=Sender())


def test_session_alerts_false_still_sends_nothing(env):
    assert sent({**NEEDS, "session_alerts": False}, env, stop("Should I?")) == []


def test_a_headless_session_still_sends_nothing(env):
    assert sent(NEEDS, {**env, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, stop("Should I?")) == []


# ---- what the alert holds ----------------------------------------------------------------------------------------------


def test_the_session_text_never_reaches_the_alert(env):
    secret = "planted-" + "7" * 12
    question = f"The key is {secret}. Which one should I use?"
    for mode in ("title", "full", "plain"):
        [(body, headers)] = sent({**NEEDS, "alert_text": mode}, env, stop(question))
        assert secret.encode() not in body and b"Which one" not in body
        assert secret not in repr(headers)
    [(body, _)] = sent({**NEEDS, "alert_text": "plain"}, env, stop(question, cwd="/work/private-client"))
    assert body == b"a session needs you"  # plain still sends no folder name


def test_full_adds_the_runners_own_message_for_a_notification_only(env):
    n = {"hook_event_name": "Notification", "cwd": "/w/app", "message": "Claude needs your permission"}
    [(body, _)] = sent({**NEEDS, "alert_text": "full"}, env, n)
    assert body == b"app needs you: Claude needs your permission"
    [(body, _)] = sent({**NEEDS, "alert_text": "full"}, env, stop("Which one?", cwd="/w/app"))
    assert body == b"app needs you"  # a Stop has no runner message, and its last message is not sent


# ---- the last message from the transcript ------------------------------------------------------------------------------


def transcript(tmp_path, *entries, raw=""):
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries) + raw)
    return str(path)


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def test_a_transcript_gives_the_last_assistant_text_when_the_payload_has_none(env, tmp_path):
    path = transcript(
        tmp_path,
        user("go"),
        assistant({"type": "text", "text": "Which table first?"}),
        user("the orders table"),
        assistant({"type": "text", "text": "Which index do you want?"}, {"type": "tool_use", "name": "Bash"}),
        assistant({"type": "tool_use", "name": "Read", "input": {}}),
    )
    assert len(sent(NEEDS, env, stop(None, transcript_path=path))) == 1


def test_a_transcript_that_ends_with_a_plain_finish_sends_nothing(env, tmp_path):
    path = transcript(
        tmp_path,
        assistant({"type": "text", "text": "Which table first?"}),
        user("orders"),
        assistant({"type": "text", "text": "Done."}),
        raw="not json\n\n",
    )
    assert sent(NEEDS, env, stop(None, transcript_path=path)) == []


def test_the_payloads_message_wins_over_the_transcript(env, tmp_path):
    path = transcript(tmp_path, assistant({"type": "text", "text": "Which table first?"}))
    assert sent(NEEDS, env, stop("Done.", transcript_path=path)) == []


def test_a_transcript_message_given_as_plain_text_reads(env, tmp_path):
    path = transcript(tmp_path, {"message": {"role": "assistant", "content": "Shall I go on?"}})
    assert len(sent(NEEDS, env, stop(None, transcript_path=path))) == 1


def test_only_the_end_of_a_big_transcript_is_read(env, tmp_path):
    filler = [user("x" * 1000) for _ in range(400)]
    path = transcript(tmp_path, assistant({"type": "text", "text": "Early question?"}), *filler)
    assert ntfy._transcript_text(path) == ""  # the assistant line is further back than the tail read
    assert ntfy._transcript_text(str(tmp_path / "missing.jsonl")) == ""
    assert ntfy._transcript_text(str(tmp_path)) == ""  # a folder
    assert ntfy._transcript_text(None) == "" and ntfy._transcript_text("") == ""
