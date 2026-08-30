#!/bin/bash
#
# build_gnuradio.sh
#
# Builds the PetaLinux image (GNU Radio + the gr-fau-modem OOT module) for
# one or both boards, then invokes extract_gnuradio.sh to package the
# resulting RPM deploy pools into headless GNU Radio tarballs.
#
# Both boards' petalinuxbsp.conf already IMAGE_INSTALL the full set of
# gnuradio-* component packages plus gr-fau-modem, so a plain
# `petalinux-build` (no -c) builds everything extract_gnuradio.sh looks for.
#
# Usage:
#   ./build_gnuradio.sh                       # build + extract, both boards
#   ./build_gnuradio.sh -b 7010                # build + extract, 7010 only
#   ./build_gnuradio.sh -c gr-fau-modem        # rebuild just one recipe (fast
#                                               # path after a modem source
#                                               # change), both boards
#   ./build_gnuradio.sh --skip-extract         # build only, don't package
#
set -eu

PETALINUX_SETTINGS="/opt/petalinux/2024.2/settings.sh"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="${REPO_ROOT}/scripts"

BOARDS=("7010" "7020")
declare -A PROJ
PROJ[7010]="${REPO_ROOT}/build_7010/petalinux_7010_os"
PROJ[7020]="${REPO_ROOT}/build_7020/petalinux_7020_os"

BUILD_TARGET=""
ONLY_BOARD=""
SKIP_EXTRACT=0

usage() {
    cat <<EOF
Usage: $0 [-b 7010|7020] [-c <bitbake-target>] [--skip-extract]

  -b BOARD         Build only this board (default: both 7010 and 7020).
  -c TARGET        Build only this bitbake target instead of the full image
                    (e.g. "gr-fau-modem" or "gnuradio"). Default: full image.
  --skip-extract   Build only; don't run extract_gnuradio.sh afterward.
EOF
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        -b) ONLY_BOARD="$2"; shift 2 ;;
        -c) BUILD_TARGET="$2"; shift 2 ;;
        --skip-extract) SKIP_EXTRACT=1; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

if [ -n "${ONLY_BOARD}" ]; then
    if [ -z "${PROJ[${ONLY_BOARD}]+x}" ]; then
        echo "ERROR: unknown board '${ONLY_BOARD}' (expected 7010 or 7020)." >&2
        exit 1
    fi
    BOARDS=("${ONLY_BOARD}")
fi

if [ ! -f "${PETALINUX_SETTINGS}" ]; then
    echo "ERROR: PetaLinux settings not found at ${PETALINUX_SETTINGS}" >&2
    exit 1
fi

# shellcheck disable=SC1090
source "${PETALINUX_SETTINGS}"

overall_rc=0

for board in "${BOARDS[@]}"; do
    proj="${PROJ[${board}]}"
    echo "==================================================================="
    echo "Board ${board}: ${proj}"

    if [ ! -d "${proj}" ]; then
        echo "  ERROR: project directory not found. Skipping ${board}." >&2
        overall_rc=1
        continue
    fi

    if [ -n "${BUILD_TARGET}" ]; then
        echo "  Running: petalinux-build -c ${BUILD_TARGET}"
        if ! ( cd "${proj}" && petalinux-build -c "${BUILD_TARGET}" ); then
            echo "  ERROR: petalinux-build -c ${BUILD_TARGET} failed for ${board}." >&2
            overall_rc=1
            continue
        fi
    else
        echo "  Running: petalinux-build (full image)"
        if ! ( cd "${proj}" && petalinux-build ); then
            echo "  ERROR: petalinux-build failed for ${board}." >&2
            overall_rc=1
            continue
        fi
    fi

    echo "  OK: build finished for ${board}."
done

echo "==================================================================="
if [ "${overall_rc}" -ne 0 ]; then
    echo "One or more board builds failed; skipping extraction." >&2
    exit "${overall_rc}"
fi

if [ "${SKIP_EXTRACT}" -eq 1 ]; then
    echo "Build(s) finished. Skipping extraction (--skip-extract)."
    exit 0
fi

echo "Builds finished. Running extract_gnuradio.sh..."
( cd "${SCRIPTS_DIR}" && bash extract_gnuradio.sh )
exit $?
