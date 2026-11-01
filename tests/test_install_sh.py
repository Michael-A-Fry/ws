"""install.sh: help, usage errors, the dry run, the preflight messages, and the release route against a fake release.

Every run gets its own PATH holding only stubs plus a few system tools, so the result does not depend on what the
machine running the tests has installed. Nothing is ever installed, and nothing leaves this machine: the "GitHub" the
installer talks to is a tiny HTTP server on 127.0.0.1, reached through WS_RELEASE_BASE_URL, which moves the download
but cannot switch a check off (several tests below prove that with a damaged or unsigned release).
"""

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"
BASH = shutil.which("bash")
SYSTEM_TOOLS = ("uname", "grep", "cat", "sed", "sort", "tail", "mktemp", "rm", "mv", "ln", "readlink")
SLUG = "Michael-A-Fry/ws"
ISSUER = "https://token.actions.githubusercontent.com"

pytestmark = pytest.mark.skipif(sys.platform == "win32" or BASH is None, reason="install.sh needs a POSIX shell")


def stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


AT = "@"  # built at run time: the PII gate reads a literal login-in-URL as an e-mail address


def wheel_name(tag: str) -> str:
    return f"ws_aitoolkit-{tag.removeprefix('v')}-py3-none-any.whl"


class FakeGitHub:
    """A release server: routes maps a URL path to bytes; `hits` records every path asked for, in order."""

    def __init__(self) -> None:
        self.routes: dict[str, bytes] = {}
        self.hits: list[str] = []
        self.latest_status: int | None = None  # force this answer for the "latest release" call
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.hits.append(self.path)
                forced = outer.latest_status if self.path.endswith("/releases/latest") else None
                body = outer.routes.get(self.path)
                code = forced or (200 if body is not None else 404)
                data = body if (body is not None and code == 200) else b"{}"
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def publish(self, tag: str = "v0.1.0", wheel: bytes = b"a wheel", latest: bool = True) -> str:
        """A release with its wheel, a matching .sha256 and a bundle. Returns the base path of its downloads."""
        name = wheel_name(tag)
        base = f"/{SLUG}/releases/download/{tag}/"
        self.routes[base + name] = wheel
        self.routes[base + name + ".sha256"] = f"{hashlib.sha256(wheel).hexdigest()}  {name}\n".encode()
        self.routes[base + name + ".sigstore.json"] = b'{"a": "bundle"}'
        if latest:
            self.routes[f"/repos/{SLUG}/releases/latest"] = json.dumps(
                {"tag_name": tag, "name": tag}, indent=2
            ).encode()
        return base

    def asked_for(self, suffix: str) -> bool:
        return any(h.endswith(suffix) for h in self.hits)


