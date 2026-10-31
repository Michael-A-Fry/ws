# ws

Your memory, skills and rules, in any AI.

Early days: no release yet. From a clone, with [uv](https://docs.astral.sh/uv/):

```sh
uv run ws init      # three questions: does this machine stay on? a server? your notes folder?
uv run ws doctor    # every check with an ID; exit 1 while anything still needs setting up
```

`ws doctor --json` gives the same report for scripts. macOS and Linux run ws directly; on Windows it runs inside WSL2.

- Licence: [Apache-2.0](LICENSE). Contributions need the [CLA](CLA.md), signed through a bot on your first pull request.
