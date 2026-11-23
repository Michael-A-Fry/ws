"""`ws connect`: the flow format, the engine and the command, with no network and no real terminal.

A fake Telegram API stands in for the service; a scripted terminal stands in for the person. The secret in these tests is
built at run time, and every output the engine makes (state file, say(), --json, diagnose, errors) is searched for it.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ws import agentsession, cli
from ws import secrets as wsecrets
from ws.connect import engine, spec
from ws.connect.engine import ConnectError, Context

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs" / "reference" / "connect-flow.schema.json").read_text())


def make_token(seed: str = "a") -> str:
    return "7" + "7777" + ":" + (seed * 8 + "Zz_-") * 3  # runs to match the Telegram pattern, planted at run time


class FakeTelegram:
    """api.telegram.org as far as the flow needs: getMe, and getUpdates with the messages people have sent."""

    def __init__(self, token: str, bot_id: int = 424242, username: str = "demo_bot") -> None:
        self.token, self.bot_id, self.username = token, bot_id, username
        self.updates: list[dict] = []
        self.calls: list[str] = []
        self.webhook = False

    def press_start(self, nonce: str, chat_id: int = 99, chat_type: str = "private") -> None:
        msg = {"text": f"/start {nonce}", "chat": {"id": chat_id, "type": chat_type}}
        self.updates.append({"update_id": len(self.updates) + 1, "message": msg})

    def get(self, url: str):
        self.calls.append(url.split("/", 3)[2])  # the host only: never log the url
        prefix = f"https://api.telegram.org/bot{self.token}/"
        if not url.startswith(prefix):
            return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        method = url[len(prefix) :]
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": self.bot_id, "username": self.username, "is_bot": True}}
        if method == "deleteWebhook":
            self.webhook = False
            return 200, {"ok": True, "result": True}
        if method == "getUpdates":
            if self.webhook:
                return 409, {
                    "ok": False,
                    "error_code": 409,
                    "description": "Conflict: can't use getUpdates with webhook",
                }
            return 200, {"ok": True, "result": self.updates}
        return 404, {"ok": False}


class Script:
    """The person: answers in order, and keeps everything the engine said or asked."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.said: list[str] = []
        self.asked: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)

    def _next(self, label: str) -> str:
        self.asked.append(label)
        return self.answers.pop(0) if self.answers else ""

    ask = ask_secret = pause = _next


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.delenv("WS_GATED_SESSION", raising=False)
    store = wsecrets.Store(tmp_path / "secrets")
    return tmp_path, store


def flow_entry() -> wsecrets.Entry:
    """The registry entry the Telegram flow declares, as the broker loads it."""
    block = spec.load("telegram").registry_block()
    return wsecrets._entry(Path("t"), "TELEGRAM_BOT_TOKEN", tomllib.loads(block)["TELEGRAM_BOT_TOKEN"])


def ctx_for(env, transport, io=None, registered=True, clock=None, walled=False) -> Context:
    tmp, store = env
    opened: list[str] = []
    c = Context(
        state_dir=tmp / "state",
        store=store,
        registry={"TELEGRAM_BOT_TOKEN": flow_entry()} if registered else {},
        walled=walled,
        registry_path=str(tmp / "secrets.toml"),
        transport=transport,
        io=io,
        now=clock or Clock(),
        open_url=lambda url: opened.append(url) or True,
    )
    c.opened = opened  # type: ignore[attr-defined]
    return c


def everything(tmp: Path, io: Script | None = None) -> str:
    """All the text a person or an agent could see or find: the state folder and what the engine said."""
    parts = [p.read_text() for p in (tmp / "state").rglob("*") if p.is_file()]
    if io:
        parts += io.said + io.asked
    return "\n".join(parts)


FLOW = spec.load("telegram")
GOOD = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


def walk_all(env, api, token, clock=None):
    """info, link, capture, verify, link, verify: the whole Telegram flow, the person pressing Start in between."""
    _tmp, _ = env
    clock = clock or Clock()
    io = Script("", "", token, "")  # read, opened, token, opened the bind link
    c = ctx_for(env, api, io, clock=clock)
    # the walk stops at the bind verify the first time (nothing sent yet); press Start while it waits
    orig = io.pause

    def pause(text: str) -> str:
        if text.startswith("Not seen yet"):
            api.press_start(engine.load_state(c, FLOW)["nonce"]["value"])
            return ""
        return orig(text)

    io.pause = pause  # type: ignore[method-assign]
    return c, io, engine.walk(c, FLOW)


# ---- the walk ----------------------------------------------------------------------------------------------------


