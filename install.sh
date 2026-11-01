#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# ws installer. Run it from a checkout or straight from the web:
#
#   bash install.sh [options]
#   curl -fsSL https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh | bash -s -- [options]
#
# By default it installs the wheel of the latest GitHub release, after checking the wheel's SHA-256 (and its cosign
# signature when cosign 3 or newer is installed). Everything runs inside main(), and the last line of this file calls it,
# so a download that stops part way defines some functions and runs nothing.
#
# Exit codes: 0 done (or a dry run that passed), 1 a failure (an installer's own exit code is passed on), 2 bad usage.

set -euo pipefail

REPO_SLUG="Michael-A-Fry/ws"
REPO_URL="https://github.com/${REPO_SLUG}"
RAW_URL="https://raw.githubusercontent.com/${REPO_SLUG}/main/install.sh"
PACKAGE="ws-aitoolkit"
TOTAL_STEPS=5

# State shared by the phases below.
CURRENT_STEP=0
OS_FAMILY="unknown"
PYTHON_BIN=""
INSTALLER=""
WORKDIR=""

RESET="" BOLD="" DIM="" RED="" GREEN="" YELLOW="" BLUE="" CYAN=""

# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

# Colour only on a terminal that can show it, so logs and pipes never hold escape codes.
setup_colour() {
    local colours=""
    if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
        colours="$(tput colors 2>/dev/null </dev/null || true)"
        case "${colours}" in '' | *[!0-9]*) colours=0 ;; esac
        if [ "${colours}" -ge 8 ]; then
            RESET="$(tput sgr0 </dev/null)"
            BOLD="$(tput bold </dev/null)"
            DIM="$(tput dim </dev/null)"
            RED="$(tput setaf 1 </dev/null)"
            GREEN="$(tput setaf 2 </dev/null)"
            YELLOW="$(tput setaf 3 </dev/null)"
            BLUE="$(tput setaf 4 </dev/null)"
            CYAN="$(tput setaf 6 </dev/null)"
        fi
    fi
}

step() {
    CURRENT_STEP=$((CURRENT_STEP + 1))
    printf '\n%s[%s/%s]%s %s%s%s\n' "${BLUE}" "${CURRENT_STEP}" "${TOTAL_STEPS}" "${RESET}" "${BOLD}" "$1" "${RESET}"
}
info() { printf '  %s->%s %s\n' "${CYAN}" "${RESET}" "$1"; }
ok() { printf '  %sok%s %s\n' "${GREEN}" "${RESET}" "$1"; }
warn() { printf '  %swarn%s %s\n' "${YELLOW}" "${RESET}" "$1"; }
error() { printf '  %serror%s %s\n' "${RED}" "${RESET}" "$1" >&2; }

usage() {
    cat <<USAGE
Usage: bash install.sh [options]
       curl -fsSL ${RAW_URL} | bash -s -- [options]

Installs the ws command with uv or pipx. With no options it installs the latest GitHub
release, after checking its SHA-256 (and its cosign signature, when cosign 3+ is installed).

Options:
  --version TAG       install this release (a tag like v0.1.0) instead of the latest
  --pypi              install ${PACKAGE} from PyPI instead of a release
  --from PATH         install a checkout of the repo that is on this machine
  --no-verify         do not check the release's SHA-256 or signature (not recommended)
  --skip-smoke-test   do not run 'ws --version' and 'ws doctor --offline' afterwards
  --move-existing-ws-to PATH
                      if another program named 'ws' is in the install folder, rename it to PATH (a rename,
                      never a delete) and, after the install, set it as the program that gets words ws lacks
                      ('ws config fallback PATH')
  --dry-run           run the checks and say what would happen; install nothing
  --help              show this text

Environment:
  WS_RELEASE_BASE_URL  fetch releases from this address instead of GitHub (https, or http on
                       this machine); it moves the download and never switches a check off
  WS_SKIP_VERIFY=1     the same as --skip-smoke-test

Exit codes: 0 done, 1 failed, 2 bad usage.
USAGE
}

usage_error() {
    error "$1"
    usage
    exit 2
}

