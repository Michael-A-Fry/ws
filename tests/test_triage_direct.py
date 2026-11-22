"""ws.triage's direct API model: one POST to a loopback stub standing in for the broker's lease, so no test reaches a
network, a broker or a key. The pipeline helpers (fixture mailbox, stub config) are test_triage's."""

from __future__ import annotations

import dataclasses
import http.server
import json
import threading

import pytest
from test_triage import Route, _replace, config, go, raw, reply

from ws import jobs, triage

PLACEHOLDER = "ws-lease-placeholder"
LEASE_TOKEN = "LEASE-TOKEN-NOT-A-KEY"
PRICES = {"max_tokens": 300, "price_in_usd_per_mtok": 1.0, "price_out_usd_per_mtok": 5.0}
DIRECT_MODEL = {
    "runner": "anthropic-api",
    "model": "small-model",
    "secrets": [{"name": "api-key", "host": "api.example.org"}],
    "runner_options": PRICES,
}


def message(text, tin=900, tout=60, **usage):
    return {
        "type": "message",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": tin, "output_tokens": tout, **usage},
    }


class StubApi:
    """A Messages API on a loopback port: records every request, answers with `body` (or a drafted reply)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status, self.body = 200, None
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                stub.requests.append(
                    {"path": self.path, "headers": dict(self.headers.items()), "body": json.loads(self.rfile.read(n))}
                )
                out = stub.body
                if out is None:
                    out = message(json.dumps(reply(True, "asks", "Kia ora Sam,\nI will send it soon. Cheers, Alex")))
                data = out if isinstance(out, bytes) else json.dumps(out).encode()
                self.send_response(stub.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/l/{LEASE_TOKEN}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    s = StubApi()
    yield s
    s.close()


def direct(tmp_path, api, base=None, lease=None, **model):
    """(the model, the secrets asked for, the leases released), the broker replaced by the stub's address."""
    fields = {
        "runner": "anthropic-api",
        "model": "small-model",
        "secrets": (jobs.SecretUse("api-key", "api.example.org"),),
        "runner_options": PRICES,
        **model,
    }
    cfg = _replace(config(tmp_path), model=fields)
    asked, released = [], []

    def default_lease(s):
        asked.append(s)
        return triage.broker.Lease("L1", base or api.base, "x-api-key", "")

    return triage.DirectModel(cfg, lease=lease or default_lease, release=released.append), asked, released


def test_the_direct_call_is_one_post_with_the_prompt_as_system_and_the_thread_as_the_one_message(tmp_path, api):
    m, asked, released = direct(tmp_path, api)
    got = m("Be brief.", "THE THREAD", 0.05)
    assert got.ok and got.text.startswith("{") and (got.tokens_in, got.tokens_out) == (900, 60)
    assert got.cost_usd == pytest.approx((900 * 1.0 + 60 * 5.0) / 1e6)
    (req,) = api.requests
    assert req["path"] == f"/l/{LEASE_TOKEN}/v1/messages"
    assert req["body"] == {
        "model": "small-model",
        "max_tokens": 300,
        "system": "Be brief.",
        "messages": [{"role": "user", "content": "THE THREAD"}],
    }
    sent = {k.lower(): v for k, v in req["headers"].items()}  # urllib capitalises a header's name
    assert sent["x-api-key"] == PLACEHOLDER and sent["anthropic-version"] == "2023-06-01"
    assert [s.name for s in asked] == ["api-key"] and len(released) == 1  # leased, then released


def test_cached_input_tokens_count_as_input(tmp_path, api):
    api.body = message("{}", tin=10, tout=1, cache_creation_input_tokens=5, cache_read_input_tokens=100)
    assert direct(tmp_path, api)[0]("i", "d", 0.05).tokens_in == 115


@pytest.mark.parametrize("status", [401, 429, 529])
def test_an_api_error_is_the_code_only_and_never_the_lease(tmp_path, api, status):
    api.status, api.body = status, {"type": "error", "error": {"message": f"echo {LEASE_TOKEN}"}}
    m, _, released = direct(tmp_path, api)
    got = m("i", "d", 0.05)
    assert not got.ok and got.error == f"the API answered HTTP {status}" and LEASE_TOKEN not in got.error
    assert len(released) == 1


@pytest.mark.parametrize("body", [b"not json", {"content": "x"}, {"usage": {}}, b"x" * (triage.MAX_REPLY_BYTES + 5)])
def test_an_answer_that_is_not_a_message_is_an_error(tmp_path, api, body):
    api.body = body
    got = direct(tmp_path, api)[0]("i", "d", 0.05)
    assert not got.ok and got.error == "the API's answer was not a message"


def test_a_lease_that_is_not_loopback_is_not_sent_to_and_a_dead_port_is_an_error_without_the_address(tmp_path, api):
    m, _, released = direct(tmp_path, api, base="https://api.example.org/l/" + LEASE_TOKEN)
    got = m("i", "d", 0.05)
    assert not got.ok and "not a loopback address" in got.error and api.requests == [] and len(released) == 1
    m, _, _ = direct(tmp_path, api, base=f"http://127.0.0.1:1/l/{LEASE_TOKEN}")
    got = m("i", "d", 0.05)
    assert not got.ok and got.error.startswith("the API could not be reached") and LEASE_TOKEN not in got.error