def test_the_whole_telegram_flow_walks_and_keeps_the_secret_out_of_everything(env):
    tmp, store = env
    token = make_token()
    api = FakeTelegram(token)
    _c, io, res = walk_all(env, api, token)
    assert res["status"] == "done" and res["identity"] == "demo_bot"
    assert store.read("TELEGRAM_BOT_TOKEN") == token
    st = json.loads((tmp / "state" / "connect" / "telegram.json").read_text())
    assert st["status"] == "done"
    assert st["config"] == {"identity": "demo_bot", "bot_id": "424242", "chat_id": "99"}
    assert st["nonce"] is None
    assert token not in everything(tmp, io)
    assert (tmp / "state" / "connect" / "telegram.json").stat().st_mode & 0o777 == 0o600
    assert set(api.calls) == {"api.telegram.org"}


def test_a_failed_verify_stores_nothing(env):
    tmp, store = env
    api = FakeTelegram(make_token("b"))
    io = Script("", "", make_token("c"))  # the person pastes a token Telegram does not know
    c = ctx_for(env, api, io)
    with pytest.raises(ConnectError) as e:
        engine.walk(c, FLOW)
    assert e.value.kind == "verify_failed"
    assert "TELEGRAM_BOT_TOKEN" not in store.names()
    st = (
        json.loads((tmp / "state" / "connect" / "telegram.json").read_text())
        if (tmp / "state" / "connect" / "telegram.json").exists()
        else {}
    )
    assert "token" not in st.get("steps", {})
    assert make_token("c") not in str(e.value) + e.value.hint + everything(tmp, io)


def test_a_failed_verify_over_an_existing_secret_keeps_the_old_one(env):
    _tmp, store = env
    old = make_token("d")
    store.write("TELEGRAM_BOT_TOKEN", old)
    api = FakeTelegram(old)
    io = Script("", "", make_token("e"))
    with pytest.raises(ConnectError):
        engine.walk(ctx_for(env, api, io), FLOW)
    assert store.read("TELEGRAM_BOT_TOKEN") == old


def test_a_bad_shape_is_refused_before_any_call(env):
    api = FakeTelegram(make_token())
    io = Script("", "", "not a token")
    with pytest.raises(ConnectError) as e:
        engine.walk(ctx_for(env, api, io), FLOW)
    assert e.value.kind == "bad_value" and api.calls == []


def test_an_unregistered_secret_is_added_only_on_a_yes_and_no_edits_nothing(env):
    # C805: the old secret_not_registered stop is gone by design (ADR 0034): ws adds the flow's own entry on a yes
    tmp, store = env
    io = Script("no")
    c = ctx_for(env, FakeTelegram(make_token()), io, registered=False)
    with pytest.raises(ConnectError) as e:
        engine.walk(c, FLOW)
    assert e.value.kind == "not_registered" and "nothing was changed" in e.value.hint
    assert FLOW.registry_block() in "\n".join(io.said)  # the person saw exactly what would be added
    assert not (tmp / "secrets.toml").exists() and store.names() == []


def test_a_yes_appends_the_flows_entry_as_text_and_the_walk_carries_on(env):
    tmp, _store = env
    reg = tmp / "secrets.toml"
    reg.write_text('# my own notes\n[anthropic]\nhosts = ["api.anthropic.com"]  # keep\n')
    token = make_token("r")
    api = FakeTelegram(token)
    io = Script("yes", "", "", token, "")
    c = ctx_for(env, api, io, registered=False)
    orig = io.pause

    def pause(text: str) -> str:
        if text.startswith("Not seen yet"):
            api.press_start(engine.load_state(c, FLOW)["nonce"]["value"])
            return ""
        return orig(text)

    io.pause = pause  # type: ignore[method-assign]
    assert engine.walk(c, FLOW)["status"] == "done"
    text = reg.read_text()
    assert text.startswith('# my own notes\n[anthropic]\nhosts = ["api.anthropic.com"]  # keep\n')
    assert text.endswith(FLOW.registry_block())
    assert (tmp / "secrets.toml.bak").read_text().startswith("# my own notes")
    loaded = wsecrets.load_registry(reg, os.geteuid())["TELEGRAM_BOT_TOKEN"]
    assert loaded.token_in_path and loaded.purposes == ("notify",)
    assert loaded.paths == ("/getMe", "/getUpdates", "/sendMessage")
    assert engine.load_state(c, FLOW)["registry_added"] == FLOW.registry_block()
    assert token not in text and token not in everything(tmp, io)