print_troubleshooting() {
    printf '\n%sTroubleshooting%s\n%s---------------%s\n' "${BOLD}" "${RESET}" "${DIM}" "${RESET}"
    printf '\n%s1. Neither uv nor pipx is installed%s\n' "${BOLD}" "${RESET}"
    printf '   uv, any OS:       curl -LsSf https://astral.sh/uv/install.sh | sh\n'
    printf '   macOS:            brew install uv   (or: brew install pipx)\n'
    printf '   Debian/Ubuntu:    sudo apt-get install -y pipx\n'
    printf '   Fedora/RHEL:      sudo dnf install -y pipx\n'
    printf '   Arch:             sudo pacman -S --needed python-pipx\n'
    printf '\n%s2. Python is older than 3.11%s\n' "${BOLD}" "${RESET}"
    printf '   ws needs Python 3.11 or newer. With uv you can skip this: uv fetches one.\n'
    printf '   Debian/Ubuntu:    sudo apt-get install -y python3\n'
    printf '   macOS:            brew install python@3.12\n'
    printf '\n%s3. You are on Windows%s\n' "${BOLD}" "${RESET}"
    printf '   ws runs inside WSL2, not in native Windows. From an administrator PowerShell:\n'
    printf '     wsl --install -d Ubuntu-24.04\n'
    printf '   then run this script again inside that Ubuntu shell.\n'
    printf '\n%s4. ws is not found after the install%s\n' "${BOLD}" "${RESET}"
    printf '   The install folder is not on your PATH yet. Run one of these, then open a new terminal:\n'
    printf '     uv tool update-shell        (installed with uv)\n'
    printf '     pipx ensurepath             (installed with pipx)\n'
    printf '\n%s5. The release download fails%s\n' "${BOLD}" "${RESET}"
    printf '   It needs curl or wget, sha256sum or shasum, and a network that reaches github.com.\n'
    printf '   Releases: %s/releases\n' "${REPO_URL}"
    printf '   "No release yet" or a rate limit: name a release with --version vX.Y.Z, or install a checkout:\n'
    printf '     git clone %s.git ws && bash ws/install.sh --from ws\n' "${REPO_URL}"
    printf '   A checksum or signature failure means the download is not the release. Do not use it.\n'
    printf '\n%s6. Still stuck%s\n' "${BOLD}" "${RESET}"
    printf '   Run again with --dry-run, then open an issue with the output: %s/issues\n' "${REPO_URL}"
}

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

require_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# Prints the name of the first Python on PATH that is 3.11 or newer; returns 1 when there is none.
find_python() {
    local candidate
    for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
        if require_cmd "${candidate}" &&
            "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' </dev/null >/dev/null 2>&1; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    return 1
}

# debian, fedora, arch or other, from the whole words of ID and ID_LIKE in /etc/os-release.
linux_family() {
    local ids=""
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        ids="$(. /etc/os-release && printf '%s %s' "${ID:-}" "${ID_LIKE:-}")"
    fi
    case " ${ids} " in
        *" debian "* | *" ubuntu "*) printf 'debian' ;;
        *" fedora "* | *" rhel "* | *" centos "*) printf 'fedora' ;;
        *" arch "*) printf 'arch' ;;
        *) printf 'other' ;;
    esac
}

installer_hint() {
    if [ "${OS_FAMILY}" = "macos" ]; then
        printf '       brew install uv        (or: curl -LsSf https://astral.sh/uv/install.sh | sh)\n'
        return 0
    fi
    printf '       curl -LsSf https://astral.sh/uv/install.sh | sh\n'
    case "$(linux_family)" in
        debian) printf '       or: sudo apt-get install -y pipx && pipx ensurepath\n' ;;
        fedora) printf '       or: sudo dnf install -y pipx && pipx ensurepath\n' ;;
        arch) printf '       or: sudo pacman -S --needed python-pipx && pipx ensurepath\n' ;;
        *) printf '       or install pipx with your package manager, then: pipx ensurepath\n' ;;
    esac
}

python_hint() {
    if [ "${OS_FAMILY}" = "macos" ]; then
        printf '       brew install python@3.12\n'
        return 0
    fi
    case "$(linux_family)" in
        debian) printf '       sudo apt-get update && sudo apt-get install -y python3\n' ;;
        fedora) printf '       sudo dnf install -y python3\n' ;;
        arch) printf '       sudo pacman -S --needed python\n' ;;
        *) printf '       install Python 3.11 or newer with your package manager\n' ;;
    esac
}

