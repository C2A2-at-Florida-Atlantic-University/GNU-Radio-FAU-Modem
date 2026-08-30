#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Transmit a sine wave out of the FAU modem TX/DAC board (Zynq-7010).

Flowgraph:

    analog.sig_source_c  [-> multiply by phase pattern]  ->  fau_modem.fau_sink

The hardware DDS in the PL mixes the baseband IQ stream up to the NCO
frequency, so with the default --tone of 0 Hz the sig_source emits a
constant (amplitude + 0j), and what actually comes out of the DAC is a pure
sine at the NCO frequency (120 kHz). Give --tone a nonzero value to offset
the output tone to nco_freq + tone instead.

NOTE: sig_source_c with GR_COS_WAVE is ANALYTIC (it calls nco.sincos()), so
a nonzero --tone produces a single sideband at nco+tone, not a pair of
tones straddling the NCO.

--phase-shift periodically rotates the carrier phase; see build_phase_pattern.

Needs root: fau_sink drives the AXI DMA over /dev/mem.

Do NOT put a throttle block in front of fau_sink -- the DMA ring is the
rate limiter, and a throttle would only fight it and cause underruns.

Shared preflight/PL-probing lives in fau_tx_common.py next to this file.
"""

import argparse
import math
import sys

from fau_tx_common import (add_common_args, check_common_args, make_sink,
                           preflight, run_flowgraph, step)
import fau_tx_common

fau_tx_common.TAG = "tx_sine"

# Allowed --phase-shift values. 0 disables the whole path. Both nonzero values
# divide 360 evenly, which is what lets the pattern below close cleanly and
# repeat forever without a discontinuity at the wrap.
PHASE_SHIFT_CHOICES = (0, 90, 180)



def build_phase_pattern(samp_rate, period, shift_deg):
    """One full cycle of the periodic phase rotation, as unit-magnitude IQ.

    The carrier phase advances by shift_deg every `period` seconds and wraps
    after 360 degrees, so --phase-shift 180 alternates 0,180,0,180... (a BPSK
    style flip) and --phase-shift 90 walks 0,90,180,270 before repeating.
    Multiplying the baseband stream by exp(j*theta) rotates the RF carrier by
    the same theta, because the PL's DDS mixer is a complex multiply -- the
    shift lands on the transmitted carrier, not just on the baseband.

    Returns (phasors, samples_per_step). Only the 2 or 4 DISTINCT phasors come
    back: blocks.repeat holds each one for samples_per_step samples inside the
    flowgraph, so nothing here scales with --phase-period and no expanded
    pattern is ever stored.
    """
    samples_per_step = int(round(period * samp_rate))
    if samples_per_step < 1:
        raise ValueError(
            "--phase-period %g s is shorter than one sample at %g sps"
            % (period, samp_rate))

    phasors = []
    for k in range(360 // shift_deg):
        theta = math.radians(k * shift_deg)
        # Snap to exact values at the quadrant boundaries: math.cos(pi/2) is
        # 6.1e-17, not 0, and a stray 6e-17 in the "constant" leg is noise the
        # scope does not need to show.
        phasors.append(complex(round(math.cos(theta), 12),
                               round(math.sin(theta), 12)))
    return phasors, samples_per_step

def build_flowgraph(gr, analog, blocks, fau_modem, args, nsamples):
    class tx_sine(gr.top_block):
        def __init__(self):
            gr.top_block.__init__(self, "FAU Modem TX Sine",
                                  catch_exceptions=True)

            # GR_COS_WAVE on a complex sig_source is exp(j*2*pi*tone*t) *
            # amplitude, i.e. a constant (amplitude + 0j) when tone == 0.
            self.src = analog.sig_source_c(
                args.samp_rate, analog.GR_COS_WAVE, args.tone, args.amplitude)

            # Optional periodic phase rotation, multiplied into the baseband
            # stream ahead of the sink. Kept out of the chain entirely when
            # --phase-shift is 0 so the default path is unchanged.
            self.phase = None
            if args.phase_shift:
                phasors, hold = build_phase_pattern(
                    args.samp_rate, args.phase_period, args.phase_shift)
                # Only the 2-4 phasors are held; blocks.repeat stretches each
                # one across the hold on the fly, so a long --phase-period no
                # longer costs any memory.
                self.phase = blocks.vector_source_c(phasors, repeat=True)
                self.hold = blocks.repeat(gr.sizeof_gr_complex, hold)
                self.rotate = blocks.multiply_cc()

            self.sink = make_sink(fau_modem, args)

            # src [-> rotate] [-> head] -> sink
            chain_head = self.src
            if self.phase is not None:
                self.connect(self.src, (self.rotate, 0))
                self.connect(self.phase, self.hold, (self.rotate, 1))
                chain_head = self.rotate

            if nsamples > 0:
                self.head = blocks.head(gr.sizeof_gr_complex, nsamples)
                self.connect(chain_head, self.head, self.sink)
            else:
                self.connect(chain_head, self.sink)

    return tx_sine()


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    p.add_argument("--tone", type=float, default=0.0,
                   help="baseband tone in Hz; 0 gives a pure sine at the NCO "
                        "frequency (default: %(default)g). Analytic, so the "
                        "output is a single tone at nco+tone")
    p.add_argument("--phase-shift", type=int, default=0,
                   choices=PHASE_SHIFT_CHOICES, metavar="DEG",
                   help="periodically advance the carrier phase by this many "
                        "degrees: 0 = off (default), 90 walks 0/90/180/270, "
                        "180 alternates 0/180 (a BPSK-style flip)")
    p.add_argument("--phase-period", type=float, default=0.01, metavar="SEC",
                   help="how long the carrier holds each phase before the next "
                        "--phase-shift step, in seconds (default: %(default)g). "
                        "Ignored when --phase-shift is 0")
    args = p.parse_args()

    factor = check_common_args(args)

    if abs(args.tone) > args.samp_rate / 2:
        raise SystemExit("--tone %g Hz exceeds Nyquist for %g sps"
                         % (args.tone, args.samp_rate))

    if args.phase_shift:
        if args.phase_period <= 0.0:
            raise SystemExit("--phase-period must be > 0 s")
        # Fail here rather than inside the flowgraph constructor, which runs
        # after /dev/mem is open and the PL GPIOs have been touched.
        try:
            build_phase_pattern(args.samp_rate, args.phase_period,
                                args.phase_shift)
        except ValueError as exc:
            raise SystemExit(str(exc))

    if not preflight(args.allow_unreserved):
        return 2
    if args.preflight:
        return 0

    step("importing gnuradio ...")
    from gnuradio import analog, blocks, gr
    from gnuradio import fau_modem
    step("gnuradio %s, fau_modem from %s"
         % (gr.version(), fau_modem.__file__))

    nsamples = int(args.duration * args.samp_rate) if args.duration > 0 else 0

    print("TX sine: %g sps (CIC interp %d), NCO %g Hz, baseband tone %g Hz, "
          "amplitude %g -> output at %g Hz"
          % (args.samp_rate, factor, args.nco, args.tone, args.amplitude,
             args.nco + args.tone))
    if args.phase_shift:
        steps = 360 // args.phase_shift
        seq = "/".join(str(k * args.phase_shift) for k in range(steps))
        print("         phase: %d deg every %g s (%s deg, repeating; "
              "%d samples per step, %.1f Hz step rate)"
              % (args.phase_shift, args.phase_period, seq,
                 int(round(args.phase_period * args.samp_rate)),
                 1.0 / args.phase_period))
    else:
        print("         phase: fixed (--phase-shift 0)")

    tb = build_flowgraph(gr, analog, blocks, fau_modem, args, nsamples)
    return run_flowgraph(tb, args)


if __name__ == "__main__":
    sys.exit(main())