def test_a_broker_that_will_not_lease_is_an_error_and_nothing_is_sent(tmp_path, api):
    def refuse(s):
        raise triage.broker.BrokerRefused("not_allowed", "no")

    got = direct(tmp_path, api, lease=refuse)[0]("i", "d", 0.05)
    assert not got.ok and got.error == "the key could not be leased (BrokerRefused)" and api.requests == []


def test_a_call_whose_worst_case_passes_the_budget_is_not_made(tmp_path, api):
    m, asked, _ = direct(tmp_path, api)
    got = m("i", "x" * 4000, 0.0001)  # 1,000 tokens in and 300 out cost up to US$0.0025
    assert not got.ok and "budget_usd" in got.error and asked == [] and api.requests == []


def test_an_unpriced_direct_run_costs_zero_and_is_bounded_by_the_token_caps_only(tmp_path, api):
    m, _, _ = direct(tmp_path, api, runner_options={}, allow_unpriced=True)
    got = m("i", "d", 0.0001)
    assert got.ok and got.cost_usd == 0.0 and got.tokens_in == 900


def test_the_direct_runner_settings_are_checked(tmp_path):
    prompt, rules = tmp_path / "prompt.md", tmp_path / "rules.toml"
    base = {"mailboxes": ["a"], "rules": str(rules), "prompt_file": str(prompt)}
    env = {"HOME": str(tmp_path)}
    config(tmp_path)  # writes the prompt and rules files
    got = triage.parse_config({**base, "model": DIRECT_MODEL}, "j", tmp_path, env)
    assert got.model.runner == "anthropic-api" and got.model.runner_options["max_tokens"] == 300
    for model, match in (
        ({**DIRECT_MODEL, "secrets": []}, "needs \\[\\[triage.model.secrets"),
        ({**DIRECT_MODEL, "runner_options": {"max_tokens": 9}}, "price_in_usd_per_mtok and price_out"),
        ({**DIRECT_MODEL, "runner_options": {**PRICES, "stream": True}}, "takes runner_options"),
        ({**DIRECT_MODEL, "runner_options": {**PRICES, "max_tokens": 0}}, "above 0"),
        ({**DIRECT_MODEL, "runner_options": {**PRICES, "max_tokens": True}}, "above 0"),
    ):
        with pytest.raises(triage.TriageError, match=match):
            triage.parse_config({**base, "model": model}, "j", tmp_path, env)
    ok = {**DIRECT_MODEL, "runner_options": {}, "allow_unpriced": True}
    assert triage.parse_config({**base, "model": ok}, "j", tmp_path, env).model.allow_unpriced


def test_make_model_picks_the_direct_call_or_a_ws_runner(tmp_path):
    assert isinstance(triage.make_model(config(tmp_path), tmp_path), triage.RunnerModel)
    cfg = _replace(config(tmp_path), model={"runner": "anthropic-api"})
    assert isinstance(triage.make_model(cfg, tmp_path), triage.DirectModel)


def test_the_fixture_dry_run_through_the_direct_model_sends_under_3k_tokens_a_thread(tmp_path, api):
    """Nothing new is no call; one call per thread the rules leave; no label or draft in a dry run; and the worst case
    of a call (a 1,500-character prompt, the voice at its cap, a thread at its cap) is under 3,000 tokens (4 characters
    each, the count the thread cap uses). Real usage comes back from the API; here the stub says what it says."""
    r = Route()
    r.label = r.draft = lambda *a: pytest.fail("a dry run must not label or draft")  # type: ignore[assignment]
    m, _, _ = direct(tmp_path, api)
    cfg = dataclasses.replace(config(tmp_path), prompt="p" * 1500, voice="v" * triage.VOICE_MAX, model=m.cfg.model)
    assert go(tmp_path, r, m, cfg=cfg, dry=True)["model_calls"] == 0 and api.requests == []  # nothing new
    r.add(1, raw(List_Unsubscribe="<u>"))
    r.add(2, raw(subject="Report", body="Can you send the report? " * 600))  # a long thread: cut to its cap
    rec = go(tmp_path, r, m, cfg=cfg, dry=True)
    assert rec["threads_seen"] == 2 and rec["rule_labelled"] == 1 and rec["model_calls"] == 1 == len(api.requests)
    sent = api.requests[0]["body"]
    chars = len(sent["system"]) + sum(len(x["content"]) for x in sent["messages"])
    print(f"direct call: {chars} characters, about {chars // triage.CHARS_PER_TOKEN} tokens in (worst case)")
    assert chars // triage.CHARS_PER_TOKEN < 3000
    assert rec["tokens_in"] == 900 and rec["drafts"] == 1
