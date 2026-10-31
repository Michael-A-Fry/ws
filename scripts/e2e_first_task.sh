#!/usr/bin/env bash
# One timed run of the person's path, on a throw-away HOME: install -> doctor -> setup -> first task -> rate ->
# uninstall -> nothing left behind. It is PLAN section 6 row 5's "e2e timed on every cell": the same script runs on
# macOS, Linux, WSL2 and the lab VM (.github/workflows/cli.yml, lab.yml).
#
# The model is the mock provider (`python -m ws.testing.mockprovider`, scenario first_task): no network, no paid
# provider, no real key. It serves https on 127.0.0.1 with a throw-away certificate (made with openssl, 127.0.0.1 as its
# subject alternative name), and the key in the store is a placeholder. The broker is the real one, `ws broker serve` on
# the default socket in WS_HOME, started the way a person's service would be, with SSL_CERT_FILE naming that
# certificate so it trusts the mock; `ws run` leases from it, and the broker relays over real TLS. The job has no `cwd`,
# so the run's default working folder is what gets tested: the mock writes a relative file, and the script reads the
# folder back from the run record. The runner is Claude Code when it is installed, else OpenCode
# (WS_E2E_RUNNER=claude-code|opencode picks one).
#
# What that does not prove: the real provider's certificate chain and hostnames (the mock's certificate is trusted by
# file), and anything about a real model. Where the secret wall is in use (a separate system user holds the store) the
# broker runs as that user; here it runs as the person.
#
# Writes e2e-timing.json (WS_E2E_OUT to move it):
#   {cell, os, steps: [{name, seconds, exit, ok}], total_seconds, run_id, run_duration_ms, ws_version, runner}
# prints a one-line summary, and exits non-zero when any step failed. The first failed step stops the run; the clean-up
# still happens and the file still says how far it got.
#
# `--smoke` instead runs the credential-free smoke only: `ws --help`, `doctor --offline`, `config` and `status` in a
# WS_HOME whose path has a space, with every provider key variable unset, and fails on a crash (an exit above 1, or a
# Python traceback). It runs the command in WS_SMOKE_CMD (default `ws`; CI uses `uv run --locked ws`), needs no python3
# and no runner, and is what the native Windows job runs too. `ws doctor --offline` exits 1 on a fresh machine; that is
# the normal answer.
#
# Under GitHub Actions the summary line is also appended to $GITHUB_STEP_SUMMARY.
#
# Environment: WS_E2E_CELL (default "local"), WS_E2E_OUT (default ./e2e-timing.json), WS_E2E_RUNNER, WS_E2E_KEEP=1 keeps
# the scratch folder (it is printed). Needs bash 3.2 or newer, python3 and uv or pipx.

# The steps are called through run_step and the clean-up through a trap, which shellcheck cannot follow.
# shellcheck disable=SC2329
set -u

# Every variable that could hand a runner or a provider a credential. Unset in both modes.
PROVIDER_VARS="ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL OPENAI_API_KEY OPENAI_BASE_URL OPENAI_ORG_ID"
PROVIDER_VARS="$PROVIDER_VARS GEMINI_API_KEY GOOGLE_API_KEY OPENROUTER_API_KEY MISTRAL_API_KEY GROQ_API_KEY XAI_API_KEY"
PROVIDER_VARS="$PROVIDER_VARS DEEPSEEK_API_KEY AZURE_OPENAI_API_KEY AWS_BEARER_TOKEN_BEDROCK"

unset_provider_vars() {
    local v
    for v in $PROVIDER_VARS; do
        unset "$v"
    done
}

