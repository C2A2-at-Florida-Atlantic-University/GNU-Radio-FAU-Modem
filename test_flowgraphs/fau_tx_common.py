#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Shared plumbing for the fau_modem TX example flowgraphs.

tx_sine.py and tx_chirp.py differ only in the source block they put in front
of fau_sink; everything else -- the preflight checks, the /proc/iomem and
device-tree probing, the PL register dump, the common CLI arguments and the
run/summary loop -- is identical. That is kept here rather than copied,
because a copy of this file has already gone stale once on a board and cost
several rounds of hardware debugging chasing a fault that was really an old
script reporting through old code paths.

Imported by name from the same directory, which works because Python puts a
script's own directory first on sys.path -- including when the scripts are
installed to share/gnuradio/fau_modem/examples/ and run by absolute path.
"""

import os
import sys

# Prefix used by step(); each script overrides it with its own name.
TAG = "fau_tx"

FABRIC_RATE = 10e6

# hw/board_map.h: dma_layout::WINDOW_PHYS / WINDOW_SIZE. Kept in sync by hand;
# this script only uses them to produce a better error message than a SIGBUS.
# If you change these, change board_map.h in the same commit -- a mismatch means
# the preflight checks a different window than the block actually maps, which is
# worse than no check at all.
WINDOW_PHYS = 0x1F000000
WINDOW_SIZE = 16 * 1024 * 1024


def step(msg):
    """Announce each stage that can die without a Python traceback.

    The three failure modes that produce no traceback at all:
      * SIGBUS ("Bus error") while constructing fau_sink -- an AXI write to a
        PL address the loaded bitstream does not decode. The constructor
        writes the TRI register of the CIC and DDS AXI GPIOs, so this fires
        before start() ever runs.
      * A hang inside tb.start() -- fau_sink::start() throws (e.g. the
        reserved-window check), and GNU Radio's thread-per-block scheduler
        deadlocks on its start barrier when a block's start() throws. The
        --allow-unreserved preflight below exists to keep you out of this.
      * SIGKILL from the OOM killer, which is its own conversation.
    """
    print("[%s] %s" % (TAG, msg), file=sys.stderr, flush=True)


def validate_rate(samp_rate):
    """Mirror fau_sink's construction-time CIC checks with a friendlier error."""
    if samp_rate <= 0 or FABRIC_RATE % samp_rate != 0:
        raise SystemExit(
            "sample rate %g Hz does not evenly divide the %g Hz CIC fabric rate"
            % (samp_rate, FABRIC_RATE))
    factor = int(FABRIC_RATE // samp_rate)
    if not 4 <= factor <= 65535:
        raise SystemExit(
            "CIC interpolation factor %d (= %g / %g) is outside [4, 65535]"
            % (factor, FABRIC_RATE, samp_rate))
    return factor


def window_overlaps_system_ram():
    """Reimplementation of hw::reserved_window_ok() so we can fail cleanly.

    fau_sink runs this same check inside start(), where a failure throws --
    and a throw out of a block's start() deadlocks the TPB scheduler's start
    barrier rather than surfacing as a Python exception. Checking here turns
    that hang into a message.

    Returns the offending "System RAM" range, or None if the window is clear.
    """
    try:
        with open("/proc/iomem") as f:
            lines = f.readlines()
    except OSError:
        return None  # Can't verify; the C++ side takes the same view.

    end = WINDOW_PHYS + WINDOW_SIZE
    saw_system_ram = False
    all_hidden = True
    for line in lines:
        if "System RAM" not in line:
            continue
        try:
            span = line.split(":")[0].strip()
            lo_s, hi_s = span.split("-")
            lo, hi = int(lo_s, 16), int(hi_s, 16)
        except ValueError:
            continue
        saw_system_ram = True
        if lo or hi:
            all_hidden = False
        if WINDOW_PHYS <= hi and lo < end:
            return (lo, hi)

    # Linux zeroes every /proc/iomem address for a reader without CAP_SYS_ADMIN,
    # so an unprivileged read shows "00000000-00000000 : System RAM" and would
    # look like a clean window. Report that as unverifiable, not as clear.
    if not saw_system_ram or all_hidden:
        return "hidden"
    return None


def _read_be_cells(path, ncells):
    """Read a device-tree property as a list of ncells-wide big-endian ints."""
    with open(path, "rb") as f:
        raw = f.read()
    width = ncells * 4
    out = []
    for off in range(0, len(raw) - width + 1, width):
        out.append(int.from_bytes(raw[off:off + width], "big"))
    return out


def describe_reserved_memory():
    """Report every /proc/device-tree/reserved-memory child and whether our DMA
    window falls inside one.

    Worth doing properly rather than looking for a node called "fau-dma": the
    node that actually carves out the top of DDR on these boards is named
    something else entirely, and a name-based check reported "ABSENT" while the
    region was in fact reserved. Names in a device tree are cosmetic; reg is not.
    """
    root = "/proc/device-tree/reserved-memory"
    if not os.path.isdir(root):
        return ["no /proc/device-tree/reserved-memory node at all"]

    def cells(name, default):
        try:
            return _read_be_cells(os.path.join(root, name), 1)[0]
        except (OSError, IndexError):
            return default

    addr_cells = cells("#address-cells", 1)
    size_cells = cells("#size-cells", 1)

    lines = []
    covered = False
    end = WINDOW_PHYS + WINDOW_SIZE
    for entry in sorted(os.listdir(root)):
        node = os.path.join(root, entry)
        reg = os.path.join(node, "reg")
        if not os.path.isfile(reg):
            continue
        try:
            vals = _read_be_cells(reg, 1)
        except OSError:
            continue
        # Flatten (address, size) pairs honouring #address-cells/#size-cells.
        pairs = []
        step_n = addr_cells + size_cells
        for i in range(0, len(vals) - step_n + 1, step_n):
            base = 0
            for c in range(addr_cells):
                base = (base << 32) | vals[i + c]
            length = 0
            for c in range(size_cells):
                length = (length << 32) | vals[i + addr_cells + c]
            pairs.append((base, length))

        nomap = os.path.exists(os.path.join(node, "no-map"))
        reusable = os.path.exists(os.path.join(node, "reusable"))
        for base, length in pairs:
            flags = []
            if nomap:
                flags.append("no-map")
            if reusable:
                flags.append("reusable")
            lines.append("reserved-memory: %s = 0x%08X..0x%08X (%d MiB)%s"
                         % (entry, base, base + length - 1, length // (1 << 20),
                            (" [" + ",".join(flags) + "]") if flags else ""))
            if base <= WINDOW_PHYS and end <= base + length:
                covered = True

    if not lines:
        lines.append("reserved-memory node exists but declares no reg ranges")

    if covered:
        lines.append("DMA window 0x%08X..0x%08X is inside a reserved-memory region: ok"
                     % (WINDOW_PHYS, end - 1))
    else:
        lines.append("DMA window 0x%08X..0x%08X is NOT inside any declared "
                     "reserved-memory region -- it may still be outside System RAM "
                     "(checked separately below), but nothing in the device tree "
                     "claims it" % (WINDOW_PHYS, end - 1))
    return lines


def preflight(allow_unreserved):
    """Check everything that can be checked without touching the PL."""
    ok = True

    step("python %d.%d.%d" % sys.version_info[:3])

    if os.geteuid() != 0:
        print("FAIL: not root -- fau_sink opens /dev/mem in its constructor "
              "and needs root or CAP_SYS_RAWIO", file=sys.stderr)
        ok = False
    else:
        step("running as root: ok")

    if not os.path.exists("/dev/mem"):
        print("FAIL: /dev/mem does not exist -- CONFIG_DEVMEM is off in this "
              "kernel", file=sys.stderr)
        ok = False
    else:
        step("/dev/mem present: ok")

    for line in describe_reserved_memory():
        step(line)

    overlap = window_overlaps_system_ram()
    if overlap == "hidden":
        print("FAIL: /proc/iomem addresses all read as zero -- this process cannot "
              "see the real\n      System RAM ranges, so the DMA window cannot be "
              "checked. Run as real root.", file=sys.stderr)
        ok = False
    elif overlap is None:
        step("DMA window 0x%08X..0x%08X clear of System RAM: ok"
             % (WINDOW_PHYS, WINDOW_PHYS + WINDOW_SIZE - 1))
    elif allow_unreserved:
        print("WARNING: DMA window 0x%08X..0x%08X overlaps System RAM "
              "[0x%08X, 0x%08X] -- proceeding because --allow-unreserved was "
              "given. The DMA will write into kernel-managed pages."
              % (WINDOW_PHYS, WINDOW_PHYS + WINDOW_SIZE - 1,
                 overlap[0], overlap[1]), file=sys.stderr)
    else:
        print("FAIL: DMA window 0x%08X..0x%08X overlaps System RAM "
              "[0x%08X, 0x%08X] in /proc/iomem.\n"
              "      fau_sink::start() refuses this, and a throw out of "
              "start() deadlocks GNU Radio's scheduler rather than raising.\n"
              "      Either add the `no-map` reserved-memory node to "
              "system-user.dtsi on this board, or re-run with "
              "--allow-unreserved (bring-up only)."
              % (WINDOW_PHYS, WINDOW_PHYS + WINDOW_SIZE - 1,
                 overlap[0], overlap[1]), file=sys.stderr)
        ok = False

    lock = "/run/lock"
    if not os.path.isdir(lock):
        print("FAIL: %s does not exist -- fau_sink's cross-process claim "
              "lock cannot be created" % lock, file=sys.stderr)
        ok = False
    else:
        step("%s present: ok" % lock)

    fpga_state = "/sys/class/fpga_manager/fpga0/state"
    if os.path.exists(fpga_state):
        try:
            with open(fpga_state) as f:
                step("fpga_manager state: %s" % f.read().strip())
        except OSError:
            pass
    else:
        step("no fpga_manager sysfs (expected -- the PL loads from BOOT.BIN); "
             "cannot confirm the bitstream from here")

    return ok

# ---------------------------------------------------------------------------
# PL register readback
# ---------------------------------------------------------------------------
# hw/board_map.h TX map. Read-only probes, so a wrong address here is a bad
# print, not a bad write.
_PL_REGS = [
    ("DDS PINC       0x42200000", 0x42200000, 0x00),
    ("DDS TVALID     0x42210000", 0x42210000, 0x00),
    ("dma_dds_select 0x41220000", 0x41220000, 0x00),
    ("CIC interp     0x41240000", 0x41240000, 0x00),
]
_DMA_REGS = [("MM2S_DMACR", 0x00), ("MM2S_DMASR", 0x04),
             ("MM2S_CURDESC", 0x08), ("MM2S_TAILDESC", 0x10)]
_AXI_DMA_BASE = 0x40400000


def dump_pl_state(tag):
    """Read back the PL config the block programmed, plus the MM2S registers.

    This is the state sine_gen.py prints as its [mm2s] block, so the two are
    directly comparable -- a known-good run shows DMACR=0x00010013 (its cyclic
    bit 4 is expected to differ from ours) and DMASR=0x00010008. What matters
    here is PINC/interp/mux being nonzero-and-correct, and DMASR showing the
    engine neither Halted nor Idle.
    """
    import mmap
    import struct
    step("---- PL state (%s) ----" % tag)
    try:
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    except OSError as exc:
        step("  cannot open /dev/mem: %s" % exc)
        return
    try:
        for label, base, off in _PL_REGS:
            try:
                m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED,
                              mmap.PROT_READ, offset=base)
                try:
                    m.seek(off)
                    step("  %s = 0x%08X"
                         % (label, struct.unpack("<I", m.read(4))[0]))
                finally:
                    m.close()
            except (OSError, ValueError) as exc:
                step("  %s = <unreadable: %s>" % (label, exc))
        try:
            m = mmap.mmap(fd, 0x10000, mmap.MAP_SHARED,
                          mmap.PROT_READ, offset=_AXI_DMA_BASE)
            try:
                for label, off in _DMA_REGS:
                    m.seek(off)
                    step("  %-14s = 0x%08X"
                         % (label, struct.unpack("<I", m.read(4))[0]))
            finally:
                m.close()
        except (OSError, ValueError) as exc:
            step("  AXI DMA regs unreadable: %s" % exc)
    finally:
        os.close(fd)
    step("------------------------")


