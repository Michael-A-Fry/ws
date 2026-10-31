# `ws version --json` reference

`ws version --json` prints which build of ws you are running and where it came from. Use it in a bug report, in a script
that checks an install, or to compare a machine with a release tag. It's available from v0.1.

`ws --version` stays a single line, `ws X.Y.Z`, for people.

```sh
ws version --json
```

Like every ws command, the result is in the `data` field of the standard JSON envelope. See the [CLI reference](cli.md).

## Fields

| Field | Type | Meaning |
|---|---|---|
| `version` | text | The ws version, taken from the release tag. A source checkout reports `0.0.0+src`. |
| `git_sha` | text | The full commit that the build came from. |
| `dirty` | true or false | `true` when the build came from a working tree with uncommitted changes. |
| `build_date` | text | When the build was made, as a UTC date and time. |
| `python` | text | The Python version that runs ws, for example `3.12.3`. |
| `install_path` | text | The folder that holds the installed `ws` package. |
| `install_source` | text | Where the install came from, for example a release wheel, PyPI or a local checkout. |

## Use it

Check that a release install is the commit you expect:

```sh
ws version --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["git_sha"])'
```

## Related

- [Verify a release](../how-to/verify-a-release.md) checks the file before you install it.
- [Install ws](../how-to/install.md) has the install sources.
