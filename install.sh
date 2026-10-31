#!/usr/bin/env bash
# ws installer: preflight, install ws-aitoolkit with uv or pipx, smoke test, next steps.
#
# Structure adapted from the claw-code installer (https://github.com/ultraworkers/claw-code, install.sh),
# Copyright (c) 2026 UltraWorkers and Claw Code contributors, MIT License (see LICENSES/MIT-claw-code.txt).
# The cargo build steps are replaced by a pipx/uv install; the rest of the shape (colour only on a terminal,
# preflight with the exact install line for what is missing, a smoke test, a next-steps block and a failure trap
# that prints troubleshooting) is kept.
#
# Usage:
#   bash install.sh                  install the latest ws-aitoolkit from PyPI
#   bash install.sh --version TAG    install that release tag from GitHub instead
#   bash install.sh --no-verify      skip the smoke test
#   bash install.sh --dry-run        print the steps and change nothing
#   bash install.sh --help           print usage
#
# Environment overrides:
#   WS_SKIP_VERIFY=1    same as --no-verify

set -euo pipefail

REPO_URL="https://github.com/Michael-A-Fry/ws"
PACKAGE="ws-aitoolkit"

# ---------------------------------------------------------------------------
# Pretty printing: colour only when stdout is a terminal that has colours
# ---------------------------------------------------------------------------

if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    COLOR_RESET="$(tput sgr0)"
    COLOR_BOLD="$(tput bold)"
    COLOR_DIM="$(tput dim)"
    COLOR_RED="$(tput setaf 1)"
    COLOR_GREEN="$(tput setaf 2)"
    COLOR_YELLOW="$(tput setaf 3)"
    COLOR_BLUE="$(tput setaf 4)"
    COLOR_CYAN="$(tput setaf 6)"
else
    COLOR_RESET=""
    COLOR_BOLD=""
    COLOR_DIM=""
    COLOR_RED=""
    COLOR_GREEN=""
    COLOR_YELLOW=""
    COLOR_BLUE=""
    COLOR_CYAN=""
fi

CURRENT_STEP=0
TOTAL_STEPS=5

step() {
    CURRENT_STEP=$((CURRENT_STEP + 1))
    printf '\n%s[%d/%d]%s %s%s%s\n' \
        "${COLOR_BLUE}" "${CURRENT_STEP}" "${TOTAL_STEPS}" "${COLOR_RESET}" \
        "${COLOR_BOLD}" "$1" "${COLOR_RESET}"
}

info()  { printf '%s  ->%s %s\n' "${COLOR_CYAN}" "${COLOR_RESET}" "$1"; }
ok()    { printf '%s  ok%s %s\n' "${COLOR_GREEN}" "${COLOR_RESET}" "$1"; }
warn()  { printf '%s  warn%s %s\n' "${COLOR_YELLOW}" "${COLOR_RESET}" "$1"; }
error() { printf '%s  error%s %s\n' "${COLOR_RED}" "${COLOR_RESET}" "$1" 1>&2; }

print_usage() {
    cat <<'EOF'
Usage: bash install.sh [options]

Installs the ws command (PyPI package ws-aitoolkit) with uv or pipx, then checks it runs.

Options:
  --version TAG   Install that release tag from GitHub (git+https) instead of the latest from PyPI.
  --no-verify     Skip the smoke test (ws --version, ws doctor --offline).
  --dry-run       Run the preflight and print what would be installed; change nothing.
  -h, --help      Show this help text and exit.

Environment overrides:
  WS_SKIP_VERIFY  set to 1 to skip the smoke test
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

TAG=""
DRY_RUN="0"
SKIP_VERIFY="${WS_SKIP_VERIFY:-0}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)
            if [ "$#" -lt 2 ]; then
                error "--version needs a release tag, for example --version v0.1.0"
                exit 2
            fi
            TAG="$2"
            shift
            ;;
        --no-verify)
            SKIP_VERIFY="1"
            ;;
        --dry-run)
            DRY_RUN="1"
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            error "unknown argument: $1"
            print_usage
            exit 2
            ;;
    esac
    shift
