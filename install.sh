#!/usr/bin/env bash
# ws installer: preflight, install the ws command with uv or pipx, smoke test, next steps.
#
# Structure adapted from the claw-code installer (https://github.com/ultraworkers/claw-code, install.sh).
# The cargo build steps are replaced by a pipx/uv install; the rest of the shape (colour only on a terminal,
# preflight with the exact install line for what is missing, a smoke test, a next-steps block and a failure trap
# that prints troubleshooting) is kept. The claw-code licence, which covers that adapted structure:
#
#   MIT License
#
#   Copyright (c) 2026 UltraWorkers and Claw Code contributors
#
#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in all
#   copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#   SOFTWARE.
#
# Usage: see `bash install.sh --help`.
#
# Everything that does work lives in main(), which is called on the last line. A download that stops part way, as
# `curl ... | bash` can, then defines some functions and runs nothing.

set -euo pipefail

REPO_SLUG="Michael-A-Fry/ws"
REPO_URL="https://github.com/${REPO_SLUG}"
RAW_URL="https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh"
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
    cat <<EOF
Usage: bash install.sh [options]
   or: curl -fsSL ${RAW_URL} | bash -s -- [options]

Installs the ws command with uv or pipx, then checks it runs. By default it installs the wheel of the latest GitHub
release: it downloads the wheel and its .sha256 into a temporary folder, checks the SHA-256 and, when cosign is on
your PATH, also checks the wheel's signature bundle against this repo's release workflow. A check that fails stops
the install. Until a release exists, use --from with a checkout.

Options:
  --version TAG   Install that release (for example --version vX.Y.Z) instead of the latest. Checked the same way.
  --pypi          Install ${PACKAGE} from PyPI instead of GitHub. No checksum or signature check is made here.
  --from PATH     Install from a checkout of ws on this machine (a folder with pyproject.toml), not GitHub. For the
                  end-to-end timing script and for trying a change; no download, no checks.
  --no-verify     Skip the SHA-256 and cosign checks of a release download. Loud warning; the smoke test still runs.
  --skip-smoke-test
                  Skip the smoke test (ws --version, ws doctor --offline).
  --dry-run       Run the preflight, look up the release and print what would be installed; change nothing.
  -h, --help      Show this help text and exit.

Environment overrides:
  WS_SKIP_VERIFY       set to 1 to skip the smoke test, as --skip-smoke-test does
  WS_RELEASE_BASE_URL  fetch releases from here instead of GitHub; https, or http on 127.0.0.1 or localhost only (the
                       tests use it). Every check still runs, but the wheel and its .sha256 then come from that
                       host, so only the cosign check proves they are the release: install cosign if you use it.
EOF
}

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

  ${COLOR_BOLD}5. The release route fails${COLOR_RESET}
     It needs curl or wget, sha256sum or shasum, and a network that reaches github.com. Releases: ${REPO_URL}/releases
     "No release yet" or a rate-limit answer from GitHub: name one with --version vX.Y.Z, or install a checkout:
       git clone ${REPO_URL}.git ws && bash ws/install.sh --from ws
     A checksum or signature failure means the download is not the release: do not use it. Try again, then open an issue.

  ${COLOR_BOLD}6. Still stuck${COLOR_RESET}
     Run it again with --dry-run to see the plan, then open an issue with the output:
       ${REPO_URL}/issues

EOF
}

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

# ---------------------------------------------------------------------------
# The release route: look up, download, check
# ---------------------------------------------------------------------------

WORKDIR=""

cleanup() {
    if [ -n "${WORKDIR}" ] && [ -d "${WORKDIR}" ]; then
        rm -rf -- "${WORKDIR}"
    fi
    WORKDIR=""
}

# GETs a URL into a file and prints the HTTP status. Returns non-zero when no answer came at all (network, TLS, DNS).
# curl if there is one, else wget; the redirect to GitHub's asset host has to stay on https.
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

# ---------------------------------------------------------------------------
# main: the whole run, so a partial download runs nothing
# ---------------------------------------------------------------------------

