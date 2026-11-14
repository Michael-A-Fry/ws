"""Stub secrets for tests: a registry, a store and run records in a temporary folder, with values made at run time.

Never the real store, never a real value: every value is `planted-<random hex>`, so a test can search output, logs and
errors for it and know a hit is a leak. Use it instead of writing a store or a secrets.toml by hand.
"""

from __future__ import annotations

import os
from pathlib import Path

from ws import broker
from ws import secrets as wsecrets


def planted_value() -> str:
    """A fresh value no real credential could be."""
    return "planted-" + os.urandom(12).hex()


class StubSecrets:
    """A registry, a store, a run record folder and an elevation file under `base`. Nothing outside it is touched."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.registry = self.base / "secrets.toml"
        self.store = wsecrets.Store(self.base / "secrets")
        self.records = self.base / "records"
        self.elevate_file = self.base / "elevate.json"
        self.values: dict[str, str] = {}
        self._tables: list[str] = []

    def register(self, name: str, hosts: list[str], value: bool = True, **keys: object) -> str | None:
        """Add `[name]` with these hosts and keys (tier, area, header, purposes...) and, unless value=False, plant a
        value. Returns the planted value (or None)."""
        lines = [f"[{name}]", "hosts = [" + ", ".join(f'"{h}"' for h in hosts) + "]"]
        for k, v in keys.items():
            if isinstance(v, list):
                lines.append(f"{k} = [" + ", ".join(f'"{x}"' for x in v) + "]")
            else:
                lines.append(f'{k} = "{v}"')
        self._tables.append("\n".join(lines))
        self.registry.write_text("\n\n".join(self._tables) + "\n", encoding="utf-8")
        if not value:
            return None
        v = planted_value()
        self.store.write(name, v)
        self.values[name] = v
        return v

    def record(self, run: str, area: str, tier: str, names: list[str], **kw) -> broker.RunRecord:
        """Write a run record the broker will trust (0600 in a 0700 folder, owned by this user)."""
        rec = broker.RunRecord(run=run, area=area, tier=tier, names=tuple(names), **kw)
        broker.write_run_record(self.records, rec)
        return rec

    def leaked(self, text: str) -> list[str]:
        """The names whose planted value appears in `text`."""
        return [n for n, v in self.values.items() if v in text]
