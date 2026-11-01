"""ws home: the home page, and the one data API (`ws.home.api`) it and every other reader use.

`records.py` is the shapes and the opaque rule, `sources/` is a folder of readers found by scanning it, `api.py`
merges what they return (and hands one answer to the source that asked), `areas/` draw the page from those records,
`hub.py` works out the box's pickers and starts a session, `openers/` open a session where it lives, `template.py` fills
the profile's argument templates, and `cli.py` is `ws home`, `ws home --render FILE` and `ws home data [--json]`. Only
`--render` writes a file.
"""
