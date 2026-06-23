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
#   - NO python3.10 -> python3.12 directory migration. The build now emits
#     cpython-312 extensions under python3.12/site-packages directly; copying
#     mismatched ABI binaries was the original bug and is removed. The bindings
#     build correctly because the gnuradio bbappend forces pybind11 to use
#     CMake's cross-aware FindPython (-DPYBIND11_FINDPYTHON=ON) with the target
#     pybind11_DIR, plus -DENABLE_PYTHON=ON. (The legacy FindPythonLibsNew
#     aborts a 32-bit-target / 64-bit-native cross build.)
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
#
# This recipe (meta-sdr) splits each component's cpython-312 binding .so into
# its own gnuradio-<component> package; the base 'gnuradio' package is empty
# (ALLOW_EMPTY) and 'gnuradio-grc'/'gnuradio-gr-utils' are GUI/desktop-only and
# are not built in the headless PACKAGECONFIG ("zeromq"). So the payload is the
# per-component runtime packages, NOT the base package.
#
# gnuradio-gr (core gr module) and gnuradio-pmt (PMT bindings) are mandatory.
# Trim the optional components below to match the modem flowgraph's imports.
#
# IMPORTANT: the base 'gnuradio' package is INCLUDED. It is empty of Python
# bindings (those are in the per-component packages), but it carries the C++
# runtime libraries (libgnuradio-runtime.so.*, libgnuradio-blocks.so.*, ...)
# that every binding dynamically links. Without it, imports fail at load with
# "libgnuradio-runtime.so.*: cannot open shared object file". The standalone
# libgnuradio-<c> runtime packages do NOT exist in this recipe — only their
# -dev variants — so the base package is where the .so live.
PKGS=(
    "gnuradio"           # C++ runtime libs: libgnuradio-*.so.* (REQUIRED for load)
    "gnuradio-runtime"   # __init__, eng_*, unittest helpers (mandatory)
    "gnuradio-gr"        # core gr Python module (mandatory)
    "gnuradio-pmt"       # PMT bindings (mandatory)
    "gnuradio-blocks"    # basic blocks (almost always needed)
    "gnuradio-digital"   # modulation/demod
    "gnuradio-filter"    # filters / resamplers
    "gnuradio-fft"       # FFT blocks
    "gnuradio-analog"    # analog sources/sinks
    "gnuradio-fec"       # forward error correction
    "gnuradio-channels"  # channel models
    "gnuradio-zeromq"    # ZMQ source/sink (matches PACKAGECONFIG "zeromq")
)

# Expected target Python ABI. Anything else in the staged tree is a hard stop.
WANT_PYTAG="cpython-312"
BAD_PYTAGS_GLOB="*.cpython-31[01]-*.so"   # 310 or 311 = wrong ABI for a 3.12 target

# Non-gnuradio C++ runtime dependency packages.
#
# The gnuradio cpython-312 .so bindings link C++ libraries that live in separate
# (non-gnuradio) RPMs. A gnuradio-only payload imports but fails at load time
# (e.g. "libspdlog.so.1.13: cannot open shared object file"). This list ships
# those shared libs so the headless tarball is self-contained WITHOUT a full
# image deploy.
#
# These are plain .so (no cpython tag), so the ABI guard ignores them.
# Package names below are the RUNTIME-lib packages confirmed in the deploy pool,
# NOT the -dev/-meta names:
#   fftw  -> libfftwf  (single-precision; libfftw is the double variant)
#   gmp   -> libgmp10  (runtime SONAME; 'libgmp' is dev/meta)
#   boost -> base + log + serialization (gnuradio-runtime links these)
#
# NOTE: this is a hand-maintained dependency closure (the "Option B" stopgap).
# The post-extraction ldd check below reports anything still unresolved so the
# list can be completed iteratively. The durable fix is image-based deploy.
DEPLIBS=(
    "libspdlog1.13"     # libspdlog.so.1.13
    "libfmt10"          # libfmt.so.*  (spdlog links this)
    "volk"              # libvolk.so.* (SIMD kernels; gnuradio core dep)
    "libfftwf"          # libfftw3f.so.* (single precision)
    "libgmp10"          # libgmp.so.*
    "gsl"               # libgsl.so.*, libgslcblas.so.*
    "libsndfile1"       # libsndfile.so.* (gnuradio-blocks wavfile)
    # --- boost: base + the per-library split packages the bindings link ---
    "boost"             # base libboost_*.so.*
    "boost-log"
    "boost-serialization"
    "libboost-filesystem1.84.0"        # libboost_filesystem.so.1.84.0
    "libboost-program-options1.84.0"   # libboost_program_options.so.1.84.0
    "libboost-thread1.84.0"            # libboost_thread.so.1.84.0
    # --- audio codecs (kept because gnuradio-audio is retained) ---
    "libflac12"         # libFLAC.so.12
    "libogg0"           # libogg.so.0
    "libvorbis"         # libvorbis.so.0, libvorbisenc.so.2
    "libasound2"        # libasound.so.2
    # --- zeromq transport ---
    "zeromq"            # libzmq.so.5
    # --- pyyaml C extension dependency ---
    "libyaml0.2"        # libyaml.so.0.2 (pyyaml _yaml C extension links this)
)

