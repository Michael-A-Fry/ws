"""ws.ui: the shared page toolkit. The setup page must render byte for byte as it did before the move; every component
escapes its text; the loopback server refuses a wrong Host, a wrong Origin and a missing token, and binds loopback."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import threading

import pytest

from ws import brand, osinfo
from ws.setup_ui import flow, web
from ws.ui import components, css
from ws.ui.server import BadRequest, LoopbackServer, csp

BAD = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"

# ---- the setup page is unchanged by the move ------------------------------------------------------------------------

# sha256 of `render_page` for the Facts, Choices and nonce below, taken from the code as it stood before ws.ui existed.
GOLDEN_SHA256 = "d5cdf6f87890432a4d1c5d54e354eeac184b15b376dac8ba760a4e10b6aaedd6"
GOLDEN_LEN = 27344


def _fixed():
    facts = flow.Facts(
        env={"HOME": "/nonexistent-home"},
        osi=osinfo.OSInfo("linux", "ubuntu 24.04", "x86_64", "systemd", True),
        region="NZ",
        region_source="flag",
        runners=[flow.Runner("claude", "Claude Code", "/bin/claude", "2.0.0")],
        spent=1.5,
        plugins=[flow.Plugin("finance", "Finance", "Track spending.", True, "built", True)],
    )
    choices = flow.Choices(
        plugins=frozenset({"finance"}), notes="/nonexistent-home/Notes", runner="claude", cap=20.0, region="NZ"
    )
    return facts, choices


def test_the_setup_page_renders_byte_for_byte_as_it_did_before_ws_ui(monkeypatch):
    monkeypatch.setattr(
        web, "__version__", "0.0.0"
    )  # the page prints the version; pin it so a release can't break this
    facts, choices = _fixed()
    out = web.render_page(facts, choices, "NONCE0123456789ab")
    assert len(out) == GOLDEN_LEN
    assert hashlib.sha256(out.encode()).hexdigest() == GOLDEN_SHA256


def test_the_setup_stylesheet_is_the_shared_base_then_the_setup_rules_then_the_mark():
    assert web.CSS == css.stylesheet(web.STYLE)
    assert web.CSS.startswith(brand.css() + "\n" + css.BASE.strip("\n"))
    assert web.CSS.endswith(css.mark_css())
    assert css.PANEL in web.STYLE


def test_the_stylesheet_holds_no_colour_of_its_own():
    sheet = css.stylesheet(css.components_css())
    body = sheet.split("\n", 3)[3]  # past the three :root blocks, which are the tokens
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", body)
    assert "var(--ok)" in body and "var(--warn)" in body and "var(--error)" in body


def test_the_scale_is_named_and_every_value_is_a_length():
    for scale in (css.TYPE, css.SPACE, css.RADIUS):
        assert scale and all(re.fullmatch(r"\d+(\.\d+)?px", v) for v in scale.values())


# ---- components ------------------------------------------------------------------------------------------------------


def test_every_component_escapes_script():
    fragments = [
        components.panel(BAD, "<p>ok</p>", aside=BAD),
        components.row(BAD, BAD),
        components.status("ok", BAD),
        components.status(BAD, BAD),  # an unknown state is text too
        components.sparkline([1, 2], label=BAD),
        components.page(BAD, "<main></main>", BAD, ""),
    ]
    for frag in fragments:
        assert "<script>" not in frag and "alert(1)</script>" not in frag
    assert ESCAPED in fragments[0] and ESCAPED in fragments[1] and ESCAPED in fragments[2]
    assert ESCAPED in fragments[4] and ESCAPED in fragments[5]


def test_escaping_covers_quotes_in_attributes():
    out = components.sparkline([1], label='x" onload="boom')
    assert 'onload="boom' not in out and "&quot;" in out
    assert 'nonce="a&quot;b"' in components.page("t", "", 'a"b')


def test_a_panel_has_a_heading_a_body_and_an_optional_aside():
    out = components.panel("Sessions", "<p>x</p>", aside="3")
    assert out.startswith('<section class="panel"><h2>Sessions<span>3</span></h2>') and "<p>x</p>" in out
    assert components.panel("Sessions", "").startswith('<section class="panel"><h2>Sessions</h2>')


def test_a_status_is_a_glyph_and_a_word_never_colour_alone():
    for state, (_, glyph, word) in components.STATES.items():
        out = components.status(state)
        assert glyph in out and f">{word}<" in out and 'aria-hidden="true"' in out
    assert len({g for _, g, _ in components.STATES.values()}) == len(components.STATES)  # shape tells states apart
    assert "unknown" in components.status("nonsense")
    assert ">held up<" in components.status("warn", "held up")


def test_the_mark_is_the_brand_logo_and_matches_the_stylesheet_rows():
    out = components.mark()
    assert out.count("<i ") == len(brand.LOGO) and 'aria-label="ws"' in out
    assert f".logo .r{len(brand.LOGO) - 1}{{" in css.mark_css()


def test_sparkline_on_nothing_one_point_and_many():
    empty = components.sparkline([])
    assert "<svg" in empty and "no data" in empty and "polyline" not in empty and "circle" not in empty
    one = components.sparkline([1])
    assert "<circle" in one and "polyline" not in one and "1 point," in one
    many = components.sparkline([1, 2, 3])
    assert "<polyline" in many and "3 points" in many and "from 1 to 3" in many
    flat = components.sparkline([5, 5, 5])
    assert "<polyline" in flat and "nan" not in flat.lower()


def test_sparkline_drops_what_cannot_be_drawn():
    out = components.sparkline([float("nan"), float("inf"), True, "3", 2, 4])
    assert "2 points" in out and "nan" not in out.lower() and "inf" not in out.lower()
    assert "no data" in components.sparkline([float("nan")])


def test_sparkline_stays_inside_its_box_and_uses_attributes_only():
    out = components.sparkline([0, 10, 3, 7], width=80, height=20)
    assert 'viewBox="0 0 80 20"' in out and "style=" not in out
    for x, y in re.findall(r"(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)", re.search(r'points="([^"]+)"', out).group(1)):
        assert 0 <= float(x) <= 80 and 0 <= float(y) <= 20


def test_a_page_carries_the_nonce_on_its_style_and_script_and_the_extra_rules():
    out = components.page("Home", "<main>x</main>", "N1", ".mine{color:var(--ok)}", script="draw()")
    assert '<style nonce="N1">' in out and '<script nonce="N1">draw()</script>' in out
    assert ".mine{color:var(--ok)}" in out and ".spark" in out and "<title>Home</title>" in out
    assert "<script" not in components.page("Home", "", "N1")
    assert "nonce" not in components.page("Home", "", "")


# ---- the server -------------------------------------------------------------------------------------------------------


def _routes():
    seen = []

    def echo(data):
        seen.append(data)
        if data.get("bad"):
            raise BadRequest
        if data.get("boom"):
            raise RuntimeError("secret detail")
        return (200, {"got": data}, True) if data.get("stop") else (200, {"got": data})

    get = {"/": lambda nonce: f"<p>{nonce}</p>", "/data": lambda nonce: {"nonce": bool(nonce)}}
    return get, {"/echo": echo}, seen


@pytest.fixture
def served():
    get, post, seen = _routes()
    srv = LoopbackServer(get, post, server_version="ws-test", idle_seconds=30)
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(result=srv.serve()), daemon=True)
    t.start()
    yield srv, out, seen
    srv.close()
    t.join(5)


def _req(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    h = {"Host": f"127.0.0.1:{srv.port}", **(headers or {})}
    try:
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        return r.status, r.read(), dict(r.getheaders())
    finally:
        conn.close()


def _post(srv, body, *, token=True, origin=True, host=None, path="/echo"):
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Ws-Token"] = srv.token
    if origin:
        h["Origin"] = f"http://127.0.0.1:{srv.port}"
    if host:
        h["Host"] = host
    return _req(srv, "POST", path, json.dumps(body), h)


def test_the_server_binds_loopback_only_on_a_random_port(served):
    srv, _, _ = served
    assert srv.httpd.server_address[0] == "127.0.0.1" and srv.port > 0
    assert srv.url == f"http://127.0.0.1:{srv.port}/?t={srv.token}"
    other = LoopbackServer({"/": lambda n: ""})
    try:
        assert other.port != srv.port and other.token != srv.token and len(other.token) >= 16
    finally:
        other.httpd.server_close()


def test_a_get_needs_the_right_host_and_the_token(served):
    srv, _, _ = served
    assert _req(srv, "GET", f"/?t={srv.token}")[0] == 200
    assert _req(srv, "GET", "/")[0] == 403  # no token
    assert _req(srv, "GET", "/?t=wrong")[0] == 403
    assert _req(srv, "GET", f"/?t={srv.token}", headers={"Host": "example.test"})[0] == 403
    assert _req(srv, "GET", f"/?t={srv.token}", headers={"Host": f"127.0.0.1:{srv.port + 1}"})[0] == 403
    assert _req(srv, "GET", f"/?t={srv.token}", headers={"Host": f"localhost:{srv.port}"})[0] == 200
    assert _req(srv, "GET", "/?t=%C3%A9")[0] == 403  # a non-ASCII token is a no, not a traceback


def test_a_post_needs_the_token_header_the_origin_and_the_host(served):
    srv, _, seen = served
    assert _post(srv, {"a": 1})[0] == 200
    assert _post(srv, {"a": 2}, token=False)[0] == 403
    assert _post(srv, {"a": 3}, origin=False)[0] == 403
    assert _post(srv, {"a": 4}, host="example.test")[0] == 403
    status, _, _ = _req(
        srv,
        "POST",
        "/echo",
        "{}",
        {"Content-Type": "application/json", "X-Ws-Token": srv.token, "Origin": "http://evil.test"},
    )
    assert status == 403
    status, _, _ = _req(
        srv,
        "POST",
        f"/echo?t={srv.token}",
        "{}",
        {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{srv.port}"},
    )
    assert status == 403  # the token in the address is not enough for a POST
    assert seen == [{"a": 1}]  # nothing refused reached the route


def test_a_wrong_token_header_is_refused_even_with_the_right_origin(served):
    srv, _, seen = served
    status, _, _ = _req(
        srv,
        "POST",
        "/echo",
        "{}",
        {"Content-Type": "application/json", "X-Ws-Token": "t\xe9k\xe9n", "Origin": f"http://127.0.0.1:{srv.port}"},
    )
    assert status == 403 and not seen


def test_routes_pages_json_and_unknown_paths(served):
    srv, _, _ = served
    status, body, headers = _req(srv, "GET", f"/?t={srv.token}")
    nonce = re.search(rb"<p>(.+)</p>", body).group(1).decode()
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert headers["Content-Security-Policy"] == csp(nonce) and f"'nonce-{nonce}'" in headers["Content-Security-Policy"]
    status, body, headers = _req(srv, "GET", f"/data?t={srv.token}")
    assert status == 200 and json.loads(body) == {"nonce": True} and headers["Content-Security-Policy"] == csp()
    assert _req(srv, "GET", f"/missing?t={srv.token}")[0] == 404
    assert _req(srv, "GET", "/missing")[0] == 403  # no token: it doesn't say which paths exist
    assert _post(srv, {}, path="/missing")[0] == 404


def test_the_nonce_is_new_for_every_response(served):
    srv, _, _ = served
    first = _req(srv, "GET", f"/?t={srv.token}")[1]
    second = _req(srv, "GET", f"/?t={srv.token}")[1]
    assert first != second


def test_every_response_carries_the_security_headers(served):
    srv, _, _ = served
    for headers in (_req(srv, "GET", "/")[2], _req(srv, "GET", f"/?t={srv.token}")[2], _post(srv, {})[2]):
        assert headers["Cache-Control"] == "no-store" and headers["X-Frame-Options"] == "DENY"
        assert headers["X-Content-Type-Options"] == "nosniff" and headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["Server"].startswith("ws-test")


def test_the_csp_without_a_nonce_allows_nothing():
    assert csp() == "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    assert "unsafe-inline" not in csp("abc") and "style-src 'nonce-abc'" in csp("abc")


def test_a_bad_body_a_bad_request_and_a_route_that_raises(served, capsys):
    srv, _, _ = served
    assert _req(srv, "POST", "/echo", "not json", {**_post_headers(srv)})[0] == 400
    assert _req(srv, "POST", "/echo", "[1]", {**_post_headers(srv)})[0] == 400  # JSON, but not an object
    assert _req(srv, "POST", "/echo", "{}" + " " * 70_000, {**_post_headers(srv)})[0] == 400  # over the size limit
    assert _post(srv, {"bad": True})[0] == 400
    status, body, _ = _post(srv, {"boom": True})
    assert status == 500 and b"secret detail" not in body and json.loads(body)["error"]
    assert _post(srv, {"a": 1})[0] == 200  # still answering
    status, _, _ = _req(srv, "POST", "/echo", "{}", {**_post_headers(srv), "Content-Type": "text/plain"})
    assert status == 415
    assert "Traceback" not in capsys.readouterr().err


def _post_headers(srv):
    return {"Content-Type": "application/json", "X-Ws-Token": srv.token, "Origin": f"http://127.0.0.1:{srv.port}"}


def test_a_route_can_stop_the_server_after_its_reply_and_serve_returns_the_result():
    get, post, _ = _routes()
    srv = LoopbackServer(get, post, idle_seconds=30)
    srv.result = {"done": True}
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(result=srv.serve()), daemon=True)
    t.start()
    status, body, _ = _post(srv, {"stop": True})
    t.join(5)
    assert status == 200 and json.loads(body)["got"] == {"stop": True}
    assert not t.is_alive() and out["result"] == {"done": True}


def test_only_a_request_with_the_token_keeps_the_server_alive():
    import time

    get, post, _ = _routes()
    srv = LoopbackServer(get, post, idle_seconds=0.6)
    done = threading.Event()
    threading.Thread(target=lambda: (srv.serve(), done.set()), daemon=True).start()
    end = time.monotonic() + 3
    while not done.is_set() and time.monotonic() < end:
        try:
            _req(srv, "GET", "/?t=wrong")  # a stranger knocking does not reset the idle clock
        except OSError:
            break
        time.sleep(0.1)
    assert done.wait(3)


def test_an_idle_server_closes_and_returns_nothing():
    get, post, _ = _routes()
    assert LoopbackServer(get, post, idle_seconds=0.3).serve() is None


def test_the_denied_and_failed_messages_are_the_callers():
    srv = LoopbackServer({"/": lambda n: ""}, denied="Use the link again.", failed="It broke.")
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    try:
        status, body, _ = _req(srv, "GET", "/")
        assert status == 403 and body == b"Use the link again."
    finally:
        srv.close()
        t.join(5)


# ---- components the home page added ---------------------------------------------------------------------------------


def test_the_home_components_escape_script_and_quotes():
    frags = [
        components.empty(BAD),
        components.chip(BAD),
        components.stat(BAD, BAD, BAD),
        components.link(BAD, "https://example.test/?a=1&b=" + BAD),
        components.action(BAD, opener=BAD, kind=BAD, id='x" onclick="boom'),
    ]
    for frag in frags:
        assert "<script>" not in frag and "alert(1)</script>" not in frag
    assert 'onclick="boom' not in frags[4] and "data-opener=" in frags[4]
    assert ESCAPED in frags[0] and ESCAPED in frags[1] and ESCAPED in frags[2]


def test_a_link_is_a_plain_anchor_for_a_safe_scheme_and_text_for_any_other():
    out = components.link("Open", "https://claude.ai/code/x")
    assert out == '<a class="act" href="https://claude.ai/code/x" target="_blank" rel="noopener noreferrer">Open</a>'
    for href in ("javascript:alert(1)", "data:text/html,x", "//evil.test/x", "/etc/passwd", "file:///x", ""):
        assert "<a " not in components.link("Open", href)
    assert "<a " in components.link("Open", "claude://session/1")


def test_an_action_turns_underscores_in_a_name_into_dashes():
    assert (
        components.action("Go", session_id="s1") == '<button type="button" class="act" data-session-id="s1">Go</button>'
    )


def test_the_sparkline_takes_a_fixed_scale_and_whole_pixel_sizes():
    flat_top = components.sparkline([0.96, 1.0], lo=0.0, hi=1.0)
    auto = components.sparkline([0.96, 1.0])
    assert flat_top != auto  # a fixed scale doesn't stretch a small wobble across the box
    ys = [float(p.split(",")[1]) for p in re.search(r'points="([^"]+)"', flat_top).group(1).split()]
    assert max(ys) - min(ys) < 2
    assert 'width="160" height="32"' in components.sparkline([1, 2], width=160.0, height=32.0)  # type: ignore[arg-type]
