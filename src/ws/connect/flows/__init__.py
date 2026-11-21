"""The flows that ship with ws, each a module holding its TOML text in `TOML`; `ws.connect.spec` loads them.

A flow is Python data, not a `.toml` file, because `ws secret wall` copies only `.py` files. Flows outside the package are plain `.toml` files read by `spec.load(..., folder=...)`.
"""