# ---------------------------------------------------------------------------
# Shared CLI + run loop
# ---------------------------------------------------------------------------
def add_common_args(p):
    """Every argument that is not specific to one script's source block."""
    p.add_argument("--samp-rate", type=float, default=400e3,
                   help="baseband sample rate in Hz (default: %(default)g)")
    p.add_argument("--nco", type=float, default=120e3,
                   help="DDS/NCO mixer frequency in Hz (default: %(default)g)")
    p.add_argument("--amplitude", type=float, default=0.5,
                   help="baseband amplitude, 0..1 full scale (default: %(default)g)")
    p.add_argument("--tx-scale", type=float, default=1.0,
                   help="linear gain applied before Q15 packing (default: "
                        "%(default)g). Amplitude 1.0 already reaches full-scale "
                        "Q15, so >1 only clips")
    p.add_argument("--duration", type=float, default=0.0,
                   help="seconds to transmit; 0 means run until Ctrl-C "
                        "(default: %(default)g)")
    p.add_argument("--bd-samples", type=int, default=8192,
                   help="complex samples per DMA ring slot (default: %(default)d)")
    p.add_argument("--num-bds", type=int, default=16,
                   help="DMA ring depth in slots (default: %(default)d)")
    p.add_argument("--prefill-bds", type=int, default=4,
                   help="slots kept posted ahead of the DMA (default: %(default)d)")
    p.add_argument("--allow-unreserved", action="store_true",
                   help="skip the /proc/iomem reserved-window check (bring-up only)")
    p.add_argument("--verbose", action="store_true",
                   help="enable the ring-integrity watchdog and extra logging")
    p.add_argument("--preflight", action="store_true",
                   help="run the environment checks and exit without touching the PL")
    p.add_argument("--dump-pl", action="store_true",
                   help="after start, read back the PL config (DDS PINC, CIC "
                        "rate, source mux) and the MM2S registers. Directly "
                        "comparable to sine_gen.py's [mm2s] block")
    p.add_argument("--probe", action="store_true",
                   help="construct fau_sink and exit without start()ing the "
                        "flowgraph, to separate constructor faults from "
                        "start() faults")
    return p


