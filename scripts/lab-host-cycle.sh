#!/usr/bin/env bash
# One lab cycle, run on the Proxmox HOST (never inside the VM): reset the VM from its clean snapshot, start it, give the
# runner in it a single-use registration, wait for its one job to finish, stop the VM. See docs/reference/lab.md.
#
#   lab-host-cycle.sh --token-file FILE --vm-id ID --node NODE --snapshot NAME [--repo OWNER/REPO] [--timeout SECONDS]
#                     [--pve-env FILE]
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
# By default the VM is driven with `qm`, on the Proxmox host. With --pve-env FILE it is driven through the Proxmox API
# instead, so the cycle can run from any machine that can reach the API (FILE is a shell env file, mode 0600 or tighter,
# with PVE_HOST, PVE_TOKEN_ID and PVE_TOKEN_SECRET, and optionally PVE_CA, a CA certificate file). The API token needs
# only VM.PowerMgmt, VM.Snapshot.Rollback and VM.Monitor on that one VM.
#
# The configuration is written atomically (a temporary file, then a rename) and the runner service is restarted after
# the write, so a service that starts on the file appearing can never read it half-written or empty.
#
# Needs on the host: qm (or, with --pve-env, nothing of Proxmox), curl, python3. In the VM: the guest agent, and the runner service of docs/reference/lab.md, which
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
TOKEN_FILE="" PVE_ENV="" VM_ID="" NODE="" SNAPSHOT=""
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
        --pve-env) PVE_ENV="${2:-}"; shift 2 ;;
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

if [ -n "$PVE_ENV" ]; then
    [ -f "$PVE_ENV" ] || die "$PVE_ENV is not a file"
    case "$(stat -c %a "$PVE_ENV" 2> /dev/null || stat -f %Lp "$PVE_ENV")" in
        400 | 600) ;;
        *) die "$PVE_ENV must be mode 0600 or 0400 (it holds a Proxmox API token)" ;;
    esac
    PVE_HOST="" PVE_TOKEN_ID="" PVE_TOKEN_SECRET="" PVE_CA=""
    # shellcheck disable=SC1090
    . "$PVE_ENV"
    [ -n "$PVE_HOST" ] && [ -n "$PVE_TOKEN_ID" ] && [ -n "$PVE_TOKEN_SECRET" ] || die "$PVE_ENV needs PVE_HOST, PVE_TOKEN_ID and PVE_TOKEN_SECRET"
fi

# pve_raw METHOD URLPATH [curl --config lines on stdin]: one call to the Proxmox API; prints the "data" member
pve_raw() {
    local method="$1" path="$2" extra="" ca=()
    [ -t 0 ] || extra="$(cat)"
    [ -z "$PVE_CA" ] || ca=(--cacert "$PVE_CA")
    { printf 'header = "Authorization: PVEAPIToken=%s=%s"\n' "$PVE_TOKEN_ID" "$PVE_TOKEN_SECRET"; printf '%s\n' "$extra"; } |
        curl -fsS --max-time 60 "${ca[@]}" --config - -X "$method" "https://$PVE_HOST:8006/api2/json$path" |
        python3 -c 'import json,sys; d=json.load(sys.stdin).get("data"); print("" if d is None else d if isinstance(d,str) else json.dumps(d))'
}
pve() { pve_raw "$1" "/nodes/$NODE/qemu/$VM_ID$2"; }

# pve_task METHOD PATH: a call that returns a task id; wait for the task and fail unless it ended OK
pve_task() {
    local upid n=0 st
    upid="$(pve "$1" "$2" < /dev/null)"
    while :; do
        st="$(pve_raw GET "/nodes/$NODE/tasks/$(python3 -c 'import sys,urllib.parse as u; print(u.quote(sys.argv[1],safe=""))' "$upid")/status" < /dev/null |
            python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d.get("exitstatus",""))')"
        case "$st" in
            "stopped OK") return 0 ;;
            stopped*) die "Proxmox task failed: $st ($1 $2)" ;;
        esac
        n=$((n + 1))
        [ "$n" -le 150 ] || die "Proxmox task did not finish in 5 minutes ($1 $2)"
        sleep 2
    done
}

# vm_run SCRIPT < input: run SCRIPT with /bin/sh in the VM as root through the guest agent, input on its stdin; fails
# unless it exits 0. The input never goes on a command line (qm: --pass-stdin; API: the request body, read from stdin).
vm_run() {
    if [ -z "$PVE_ENV" ]; then
        qm guest exec "$VM_ID" --pass-stdin 1 --synchronous 1 -- /bin/sh -c "$1" > /dev/null
        return
    fi
    local input pid out
    input="$(cat)"
    pid="$(printf 'data-urlencode = "command=/bin/sh"\ndata-urlencode = "command=-c"\ndata-urlencode = "command=%s"\ndata-urlencode = "input-data=%s"\n' \
        "$1" "$input" | pve POST /agent/exec | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["pid"])')"
    input=""
    for _ in $(seq 1 60); do
        out="$(pve GET "/agent/exec-status?pid=$pid" < /dev/null)"
        if [ "$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("exited",0))')" = 1 ]; then
            [ "$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("exitcode",1))')" = 0 ] || die "the command in the VM failed"
            return 0
        fi
        sleep 1
    done
    die "the command in the VM did not finish in 1 minute"
}

vm_stop() {
    if [ -z "$PVE_ENV" ]; then
        qm stop "$VM_ID" > /dev/null 2>&1 || true
    else
        (pve_task POST /status/stop) > /dev/null 2>&1 || true # a subshell: die must not end the cleanup
    fi
}
vm_rollback() {
    if [ -z "$PVE_ENV" ]; then qm rollback "$VM_ID" "$SNAPSHOT"; else pve_task POST "/snapshot/$SNAPSHOT/rollback"; fi
}
vm_start() {
    if [ -z "$PVE_ENV" ]; then qm start "$VM_ID"; else pve_task POST /status/start; fi
}
vm_ping() {
    if [ -z "$PVE_ENV" ]; then qm agent "$VM_ID" ping > /dev/null 2>&1; else pve POST /agent/ping < /dev/null > /dev/null 2>&1; fi
}

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
    vm_stop
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
vm_rollback
vm_start

echo "lab-host-cycle: waiting for the guest agent"
i=0
until vm_ping; do
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
# A temporary file in the same folder, then a rename: the service that watches the file never sees it empty or partial.
# The restart afterwards is for the case where it started anyway, on an earlier change, before the content was whole.
printf '%s' "$JIT" | vm_run 'umask 077 && cat > /etc/ws-lab/jitconfig.new && [ -s /etc/ws-lab/jitconfig.new ] && mv /etc/ws-lab/jitconfig.new /etc/ws-lab/jitconfig && systemctl restart ws-lab-runner.service'
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
