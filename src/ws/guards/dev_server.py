"""dev-server: a development server listens on this machine only. A command that starts one on every interface (or on an
address other than loopback) is denied, and the reason names the flag to add. The person can approve the command once,
and `allow_hosts` lists addresses that are fine to bind (a VPN or tailnet address, say).

The servers read, and what puts each on every interface:

    python -m http.server   no `--bind`/`-b` (the default is every interface), or one that isn't loopback
    php -S HOST:PORT        a HOST that isn't loopback, or `:PORT`
    serve (also npx serve)  no `-l`/`--listen` naming a loopback host (`-l localhost:3000`, `-l tcp://127.0.0.1:3000`)
    vite                    `--host` with no value, or a value that isn't loopback
    flask run               `--host`/`-h` (or `FLASK_RUN_HOST`) that isn't loopback
    jupyter lab|notebook    `--ip` (or `--ServerApp.ip`, `--NotebookApp.ip`) that isn't loopback, `*` included
    rails server            `-b`/`--binding` that isn't loopback
    uvicorn                 `--host` that isn't loopback
    hugo server             `--bind` that isn't loopback
    http-server             no `-a`/`--address`, or one that isn't loopback (the default is every interface)
    manage.py runserver     an `ADDR:PORT` whose address isn't loopback (`0.0.0.0:8000`, `0:8000`); a bare port is fine
    next dev|start          no `-H`/`--hostname` (the default is every interface), or one that isn't loopback
    streamlit run           no `--server.address`, or one that isn't loopback
    gunicorn                a `-b`/`--bind` that isn't loopback
    webpack serve           `--host` that isn't loopback (also `webpack-dev-server`)
    astro dev|preview       `--host` with no value, or a value that isn't loopback

Loopback is `localhost`, `127.0.0.0/8`, `::1` and `[::1]`. A server that listens on loopback unless told otherwise (vite,
flask, jupyter, rails, uvicorn, hugo, runserver, gunicorn, webpack, astro) is checked only when it is told otherwise. The server is found through `python -m`, a
path (`./node_modules/.bin/vite`), `python manage.py` and `django-admin`, and the launchers `npx`, `bunx`, `pnpx`, `uvx`, `npm exec`, `pnpm exec|dlx`,
`yarn exec|dlx`, `bun x`, `uv run`, `poetry run`, `pipenv run`, `pdm run`, `rye run`, `hatch run`, `conda run`, `pipx
run` and `bundle exec`. A script that holds the flags (`npm run dev`) isn't read, nor is a host set by a variable
(`env HOST=0.0.0.0 npm run dev`), nor a listener that isn't a development server (`socat`, `nc`).
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass

from ws import policy
from ws.guards import Finding, known_keys, text_list
from ws.policy import PolicyError

NAME = "dev-server"
EVENTS = ("pre",)
KINDS = ("command",)
PRIORITY = 10

_HOST = re.compile(r"[A-Za-z0-9._:\[\]-]+")
_LOOPBACK = "127.0.0.1"
_VERB_LAUNCHERS = {  # launcher -> the words after it that start a program
    "npm": {"exec"},
    "pnpm": {"exec", "dlx"},
    "yarn": {"exec", "dlx"},
    "bun": {"x"},
    "uv": {"run"},
    "poetry": {"run"},
    "pipenv": {"run"},
    "pdm": {"run"},
    "rye": {"run"},
    "hatch": {"run"},
    "conda": {"run"},
    "pipx": {"run"},
    "bundle": {"exec"},
}
_DIRECT_LAUNCHERS = {"npx", "bunx", "pnpx", "uvx"}
_LAUNCHER_VALUE = {  # options of a launcher that read a value, so the value isn't taken for the program
    "-p",
    "-c",
    "-n",
    "-w",
    "--package",
    "--call",
    "--from",
    "--with",
    "--python",
    "--index",
    "--directory",
    "--project",
    "--env-file",
    "--group",
    "--extra",
    "--with-requirements",
    "--prefix",
    "--cwd",
    "--name",
}
_PY_VALUE = {"-X", "-W"}
_JUPYTER_SUBS = {"lab", "notebook", "server", "nbclassic", "labhub"}
_JUPYTER_PROGRAMS = {"jupyter-lab", "jupyter-notebook", "jupyter-server", "jupyter-nbclassic", "jupyter-labhub"}
_JUPYTER_MODULES = {"notebook", "jupyterlab", "jupyter_server"}
_VITE_SUBS = {"dev", "build", "preview", "serve", "optimize"}
_ASTRO_SUBS = {"dev", "build", "preview", "check", "sync"}


@dataclass(frozen=True)
class Config:
    allow_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Problem:
    """What a server command does that is not allowed, and what to add or change."""

    server: str
    bound: str  # where it listens, as text
    fix: str  # the flag, spelled out


def host_of(addr: str) -> str:
    """The host of `HOST`, `HOST:PORT`, `[::1]:PORT`, `tcp://HOST:PORT` or `PORT`, lower case, no brackets. A port
    alone or an empty host is the empty text (every interface)."""
    addr = addr.strip().lower()
    if "://" in addr:
        addr = addr.split("://", 1)[1].split("/", 1)[0]
    if addr.startswith("["):
        return addr[1:].split("]", 1)[0]
    if addr.count(":") == 1:
        addr = addr.split(":", 1)[0]
    return "" if addr.isdecimal() else addr


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("allow_hosts",))
    hosts = text_list(table, "allow_hosts", where)
    for h in hosts:
        if not _HOST.fullmatch(h) or host_of(h) in ("", "0.0.0.0", "::", "*"):
            raise PolicyError(f"{where}: allow_hosts: {h!r} must be a host name or an address (not every interface)")
    return Config(tuple(host_of(h) for h in hosts))


def _loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _exposed(addr: str, cfg: Config) -> bool:
    """Does listening at `addr` (a host or address:port) reach beyond this machine, and is that not allowed?"""
    host = host_of(addr)
    return not (_loopback(host) or host in cfg.allow_hosts)


# --- reading a command's options ----------------------------------------------------------------------------------


def _values(args: list[str], longs: tuple[str, ...] = (), shorts: tuple[str, ...] = ()) -> list[str | None]:
    """The value of each use of an option, in order: `--bind X`, `--bind=X`, `-b X`, `-bX`. None: it is the last word,
    or the next word is another option. Stops at `--`."""
    out: list[str | None] = []
    i = 0
    while i < len(args):
        w = args[i]
        if w == "--":
            break
        nxt = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else None
        if w in longs or w in shorts:
            out.append(nxt)
            i += 1 if nxt is None else 2
            continue
        glued = next((w[len(o) + 1 :] for o in longs if w.startswith(o + "=")), None)
        if glued is None:
            glued = next((w[len(o) :].removeprefix("=") for o in shorts if w.startswith(o) and len(w) > len(o)), None)
        if glued is not None:
            out.append(glued)
        i += 1
    return out


def _last(args, longs=(), shorts=()):
    """(whether the option is there, its last value)."""
    found = _values(args, longs, shorts)
    return (True, found[-1]) if found else (False, None)


def _bound(server: str, args, longs, shorts, cfg: Config, fix: str, *, default_open: bool) -> Problem | None:
    """A `--bind`-style option: a problem when its value is exposed, or when it is missing and `default_open`."""
    given, value = _last(args, longs, shorts)
    if not given:
        return Problem(server, "every interface (the default)", fix) if default_open else None
    if value is not None and _exposed(value, cfg):
        return Problem(server, value or "every interface", fix)
    return None


# --- one check for each server ------------------------------------------------------------------------------------


def _http_server(args, env, cfg):
    if "-h" in args:
        return None
    return _bound("python -m http.server", args, ("--bind",), ("-b",), cfg, "--bind 127.0.0.1", default_open=True)


def _legacy_http_server(args, env, cfg):
    return Problem(
        "python -m SimpleHTTPServer", "every interface (the default)", "python3 -m http.server --bind 127.0.0.1"
    )


def _php(args, env, cfg):
    for value in _values(args, (), ("-S",)):
        if value is not None and _exposed(value, cfg):
            port = value.rsplit(":", 1)[-1] if ":" in value else "8000"
            return Problem("php -S", value, f"-S localhost:{port if port.isdecimal() else '8000'}")
    return None


def _serve(args, env, cfg):
    fix = "-l localhost:3000"
    if "-h" in args:
        return None
    listens = _values(args, ("--listen",), ("-l",))
    if not listens:
        return Problem("serve", "every interface (the default)", fix)
    for value in listens:
        if value is not None and not value.startswith("unix:") and _exposed(value, cfg):
            return Problem("serve", value, fix)
    return None


def _host_flag(server, args, cfg, subs):
    """A `--host` that takes a value or none: no value is every interface. `subs` are the words that follow it as a
    sub-command, so they aren't taken for its value."""
    fix = "--host 127.0.0.1 (or leave --host out)"
    i = 0
    while i < len(args):
        w = args[i]
        if w == "--":
            break
        if w == "--host":
            nxt = args[i + 1] if i + 1 < len(args) else ""
            value = nxt if _HOST.fullmatch(nxt) and not nxt.startswith("-") and nxt not in subs else None
            if value is None:
                return Problem(server, "every interface (--host with no value)", fix)
            if _exposed(value, cfg):
                return Problem(server, value, fix)
            i += 1
        elif w.startswith("--host=") and _exposed(w.partition("=")[2], cfg):
            return Problem(server, w.partition("=")[2] or "every interface", fix)
        i += 1
    return None