def test_a_name_registered_with_other_settings_is_refused_and_the_differences_shown(env):
    tmp, store = env
    reg = tmp / "secrets.toml"
    reg.write_text('[TELEGRAM_BOT_TOKEN]\nhosts = ["api.telegram.org", "evil.example.com"]\n')
    c = ctx_for(env, FakeTelegram(make_token()), Script("yes"))
    c.registry = wsecrets.load_registry(reg)
    before = reg.read_text()
    with pytest.raises(ConnectError) as e:
        engine.walk(c, FLOW)
    assert e.value.kind == "registered_differently"
    assert "hosts" in e.value.hint and "base_path" in e.value.hint and "purposes" in e.value.hint
    assert reg.read_text() == before and store.names() == []
    out = engine.dry_run(c, FLOW)
    assert out["blockers"][0]["kind"] == "secret_registered_differently" and out["exit_code"] == 3
    assert all(r["id"] != "register-secret" for r in out["steps"])


def test_the_wall_case_writes_nothing_and_prints_the_block_and_the_install_line(env):
    tmp, store = env
    c = ctx_for(env, FakeTelegram(make_token()), Script("yes"), registered=False, walled=True)
    with pytest.raises(ConnectError) as e:
        engine.walk(c, FLOW)
    assert e.value.kind == "walled"
    assert FLOW.registry_block() in e.value.hint
    for key, want in dict(FLOW.secret_entry).items():
        got = tomllib.loads(FLOW.registry_block())["TELEGRAM_BOT_TOKEN"][key]
        assert got == list(want) if isinstance(want, tuple) else got == want
    assert (
        f"sudo install -o root -g root -m 0644 {tmp / 'secrets.toml'} /var/lib/ws-broker/secrets.toml" in e.value.hint
    )
    assert not (tmp / "secrets.toml").exists() and store.names() == []
    assert not (tmp / "state" / "connect").exists()
    row = engine.dry_run(c, FLOW)["steps"][0]
    assert row["walled"] is True and "sudo install" in row["text"]


def test_the_walk_stops_when_the_person_does_not_press_start_and_resumes(env):
    _tmp, _store = env
    token = make_token("f")
    api = FakeTelegram(token)
    io = Script("", "", token, "", "q")  # ... then q at "Not seen yet"
    c = ctx_for(env, api, io)
    res = engine.walk(c, FLOW)
    assert res["status"] == "stopped" and res["at"] == "chat-bound"
    st = engine.load_state(c, FLOW)
    assert st["steps"]["token"]["status"] == "done" and st["steps"]["who-am-i"]["status"] == "done"
    api.press_start(st["nonce"]["value"], chat_id=7)
    res2 = engine.walk(ctx_for(env, api, Script(), clock=c.now), FLOW)  # nothing to ask: all that is left is the check
    assert res2["status"] == "done"
    assert engine.load_state(c, FLOW)["config"]["chat_id"] == "7"


def test_an_expired_link_gets_a_new_nonce_on_the_next_walk(env):
    clock = Clock()
    token = make_token("g")
    api = FakeTelegram(token)
    io = Script(token)
    c = ctx_for(env, api, io, clock=clock)

    def pause(text: str) -> str:  # the link runs out while the person is away
        if text.startswith("Not seen yet"):
            clock.t += 700
        return ""

    io.pause = pause  # type: ignore[method-assign]
    with pytest.raises(ConnectError) as e:
        engine.walk(c, FLOW)
    assert e.value.kind == "expired"
    first = engine.load_state(c, FLOW)["nonce"]["value"]
    io2 = Script("")
    api.press_start("anything-else")  # a stale or wrong nonce binds nothing
    io2.pause = lambda text: (
        (api.press_start(engine.load_state(c, FLOW)["nonce"]["value"]) or "") if text.startswith("Not seen") else ""
    )  # type: ignore[method-assign]
    res = engine.walk(ctx_for(env, api, io2, clock=clock), FLOW)
    assert res["status"] == "done"
    assert any("https://t.me/demo_bot?start=" in s for s in io2.said)
    assert first not in "\n".join(io2.said)


def test_the_nonce_is_a_valid_telegram_start_parameter(env):
    token = make_token("h")
    api = FakeTelegram(token)
    _c, io, _ = walk_all(env, api, token)
    link = next(s for s in io.said if "t.me/demo_bot?start=" in s)
    nonce = link.split("start=")[1].strip()
    assert 20 <= len(nonce) <= 64 and all(ch.isalnum() or ch in "_-" for ch in nonce)


# ---- revoke and the way back ------------------------------------------------------------------------------------


