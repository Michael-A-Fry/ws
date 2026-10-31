"""The docs integrity check: required files, links, images, anchors, banned strings and the pending list.
Banned values are built at run time so this file passes the PII gate and the check itself."""

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_docs_integrity.py"
spec = importlib.util.spec_from_file_location("check_docs_integrity", SCRIPT)
cdi = importlib.util.module_from_spec(spec)
sys.modules["check_docs_integrity"] = cdi
spec.loader.exec_module(cdi)

PRIVATE_IP = "192.168" + ".9.9"
TAILNET = "box." + "ts" + ".net"
OLD_NAME = "ws" + "-toolkit"
PRIVATE_NET = "10." + "0.0.0/8"


def make_repo(tmp_path, files=None):
    """A folder with every required file, plus `files` (path -> text)."""
    root = tmp_path / "repo"
    root.mkdir()
    for rel in cdi.REQUIRED_FILES:
        (root / rel).write_text("# " + rel + "\n", encoding="utf-8")
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def problems(root, pending=()):
    return cdi.run(root, set(pending))


def test_clean_repo_passes(tmp_path):
    root = make_repo(tmp_path, {"docs/a.md": "# A\n\nSee [README](../README.md#readmemd).\n"})
    assert problems(root) == []


def test_missing_required_file_is_reported_unless_pending(tmp_path):
    root = make_repo(tmp_path)
    (root / "SUPPORT.md").unlink()
    assert problems(root) == ["missing required policy file: SUPPORT.md"]
    assert problems(root, {"SUPPORT.md"}) == []


def test_missing_link_image_and_html_targets(tmp_path):
    text = '[a](nope.md)\n\n![b](img/none.png)\n\n<a href="gone.md">c</a>\n\n[ref]: missing-ref.md\n'
    root = make_repo(tmp_path, {"docs/a.md": text})
    errs = problems(root)
    for target in ("nope.md", "img/none.png", "gone.md", "missing-ref.md"):
        assert any(e == f"docs/a.md: missing link target: {target}" for e in errs), target
    assert len(errs) == 4


def test_pending_list_allows_missing_targets_only(tmp_path):
    root = make_repo(tmp_path, {"README.md": "[a](docs/new.md) [b](docs/other.md) [c](docs/dir/)\n"})
    errs = problems(root, {"docs/new.md", "docs/dir"})
    assert errs == ["README.md: missing link target: docs/other.md"]


def test_anchor_must_exist_and_repeated_headings_get_suffixes(tmp_path):
    target = "# Top\n\n## Set up `ws`\n\n## Notes\n\n## Notes\n\n```\n# not a heading\n```\n"
    src = "[ok](t.md#set-up-ws) [ok2](t.md#notes-1) [bad](t.md#nothing) [bad2](t.md#not-a-heading) [self](#here)\n\n# Here\n"
    root = make_repo(tmp_path, {"t.md": target, "s.md": src})
    errs = problems(root)
    assert sorted(errs) == [
        "s.md: missing anchor #not-a-heading in t.md",
        "s.md: missing anchor #nothing in t.md",
    ]


def test_links_in_code_and_external_links_are_ignored(tmp_path):
    text = (
        "`[x](nope.md)`\n\n```md\n[y](nope2.md)\n```\n\n[web](https://example.com/x.md) [mail](mailto:a@example.com)\n"
    )
    root = make_repo(tmp_path, {"docs/a.md": text})
    assert problems(root) == []


def test_link_leaving_the_repo_is_reported(tmp_path):
    root = make_repo(tmp_path, {"docs/a.md": "[x](../../outside.md)\n"})
    assert problems(root) == ["docs/a.md: link leaves the repository: ../../outside.md"]


def test_banned_strings(tmp_path):
    text = "\n".join(
        [
            f"pipx install {OLD_NAME}",
            "It is build-from-source only.",
            f"Open http://{TAILNET}:8080 for it.",
            f"The box is at {PRIVATE_IP}.",
        ]
    )
    root = make_repo(tmp_path, {"docs/a.md": text + "\n"})
    assert sorted(problems(root)) == [
        "docs/a.md:1: the old name ws-toolkit as an install target",
        "docs/a.md:2: 'build-from-source only' (there is an installer and a package)",
        "docs/a.md:3: a private host name",
        "docs/a.md:4: a private IP address",
    ]


def test_things_that_look_banned_but_are_fine(tmp_path):
    text = f"The name {OLD_NAME} is held on PyPI.\nBlocks {PRIVATE_NET} and 8.8.8.8.\nVersion 1.2.3.4 of it.\n"
    root = make_repo(tmp_path, {"docs/a.md": text})
    assert problems(root) == []


def test_command_line_exit_codes(tmp_path):
    root = make_repo(tmp_path, {"README.md": "[a](docs/new.md)\n"})
    pending = tmp_path / "pending.txt"
    pending.write_text("# comment\ndocs/new.md  # added elsewhere\n", encoding="utf-8")

    def cli(*args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root), *args], capture_output=True, text=True, check=False
        )

    assert cli().returncode == 1
    assert cli("--allow-missing", str(pending)).returncode == 0
    assert cli("--allow-missing", str(tmp_path / "none.txt")).returncode == 2
    (root / "docs").mkdir()
    (root / "docs" / "new.md").write_text("# New\n", encoding="utf-8")
    assert "now exists" in cli("--allow-missing", str(pending)).stdout


def test_two_stray_backticks_far_apart_do_not_hide_the_links_between_them(tmp_path):
    text = "# A\n\nA stray ` here.\n\nSee [gone](missing.md) and [bad](#nope).\n\nAnother stray ` there.\n"
    root = make_repo(tmp_path, {"docs/a.md": text})
    found = " ".join(problems(root))
    assert "missing.md" in found and "nope" in found


def test_code_spans_on_one_line_still_hide_their_links_including_multi_backtick_ones(tmp_path):
    text = "# A\n\nUse `[x](missing.md)` and ``[y](`gone.md`)`` in text.\n"
    root = make_repo(tmp_path, {"docs/a.md": text})
    assert problems(root) == []