# smoke: the four read-only commands, a crash is a failure. $WS_SMOKE_CMD is split on purpose (`uv run --locked ws`).
smoke() {
    local dir cmd rc bad=0 args out
    unset_provider_vars
    dir="$(mktemp -d "${TMPDIR:-/tmp}/ws-smoke.XXXXXX")" || return 1
    export WS_HOME="$dir/ws home"
    mkdir -p "$WS_HOME"
    for args in "--help" "doctor --offline" "config" "status"; do
        # shellcheck disable=SC2086  # the command and its arguments are words on purpose
        out="$(${WS_SMOKE_CMD:-ws} $args 2>&1 < /dev/null)"
        rc=$?
        if [ "$rc" -gt 1 ] || printf '%s' "$out" | grep -q 'Traceback (most recent call last)'; then
            printf 'smoke: ws %s CRASHED (exit %s)\n%s\n' "$args" "$rc" "$out" >&2
            bad=1
        else
            printf 'smoke: ws %-15s exit %s\n' "$args" "$rc"
        fi
    done
    rm -rf "$dir"
    cmd="${WS_SMOKE_CMD:-ws}"
    if [ "$bad" = "0" ]; then
        echo "smoke: ok ($cmd, WS_HOME with a space, no provider key set)"
        summary "smoke ok: $cmd with no provider key, WS_HOME with a space (cell ${WS_E2E_CELL:-local})"
    fi
    return "$bad"
}

# summary LINE: append a line to the GitHub job summary, when there is one.
summary() {
    [ -n "${GITHUB_STEP_SUMMARY:-}" ] && printf -- '- %s\n' "$1" >> "$GITHUB_STEP_SUMMARY"
    return 0
}

if [ "${1:-}" = "--smoke" ]; then
    smoke
    exit $?
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
CELL="${WS_E2E_CELL:-local}"
OUT="${WS_E2E_OUT:-$PWD/e2e-timing.json}"
OS_NAME="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$OS_NAME" in
    linux) grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null && OS_NAME="linux-wsl2" ;;
esac
PLACEHOLDER_KEY="placeholder-not-a-key"
TOOL="ws-aitoolkit"

# Keep the tools' own caches where they are; only HOME is thrown away. Read before HOME changes.
if command -v uv >/dev/null 2>&1; then
    UV_CACHE_DIR="${UV_CACHE_DIR:-$(uv cache dir 2>/dev/null)}"
    UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$(uv python dir 2>/dev/null)}"
    export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR
fi

# Under /tmp, not TMPDIR: the broker socket is $WS_HOME/run/broker.sock, and a Unix socket path tops out near 100
# characters (macOS's TMPDIR alone is 50).
BASE="$(mktemp -d /tmp/ws-e2e.XXXXXX)" || exit 1
unset XDG_BIN_HOME XDG_DATA_HOME XDG_CONFIG_HOME XDG_STATE_HOME XDG_CACHE_HOME WS_BROKER_SOCKET
unset_provider_vars
export HOME="$BASE/home"
export WS_HOME="$BASE/ws home" # a space in the path, on purpose
LOGS="$BASE/logs"
mkdir -p "$HOME" "$WS_HOME" "$LOGS"
export PATH="$HOME/.local/bin:$PATH"

STEPS="$BASE/steps.tsv"
: > "$STEPS"
FAILED=0
WSPY=""
MOCK_PID=""
BROKER_PID=""
RUN_ID=""
RUN_MS=""
WS_VERSION=""
RUNNER=""
T_START=""

