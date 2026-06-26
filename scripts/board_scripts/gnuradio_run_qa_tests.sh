#!/bin/bash
#
# run_gr_qa.sh
# Locate and run the GNU Radio qa_*.py regression tests on-target WITHOUT ctest
# (the upstream run-ptest wrapper calls ctest, which isn't installed on the
# headless image — and shouldn't be). This drives the same Python unittest files
# directly via pytest, which IS present.
#
# The qa tests live nested under the ptest tree, e.g.
#   <root>/gr-blocks/python/blocks/qa_*.py
# so a flat glob misses them; this finds them recursively.
#
# SCOPING: by default it runs only the components whose bindings were actually
# built into this image (PACKAGECONFIG="zeromq", DTV/UHD/QTGUI/VOCODER/etc OFF).
# qa files for non-built components import modules that don't exist and fail for
# reasons unrelated to build integrity. Use --all to run everything anyway.
#
# Usage:
#   run_gr_qa.sh                 # shipped components only (default)
#   run_gr_qa.sh --all           # every qa_*.py found (expect import failures)
#   run_gr_qa.sh --list          # just list the qa files that would run
#   run_gr_qa.sh --root <dir>    # override test root (default: autodetect)
#
set -u

# --- Components whose bindings this image actually ships. ---
# Matches the extraction PKGS set. qa files under these subtrees are the only
# ones expected to pass; others reference modules not present on the target.
SHIPPED_COMPONENTS=(
    "gnuradio-runtime"
    "gr-blocks"
    "gr-digital"
    "gr-filter"
    "gr-fft"
    "gr-analog"
    "gr-fec"
    "gr-channels"
    "gr-zeromq"
    "gr-pdu"        # pdu bindings ship with runtime/blocks
)

# --- Candidate locations for the ptest tree (first that exists wins). ---
CANDIDATE_ROOTS=(
    "/usr/lib/gnuradio/ptest"
    "/usr/lib/gnuradio/ptests"
    "/usr/share/gnuradio/ptest"
)

MODE="shipped"      # shipped | all | list
ROOT=""
# Preserve the original argument list so the tee re-exec can replay it exactly
# (the parse loop below consumes $@ via shift).
ORIG_ARGS=( "$@" )
# Per-test timeout (seconds). Requires pytest-timeout; if absent, ignored with a
# note. A hung test (e.g. gr-zeromq pub/sub blocking on a socket recv) will then
# fail instead of stalling the whole run forever.
TIMEOUT="${GR_QA_TIMEOUT:-60}"
# Components to skip by default. gr-zeromq's qa_zeromq_pubsub.py blocks on a
# network socket with no internal timeout and hangs on slow targets — it is a
# test-harness issue, not a binding defect. Validate zeromq separately with a
# bounded functional test. Override with SKIP_COMPONENTS="" to include it.
read -r -a SKIP_COMPONENTS <<< "${SKIP_COMPONENTS:-gr-zeromq}"
# Logfile: all console output is also written here (tee). Default is a
# timestamped file in $HOME; override with --log <path> or GR_QA_LOG=<path>.
# Set --log "" (empty) to disable file logging.
LOGFILE="${GR_QA_LOG-}"
LOG_SET=0   # track whether the user explicitly set --log

while [ $# -gt 0 ]; do
    case "$1" in
        --all)        MODE="all" ;;
        --list)       MODE="list" ;;
        --root)       shift; ROOT="${1:-}" ;;
        --timeout)    shift; TIMEOUT="${1:-60}" ;;
        --no-skip)    SKIP_COMPONENTS=() ;;
        --log)        shift; LOGFILE="${1:-}"; LOG_SET=1 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,34p'
            exit 0 ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

# Default logfile if not explicitly set/disabled.
if [ "${LOG_SET}" -eq 0 ] && [ -z "${LOGFILE}" ]; then
    LOGFILE="${HOME}/gr_qa_$(uname -n)_$(date +%Y%m%d_%H%M%S).log"
fi

# --- Re-exec through tee so ALL output (this script + pytest) is logged. ---
# Guard with GR_QA_TEEING to avoid an infinite re-exec loop. Using a FIFO + tee
# preserves the live console while capturing to file; exit status is taken from
# the script body via PIPESTATUS.
if [ -n "${LOGFILE}" ] && [ -z "${GR_QA_TEEING:-}" ]; then
    export GR_QA_TEEING=1
    # Ensure the log dir exists; fall back to /tmp if HOME isn't writable.
    logdir="$(dirname "${LOGFILE}")"
    if ! mkdir -p "${logdir}" 2>/dev/null || ! touch "${LOGFILE}" 2>/dev/null; then
        echo "NOTE: cannot write ${LOGFILE}; logging to /tmp instead." >&2
        LOGFILE="/tmp/$(basename "${LOGFILE}")"
    fi
    # Re-run self with identical args, piping combined stdout+stderr through tee.
    # Invoke via `bash "$0"` (not "$0" directly) so it works even when the script
    # lacks +x or lives on a noexec mount. ${ORIG_ARGS[@]+...} guards set -u.
    {
        bash "$0" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
    } 2>&1 | tee "${LOGFILE}"
    rc="${PIPESTATUS[0]}"
    echo "Log written to: ${LOGFILE}"
    exit "${rc}"
