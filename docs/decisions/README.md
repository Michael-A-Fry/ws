# Decision records

One file per decision that shapes the engine, in the [MADR 4.0.0](https://adr.github.io/madr/) format: status, date,
context, options, outcome, consequences and a confirmation line that names the test or check keeping it true. The
process is [0013](0013-decision-records-in-madr.md).

A record isn't rewritten once accepted. A changed decision gets a new record, and the old one's status becomes
`superseded by NNNN`. A number is never reused. The numbers continue an earlier series, so some are absent here: those
decisions weren't about the engine.

| No. | Decision | Status | Date |
|---|---|---|---|
| [0003](0003-public-from-the-first-commit.md) | Build in public, runner-neutral, with Claude Code and OpenCode first | accepted | 2026-10-31 |
| [0004](0004-install-by-os-with-few-questions.md) | Detect the operating system and ask as little as possible at install | accepted | 2026-10-31 |
| [0005](0005-sending-is-draft-first.md) | An agent drafts; a person sends | accepted | 2026-10-31 |
| [0008](0008-no-run-without-a-monthly-cap.md) | Start no paid run without a monthly cap the person set | accepted | 2026-10-31 |
| [0010](0010-package-name-ws-aitoolkit.md) | Name the package `ws-aitoolkit` and the command `ws` | accepted | 2026-11-07 |
| [0012](0012-licence-apache-2.md) | Licence the code Apache-2.0 | accepted | 2026-10-31 |
| [0013](0013-decision-records-in-madr.md) | Record each engine decision once, in MADR 4.0.0 | accepted | 2026-11-07 |
| [0014](0014-agents-md-holds-only-what-code-cannot-say.md) | Keep AGENTS.md to facts an agent can't discover, and the code map in the reference | accepted | 2026-11-07 |
| [0015](0015-github-issues-is-the-engine-register.md) | The engine's register is GitHub Issues | accepted | 2026-11-07 |
| [0016](0016-descriptions-say-what-ws-does-today.md) | Descriptions name the runners ws supports today | accepted | 2026-11-07 |
| [0017](0017-the-gate-fails-closed.md) | The gate and the policy fail closed | accepted | 2026-11-07 |
| [0018](0018-core-is-standard-library-only.md) | The core uses the Python standard library only | accepted | 2026-11-07 |
| [0019](0019-one-resident-gate-answers-the-hooks.md) | One resident gate answers the hooks, over a private Unix socket | accepted | 2026-11-08 |
| [0022](0022-reserve-before-a-run.md) | Reserve before a run, settle after it | accepted | 2026-11-08 |
| [0023](0023-one-message-store-with-class-bodies-apart.md) | One message store, with the bodies of walled classes kept apart | accepted | 2026-11-08 |
| [0024](0024-a-config-without-the-sandbox-stays-without-it.md) | A config written without the sandbox stays without it until the policy says so | accepted | 2026-11-09 |

To add one, copy the newest record, take the next number and add a row here.
