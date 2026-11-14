# Items

Every thing ws keeps is an item with a name, `kind:id`: `run:20261101T0000Z-ab`, `session:abc123`, `area:a1`, `row:C237`.
`src/ws/items.py` is a thin layer over the stores that already exist. It moves nothing: a run is still a line in
`runs.jsonl`, a receipt a row in the queue database. The layer gives one way to name a thing, one way to list things, one
index of what points at what, and one checked way to change a thing that a person and an agent can both edit.

There is no command yet. Call it from code, or `python -m ws.items check | get KIND:ID | list KIND [FIELD=VALUE ...] |
rebuild` (exit 0 fine, 1 the check found faults or `get` found nothing, 2 usage).

## The kinds

`KINDS` in `items.py` has one row per kind. The columns are the owning module, the store (named as
[stores](stores.md) names it), the id pattern, whether the store is the truth or derived from another, and the write rule:

- `append-only`: written new and never edited (runs, ratings, messages, every log).
- `gated`: changed only through `items.change`, which checks the revision (areas).
- `single writer`: one module writes it and nothing else does (sessions, receipts, approvals, the register, views).

A row declares exactly one of the three; a fourth value, or none, fails `items.check`. The table is code, with no plug-in
loading. A kind's reader goes through the owning module's reader and never shows a body: a message is its envelope, a
gate call has no payload, an approval has no bound command, an OpenCode ask has no question.

| Kind | Module | Store | Id pattern | Truth | Write rule |
|---|---|---|---|---|---|
| `run` | `runrecord` | Run records; Run records, fallback | `[0-9A-Za-z_.:-]+` | truth | append-only |
| `rating` | `rating` | Ratings | `\S+@\S+` | truth | append-only |
| `canary` | `canary` | Canary | `h[0-9a-f]{12}` | truth | append-only |
| `eval-result` | `eval` | Eval results | `h[0-9a-f]{12}` | truth | append-only |
| `eval-look` | `eval` | Eval looks | `h[0-9a-f]{12}` | truth | append-only |
| `gate-call` | `guards.record_calls` | Gate calls | `h[0-9a-f]{12}` | truth | append-only |
| `claim` | `guards.claims` | Claims | `h[0-9a-f]{12}` | truth | append-only |
| `verdict` | `gate` | Gate verdicts | `h[0-9a-f]{12}` | truth | append-only |
| `wall-report` | `sandbox` | Wall report | `h[0-9a-f]{12}` | truth | append-only |
| `footprint` | `node` | Node footprint | `h[0-9a-f]{12}` | truth | append-only |
| `ask` | `adapters.opencode` | OpenCode asks | `\S+` | truth | append-only |
| `broker` | `broker` | Broker audit | `h[0-9a-f]{12}` | truth | append-only |
| `session` | `session` | Session | `[0-9A-Za-z_-]+` | truth | single writer |
| `identity` | `session` | Session identity | `[0-9A-Za-z_-]+` | truth | single writer |
| `guard-record` | `guards` | Guard record | `[0-9A-Za-z_.-]+` | truth | single writer |
| `approval` | `approvals` | Approval, pending and granted | `\S+` | truth | single writer |
| `versions` | `canary` | Seen versions | `current` | truth | single writer |
| `limits` | `usage` | Subscription limits | `current` | derived | single writer |
| `receipt` | `queue` | The queue database | `[0-9A-Za-z_.:-]+` | truth | single writer |
| `message` | `msg` | The messages database | `\S+` | truth | append-only |
| `area` | `items` | Areas | `a[0-9]+` | truth | gated |
| `view` | `items` | Saved views | `[a-z][a-z0-9-]*` | truth | single writer |
| `row` | `items` | Register | `[A-Za-z]+-?\d+` | truth | single writer |

A log line with no id of its own gets `h` and 12 hex digits of its content, so the same line is the same item.

## Get, list, views

- `items.get("run:ID")` returns the item or None. A kind not in the table, or a ref that isn't `kind:id`, is a typed
  error (`unknown-kind`, `bad-ref`).
