"""Memory role, markdown-git: the checks moved here from doctor, and the folder it asks install for."""

from __future__ import annotations

from ws import install, memory
from ws.roles import markdown_git


def test_check_walks_missing_then_not_git_then_ok(box, linux, env, write_profile):
    mem = box / "mem"
    write_profile({"roles": {"memory": {"path": str(mem)}}})
    status, detail = markdown_git.check({"path": str(mem)}, linux, env, False)
    assert status == "fail" and "ws memory init" in detail
    mem.mkdir()
    assert markdown_git.check({"path": str(mem)}, linux, env, False)[0] == "warn"
    memory.init(mem)
    memory.add("fact", "One")
    status, detail = markdown_git.check({"path": str(mem)}, linux, env, False)
    assert status == "ok" and "git repo" in detail
    assert markdown_git.check({}, linux, env, False) == ("fail", "no path set")


def test_steps_name_the_memory_folder_as_a_root(box, env):
    [step] = markdown_git.steps({}, install.Answers(memory="~/ws-memory"), env)
    assert step.kind == "dir" and step.root and step.path == box / "ws-memory"
    [step] = markdown_git.steps({}, install.Answers(), env)
    assert step.path == box / "ws-memory"