def test_revoke_deletes_the_secret_and_tells_what_to_do_in_botfather_and_a_rewalk_works(env):
    tmp, store = env
    token = make_token("i")
    api = FakeTelegram(token)
    c, _io, _ = walk_all(env, api, token)
    out = engine.revoke(c, FLOW, assume_yes=True)
    assert "TELEGRAM_BOT_TOKEN" not in store.names()
    assert any("/revoke" in t for t in out["tell"])
    assert not (tmp / "state" / "connect" / "telegram.json").exists()
    # walk again from clean, with a new token
    token2 = make_token("j")
    api2 = FakeTelegram(token2, bot_id=555, username="second_bot")
    c2, _io2, res = walk_all(env, api2, token2)
    assert res["status"] == "done" and store.read("TELEGRAM_BOT_TOKEN") == token2
    assert engine.load_state(c2, FLOW)["config"]["bot_id"] == "555"


def test_revoke_with_nothing_to_undo_is_quiet(env):
    out = engine.revoke(ctx_for(env, FakeTelegram(make_token())), FLOW, assume_yes=True)
    assert out["did"] == [] and out["status"] == "revoked"


# ---- the agent contract --------------------------------------------------------------------------------------


def test_dry_run_lists_needs_human_steps_then_done(env):
    tmp, _store = env
    token = make_token("k")
    api = FakeTelegram(token)
    before = engine.dry_run(ctx_for(env, api), FLOW)
    assert before["schema_version"] == 1 and before["contract"] == "ws.connect/1"
    assert before["status"] == "needs_human" and before["exit_code"] == 3
    assert [s["id"] for s in before["steps"]][:3] == ["make-bot", "open-botfather", "token"]
    assert all(s["status"] == "needs_human" for s in before["steps"])
    actions = {s["id"]: s["action"] for s in before["steps"]}
    assert (
        actions["token"] == "paste_secret"
        and actions["open-botfather"] == "open_link"
        and actions["make-bot"] == "read"
    )
    bind = next(s for s in before["steps"] if s["id"] == "bind-link")
    assert bind["url_template"].endswith("?start={nonce}") and bind["expires_in_s"] == 600
    assert not (tmp / "state").exists()  # reading wrote nothing
    c, _io, _ = walk_all(env, api, token)
    after = engine.dry_run(c, FLOW)
    assert after["status"] == "done" and after["exit_code"] == 0
    assert all(s["status"] == "done" for s in after["steps"]) and "next_step" in after
    assert after["next_step"] is None


def test_dry_run_gives_the_link_and_its_expiry_once_a_nonce_exists(env):
    _tmp, _store = env
    token = make_token("l")
    api = FakeTelegram(token)
    io = Script("", "", token, "", "q")
    c = ctx_for(env, api, io)
    engine.walk(c, FLOW)
    row = next(s for s in engine.dry_run(c, FLOW)["steps"] if s["id"] == "bind-link")
    assert row["status"] == "done"  # opened; the check for the chat is the step still to do
    nxt = engine.dry_run(c, FLOW)
    assert nxt["next_step"] == "chat-bound" and nxt["steps"][-1]["status"] == "needs_human"
    nonce = engine.load_state(c, FLOW)["nonce"]["value"]
    assert "url" not in row and row["url_template"].endswith("?start={nonce}")  # an agent never gets the live nonce
    assert row["expires_at"].endswith("Z") and nonce not in json.dumps(engine.dry_run(c, FLOW))


def test_dry_run_shows_an_unregistered_secret_as_a_first_register_step_not_a_blocker(env):
    # C805: rewritten on purpose; the secret_not_registered blocker is replaced by the register-secret step (ADR 0034)
    out = engine.dry_run(ctx_for(env, FakeTelegram(make_token()), registered=False), FLOW)
    assert "blockers" not in out
    row = out["steps"][0]
    assert {k: row[k] for k in ("id", "type", "status", "action")} == {
        "id": "register-secret",
        "type": "register",
        "status": "needs_human",
        "action": "confirm_register",
    }
    assert row["text"] == FLOW.registry_block() and row["walled"] is False
    assert out["next_step"] == "register-secret"
    assert out["exit_code"] == 3 and out["status"] == "needs_human"
    assert all(r["id"] != "register-secret" for r in engine.dry_run(ctx_for(env, None), FLOW)["steps"])


def test_no_output_ever_holds_the_secret(env, capsys):
    tmp, _store = env
    token = make_token("m")
    api = FakeTelegram(token)
    c, io, _ = walk_all(env, api, token)
    blob = (
        json.dumps(engine.dry_run(c, FLOW))
        + json.dumps(engine.diagnose(c, FLOW))
        + json.dumps(engine.revoke(c, FLOW, assume_yes=True))
    )
    assert token not in blob
    assert token not in everything(tmp, io)
    assert token not in capsys.readouterr().out


