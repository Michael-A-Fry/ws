"""How `ws doctor` prints: a glyph plus a word per check, one closing line to `ws setup`, and colour that only reinforces."""

from __future__ import annotations

import pytest

from ws import brand, cli, doctor, osinfo
from ws.doctor import Check
from ws.style import Style

OSI = osinfo.OSInfo(kind="linux", release="ubuntu 24.04", arch="x86_64", scheduler="systemd", supported=True)

CHECKS = [
    Check("WS001", "platform", doctor.OK, "linux (ubuntu 24.04, x86_64)"),
    Check("WS007", "ptrace", doctor.WARN, "ptrace_scope is 0", "echo 1"),
    Check("WS008", "gate", doctor.FAIL, "not in place", "run `ws install`"),
    Check("WS103", "budget", doctor.UNCONFIGURED, "not set up: your own monthly spend cap", "add [roles.budget]"),
]


def strip(text: str) -> str:
    return brand.ANSI_RE.sub("", text)


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / ".ws"))
    for name in ("NO_COLOR", "FORCE_COLOR", "COLORFGBG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    return tmp_path


def test_every_check_shows_a_glyph_and_its_status_word():
    lines = doctor.render(CHECKS, OSI).splitlines()
    body = {line[:5]: line for line in lines if line.startswith("WS")}
    assert "✓ ok" in body["WS001"] and "! warn" in body["WS007"]
    assert "✗ fail" in body["WS008"] and "! unconfigured" in body["WS103"]


def test_statuses_and_roles_line_up_in_columns():
    lines = {line[:5]: line for line in doctor.render(CHECKS, OSI).splitlines() if line.startswith("WS")}
    roles = {"WS001": "platform", "WS007": "ptrace", "WS008": "gate", "WS103": "budget"}
    assert len({line.index(roles[i]) for i, line in lines.items()}) == 1
    fix = [line for line in doctor.render(CHECKS, OSI).splitlines() if "fix:" in line]
    assert len({line.index("fix:") for line in fix}) == 1 and fix[0].index("fix:") == lines["WS001"].index("linux")


def test_colour_changes_nothing_but_the_escape_codes():
    plain = doctor.render(CHECKS, OSI)
    coloured = doctor.render(CHECKS, OSI, Style(brand.FULL))
    assert coloured != plain and "\x1b[" in coloured and "\x1b[" not in plain
    assert strip(coloured) == plain
    assert strip(doctor.render(CHECKS, OSI, Style(brand.BASIC))) == plain


def test_the_closing_line_points_at_setup_when_something_is_unconfigured():
    lines = doctor.render(CHECKS, OSI).splitlines()
    assert lines[-2] == "4 checks: 1 ok, 1 warn, 1 fail, 1 unconfigured"
    assert lines[-1] == "1 unconfigured: run `ws setup` to set it up."
    many = [Check(f"WS10{n}", "r", doctor.UNCONFIGURED, "x") for n in range(3)]
    assert doctor.render(many, OSI).splitlines()[-1] == "3 unconfigured: run `ws setup` to set them up."


def test_no_closing_line_when_nothing_is_unconfigured():
    out = doctor.render(CHECKS[:3], OSI)
    assert "ws setup" not in out
    assert out.splitlines()[-1] == "3 checks: 1 ok, 1 warn, 1 fail, 0 unconfigured"
    assert doctor.render(CHECKS[:1], OSI).splitlines()[-1].startswith("1 check:")


def test_a_bad_job_count_reads_as_a_plural(box):
    (box / ".ws" / "jobs").mkdir(parents=True)
    for name in ("one", "two"):
        (box / ".ws" / "jobs" / f"{name}.toml").write_text("this is = not valid [")
    env = {"HOME": str(box), "WS_HOME": str(box / ".ws")}
    details = [c.detail for c in doctor.run_checks(OSI, box / "p.toml", env=env, network=False)]
    assert any(d.startswith("2 job files ws would refuse") for d in details)


def test_ansi_stripped_doctor_output_equals_its_no_color_output(box, capsys, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    cli.main(["doctor", "--offline"])
    forced = capsys.readouterr().out
    assert "\x1b[" in forced
    cli.main(["doctor", "--offline", "--no-color"])
    flagged = capsys.readouterr().out
    monkeypatch.delenv("FORCE_COLOR")
    monkeypatch.setenv("NO_COLOR", "1")
    cli.main(["doctor", "--offline"])
    no_color = capsys.readouterr().out
    assert "\x1b[" not in flagged and "\x1b[" not in no_color
    assert strip(forced) == flagged == no_color
    assert no_color.splitlines()[-1].endswith("run `ws setup` to set them up.")
