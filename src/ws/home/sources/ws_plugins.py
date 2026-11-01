"""The plugins (the modules in the code): which exist, which are on, which are built and whether this region suits them."""

from __future__ import annotations

from ws import install, modules
from ws.home.records import Plugin
from ws.home.sources import BAD_FORMAT, SourceError

NAME = "ws_plugins"
PROVIDES = ("plugins",)
ORDER = 20


def available(env):
    return True, ""


def read(kind, env):
    environ = dict(env.environ)
    try:
        on = install.answers_from_profile(environ).modules
    except install.InstallError as e:
        raise SourceError(BAD_FORMAT, "the profile's modules can't be read") from e
    here = modules.region_of(environ)[0]
    return [
        Plugin(
            name=m.name,
            on=m.name in on,
            built=m.built,
            region_ok=not m.region or here is None or here in m.region,
        )
        for m in modules.REGISTRY.values()
    ]