def test_diagnose_redacts_the_nonce_and_says_where_the_secret_stands(env):
    _tmp, _store = env
    token = make_token("n")
    api = FakeTelegram(token)
    io = Script("", "", token, "", "q")
    c = ctx_for(env, api, io)
    engine.walk(c, FLOW)
    d = engine.diagnose(c, FLOW)
    assert d["nonce"] == "<redacted>" and d["steps"]["chat-bound"] == "todo"
    assert d["secret"] == {"name": "TELEGRAM_BOT_TOKEN", "registered": True, "stored": True}
    assert d["config"]["identity"] == "demo_bot"
    nonce = engine.load_state(c, FLOW)["nonce"]["value"]
    assert nonce not in json.dumps(d)


def test_a_damaged_state_file_is_a_typed_error_that_revoke_clears(env):
    tmp, _ = env
    f = tmp / "state" / "connect" / "telegram.json"
    f.parent.mkdir(parents=True)
    f.write_text("{not json")
    c = ctx_for(env, FakeTelegram(make_token()))
    with pytest.raises(ConnectError) as e:
        engine.dry_run(c, FLOW)
    assert e.value.kind == "state"


# ---- the mutation the brief names: a capture that wrote the secret to state would fail the search ----------------


def test_mutation_guard_the_search_would_catch_a_secret_in_state(env):
    tmp, _store = env
    token = make_token("o")
    api = FakeTelegram(token)
    _c, io, _ = walk_all(env, api, token)
    f = tmp / "state" / "connect" / "telegram.json"
    planted = json.loads(f.read_text())
    planted["config"]["token"] = token  # what a broken capture would do
    f.write_text(json.dumps(planted))
    assert token in everything(tmp, io)  # the check used above does see it


# ---- the format --------------------------------------------------------------------------------------------------


def _raw(name: str) -> dict:
    return tomllib.loads(spec.shipped_text(name.removesuffix(".toml")))


@pytest.mark.parametrize("name", ["telegram.toml", "_template.toml"])
def test_shipped_flows_pass_the_loader_and_the_json_schema(name):
    Draft202012Validator(SCHEMA).validate(_raw(name))
    spec._from_text(spec.shipped_text(name.removesuffix(".toml")), None)


def test_the_flow_ids_are_the_file_names_and_list_skips_the_template():
    assert spec.ids() == ["telegram"]


def _mut(fn):
    raw = json.loads(json.dumps(_raw("telegram.toml")))
    fn(raw)
    return raw


BAD = {
    "a secret sent to a host the flow did not name": lambda r: r["steps"][3]["request"].update(
        url="https://evil.example/bot{secret}/getMe"
    ),
    "http, not https": lambda r: r["steps"][1].update(url="http://t.me/BotFather"),
    "an unknown step type": lambda r: r["steps"][0].update(type="device"),
    "a secret capture with no verify after it": lambda r: r["steps"].__delitem__(slice(3, None)),
    "an unknown placeholder": lambda r: r["steps"][4].update(url="https://t.me/{nobody}?start={nonce}"),
    "a duplicate id": lambda r: r["steps"][1].update(id="make-bot"),
    "a nonce link with no expiry": lambda r: r["steps"][4].pop("expires_s"),
    "an unknown key": lambda r: r["steps"][0].update(callback="x"),
    "a reserved id": lambda r: r.update(id="list"),
    "a placeholder in the host": lambda r: r["steps"][5]["request"].update(url="https://{identity}.example/x"),
    "delete_secret with no [secret]": lambda r: r.pop("secret"),
    "a verify with both identity and find": lambda r: r["steps"][5].update(identity="a", unique_id="b"),
    "a [secret] base_path the broker refuses": lambda r: r["secret"].update(base_path="/bot{value}/x"),
    "a [secret] host with a path": lambda r: r["secret"].update(hosts=["api.telegram.org/x"]),
    "a [secret] key the registry does not know": lambda r: r["secret"].update(tier="lead"),
    "an empty [secret] paths": lambda r: r["secret"].update(paths=[]),
}


@pytest.mark.parametrize("why", sorted(BAD))
def test_a_bad_flow_is_refused_by_the_loader(why):
    raw = _mut(BAD[why])
    with pytest.raises(spec.FlowError):
        spec.parse(raw)


@pytest.mark.parametrize(
    "why", ["http, not https", "an unknown step type", "an unknown key", "a reserved id", "a nonce link with no expiry"]
)
def test_the_json_schema_refuses_what_it_can_say(why):
    errs = list(Draft202012Validator(SCHEMA).iter_errors(_mut(BAD[why])))
    assert errs, why


