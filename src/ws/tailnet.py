"""The tailnet checks a one-shot phone page uses: which address it may listen on, and whether a client is the owner's.

A page that hands a person something (a secret to see once, say) listens on exactly one Tailscale address, never a
wildcard, a LAN or a public one, so an empty `tailscale ip -4` can never mean every interface. A client gets the page
only when `tailscale whois` names a login that starts with the owner setting and its device carries no tags (a tagged
device is a server, not a person's phone or laptop). The owner is a setting with no default: with none, nobody passes.
Any error from tailscale (missing, slow, odd output) is a no. Standard library only; tailscale is run, never imported.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from collections.abc import Callable

TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
TIMEOUT_S = 5


class TailnetError(ValueError):
    """This machine has no usable tailnet address. The message is safe to print."""


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S, check=False).stdout


def tailnet_ip(run: Callable[[list[str]], str] = _run) -> str:
    """This machine's Tailscale IPv4 address (inside the tailnet range), from `tailscale ip -4`. Raises TailnetError."""
    try:
        ip = ipaddress.ip_address(run(["tailscale", "ip", "-4"]).splitlines()[0].strip())
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        raise TailnetError("no Tailscale IPv4 address (is tailscale installed and up?)") from None
    if ip.version != 4 or ip not in TAILNET[0]:
        raise TailnetError("tailscale did not give an address in the tailnet range")
    return str(ip)


def bind_ok(addr: str | None, allow_loopback: bool = False) -> bool:
    """True for exactly one tailnet address (loopback too when allow_loopback, for a box with no tailscale and for
    tests). Never 0.0.0.0, '::', an empty string, a LAN or public address, or anything that is not one IP."""
    try:
        ip = ipaddress.ip_address((addr or "").strip())
    except ValueError:
        return False
    if ip.is_unspecified:
        return False
    if any(ip in net for net in TAILNET):
        return True
    return bool(allow_loopback and ip.is_loopback)


def whois_ok(ip: str, owner: str | None, run: Callable[[list[str]], str] = _run) -> bool:
    """True when `tailscale whois` says `ip` is the owner's own device: a login starting with `owner` and no tags.
    An empty or missing owner, and any error, is False."""
    if not owner:
        return False
    try:
        w = json.loads(run(["tailscale", "whois", "--json", ip]))
        login = (w.get("UserProfile") or {}).get("LoginName", "")
        tags = (w.get("Node") or {}).get("Tags") or []
    except Exception:  # noqa: BLE001  any failure of an outside tool is a no
        return False
    return isinstance(login, str) and login.startswith(owner) and not tags


def headers(csp: str) -> list[tuple[str, str]]:
    """The headers every page response carries: no-store, no referrer, no framing, nosniff and the page's own CSP."""
    return [
        ("Cache-Control", "no-store"),
        ("Pragma", "no-cache"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Frame-Options", "DENY"),
        ("X-Content-Type-Options", "nosniff"),
        ("Content-Security-Policy", csp),
    ]