done

# The tag ends up in a pip requirement, so keep it to what a git tag normally holds.
case "${TAG}" in
    *[!A-Za-z0-9._/-]*)
        error "invalid release tag: ${TAG} (letters, digits and . _ / - only)"
        exit 2
        ;;
esac

# ---------------------------------------------------------------------------
# Troubleshooting, printed by the failure trap
# ---------------------------------------------------------------------------

print_troubleshooting() {
    cat <<EOF

${COLOR_BOLD}Troubleshooting${COLOR_RESET}
${COLOR_DIM}---------------${COLOR_RESET}

  ${COLOR_BOLD}1. Neither uv nor pipx is installed${COLOR_RESET}
     uv, any OS:     curl -LsSf https://astral.sh/uv/install.sh | sh
     macOS:          brew install uv        (or: brew install pipx)
     Debian/Ubuntu:  sudo apt-get install -y pipx
     Fedora/RHEL:    sudo dnf install -y pipx
     Arch:           sudo pacman -S --needed python-pipx

  ${COLOR_BOLD}2. Python older than 3.11${COLOR_RESET}
     ws needs Python 3.11 or newer. With uv you can skip this: uv fetches one for you.
     Debian/Ubuntu:  sudo apt-get install -y python3
     macOS:          brew install python@3.12

  ${COLOR_BOLD}3. Windows${COLOR_RESET}
     ws runs inside WSL2, not in native Windows. From an administrator PowerShell:
       wsl --install -d Ubuntu-24.04
     Then run this script again inside that Ubuntu shell.

  ${COLOR_BOLD}4. 'ws' not found after the install${COLOR_RESET}
     The installer put it in a folder that is not on your PATH yet. Run one of:
       uv tool update-shell
       pipx ensurepath
     then open a new terminal.

  ${COLOR_BOLD}5. --version TAG fails${COLOR_RESET}
     TAG must be a release tag that exists: ${REPO_URL}/tags
     That route needs git on your PATH.

  ${COLOR_BOLD}6. Still stuck${COLOR_RESET}
     Run it again with --dry-run to see the plan, then open an issue with the output:
       ${REPO_URL}/issues

EOF
}

# shellcheck disable=SC2154  # rc is set inside the trap string, when it fires
trap 'rc=$?; if [ "$rc" -ne 0 ]; then error "installation failed (exit ${rc})"; print_troubleshooting; fi' EXIT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

require_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# Prints the first python on PATH that is 3.11 or newer; returns 1 when there is none.
find_python() {
    local candidate
    for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
        if require_cmd "${candidate}" \
            && "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    return 1
}

# Prints "debian", "fedora", "arch" or "other" from /etc/os-release.
linux_family() {
    local ids=""
    if [ -r /etc/os-release ]; then
        # shellcheck source=/dev/null
        ids="$(. /etc/os-release && printf '%s %s' "${ID:-}" "${ID_LIKE:-}")"
    fi
    case " ${ids} " in
        *" debian "*|*" ubuntu "*) printf 'debian' ;;
        *" fedora "*|*" rhel "*|*" centos "*) printf 'fedora' ;;
        *" arch "*) printf 'arch' ;;
        *) printf 'other' ;;
    esac
}

# The exact line that installs uv or pipx on this OS.
installer_hint() {
    case "${OS_FAMILY}" in
        macos)
            printf '       brew install uv        (or: curl -LsSf https://astral.sh/uv/install.sh | sh)\n'
            ;;
        *)
            printf '       curl -LsSf https://astral.sh/uv/install.sh | sh\n'
            case "$(linux_family)" in
                debian) printf '       or: sudo apt-get install -y pipx && pipx ensurepath\n' ;;
                fedora) printf '       or: sudo dnf install -y pipx && pipx ensurepath\n' ;;
                arch)   printf '       or: sudo pacman -S --needed python-pipx && pipx ensurepath\n' ;;
                *)      printf '       or install pipx with your package manager, then: pipx ensurepath\n' ;;
            esac
            ;;
    esac
}

