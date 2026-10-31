#!/usr/bin/env bash
# One lab cycle, run on the Proxmox HOST (never inside the VM): reset the VM from its clean snapshot, start it, give the
# runner in it a single-use registration, wait for its one job to finish, stop the VM. See docs/reference/lab.md.
#
#   lab-host-cycle.sh --token-file FILE --vm-id ID --node NODE --snapshot NAME [--repo OWNER/REPO] [--timeout SECONDS]
#
# The repo-admin token lives in FILE on the host (mode 0600 or tighter, refused otherwise) and is only ever sent to the
# GitHub API from here. The VM never holds it: after the reset the host asks GitHub for a just-in-time runner
# configuration (POST /repos/{repo}/actions/runners/generate-jitconfig, labels ws-lab; a JIT runner is always
# ephemeral: it takes one job, then GitHub forgets it) and writes only that configuration into the VM through the
# QEMU guest agent. A leaked configuration is good for one runner, once; it cannot register anything else.
#
# The configuration reaches the VM on the standard input of `qm guest exec --pass-stdin` (Proxmox VE 8 or newer), so it
# is never on the host's command line where `ps` could show it.
#
# Needs on the host: qm, curl, python3. In the VM: the guest agent, and the runner service of docs/reference/lab.md, which
# starts `./run.sh --jitconfig` when /etc/ws-lab/jitconfig appears. Not run by CI; run it from a systemd timer or by hand.
# ID and NODE are Proxmox's own VM id and node name; nothing here is a hostname or an address.
#
# The token is a fine-grained personal access token for this one repository with the Administration permission set to
# write (that is what generate-jitconfig and runner removal need), and nothing else.
#
# Exit: 0 the job finished and the VM is stopped, 1 something failed (the VM is stopped and the runner removed), 2 usage.

set -euo pipefail

REPO="Michael-A-Fry/ws"
TIMEOUT=2400
TOKEN_FILE="" VM_ID="" NODE="" SNAPSHOT=""
RUNNER_ID=""
TOKEN=""

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

die() {
    echo "lab-host-cycle: $1" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --token-file) TOKEN_FILE="${2:-}"; shift 2 ;;
        --vm-id) VM_ID="${2:-}"; shift 2 ;;
        --node) NODE="${2:-}"; shift 2 ;;
        --snapshot) SNAPSHOT="${2:-}"; shift 2 ;;
        --repo) REPO="${2:-}"; shift 2 ;;
        --timeout) TIMEOUT="${2:-}"; shift 2 ;;
        -h | --help) usage ;;
        *) echo "lab-host-cycle: unknown argument: $1" >&2; usage ;;
    esac
done
[ -n "$TOKEN_FILE" ] && [ -n "$VM_ID" ] && [ -n "$NODE" ] && [ -n "$SNAPSHOT" ] || usage
case "$VM_ID$NODE$SNAPSHOT$REPO$TIMEOUT" in
    *[!A-Za-z0-9._/-]*) echo "lab-host-cycle: ids, names and the repo take letters, digits and . _ / - only" >&2; exit 2 ;;
esac

[ -f "$TOKEN_FILE" ] || die "$TOKEN_FILE is not a file"
mode="$(stat -c %a "$TOKEN_FILE" 2> /dev/null || stat -f %Lp "$TOKEN_FILE")"
case "$mode" in
    400 | 600) ;;
    *) die "$TOKEN_FILE has mode $mode; it must be 0600 or 0400 (it is a repo-admin token)" ;;
esac
TOKEN="$(cat "$TOKEN_FILE")"

api() { # api METHOD PATH [JSON BODY]: the token goes in a header read from stdin, not onto the command line
    local method="$1" path="$2" body="${3:-}"
    if [ -n "$body" ]; then
        printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | curl -fsS --config - -X "$method" \
            -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
            -H "Content-Type: application/json" --data "$body" "https://api.github.com$path"
    else
        printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | curl -fsS --config - -X "$method" \
            -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
            "https://api.github.com$path"
    fi
}

stop_vm() {
    qm stop "$VM_ID" > /dev/null 2>&1 || true
}

# runner_code: the HTTP status of the runner's own page (000 when GitHub could not be reached at all).
runner_code() {
    printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | curl -sS --config - -o /dev/null -w '%{http_code}' \
        -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
        --max-time 30 "https://api.github.com/repos/$REPO/actions/runners/$RUNNER_ID" 2> /dev/null || echo 000
}

cleanup() {
    local rc=$?
    # a runner that never took its job is removed, so it cannot be used later; the VM goes either way
    if [ -n "$RUNNER_ID" ]; then
        api DELETE "/repos/$REPO/actions/runners/$RUNNER_ID" > /dev/null 2>&1 || true
    fi
    stop_vm
    exit "$rc"
}
trap cleanup EXIT

echo "lab-host-cycle: reset VM $VM_ID to snapshot $SNAPSHOT"
stop_vm
qm rollback "$VM_ID" "$SNAPSHOT"
qm start "$VM_ID"

echo "lab-host-cycle: waiting for the guest agent"
i=0
until qm agent "$VM_ID" ping > /dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -le 90 ] || die "the guest agent did not answer in 3 minutes"
    sleep 2
done

echo "lab-host-cycle: asking GitHub for a single-use runner configuration"
reply="$(api POST "/repos/$REPO/actions/runners/generate-jitconfig" \
    "{\"name\":\"ws-lab-$(date +%s)\",\"runner_group_id\":1,\"labels\":[\"ws-lab\"],\"work_folder\":\"_work\"}")"
RUNNER_ID="$(printf '%s' "$reply" | python3 -c 'import json,sys; print(json.load(sys.stdin)["runner"]["id"])')"
JIT="$(printf '%s' "$reply" | python3 -c 'import json,sys; print(json.load(sys.stdin)["encoded_jit_config"])')"
[ -n "$RUNNER_ID" ] && [ -n "$JIT" ] || die "GitHub gave no runner configuration"

echo "lab-host-cycle: handing the configuration to the VM (runner $RUNNER_ID)"
printf '%s' "$JIT" | qm guest exec "$VM_ID" --pass-stdin 1 --synchronous 1 -- \
    /bin/sh -c 'umask 077 && cat > /etc/ws-lab/jitconfig' > /dev/null
JIT=""

echo "lab-host-cycle: waiting for the job (up to $TIMEOUT s)"
deadline=$(($(date +%s) + TIMEOUT))
blips=0
while :; do
    code="$(runner_code)"
    case "$code" in
        404) break ;; # GitHub has forgotten the runner: its one job is done
        200) blips=0 ;;
        401) die "GitHub rejected the token (401)" ;;
        *)
            # a 5xx, a rate limit or a network blip says nothing about the job: wait longer and ask again
            blips=$((blips + 1))
            echo "lab-host-cycle: GitHub answered $code; asking again" >&2
            [ "$blips" -le 5 ] || blips=5
            sleep $((15 * (1 << blips)))
            ;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || die "the job did not finish in $TIMEOUT s"
    [ "$code" != "200" ] || sleep 15
done
RUNNER_ID="" # its job is done
echo "lab-host-cycle: the job finished; stopping the VM"
