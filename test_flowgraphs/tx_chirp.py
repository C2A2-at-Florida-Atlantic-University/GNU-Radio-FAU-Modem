#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Transmit a repeating linear frequency chirp out of the FAU modem TX board.

Flowgraph:

    analog.sig_source_f (sawtooth, Hz)
        -> analog.frequency_modulator_fc
        -> blocks.multiply_const_cc
        -> fau_modem.fau_sink

The sweep is SYNTHESISED CONTINUOUSLY, sample by sample, for as long as the
flowgraph runs -- nothing is precomputed and no sweep is held in RAM. The
sawtooth carries the instantaneous frequency in Hz (its amplitude is the span
and its offset is --f-start), and the FM block integrates that into phase, so
memory is O(1) and --period can be arbitrarily long.

That structure also makes the repeat phase-continuous for free: the FM block's
phase accumulator never resets, so the only thing that steps at the seam is
frequency, which is inherent to a sawtooth sweep. The earlier
precompute-and-replay version had a second, avoidable phase click there.

The sweep is described by --bandwidth, --direction and --period. It is always
CENTRED ON THE NCO, so --bandwidth 120e3 with the default 120 kHz NCO sweeps
between 60 kHz and 180 kHz at the DAC, and --direction picks which end it
starts from. The run banner prints the resulting RF span so there is no need
to do that arithmetic in your head.

Because the baseband is complex, the sweep is single-sideband: there is no
mirror image on the other side of the NCO.

Needs root: fau_sink drives the AXI DMA over /dev/mem.

Do NOT put a throttle block in front of fau_sink -- the DMA ring is the rate
limiter, and a throttle would only fight it and cause underruns.

Shared preflight/PL-probing lives in fau_tx_common.py next to this file.
"""

import argparse
import math
import sys

from fau_tx_common import (add_common_args, check_common_args, make_sink,
                           preflight, run_flowgraph, step)
import fau_tx_common

fau_tx_common.TAG = "tx_chirp"


def sweep_edges(args):
    """(f_start, f_stop) baseband offsets for the requested sweep.

    The band is centred on the NCO, so it runs +/- bandwidth/2 either side of
    baseband DC, and --direction only decides which edge is the start.
    """
    half = args.bandwidth / 2.0
    return (-half, half) if args.direction == "up" else (half, -half)


def build_flowgraph(gr, analog, blocks, fau_modem, args, nsamples):
    class tx_chirp(gr.top_block):
        def __init__(self):
            gr.top_block.__init__(self, "FAU Modem TX Chirp",
                                  catch_exceptions=True)

            # Instantaneous frequency, in Hz, as a sawtooth ramp. GR's saw wave
            # is offset + ampl*(phase/2pi) + ampl/2, so amplitude carries the
            # span and offset carries the low end -- and a negative span (a
            # down-chirp) needs no special casing, the ramp just runs the other
            # way.
            f_start, f_stop = sweep_edges(args)
            self.saw = analog.sig_source_f(
                args.samp_rate, analog.GR_SAW_WAVE, 1.0 / args.period,
                f_stop - f_start, f_start)

            # The NCO inside sig_source starts at phase 0, which is the MIDDLE
            # of the ramp; -pi puts the first sample at the starting edge so
            # the very first sweep is a whole one. Only cosmetic, and only for
            # the first sweep, hence the soft failure.
            try:
                self.saw.set_phase(-math.pi)
            except AttributeError:
                print("%s: sig_source has no set_phase(); the first sweep "
                      "starts mid-band" % fau_tx_common.TAG)

            # Integrate Hz into phase. sensitivity = 2*pi/samp_rate makes the
            # input scale exactly Hz, so the sawtooth above needs no scaling.
            self.fm = analog.frequency_modulator_fc(
                2.0 * math.pi / args.samp_rate)

            # frequency_modulator_fc always emits unit magnitude.
            self.amp = blocks.multiply_const_cc(args.amplitude)

            self.sink = make_sink(fau_modem, args)

            if nsamples > 0:
                self.head = blocks.head(gr.sizeof_gr_complex, nsamples)
                self.connect(self.saw, self.fm, self.amp, self.head, self.sink)
            else:
                self.connect(self.saw, self.fm, self.amp, self.sink)

    return tx_chirp()


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    p.add_argument("--bandwidth", type=float, default=200e3, metavar="HZ",
                   help="width of the swept band, centred on --nco "
                        "(default: %(default)g). Must not exceed the sample "
                        "rate, since the sweep reaches +/- bandwidth/2 at "
                        "baseband")
    p.add_argument("--direction", choices=("up", "down"), default="up",
                   help="sweep from the bottom of the band to the top (up, "
                        "default) or from the top to the bottom (down)")
    p.add_argument("--period", type=float, default=0.01, metavar="SEC",
                   help="seconds for one sweep, after which it repeats "
                        "(default: %(default)g)")
    args = p.parse_args()

    factor = check_common_args(args)

    if args.bandwidth <= 0.0:
        raise SystemExit("--bandwidth must be > 0 Hz; use tx_sine.py for a "
                         "fixed tone")
    # The sweep reaches +/- bandwidth/2 at baseband, so the whole band fits
    # exactly when bandwidth == samp_rate.
    if args.bandwidth > args.samp_rate:
        raise SystemExit(
            "--bandwidth %g Hz exceeds the sample rate %g sps; the sweep would "
            "pass Nyquist (+/-%g Hz) and alias"
            % (args.bandwidth, args.samp_rate, args.samp_rate / 2))
    if args.period <= 0.0:
        raise SystemExit("--period must be > 0 s")

    # No upper bound on --period any more -- the sweep is generated on the fly,
    # so a long one costs nothing. Only the floor still matters: a sweep needs
    # a handful of samples to be a sweep rather than a step.
    n_per_sweep = args.period * args.samp_rate
    if n_per_sweep < 2:
        raise SystemExit(
            "--period %g s is only %.1f sample(s) at %g sps; a sweep needs at "
            "least 2" % (args.period, n_per_sweep, args.samp_rate))

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

    f_start, f_stop = sweep_edges(args)
    print("TX chirp: %g sps (CIC interp %d), NCO %g Hz, amplitude %g"
          % (args.samp_rate, factor, args.nco, args.amplitude))
    print("          %g Hz band, sweeping %s over %g s (%.3g Hz/s)"
          % (args.bandwidth, args.direction, args.period,
             (f_stop - f_start) / args.period))
    print("          baseband %g -> %g Hz; DAC output sweeps %g -> %g Hz, "
          "repeating at %.4g Hz"
          % (f_start, f_stop, args.nco + f_start, args.nco + f_stop,
             1.0 / args.period))
    # A band wider than 2*nco walks the sweep through DC and out the far side
    # as a negative RF frequency, which the DAC reproduces folded back up as
    # its mirror -- so the low edge stops moving down and starts moving back
    # up. Worth saying out loud; it is a legal request, just rarely the
    # intended one.
    if args.nco + f_start < 0.0 or args.nco + f_stop < 0.0:
        print("          WARNING: %g Hz around a %g Hz NCO reaches below DC; "
              "the negative part folds" % (args.bandwidth, args.nco))
        print("                   back up as its mirror image. Raise --nco or "
              "narrow --bandwidth to avoid it")

    print("          generated continuously (%.0f samples/sweep, none stored); "
          "repeat is phase-continuous" % n_per_sweep)

    tb = build_flowgraph(gr, analog, blocks, fau_modem, args, nsamples)
    return run_flowgraph(tb, args)


if __name__ == "__main__":
    sys.exit(main())