now() { python3 -c 'import time; print("%.3f" % time.time())'; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.2f", b - a }'; }

# run_step NAME "OK EXIT CODES" COMMAND...: time it, keep its output in logs/NAME.log, stop the run on a bad exit.
run_step() {
    local name="$1" accept="$2" t0 t1 rc ok=0 code
    shift 2
    [ "$FAILED" = "0" ] || return 1
    t0="$(now)"
    "$@" > "$LOGS/$name.log" 2>&1 < /dev/null
    rc=$?
    t1="$(now)"
    for code in $accept; do
        [ "$rc" = "$code" ] && ok=1
    done
    printf '%s\t%s\t%s\t%s\n' "$name" "$(elapsed "$t0" "$t1")" "$rc" "$ok" >> "$STEPS"
    if [ "$ok" = "1" ]; then
        printf 'e2e: %-14s %6s s  exit %s\n' "$name" "$(elapsed "$t0" "$t1")" "$rc"
        return 0
    fi
    FAILED=1
    printf 'e2e: %-14s FAILED (exit %s); last lines of %s:\n' "$name" "$rc" "$LOGS/$name.log" >&2
    tail -n 25 "$LOGS/$name.log" | sed 's/^/    /' >&2
    return 1
}

cleanup() {
    [ -n "$BROKER_PID" ] && kill "$BROKER_PID" 2> /dev/null
    [ -n "$MOCK_PID" ] && kill "$MOCK_PID" 2> /dev/null
    wait 2> /dev/null
    write_json
    if [ "${WS_E2E_KEEP:-0}" = "1" ]; then
        echo "e2e: scratch folder kept at $BASE"
    else
        rm -rf "$BASE"
    fi
}

write_json() {
    local total
    total="$(elapsed "${T_START:-0}" "$(now)")"
    python3 - "$STEPS" "$OUT" "$CELL" "$OS_NAME" "$total" "$RUN_ID" "$RUN_MS" "$WS_VERSION" "$RUNNER" << 'PY'
import json, sys

steps_file, out, cell, os_name, total, run_id, run_ms, version, runner = sys.argv[1:10]
steps = []
for line in open(steps_file, encoding="utf-8"):
    name, seconds, code, ok = line.rstrip("\n").split("\t")
    steps.append({"name": name, "seconds": float(seconds), "exit": int(code), "ok": ok == "1"})
doc = {
    "cell": cell,
    "os": os_name,
    "steps": steps,
    "total_seconds": float(total),
    "run_id": run_id or None,
    "run_duration_ms": int(run_ms) if run_ms else None,
    "ws_version": version or None,
    "runner": runner or None,
}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
PY
}

# ----- the steps -----

list_home() { find "$HOME" \( -type f -o -type l -o -type d \) | sed "s|^$HOME|~|" | sort; }

do_install() { bash "$SRC/install.sh" --from "$SRC"; }

find_python() {
    # the interpreter ws was installed with: the first line of the `ws` script
    local ws_bin
    ws_bin="$(command -v ws)" || return 1
    WSPY="$(head -n 1 "$ws_bin" | sed 's/^#!//')"
    [ -x "$WSPY" ] || return 1
    WS_VERSION="$(ws --version | sed 's/^ws //')"
}

pick_runner() {
    case "${WS_E2E_RUNNER:-}" in
        claude-code | opencode) RUNNER="$WS_E2E_RUNNER" ;;
        "")
            if command -v claude > /dev/null 2>&1; then
                RUNNER="claude-code"
            elif command -v opencode > /dev/null 2>&1; then
                RUNNER="opencode"
            else
                echo "no runner: install Claude Code or OpenCode (tests/e2e/runners has the pinned versions)"
                return 1
            fi
            ;;
        *)
            echo "WS_E2E_RUNNER must be claude-code or opencode"
            return 1
            ;;
    esac
}

do_setup() { ws setup --yes --runner "$RUNNER" --cap 20; }

# A self-signed certificate for 127.0.0.1 that is also its own authority: Python 3.13+ checks strictly, so it says so.
# A config file, not -addext, because macOS's openssl is LibreSSL.
make_cert() {
    cat > "$BASE/cert.cnf" << 'CNF'
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = 127.0.0.1
[v3]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1
CNF
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "$BASE/mock-key.pem" -out "$BASE/mock-cert.pem" -days 2 \
        -config "$BASE/cert.cnf"
}

# The mock writes ./hello.txt: relative to the runner's working folder, which `ws run` chooses.
start_mock() {
    local provider=anthropic i url=""
    [ "$RUNNER" = "opencode" ] && provider=openai
    "$WSPY" -I -m ws.testing.mockprovider --scenario first_task --provider "$provider" --work . \
        --tls-cert "$BASE/mock-cert.pem" --tls-key "$BASE/mock-key.pem" > "$BASE/mock.out" 2> "$BASE/mock.err" &
    MOCK_PID=$!
    i=0
    while [ "$i" -lt 100 ]; do
        url="$(sed -n 's/^MOCK_[A-Z]*_BASE_URL=//p' "$BASE/mock.out" 2> /dev/null | head -n 1)"
        [ -n "$url" ] && break
        kill -0 "$MOCK_PID" 2> /dev/null || break
        sleep 0.2
        i=$((i + 1))
    done
    if [ -z "$url" ]; then
        echo "the mock provider printed no base URL"
        cat "$BASE/mock.err"
        return 1
    fi
    MOCK_HOST="${url#https://}"
    echo "mock provider ($provider) at $url"
}