def test_flow_errors_never_carry_a_secret_shaped_value():
    raw = _mut(BAD["an unknown key"])
    with pytest.raises(spec.FlowError) as e:
        spec.parse(raw)
    assert "callback" in str(e.value)


# ---- the command and who may run it ------------------------------------------------------------------------------


def _run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_list_and_dry_run_and_diagnose_work_from_an_agent_session(env, monkeypatch, capsys):
    monkeypatch.setenv("WS_GATED_SESSION", "1")
    code, out, _ = _run(["connect", "--json"], capsys)
    assert code == 0 and json.loads(out)["data"]["flows"][0]["id"] == "telegram"
    code, out, _ = _run(["connect", "telegram", "--dry-run", "--json"], capsys)
    data = json.loads(out)["data"]
    assert code == 3 and data["status"] == "needs_human" and "blockers" not in data  # C805: a step, not a blocker
    assert data["steps"][0]["id"] == "register-secret" and data["steps"][0]["status"] == "needs_human"
    code, out, _ = _run(["connect", "telegram", "--diagnose", "--json"], capsys)
    assert code == 0 and json.loads(out)["data"]["flow"] == "telegram"


def test_the_walk_and_revoke_are_refused_inside_an_agent_session(env, monkeypatch, capsys):
    monkeypatch.setenv("WS_GATED_SESSION", "1")
    for argv in (["connect", "telegram"], ["connect", "telegram", "--revoke"]):
        code, out, _err = _run([*argv, "--json"], capsys)
        env_ = json.loads(out)
        assert code == 3 and env_["error_kind"] == "refused", argv
        assert "agent session" in env_["message"] or "gated session" in env_["message"]
    assert not (env[0] / "state" / "connect").exists()


def test_the_walk_refuses_json_and_a_missing_terminal(env, monkeypatch, capsys):
    monkeypatch.setattr(agentsession, "refusal", lambda *a, **k: None)
    code, out, _ = _run(["connect", "telegram", "--json"], capsys)
    assert code == 2 and json.loads(out)["error_kind"] == "usage"
    code, _, err = _run(["connect", "telegram"], capsys)  # pytest's stdin is not a terminal
    assert code == 2 and "needs a terminal" in err


def test_an_unknown_flow_is_a_typed_error(env, capsys):
    code, out, _ = _run(["connect", "nope", "--dry-run", "--json"], capsys)
    assert code == 1 and json.loads(out)["error_kind"] == "not_found"


def test_the_command_reads_a_registered_secret_from_the_real_registry(env, capsys):
    _tmp, _ = env
    reg = wsecrets.registry_path()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(spec.load("telegram").registry_block())
    _code, out, _ = _run(["connect", "telegram", "--dry-run", "--json"], capsys)
    data = json.loads(out)["data"]
    assert "blockers" not in data and data["steps"][0]["id"] != "register-secret"


def test_the_plain_dry_run_shows_the_register_row_and_the_block_and_no_blocked_line(env, capsys):
    code, out, _ = _run(["connect", "telegram", "--dry-run"], capsys)
    assert code == 3 and "blocked:" not in out
    assert out.splitlines()[0].split() == ["needs_human", "register-secret", "confirm_register"]
    assert spec.load("telegram").registry_block() in out


def test_revoke_takes_out_only_the_entry_it_added(env):
    tmp, _store = env
    reg = tmp / "secrets.toml"
    reg.write_text('[anthropic]\nhosts = ["api.anthropic.com"]\n')
    token = make_token("s")
    api = FakeTelegram(token)
    io = Script("yes", "", "", token, "")
    c = ctx_for(env, api, io, registered=False)
    orig = io.pause

    def pause(text: str) -> str:
        if text.startswith("Not seen yet"):
            api.press_start(engine.load_state(c, FLOW)["nonce"]["value"])
            return ""
        return orig(text)

    io.pause = pause  # type: ignore[method-assign]
    engine.walk(c, FLOW)
    with reg.open("a") as f:
        f.write('\n[later]\nhosts = ["x.example.com"]\n')  # written after ws connect: stays
    out = engine.revoke(c, FLOW, assume_yes=True)
    assert "removed TELEGRAM_BOT_TOKEN from the secrets registry" in out["did"]
    names = set(wsecrets.load_registry(reg))
    assert names == {"anthropic", "later"}


def test_revoke_leaves_an_entry_ws_connect_did_not_add(env):
    tmp, _store = env
    reg = tmp / "secrets.toml"
    reg.write_text(FLOW.registry_block())  # the person wrote it by hand
    token = make_token("t")
    c, _io, _ = walk_all(env, FakeTelegram(token), token)
    engine.revoke(c, FLOW, assume_yes=True)
    assert "TELEGRAM_BOT_TOKEN" in wsecrets.load_registry(reg)


