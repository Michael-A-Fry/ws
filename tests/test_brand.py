"""The look in `ws.brand`: contrast of every declared pair, the CSS written from the tokens, the colour switch."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ws import brand
from ws.brand import BASIC, FULL, OFF, PAIRS, TOKENS, Ansi, color_mode, contrast, token, xterm_hex

SRC = Path(brand.__file__).parent
SURFACES = {"bg", "panel", "raise", "line"}  # backgrounds and a hairline: never a foreground that carries meaning
HEX = re.compile(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b")


# ---- contrast -------------------------------------------------------------------------------------------------------


def test_contrast_matches_the_wcag_definition():
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0)
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)
    assert contrast("#ffffff", "#777777") == contrast("#777777", "#ffffff")


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize(("fg", "bg", "kind"), PAIRS, ids=[f"{f}-on-{b}-{k}" for f, b, k in PAIRS])
def test_every_declared_pair_reaches_its_ratio(theme, fg, bg, kind):
    pick = (lambda t: t.dark) if theme == "dark" else (lambda t: t.light)
    ratio = contrast(pick(token(fg)), pick(token(bg)))
    assert ratio >= brand.KIND_MIN[kind], f"{theme}: {fg} on {bg} is {ratio:.2f}, a {kind} needs {brand.KIND_MIN[kind]}"


def test_every_colour_role_is_checked_somewhere():
    used = {fg for fg, _, _ in PAIRS}
    assert {t.name for t in TOKENS} - used == SURFACES
    assert {kind for _, _, kind in PAIRS} <= set(brand.KIND_MIN)


def test_the_mark_is_checked_in_both_themes_on_its_page_background():
    """Each row of the mark takes a role; the light ramp must hold 3:1 on the light page (the old inline dark
    gradient was 1.7:1 at the bottom)."""
    for role in brand.GRADIENT_ROLES:
        assert contrast(token(role).light, token("bg").light) >= 3
        assert contrast(token(role).dark, token("bg").dark) >= 3


def test_the_basic_terminal_grey_holds_4_5_on_black_and_white():
    grey = xterm_hex(int(token("muted").basic.rsplit(";", 1)[1]))
    assert contrast(grey, "#000000") >= 4.5 and contrast(grey, "#ffffff") >= 4.5


# ---- one source ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["accent", "indigo", "teal", "ok", "warn", "error"])
def test_the_page_and_the_terminal_agree_on_the_brand_colours(role):
    t = token(role)
    assert t.dark == xterm_hex(t.xterm)


def test_tokens_are_well_formed_and_named_once():
    names = [t.name for t in TOKENS]
    assert len(names) == len(set(names))
    for t in TOKENS:
        assert re.fullmatch(r"#[0-9a-f]{6}", t.dark) and re.fullmatch(r"#[0-9a-f]{6}", t.light), t.name
        assert t.xterm is None or 0 <= t.xterm <= 255
    for role in (*brand.GRADIENT_ROLES, *brand.RAMP_ROLES, *brand.SMALL_ROLES):
        assert token(role).xterm is not None


def test_the_css_is_written_from_the_tokens_once_per_theme():
    css = brand.css()
    dark, toggled, media = css.split("\n")
    assert dark.startswith(":root{") and "color-scheme:dark" in dark
    assert toggled.startswith(":root[data-theme=light]{") and "color-scheme:light" in toggled
    assert media.startswith("@media(prefers-color-scheme:light){:root:not([data-theme=dark]){")
    assert toggled.split("{", 1)[1].rstrip("}") == media.split("{:root:not([data-theme=dark]){", 1)[1].rstrip("}")
    for t in TOKENS:
        assert f"--{t.name}:{t.dark}" in dark and f"--{t.name}:{t.light}" in toggled
    assert "--tint:rgba(135,95,255,0.13)" in dark and "--glow:rgba(135,95,255,0.17)" in dark
    assert re.findall(r"--tint:rgba\(([\d,]+),", toggled) == ["106,63,224"]  # the light accent, not the dark one


def test_tokens_json_is_dtcg_shaped_and_round_trips():
    doc = json.loads(json.dumps(brand.tokens_json()))
    accent = doc["color"]["accent"]
    assert accent["$type"] == "color" and accent["$value"] == token("accent").dark
    assert accent["$extensions"]["ws"]["light"] == token("accent").light
    assert accent["$extensions"]["ws"]["xterm"] == 99
    assert doc["color"]["ok"]["$extensions"]["ws"]["basic"] == "32"
    assert set(doc["color"]) == {t.name for t in TOKENS}


# ---- no colour literal outside brand.py -----------------------------------------------------------------------------

# A file that may hold a #rrggbb, and why. Empty on purpose: every colour belongs in brand.py.
HEX_ALLOWED: dict[str, str] = {}


def test_no_hex_colour_in_src_outside_brand_py():
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "brand.py" or rel in HEX_ALLOWED:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if HEX.search(line):
                found.append(f"{rel}:{n}: {line.strip()[:70]}")
    assert not found, "a colour literal belongs in ws/brand.py:\n" + "\n".join(found)


# ---- when colour is on ----------------------------------------------------------------------------------------------

DARK = {"COLORFGBG": "15;0"}
LIGHT = {"COLORFGBG": "0;15"}


@pytest.mark.parametrize(
    ("env", "tty", "flag", "want"),
    [
        ({}, True, False, BASIC),  # a terminal that doesn't say it is dark: the terminal's own colours
        (DARK, True, False, FULL),
        ({"COLORFGBG": "15;default;0"}, True, False, FULL),
        (LIGHT, True, False, BASIC),
        ({"COLORFGBG": "junk"}, True, False, BASIC),
        ({}, False, False, OFF),  # a pipe
        ({"TERM": "dumb"}, True, False, OFF),
        ({"TERM": "xterm-256color"}, True, False, BASIC),
        # NO_COLOR: only a non-empty value turns colour off
        ({"NO_COLOR": "1"}, True, False, OFF),
        ({"NO_COLOR": "0"}, True, False, OFF),
        ({"NO_COLOR": ""}, True, False, BASIC),
        ({"NO_COLOR": "", **DARK}, True, False, FULL),
        # --no-color beats everything
        ({}, True, True, OFF),
        ({"FORCE_COLOR": "1"}, True, True, OFF),
        # FORCE_COLOR: colour for a pipe or a dumb terminal; "0" and empty are no request; NO_COLOR wins
        ({"FORCE_COLOR": "1"}, False, False, BASIC),
        ({"FORCE_COLOR": "1", **DARK}, False, False, FULL),
        ({"FORCE_COLOR": "1", "TERM": "dumb"}, True, False, BASIC),
        ({"FORCE_COLOR": "0"}, False, False, OFF),
        ({"FORCE_COLOR": ""}, False, False, OFF),
        ({"FORCE_COLOR": "1", "NO_COLOR": "1"}, True, False, OFF),
    ],
)
def test_color_mode_table(env, tty, flag, want):
    assert color_mode(env, tty, no_color=flag) == want


@pytest.mark.parametrize(
    ("env", "tty", "want"),
    [({}, True, True), ({"TERM": "xterm"}, True, True), ({"TERM": "dumb"}, True, False), ({}, False, False)],
)
def test_can_redraw_table(env, tty, want):
    assert brand.can_redraw(env, tty) is want


@pytest.mark.parametrize(
    ("value", "want"),
    [("15;0", True), ("0;15", False), ("15;default;0", True), ("0;7", False), ("12;8", True), ("", None), ("x", None)],
)
def test_dark_background_reads_colorfgbg(value, want):
    assert brand.dark_background({"COLORFGBG": value} if value else {}) is want


def test_status_roles_follow_the_mode_and_the_mark_never_does():
    full, basic, off = Ansi(FULL), Ansi(BASIC), Ansi(OFF)
    assert full.mint("x") == "\x1b[38;5;49mx\x1b[0m" and basic.mint("x") == "\x1b[32mx\x1b[0m"
    assert full.amber("x") == "\x1b[38;5;214mx\x1b[0m" and basic.amber("x", bold=True) == "\x1b[1;33mx\x1b[0m"
    assert full.error("x") == "\x1b[38;5;203mx\x1b[0m" and basic.error("x") == "\x1b[31mx\x1b[0m"
    assert basic.violet("x") == full.violet("x") == "\x1b[38;5;99mx\x1b[0m"
    assert basic.logo() == full.logo() and basic.rule(20) == full.rule(20)
    assert off.mint("x") == off.violet("x") == off.dim("x") == "x" and off.rule(3) == "━━━"
    assert Ansi(True).mode == FULL and Ansi(False).mode == OFF


def test_stripping_the_codes_leaves_the_plain_text_in_every_mode():
    for mode in (FULL, BASIC):
        a = Ansi(mode)
        parts = [
            a.mint("a", True),
            a.amber("b"),
            a.error("c"),
            a.violet("d"),
            a.dim("e"),
            a.grey("f"),
            a.track(2, 4, 8),
        ]
        assert brand.ANSI_RE.sub("", "".join(parts)) == "abcdef" + "━" * 8