def _vite(args, env, cfg):
    positional = [a for a in args if not a.startswith("-")]
    if positional and positional[0] == "build":
        return None
    return _host_flag("vite", args, cfg, _VITE_SUBS)


def _astro(args, env, cfg):
    positional = [a for a in args if not a.startswith("-")]
    if not positional or positional[0] not in ("dev", "preview"):
        return None
    return _host_flag("astro", args, cfg, _ASTRO_SUBS)


def _flask(args, env, cfg):
    if "run" not in args:
        return None
    fix = "--host 127.0.0.1"
    from_env = next((e.partition("=")[2] for e in env if e.startswith("FLASK_RUN_HOST=")), None)
    if from_env is not None and _exposed(from_env, cfg):
        return Problem("flask run", f"{from_env or 'every interface'} (FLASK_RUN_HOST)", fix)
    return _bound("flask run", args[args.index("run") + 1 :], ("--host",), ("-h",), cfg, fix, default_open=False)


def _jupyter(args, env, cfg):
    ip = tuple(w.split("=", 1)[0] for w in args if re.fullmatch(r"--[A-Za-z]+\.ip(=.*)?", w)) + ("--ip",)
    return _bound("jupyter", args, ip, (), cfg, "--ip 127.0.0.1", default_open=False)


def _rails(args, env, cfg):
    if not args or args[0] not in ("s", "server"):
        return None
    return _bound("rails server", args[1:], ("--binding",), ("-b",), cfg, "-b 127.0.0.1", default_open=False)


