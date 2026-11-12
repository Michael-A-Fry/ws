"""Policy v-next tables: data classes and class_of, budget purposes, autonomy, exception expiry, the `prepare` pin."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ws import policy
from ws.policy import PolicyError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    return h


def classes(body: str) -> dict[str, policy.Class]:
    return policy.loads(body).classes


def refused(text: str, *words: str) -> None:
    with pytest.raises(PolicyError) as e:
        policy.loads(text)
    for w in words:
        assert w in str(e.value), str(e.value)


# --- [classes] -------------------------------------------------------------------------------------------------


def test_no_classes_is_the_old_behaviour(tmp_path):
    pol = policy.loads("")
    assert pol.classes == {} and policy.class_of(pol, tmp_path) is None
    assert policy.class_of(policy.default_policy(), tmp_path) is None


def test_a_full_class_parses_with_every_key():
    c = classes(
        """
[classes.work]
access = "walled"
os_user = "ws-work"
allowed_providers = ["anthropic", "local"]
local_only = true
allowed_hosts = ["api.example.com", "*.example.org:443"]
readable_by = ["lead"]
inbound = "allowlist"
inbound_allow = ["lead", "owner"]
retention_days = 90
repos = ["~/code/work/**", "client-*"]

[classes.lead]
access = "full"
repos = ["~/code/lead"]
"""
    )["work"]
    assert c == policy.Class(
        "work",
        "walled",
        ("~/code/work/**", "client-*"),
        "ws-work",
        ("anthropic", "local"),
        True,
        ("api.example.com", "*.example.org:443"),
        ("lead",),
        "allowlist",
        ("lead", "owner"),
        90,
    )


def test_defaults_follow_the_access():
    got = classes(
        """
[classes.a]
access = "full"
repos = ["/a"]
[classes.b]
access = "walled"
repos = ["/b"]
[classes.c]
access = "counts-only"
repos = ["/c"]
"""
    )
    assert [got[n].inbound for n in "abc"] == ["lead-only", "off", "off"]
    for c in got.values():
        assert (c.retention_days, c.os_user, c.local_only, c.allowed_hosts, c.readable_by, c.inbound_allow) == (
            30,
            None,
            False,
            (),
            (),
            (),
        )
        assert c.allowed_providers is None  # not set: the consumer decides the walled default


def test_an_empty_provider_list_is_set_and_means_none():
    c = classes('[classes.a]\naccess = "walled"\nrepos = ["/a"]\nallowed_providers = []\n')["a"]
    assert c.allowed_providers == ()


def test_readable_by_star_and_names():
    got = classes(
        """
