#!/usr/bin/env python3
"""
fix_volk_config.py

Run VOLK's profiler, then correct kernel entries that the profiler selects for
SPEED but which are functionally BROKEN on this cortex-a9 target.

Background:
  volk_profile benchmarks each kernel and picks the fastest implementation whose
  averaged error (over a large random vector) falls under a loose tolerance. On
  this target it picks the hand-written NEON assembly `a_neonasm` for
  `volk_32f_x2_dot_prod_32f` because it is fastest and passes the statistical
  tolerance (1.4e-3 < 1e-2) -- but it produces STRUCTURALLY WRONG output (zeros)
  for small/exact inputs, which broke the gr-filter resampler/FIR qa tests.
  Forcing that kernel to the `neon` (intrinsics) implementation fixes it while
  keeping SIMD acceleration everywhere else.

This script:
  1. Runs `volk_profile` (writes ~/.volk/volk_config for the current user).
  2. Reads the config, and for each kernel in OVERRIDES rewrites its line to the
     known-good implementation if it differs.
  3. Writes the config back only if something changed (idempotent).

IMPORTANT: VOLK config is PER-USER (~/.volk/volk_config). Run this AS THE USER
that will run the modem (petalinux) -- NOT via sudo, or it edits root's config.

Usage:
  ./fix_volk_config.py                 # profile, then apply overrides
  ./fix_volk_config.py --no-profile    # skip profiling, just fix existing config
  ./fix_volk_config.py --dry-run       # show what would change, write nothing
  ./fix_volk_config.py --config PATH   # operate on a specific config file
"""

import argparse
import os
import shutil
import subprocess
import sys

# kernel name -> required "aligned unaligned" implementations.
# Default: the one confirmed-broken kernel. Add the complex sibling here if
# on-target testing shows volk_32fc_32f_dot_prod_32fc also needs correcting:
#   "volk_32fc_32f_dot_prod_32fc": ("neon", "generic"),
OVERRIDES = {
    "volk_32f_x2_dot_prod_32f": ("neon", "generic"),
}


def default_config_path() -> str:
    # VOLK_CONFIGPATH overrides $HOME if set; otherwise ~/.volk/volk_config.
    base = os.environ.get("VOLK_CONFIGPATH") or os.path.expanduser("~")
    return os.path.join(base, ".volk", "volk_config")


def run_volk_profile() -> int:
    exe = shutil.which("volk_profile") or "/usr/bin/volk_profile"
    if not os.path.exists(exe):
        print(f"ERROR: volk_profile not found (looked for {exe}).", file=sys.stderr)
        print("       It ships in the 'volk' package; confirm it is installed.",
              file=sys.stderr)
        return 127
    print(f"Running {exe} (slow on cortex-a9; several minutes)...")
    # Inherit stdout/stderr so the profiler's progress is visible.
    proc = subprocess.run([exe])
    print(f"volk_profile exit: {proc.returncode}")
    return proc.returncode


def apply_overrides(config_path: str, dry_run: bool) -> int:
    if not os.path.isfile(config_path):
        print(f"ERROR: config not found at {config_path}.", file=sys.stderr)
        print("       Did volk_profile run as this user? (Per-user path.)",
              file=sys.stderr)
        return 1

    with open(config_path, "r") as f:
        lines = f.readlines()

    changed = False
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        # config lines look like:  <kernel_name> <aligned_impl> <unaligned_impl>
        parts = stripped.split()
        if parts and parts[0] in OVERRIDES:
            kernel = parts[0]
            want_aligned, want_unaligned = OVERRIDES[kernel]
            cur_aligned = parts[1] if len(parts) > 1 else ""
            cur_unaligned = parts[2] if len(parts) > 2 else ""
            seen.add(kernel)
            if (cur_aligned, cur_unaligned) != (want_aligned, want_unaligned):
                print(f"  {kernel}: '{cur_aligned} {cur_unaligned}' "
                      f"-> '{want_aligned} {want_unaligned}'")
                out.append(f"{kernel} {want_aligned} {want_unaligned}\n")
                changed = True
            else:
                print(f"  {kernel}: already '{want_aligned} {want_unaligned}' (ok)")
                out.append(line)
        else:
            out.append(line)

    # Warn about overrides whose kernel never appeared (name typo / VOLK version).
    for kernel in OVERRIDES:
        if kernel not in seen:
            print(f"  WARNING: kernel '{kernel}' not found in config; "
                  f"override not applied.", file=sys.stderr)

    if not changed:
        print("No changes needed; config already correct.")
        return 0

    if dry_run:
        print("--dry-run: not writing changes.")
        return 0

    # Back up once, then write.
    backup = config_path + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(config_path, backup)
        print(f"Backup written: {backup}")
    with open(config_path, "w") as f:
        f.writelines(out)
    print(f"Updated: {config_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run volk_profile and correct broken kernel selections.")
    ap.add_argument("--no-profile", action="store_true",
                    help="skip volk_profile; only edit the existing config")
    ap.add_argument("--dry-run", action="store_true",
                    help="show changes without writing")
    ap.add_argument("--config", default=None,
                    help="config file path (default: $VOLK_CONFIGPATH or ~/.volk/volk_config)")
    args = ap.parse_args()

    if os.geteuid() == 0:
        print("WARNING: running as root — this edits ROOT's volk_config, not the",
              file=sys.stderr)
        print("         modem user's. Run as the runtime user (petalinux), no sudo.",
              file=sys.stderr)

    config_path = args.config or default_config_path()
    print(f"VOLK config: {config_path}")

    if not args.no_profile:
        rc = run_volk_profile()
        # Non-zero profile exit is worth surfacing but we still try to fix the
        # config it may have partially written.
        if rc not in (0,):
            print(f"NOTE: volk_profile returned {rc}; attempting config fix anyway.",
                  file=sys.stderr)

    return apply_overrides(config_path, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())