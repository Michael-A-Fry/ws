# The Rule of Two

The Rule of Two is the rule that a session must not hold all three of these at once: untrusted input, private data and a
way to send things out.

## The rule

| Leg | Meaning | Example |
|---|---|---|
| A | Untrusted input: content that anyone can write to | An inbox, a web page |
| B | Private data: things you would not publish | Your notes, your finances |
| C | A way out: any act that reaches outside | Sending mail, a web fetch, a shell that can reach the network |

Two legs are fine. Three together are refused. With all three, a message in your inbox can tell the agent to read your
notes and send them to a stranger.

## Why it exists

Prompt injection works when attacker text and your data meet in one context and the agent can act on the outside world.
Removing any one leg breaks the attack. The rule is cheap to check before a job starts, so ws checks it every time.

## How ws enforces it

The code is `rule_of_two` in `src/ws/policy.py`, called by `src/ws/run.py` before a job starts.

1. You describe each source of content in `policy.toml` under `[sources.NAME]`: `untrusted`, `sensitive`, the `mode`
   (`read`, `act` or `leave`), and the `acts` and `outward` acts it allows.
2. A job file lists the `sources` it reads.
3. Before the runner starts, `ws run` collects the legs the job would hold. Leg C counts:
   - an outward act of a source in mode `act`, when the dial isn't `prepare`;
   - an allowed web tool (`web_fetch` or `web_search` not in `deny_tools`);
   - the shell, when the runner has one and any common network command isn't in `deny_commands`. The probes are `curl`,
     `wget`, `nc`, `ncat`, `ssh`, `scp`, `sftp`, `ftp`, `telnet`, `socat`, and `python -c`, `node -e`, `perl -e` and
     `ruby -e`.
4. If all three legs are present, the run is refused. `ws run` exits 3 and still writes a run record with status
   `refused`. A source set to `leave` is refused outright, and so is a source the policy doesn't know.
5. A named `[[exceptions]]` entry lifts the refusal when its `job` matches and every source the session reads is one it
   names. The run record says which exception applied (`exception:NAME`).

The shell counts as a way out because a shell that can run `curl` can send data anywhere. To close that leg you deny the
whole set of network commands. There is no half measure.

## What the rule doesn't stop

- **Interactive sessions.** The check runs in `ws run` and so in `ws eval`, which goes through `ws run`. A session you
  start by hand with `claude` or `opencode` isn't checked. The gate and the wall still apply to it.
- **A source you mislabel.** The rule trusts your `untrusted` and `sensitive` flags. A source you leave unmarked isn't
  counted.
- **Ways out the policy doesn't know.** A network tool that's not in the probe list, or a program you install later,
  isn't counted until you deny it.
- **Two legs misused.** An agent with untrusted input and a way out, but no private data, can still do harm that isn't
  a leak, such as running a destructive command. That's the gate's job.

## Do it

Follow [Apply the Rule of Two](../how-to/apply-the-rule-of-two.md).