[classes.a]
access = "full"
repos = ["/a"]
readable_by = ["*"]
[classes.b]
access = "walled"
repos = ["/b"]
readable_by = ["a"]
"""
    )
    assert got["a"].readable_by_class("b") and got["a"].readable_by_class("anything")
    assert got["b"].readable_by_class("a") and not got["b"].readable_by_class("b")
    assert not policy.UNCLASSIFIED.readable_by_class("a")


A = 'access = "walled"\nrepos = ["/a"]\n'


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("[classes]\nx = 1\n", ("[classes.x]",)),
        ('[classes."bad name"]\n' + A, ("plain name",)),
        ('[classes."a\\n"]\n' + A, ("plain name",)),  # a name with a trailing new line
        ("[classes.unclassified]\n" + A, ("unclassified",)),
        ('[classes.a]\nrepos = ["/a"]\n', ("access is required",)),
        ('[classes.a]\naccess = "open"\nrepos = ["/a"]\n', ("access must be one of",)),
        ("[classes.a]\naccess = 1\nrepos = ['/a']\n", ("access",)),
        ("[classes.a]\n" + A + "bogus = 1\n", ("unknown key", "bogus")),
        ("[classes.a]\n" + A + 'os_user = "bad user"\n', ("os_user",)),
        ("[classes.a]\n" + A + "os_user = 5\n", ("os_user",)),
        ("[classes.a]\n" + A + 'allowed_providers = ["a b"]\n', ("allowed_providers",)),
        ("[classes.a]\n" + A + 'allowed_providers = "x"\n', ("allowed_providers",)),
        ("[classes.a]\n" + A + 'local_only = "yes"\n', ("local_only",)),
        ("[classes.a]\n" + A + 'allowed_hosts = ["not a host"]\n', ("allowed_hosts",)),
        ("[classes.a]\n" + A + 'allowed_hosts = ["host.example\\n"]\n', ("allowed_hosts",)),
        ("[classes.a]\n" + A + 'os_user = "alice\\n"\n', ("os_user",)),
        ("[classes.a]\n" + A + 'readable_by = ["ghost"]\n', ("readable_by", "ghost")),
        ("[classes.a]\n" + A + 'readable_by = ["*", "a"]\n', ("readable_by",)),
        ("[classes.a]\n" + A + 'inbound = "everyone"\n', ("inbound must be one of",)),
        ("[classes.a]\n" + A + 'inbound = "allowlist"\n', ("inbound_allow is required",)),
        ("[classes.a]\n" + A + 'inbound = "allowlist"\ninbound_allow = []\n', ("inbound_allow is required",)),
        ("[classes.a]\n" + A + 'inbound = "lead-only"\ninbound_allow = ["x"]\n', ("only applies",)),
        ("[classes.a]\n" + A + "retention_days = 0\n", ("retention_days",)),
        ("[classes.a]\n" + A + "retention_days = 3651\n", ("retention_days",)),
        ("[classes.a]\n" + A + "retention_days = true\n", ("retention_days",)),
        ("[classes.a]\n" + A + "retention_days = 30.0\n", ("retention_days",)),
        ('[classes.a]\naccess = "walled"\n', ("repos is required",)),
        ('[classes.a]\naccess = "walled"\nrepos = []\n', ("repos is required",)),
        ('[classes.a]\naccess = "walled"\nrepos = [""]\n', ("repos",)),
        ('[classes.a]\naccess = "walled"\nrepos = "/a"\n', ("repos",)),
    ],
)
def test_a_bad_class_refuses_the_whole_file(text, words):
    refused(text, *words)
    # nothing is partly loaded: a good table beside a bad one refuses too
    refused('[classes.good]\naccess = "full"\nrepos = ["/g"]\n' + text, *words)


def test_retention_bounds_are_inclusive():
    for days in (1, 3650):
        assert classes(f"[classes.a]\n{A}retention_days = {days}\n")["a"].retention_days == days


# --- class_of --------------------------------------------------------------------------------------------------


def two(tmp_path) -> policy.Policy:
    secret, open_ = tmp_path / "secret", tmp_path / "open"
    for d in (secret / "sub", open_):
        d.mkdir(parents=True)
    return policy.loads(
        f"""
