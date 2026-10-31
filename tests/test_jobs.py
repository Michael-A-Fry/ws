"""Job files: what a good one looks like, and that each bad one is refused with a message that names the key."""

from __future__ import annotations

import json
import os

import pytest

from ws import jobs

GOOD = """
runner = "claude-code"
model = "claude-haiku-4-5"
prompt = "Summarise the inbox."
"""


def load(text: str, name: str = "daily", base=None):
    return jobs.loads(name, text, base or jobs.jobs_dir({"WS_HOME": "/nonexistent"}))


def test_a_minimal_job_gets_the_headless_defaults():
    j = load(GOOD)
    assert (j.runner, j.model, j.prompt) == ("claude-code", "claude-haiku-4-5", "Summarise the inbox.")
    assert j.bare is True and j.timeout_s == 900 and j.max_turns is None and j.cwd is None
    assert j.sources == () and j.secrets == () and j.runner_options == {}


def test_every_key_is_read():
    j = load(
        GOOD
        + """
cwd = "~/work"
timeout_s = 60
max_turns = 5
bare = false
sources = ["notes", "mail"]
[[secrets]]
name = "anthropic"
host = "API.Anthropic.com"
purpose = "nightly"
[runner_options]
allowed_tools = ["Read"]
"""
    )
    assert (j.cwd, j.timeout_s, j.max_turns, j.bare) == ("~/work", 60, 5, False)
    assert j.sources == ("notes", "mail")
    assert j.secrets == (jobs.SecretUse("anthropic", "api.anthropic.com", "nightly"),)
    assert j.runner_options == {"allowed_tools": ["Read"]}


def test_green_is_optional_and_one_of_the_four_levels():
    assert load(GOOD).green is None
    for level in ("targeted", "package", "full", "merge_ready"):
        assert load(GOOD + f'green = "{level}"\n').green == level


def test_the_purpose_of_a_secret_defaults_to_run():
    j = load(GOOD + '[[secrets]]\nname = "anthropic"\nhost = "api.anthropic.com"\n')
    assert j.secrets[0].purpose == "run"


def test_prompt_file_is_read_relative_to_the_jobs_folder(tmp_path):
    (tmp_path / "daily.md").write_text("From a file.\n", encoding="utf-8")
    j = load('runner = "opencode"\nmodel = "a/b"\nprompt_file = "daily.md"\n', base=tmp_path)
    assert j.prompt == "From a file.\n"


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ('model = "m"\nprompt = "p"\n', "runner is required"),
        ('runner = "claude-code"\nprompt = "p"\n', "model is required"),
        ('runner = "claude-code"\nmodel = "m"\n', "exactly one of prompt and prompt_file"),
        (GOOD + 'prompt_file = "x.md"\n', "exactly one of prompt and prompt_file"),
        (GOOD.replace("claude-code", "nope"), "runner 'nope' is not known"),
        (GOOD + "timeout_s = 0\n", "timeout_s must be a number above 0"),
        (GOOD + "timeout_s = true\n", "timeout_s must be a number above 0"),
        (GOOD + "max_turns = 1.5\n", "max_turns must be a whole number above 0"),
        (GOOD + 'bare = "yes"\n', "bare must be true or false"),
        (GOOD + "sources = [1]\n", "sources must be a list of source names"),
        (GOOD + "colour = 1\n", "unknown key 'colour'"),
        (GOOD + 'green = "great"\n', "green must be one of targeted, package, full, merge_ready"),
        (GOOD + "green = 3\n", "green must be a non-empty string"),
        (GOOD + "runner_options = 3\n", "runner_options must be a table"),
        (GOOD + '[[secrets]]\nhost = "h.example"\n', "secrets #1: name is required"),
        (GOOD + '[[secrets]]\nname = "k"\n', "secrets #1: host is required"),
        (GOOD + '[[secrets]]\nname = "k"\nhost = "https://h.example/x"\n', "secrets #1: host"),
        (GOOD + '[[secrets]]\nname = "9k"\nhost = "h.example"\n', "secrets #1: name '9k'"),
        (GOOD + '[[secrets]]\nname = "k"\nhost = "h.example"\nvalue = "x"\n', "secrets #1: unknown key 'value'"),
        ("runner = [", "job daily"),
    ],
)
def test_a_bad_job_names_the_key(text, names):
    with pytest.raises(jobs.JobError) as e:
        load(text)
    assert names in str(e.value)


def test_a_missing_or_empty_prompt_file_is_refused(tmp_path):
    with pytest.raises(jobs.JobError, match="prompt_file: .*No such file"):
        load('runner = "opencode"\nmodel = "a/b"\nprompt_file = "gone.md"\n', base=tmp_path)
    (tmp_path / "blank.md").write_text("  \n", encoding="utf-8")
    with pytest.raises(jobs.JobError, match="is empty"):
        load('runner = "opencode"\nmodel = "a/b"\nprompt_file = "blank.md"\n', base=tmp_path)


