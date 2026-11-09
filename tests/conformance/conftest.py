"""Fixtures for the runner conformance suite: one `exp` per runner adapter, a scratch HOME, and `attempt`, which turns a
row's assertions into a result record that honours the runner's known failures (ws.conformance.settle).

A new adapter module gets a table with no edit here. The live tier (a real runner against a stub model) needs the
runner binary and skips without it, unless WS_E2E_REQUIRE=1 (CI), when it fails.
"""

from __future__ import annotations

import pytest

from ws import conformance


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "exp" in metafunc.fixturenames:
        found = conformance.all_expectations()
        metafunc.parametrize("exp", list(found.values()), ids=list(found))


@pytest.fixture
def box(tmp_path, monkeypatch):
    """HOME and WS_HOME under tmp_path, as every test in this repo has them; no git config of the machine's."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(home)
    return home


@pytest.fixture
def attempt():
    """attempt(exp, row, fn): run `fn`; its AssertionError (or any Exception) is the row failing. The result record is
    asserted here: SUCCESS, or INFO for a failure the runner lists as known. A skip passes through."""

    def go(exp: conformance.Expect, row: str, fn) -> dict:
        try:
            fn()
            error = None
        except Exception as e:  # noqa: BLE001 - any failure of the row is the row's result
            error = e
        result = conformance.settle(exp, row, error)
        assert result["status"] in ("SUCCESS", "INFO"), f"{result['name']}: {result['error']}"
        return result

    return go