python_hint() {
    case "${OS_FAMILY}" in
        macos) printf '       brew install python@3.12\n' ;;
        *)
            case "$(linux_family)" in
                debian) printf '       sudo apt-get update && sudo apt-get install -y python3\n' ;;
                fedora) printf '       sudo dnf install -y python3\n' ;;
                arch)   printf '       sudo pacman -S --needed python\n' ;;
                *)      printf '       install Python 3.11 or newer with your package manager\n' ;;
            esac
            ;;
    esac
}

# Where the installed `ws` is: on PATH, else where uv or pipx put commands.
find_ws() {
    local bin_dir
    if require_cmd ws; then
        command -v ws
        return 0
    fi
    if [ "${INSTALLER}" = "uv" ]; then
        bin_dir="$(uv tool dir --bin 2>/dev/null || true)"
        if [ -n "${bin_dir}" ] && [ -x "${bin_dir}/ws" ]; then
            printf '%s/ws' "${bin_dir}"
            return 0
        fi
    fi
    bin_dir="${PIPX_BIN_DIR:-${HOME}/.local/bin}"
    if [ -x "${bin_dir}/ws" ]; then
        printf '%s/ws' "${bin_dir}"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Step 1: detect OS
# ---------------------------------------------------------------------------

printf '%sws installer%s\n' "${COLOR_BOLD}" "${COLOR_RESET}"
if [ "${DRY_RUN}" = "1" ]; then
    warn "dry run: nothing will be installed"
fi

step "Detecting host environment"

UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
UNAME_M="$(uname -m 2>/dev/null || echo unknown)"
OS_FAMILY="unknown"
IS_WSL="0"

case "${UNAME_S}" in
    Linux*)
        OS_FAMILY="linux"
        if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
            IS_WSL="1"
        fi
        ;;
    Darwin*)
        OS_FAMILY="macos"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        OS_FAMILY="windows-shell"
        ;;
esac

info "uname:        ${UNAME_S} ${UNAME_M}"
info "os family:    ${OS_FAMILY}"
if [ "${IS_WSL}" = "1" ]; then
    info "wsl:          yes"
fi

case "${OS_FAMILY}" in
    linux|macos)
        ok "supported platform detected"
        ;;
    windows-shell)
        error "This is a native Windows shell. ws runs inside WSL2, not in native Windows."
        error "From an administrator PowerShell run: wsl --install -d Ubuntu-24.04"
        error "Then run this script again inside that Ubuntu shell."
        exit 1
        ;;
    *)
        error "Unsupported or unknown OS: ${UNAME_S}"
        error "Supported: Linux, macOS, and Windows via WSL2."
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Step 2: preflight
# ---------------------------------------------------------------------------

step "Checking prerequisites"

MISSING_PREREQS=0
INSTALLER=""

if PYTHON_BIN="$(find_python)"; then
    ok "python found: ${PYTHON_BIN} ($("${PYTHON_BIN}" --version 2>&1))"
    PYTHON_OK=1
else
    PYTHON_OK=0
fi

if require_cmd uv; then
    INSTALLER="uv"
    ok "uv found: $(uv --version 2>/dev/null || echo unknown)"
elif require_cmd pipx; then
    INSTALLER="pipx"
    ok "pipx found: $(pipx --version 2>/dev/null || echo unknown)"
else
    error "neither uv nor pipx is installed. Install one of them, then run this script again:"
    installer_hint 1>&2
    MISSING_PREREQS=1
fi

if [ "${PYTHON_OK}" = "0" ]; then
    if [ "${INSTALLER}" = "uv" ]; then
        warn "no Python 3.11 or newer on PATH; uv will fetch one when it installs ws"
    else
        error "Python 3.11 or newer is required and was not found. Install it, then run this script again:"
        python_hint 1>&2
        MISSING_PREREQS=1
    fi
fi

if [ -n "${TAG}" ] && ! require_cmd git; then
    error "--version installs from GitHub and needs git, which is not on PATH"
    MISSING_PREREQS=1