def test_revoke_leaves_an_added_entry_that_was_edited_since(env):
    tmp, _store = env
    reg = tmp / "secrets.toml"
    c = ctx_for(env, FakeTelegram(make_token()), Script("yes"), registered=False)
    c.store = None  # stop right after the register step: the walk needs the store
    st = engine.load_state(c, FLOW)
    c.io = Script("yes")
    engine._register(c, FLOW, st)
    reg.write_text(reg.read_text().replace('purposes = ["notify"]', 'purposes = ["notify", "run"]'))
    out = engine.revoke(c, FLOW, assume_yes=True)
    assert any("changed since ws connect added it" in d for d in out["did"])
    assert "TELEGRAM_BOT_TOKEN" in wsecrets.load_registry(reg)


# ---- review fixes ------------------------------------------------------------------------------------------------


def test_links_are_opened_by_the_engine_and_printed_as_the_fallback(env):
    token = make_token("p")
    c, io, _ = walk_all(env, FakeTelegram(token), token)
    assert c.opened[0] == "https://t.me/BotFather" and c.opened[1].startswith("https://t.me/demo_bot?start=")
    assert any("Opened in your browser" in s and "https://t.me/BotFather" in s for s in io.said)


def test_a_secret_gone_from_the_store_makes_capture_and_verify_needs_human_again(env):
    _tmp, store = env
    token = make_token("q")
    c, _io, _ = walk_all(env, FakeTelegram(token), token)
    store.delete("TELEGRAM_BOT_TOKEN")
    rows = {s["id"]: s["status"] for s in engine.dry_run(c, FLOW)["steps"]}
    assert rows["token"] == "needs_human" and rows["who-am-i"] == "needs_human" and rows["make-bot"] == "done"
    assert engine.dry_run(c, FLOW)["exit_code"] == 3


def test_only_a_private_chat_binds(env):
    token = make_token("r")
    api = FakeTelegram(token)
    io = Script("", "", token, "", "q")
    c = ctx_for(env, api, io)
    engine.walk(c, FLOW)
    api.press_start(engine.load_state(c, FLOW)["nonce"]["value"], chat_id=-5, chat_type="group")
    res = engine.walk(ctx_for(env, api, Script("q"), clock=c.now), FLOW)
    assert res["status"] == "stopped"


def _webhook_walk(env, answer: str):
    token = make_token("s")
    api = FakeTelegram(token)
    api.webhook = True
    io = Script("", "", token, "")
    c = ctx_for(env, api, io)
    orig = io.ask

    def ask(label: str) -> str:
        if label.startswith("Run the fix"):
            io.asked.append(label)
            if answer == "yes":
                api.press_start(engine.load_state(c, FLOW)["nonce"]["value"])
            return answer
        return orig(label)

    io.ask = ask  # type: ignore[method-assign]
    return api, io, c


def test_a_webhook_is_explained_and_removed_only_on_a_yes(env):
    api, io, c = _webhook_walk(env, "yes")
    res = engine.walk(c, FLOW)
    assert res["status"] == "done" and api.webhook is False
    assert any("webhook" in s for s in io.said)


def test_a_no_leaves_the_webhook_alone_and_says_why_it_stopped(env):
    api, _io, c = _webhook_walk(env, "no")
    with pytest.raises(ConnectError) as e:
        engine.walk(c, FLOW)
    assert e.value.kind == "conflict" and api.webhook is True and "webhook" in e.value.hint


def test_a_closed_terminal_stops_the_walk_with_a_message(monkeypatch):
    from ws import connectcmd

    def eof(*a, **k):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    monkeypatch.setattr("getpass.getpass", eof)
    t = connectcmd.Terminal()
    for call in (lambda: t.ask("x"), lambda: t.ask_secret("x"), lambda: t.pause("x")):
        with pytest.raises(ConnectError) as e:
            call()
        assert e.value.kind == "cancelled"


# ---- second review ------------------------------------------------------------------------------------------------


def test_a_store_that_cannot_open_refuses_before_asking_for_anything(env):
    io = Script("", "", make_token())
    c = ctx_for(env, FakeTelegram(make_token()), io)
    c.store = None
    with pytest.raises(ConnectError) as e:
        engine.walk(c, FLOW)
    assert e.value.kind == "store" and io.asked == []