# Removes the download folder. Safe to call twice.
cleanup() {
    if [ -n "${WORKDIR}" ] && [ -d "${WORKDIR}" ]; then
        rm -rf -- "${WORKDIR}"
    fi
    WORKDIR=""
}

# The EXIT trap once the arguments are valid: tidy up, and on a failure say so and print the troubleshooting text.
# The script's exit status is left as it was, so an installer's own code (7, say) reaches the caller.
on_exit() {
    local status=$?
    cleanup
    if [ "${status}" -ne 0 ]; then
        error "installation failed (exit ${status})"
        print_troubleshooting
    fi
}

# Accepts https, or plain http to this machine (the tests' fake release server). No login part, no path tricks.
valid_base_url() {
    local url="$1" rest host name port
    case "${url}" in
        *[[:space:]]* | *@* | *\\* | *\?* | *\#*) return 1 ;;
    esac
    case "${url}" in
        https://*)
            rest="${url#https://}"
            host="${rest%%/*}"
            [ -n "${host}" ]
            return $?
            ;;
        http://*)
            rest="${url#http://}"
            host="${rest%%/*}"
            name="${host%%:*}"
            case "${name}" in localhost | 127.0.0.1) ;; *) return 1 ;; esac
            if [ "${host}" != "${name}" ]; then
                port="${host#*:}"
                case "${port}" in '' | *[!0-9]*) return 1 ;; esac
            fi
            return 0
            ;;
    esac
    return 1
}

# ---------------------------------------------------------------------------
# The release route: look up, download, check
# ---------------------------------------------------------------------------

http_get() {
    local url="$1" dest="$2" hdrs rc=0
    if require_cmd curl; then
        curl -sSL --connect-timeout 20 --max-time 300 --proto-redir '=https' -o "${dest}" -w '%{http_code}' "${url}" </dev/null
        return $?
    fi
    hdrs="${dest}.headers"
    wget -q -S --timeout=30 --tries=2 -O "${dest}" "${url}" 2>"${hdrs}" </dev/null || rc=$?
    if [ "${rc}" = "0" ]; then
        printf '200'
    elif [ "${rc}" = "8" ]; then # the server answered with an error status; the last HTTP line is the final answer
        sed -n 's|^ *HTTP/[0-9.]* \([0-9][0-9]*\).*|\1|p' "${hdrs}" | tail -n 1
        rc=0
    fi
    rm -f -- "${hdrs}"
    return "${rc}"
}

# Checks FILE against its sha256sum-format line in SUMFILE, both inside DIR. Linux has sha256sum, macOS has shasum.
check_sha256() {
    local dir="$1" sumfile="$2"
    if require_cmd sha256sum; then
        (cd "${dir}" && sha256sum -c "${sumfile}" </dev/null)
    else
        (cd "${dir}" && shasum -a 256 -c "${sumfile}" </dev/null)
    fi
}

# Reads the tag name out of the GitHub "latest release" JSON on stdin (no jq needed: the key is on a line of its own).
tag_from_json() {
    sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | sed 1q
}

# A plain release tag, vX.Y.Z: the release workflow only builds those, and the wheel's file name comes from it.
is_release_tag() {
    printf '%s\n' "$1" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'
}

make_workdir() {
    if [ -z "${WORKDIR}" ]; then
        WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/ws-install.XXXXXX")"
    fi
}

# Sets RELEASE_TAG to the latest release's tag. Exits 1 when it cannot, except in a dry run, which warns and leaves
# the tag empty so the rest of the preflight still prints.
RELEASE_TAG=""
find_latest_release() {
    local url="$1" dry="$2" status="" why=""
    RELEASE_TAG=""
    make_workdir
    if status="$(http_get "${url}" "${WORKDIR}/latest.json")"; then
        case "${status}" in
            200)
                RELEASE_TAG="$(tag_from_json <"${WORKDIR}/latest.json")"
                if is_release_tag "${RELEASE_TAG}"; then
                    return 0
                fi
                why="GitHub's answer did not hold a release tag like vX.Y.Z"
                RELEASE_TAG=""
                ;;
            404)
                if [ "${dry}" = "1" ]; then
                    warn "no release has been published yet; a real run needs --from with a checkout (or --version once one exists)"
                    return 0
                fi
                error "no release has been published yet at ${REPO_URL}/releases"
                error "Install a checkout instead: git clone ${REPO_URL}.git ws && bash ws/install.sh --from ws"
                exit 1
                ;;
            *) why="GitHub answered HTTP ${status} (a rate limit looks like this)" ;;
        esac
    else
        why="could not reach ${url}"
    fi
    if [ "${dry}" = "1" ]; then
        warn "${why}; a real run would stop here"
        return 0
    fi
    error "could not find the latest release: ${why}"
    error "Check the network, or name a release with --version vX.Y.Z (${REPO_URL}/releases)"
    exit 1
}

