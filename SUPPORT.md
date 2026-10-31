# Support

Pick the lightest channel that fits.

| You want to | Go to |
|---|---|
| Learn a task | [Usage](docs/usage.md), or the [documentation map](docs/README.md) |
| Fix an install or a doctor failure | [Troubleshoot](docs/how-to/troubleshoot.md) |
| Report a bug | [Bug report form](https://github.com/Michael-A-Fry/ws/issues/new?template=bug.yml) |
| Suggest a feature | [Feature request form](https://github.com/Michael-A-Fry/ws/issues/new?template=feature.yml) |
| Ask a question | [Open an issue](https://github.com/Michael-A-Fry/ws/issues/new/choose); GitHub Discussions isn't turned on |
| Report a vulnerability | [SECURITY.md](SECURITY.md), never a public issue |

## What to include

Run these two and paste the output:

```sh
ws version --json
ws doctor --json
```

Then add your OS and shell, the command you ran, what you expected and what happened instead.

Scrub API keys, bearer tokens, private prompts, transcripts and paths that reveal who or where you are. ws never prints
a secret value, but a command of your own might.

## How fast

ws has one maintainer. Replies take days, not hours.
