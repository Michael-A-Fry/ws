"""Connect flows: the one format for the steps only a person can do, and the engine that walks them.

An integration that needs a person (make a bot, paste a token, press Start) is a data file, `flows/<id>.toml` in this
package, in a closed set of step types: `info`, `link`, `capture`, `verify` and, per step, `revoke`. `spec` loads and
checks a file, `engine` keeps the state and walks, shows or undoes the steps, and `ws connect` (`ws.connectcmd`) is the
command. The shape follows Home Assistant's config flow (declarative steps, a stable unique id, nothing configured until a
real call passes); docs/decisions/0033 gives the source of each rule and docs/reference/connect.md the format.

This is a format, not a plug-in framework: the steps are a closed set, flows are found only in this folder, and a
third party cannot add one. It is not a stable extension point before 1.0.
"""