def test_the_job_name_cannot_leave_the_jobs_folder(tmp_path):
    env = {"WS_HOME": str(tmp_path)}
    with pytest.raises(jobs.JobError, match="letters, digits"):
        jobs.load("../evil", env)
    with pytest.raises(jobs.JobError, match="no job 'gone'"):
        jobs.load("gone", env)


def test_load_reads_the_file_in_ws_home(tmp_path):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "daily.toml").write_text(GOOD, encoding="utf-8")
    assert jobs.load("daily", {"WS_HOME": str(tmp_path)}).model == "claude-haiku-4-5"


def test_runner_options_are_checked_by_the_helpers():
    j = load(GOOD + '[runner_options]\nallowed_tools = ["Read", 3]\nexe = 5\nenv = { A = "1" }\nwat = 1\n')
    with pytest.raises(ValueError, match=r"runner_options\.wat is not an option of claude-code"):
        jobs.check_options(j, {"exe", "allowed_tools", "env"})
    with pytest.raises(ValueError, match=r"runner_options\.allowed_tools must be a list of strings"):
        jobs.string_list(j, "allowed_tools")
    with pytest.raises(ValueError, match=r"runner_options\.exe must be a str"):
        jobs.option(j, "exe", str, "claude")
    assert jobs.extra_env(j) == {"A": "1"}


def test_prompt_file_must_be_inside_the_jobs_folder(tmp_path):
    base = tmp_path / "jobs"
    base.mkdir()
    (tmp_path / "outside.md").write_text("secret notes", encoding="utf-8")
    (base / "link.md").symlink_to(tmp_path / "outside.md")
    for name in ("../outside.md", str(tmp_path / "outside.md"), "link.md"):
        with pytest.raises(jobs.JobError, match="must be inside"):
            load(f'runner = "opencode"\nmodel = "a/b"\nprompt_file = {json.dumps(name)}\n', base=base)


def test_allow_unpriced_is_read_and_checked():
    assert load(GOOD).allow_unpriced is False
    assert load(GOOD + "allow_unpriced = true\n").allow_unpriced is True
    with pytest.raises(jobs.JobError, match="allow_unpriced must be true or false"):
        load(GOOD + 'allow_unpriced = "yes"\n')


def private_home(tmp_path):
    (tmp_path / "jobs").mkdir()
    f = tmp_path / "jobs" / "daily.toml"
    f.write_text(GOOD, encoding="utf-8")
    (tmp_path / "jobs").chmod(0o755)
    f.chmod(0o644)
    return {"WS_HOME": str(tmp_path)}, f


def test_a_job_file_or_folder_others_can_write_is_refused(tmp_path):
    env, f = private_home(tmp_path)
    assert jobs.load("daily", env).name == "daily"
    for mode in (0o664, 0o646):
        f.chmod(mode)
        with pytest.raises(jobs.JobError, match="can be written by others"):
            jobs.load("daily", env)
    f.chmod(0o600)
    assert jobs.load("daily", env).name == "daily"
    f.parent.chmod(0o775)
    with pytest.raises(jobs.JobError, match="can be written by others"):
        jobs.load("daily", env)


def test_a_job_file_that_belongs_to_someone_else_is_refused(tmp_path, monkeypatch):
    env, f = private_home(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: f.stat().st_uid + 1)
    with pytest.raises(jobs.JobError, match="belongs to another user"):
        jobs.load("daily", env)


def test_a_prompt_file_others_can_write_is_refused(tmp_path):
    base = tmp_path / "jobs"
    base.mkdir()
    p = base / "daily.md"
    p.write_text("hi", encoding="utf-8")
    p.chmod(0o666)
    with pytest.raises(jobs.JobError, match="prompt_file: .*can be written by others"):
        load('runner = "opencode"\nmodel = "a/b"\nprompt_file = "daily.md"\n', base=base)


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_BASE_URL", "anthropic_api_key", "OPENAI_BASE_URL", "OPENCODE_CONFIG", "CLAUDE_CONFIG_DIR",
        "WS_HOME", "PATH", "HOME", "FOO_KEY", "FOO_TOKEN", "FOO_SECRET", "LD_PRELOAD", "NODE_OPTIONS", "PYTHONPATH",
    ],
)  # fmt: skip
def test_job_env_refuses_reserved_names(name):
    j = load(GOOD + f'[runner_options]\nenv = {{ {name} = "x" }}\n')
    with pytest.raises(ValueError, match=f"runner_options.env: {name} is reserved"):
        jobs.extra_env(j)


def test_job_env_allows_ordinary_names_and_refuses_odd_ones():
    assert jobs.extra_env(load(GOOD + '[runner_options]\nenv = { XDG_CACHE_HOME = "/c", TZ = "UTC" }\n')) == {
        "XDG_CACHE_HOME": "/c",
        "TZ": "UTC",
    }
    with pytest.raises(ValueError, match="not a variable name"):
        jobs.extra_env(load(GOOD + '[runner_options]\nenv = { "A B" = "x" }\n'))