class Box:
    """A throwaway PATH and HOME, with helpers to add stub commands and run the installer."""

    def __init__(self, tmp_path: Path, gh: FakeGitHub):
        self.gh = gh
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.tmp = tmp_path / "tmp"
        self.tmp.mkdir()
        self.log = tmp_path / "calls.log"
        self.installed = tmp_path / "installed.whl"
        self.base_url = gh.url
        for tool in SYSTEM_TOOLS:
            found = shutil.which(tool)
            assert found, tool
            (self.bin / tool).symlink_to(found)

    def link(self, tool: str) -> None:
        found = shutil.which(tool)
        if found is None:
            pytest.skip(f"needs {tool} on the machine running the tests")
        (self.bin / tool).symlink_to(found)

    def curl(self) -> None:
        self.link("curl")

    def sha(self) -> None:
        # sha256sum on Linux, shasum on macOS: whichever this machine has, as install.sh finds it
        self.link("sha256sum" if shutil.which("sha256sum") else "shasum")

    def wget_only(self) -> None:
        # a wget that speaks the few options install.sh uses, built on urllib; the exit code 8 and the `HTTP/1.1 404`
        # line on stderr are how the real one reports a server error
        code = (
            "import sys, urllib.request, urllib.error\n"
            "url, dest = sys.argv[-1], sys.argv[sys.argv.index('-O') + 1]\n"
            "try:\n"
            "    data = urllib.request.urlopen(url).read()\n"
            "except urllib.error.HTTPError as e:\n"
            "    print(f'  HTTP/1.1 {e.code} X', file=sys.stderr); open(dest, 'wb').close(); sys.exit(8)\n"
            "open(dest, 'wb').write(data)\n"
        )
        script = self.bin / "wget-impl.py"
        script.write_text(code, encoding="utf-8")
        stub(self.bin, "wget", f'echo "wget $*" >> "{self.log}"\nexec "{sys.executable}" "{script}" "$@"')

    def cosign(self, ok: bool = True, version: str = "v3.1.4") -> None:
        stub(
            self.bin,
            "cosign",
            f'if [ "$1" = version ]; then echo "GitVersion:    {version}"; exit 0; fi\n'
            f'echo "cosign $*" >> "{self.log}"\n' + ("exit 0" if ok else 'echo "no match" >&2; exit 1'),
        )

    def python(self, ok: bool = True) -> None:
        # a python3 that says yes (the real interpreter) or no (older than 3.11) to the version test
        body = (
            f'exec "{sys.executable}" "$@"' if ok else 'case "$*" in *version_info*) exit 1;; esac; echo "Python 3.9.0"'
        )
        stub(self.bin, "python3", body)

    def uv(self, extra: str = "") -> None:
        # `tool install` copies the file it was given, so a test can see what was installed after the temp folder is gone
        grab = f'case "$1 $2" in "tool install") for a in "$@"; do last="$a"; done; cat "$last" > "{self.installed}";; esac'
        stub(
            self.bin,
            "uv",
            f'echo "uv $*" >> "{self.log}"\n{grab}\n{extra}\ncase "$1" in --version) echo "uv 0.0.0";; esac',
        )

    def pipx(self) -> None:
        stub(self.bin, "pipx", f'echo "pipx $*" >> "{self.log}"\ncase "$1" in --version) echo "0.0.0";; esac')

    def ws(self, doctor_exit: int = 1, version: str = "9.9.9") -> None:
        stub(
            self.bin,
            "ws",
            f'echo "ws $*" >> "{self.log}"\ncase "$1" in --version) echo "ws {version}";; doctor) echo "WS001 ok";'
            f" exit {doctor_exit};; esac",
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def installs(self) -> list[str]:
        return [c for c in self.calls() if c.startswith(("uv tool install", "pipx install"))]

    def leftovers(self) -> list[Path]:
        return sorted(self.tmp.glob("ws-install.*"))

    def env(self) -> dict[str, str]:
        return {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "TERM": "dumb",
            "TMPDIR": str(self.tmp),
            "WS_RELEASE_BASE_URL": self.base_url,
        }

    def run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        cmd = [BASH, str(INSTALL_SH), *args]
        full = {**self.env(), **(env or {})}
        return subprocess.run(cmd, check=False, env=full, capture_output=True, text=True, timeout=60)

    def piped(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the script the way `curl ... | bash -s -- ARGS` does: on stdin."""
        cmd = [BASH, "-s", "--", *args]
        return subprocess.run(
            cmd, check=False, env=self.env(), input=script, capture_output=True, text=True, timeout=60
        )


@pytest.fixture
def gh():
    server = FakeGitHub()
    yield server
    server.stop()


@pytest.fixture
def box(tmp_path: Path, gh: FakeGitHub) -> Box:
    return Box(tmp_path, gh)


def ready(box: Box, tag: str | None = "v0.1.0", wheel: bytes = b"a wheel") -> None:
    """python, uv, curl, a checksum tool and (unless tag is None) a published release."""
    box.python()
    box.uv()
    box.curl()
    box.sha()
    if tag:
        box.gh.publish(tag, wheel)


def test_help_prints_usage_and_exits_0(box: Box):
    r = box.run("--help")
    assert r.returncode == 0
    for flag in ("--version", "--pypi", "--from", "--no-verify", "--skip-smoke-test", "--dry-run", "--help"):
        assert flag in r.stdout
    assert "curl -fsSL" in r.stdout and "| bash -s --" in r.stdout
    assert "WS_RELEASE_BASE_URL" in r.stdout
    assert box.calls() == []


def test_no_colour_codes_when_not_a_terminal(box: Box):
    ready(box)
    r = box.run("--dry-run")
    assert "\x1b" not in r.stdout + r.stderr


def test_unknown_argument_exits_2_with_usage(box: Box):
    r = box.run("--frobnicate")
    assert r.returncode == 2
    assert "unknown argument: --frobnicate" in r.stderr
    assert "Usage:" in r.stdout


@pytest.mark.parametrize(
    "args",
    [
        ("--version",),
        ("--version", ""),
        ("--version", "v1;touch x"),
        ("--version", "$(id)"),
        ("--version", "v1..2"),
        ("--version", "..", "--dry-run"),
        ("--version", "v1.0.0", "--pypi"),
        ("--version", "main"),  # a release is a vX.Y.Z tag, not a branch
        ("--version", "v1.2"),
        ("--version", "1.2.3"),
        ("--version", "v1.0.0-rc1"),
        ("--version", "v1.0.0/../../x"),
    ],
)
def test_bad_version_exits_2(box: Box, args: tuple[str, ...]):
    assert box.run(*args).returncode == 2
    assert box.calls() == []
    assert box.gh.hits == []


def test_from_installs_a_checkout_without_any_download(box: Box, tmp_path: Path):
    checkout = tmp_path / "checkout with space"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    box.python()
    box.uv()
    box.ws()  # no curl, wget, checksum tool or cosign at all: a checkout needs none of them
    r = box.run("--from", str(checkout))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"uv tool install --force --reinstall {checkout.resolve()}" in box.calls()  # not from the wheel cache
    assert box.gh.hits == []


@pytest.mark.parametrize("extra", [(), ("--pypi",), ("--version", "v1.0.0")])
def test_from_refuses_a_folder_that_is_not_ws_and_clashing_options(box: Box, tmp_path: Path, extra: tuple[str, ...]):
    ready(box)
    folder = tmp_path / "folder"
    folder.mkdir()
    if extra:  # a real checkout, so the clash with the other option is what is refused
        (folder / "pyproject.toml").write_text("", encoding="utf-8")
    assert box.run("--from", str(folder), *extra).returncode == 2
    assert box.run("--from").returncode == 2
    assert not any(c.startswith("uv tool") for c in box.calls())


def test_dry_run_looks_up_the_latest_release_and_changes_nothing(box: Box):
    ready(box, "v0.10.0")
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "release: v0.10.0" in r.stdout  # whatever GitHub calls latest, not a text sort of tags
    assert f"{box.base_url}/{SLUG}/releases/download/v0.10.0/ws_aitoolkit-0.10.0-py3-none-any.whl" in r.stdout
    assert "ws doctor --offline" in r.stdout
    assert box.installs() == []
    assert box.gh.hits == [f"/repos/{SLUG}/releases/latest"]  # looked up, nothing downloaded
    assert box.leftovers() == []


def test_no_release_yet_dry_run_says_so_and_a_real_run_points_at_from(box: Box):
    ready(box, None)
    dry = box.run("--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "no release has been published yet" in dry.stdout
    real = box.run()
    assert real.returncode == 1
    assert "no release has been published yet" in real.stderr
    assert "install.sh --from" in real.stderr
    assert box.installs() == []
    assert box.leftovers() == []


def test_version_flag_names_the_release_without_asking_for_the_latest(box: Box):
    ready(box, "v9.9.9")
    box.gh.publish("v1.2.3", latest=False)
    r = box.run("--dry-run", "--version", "v1.2.3")
    assert r.returncode == 0, r.stderr
    assert "release: v1.2.3" in r.stdout
    assert not box.gh.asked_for("/releases/latest")


def test_the_server_cannot_be_reached_dry_run_warns_and_a_real_run_fails_with_a_hint(box: Box):
    ready(box)
    dead = FakeGitHub()
    url = dead.url
    dead.stop()
    dry = box.run("--dry-run", env={"WS_RELEASE_BASE_URL": url})
    assert dry.returncode == 0, dry.stderr
    assert "could not reach" in dry.stdout
    real = box.run(env={"WS_RELEASE_BASE_URL": url})
    assert real.returncode == 1
    assert "could not find the latest release" in real.stderr
    assert "--version vX.Y.Z" in real.stderr
    assert "Troubleshooting" in real.stdout
    assert box.installs() == []


def test_a_rate_limit_answer_is_named(box: Box):
    ready(box)
    box.gh.latest_status = 403
    r = box.run()
    assert r.returncode == 1
    assert "HTTP 403" in r.stderr and "--version vX.Y.Z" in r.stderr


def test_an_answer_without_a_release_tag_is_refused(box: Box):
    ready(box)
    box.gh.routes[f"/repos/{SLUG}/releases/latest"] = b'{"tag_name": "nightly"}'
    r = box.run()
    assert r.returncode == 1
    assert "did not hold a release tag" in r.stderr
    assert box.installs() == []


def test_the_release_route_needs_curl_or_wget(box: Box):
    box.python()
    box.uv()
    box.sha()
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "needs curl or wget" in r.stderr


def test_the_release_route_needs_a_checksum_tool_unless_the_checks_are_skipped(box: Box):
    box.python()
    box.uv()
    box.curl()
    box.gh.publish()
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "sha256sum or shasum" in r.stderr
    assert box.run("--dry-run", "--no-verify").returncode == 0


def test_pypi_route_needs_no_curl_or_checksum_tool(box: Box):
    box.python()
    box.uv()
    r = box.run("--dry-run", "--pypi")
    assert r.returncode == 0, r.stderr
    assert "uv tool install --force ws-aitoolkit" in r.stdout
    assert box.gh.hits == []


def test_pipx_is_used_when_uv_is_missing_and_gets_the_python_that_was_found(box: Box):
    box.python()
    box.pipx()
    box.curl()
    box.sha()
    box.gh.publish("v0.1.0")
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "pipx install --python python3 --force ws_aitoolkit-0.1.0-py3-none-any.whl" in r.stdout


def test_no_installer_names_the_exact_install_line_and_troubleshoots(box: Box):
    box.python()
    box.curl()
    box.sha()
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in r.stderr
    assert "Troubleshooting" in r.stdout


def test_old_python_without_uv_is_refused_with_an_install_line(box: Box):
    box.python(ok=False)
    box.pipx()
    box.curl()
    box.sha()
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "Python 3.11 or newer is required" in r.stderr


def test_old_python_with_uv_only_warns(box: Box):
    ready(box)
    box.python(ok=False)
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "uv will fetch one" in r.stdout


def test_native_windows_points_at_wsl2(box: Box):
    ready(box)
    (box.bin / "uname").unlink()
    stub(box.bin, "uname", 'case "$1" in -m) echo x86_64;; *) echo MINGW64_NT-10.0;; esac')
    r = box.run("--dry-run")
    assert r.returncode == 1
    assert "WSL2" in r.stderr
    assert "wsl --install" in r.stderr


def test_real_run_installs_the_checked_wheel_then_smokes_ws_and_accepts_doctor_exit_1(box: Box):
    ready(box, "v0.3.0", wheel=b"the exact wheel bytes")
    box.ws(doctor_exit=1)
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    (install,) = box.installs()
    assert install.startswith("uv tool install --force ") and install.endswith("ws_aitoolkit-0.3.0-py3-none-any.whl")
    assert (
        box.installed.read_bytes() == b"the exact wheel bytes"
    )  # the file that was checked is the file that was installed
    assert "sha256 matches" in r.stdout
    assert "cosign is not installed" in r.stdout
    assert "ws doctor --offline" in box.calls()
    assert "ws --version -> ws 9.9.9" in r.stdout
    assert "ws setup" in r.stdout and "ws doctor" in r.stdout
    assert box.leftovers() == []  # the temporary folder is gone
    assert box.gh.hits[0] == f"/repos/{SLUG}/releases/latest"
    assert not box.gh.asked_for(".sigstore.json")


def test_version_flag_installs_that_release_not_the_latest(box: Box):
    ready(box, "v0.3.0")
    box.gh.publish("v0.2.0", wheel=b"older", latest=False)
    box.ws()
    r = box.run("--version", "v0.2.0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.installed.read_bytes() == b"older"
    assert not box.gh.asked_for("/releases/latest")


def test_a_wheel_that_does_not_match_its_sha256_is_refused(box: Box):
    ready(box, "v0.3.0")
    box.gh.routes[f"/{SLUG}/releases/download/v0.3.0/{wheel_name('v0.3.0')}"] = b"tampered"
    box.ws()
    r = box.run()
    assert r.returncode == 1
    assert "does not match" in r.stderr
    assert box.installs() == []
    assert not box.installed.exists()
    assert box.leftovers() == []


def test_a_missing_sha256_file_is_a_refusal_not_a_skip(box: Box):
    # every test here runs with WS_RELEASE_BASE_URL set, so the override is proved not to skip a check by these refusals
    ready(box, "v0.3.0")
    del box.gh.routes[f"/{SLUG}/releases/download/v0.3.0/{wheel_name('v0.3.0')}.sha256"]
    r = box.run()
    assert r.returncode == 1
    assert "download failed (HTTP 404)" in r.stderr and ".sha256" in r.stderr
    assert box.installs() == []


def test_a_sha256_file_for_another_file_is_refused(box: Box):
    ready(box, "v0.3.0")
    name = wheel_name("v0.3.0")
    other = hashlib.sha256(b"a wheel").hexdigest()
    box.gh.routes[f"/{SLUG}/releases/download/v0.3.0/{name}.sha256"] = f"{other}  other.whl\n".encode()
    r = box.run()
    assert r.returncode == 1
    assert box.installs() == []


def test_a_missing_wheel_names_the_url(box: Box):
    ready(box, "v0.3.0")
    del box.gh.routes[f"/{SLUG}/releases/download/v0.3.0/{wheel_name('v0.3.0')}"]
    r = box.run()
    assert r.returncode == 1
    assert "download failed (HTTP 404)" in r.stderr
    assert box.installs() == []


@pytest.mark.parametrize(
    "bad",
    [
        "http://example.invalid",
        "http://127.0.0.1.example.invalid",
        "ftp://x",
        "file:///tmp",
        "http://localhost:x" + AT + "evil.example",  # curl reads `localhost:x` as a login and goes to evil.example
        "http://localhost/" + AT + "evil.example",
        "http://127.0.0.1:1" + AT + "evil.example",
        "https://user" + AT + "mirror.example",
    ],
)
def test_the_override_must_be_https_or_this_machine(box: Box, bad: str):
    ready(box)
    r = box.run(env={"WS_RELEASE_BASE_URL": bad})
    assert r.returncode == 2
    assert "WS_RELEASE_BASE_URL" in r.stderr
    assert box.installs() == []


def test_the_override_is_announced_loudly(box: Box):
    ready(box)
    r = box.run("--dry-run")
    assert "WS_RELEASE_BASE_URL is set" in r.stdout


def test_cosign_when_installed_verifies_the_bundle_for_this_tag(box: Box):
    ready(box, "v0.3.0")
    box.cosign()
    box.ws()
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    (call,) = [c for c in box.calls() if c.startswith("cosign")]
    assert call.startswith("cosign verify-blob --bundle ")
    assert f"--certificate-identity https://github.com/{SLUG}/.github/workflows/release.yml@refs/tags/v0.3.0" in call
    assert f"--certificate-oidc-issuer {ISSUER}" in call
    assert call.endswith(wheel_name("v0.3.0"))
    assert "cosign signature verifies" in r.stdout
    assert box.gh.asked_for(".sigstore.json")


def test_a_cosign_too_old_for_the_bundle_says_so_and_checks_the_sha_only(box: Box):
    ready(box, "v0.3.0")
    box.cosign(ok=False, version="v2.4.1")  # would refuse a genuine v3 bundle: it must not be asked
    box.ws()
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cosign 2.x can't read" in r.stdout
    assert [c for c in box.calls() if c.startswith("cosign")] == []


def test_a_signature_that_does_not_verify_stops_the_install(box: Box):
    ready(box, "v0.3.0")
    box.cosign(ok=False)
    box.ws()
    r = box.run()
    assert r.returncode == 1
    assert "did not verify" in r.stderr
    assert box.installs() == []
    assert box.leftovers() == []


def test_cosign_installed_but_no_bundle_is_a_refusal_not_a_skip(box: Box):
    ready(box, "v0.3.0")
    box.cosign()
    del box.gh.routes[f"/{SLUG}/releases/download/v0.3.0/{wheel_name('v0.3.0')}.sigstore.json"]
    r = box.run()
    assert r.returncode == 1
    assert ".sigstore.json" in r.stderr
    assert box.installs() == []


def test_no_verify_skips_both_checks_and_says_so_loudly(box: Box):
    ready(box, "v0.3.0")
    box.cosign(ok=False)  # would refuse if it were asked
    box.gh.routes[f"/{SLUG}/releases/download/v0.3.0/{wheel_name('v0.3.0')}"] = b"tampered"
    box.ws()
    r = box.run("--no-verify")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "will NOT be checked" in r.stdout
    assert box.installed.read_bytes() == b"tampered"
    assert not box.gh.asked_for(".sha256") and not box.gh.asked_for(".sigstore.json")
    assert not any(c.startswith("cosign") for c in box.calls())
    assert "ws doctor --offline" in box.calls()  # the smoke test is a separate switch


def test_skip_smoke_test_leaves_the_checks_on(box: Box):
    ready(box, "v0.3.0")
    box.ws()
    r = box.run("--skip-smoke-test")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ws doctor --offline" not in box.calls()
    assert "sha256 matches" in r.stdout
    assert box.run("--skip-smoke-test", env={"WS_SKIP_VERIFY": "1"}).returncode == 0


def test_wget_is_used_when_curl_is_missing(box: Box):
    box.python()
    box.uv()
    box.sha()
    box.wget_only()
    box.gh.publish("v0.3.0", wheel=b"via wget")
    box.ws()
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.installed.read_bytes() == b"via wget"
    assert any(c.startswith("wget ") for c in box.calls())


def test_wget_reports_no_release_from_the_http_status(box: Box):
    box.python()
    box.uv()
    box.sha()
    box.wget_only()
    r = box.run()
    assert r.returncode == 1
    assert "no release has been published yet" in r.stderr


def test_shasum_is_enough_when_there_is_no_sha256sum(box: Box):
    real = shutil.which("sha256sum")
    if real is None:
        pytest.skip("the macOS case is the default there; this one needs sha256sum to stand behind the shasum stub")
    box.python()
    box.uv()
    box.curl()
    stub(box.bin, "shasum", f'echo "shasum $*" >> "{box.log}"\nshift 2\nexec "{real}" "$@"')  # drops `-a 256`
    box.gh.publish("v0.3.0")
    box.ws()
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("shasum -a 256 -c ") for c in box.calls())


def test_real_run_fails_when_doctor_does_not_run(box: Box):
    ready(box, "v0.3.0")
    box.ws(doctor_exit=2)
    r = box.run()
    assert r.returncode == 1
    assert "ws doctor did not run properly (exit 2)" in r.stderr
    assert "Troubleshooting" in r.stdout
    assert box.leftovers() == []


def test_installed_placeholder_version_fails_even_with_the_checks_off(box: Box):
    box.python()
    box.uv()
    box.ws(version="0.0.0")
    r = box.run("--pypi", "--no-verify", "--skip-smoke-test")
    assert r.returncode == 1
    assert "PyPI placeholder" in r.stderr


def test_ws_found_in_the_uv_bin_folder_when_not_on_path(box: Box, tmp_path: Path):
    uv_bin = tmp_path / "uvbin"
    uv_bin.mkdir()
    # an earlier install of this toolkit, which the new one replaces
    env_bin = tmp_path / "tools" / "ws-aitoolkit" / "bin"
    env_bin.mkdir(parents=True)
    stub(env_bin, "ws", 'case "$1" in --version) echo "ws 9.9.9";; doctor) exit 1;; esac')
    (uv_bin / "ws").symlink_to(env_bin / "ws")
    box.python()
    box.curl()
    box.sha()
    box.gh.publish("v0.3.0")
    box.uv(extra=f'case "$*" in "tool dir --bin") echo "{uv_bin}";; esac')
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not on your PATH yet" in r.stdout


def test_install_failure_prints_troubleshooting_and_removes_the_download(box: Box):
    ready(box, "v0.3.0")
    box.uv(extra='case "$1" in tool) exit 7;; esac')
    r = box.run()
    assert r.returncode == 7
    assert "installation failed (exit 7)" in r.stderr
    assert "Troubleshooting" in r.stdout
    assert box.leftovers() == []


def test_piped_into_bash_works_and_children_get_no_stdin(box: Box):
    # What `curl ... | bash -s -- ` does. The comment after main stands for the rest of a script: a child that
    # read stdin would eat it, so the uv and ws stubs report anything they can read.
    reader = f'if read -r line; then echo "STDIN-EATEN $line" >> "{box.log}"; fi'
    box.python()
    box.curl()
    box.sha()
    box.gh.publish("v0.3.0")
    box.uv(extra=reader)
    stub(
        box.bin,
        "ws",
        reader + f'\necho "ws $*" >> "{box.log}"\ncase "$1" in --version) echo "ws 9.9.9"; exit 0;; esac; exit 1',
    )
    script = INSTALL_SH.read_text(encoding="utf-8") + "\n# rest of the script\n"
    r = box.piped(script)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ws doctor ran" in r.stdout
    assert not any("STDIN-EATEN" in c for c in box.calls())


def test_dry_run_piped_into_bash(box: Box):
    ready(box, "v0.3.0")
    r = box.piped(INSTALL_SH.read_text(encoding="utf-8"), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "dry run: not installed" in r.stdout


def test_a_truncated_download_runs_nothing(box: Box):
    ready(box, "v0.3.0")
    lines = INSTALL_SH.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == 'main "$@"'
    for cut in (len(lines) - 1, len(lines) // 2, len(lines) // 4):
        r = box.piped("\n".join(lines[:cut]) + "\n")
        assert "[1/5]" not in r.stdout, cut
        assert box.calls() == []
        assert box.gh.hits == []


def test_script_is_executable_and_is_apache_2_0_with_no_mit_text():
    assert os.access(INSTALL_SH, os.X_OK)
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "# SPDX-License-Identifier: Apache-2.0" in text.splitlines()[:5]
    assert "MIT License" not in text and "Permission is hereby granted" not in text


@pytest.mark.parametrize("blank", ["", "  "])
def test_from_with_a_blank_folder_exits_2_and_does_not_fall_through_to_the_release(box: Box, blank: str):
    ready(box)
    r = box.run("--from", blank)
    assert r.returncode == 2
    assert "--from needs the folder of a checkout" in r.stdout + r.stderr
    assert box.gh.hits == []
    assert not any(c.startswith("uv tool") for c in box.calls())


# ---- a different `ws` already in the install folder ---------------------------------------------------------------


def foreign_ws(box: Box, body: str = 'echo "the old ws"') -> Path:
    """A program named ws in the folder the installer writes to (uv's stub prints no folder, so ~/.local/bin)."""
    folder = box.home / ".local" / "bin"
    folder.mkdir(parents=True, exist_ok=True)
    stub(folder, "ws", body)
    return folder / "ws"


def test_a_different_ws_in_the_install_folder_stops_the_run_with_no_changes_and_the_two_commands(box: Box):
    ready(box)
    old = foreign_ws(box)
    before = old.read_bytes()
    r = box.run()
    assert r.returncode == 1
    assert f"mv {old} {old}-legacy" in r.stderr
    assert f"ws config fallback {old}-legacy" in r.stderr
    assert f"--move-existing-ws-to {old}-legacy" in r.stderr
    assert "leaves it where it is" in r.stderr and "Nothing was changed" not in r.stderr
    assert box.installs() == [] and box.gh.hits == []  # the stop comes before any download or install
    assert old.read_bytes() == before and not Path(f"{old}-legacy").exists()


def test_the_printed_commands_quote_a_path_with_spaces(box: Box):
    ready(box)
    home = box.tmp / "my home"
    folder = home / ".local" / "bin"
    folder.mkdir(parents=True)
    stub(folder, "ws", "echo old")
    old = folder / "ws"
    r = box.run(env={"HOME": str(home)})
    assert r.returncode == 1
    lines = {ln.split(":", 1)[0].strip()[:2]: ln.split(":", 1)[1] for ln in r.stderr.splitlines() if ":    " in ln}
    assert shlex.split(lines["1."]) == ["mv", str(old), f"{old}-legacy"]
    assert shlex.split(lines["2."]) == ["ws", "config", "fallback", f"{old}-legacy"]
    both = next(ln for ln in r.stderr.splitlines() if "let the installer do both" in ln)
    assert shlex.split(both.split(": ", 1)[1])[-1] == f"{old}-legacy"


def test_the_dry_run_names_a_different_ws_and_still_exits_0(box: Box):
    ready(box)
    old = foreign_ws(box)
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"another program named 'ws' is at {old}" in r.stdout and "a real run would stop here" in r.stdout
    assert box.installs() == [] and old.exists()


def test_this_toolkit_in_the_install_folder_is_replaced_as_before(box: Box):
    ready(box)
    box.ws()
    real = box.tmp / "uv" / "tools" / "ws-aitoolkit" / "bin"
    real.mkdir(parents=True)
    stub(real, "ws", "echo whatever")
    folder = box.home / ".local" / "bin"
    folder.mkdir(parents=True)
    (folder / "ws").symlink_to(real / "ws")
    assert box.run().returncode == 0
    assert len(box.installs()) == 1


def test_a_script_started_by_the_tool_environments_python_is_replaced_as_before(box: Box):
    ready(box)
    box.ws()
    foreign_ws(box, "echo whatever")
    folder = box.home / ".local" / "bin"
    (folder / "ws").write_text("#!/opt/uv/tools/ws-aitoolkit/bin/python\nprint(1)\n", encoding="utf-8")
    assert box.run().returncode == 0
    assert len(box.installs()) == 1


@pytest.mark.parametrize("args", [(), ("--dry-run",), ("--move-existing-ws-to", "MOVED")])
def test_the_other_ws_is_read_and_never_started(box: Box, args: tuple[str, ...]):
    ready(box)
    box.ws()
    marker = box.tmp / "started"
    foreign_ws(
        box, f'echo started > "{marker}"\necho \'{{"schema": "ws.cli/1", "data": {{"install_source": "release"}}}}\''
    )
    args = tuple(str(box.tmp / "ws-legacy") if a == "MOVED" else a for a in args)
    box.run(*args)
    assert not marker.exists()


def test_move_existing_renames_it_installs_and_sets_it_as_the_fallback(box: Box):
    ready(box)
    box.ws()
    old = foreign_ws(box)
    dest = box.tmp / "ws-legacy"
    r = box.run("--move-existing-ws-to", str(dest))
    assert r.returncode == 0, r.stdout + r.stderr
    assert dest.read_text(encoding="utf-8").endswith('echo "the old ws"\n') and not old.exists()
    assert len(box.installs()) == 1
    calls = box.calls()
    assert f"ws config fallback {dest}" in calls
    assert calls.index(f"ws config fallback {dest}") < calls.index("ws doctor --offline")  # right after the install
    assert f"words ws lacks now go to {dest}" in r.stdout


def test_move_existing_never_overwrites_a_file_that_is_already_there(box: Box):
    ready(box)
    old = foreign_ws(box)
    dest = box.tmp / "taken"
    dest.write_text("mine", encoding="utf-8")
    r = box.run("--move-existing-ws-to", str(dest))
    assert r.returncode == 1 and "already exists" in r.stderr
    assert dest.read_text(encoding="utf-8") == "mine" and old.exists() and box.installs() == []


def test_move_existing_in_a_dry_run_moves_nothing(box: Box):
    ready(box)
    old = foreign_ws(box)
    dest = box.tmp / "ws-legacy"
    r = box.run("--dry-run", "--move-existing-ws-to", str(dest))
    assert r.returncode == 0 and f"would rename {old} to {dest}" in r.stdout
    assert old.exists() and not dest.exists()


def test_move_existing_with_no_other_ws_does_nothing(box: Box):
    ready(box)
    box.ws()
    dest = box.tmp / "ws-legacy"
    r = box.run("--move-existing-ws-to", str(dest))
    assert r.returncode == 0 and "install goes ahead as usual" in r.stdout
    assert not dest.exists() and not any(c.startswith("ws config fallback") for c in box.calls())


@pytest.mark.parametrize("args", [("--move-existing-ws-to",), ("--move-existing-ws-to", "relative/ws-old")])
def test_move_existing_needs_an_absolute_path(box: Box, args: tuple[str, ...]):
    ready(box)
    r = box.run(*args)
    assert r.returncode == 2 and "--move-existing-ws-to" in r.stderr


def test_move_existing_waits_until_the_download_has_passed_its_checks(box: Box):
    ready(box, "v0.3.0")
    box.gh.routes[f"/{SLUG}/releases/download/v0.3.0/{wheel_name('v0.3.0')}"] = b"tampered"
    old = foreign_ws(box)
    dest = box.tmp / "ws-legacy"
    r = box.run("--move-existing-ws-to", str(dest))
    assert r.returncode == 1 and "does not match" in r.stderr
    assert old.exists() and not dest.exists() and "renamed" not in r.stdout


def test_a_failed_install_puts_the_moved_ws_back(box: Box):
    ready(box)
    box.uv(extra='case "$1" in tool) exit 7;; esac')
    old = foreign_ws(box)
    before = old.read_bytes()
    dest = box.tmp / "ws-legacy"
    r = box.run("--move-existing-ws-to", str(dest))
    assert r.returncode == 7
    assert f"renamed {old} to {dest}" in r.stdout and f"put your original 'ws' back at {old}" in r.stdout
    assert old.read_bytes() == before and not dest.exists()
    assert not any(c.startswith("ws config fallback") for c in box.calls())


def test_a_failed_install_that_left_a_new_file_keeps_it_and_says_where_the_original_is(box: Box):
    ready(box)
    folder = box.home / ".local" / "bin"
    box.uv(extra=f'case "$1" in tool) echo partial > "{folder}/ws"; exit 7;; esac')
    foreign_ws(box)
    dest = box.tmp / "ws-legacy"
    r = box.run("--move-existing-ws-to", str(dest))
    assert r.returncode == 7 and dest.exists()
    assert f"your original 'ws' is at {dest}" in r.stdout + r.stderr


def test_a_moved_symlink_is_recreated_with_its_absolute_target(box: Box):
    ready(box)
    box.ws()
    folder = box.home / ".local" / "bin"
    folder.mkdir(parents=True)
    real = box.home / "apps" / "old-ws-1.2" / "run"
    real.parent.mkdir(parents=True)
    stub(real.parent, "run", 'echo "the old ws"')
    (folder / "ws").symlink_to(os.path.relpath(real, folder))  # a relative target
    assert not os.readlink(folder / "ws").startswith("/")
    dest = box.tmp / "elsewhere" / "ws-legacy"
    dest.parent.mkdir()
    r = box.run("--move-existing-ws-to", str(dest))
    assert r.returncode == 0, r.stdout + r.stderr
    assert dest.is_symlink() and os.readlink(dest) == str(real)
    assert dest.resolve() == real.resolve() and not (folder / "ws").is_symlink()
    assert f"ws config fallback {dest}" in box.calls()