[classes.walled]
access = "walled"
repos = ["{secret}"]
[classes.open]
access = "full"
repos = ["{open_}/**"]
"""
    )


def test_class_of_matches_the_repo_and_below(tmp_path):
    pol = two(tmp_path)
    for p in (tmp_path / "secret", tmp_path / "secret" / "sub", tmp_path / "secret" / "not-there"):
        assert policy.class_of(pol, p).name == "walled"
    assert policy.class_of(pol, tmp_path / "open").name == "open"
    assert policy.class_of(pol, str(tmp_path / "open" / "x" / "y")).name == "open"


def test_none_matching_is_the_unclassified_class(tmp_path):
    got = policy.class_of(two(tmp_path), tmp_path / "elsewhere")
    assert got is policy.UNCLASSIFIED
    assert (got.access, got.inbound, got.readable_by, got.allowed_hosts) == ("counts-only", "off", (), ())


def test_a_symlink_into_a_walled_repo_counts_as_that_repo(tmp_path):
    pol = two(tmp_path)
    link = tmp_path / "open" / "shortcut"
    link.symlink_to(tmp_path / "secret", target_is_directory=True)
    # as written it is under open/**; resolved it is the walled repo
    assert policy.class_of(pol, link).name == "walled"
    assert policy.class_of(pol, link / "sub").name == "walled"


def test_a_link_into_a_walled_repo_is_walled_whichever_class_has_the_longer_name(tmp_path):
    # the looser class has the longer literal folder, so the written spelling would win on length: only the real path counts
    walled, loose = tmp_path / "w", tmp_path / "open-repo-long"
    (walled / "secret").mkdir(parents=True)
    (loose / "app").mkdir(parents=True)
    (loose / "app" / "link").symlink_to(walled / "secret", target_is_directory=True)
    pol = policy.loads(
        f'[classes.walled]\naccess = "walled"\nrepos = ["{walled}/**"]\n'
        f'[classes.open]\naccess = "full"\nrepos = ["{loose}/**"]\n'
    )
    for p in (loose / "app" / "link", loose / "app" / "link" / "sub", loose / "app" / "link" / ".." / "secret"):
        assert policy.class_of(pol, p).name == "walled"
    assert policy.class_of(pol, loose / "app").name == "open"


def test_a_link_from_a_narrow_loose_class_into_a_broad_walled_one_is_walled(tmp_path):
    code, proj = tmp_path / "code", tmp_path / "work" / "proj"
    (code / "fiu").mkdir(parents=True)
    proj.mkdir(parents=True)
    (proj / "link").symlink_to(code / "fiu", target_is_directory=True)
    pol = policy.loads(
        f'[classes.walled]\naccess = "walled"\nrepos = ["{code}/**"]\n'
        f'[classes.full]\naccess = "full"\nrepos = ["{proj}/**"]\n'
    )
    assert policy.class_of(pol, proj / "link").name == "walled"
    assert policy.class_of(pol, proj / "link" / "sub").name == "walled"
    assert policy.class_of(pol, proj / "other").name == "full"


def test_a_link_out_of_a_walled_repo_to_no_class_is_unclassified(tmp_path):
    walled, elsewhere = tmp_path / "w", tmp_path / "elsewhere"
    walled.mkdir()
    elsewhere.mkdir()
    (walled / "out").symlink_to(elsewhere, target_is_directory=True)
    pol = policy.loads(f'[classes.walled]\naccess = "walled"\nrepos = ["{walled}/**"]\n')
    assert policy.class_of(pol, walled / "out") is policy.UNCLASSIFIED


def test_case_games_cannot_reach_a_looser_class(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "CASELESS_PATHS", True)
    pol = policy.loads(
        f'[classes.walled]\naccess = "walled"\nrepos = ["{tmp_path}/Code/Fiu"]\n'
        f'[classes.full]\naccess = "full"\nrepos = ["{tmp_path}/**"]\n'
    )
    for spelling in ("code/fiu", "CODE/FIU", "Code/Fiu/sub"):
        assert policy.class_of(pol, tmp_path / spelling).name == "walled"
    monkeypatch.setattr(policy, "CASELESS_PATHS", False)
    assert policy.class_of(pol, tmp_path / "CODE" / "FIU").name == "full"  # a case-sensitive disk: another folder


def test_a_repo_glob_that_is_itself_a_link_matches_the_real_path(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    pol = policy.loads(f'[classes.a]\naccess = "walled"\nrepos = ["{tmp_path}/alias"]\n')
    assert policy.class_of(pol, real).name == "a"
    assert policy.class_of(pol, tmp_path / "alias").name == "a"


def test_overlap_the_most_specific_wins_whatever_the_order(tmp_path):
    for order in (("outer", "inner"), ("inner", "outer")):
        tables = {
            "outer": f'[classes.outer]\naccess = "walled"\nrepos = ["{tmp_path}/**"]\n',
            "inner": f'[classes.inner]\naccess = "full"\nrepos = ["{tmp_path}/code/app/**"]\n',
        }
        pol = policy.loads("".join(tables[n] for n in order))
        assert policy.class_of(pol, tmp_path / "code" / "app" / "x").name == "inner"
        assert policy.class_of(pol, tmp_path / "code" / "other").name == "outer"


def test_a_tie_goes_to_the_stricter_access(tmp_path):
    def winner(first: str, second: str) -> str:
        pol = policy.loads(
            f'[classes.a]\naccess = "{first}"\nrepos = ["{tmp_path}/r"]\n'
            f'[classes.b]\naccess = "{second}"\nrepos = ["{tmp_path}/r"]\n'
        )
        return policy.class_of(pol, tmp_path / "r").name

    assert winner("full", "walled") == "b" and winner("walled", "full") == "a"
    assert winner("walled", "counts-only") == "b" and winner("counts-only", "full") == "a"
    assert winner("walled", "walled") == "a"  # equal in both: the first name, so the answer never depends on file order


def test_a_bare_name_matches_at_any_depth_but_is_the_least_specific(tmp_path):
    pol = policy.loads(
        f'[classes.any]\naccess = "walled"\nrepos = ["ws-private"]\n[classes.home]\naccess = "full"\nrepos = ["{tmp_path}/**"]\n'
    )
    assert policy.class_of(pol, tmp_path / "a" / "ws-private").name == "home"
    assert policy.class_of(pol, Path("/elsewhere/ws-private")).name == "any"


def test_caseless_matching_where_the_platform_is_caseless(tmp_path, monkeypatch):
    pol = policy.loads(f'[classes.a]\naccess = "walled"\nrepos = ["{tmp_path}/Secret"]\n')
    monkeypatch.setattr(policy, "CASELESS_PATHS", False)
    assert policy.class_of(pol, tmp_path / "SECRET") is policy.UNCLASSIFIED
    monkeypatch.setattr(policy, "CASELESS_PATHS", True)
    assert policy.class_of(pol, tmp_path / "SECRET").name == "a"
    assert policy.class_of(pol, tmp_path / "secret" / "x").name == "a"


def test_a_path_that_cannot_be_read_is_the_strictest_class(tmp_path):
    assert policy.class_of(two(tmp_path), "bad\x00path") is policy.UNCLASSIFIED


def test_a_relative_path_is_taken_from_the_working_folder(tmp_path, monkeypatch):
    pol = two(tmp_path)
    monkeypatch.chdir(tmp_path / "secret")
    assert policy.class_of(pol, ".").name == "walled"


# --- [budget.purposes] -----------------------------------------------------------------------------------------


def test_budget_purposes_parse():
    pol = policy.loads("[budget.purposes]\nchat = 5\nresearch = 12.5\nnone = 0\n")
    assert pol.budget_purposes == {"chat": 5.0, "research": 12.5, "none": 0.0}
    assert policy.loads("").budget_purposes == {}
    assert policy.loads("[budget]\n").budget_purposes == {}


@pytest.mark.parametrize(
    "text",
    [
        "[budget.purposes]\nchat = -1\n",
        "[budget.purposes]\nchat = true\n",
        '[budget.purposes]\nchat = "5"\n',
        "[budget.purposes]\nchat = inf\n",
        "[budget.purposes]\nchat = nan\n",
        "[budget.purposes]\nchat = 1e999\n",
        "[budget.purposes]\n'bad name' = 5\n",
        '[budget.purposes]\n"chat\\n" = 5\n',
        "[budget]\ntotal = 5\n",
        "budget = 5\n",
        "[budget]\npurposes = 5\n",
    ],
)
def test_bad_budget_purposes_refuse_the_file(text):
    refused(text, "budget")


def test_a_budget_month_is_the_utc_calendar_month():
    nz = timezone(timedelta(hours=13))
    # 08:00 on 1 Dec in Auckland is still 19:00 on 30 Nov in UTC: the November window
    start, end = policy.budget_month(datetime(2026, 12, 1, 8, 0, tzinfo=nz))
    assert (start, end) == (datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    assert policy.budget_month(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        policy.budget_month(datetime(2026, 12, 1))


# --- [autonomy] ------------------------------------------------------------------------------------------------


def test_autonomy_defaults_and_values():
    assert policy.loads("").autonomy == policy.Autonomy("decide-and-tell", "when-broken", ())
    got = policy.loads(
        '[autonomy]\nlevel = "autonomous"\ninterrupts = "never"\nalways_ask = ["spending", "data-deletion", "spending"]\n'
    ).autonomy
    assert got == policy.Autonomy("autonomous", "never", ("spending", "data-deletion"))


def test_autonomy_is_not_the_interrupts_counts():
    pol = policy.loads('[autonomy]\ninterrupts = "daily-summary"\n[interrupts]\ndigest_per_day = 2\n')
    assert pol.autonomy.interrupts == "daily-summary" and pol.interrupts.digest_per_day == 2


@pytest.mark.parametrize(
    "text",
    [
        '[autonomy]\nlevel = "reckless"\n',
        '[autonomy]\ninterrupts = "always"\n',
        "[autonomy]\nlevel = 1\n",
        '[autonomy]\nalways_ask = "spending"\n',
        '[autonomy]\nalways_ask = ["two words"]\n',
        "[autonomy]\nmore = 1\n",
    ],
)
def test_bad_autonomy_refuses_the_file(text):
    refused(text, "autonomy")


# --- [[exceptions]] owner and expires --------------------------------------------------------------------------

EX = """
[sources.mail]
untrusted = true
[sources.notes]
sensitive = true
[gate]
allow_hosts = ["x.example"]
[[exceptions]]
name = "digest"
job = "morning"
sources = ["mail", "notes"]
reason = "reads mail, writes only to me"
"""


def exception(extra: str) -> policy.RuleException:
    return policy.loads(EX + extra).exceptions[0]


def test_exception_without_expiry_never_expires():
    e = exception("")
    assert (e.owner, e.expires) == (None, None)
    assert not e.expired(datetime(2999, 1, 1, tzinfo=UTC))


def test_a_date_expires_at_midnight_utc_that_day():
    e = exception('owner = "Michael"\nexpires = 2026-12-01\n')
    assert e.owner == "Michael" and e.expires == datetime(2026, 12, 1, tzinfo=UTC)
    just_before = datetime(2026, 12, 1, tzinfo=UTC) - timedelta(seconds=1)
    assert not e.expired(just_before)
    assert e.expired(datetime(2026, 12, 1, tzinfo=UTC))
    # the same instant in another zone: 13:00 in Auckland (+13) on 1 Dec is 00:00Z that day
    assert e.expired(datetime(2026, 12, 1, 13, 0, tzinfo=timezone(timedelta(hours=13))))
    assert not e.expired(datetime(2026, 12, 1, 12, 59, tzinfo=timezone(timedelta(hours=13))))


def test_an_offset_datetime_is_kept_as_an_instant():
    e = exception("expires = 2026-12-01T09:00:00+13:00\n")
    assert e.expires == datetime(2026, 11, 30, 20, 0, tzinfo=UTC) and e.expires.utcoffset() == timedelta(0)
    assert exception("expires = 2026-12-01T09:00:00Z\n").expires == datetime(2026, 12, 1, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "extra",
    [
        "expires = 2026-12-01T09:00:00\n",  # a local date-time has no instant
        "expires = 09:00:00\n",
        'expires = "2026-12-01"\n',
        "expires = 5\n",
        "expires = true\n",
        'owner = ""\n',
        "owner = 3\n",
    ],
)
def test_bad_expiry_or_owner_refuses_the_file(extra):
    refused(EX + extra, "[[exceptions]] #1")


def test_expired_wants_an_aware_clock():
    e = exception("expires = 2026-12-01\n")
    with pytest.raises(ValueError, match="timezone-aware"):
        e.expired(datetime(2026, 12, 1))


def test_rule_of_two_ignores_an_expired_exception_and_names_it():
    pol = policy.loads(EX + "expires = 2026-12-01\n[sources.web]\n")
    args = (pol, ["mail", "notes"], "morning")
    before = policy.rule_of_two(*args, now=datetime(2026, 11, 30, 23, 59, tzinfo=UTC))
    assert (before.status, before.exception) == ("exception", "digest")
    after = policy.rule_of_two(*args, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert after.status == "refused" and not after.ok
    assert "exception 'digest' expired at 2026-12-01T00:00:00+00:00" in after.reason
    other_job = policy.rule_of_two(pol, ["mail", "notes"], "other", now=datetime(2026, 12, 1, tzinfo=UTC))
    assert "expired" not in other_job.reason  # only an exception that would have covered the job is named


def test_rule_of_two_uses_the_real_clock_by_default():
    gone = policy.loads(EX + "expires = 2001-01-01\n")
    assert policy.rule_of_two(gone, ["mail", "notes"], "morning").status == "refused"
    future = policy.loads(EX + f"expires = {date.today().year + 50}-01-01\n")
    assert policy.rule_of_two(future, ["mail", "notes"], "morning").status == "exception"


# --- compatibility and the `prepare` pin -----------------------------------------------------------------------


def test_the_scaffolded_policy_is_prepare_and_has_no_classes():
    text = policy.default_toml()
    assert 'dial = "prepare"' in text and 'dial = "act"' not in text
    for table in ("[classes", "[budget", "[autonomy"):
        assert table not in text
    pol = policy.loads(text)
    assert (pol.dial, pol.classes, pol.budget_purposes, pol.autonomy) == ("prepare", {}, {}, policy.Autonomy())


def test_no_code_in_ws_writes_a_policy_with_the_act_dial():
    for src in (ROOT / "src" / "ws").rglob("*.py"):
        assert not re.search(r"dial\s*=\s*[\"']act[\"']", src.read_text(encoding="utf-8")), src.name


def test_every_policy_in_the_docs_still_loads():
    """The policy examples in the docs (fenced TOML with `schema = 1`) load with the new fields at their defaults."""
    found = 0
    for md in sorted((ROOT / "docs").rglob("*.md")):
        for block in re.findall(r"```toml\n(.*?)```", md.read_text(encoding="utf-8"), re.DOTALL):
            if re.search(r"^schema = 1$", block, re.MULTILINE):
                policy.loads(block)
                found += 1
    assert found >= 2


def test_an_existing_policy_loads_unchanged_with_the_new_fields_at_their_defaults():
    pol = policy.loads(EX)
    assert (pol.classes, pol.budget_purposes, pol.autonomy) == ({}, {}, policy.Autonomy())
    assert pol.exceptions[0].expires is None


def test_admin_refuses_session_prepare_and_start_from_a_gated_shell():
    pol = policy.default_policy()
    for cmd in (
        "ws session prepare --role lead",
        "ws session start --session-id x",
        "uv run ws session prepare --role lead",
    ):
        assert policy.is_denied_command(pol, cmd), cmd
    assert policy.is_denied_command(pol, "ws session nudge --session-id x") is None


_CWD_SPELLINGS = ["-C /tmp", "-C/tmp", "-C=/tmp", "--cwd /tmp", "--cwd=/tmp", "--cw /tmp", "--c=/tmp", "-C . --json"]
_ADMIN_LINES = [
    "session prepare --role lead",
    "session start --session-id x",
    "secret ask NAME",
    "gate generate",
    "gate init",
    "install --gate launch",
    "skills adopt x",
]


@pytest.mark.parametrize("line", _ADMIN_LINES)
@pytest.mark.parametrize("flag", _CWD_SPELLINGS)
def test_admin_is_refused_whatever_cwd_flag_sits_before_or_inside_the_command(flag, line):
    """`-C PATH` is a global flag `ws` takes anywhere: its value must not count as a subcommand word."""
    pol = policy.default_policy()
    head, _, tail = line.partition(" ")
    for cmd in (f"ws {flag} {line}", f"uv run ws {flag} {line}", f"ws {head} {flag} {tail}"):
        assert policy.is_denied_command(pol, cmd), cmd


@pytest.mark.parametrize("flag", _CWD_SPELLINGS)
def test_a_cwd_flag_before_an_allowed_command_stays_allowed(flag):
    pol = policy.default_policy()
    assert policy.is_denied_command(pol, f"ws {flag} status") is None
    assert policy.is_denied_command(pol, f"ws {flag} session nudge --session-id x") is None
    assert policy.is_denied_command(pol, "ws -C . status") is None


_LAUNCHERS = [
    "ws",
    "uv run ws",
    "uv run --with pkg ws",
    "uv run --no-project --with a --with b ws",
    "uvx --from x ws",
    "uvx --from=x --python 3.12 ws",
    "uvx ws",
    "uv tool run ws",
    "pipx run ws",
    "/abs/path/bin/ws",
    "./bin/ws",
    "~/.local/bin/ws",
    "env -u N ws",
    "env -u N A=1 B=2 ws",
    "env A=1 /opt/bin/ws",
    "python -m ws",
    "python -I -m ws",
]


@pytest.mark.parametrize("line", _ADMIN_LINES)
@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_admin_is_refused_after_any_launcher_wrapper_or_path(launcher, line):
    pol = policy.default_policy()
    for cmd in (f"{launcher} {line}", f"{launcher} -C . {line}"):
        assert policy.is_denied_command(pol, cmd), cmd


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_a_launcher_before_an_allowed_command_stays_allowed(launcher):
    pol = policy.default_policy()
    for cmd in (f"{launcher} status", f"{launcher} -C . status", f"{launcher} session nudge --session-id x"):
        assert policy.is_denied_command(pol, cmd) is None, cmd
    assert policy.is_denied_command(pol, f"{launcher} install --gate every-session") is None


def test_words_that_only_look_like_ws_are_not_read_as_the_command():
    pol = policy.default_policy()
    for cmd in ("cat ws secret", "ls ws", "grep -r secret ws", "git add ws"):
        assert policy.is_denied_command(pol, cmd) is None, cmd


# --- owner, review_date, per-repo settings, and who may send in -------------------------------------------------------------

W = 'access = "walled"\nrepos = ["/srv/alpha/**", "/srv/alpha-extra/**"]\n'


def test_owner_and_review_date_parse_and_default_to_none_set():
    got = classes("[classes.a]\n" + W + 'owner = "  A. Person "\nreview_date = 2027-03-01\n')["a"]
    assert got.owner == "A. Person" and got.review_date == date(2027, 3, 1)
    plain = classes("[classes.a]\n" + W)["a"]
    assert (plain.owner, plain.review_date, plain.repo_settings) == ("", None, ())  # an old policy loads as it did


@pytest.mark.parametrize(
    ("extra", "words"),
    [
        ('owner = ""\n', ("owner must not be empty",)),
        ('owner = "   "\n', ("owner must not be empty",)),
        ("owner = 5\n", ("owner must be text",)),
        ('review_date = "2027-03-01"\n', ("review_date must be a TOML date",)),
        ("review_date = 2027-03-01T09:00:00Z\n", ("review_date must be a TOML date",)),  # a time of day is not a date
        ("review_date = 2027\n", ("review_date must be a TOML date",)),
        ("review_date = true\n", ("review_date must be a TOML date",)),
    ],
)
def test_a_bad_owner_or_review_date_refuses_the_whole_file(extra, words):
    refused("[classes.a]\n" + W + extra, *words)
    refused('[classes.good]\naccess = "full"\nrepos = ["/g"]\n[classes.a]\n' + W + extra, *words)  # not a partial load


def test_a_review_is_due_on_its_day_in_utc_whatever_the_clock_is_spelled_in():
    c = classes("[classes.a]\n" + W + "review_date = 2027-03-01\n")["a"]
    due = [
        (datetime(2027, 2, 28, 23, 59, 59, tzinfo=UTC), False),
        (datetime(2027, 3, 1, 0, 0, tzinfo=UTC), True),
        (datetime(2027, 3, 1, 8, 0, tzinfo=timezone(timedelta(hours=12))), False),  # 20:00 UTC on 28 Feb
        (datetime(2027, 2, 28, 20, 0, tzinfo=timezone(timedelta(hours=-5))), True),  # 01:00 UTC on 1 Mar
        (datetime(2030, 1, 1, tzinfo=UTC), True),
    ]
    assert [c.review_due(now) for now, _ in due] == [ok for _, ok in due]
    with pytest.raises(ValueError):
        c.review_due(datetime(2027, 3, 1))  # a clock with no timezone is not guessed at
    assert classes("[classes.a]\n" + W)["a"].review_due(datetime(2099, 1, 1, tzinfo=UTC)) is False  # no date: never due


RS = '[classes.a.repo_settings."/srv/alpha/**"]\n'
OU = 'os_user = "ws-alpha"\n'  # a per-repo table narrows a walled class's file, so the class names an account


def test_a_per_repo_table_parses_and_narrows_only_for_a_path_inside_its_glob(tmp_path):
    c = classes(
        "[classes.a]\n"
        + W
        + OU
        + RS
        + 'deny = ["Bash(curl *)"]\ndeny_read = ["/srv/alpha/secret"]\ndeny_write = ["/srv/alpha/frozen"]\n'
        + '[classes.a.repo_settings."/srv/alpha-extra/**"]\ndeny = ["Bash(wget *)"]\n'
    )["a"]
    assert [r.glob for r in c.repo_settings] == ["/srv/alpha/**", "/srv/alpha-extra/**"]
    got = c.narrowing_for("/srv/alpha/app")
    assert (got.deny, got.deny_read, got.deny_write) == (
        ("Bash(curl *)",),
        ("/srv/alpha/secret",),
        ("/srv/alpha/frozen",),
    )
    assert (
        c.narrowing_for("/srv/alpha-extra/x").deny == ("Bash(wget *)",)
        and c.narrowing_for("/srv/alpha-extra/x").deny_read == ()
    )
    assert c.narrowing_for("/srv/elsewhere") == policy.RepoSettings("")  # a path in none of the globs: nothing added


@pytest.mark.parametrize(
    "key", ["allow", "allowed_hosts", "allow_write", "allow_read", "permissions", "bypass", "inbound", "env", "hooks"]
)
def test_a_per_repo_key_that_would_widen_the_class_file_is_refused_naming_the_key(key):
    text = "[classes.a]\n" + W + OU + RS + f"{key} = []\n"
    refused(text, key, "would widen the class file", "may only narrow")
    refused(
        '[classes.good]\naccess = "full"\nrepos = ["/g"]\n' + text, key, "would widen"
    )  # the whole file, never partly


def test_a_per_repo_table_must_name_a_repo_glob_of_the_class_and_hold_lists_of_text():
    refused(
        "[classes.a]\n" + W + OU + '[classes.a.repo_settings."/srv/other/**"]\ndeny = []\n',
        "not one of the class's repos",
    )
    refused("[classes.a]\n" + W + OU + RS + "deny = [1]\n", "deny must be a list of non-empty text")
    refused("[classes.a]\n" + W + OU + 'repo_settings = "x"\n', "repo_settings must be a table of tables")
    refused("[classes.a]\n" + W + OU + 'repo_settings = { "/srv/alpha/**" = 5 }\n', "must be a table")


def test_a_whole_policy_file_with_one_bad_class_table_is_not_loaded_at_all(tmp_path):
    f = tmp_path / "policy.toml"
    f.write_text(
        '[classes.good]\naccess = "full"\nrepos = ["/g"]\nowner = "me"\nreview_date = 2027-01-01\n'
        "[classes.a]\n" + W + OU + RS + 'allow = ["Bash(*)"]\n',
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="allow"):
        policy.load(f)


def test_who_may_send_in_is_inbound_and_inbound_allow_and_a_walled_class_defaults_to_nobody():
    assert classes("[classes.a]\n" + W)["a"].inbound == "off"  # a walled class: nobody can send in unless it says so
    refused("[classes.a]\n" + W + 'inbound_from = ["x"]\n', "unknown key 'inbound_from'")  # no second spelling of it
    got = classes("[classes.a]\n" + W + 'inbound = "allowlist"\ninbound_allow = ["lead"]\n')["a"]
    assert (got.inbound, got.inbound_allow) == ("allowlist", ("lead",))


def test_a_class_with_repo_settings_but_no_os_user_is_refused_at_load_naming_the_class_and_the_key():
    text = "[classes.a]\n" + W + RS + 'deny = ["Bash(curl *)"]\n'
    with pytest.raises(PolicyError) as e:
        policy.loads(text)
    assert "[classes.a] repo_settings needs os_user" in str(e.value)
    refused(
        '[classes.good]\naccess = "full"\nrepos = ["/g"]\n' + text, "repo_settings needs os_user"
    )  # the whole file, never partly
    ok = classes("[classes.a]\n" + W + OU + RS + 'deny = ["Bash(curl *)"]\n')["a"]
    assert ok.os_user == "ws-alpha" and ok.repo_settings[0].deny == ("Bash(curl *)",)
    assert classes("[classes.a]\n" + W)["a"].repo_settings == ()  # no table, no account: as before
