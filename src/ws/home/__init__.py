"""ws home: one read-only data API (`ws.home.api`) that every home area, app and script reads through.

`records.py` is the shapes and the opaque rule, `sources/` is a folder of readers found by scanning it, `api.py`
merges what they return, and `cli.py` is `ws home data [--json]`. Nothing in this package writes a file.
"""