# The registry and a placeholder value for the mock's host, then the real broker, on the default socket.
start_broker() {
    local header=x-api-key i
    [ "$RUNNER" = "opencode" ] && header=authorization
    printf '[mockkey]\nhosts = ["%s"]\npurposes = ["run"]\nheader = "%s"\n' "$MOCK_HOST" "$header" > "$WS_HOME/secrets.toml"
    printf '%s' "$PLACEHOLDER_KEY" | ws secret set mockkey || return 1
    SSL_CERT_FILE="$BASE/mock-cert.pem" ws broker serve > "$BASE/broker.out" 2> "$BASE/broker.err" &
    BROKER_PID=$!
    i=0
    while [ "$i" -lt 100 ]; do
        ws broker status > /dev/null 2>&1 && return 0
        kill -0 "$BROKER_PID" 2> /dev/null || break
        sleep 0.2
        i=$((i + 1))
    done
    echo "the broker did not start"
    cat "$BASE/broker.err"
    return 1
}

write_job() {
    mkdir -p "$WS_HOME/jobs"
    {
        printf 'runner = "%s"\n' "$RUNNER"
        if [ "$RUNNER" = "claude-code" ]; then
            printf 'model = "claude-sonnet-4-5"\n'
        else
            printf 'model = "stub/m"\nallow_unpriced = true\n' # a stub model has no price
        fi
        printf 'prompt = "Write hello.txt in this folder."\ntimeout_s = 240\n'
        printf '\n[runner_options]\n'
        if [ "$RUNNER" = "claude-code" ]; then
            printf 'allowed_tools = ["Edit"]\n'
        else
            printf 'offline = true\n'
            printf 'provider = { npm = "@ai-sdk/openai-compatible", name = "stub", models = { m = { name = "m" } } }\n'
            printf 'env = { XDG_CACHE_HOME = "%s" }\n' "$BASE/cache"
        fi
        printf '\n[[secrets]]\nname = "mockkey"\nhost = "%s"\npurpose = "run"\n' "$MOCK_HOST"
    } > "$WS_HOME/jobs/first-task.toml"
}

do_run() { ws run first-task; }

# The job's file landed, and the run record says ok with a duration.
check_run() {
    ws runs --last 1 --json > "$BASE/runs.json" || return 1
    "$WSPY" -I - "$BASE/runs.json" "$BASE/record.tsv" << 'PY'
import json
import sys

doc = json.load(open(sys.argv[1], encoding="utf-8"))
recs = doc.get("data", doc) if isinstance(doc, dict) else doc
rec = recs[-1]
problems = []
if rec.get("status") != "ok":
    problems.append(f"status is {rec.get('status')!r}, error {rec.get('error')!r}")
if not isinstance(rec.get("duration_ms"), int) or rec["duration_ms"] <= 0:
    problems.append(f"duration_ms is {rec.get('duration_ms')!r}")
if problems:
    sys.exit("run record: " + "; ".join(problems))
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    fh.write(f"{rec['run_id']}\t{rec['duration_ms']}\t{rec.get('cwd') or ''}\n")
PY
    local rc=$?
    [ "$rc" = "0" ] || return "$rc"
    RUN_ID="$(cut -f1 "$BASE/record.tsv")"
    RUN_MS="$(cut -f2 "$BASE/record.tsv")"
    WORK="$(cut -f3 "$BASE/record.tsv")"
    echo "run $RUN_ID ok in $RUN_MS ms, working folder $WORK"
    # the default working folder: under the state dir, never inside WS_HOME (the wall makes that read-only)
    case "$WORK" in
        "$HOME/.local/state/ws/work/"*) ;;
        *)
            echo "the working folder is not under $HOME/.local/state/ws/work/: $WORK"
            return 1
            ;;
    esac
    [ -f "$WORK/hello.txt" ] || {
        echo "the job's file did not land: $WORK/hello.txt"
        return 1
    }
    grep -q hello "$WORK/hello.txt" || {
        echo "hello.txt does not say hello"
        return 1
    }
}