def check_common_args(args):
    """Shared validation. Returns the CIC factor. Raises SystemExit on error."""
    factor = validate_rate(args.samp_rate)

    if not 0.0 < args.amplitude <= 1.0:
        raise SystemExit("--amplitude must be in (0, 1]; the Q15 packing "
                         "saturates above 1.0 full scale")
    if args.tx_scale <= 0.0:
        raise SystemExit("--tx-scale must be > 0")

    guard_ms = 1e3 * args.prefill_bds * args.bd_samples / args.samp_rate
    if guard_ms < 60.0:
        print("warning: prefill guard is only %.1f ms (<60 ms); expect "
              "underruns under scheduling stalls" % guard_ms, file=sys.stderr)
    return factor


def make_sink(fau_modem, args):
    """Construct fau_sink with the common arguments."""
    step("constructing fau_sink (opens /dev/mem, mmaps the DMA window, "
         "writes the CIC/DDS GPIO TRI registers) ...")
    sink = fau_modem.fau_sink(
        samp_rate=args.samp_rate,
        nco_freq=args.nco,
        bd_samples=args.bd_samples,
        num_bds=args.num_bds,
        prefill_bds=args.prefill_bds,
        drain_on_stop=True,      # park the DAC at 0
        allow_unreserved=args.allow_unreserved,
        verbose=args.verbose,
        tx_scale=args.tx_scale,
    )
    step("fau_sink constructed: ok")
    return sink


