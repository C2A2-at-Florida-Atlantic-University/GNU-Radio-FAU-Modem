#!/bin/bash
#
# run_volk_profile.sh
# Run VOLK's on-target kernel profiler and capture its full output to a text
# file (and the console).
#
# WHY: on this cortex-a9 target, the default SIMD/NEON VOLK kernels produce
# WRONG results for some operations (confirmed: gr-filter resampler/FIR qa tests
# output zeros on the NEON path but PASS with VOLK_GENERIC=1). volk_profile
# benchmarks every kernel implementation, verifies correctness against the
# generic reference, and writes the best *correct* one per machine to
#   ~/.volk/volk_config   (or $VOLK_CONFIGPATH/.volk/volk_config)
# which lets GNU Radio avoid the broken NEON kernel WITHOUT forcing all-generic.
#
# This script captures the profiler run for the record and for diffing which
# kernels VOLK selected on each board.
#
# Usage:
#   run_volk_profile.sh                 # full profile, log to ~/volk_profile_<host>_<ts>.log
#   run_volk_profile.sh --tests-only    # run correctness tests only, no benchmark/update
#   run_volk_profile.sh --log <path>    # explicit logfile
#   run_volk_profile.sh --no-update     # benchmark+test but do NOT overwrite volk_config
#
# After a normal run, re-run the filter qa tests WITHOUT VOLK_GENERIC to confirm
# the written config fixed them:
#   python -m pytest /usr/lib/gnuradio/ptest/gr-filter/python/filter/qa_rational_resampler.py -v
#
set -u

LOGFILE=""
EXTRA_ARGS=()
MODE="profile"   # profile | tests-only

while [ $# -gt 0 ]; do
    case "$1" in
        --log)         shift; LOGFILE="${1:-}" ;;
        --tests-only)  MODE="tests-only" ;;
        --no-update)   EXTRA_ARGS+=( "--update" "false" ) ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,30p'
            exit 0 ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

# Default logfile (hostname distinguishes 7010 vs 7020).
if [ -z "${LOGFILE}" ]; then
    LOGFILE="${HOME}/volk_profile_$(uname -n)_$(date +%Y%m%d_%H%M%S).log"
fi

# Ensure logfile is writable; fall back to /tmp.
logdir="$(dirname "${LOGFILE}")"
if ! mkdir -p "${logdir}" 2>/dev/null || ! touch "${LOGFILE}" 2>/dev/null; then
    echo "NOTE: cannot write ${LOGFILE}; using /tmp instead." >&2
    LOGFILE="/tmp/$(basename "${LOGFILE}")"
fi

# Locate the volk_profile binary.
VOLK_BIN="$(command -v volk_profile || true)"
if [ -z "${VOLK_BIN}" ]; then
    for c in /usr/bin/volk_profile /usr/local/bin/volk_profile; do
        [ -x "${c}" ] && VOLK_BIN="${c}" && break
    done
fi

{
    echo "==================================================================="
    echo "VOLK profile run"
    echo "  host:    $(uname -n)"
    echo "  date:    $(date)"
    echo "  arch:    $(uname -m)"
    echo "  mode:    ${MODE}"
    echo "  logfile: ${LOGFILE}"
    echo "==================================================================="

    if [ -z "${VOLK_BIN}" ]; then
        echo "ERROR: volk_profile not found on PATH or in /usr/bin." >&2
        echo "       It ships in the 'volk' package — confirm it is installed:" >&2
        echo "         ls /usr/bin/volk_profile" >&2
        echo "       (The library libvolk is present; the profiler tool is a" >&2
        echo "        separate file that may not be in the headless payload.)" >&2
        exit 1
    fi
    echo "volk_profile: ${VOLK_BIN}"
    "${VOLK_BIN}" --version 2>&1 || true
    echo "-------------------------------------------------------------------"

    if [ "${MODE}" = "tests-only" ]; then
        # Run correctness tests without writing a config (-R matches all by regex;
        # --update false avoids overwriting volk_config). Captures which kernels
        # pass/fail their correctness check vs the generic reference.
        echo "Running VOLK kernel correctness tests (no config update)..."
        "${VOLK_BIN}" --update false 2>&1
    else
        # Full profile: benchmark + correctness, then write the best correct
        # implementation per kernel to ~/.volk/volk_config.
        echo "Running full VOLK profile (benchmark + correctness + config write)..."
        echo "This is slow on cortex-a9 — expect several minutes."
        "${VOLK_BIN}" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} 2>&1
    fi
    prc=$?

    echo "-------------------------------------------------------------------"
    echo "volk_profile exit: ${prc}"
    # Show where the config landed and its contents, if written.
    for cfg in "${HOME}/.volk/volk_config" "${VOLK_CONFIGPATH:-}/.volk/volk_config"; do
        if [ -n "${cfg}" ] && [ -f "${cfg}" ]; then
            echo "=== volk_config: ${cfg} ==="
            cat "${cfg}"
        fi
    done
    echo "==================================================================="
    echo "Next: re-run the filter qa tests WITHOUT VOLK_GENERIC to confirm the"
    echo "selected kernels are correct, e.g.:"
    echo "  python -m pytest /usr/lib/gnuradio/ptest/gr-filter/python/filter/qa_rational_resampler.py -v"
    echo "==================================================================="
    exit "${prc}"
} 2>&1 | tee "${LOGFILE}"

rc="${PIPESTATUS[0]}"
echo "Log written to: ${LOGFILE}"
exit "${rc}"