def _uvicorn(args, env, cfg):
    return _bound("uvicorn", args, ("--host",), (), cfg, "--host 127.0.0.1", default_open=False)


def _hugo(args, env, cfg):
    if not args or args[0] not in ("server", "serve"):
        return None
    return _bound("hugo server", args[1:], ("--bind",), (), cfg, "--bind 127.0.0.1", default_open=False)


def _http_server_js(args, env, cfg):
    if "-h" in args:
        return None
    return _bound("http-server", args, ("--address",), ("-a",), cfg, "-a 127.0.0.1", default_open=True)


def _django(args, env, cfg):
    if "runserver" not in args:
        return None
    operands = [a for a in args[args.index("runserver") + 1 :] if not a.startswith("-")]
    if not operands or operands[0].isdecimal():  # no address, or a port alone: loopback
        return None
    if _exposed(operands[0], cfg):
        return Problem("runserver", operands[0], "runserver 127.0.0.1:8000")
    return None


def _next(args, env, cfg):
    if not args or args[0] not in ("dev", "start"):
        return None
    return _bound("next", args[1:], ("--hostname",), ("-H",), cfg, "-H 127.0.0.1", default_open=True)


def _streamlit(args, env, cfg):
    if "run" not in args:
        return None
    return _bound(
        "streamlit run", args, ("--server.address",), (), cfg, "--server.address 127.0.0.1", default_open=True
    )


def _gunicorn(args, env, cfg):
    for value in _values(args, ("--bind",), ("-b",)):
        if value is not None and not value.startswith(("unix:", "fd://")) and _exposed(value, cfg):
            return Problem("gunicorn", value or "every interface", "-b 127.0.0.1:8000")
    return None


