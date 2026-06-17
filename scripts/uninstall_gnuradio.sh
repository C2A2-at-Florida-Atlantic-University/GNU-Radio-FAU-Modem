#!/bin/bash
#
# uninstall_gnuradio.bash   (runs ON the Zynq modem)
#
# Removes exactly the files that were placed at "/" by unpacking a
# gnuradio_headless_*.tar.gz. The tarball itself is the manifest: only paths it
# contains are removed, so this can never delete a file the package did not own.
#
# Directories are NOT removed (empty leftovers are acceptable and avoid any risk
# of deleting a shared dir like /usr/bin). Only regular files / symlinks listed
# in the tarball are unlinked.
#
# Usage:
#   ./uninstall_gnuradio.bash <tarball.tar.gz> [install-root]
#     install-root defaults to "/".
#
# Dry run (show what would be removed, delete nothing):
#   DRY_RUN=1 ./uninstall_gnuradio.bash gnuradio_headless_7010.tar.gz
#
set -u

TARBALL="${1:-}"
ROOT="${2:-/}"
DRY_RUN="${DRY_RUN:-0}"

if [ -z "${TARBALL}" ]; then
    echo "Usage: $0 <tarball.tar.gz> [install-root]" >&2
    exit 2
fi
if [ ! -f "${TARBALL}" ]; then
    echo "ERROR: tarball not found: ${TARBALL}" >&2
    exit 2
fi

# Normalize root to end without a trailing slash (except literal "/").
if [ "${ROOT}" != "/" ]; then
    ROOT="${ROOT%/}"
fi

echo "Tarball : ${TARBALL}"
echo "Root    : ${ROOT}"
if [ "${DRY_RUN}" != "0" ]; then
    echo "Mode    : DRY RUN (nothing will be deleted)"
fi
echo "-------------------------------------------------------------------"

removed=0
skipped_dir=0
absent=0
errors=0

# Read the tarball's member list. tar prints directory members with a trailing
# slash, which is how we distinguish them from files.
while IFS= read -r entry; do
    # Skip empty lines.
    [ -z "${entry}" ] && continue

    # Directory members end in "/" — never delete directories.
    case "${entry}" in
        */) skipped_dir=$((skipped_dir+1)); continue ;;
    esac

    # Build the absolute on-disk path. Tar entries are relative ("usr/bin/foo").
    if [ "${ROOT}" = "/" ]; then
        target="/${entry}"
    else
        target="${ROOT}/${entry}"
    fi

    # Collapse any accidental double slash.
    target="${target//\/\///}"

    # Only act on things that exist as a file or symlink.
    if [ -L "${target}" ] || [ -f "${target}" ]; then
        if [ "${DRY_RUN}" != "0" ]; then
            echo "would remove: ${target}"
            removed=$((removed+1))
        else
            if rm -f "${target}"; then
                echo "removed: ${target}"
                removed=$((removed+1))
            else
                echo "ERROR removing: ${target}" >&2
                errors=$((errors+1))
            fi
        fi
    elif [ -e "${target}" ]; then
        # Exists but is neither a regular file nor a symlink (e.g. a dir that
        # tar listed without trailing slash, or a special file). Leave it.
        echo "skip (not a file/symlink): ${target}"
        skipped_dir=$((skipped_dir+1))
    else
        absent=$((absent+1))
    fi
done < <(tar tzf "${TARBALL}")

echo "-------------------------------------------------------------------"
echo "Files removed     : ${removed}"
echo "Directories left  : ${skipped_dir}   (intentionally not deleted)"
echo "Already absent    : ${absent}"
if [ "${errors}" -gt 0 ]; then
    echo "Errors            : ${errors}" >&2
    exit 1
fi
echo "Done."
exit 0