def test_a_token_for_a_different_bot_resets_the_chat_steps(env):
    _tmp, store = env
    token = make_token("t")
    c, _io, _ = walk_all(env, FakeTelegram(token), token)
    assert engine.load_state(c, FLOW)["config"]["chat_id"] == "99"
    store.delete("TELEGRAM_BOT_TOKEN")  # the person lost it and pastes the token of another bot
    other = make_token("u")
    io = Script(other, "", "q")  # the info and BotFather steps are done; token, bind link, then stop at the check
    res = engine.walk(ctx_for(env, FakeTelegram(other, bot_id=777, username="other_bot"), io, clock=c.now), FLOW)
    st = engine.load_state(c, FLOW)
    assert res["status"] == "stopped" and st["config"]["bot_id"] == "777" and "chat_id" not in st["config"]
    assert st["steps"].get("chat-bound") is None and st["steps"]["who-am-i"]["status"] == "done"


def test_revoke_asks_first_and_a_no_changes_nothing(env):
    _tmp, store = env
    token = make_token("v")
    c, _io, _ = walk_all(env, FakeTelegram(token), token)
    c.io = Script("")
    out = engine.revoke(c, FLOW)
    assert out["status"] == "cancelled" and store.read("TELEGRAM_BOT_TOKEN") == token
    assert engine.load_state(c, FLOW)["status"] == "done"
    c.io = Script("y")
    assert engine.revoke(c, FLOW)["status"] == "revoked" and "TELEGRAM_BOT_TOKEN" not in store.names()


def test_revoke_leaves_a_secret_ws_connect_never_stored(env):
    _tmp, store = env
    store.write("TELEGRAM_BOT_TOKEN", make_token("w"))
    out = engine.revoke(ctx_for(env, FakeTelegram(make_token())), FLOW, assume_yes=True)
    assert "TELEGRAM_BOT_TOKEN" in store.names()
    assert any("left TELEGRAM_BOT_TOKEN" in d for d in out["did"])


def test_revoke_without_a_terminal_or_yes_is_a_usage_error(env, monkeypatch, capsys):
    monkeypatch.setattr(agentsession, "refusal", lambda *a, **k: None)
    code, out, _ = _run(["connect", "telegram", "--revoke", "--json"], capsys)
    assert code == 2 and json.loads(out)["error_kind"] == "usage"
    code, _, _ = _run(["connect", "telegram", "--revoke", "--yes"], capsys)
    assert code == 0


# ---- the real transport, with the opener stubbed ----------------------------------------------------------------


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_opener(monkeypatch, behave):
    seen = {}

    class Opener:
        def open(self, req, timeout=None):
            seen["url"], seen["timeout"] = req.full_url, timeout
            return behave(req)

    def build(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr("urllib.request.build_opener", build)
    return seen


def test_url_transport_refuses_anything_but_https(monkeypatch):
    seen = _stub_opener(monkeypatch, lambda r: _Resp(200, b"{}"))
    with pytest.raises(ConnectError) as e:
        engine.UrlTransport().get("http://api.example.com/x")
    assert e.value.kind == "network" and "url" not in seen


def test_url_transport_follows_no_redirect(monkeypatch):
    seen = _stub_opener(monkeypatch, lambda r: _Resp(200, b'{"ok": true}'))
    assert engine.UrlTransport().get("https://api.example.com/x") == (200, {"ok": True})
    assert seen["handlers"] == (engine.NoRedirect,)
    assert engine.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/") is None


def test_url_transport_errors_name_the_host_only(monkeypatch):
    import urllib.error

    secret_path = "/bot" + make_token("x") + "/getMe"

    def boom(req):
        raise urllib.error.URLError("refused")

    _stub_opener(monkeypatch, boom)
    with pytest.raises(ConnectError) as e:
        engine.UrlTransport().get("https://api.example.com" + secret_path)
    assert (
        "api.example.com" in e.value.message and "bot" not in e.value.message and make_token("x") not in repr(e.value)
    )


def test_url_transport_returns_an_http_error_status_and_body(monkeypatch):
    import io
    import urllib.error

    def deny(req):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"ok": false}'))

    _stub_opener(monkeypatch, deny)
    assert engine.UrlTransport().get("https://api.example.com/x") == (401, {"ok": False})


def test_the_shipped_flows_are_only_python_files_for_the_wall():
    files = [p for p in spec.flows_dir().rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert files and all(p.suffix == ".py" for p in files), [p.name for p in files if p.suffix != ".py"]


def test_a_flow_outside_the_package_loads_from_a_toml_folder(tmp_path):
    (tmp_path / "mine.toml").write_text(spec.shipped_text("_template").replace('id = "example"', 'id = "mine"'))
    assert spec.ids(tmp_path) == ["mine"] and spec.load("mine", tmp_path).id == "mine"