# Downloads URL to DEST, or exits 1 saying what came back.
download_asset() {
    local url="$1" dest="$2" status=""
    if ! status="$(http_get "${url}" "${dest}")"; then
        error "download failed (no answer): ${url}"
        exit 1
    fi
    if [ "${status}" != "200" ]; then
        error "download failed (HTTP ${status}): ${url}"
        exit 1
    fi
}

# Sets INSTALL_SRC to the wheel of release TAG, downloaded into WORKDIR and checked. A dry run only says what it would do.
INSTALL_SRC=""
fetch_release() {
    local base="$1" tag="$2" skip_checks="$3" dry="$4"
    local wheel="ws_aitoolkit-${tag#v}-py3-none-any.whl" out
    local url="${base}/${tag}/${wheel}"
    local identity="https://github.com/${REPO_SLUG}/.github/workflows/release.yml@refs/tags/${tag}"
    local issuer="https://token.actions.githubusercontent.com"

    if [ "${dry}" = "1" ]; then
        info "would download: ${url}"
        if [ "${skip_checks}" = "0" ]; then
            info "would check:    ${wheel}.sha256, and the signature bundle when cosign is installed"
        fi
        INSTALL_SRC="${wheel}"
        return 0
    fi

    make_workdir
    info "downloading ${wheel}"
    download_asset "${url}" "${WORKDIR}/${wheel}"
    INSTALL_SRC="${WORKDIR}/${wheel}"
    if [ "${skip_checks}" = "1" ]; then
        return 0
    fi

    download_asset "${url}.sha256" "${WORKDIR}/${wheel}.sha256"
    if ! grep -Fq -- "  ${wheel}" "${WORKDIR}/${wheel}.sha256" || ! out="$(check_sha256 "${WORKDIR}" "${wheel}.sha256" 2>&1)"; then
        error "the SHA-256 of ${wheel} does not match the release's .sha256 file. Nothing was installed."
        error "The download is damaged or is not the release. Do not use it."
        exit 1
    fi
    ok "sha256 matches: ${out}"

    local cosign_major=""
    if require_cmd cosign; then
        # pure bash: `cosign version` prints "GitVersion:    v3.1.4" among other lines
        local line
        while IFS= read -r line; do
            case "${line}" in
                GitVersion:*)
                    line="${line#GitVersion:}"
                    line="${line#"${line%%[![:space:]]*}"}"
                    line="${line#v}"
                    line="${line%%.*}"
                    case "${line}" in ''|*[!0-9]*) ;; *) cosign_major="${line}" ;; esac
                    break
                    ;;
            esac
        done <<EOF_COSIGN
$(cosign version 2>/dev/null </dev/null || true)
EOF_COSIGN
    fi
    if [ -n "${cosign_major}" ] && [ "${cosign_major}" -lt 3 ]; then
        warn "cosign ${cosign_major}.x can't read the release's signature bundle (cosign 3 or newer can), so only the SHA-256 was checked; upgrade cosign to check the signature too"
    elif require_cmd cosign; then
        download_asset "${url}.sigstore.json" "${WORKDIR}/${wheel}.sigstore.json"
        if ! out="$(cosign verify-blob --bundle "${WORKDIR}/${wheel}.sigstore.json" \
            --certificate-identity "${identity}" --certificate-oidc-issuer "${issuer}" \
            "${WORKDIR}/${wheel}" 2>&1 </dev/null)"; then
            printf '%s\n' "${out}" 1>&2
            error "the signature of ${wheel} did not verify as made by this repo's release workflow for ${tag}. Nothing was installed."
            exit 1
        fi
        ok "cosign signature verifies (made by the release workflow for ${tag})"
    else
        info "cosign is not installed, so only the SHA-256 was checked; install cosign to check the signature too"
    fi
}

