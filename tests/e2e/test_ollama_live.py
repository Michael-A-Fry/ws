"""A real local model answers: one request to an Ollama server named by `WS_OLLAMA_URL`.

Skipped unless `WS_OLLAMA_URL` is set (for example `http://localhost:11434`). The model is `WS_OLLAMA_MODEL`, or else the
one the server already has loaded, or else the first it lists, so the test pulls nothing and loads as little as it can.
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

URL = os.environ.get("WS_OLLAMA_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not URL, reason="WS_OLLAMA_URL is not set")


def _get(path: str) -> dict:
    with urllib.request.urlopen(URL + path, timeout=30) as r:
        return json.load(r)


def _model() -> str:
    named = os.environ.get("WS_OLLAMA_MODEL")
    if named:
        return named
    loaded = _get("/api/ps").get("models") or []
    listed = _get("/api/tags").get("models") or []
    if not (loaded or listed):
        pytest.skip("the server has no model pulled")
    return (loaded or listed)[0]["name"]


def test_a_local_model_gives_a_non_empty_reply():
    body = json.dumps(
        {
            "model": _model(),
            "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
            "stream": False,
            "think": False,
            "options": {"num_predict": 16},
        }
    ).encode()
    req = urllib.request.Request(URL + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        assert r.status == 200
        reply = json.load(r)
    assert reply.get("done") is True
    assert reply["message"]["content"].strip()
