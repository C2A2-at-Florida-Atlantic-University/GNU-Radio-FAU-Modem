#!/bin/bash
#
# extract_gnuradio.bash
# Extract the headless GNU Radio runtime payload from the PetaLinux RPM deploy
# pools for BOTH the 7010 and 7020 builds, producing one .tar.gz per board.
#
# Behavior:
#   - Iterates over both boards. If a board's deploy dir or required RPMs are
#     missing, prints a WARNING and moves on (does not abort the other board).
#   - Resolves RPM filenames by glob, so it survives git-hash / release bumps
#     (no hardcoded version string).
#   - NO python3.10 -> python3.12 directory migration. With the
#     python3targetconfig fix the build now emits cpython-312 extensions under
#     python3.12/site-packages directly; copying mismatched ABI binaries was the
#     original bug and is removed.
#   - ABI GUARD: after extraction, refuses to package a board if any
#     cpython-310 / cpython-311 tagged .so is present (wrong-ABI build slipped
#     through). This is the failure mode that previously shipped broken modems.
#
set -u

REPO_ROOT="../"
ARCH="cortexa9t2hf_neon"

# Board name -> its PetaLinux project path (relative to REPO_ROOT).
BOARDS=("7010" "7020")
declare -A PROJ
PROJ[7010]="build_7010/petalinux_7010_os"
PROJ[7020]="build_7020/petalinux_7020_os"

# Runtime package set (no version/arch suffix — resolved by glob below).
PKGS=(
    "gnuradio"
    "gnuradio-runtime"
    "gnuradio-audio"
    "gnuradio-fec"
    "gnuradio-filter"
    "gnuradio-gr-utils"
    "gnuradio-grc"
)

# Expected target Python ABI. Anything else in the staged tree is a hard stop.
WANT_PYTAG="cpython-312"
BAD_PYTAGS_GLOB="*.cpython-31[01]-*.so"   # 310 or 311 = wrong ABI for a 3.12 target

overall_rc=0

extract_board() {
    local board="$1"
    local rpm_dir="${REPO_ROOT}/${PROJ[$board]}/build/tmp/deploy/rpm/${ARCH}"
    local stage="${HOME}/gnuradio_headless_staging_${board}"
    local out="${HOME}/gnuradio_headless_${board}.tar.gz"

    echo "==================================================================="
    echo "Board ${board}"
    echo "  RPM dir: ${rpm_dir}"

    if [ ! -d "${rpm_dir}" ]; then
        echo "  WARNING: deploy dir not found. Skipping ${board}." >&2
        overall_rc=1
        return
    fi

    # Fresh staging tree.
    rm -rf "${stage}"
    mkdir -p "${stage}"

    local missing=0
    local extracted=0
    local pkg file
    for pkg in "${PKGS[@]}"; do
        # Resolve the actual RPM by glob (handles auto-incremented hash/release).
        # Guard against the glob matching nothing.
        local matches=( "${rpm_dir}/${pkg}-"*"${ARCH}.rpm" )
        if [ ! -e "${matches[0]}" ]; then
            echo "  WARNING: no RPM found for package '${pkg}'." >&2
            missing=$((missing+1))
            continue
        fi
        if [ "${#matches[@]}" -gt 1 ]; then
            echo "  WARNING: multiple RPMs matched '${pkg}', using first:" >&2
            printf '           %s\n' "${matches[@]}" >&2
        fi
        file="${matches[0]}"
        echo "  Extracting: $(basename "${file}")"
        ( cd "${stage}" && rpm2cpio "${file}" | cpio -idm ) > /dev/null 2>&1
        extracted=$((extracted+1))
    done

    if [ "${extracted}" -eq 0 ]; then
        echo "  WARNING: no packages extracted for ${board}. Skipping tarball." >&2
        overall_rc=1
        return
    fi
    if [ "${missing}" -gt 0 ]; then
        echo "  WARNING: ${missing} package(s) missing for ${board}; tarball will be incomplete." >&2
        overall_rc=1
    fi

    # ABI GUARD: a wrong-tag .so means the build did not link the 3.12 target
    # Python. Do NOT package it — that ships a modem that ModuleNotFoundErrors
    # or segfaults. Surface it loudly instead.
    local bad
    bad=$(find "${stage}" -name "${BAD_PYTAGS_GLOB}" 2>/dev/null)
    if [ -n "${bad}" ]; then
        echo "  ERROR: wrong-ABI extension modules found (expected ${WANT_PYTAG}):" >&2
        echo "${bad}" | sed 's/^/           /' >&2
        echo "  ERROR: build linked the wrong Python. Fix the recipe" >&2
        echo "         (inherit python3targetconfig), cleansstate + rebuild." >&2
        echo "  Refusing to package ${board}." >&2
        overall_rc=1
        return
    fi

    # Sanity: confirm the expected tag is actually present (catch an empty/odd tree).
    if ! find "${stage}" -name "*.${WANT_PYTAG}-*.so" 2>/dev/null | grep -q .; then
        echo "  WARNING: no ${WANT_PYTAG} .so found in staged tree for ${board}." >&2
        echo "           Packaging anyway, but verify the payload is correct." >&2
        overall_rc=1
    fi

    if [ ! -d "${stage}/usr" ]; then
        echo "  WARNING: no 'usr/' tree produced for ${board}. Skipping tarball." >&2
        overall_rc=1
        return
    fi

    tar -czf "${out}" -C "${stage}" usr/
    echo "  OK: headless ${board} package -> ${out}"
}

for b in "${BOARDS[@]}"; do
    extract_board "${b}"
done

echo "==================================================================="
if [ "${overall_rc}" -ne 0 ]; then
    echo "Completed with warnings/errors (see above)."
else
    echo "Both boards packaged cleanly."
fi
exit "${overall_rc}"