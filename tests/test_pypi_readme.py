"""The README PyPI shows has no relative link: scripts/pypi_readme.py pins them to the tag, and checks built files."""

from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("pypi_readme", ROOT / "scripts" / "pypi_readme.py")
assert _spec and _spec.loader
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

TAG = "v9.8.7"
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _metadata(body: str) -> str:
    return (
        f"Metadata-Version: 2.4\nName: ws-aitoolkit\nVersion: 9.8.7\nDescription-Content-Type: text/markdown\n\n{body}"
    )


def _wheel(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "ws_aitoolkit-9.8.7-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ws/__init__.py", "")
        zf.writestr("ws_aitoolkit-9.8.7.dist-info/METADATA", _metadata(body))
    return path


def _sdist(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "ws_aitoolkit-9.8.7.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for name, text in (("ws_aitoolkit-9.8.7/PKG-INFO", _metadata(body)), ("ws_aitoolkit-9.8.7/README.md", body)):
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def test_the_repo_readme_has_relative_links_so_the_check_bites():
    left = pr.relative_targets(README)
    assert "CHANGELOG.md" in left and "docs/README.md" in left


def test_the_rewritten_readme_has_no_relative_link_or_image():
    out = pr.rewrite(README, TAG)
    assert pr.relative_targets(out) == []
    assert f"https://github.com/Michael-A-Fry/ws/blob/{TAG}/CHANGELOG.md" in out
    assert f"https://github.com/Michael-A-Fry/ws/blob/{TAG}/docs/README.md" in out
    assert "/blob/main/docs" not in out


def test_links_pin_to_the_tag_images_to_raw_and_anchors_to_the_readme():
    text = (
        'See [the docs](docs/usage.md "Usage") and [Install](#install), [ref][r].\n'
        "![The setup screen](./docs/assets/setup.gif)\n"
        '<img src="docs/assets/gate.png" alt="gate"> <a href="SECURITY.md">sec</a>\n'
        "[r]: CONTRIBUTING.md\n"
        "[abs](https://example.org/x) [mail](mailto:a@b) [`ws doctor`](docs/reference/cli.md)\n"
    )
    out = pr.rewrite(text, TAG)
    base = f"https://github.com/Michael-A-Fry/ws/blob/{TAG}"
    raw = f"https://raw.githubusercontent.com/Michael-A-Fry/ws/{TAG}"
    assert f'[the docs]({base}/docs/usage.md "Usage")' in out
    assert f"[Install]({base}/README.md#install)" in out
    assert f"![The setup screen]({raw}/docs/assets/setup.gif)" in out
    assert f'src="{raw}/docs/assets/gate.png"' in out and f'href="{base}/SECURITY.md"' in out
    assert f"[r]: {base}/CONTRIBUTING.md" in out
    assert "[abs](https://example.org/x)" in out and "[mail](mailto:a@b)" in out
    assert f"[`ws doctor`]({base}/docs/reference/cli.md)" in out
    assert pr.relative_targets(out) == []


def test_code_is_left_alone():
    text = "Run `[x](docs/a.md)` then:\n\n```md\n[y](docs/b.md)\n```\n\n~~~\n![z](c.png)\n~~~\n"
    assert pr.rewrite(text, TAG) == text
    assert pr.relative_targets(text) == []


def test_a_tag_that_is_not_vxyz_is_refused():
    with pytest.raises(ValueError):
        pr.rewrite(README, "main")
    assert pr.main(["rewrite", "--tag", "main", str(ROOT / "README.md")]) == 2


@pytest.mark.parametrize("build", [_wheel, _sdist])
def test_check_reads_the_description_out_of_built_files(tmp_path, build, capsys):
    assert pr.main(["check", str(build(tmp_path, README))]) == 1
    assert "relative link CHANGELOG.md" in capsys.readouterr().out
    clean = tmp_path / "clean"
    clean.mkdir()
    assert pr.main(["check", str(build(clean, pr.rewrite(README, TAG)))]) == 0


def test_a_built_file_without_a_description_is_a_usage_error(tmp_path):
    path = tmp_path / "ws_aitoolkit-9.8.7-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ws_aitoolkit-9.8.7.dist-info/METADATA", "Metadata-Version: 2.4\nName: ws-aitoolkit\n")
    assert pr.main(["check", str(path)]) == 2


def test_the_rewrite_runs_in_place(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("[Changelog](CHANGELOG.md)\n", encoding="utf-8")
    assert pr.main(["rewrite", "--tag", TAG, str(path)]) == 0
    assert (
        path.read_text(encoding="utf-8")
        == f"[Changelog](https://github.com/Michael-A-Fry/ws/blob/{TAG}/CHANGELOG.md)\n"
    )


def test_the_release_workflow_pins_before_the_build_and_checks_after_it():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    stamp = wf.index("src/ws/_build.py")
    pin = wf.index("scripts/pypi_readme.py rewrite")
    build = wf.index("run: uv build")
    check = wf.index("scripts/pypi_readme.py check dist/*.whl dist/*.tar.gz")
    assert stamp < pin < build < check


def test_pypi_shows_the_project_links():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for key in ("Source", "Documentation", "Changelog", "Issues", "Security"):
        assert f'\n{key} = "https://github.com/Michael-A-Fry/ws' in text