- `items.list(kind, where, sort, group)` goes through the reader. `where` is equality on fields and nothing else; `sort`
  is a field name (`-field` descends); `group` gives a dict by that field. It returns at most 300 rows. There is no
  expression language: a view that needs more is code.
- A saved view is a TOML file `views/NAME.toml` in the config folder with `version = 1`, `kind`, and optionally `where`
  (a table), `sort` and `group`. `items.render_view("NAME")` resolves it at read time, so a new view is a new file.

## The link index

One SQLite table `links(from_ref, rel, to_ref)` in the cache folder (`items/links.db`), deleted and rebuilt from the
stores on each run and never patched. `rel` is one of `supersedes`, `source`, `about`, `used`, `added`, `produced`,
`parent`. `items.rebuild_index()` rebuilds it; `items.linked(ref, db, rel, inbound)` answers "what does this point at" and
"what points here." Rebuilding from nothing gives the same table as the live one.

Links from the logs are soft: a session record is removed after 30 days, so a run that points at one isn't a fault. A
link from an area, a register row or a view is strict: `items.check` exits 1 when it points at nothing (a row whose `area`
isn't in the table, a `needs` that names no row).

## Areas and register rows

An area is a JSON record `areas/ID.json` in the config folder: `name`, `rank`, `people`. Its id is opaque (`a1`) and
no id holds an area's name, so a rename changes one record and no item id. `rank` and `people` change only with a
person as the actor (`person:NAME`); an agent's change to them is refused.

A register row is a Markdown task line whose first word is its id, with fields in a trailing `{...}`:

```text
- [ ] C237 Build the item layer {area=a1 due=2026-11-20 needs=C12,C13 every=7d who=sam chase_on=2026-11-18}
```

`area`, `due`, `needs`, `every`, `who` and `chase_on` are read by `items.parse_register`, the one parser. The reader
takes the file `WS_REGISTER`, else `TODO.md` in the working folder.

## The checked change path

`items.change(ref, delta, expected_rev, actor)` is for a gated kind only. It re-reads the item, applies the delta,
validates it with the kind's validator and writes by temp file and rename, under a lock, only if the item's revision (the
sha256 of its bytes) is the one the caller saw. A moved revision gives outcome `conflict` with the fresh state and its
revision, so the caller decides again; a lock held too long is tried up to three times. Outcomes: `ok`, `conflict`,
`refused`, `invalid`, `missing`, `busy` (the lock was held through all three tries; nothing was written). The actor
goes in `changed_by`. The human-only rule trusts the actor string the caller passes (an empty `person:` is refused),
so it's advisory until a session has an identity ws can check. Append-only kinds never use it.

## Add a record kind

Every place that must spell a kind fails silently if you forget one: a kind with no entry in `LINK_SPEC` has no links and
says nothing; a kind no page lists is invisible to a reader. Do these, then run `uv run --locked pytest -q
tests/test_items.py`, which fails for each place that lacks the kind (`items.registry_gaps`):

1. Add the store to [stores](stores.md) if it's a new file, and give the row a name there.
2. Add the `Kind` row to `KINDS`: module, store name, id pattern, truth or derived, one write rule, reader.
3. Add the kind to `LINK_SPEC`: `(rel, target kind, field)` triples, or `()` for a kind that points at nothing.
4. Add the row to the kinds table on this page.
5. Add a planted item to the reader test in `tests/test_items.py`.

To build the checklist for your own kind, grep for the spelling of an existing one (`grep -rn '"run"' src tests docs`) and
add yours wherever it turns up.

## The commitment ledger

`items.extract_commitments(text)` finds what a text promises (open checkboxes, `TODO:` and `follow-up:` lines, lines that
say something must be done). `items.recall(cases)` is its eval: a seeded set the extractor must find, reported as recall.
`items.ledger_rows(text, source)` gives the rows an `items.jsonl` ledger would hold. ws doesn't write that file yet; its
row on the stores page comes with the writer.