def run_flowgraph(tb, args):
    """start() -> wait -> Ctrl-C -> summary. Returns a process exit code."""
    import signal

    if args.probe:
        step("--probe: constructed cleanly, not starting. Exiting.")
        return 0

    def sig_handler(_sig=None, _frame=None):
        tb.stop()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    step("tb.start() -- ~1.2 s of PL fabric + CIC settle before samples flow")
    tb.start()
    step("transmitting")

    # Opt-in: comparing this against sine_gen.py's [mm2s] block is what tells a
    # PL-configuration fault apart from a flowgraph-side one, but it is noise on
    # a healthy run.
    if args.dump_pl:
        dump_pl_state("after start, streaming")

    try:
        tb.wait()
    except KeyboardInterrupt:
        tb.stop()
        tb.wait()

    err = tb.sink.last_error()
    total = tb.sink.bds_moved()
    silence = tb.sink.silence_bds()
    data = total - silence
    print("BDs moved: %d (data: %d, silence: %d), underruns: %d, clipped Q15: %d"
          % (total, data, silence, tb.sink.underruns(), tb.sink.clipped()))
    if data == 0:
        print("\n*** NO DATA BDs WERE POSTED.\n"
              "    Every BD the DMA transmitted carried the silence buffer, so a flat\n"
              "    DAC output is the CORRECT result here and the fault is upstream of\n"
              "    the PL, not in the bitstream. fau_sink accumulates partial work()\n"
              "    calls, so this should no longer be reachable -- if you are seeing\n"
              "    it, the deployed library is older than the flowgraph.\n",
              file=sys.stderr)
    elif silence > data:
        print("\n*** More silence (%d) than data (%d) BDs: the flowgraph is not keeping\n"
              "    the ring fed, so the DAC output is mostly gaps.\n" % (silence, data),
              file=sys.stderr)
    if err:
        print("fau_sink stopped itself: %s" % err, file=sys.stderr)
        return 1
    return 0