main() {
    local tag="" tag_given="0" from_pypi="0" from_path="" dry_run="0" no_verify="0" skip_smoke="${WS_SKIP_VERIFY:-0}"

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --version)
                if [ "$#" -ge 2 ]; then
                    tag="$2"
                    tag_given="1"
                    shift
                else
                    error "--version needs a release tag, for example --version vX.Y.Z"
                    exit 2
                fi
                ;;
            --pypi)
                from_pypi="1"
                ;;
            --from)
                if [ "$#" -ge 2 ] && [ -n "$2" ]; then
                    from_path="$2"
                    shift
                else
                    error "--from needs the folder of a ws checkout, for example --from ."
                    exit 2
                fi
                ;;
            --no-verify)
                no_verify="1"
                ;;
            --skip-smoke-test)
                skip_smoke="1"
                ;;
            --dry-run)
                dry_run="1"
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

    # The tag ends up in a URL and a file name, so it has to be exactly what the release workflow builds: vX.Y.Z.
    if [ "${tag_given}" = "1" ]; then
        if ! is_release_tag "${tag}"; then
            error "invalid release tag: '${tag}' (a release is named vX.Y.Z, for example v0.1.0)"
            exit 2
        fi
        if [ "${from_pypi}" = "1" ]; then
            error "--version and --pypi do not go together: a tag is a GitHub thing"
            exit 2
        fi
    fi

    if [ -n "${from_path}" ]; then
        if [ "${tag_given}" = "1" ] || [ "${from_pypi}" = "1" ]; then
            error "--from does not go with --version or --pypi: it installs the checkout as it is"
            exit 2
        fi
        if [ ! -f "${from_path}/pyproject.toml" ]; then
            error "--from ${from_path}: no pyproject.toml there, so it is not a ws checkout"
            exit 2
        fi
        from_path="$(cd "${from_path}" && pwd)"
    fi

    # Where releases come from. The override moves the download (the tests point it at a local server); it has no
    # say in whether the checks run. Anything but https, or http to this machine, is refused.
    local api_url="https://api.github.com/repos/${REPO_SLUG}/releases/latest"
    local download_base="${REPO_URL}/releases/download"
    if [ -n "${WS_RELEASE_BASE_URL:-}" ]; then
        local base="${WS_RELEASE_BASE_URL%/}"
        # The host is matched whole: no login part (`user@host`), and a local http host ends at a port or a slash.
        if ! printf '%s' "${base}" | grep -Eq '^(https://[A-Za-z0-9.-]+(:[0-9]+)?|http://(127\.0\.0\.1|localhost)(:[0-9]+)?)(/[^@]*)?$'; then
            error "WS_RELEASE_BASE_URL must be https, or http on 127.0.0.1 or localhost, with no login part: '${base}'"
            exit 2
        fi
        api_url="${base}/repos/${REPO_SLUG}/releases/latest"
        download_base="${base}/${REPO_SLUG}/releases/download"
    fi

    # shellcheck disable=SC2154  # rc is set inside the trap string, when it fires
    trap 'rc=$?; cleanup; if [ "$rc" -ne 0 ]; then error "installation failed (exit ${rc})"; print_troubleshooting; fi' EXIT

    printf '%sws installer%s\n' "${COLOR_BOLD}" "${COLOR_RESET}"
    if [ "${dry_run}" = "1" ]; then
        warn "dry run: nothing will be installed"
    fi

    # ----- Step 1: detect OS -------------------------------------------------

    step "Detecting host environment"

    local uname_s uname_m is_wsl="0"
    uname_s="$(uname -s 2>/dev/null || echo unknown)"
    uname_m="$(uname -m 2>/dev/null || echo unknown)"
    OS_FAMILY="unknown"

    case "${uname_s}" in
        Linux*)
            OS_FAMILY="linux"
            if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
                is_wsl="1"
            fi
            ;;
        Darwin*)
            OS_FAMILY="macos"
            ;;
        MINGW*|MSYS*|CYGWIN*)
            OS_FAMILY="windows-shell"
            ;;
    esac

    info "uname:        ${uname_s} ${uname_m}"
    info "os family:    ${OS_FAMILY}"
    if [ "${is_wsl}" = "1" ]; then
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
            error "Unsupported or unknown OS: ${uname_s}"
            error "Supported: Linux, macOS, and Windows via WSL2."
            exit 1
            ;;
    esac

    # ----- Step 2: preflight -------------------------------------------------

    step "Checking prerequisites"

    local missing="0" python_ok="0"
    PYTHON_BIN=""
    INSTALLER=""

    if PYTHON_BIN="$(find_python)"; then
        ok "python found: ${PYTHON_BIN} ($("${PYTHON_BIN}" --version 2>&1))"
        python_ok="1"
    fi

    if require_cmd uv; then
        INSTALLER="uv"
        ok "uv found: $(uv --version 2>/dev/null </dev/null || echo unknown)"
    elif require_cmd pipx; then
        INSTALLER="pipx"
        ok "pipx found: $(pipx --version 2>/dev/null </dev/null || echo unknown)"
    else
        error "neither uv nor pipx is installed. Install one of them, then run this script again:"
        installer_hint 1>&2
        missing="1"
    fi

    if [ "${python_ok}" = "0" ]; then
        if [ "${INSTALLER}" = "uv" ]; then
            warn "no Python 3.11 or newer on PATH; uv will fetch one when it installs ws"
        else
            error "Python 3.11 or newer is required and was not found. Install it, then run this script again:"
            python_hint 1>&2
            missing="1"
        fi
    fi

    if [ "${from_pypi}" = "0" ] && [ -z "${from_path}" ]; then
        if ! require_cmd curl && ! require_cmd wget; then
            error "installing a release needs curl or wget, and neither is on PATH (or use --pypi, or --from with a checkout)"
            missing="1"
        fi
        if [ "${no_verify}" = "0" ] && ! require_cmd sha256sum && ! require_cmd shasum; then
            error "checking a release needs sha256sum or shasum, and neither is on PATH (Linux: coreutils; macOS has shasum)"
            missing="1"
        fi
    fi

    if [ "${missing}" != "0" ]; then
        error "Missing required tools. See troubleshooting below."
        exit 1
    fi

    # ----- Step 3: install ---------------------------------------------------

    step "Installing ws"

    local install_src
    if [ "${from_pypi}" = "1" ]; then
        install_src="${PACKAGE}"
        info "source: PyPI (${PACKAGE}); no checksum or signature is checked on this route"
    elif [ -n "${from_path}" ]; then
        install_src="${from_path}"
        info "source: checkout at ${install_src}"
    else
        if [ -n "${WS_RELEASE_BASE_URL:-}" ]; then
            warn "WS_RELEASE_BASE_URL is set: releases come from ${WS_RELEASE_BASE_URL%/}, not from GitHub"
        fi
        if [ "${no_verify}" = "1" ]; then
            warn "--no-verify: the SHA-256 and the signature of the download will NOT be checked."
            warn "Anyone who can alter the download can alter what runs on this machine. Continue only if you accept that."
        fi
        # These two set globals and exit on failure themselves: a command substitution would lose WORKDIR.
        if [ "${tag_given}" = "0" ]; then
            find_latest_release "${api_url}" "${dry_run}"
            tag="${RELEASE_TAG}"
        fi
        if [ -n "${tag}" ]; then
            info "release: ${tag}"
            fetch_release "${download_base}" "${tag}" "${no_verify}" "${dry_run}"
            install_src="${INSTALL_SRC}"
        else
            install_src="<wheel of the latest release>"
        fi
    fi

    # A checkout changes under the same version number, so uv must not serve it from its cache of built wheels.
    local uv_flags="--force"
    if [ -n "${from_path}" ]; then
        uv_flags="--force --reinstall"
    fi

    local path_fix uninstall_cmd
    if [ "${INSTALLER}" = "uv" ]; then
        path_fix="uv tool update-shell"
        uninstall_cmd="uv tool uninstall ${PACKAGE}"
        info "running: uv tool install ${uv_flags} ${install_src}"
    else
        path_fix="pipx ensurepath"
        uninstall_cmd="pipx uninstall ${PACKAGE}"
        info "running: pipx install --python ${PYTHON_BIN} --force ${install_src}"
    fi

    if [ "${dry_run}" = "1" ]; then
        ok "dry run: not installed"
    else
        # The installers get no stdin: a child that reads it would eat the rest of a script piped into bash.
        if [ "${INSTALLER}" = "uv" ]; then
            # shellcheck disable=SC2086  # the flags are words on purpose
            uv tool install ${uv_flags} "${install_src}" </dev/null
        else
            pipx install --python "${PYTHON_BIN}" --force "${install_src}" </dev/null
        fi
        ok "installed ${install_src}"

        # PyPI holds a 0.0.0 placeholder; if that is what arrived, say so rather than carry on.
        local ws_bin version_out
        if ws_bin="$(find_ws)"; then
            version_out="$("${ws_bin}" --version 2>&1 </dev/null || true)"
            case "${version_out}" in
                *" 0.0.0")
                    error "installed ${version_out}, which is the PyPI placeholder and not ws. Install a release from GitHub (the default)."
                    exit 1
                    ;;
            esac
        fi
    fi

    # ----- Step 4: smoke test ------------------------------------------------

    step "Verifying the installed command"

    if [ "${dry_run}" = "1" ]; then
        info "dry run: would run: ws --version, then ws doctor --offline"
    elif [ "${skip_smoke}" = "1" ]; then
        warn "smoke test skipped (--skip-smoke-test or WS_SKIP_VERIFY=1)"
    else
        local ws_bin version_out doctor_out doctor_rc=0
        if ! ws_bin="$(find_ws)"; then
            error "ws was installed but is not on PATH and not in the usual folders."
            error "Run: ${path_fix}, then open a new terminal."
            exit 1
        fi
        if ! require_cmd ws; then
            warn "ws is at ${ws_bin}, which is not on your PATH yet; see Next steps"
        fi

        info "running: ws --version"
        if version_out="$("${ws_bin}" --version 2>&1 </dev/null)"; then
            ok "ws --version -> ${version_out}"
        else
            error "ws --version failed:"
            printf '%s\n' "${version_out}" 1>&2
            exit 1
        fi

        # Exit 1 is the normal answer on a fresh machine (roles not set up yet). Anything else means it did not run.
        info "running: ws doctor --offline"
        doctor_out="$("${ws_bin}" doctor --offline 2>&1 </dev/null)" || doctor_rc=$?
        printf '%s\n' "${doctor_out}"
        case "${doctor_rc}" in
            0) ok "ws doctor ran and found nothing to do" ;;
            1) ok "ws doctor ran; it lists what is still to set up, which is expected on a new machine" ;;
            *)
                error "ws doctor did not run properly (exit ${doctor_rc})"
                exit 1
                ;;
        esac
    fi

    # ----- Step 5: next steps ------------------------------------------------

    step "Next steps"

    if [ "${dry_run}" = "1" ]; then
        cat <<EOF
Dry run finished: the preflight passed and nothing was installed.
Run it for real without --dry-run.
EOF
    else
        cat <<EOF
${COLOR_GREEN}ws is installed.${COLOR_RESET}

  ${COLOR_BOLD}ws setup${COLOR_RESET}    ${COLOR_DIM}# answer a few questions and choose your roles${COLOR_RESET}
  ${COLOR_BOLD}ws doctor${COLOR_RESET}   ${COLOR_DIM}# lists anything still to set up; run it first, and again after setup${COLOR_RESET}

If 'ws' is not found in a new terminal, run: ${path_fix}
To remove ws later: ${uninstall_cmd}
EOF
    fi

    # clear the failure trap on a clean exit
    cleanup
    trap - EXIT
}

main "$@"
