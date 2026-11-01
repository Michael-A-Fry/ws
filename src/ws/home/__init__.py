"""ws home: the home page, and the one read-only data API (`ws.home.api`) it and every other reader use.

`records.py` is the shapes and the opaque rule, `sources/` is a folder of readers found by scanning it, `api.py`
merges what they return, `areas/` draw the page from those records, `openers/` open a session where it lives, and
`cli.py` is `ws home`, `ws home --render FILE` and `ws home data [--json]`. Only `--render` writes a file.
"""
