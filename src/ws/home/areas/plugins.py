"""Plugins: the plugins that are on, each with the asks it has raised. A slot: registered here, drawn once it is built.

A plugin's asks already arrive in Needs you, so this area waits until there is something about a plugin that a person
can't see there.
"""

from __future__ import annotations

from ws.home.areas._base import View

ID = "plugins"
ORDER = 5
TITLE = "Plugins"
READS = "plugins: only the ones that are on"
EMPTY = "No plugins are on."
OPAQUE = "A plugin's asks follow the opaque rule in Needs you."
ACTIONS = "None yet."
BUILT = False


def render(view: View) -> str:
    return ""