def _webpack(args, env, cfg):
    if "serve" not in args and "s" not in args and "server" not in args:
        return None
    return _bound("webpack serve", args, ("--host",), (), cfg, "--host 127.0.0.1", default_open=False)


_PROGRAMS = {
    "php": _php,
    "serve": _serve,
    "vite": _vite,
    "flask": _flask,
    "rails": _rails,
    "uvicorn": _uvicorn,
    "hugo": _hugo,
    "http-server": _http_server_js,
    "manage.py": _django,
    "django-admin": _django,
    "next": _next,
    "streamlit": _streamlit,
    "gunicorn": _gunicorn,
    "webpack": _webpack,
    "webpack-dev-server": lambda args, env, cfg: _bound(
        "webpack-dev-server", args, ("--host",), (), cfg, "--host 127.0.0.1", default_open=False
    ),
    "astro": _astro,
    **{name: _jupyter for name in _JUPYTER_PROGRAMS},
}
_MODULES = {
    "http.server": _http_server,
    "SimpleHTTPServer": _legacy_http_server,
    "flask": _flask,
    "uvicorn": _uvicorn,
    "gunicorn": _gunicorn,
    "streamlit": _streamlit,
    **{name: _jupyter for name in _JUPYTER_MODULES},
}


# --- finding the server in a command ------------------------------------------------------------------------------


def _skip_options(words: list[str], i: int) -> int:
    """The index of the first word from `i` that isn't a launcher's option or its value; `--` ends the options."""
    while i < len(words) and words[i].startswith("-"):
        if words[i] == "--":
            return i + 1
        i += 2 if words[i] in _LAUNCHER_VALUE else 1
    return i


def _program(words: list[str], depth: int = 0) -> tuple[str, list[str], bool] | None:
    """(name, its arguments, whether it is a python module) of the program a command line runs, looking through
    launchers; None when there is none."""
    if not words or depth > 4:
        return None
    name = os.path.basename(words[0])
    if name in _DIRECT_LAUNCHERS:
        return _program(words[_skip_options(words, 1) :], depth + 1)
    if name in _VERB_LAUNCHERS and len(words) > 1:
        start = 2 if name == "uv" and words[1] == "tool" else 1  # `uv tool run PROGRAM`
        verbs = {"run"} if start == 2 else _VERB_LAUNCHERS[name]
        if start < len(words) and words[start] in verbs:
            return _program(words[_skip_options(words, start + 1) :], depth + 1)
        return None
    if re.fullmatch(r"python[0-9.]*|py", name):
        return _python(words)
    return name, words[1:], False


def _python(words: list[str]) -> tuple[str, list[str], bool] | None:
    """The module `python [options] -m MODULE ...` runs and its arguments, or the script `python [options] FILE ...`
    (by its file name); None for `-c`."""
    i = 1
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        w = words[i]
        if w == "-m" and i + 1 < len(words):
            return words[i + 1], words[i + 2 :], True
        if w.startswith("-m") and len(w) > 2:
            return w[2:], words[i + 1 :], True
        if w.startswith("-c"):
            return None
        i += 2 if w in _PY_VALUE else 1
    if i < len(words) and words[i] == "--":
        i += 1
    if i < len(words):
        return os.path.basename(words[i]), words[i + 1 :], False
    return None


def _problem(words: list[str], env: list[str], cfg: Config) -> Problem | None:
    found = _program(words)
    if found is None:
        return None
    name, args, module = found
    if "--help" in args or "--version" in args:
        return None
    if name == "jupyter":
        subs = [a for a in args if not a.startswith("-")]
        return _jupyter(args, env, cfg) if subs and subs[0] in _JUPYTER_SUBS else None
    check = (_MODULES if module else _PROGRAMS).get(name)
    return check(args, env, cfg) if check else None


def judge(call, cfg: Config, ctx) -> Finding | None:
    for words, env in policy.simple_commands_env(call.value):
        problem = _problem(words, env, cfg) if words else None
        if problem:
            return Finding(
                "deny",
                f"{problem.server} would listen on {problem.bound}, which other machines can reach. Add "
                f"`{problem.fix}` so it listens on this machine only. To bind an address on purpose, list it as "
                "`allow_hosts` under [guards.dev-server].",
                approvable=True,
            )
    return None