# Pure-Python runtime dependencies (NOT shared libraries).
#
# GNU Radio's Python modules `import` other Python packages that are NOT
# ELF-linked, so the readelf closure check below CANNOT see them — they surface
# only as ModuleNotFoundError at runtime on the target. Known imports:
#   gnuradio/blocks/variable_save_restore.py -> import yaml   (python3-pyyaml)
#   gnuradio core / many modules             -> import numpy  (python3-numpy)
# python3-pyzmq is already pulled by the zeromq blocks' python side.
#
# These are packaged as python3-<name>; their files land under
# site-packages/, so they integrate into the same usr/ tree.
PYDEPS=(
    "python3-pyyaml"    # import yaml (blocks.variable_save_restore)
    "python3-numpy"     # import numpy (pervasive)
    "python3-pyzmq"     # import zmq (gnuradio-zeromq python side)
)

overall_rc=0

# Extract a single package's RPM (resolved by glob) into the staging tree.
# Args: <pkg-name> <rpm_dir> <stage>
# Returns: 0 on success, 1 if not found or extraction failed.
# Echoes status lines; caller tallies counts.
extract_one_pkg() {
    local pkg="$1" rpm_dir="$2" stage="$3"
    # Resolve the actual RPM by glob (handles auto-incremented hash/release).
    # The "[0-9]" after the package-name dash is required: without it,
    # "gnuradio-gr-" also matches "gnuradio-gr-utils-..." and "boost-" matches
    # "boost-log-" (the version always starts with a digit, a sub-package name
    # never does). Guard against the glob matching nothing.
    local matches=( "${rpm_dir}/${pkg}-"[0-9]*"${ARCH}.rpm" )
    if [ ! -e "${matches[0]}" ]; then
        echo "  WARNING: no RPM found for package '${pkg}'." >&2
        return 1
    fi
    if [ "${#matches[@]}" -gt 1 ]; then
        echo "  WARNING: multiple RPMs matched '${pkg}', using first:" >&2
        printf '           %s\n' "${matches[@]}" >&2
    fi
    local file
    # ${matches[0]} comes from ${rpm_dir}, which is RELATIVE to scripts/
    # (REPO_ROOT="../"). The extraction below does `cd "${stage}"` first, so a
    # relative path would no longer resolve. Make it absolute before the cd.
    file="$(readlink -f "${matches[0]}")"
    echo "  Extracting: $(basename "${file}")"
    # Do NOT suppress errors fully: a silent rpm2cpio/cpio failure produced an
    # empty staging tree that looked identical to success. Check the status.
    if ! ( cd "${stage}" && rpm2cpio "${file}" | cpio -idm ) > /dev/null; then
        echo "  WARNING: extraction failed for $(basename "${file}")." >&2
        return 1
    fi
    return 0
}

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
    local pkg
    # 1) GNU Radio component packages (carry the cpython-312 bindings).
    for pkg in "${PKGS[@]}"; do
        if extract_one_pkg "${pkg}" "${rpm_dir}" "${stage}"; then
            extracted=$((extracted+1))
        else
            missing=$((missing+1))
        fi
    done

    # 2) Non-gnuradio C++ runtime dependency libs (no cpython tag).
    #    A missing one of these is what causes import-time
    #    "libX.so: cannot open shared object file" on the target.
    local dep depmissing=0
    for dep in "${DEPLIBS[@]}"; do
        if extract_one_pkg "${dep}" "${rpm_dir}" "${stage}"; then
            extracted=$((extracted+1))
        else
            # A dependency lib that genuinely isn't in the pool is worth flagging,
            # but don't fail the build on optional ones (e.g. fmt may be folded
            # into spdlog). The ldd check below is the real arbiter.
            echo "  NOTE: dependency package '${dep}' not found (may be optional)." >&2
            depmissing=$((depmissing+1))
        fi
    done

    # 3) Pure-Python runtime deps (import-level, invisible to the ELF check).
    #    A missing one of these is a ModuleNotFoundError on the target, not a
    #    shared-library failure — the closure check below will NOT catch it.
    for dep in "${PYDEPS[@]}"; do
        if extract_one_pkg "${dep}" "${rpm_dir}" "${stage}"; then
            extracted=$((extracted+1))
        else
            echo "  WARNING: python dependency '${dep}' not found." >&2
            missing=$((missing+1))
        fi
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
        echo "  ERROR: build linked the wrong Python. Check the gnuradio" >&2
        echo "         bbappend (-DPYBIND11_FINDPYTHON=ON, -DENABLE_PYTHON=ON," >&2
        echo "         target pybind11_DIR), then cleansstate + rebuild." >&2
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

    # DEPENDENCY-CLOSURE CHECK (the real arbiter for the Option-B dep list).
    # Host is x86_64 and the .so are ARM, so `ldd`/execution won't work. Instead
    # read each ELF's DT_NEEDED entries and confirm every SONAME is present
    # somewhere in the staged tree. Anything reported here is a runtime
    # "cannot open shared object file" waiting to happen on the target -> add the
    # owning package to DEPLIBS and re-run.
    #
    # Libraries the base OS image always provides (libc, libstdc++, libm, etc.)
    # are NOT expected in the tarball; they're filtered out below.
    local readelf_bin
    readelf_bin="$(command -v readelf || true)"
    if [ -z "${readelf_bin}" ]; then
        echo "  NOTE: readelf not found; skipping dependency-closure check." >&2
    else
        # SONAMEs provided by the base rootfs (toolchain/libc/common system libs).
        # Not exhaustive, but covers what petalinux-image-minimal guarantees.
        # NOTE: patterns are substrings (not anchored to \.so) because names like
        # "ld-linux-armhf.so.3" carry arch text between the stem and ".so".
        local base_libs_re='^(ld-linux|libc\.|libc-|libm\.|libm-|libdl|libpthread|librt|libutil|libgcc_s|libstdc\+\+|libz\.|libcrypt|libresolv|libnsl|libatomic)'
        # Collect names present in the staged tree: regular files AND symlinks,
        # since a NEEDED SONAME is often satisfied by a versioned symlink
        # (e.g. libgnuradio-runtime.so.3.11.0git). find matches both by default.
        local have_libs
        have_libs="$(find "${stage}" \( -name '*.so' -o -name '*.so.*' \) -printf '%f\n' 2>/dev/null | sort -u)"
        local sofile needed missing_libs=""
        while IFS= read -r sofile; do
            needed="$(${readelf_bin} -d "${sofile}" 2>/dev/null \
                       | awk -F'[][]' '/NEEDED/{print $2}')"
            local n
            for n in ${needed}; do
                # skip base OS libs
                echo "${n}" | grep -Eq "${base_libs_re}" && continue
                # present in staged tree (exact SONAME as a file or symlink)?
                echo "${have_libs}" | grep -qx "${n}" && continue
                missing_libs="${missing_libs} ${n}"
            done
        done < <(find "${stage}" \( -name '*.so' -o -name '*.so.*' \) 2>/dev/null)

        if [ -n "${missing_libs}" ]; then
            local uniq_missing
            uniq_missing="$(echo "${missing_libs}" | tr ' ' '\n' | sort -u | grep -v '^$')"
            echo "  WARNING: ${board} payload has unresolved shared libraries:" >&2
            echo "${uniq_missing}" | sed 's/^/           /' >&2
            echo "           These will fail at import on the target unless provided" >&2
            echo "           by the base image. Add the owning RPM(s) to DEPLIBS and" >&2
            echo "           re-run. (Map lib -> pkg: ls <lib-prefix>*.rpm in the pool.)" >&2
            overall_rc=1
        else
            echo "  OK: dependency-closure check passed (no unresolved libs)."
        fi
    fi

    # PYTHON-IMPORT CLOSURE CHECK (complements the ELF check above).
    # The ELF check sees shared libs but NOT Python `import` statements, which is
    # how 'yaml'/'numpy' slipped through. Scan the staged gnuradio .py files for
    # top-level third-party imports and confirm each module is present somewhere
    # in the staged site-packages. Reports anything missing so it can be added to
    # PYDEPS. Heuristic (not a full import resolver), but catches the common gap.
    local sp
    sp="$(find "${stage}" -type d -name 'site-packages' 2>/dev/null | head -1)"
    if [ -n "${sp}" ]; then
        # Modules provided by the base python3 (stdlib) — never need shipping.
        # Broad list: the file-scan below reads ALL gnuradio .py (including code
        # paths the headless set never executes), so a tight list over-reports.
        local stdlib_re='^(os|sys|re|math|cmath|time|json|logging|threading|struct|array|collections|functools|itertools|typing|warnings|abc|enum|copy|weakref|datetime|importlib|pkgutil|inspect|traceback|subprocess|signal|ctypes|io|contextlib|argparse|optparse|codecs|csv|decimal|doctest|hashlib|pathlib|pprint|random|socket|string|unittest|glob|shutil|tempfile|textwrap|base64|binascii|operator|numbers|fractions|queue|select|errno|stat|fnmatch|getopt|platform|locale|gettext|html|xml|http|urllib|email|sqlite3|gc|atexit|types|keyword|token|tokenize|dis|ast|builtins|inspect|pdb|profile|timeit|uuid|secrets|hmac|zlib|gzip|bz2|lzma|tarfile|zipfile|configparser|asyncio)$'
        # Modules that are in-tree (gnuradio siblings) or only used by components
        # NOT shipped in the headless set (qtgui/grc/modtool/plotting). These are
        # false positives for a headless runtime and are filtered out.
        local nontarget_re='^(gnuradio|pmt|PyQt5|pyqtgraph|matplotlib|scipy|mako|pygccxml|click|blocktool|Generate_LDPC_matrix_functions)$'
        # Third-party modules gnuradio's python imports at top level.
        local imports
        imports="$(grep -rhoE '^[[:space:]]*(import|from)[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*' \
                     "${sp}/gnuradio" 2>/dev/null \
                   | awk '{print $2}' | sort -u)"
        local mod py_missing=""
        for mod in ${imports}; do
            echo "${mod}" | grep -Eq "${stdlib_re}" && continue
            echo "${mod}" | grep -Eq "${nontarget_re}" && continue
            # present as a top-level module/package in site-packages?
            if [ -e "${sp}/${mod}" ] || [ -e "${sp}/${mod}.py" ] \
               || ls "${sp}/${mod}".*.so >/dev/null 2>&1 \
               || ls "${sp}/${mod}"-*.dist-info >/dev/null 2>&1 \
               || ls "${sp}/${mod}"-*.egg-info >/dev/null 2>&1; then
                continue
            fi
            py_missing="${py_missing} ${mod}"
        done
        if [ -n "${py_missing}" ]; then
            local uniq_py
            uniq_py="$(echo "${py_missing}" | tr ' ' '\n' | sort -u | grep -v '^$')"
            # ADVISORY only (does not fail the build): the file-scan cannot tell a
            # top-level import in a module you load from one in a code path the
            # headless set never reaches. The on-target import test is the real
            # acceptance gate. Anything genuinely fatal shows up there.
            echo "  NOTE: ${board} gnuradio .py reference these non-stdlib modules" >&2
            echo "        not present in the payload (may be in unreached code or" >&2
            echo "        the base image — verify with an on-target import test):" >&2
            echo "${uniq_py}" | sed 's/^/           /' >&2
        else
            echo "  OK: python-import advisory: no unexpected modules referenced."
        fi
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