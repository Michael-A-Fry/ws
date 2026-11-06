"""The vendored terminal assets: pinned by sha256, licensed, and what the page's policy allows."""

from __future__ import annotations

import base64
import hashlib
import re

import pytest

from ws.term import assets, page

# The files as published on npm (`npm pack`), hashed. Written here, not computed from the module, so a changed blob fails.
PINNED = {
    "xterm.js": "14903579ff54664cd72f8e8699e6961a6272c21863ec1c3b118cdc8af5d4a972",
    "xterm.css": "854a7c0fb70e8b1a083c16797ab827299fb18744f5ad34f227b48337e33293c6",
    "addon-fit.js": "ba3ea256ce0620a0992a197d6c9baea64823fc93d8da07a9e366ca9943c18527",
    "addon-clipboard.js": "ddae2ca81f1cadfe74d960dd1436eb334606d6aa674cab1f06683cffc22cbec5",
    "addon-web-links.js": "b74864125ef3889753b94e61eb4dabb99b2a27f920f0c9a7bd53cbe6d10f032a",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_each_shipped_file_has_the_pinned_sha256(name):
    data = assets.load(name)
    assert hashlib.sha256(data).hexdigest() == PINNED[name] == assets.FILES[name][1]


def test_the_table_lists_exactly_the_pinned_files():
    assert set(assets.FILES) == set(PINNED)


def test_a_blob_that_doesnt_match_its_hash_is_refused(monkeypatch):
    blob, _ = assets.FILES["addon-fit.js"]
    monkeypatch.setitem(assets.FILES, "addon-fit.js", (blob, "0" * 64))
    with pytest.raises(ValueError, match="pinned sha256"):
        assets.load("addon-fit.js")


def test_versions_are_pinned_exact():
    assert assets.VERSIONS == {
        "@xterm/xterm": "6.0.0",
        "@xterm/addon-fit": "0.11.0",
        "@xterm/addon-clipboard": "0.2.0",
        "@xterm/addon-web-links": "0.12.0",
    }
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in assets.VERSIONS.values())


def test_the_mit_licence_text_ships_for_every_package():
    assert set(assets.LICENCES) == set(assets.VERSIONS)
    for text in assets.LICENCES.values():
        assert "Permission is hereby granted, free of charge" in text and "xterm.js authors" in text
        assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text


def test_the_bundle_is_the_terminal_then_each_addon_and_defines_what_the_page_uses():
    text = assets.bundle().decode("utf-8")
    for name in ("xterm.js", "addon-fit.js", "addon-clipboard.js", "addon-web-links.js"):
        assert assets.load(name).decode("utf-8").rstrip() in text
    assert text.index("xterm.js.map") < text.index("addon-fit.js.map") < text.index("addon-clipboard.js")
    for api in ("FitAddon", "ClipboardAddon", "WebLinksAddon", "synchronizedOutputMode", "mouseTrackingMode"):
        assert api in text


def test_the_integrity_value_is_the_base64_of_the_bundles_sha256():
    bundle = assets.bundle()
    assert page.script_integrity(bundle) == "sha256-" + base64.b64encode(hashlib.sha256(bundle).digest()).decode()


def test_the_policy_runs_only_the_pinned_script_and_the_pages_own_nonce():
    policy = page.policy("NONCE", "sha256-HASH")
    assert "script-src 'sha256-HASH' 'nonce-NONCE'" in policy and "style-src 'nonce-NONCE'" in policy
    assert "unsafe-eval" not in policy and "*" not in policy
    assert policy.count("unsafe-inline") == 1 and "style-src-attr 'unsafe-inline'" in policy, (
        "only for style attributes"
    )
    assert (
        "unsafe-inline" not in policy.split("style-src-attr")[0] and "script-src 'sha256-HASH' 'nonce-NONCE'" in policy
    )
    assert policy.startswith("default-src 'none'") and "frame-ancestors 'none'" in policy
    assert "https:" not in policy and "http:" not in policy, "nothing loads from outside"


def test_the_page_tags_its_vendor_script_with_the_integrity_the_policy_allows():
    integrity = page.script_integrity(assets.bundle())
    html = page.page_html("N", token="TOK", xterm_css="", integrity=integrity)
    assert f'<script src="{page.ASSET_PATH}?t=TOK" integrity="{integrity}"></script>' in html
    assert len(re.findall(r"<script\b[^>]*\bsrc=", html)) == 1, "one external script, and it's ours"
    assert "http://" not in html and "https://" not in html.replace("https?:", ""), "no outside address in the page"