do_rate() { ws rate last up; }
do_uninstall() { ws uninstall --yes; }

do_tool_uninstall() {
    if command -v uv > /dev/null 2>&1 && uv tool list 2> /dev/null | grep -q "^$TOOL"; then
        uv tool uninstall "$TOOL"
    else
        pipx uninstall "$TOOL"
    fi
}

# What is in HOME now must be what was there before the install, apart from state that is not ws's: the runners' own
# (Claude Code's sessions, backups and settings; OpenCode's data, state and cache; the npm log and the folder Claude Code
# writes), uv's empty tool folders after `uv tool uninstall`, and macOS's caches. Those are named one by one, so what ws
# put under the same folders (skills in ~/.claude/skills, the work folder in ~/.local/state/ws) counts as residue.
CLAUDE_STATE='^~/\.claude\.json|^~/\.claude/(projects|backups|dev-mods|seed-admin|sessions|ssh|shell-snapshots|todos'
CLAUDE_STATE="$CLAUDE_STATE|statsig|telemetry|cache|plugins|history\.jsonl|ide|logs)(/|\$)|^~/\.config/anthropic|^~/\.npm(/|\$)"
OPENCODE_STATE='^~/\.(local/share|local/state|cache|config)/opencode(/|$)'
OTHER_STATE='^~/\.claude$|^~/\.(local|config|cache)$|^~/\.local/(share|state)$|^~/\.local/bin$|^~/\.local/share/uv'
OTHER_STATE="$OTHER_STATE|^~/Library$|^~/Library/(Caches|Application Support|Logs)(/|\$)"
RUNNER_STATE="$CLAUDE_STATE|$OPENCODE_STATE|$OTHER_STATE"
check_residue() {
    list_home > "$BASE/after.txt"
    local left
    left="$(diff "$BASE/before.txt" "$BASE/after.txt" | sed -n 's/^> //p' |
        grep -vE "$RUNNER_STATE" || true)"
    if [ -n "$left" ]; then
        echo "left behind in HOME:"
        printf '%s\n' "$left"
        return 1
    fi
    echo "HOME is as it was before the install (the runner's own state excluded)"
}

main() {
    T_START="$(now)"
    list_home > "$BASE/before.txt"
    run_step install "0" do_install || return 1
    run_step ws_python "0" find_python || return 1
    run_step doctor "0 1" ws doctor --offline || return 1 # 1 is the normal answer: roles are still to set up
    run_step pick_runner "0" pick_runner || return 1
    run_step setup "0" do_setup || return 1
    run_step mock_cert "0" make_cert || return 1
    run_step mock_start "0" start_mock || return 1
    run_step broker_start "0" start_broker || return 1
    run_step write_job "0" write_job || return 1
    run_step first_task "0" do_run || return 1
    run_step check_run "0" check_run || return 1
    run_step rate "0" do_rate || return 1
    run_step uninstall "0" do_uninstall || return 1
    run_step tool_uninstall "0" do_tool_uninstall || return 1
    run_step residue "0" check_residue || return 1
    return 0
}

trap cleanup EXIT
main
rc=$?
# steps that run inside run_step cannot set these in this shell when they are subshells; they are not (functions run
# here), so RUN_ID, RUN_MS and WS_VERSION are set by now
total="$(elapsed "$T_START" "$(now)")"
if [ "$rc" = "0" ] && [ "$FAILED" = "0" ]; then
    line="e2e ok: cell=$CELL os=$OS_NAME runner=$RUNNER ws=$WS_VERSION steps=$(wc -l < "$STEPS" | tr -d ' ') total=${total}s run=${RUN_ID} run_ms=${RUN_MS}"
    echo "$line"
    summary "$line"
    exit 0
fi
line="e2e FAILED: cell=$CELL os=$OS_NAME runner=${RUNNER:-none} total=${total}s (see $OUT)"
echo "$line" >&2
summary "$line"
exit 1