fi

# --- Resolve the test root. ---
if [ -z "${ROOT}" ]; then
    for r in "${CANDIDATE_ROOTS[@]}"; do
        if [ -d "${r}" ]; then ROOT="${r}"; break; fi
    done
fi
if [ -z "${ROOT}" ] || [ ! -d "${ROOT}" ]; then
    echo "ERROR: could not find the GNU Radio ptest tree." >&2
    echo "       Tried: ${CANDIDATE_ROOTS[*]}" >&2
    echo "       Pass --root <dir> explicitly." >&2
    exit 1
fi
echo "ptest root: ${ROOT}"

# --- Ensure pytest is available (not needed for --list). ---
if [ "${MODE}" != "list" ] && ! python -m pytest --version >/dev/null 2>&1; then
    echo "ERROR: 'python -m pytest' is unavailable. Install python3-pytest" >&2
    echo "       (add it to the image IMAGE_INSTALL / extraction PYDEPS)." >&2
    exit 1
fi

# --- Collect qa_*.py files. ---
# In 'shipped' mode, keep only files whose path contains a shipped component dir.
mapfile -t ALL_QA < <(find "${ROOT}" -name 'qa_*.py' -type f 2>/dev/null | sort)

if [ "${#ALL_QA[@]}" -eq 0 ]; then
    echo "ERROR: no qa_*.py files found under ${ROOT}." >&2
    exit 1
fi

QA_FILES=()
if [ "${MODE}" = "all" ]; then
    QA_FILES=( "${ALL_QA[@]}" )
else
    # shipped (and list) mode: filter to shipped component subtrees
    for f in "${ALL_QA[@]}"; do
        for c in "${SHIPPED_COMPONENTS[@]}"; do
            case "${f}" in
                *"/${c}/"*) QA_FILES+=( "${f}" ); break ;;
            esac
        done
    done
fi

if [ "${#QA_FILES[@]}" -eq 0 ]; then
    echo "No qa files matched the shipped-component filter." >&2
    echo "(${#ALL_QA[@]} total qa files exist; use --all to run them.)" >&2
    exit 1
fi

# --- Apply skip list (e.g. gr-zeromq, which hangs on slow targets). ---
if [ "${#SKIP_COMPONENTS[@]}" -gt 0 ]; then
    KEPT=()
    for f in "${QA_FILES[@]}"; do
        skip=0
        for s in "${SKIP_COMPONENTS[@]}"; do
            [ -z "${s}" ] && continue
            case "${f}" in *"/${s}/"*) skip=1; break ;; esac
        done
        [ "${skip}" -eq 0 ] && KEPT+=( "${f}" )
    done
    if [ "${#KEPT[@]}" -ne "${#QA_FILES[@]}" ]; then
        echo "Skipping components: ${SKIP_COMPONENTS[*]}"
        echo "  (these hang or need fixtures the ctest harness normally provides;"
        echo "   validate them separately with a bounded functional test.)"
    fi
    QA_FILES=( "${KEPT[@]}" )
fi

# --- List mode: show and exit. ---
if [ "${MODE}" = "list" ]; then
    echo "Would run ${#QA_FILES[@]} qa file(s):"
    printf '  %s\n' "${QA_FILES[@]}"
    exit 0
fi

echo "Running ${#QA_FILES[@]} qa file(s) (mode: ${MODE})."
[ "${MODE}" = "all" ] && echo "NOTE: --all includes components whose bindings were NOT built; expect import failures for those."
echo "==================================================================="

# --- Run. Each qa file is an independent unittest module; run them as a batch
#     but continue past failures so you get a full tally. -p no:cacheprovider
#     avoids pytest trying to write a cache to a read-only rootfs. ---
PYTEST_ARGS=( -p no:cacheprovider --continue-on-collection-errors -rfE )

# Per-test timeout: prevents a single hung test (socket-blocking, etc.) from
# stalling the entire run. Only added if pytest-timeout is installed.
if python -c "import pytest_timeout" >/dev/null 2>&1; then
    PYTEST_ARGS+=( "--timeout=${TIMEOUT}" "--timeout-method=thread" )
    echo "Per-test timeout: ${TIMEOUT}s (pytest-timeout present)."
else
    echo "NOTE: pytest-timeout not installed — no per-test timeout. A hung test"
    echo "      will stall the run. Add python3-pytest-timeout to the payload, or"
    echo "      keep gr-zeromq in SKIP_COMPONENTS (default) to avoid the known hang."
fi

python -m pytest "${PYTEST_ARGS[@]}" "${QA_FILES[@]}"
rc=$?

echo "==================================================================="
if [ "${rc}" -eq 0 ]; then
    echo "RESULT: all selected qa tests passed."
else
    echo "RESULT: pytest exit ${rc} (see failures above)."
    echo "  Reminder: failures referencing missing modules for NON-shipped"
    echo "  components are expected. Focus on the shipped set: ${SHIPPED_COMPONENTS[*]}"
fi
exit "${rc}"