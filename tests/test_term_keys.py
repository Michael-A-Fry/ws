"""The key table: the bytes each key-bar control sends, and the Ctrl rule."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ws.term import keys, page

EXPECTED = json.loads((Path(__file__).parent / "fixtures" / "term" / "keys_expected.json").read_text())
ROWS = {r["action"]: r for r in keys.KEYBAR}


def test_every_control_has_a_unique_action_a_label_and_a_title():
    actions = [r["action"] for r in keys.KEYBAR]
    assert len(actions) == len(set(actions))
    assert all(r["label"] and r["title"] for r in keys.KEYBAR)
    assert all(re.fullmatch(r"[a-z]+(-[a-z]+)*", a) for a in actions)


def test_the_bar_has_every_key_a_phone_lacks():
    assert set(ROWS) >= {
        "esc",
        "tab",
        "shift-tab",
        "ctrl",
        "up",
        "down",
        "left",
        "right",
        "slash",
        "shift-enter",
        "paste",
    }


@pytest.mark.parametrize("action", [a for a in EXPECTED if a not in ("about", "ctrl_of")])
def test_each_control_sends_the_measured_bytes(action):
    assert ROWS[action]["bytes"].encode("latin-1").hex() == EXPECTED[action]


def test_the_two_controls_that_are_not_byte_senders():
    assert ROWS["ctrl"]["bytes"] is None and ROWS["paste"]["bytes"] is None
    assert {a for a, r in ROWS.items() if r["bytes"] is None} == {"ctrl", "paste"}


def test_shift_enter_is_escape_then_carriage_return_not_a_bare_cr():
    assert keys.SHIFT_ENTER == "\x1b\r"
    assert keys.SHIFT_ENTER != "\r"


@pytest.mark.parametrize(("ch", "hexed"), sorted(EXPECTED["ctrl_of"].items()))
def test_ctrl_turns_a_letter_into_letter_and_0x1f_and_maps_the_punctuation(ch, hexed):
    assert keys.ctrl_byte(ch).encode("latin-1").hex() == hexed


def test_ctrl_of_a_capital_is_the_same_as_of_the_lower_case_letter():
    assert keys.ctrl_byte("C") == keys.ctrl_byte("c") == "\x03"


def test_ctrl_of_more_than_one_character_is_left_alone():
    assert keys.ctrl_byte("ab") == "ab" and keys.ctrl_byte("") == ""


def test_the_pages_buttons_are_the_table_one_for_one_in_order():
    html = page.page_html("N", token="T", xterm_css="", integrity="sha256-x")
    bar = html.split('id="keybar"', 1)[1].split("</div>", 1)[0]
    assert re.findall(r'data-action="([a-z-]+)"', bar) == [r["action"] for r in keys.KEYBAR]


def test_the_page_gets_the_ctrl_punctuation_from_the_same_table():
    html = page.page_html("N", token="T", xterm_css="", integrity="sha256-x")
    cfg = json.loads(re.search(r'id="cfg">(.*?)</script>', html, re.DOTALL).group(1))
    assert cfg["ctrl_punct"] == keys.CTRL_PUNCT and cfg["shift_enter"] == keys.SHIFT_ENTER
    assert cfg["keybar"] == keys.keybar_json()