# Where the installed `ws` is: on PATH, else where uv or pipx put commands.
find_ws() {
    local bin_dir
    if require_cmd ws; then
        command -v ws
        return 0
    fi
    if [ "${INSTALLER}" = "uv" ]; then
        bin_dir="$(uv tool dir --bin 2>/dev/null </dev/null || true)"
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

# The folder the installer puts the ws command in: uv's tool bin folder, else pipx's.
install_bin_dir() {
    local dir=""
    if [ "${INSTALLER}" = "uv" ]; then
        dir="$(uv tool dir --bin 2>/dev/null </dev/null || true)"
    fi
    printf '%s' "${dir:-${PIPX_BIN_DIR:-${HOME}/.local/bin}}"
}

# Follows a chain of symlinks to the file at the end (at most 20 steps), without needing readlink -f.
resolve_link() {
    local p="$1" target n=0
    while [ -L "${p}" ] && [ "${n}" -lt 20 ] && require_cmd readlink; do
        target="$(readlink "${p}" 2>/dev/null </dev/null || true)"
        [ -n "${target}" ] || break
        case "${target}" in
            /*) p="${target}" ;;
            *) p="${p%/*}/${target}" ;;
        esac
        n=$((n + 1))
    done
    printf '%s' "${p}"
}

# True when the file at PATH_ARG is this toolkit: it lives in the ws-aitoolkit tool environment, or its
# `version --json` answers in the ws.cli/1 shape with an install_source field.
is_this_toolkit() {
    local file="$1" real answer
    real="$(resolve_link "${file}")"
    case "${real}" in */ws-aitoolkit/*) return 0 ;; esac
    [ -f "${real}" ] && [ -x "${real}" ] || return 1
    answer="$("${real}" version --json 2>/dev/null </dev/null || true)"
    case "${answer}" in
        *'"ws.cli/1"'*'"install_source"'*) return 0 ;;
    esac
    return 1
}

# If the install folder already holds a different `ws`, stops with no changes and prints the two commands that
# keep it working as the fallback; with a move target it does the rename instead. A dry run only says what it would do.
# Sets MOVED_WS to the new place of a program it moved.
MOVED_WS=""
check_existing_ws() {
    local dry="$1" move_to="$2" bin_dir existing
    bin_dir="$(install_bin_dir)"
    existing="${bin_dir}/ws"
    if [ ! -e "${existing}" ] && [ ! -L "${existing}" ]; then
        [ -z "${move_to}" ] || info "no other 'ws' in ${bin_dir}, so there is nothing to move"
        return 0
    fi
    if is_this_toolkit "${existing}"; then
        [ -z "${move_to}" ] || info "the 'ws' in ${bin_dir} is this toolkit, so there is nothing to move"
        return 0
    fi
    if [ -n "${move_to}" ]; then
        if [ -e "${move_to}" ] || [ -L "${move_to}" ]; then
            error "${move_to} already exists; choose a name that is free for --move-existing-ws-to"
            exit 1
        fi
        if [ "${dry}" = "1" ]; then
            info "would rename ${existing} to ${move_to}, then run: ws config fallback ${move_to}"
            return 0
        fi
        mv -- "${existing}" "${move_to}" ||
            {
                error "could not rename ${existing} to ${move_to}"
                exit 1
            }
        ok "renamed ${existing} to ${move_to}"
        MOVED_WS="${move_to}"
        return 0
    fi
    if [ "${dry}" = "1" ]; then
        warn "another program named 'ws' is at ${existing}; a real run would stop here"
    else
        error "another program named 'ws' is at ${existing}. Nothing was changed."
    fi
    printf '       1. move it aside:        mv %s %s-legacy\n' "${existing}" "${existing}" >&2
    printf '       2. after the install:    ws config fallback %s-legacy\n' "${existing}" >&2
    printf '          (ws then hands it every word ws has no command for)\n' >&2
    printf '       or let the installer do both: bash install.sh --move-existing-ws-to %s-legacy\n' "${existing}" >&2
    if [ "${dry}" = "1" ]; then
        return 0
    fi
    exit 1
}

# After the install: make the moved program the fallback, using the ws that was just installed.
set_moved_fallback() {
    local ws_bin="$1" target="$2"
    if ! "${ws_bin}" config fallback "${target}" </dev/null; then
        error "ws is installed, but setting the fallback failed. Run: ws config fallback ${target}"
        exit 1
    fi
    ok "words ws lacks now go to ${target}"
}

# ---------------------------------------------------------------------------
# main: the whole run, so a partial download runs nothing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

detect_os() {
    local os_name os_arch wsl=0
    os_name="$(uname -s 2>/dev/null || true)"
    os_arch="$(uname -m 2>/dev/null || true)"
    os_name="${os_name:-unknown}"
    os_arch="${os_arch:-unknown}"
    case "${os_name}" in
        Linux*)
            OS_FAMILY="linux"
            if grep -qi -e microsoft -e wsl /proc/version 2>/dev/null </dev/null; then
                wsl=1
            fi
            ;;
        Darwin*) OS_FAMILY="macos" ;;
        MINGW* | MSYS* | CYGWIN*) OS_FAMILY="windows-shell" ;;
        *) OS_FAMILY="unknown" ;;
    esac
    info "uname:        ${os_name} ${os_arch}"
    info "os family:    ${OS_FAMILY}"
    if [ "${wsl}" = "1" ]; then
        info "wsl:          yes"
    fi
    case "${OS_FAMILY}" in
        linux | macos) ok "supported platform detected" ;;
        windows-shell)
            error "This is a native Windows shell. ws runs inside WSL2, not in native Windows."
            error "From an administrator PowerShell run: wsl --install -d Ubuntu-24.04"
            error "Then run this script again inside that Ubuntu shell."
            exit 1
            ;;
        *)
            error "Unsupported or unknown OS: ${os_name}"
            error "Supported: Linux, macOS, and Windows via WSL2."
            exit 1
            ;;
    esac
}

# Looks at everything before stopping, so one run lists every tool that is missing.
preflight() {
    local release_route="$1" no_verify="$2" missing=0 version=""

    PYTHON_BIN="$(find_python)" || PYTHON_BIN=""
    if [ -n "${PYTHON_BIN}" ]; then
        version="$("${PYTHON_BIN}" --version 2>&1 </dev/null || true)"
        ok "python found: ${PYTHON_BIN} (${version})"
    fi

    INSTALLER=""
    if require_cmd uv; then
        INSTALLER="uv"
        version="$(uv --version 2>/dev/null </dev/null || true)"
        ok "uv found: ${version:-unknown}"
    elif require_cmd pipx; then
        INSTALLER="pipx"
        version="$(pipx --version 2>/dev/null </dev/null || true)"
        ok "pipx found: ${version:-unknown}"
    else
        error "neither uv nor pipx is installed. Install one of them, then run this script again:"
        installer_hint >&2
        missing=1
    fi

    if [ -z "${PYTHON_BIN}" ]; then
        if [ "${INSTALLER}" = "uv" ]; then
            warn "no Python 3.11 or newer on PATH; uv will fetch one when it installs ws"
        else
            error "Python 3.11 or newer is required and was not found. Install it, then run this script again:"
            python_hint >&2
            missing=1
        fi
    fi

    if [ "${release_route}" = "1" ]; then
        if ! require_cmd curl && ! require_cmd wget; then
            error "installing a release needs curl or wget, and neither is on PATH (or use --pypi, or --from with a checkout)"
            missing=1
        fi
        if [ "${no_verify}" = "0" ] && ! require_cmd sha256sum && ! require_cmd shasum; then
            error "checking a release needs sha256sum or shasum, and neither is on PATH (Linux: coreutils; macOS has shasum)"
            missing=1
        fi
    fi

    if [ "${missing}" = "1" ]; then
        error "Missing required tools. See troubleshooting below."
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# main: the whole run, so a partial download runs nothing
# ---------------------------------------------------------------------------

main() {
    local dry_run=0 from_pypi=0 from_path="" have_version=0 version_tag="" no_verify=0 skip_smoke=0
    local override="${WS_RELEASE_BASE_URL:-}" api_base="https://api.github.com" download_base="https://github.com"
    local path_fix="" uninstall_cmd="" run_cmd="" ws_bin="" out="" code=0 move_existing=""

    if [ "${WS_SKIP_VERIFY:-0}" = "1" ]; then
        skip_smoke=1
    fi
    setup_colour

    while [ $# -gt 0 ]; do
        case "$1" in
            --help | -h)
                usage
                exit 0
                ;;
            --version)
                [ $# -ge 2 ] || usage_error "--version needs a release tag such as v0.1.0"
                have_version=1
                version_tag="$2"
                shift 2
                ;;
            --pypi)
                from_pypi=1
                shift
                ;;
            --from)
                [ $# -ge 2 ] || usage_error "--from needs the folder of a checkout"
                [ -n "${2//[[:space:]]/}" ] || usage_error "--from needs the folder of a checkout"
                from_path="$2"
                shift 2
                ;;
            --no-verify)
                no_verify=1
                shift
                ;;
            --skip-smoke-test)
                skip_smoke=1
                shift
                ;;
            --dry-run)
                dry_run=1
                shift
                ;;
            --move-existing-ws-to)
                [ $# -ge 2 ] || usage_error "--move-existing-ws-to needs a new path for the existing ws"
                move_existing="$2"
                shift 2
                ;;
            *) usage_error "unknown argument: $1" ;;
        esac
    done

    if [ "${have_version}" = "1" ]; then
        case "${version_tag}" in *[!A-Za-z0-9.]*) usage_error "--version takes a release tag like v1.2.3" ;; esac
        is_release_tag "${version_tag}" || usage_error "--version takes a release tag like v1.2.3, not '${version_tag}'"
        [ "${from_pypi}" = "0" ] || usage_error "--version and --pypi cannot be used together"
        [ -z "${from_path}" ] || usage_error "--version and --from cannot be used together"
    fi
    if [ -n "${from_path}" ]; then
        [ "${from_pypi}" = "0" ] || usage_error "--from and --pypi cannot be used together"
        [ -f "${from_path}/pyproject.toml" ] || usage_error "--from needs the folder of a ws checkout (no pyproject.toml in '${from_path}')"
        from_path="$(cd "${from_path}" && pwd -P)"
    fi
    if [ -n "${move_existing}" ]; then
        case "${move_existing}" in
            /*) ;;
            *) usage_error "--move-existing-ws-to needs an absolute path, not '${move_existing}'" ;;
        esac
        case "${move_existing}" in */) usage_error "--move-existing-ws-to needs a file path, not a folder" ;; esac
    fi
    if [ -n "${override}" ]; then
        valid_base_url "${override}" ||
            usage_error "WS_RELEASE_BASE_URL must be an https address, or http to this machine, with no login part"
        override="${override%/}"
        api_base="${override}"
        download_base="${override}"
    fi

    printf '%sws installer%s\n' "${BOLD}" "${RESET}"
    if [ "${dry_run}" = "1" ]; then
        warn "dry run: nothing will be installed"
    fi
    trap on_exit EXIT

    step "Detecting host environment"
    detect_os

    local release_route=1
    if [ "${from_pypi}" = "1" ] || [ -n "${from_path}" ]; then
        release_route=0
    fi
    step "Checking prerequisites"
    preflight "${release_route}" "${no_verify}"

    step "Installing ws"
    if [ "${INSTALLER}" = "uv" ]; then
        path_fix="uv tool update-shell"
        uninstall_cmd="uv tool uninstall ${PACKAGE}"
    else
        path_fix="pipx ensurepath"
        uninstall_cmd="pipx uninstall ${PACKAGE}"
    fi
    check_existing_ws "${dry_run}" "${move_existing}"
    INSTALL_SRC=""
    if [ -n "${from_path}" ]; then
        INSTALL_SRC="${from_path}"
        info "source: the checkout at ${from_path}"
    elif [ "${from_pypi}" = "1" ]; then
        INSTALL_SRC="${PACKAGE}"
        info "source: ${PACKAGE} on PyPI"
    else
        if [ -n "${override}" ]; then
            warn "WS_RELEASE_BASE_URL is set: releases come from ${override}. The checks still run."
        fi
        if [ "${no_verify}" = "1" ]; then
            warn "--no-verify: the SHA-256 and the signature of the download will NOT be checked"
        fi
        if [ "${have_version}" = "1" ]; then
            RELEASE_TAG="${version_tag}"
        else
            find_latest_release "${api_base}/repos/${REPO_SLUG}/releases/latest" "${dry_run}"
        fi
        if [ -n "${RELEASE_TAG}" ]; then
            info "release: ${RELEASE_TAG}"
            fetch_release "${download_base}/${REPO_SLUG}/releases/download" "${RELEASE_TAG}" "${no_verify}" "${dry_run}"
        fi
    fi

    if [ -n "${INSTALL_SRC}" ]; then
        if [ "${INSTALLER}" = "uv" ]; then
            run_cmd="uv tool install --force"
            if [ -n "${from_path}" ]; then
                run_cmd="${run_cmd} --reinstall"
            fi
        else
            run_cmd="pipx install --python ${PYTHON_BIN} --force"
        fi
        run_cmd="${run_cmd} ${INSTALL_SRC}"
    fi
    if [ "${dry_run}" = "1" ]; then
        info "dry run: not installed"
        if [ -n "${run_cmd}" ]; then
            info "would run: ${run_cmd}"
        fi
    else
        info "running: ${run_cmd}"
        if [ "${INSTALLER}" = "uv" ]; then
            if [ -n "${from_path}" ]; then
                uv tool install --force --reinstall "${INSTALL_SRC}" </dev/null
            else
                uv tool install --force "${INSTALL_SRC}" </dev/null
            fi
        else
            pipx install --python "${PYTHON_BIN}" --force "${INSTALL_SRC}" </dev/null
        fi
        # The PyPI name once held a placeholder at 0.0.0. Whatever the other switches say, that is not ws.
        if ws_bin="$(find_ws)"; then
            out="$("${ws_bin}" --version 2>&1 </dev/null || true)"
            case "${out}" in
                *" 0.0.0")
                    error "installed ${out}, which is the PyPI placeholder and not ws. Install a release from GitHub (the default)."
                    exit 1
                    ;;
            esac
        fi
        if [ -n "${MOVED_WS}" ]; then
            ws_bin="$(install_bin_dir)/ws"
            [ -x "${ws_bin}" ] || ws_bin="$(find_ws)" || {
                error "ws was installed but is not on PATH, so the fallback is not set. After fixing PATH run: ws config fallback ${MOVED_WS}"
                exit 1
            }
            set_moved_fallback "${ws_bin}" "${MOVED_WS}"
        fi
    fi

    step "Verifying the installed command"
    if [ "${dry_run}" = "1" ]; then
        info "dry run: would run: ws --version, then ws doctor --offline"
    elif [ "${skip_smoke}" = "1" ]; then
        warn "smoke test skipped (--skip-smoke-test or WS_SKIP_VERIFY=1)"
    else
        if ! ws_bin="$(find_ws)"; then
            error "ws was installed but is not on PATH and not in the usual folders."
            error "Run: ${path_fix}, then open a new terminal."
            exit 1
        fi
        if ! require_cmd ws; then
            warn "ws is at ${ws_bin}, which is not on your PATH yet; see Next steps"
        fi
        info "running: ws --version"
        if ! out="$("${ws_bin}" --version 2>&1 </dev/null)"; then
            error "ws --version failed:"
            printf '%s\n' "${out}" >&2
            exit 1
        fi
        ok "ws --version -> ${out}"
        info "running: ws doctor --offline"
        out="$("${ws_bin}" doctor --offline 2>&1 </dev/null)" || code=$?
        printf '%s\n' "${out}"
        case "${code}" in
            0) ok "ws doctor ran and found nothing to do" ;;
            1) ok "ws doctor ran; it lists what is still to set up, which is expected on a new machine" ;;
            *)
                error "ws doctor did not run properly (exit ${code})"
                exit 1
                ;;
        esac
    fi

    step "Next steps"
    if [ "${dry_run}" = "1" ]; then
        printf '  Dry run finished: the preflight passed and nothing was installed.\n'
        printf '  Run it for real without --dry-run.\n'
    else
        printf '  %sws is installed.%s\n\n' "${GREEN}" "${RESET}"
        printf '    %s%-10s%s %s# answer a few questions and choose your roles%s\n' "${BOLD}" "ws setup" "${RESET}" "${DIM}" "${RESET}"
        printf '    %s%-10s%s %s# lists anything still to set up; run it first, and again after setup%s\n' "${BOLD}" "ws doctor" "${RESET}" "${DIM}" "${RESET}"
        printf "\n  If 'ws' is not found in a new terminal, run: %s\n" "${path_fix}"
        printf '  To remove ws later: %s\n' "${uninstall_cmd}"
    fi

    cleanup
    trap - EXIT
}

main "$@"