fi

if [ "${MISSING_PREREQS}" -ne 0 ]; then
    error "Missing required tools. See troubleshooting below."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 3: install
# ---------------------------------------------------------------------------

step "Installing ${PACKAGE}"

if [ -n "${TAG}" ]; then
    SOURCE="git+${REPO_URL}@${TAG}"
else
    SOURCE="${PACKAGE}"
fi

if [ "${INSTALLER}" = "uv" ]; then
    INSTALL_CMD="uv tool install --force ${SOURCE}"
    PATH_FIX="uv tool update-shell"
    UNINSTALL_CMD="uv tool uninstall ${PACKAGE}"
else
    INSTALL_CMD="pipx install --force ${SOURCE}"
    PATH_FIX="pipx ensurepath"
    UNINSTALL_CMD="pipx uninstall ${PACKAGE}"
fi

info "running: ${INSTALL_CMD}"

if [ "${DRY_RUN}" = "1" ]; then
    ok "dry run: not installed"
else
    # SOURCE is deliberately a single word (the tag was checked above), so the command splits cleanly.
    if [ "${INSTALLER}" = "uv" ]; then
        uv tool install --force "${SOURCE}"
    else
        pipx install --force "${SOURCE}"
    fi
    ok "installed ${SOURCE}"
fi

# ---------------------------------------------------------------------------
# Step 4: smoke test
# ---------------------------------------------------------------------------

step "Verifying the installed command"

if [ "${DRY_RUN}" = "1" ]; then
    info "dry run: would run: ws --version, then ws doctor --offline"
elif [ "${SKIP_VERIFY}" = "1" ]; then
    warn "verification skipped (--no-verify or WS_SKIP_VERIFY=1)"
else
    if ! WS_BIN="$(find_ws)"; then
        error "ws was installed but is not on PATH and not in the usual folders."
        error "Run: ${PATH_FIX}, then open a new terminal."
        exit 1
    fi
    if ! require_cmd ws; then
        warn "ws is at ${WS_BIN}, which is not on your PATH yet; see Next steps"
    fi

    info "running: ws --version"
    if VERSION_OUT="$("${WS_BIN}" --version 2>&1)"; then
        ok "ws --version -> ${VERSION_OUT}"
    else
        error "ws --version failed:"
        printf '%s\n' "${VERSION_OUT}" 1>&2
        exit 1
    fi

    # Exit 1 is the normal answer on a fresh machine (roles not set up yet). Anything else means it did not run.
    info "running: ws doctor --offline"
    DOCTOR_RC=0
    DOCTOR_OUT="$("${WS_BIN}" doctor --offline 2>&1)" || DOCTOR_RC=$?
    printf '%s\n' "${DOCTOR_OUT}"
    case "${DOCTOR_RC}" in
        0) ok "ws doctor ran and found nothing to do" ;;
        1) ok "ws doctor ran; it lists what is still to set up, which is expected on a new machine" ;;
        *)
            error "ws doctor did not run properly (exit ${DOCTOR_RC})"
            exit 1
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# Step 5: next steps
# ---------------------------------------------------------------------------

step "Next steps"

if [ "${DRY_RUN}" = "1" ]; then
    cat <<EOF
Dry run finished: the preflight passed and nothing was installed.
Run it for real without --dry-run.
EOF
else
    cat <<EOF
${COLOR_GREEN}ws is installed.${COLOR_RESET}

  ${COLOR_BOLD}ws setup${COLOR_RESET}    ${COLOR_DIM}# answer a few questions and choose your roles${COLOR_RESET}
  ${COLOR_BOLD}ws doctor${COLOR_RESET}   ${COLOR_DIM}# lists anything still to set up; run it first, and again after setup${COLOR_RESET}

If 'ws' is not found in a new terminal, run: ${PATH_FIX}
To remove ws later: ${UNINSTALL_CMD}
EOF
fi

# clear the failure trap on a clean exit
trap - EXIT
