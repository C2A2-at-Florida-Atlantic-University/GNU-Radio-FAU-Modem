#!/usr/bin/env python3
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Check a fau_source capture against a known injected tone.

Takes the WAV written by `fau_source -> complex_to_float -> wavfile_sink`
(2 channels, 16-bit: ch0 = I, ch1 = Q) and answers the questions a glance at
a spectrum plot does not:

  * does the tone land where the NCO says it should,
  * is the front end clipping,
  * and -- the one that matters for this block -- is the stream continuous
    ACROSS BD BOUNDARIES.

That last check is the point. Every `bd_samples` samples the DMA moves to the
next ring slot and software reclaims the previous one. A splice, a repeat or a
dropped slot there is invisible in an FFT of the whole capture (it averages
out) and invisible in the block's own counters (a BD that retires with the
right length and SOF/EOF is "good" by every test fau_source can make). It
shows up as a phase step at exactly one sample index mod bd_samples, which is
what this looks for.

Usage:
    ./check_rx_capture.py capture.wav --samp-rate 200e3 --nco 100e3 \\
        --tone-rf 105e3 --bd-samples 8192
"""

import argparse
import cmath
import math
import sys
import wave

import numpy as np


def read_iq(path):
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 2:
            sys.exit("expected 2 channels (I, Q), got %d" % w.getnchannels())
        if w.getsampwidth() != 2:
            sys.exit("expected 16-bit samples, got %d-bit"
                     % (8 * w.getsampwidth()))
        n = w.getnframes()
        raw = np.frombuffer(w.readframes(n), dtype="<i2").reshape(-1, 2)
    return raw


def main():
    p = argparse.ArgumentParser()
    p.add_argument("wav")
    p.add_argument("--samp-rate", type=float, required=True,
                   help="fau_source sample rate in Hz")
    p.add_argument("--nco", type=float, required=True,
                   help="NCO frequency the block was programmed with, Hz")
    p.add_argument("--tone-rf", type=float, default=None,
                   help="frequency of the injected tone, Hz. Expected "
                        "baseband offset is tone_rf - nco.")
    p.add_argument("--bd-samples", type=int, default=8192,
                   help="must match the block's bd_samples")
    p.add_argument("--clip-warn", type=float, default=1e-4,
                   help="fraction of saturated components worth reporting")
    args = p.parse_args()

    raw = read_iq(args.wav)
    n = len(raw)
    if n == 0:
        sys.exit("capture is empty -- the flowgraph produced no samples")

    iq = raw.astype(np.float64).view()
    x = (iq[:, 0] + 1j * iq[:, 1]) / 32767.0

    print("samples          %d (%.3f s at %.0f Hz)"
          % (n, n / args.samp_rate, args.samp_rate))
    print("complete BDs     %d of %d samples%s"
          % (n // args.bd_samples, args.bd_samples,
             "" if n % args.bd_samples == 0
             else "  (+%d leftover)" % (n % args.bd_samples)))

    # ---- level and clipping -------------------------------------------
    sat = np.count_nonzero(np.abs(raw) >= 32767)
    frac = sat / (2.0 * n)
    print("rms              %.4f full-scale" % np.sqrt(np.mean(np.abs(x) ** 2)))
    print("peak             %.4f full-scale" % np.max(np.abs(x)))
    print("saturated        %d components (%.3g)%s"
          % (sat, frac, "   <-- CLIPPING" if frac > args.clip_warn else ""))
    dc = np.mean(x)
    print("dc offset        %.5f  (|.| = %.5f)" % (dc.real, abs(dc)))

    # ---- where is the tone --------------------------------------------
    nfft = 1 << int(math.floor(math.log2(min(n, 1 << 20))))
    win = np.hanning(nfft)
    spec = np.fft.fftshift(np.abs(np.fft.fft(x[:nfft] * win)))
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / args.samp_rate))
    k = int(np.argmax(spec))
    peak_hz = freqs[k]
    print("peak at          %+.1f Hz baseband  (= %.1f Hz absolute)"
          % (peak_hz, args.nco + peak_hz))

    if args.tone_rf is not None:
        want = args.tone_rf - args.nco
        err = peak_hz - want
        bin_hz = args.samp_rate / nfft
        verdict = "OK" if abs(err) <= 3 * bin_hz else "MISMATCH"
        print("expected         %+.1f Hz  ->  error %+.1f Hz (%.1f bins)  %s"
              % (want, err, err / bin_hz, verdict))
        if abs(want) > args.samp_rate / 2:
            print("                 NOTE: outside +/-%.0f Hz, it will alias"
                  % (args.samp_rate / 2))

    # signal vs everything else, as a crude SNR
    band = slice(max(0, k - 4), min(nfft, k + 5))
    sig = np.sum(spec[band] ** 2)
    tot = np.sum(spec ** 2)
    noise = max(tot - sig, 1e-30)
    print("tone / rest      %.1f dB" % (10 * np.log10(sig / noise)))

    # ---- BD boundary continuity ---------------------------------------
    # Compare the sample-to-sample jump AT each BD boundary against the jumps
    # everywhere else. On a continuous stream the two distributions match; a
    # splice makes the boundary jumps stand out by orders of magnitude.
    nbd = n // args.bd_samples
    if nbd < 2:
        print("\nBD continuity    skipped, need at least 2 complete BDs")
        return 0

    d = np.abs(np.diff(x))
    idx = np.arange(1, nbd) * args.bd_samples - 1  # jump into each new BD
    at_edge = d[idx]
    mask = np.ones(len(d), dtype=bool)
    mask[idx] = False
    interior = d[mask]

    med = float(np.median(interior))
    p999 = float(np.percentile(interior, 99.9))
    imax = float(np.max(interior))
    worst = float(np.max(at_edge))

    # Threshold off the interior TAIL, not its median: with a strong tone the
    # median step is already large (the phasor advances every sample) and a
    # multiple of it sails past a real splice. Also require twice the largest
    # interior step, so a capture that legitimately contains a transient does
    # not flag every boundary.
    thresh = max(4.0 * p999, 2.0 * imax)
    bad = int(np.count_nonzero(at_edge > thresh))

    print("\nBD continuity    %d boundaries checked" % len(at_edge))
    print("  interior step  median %.6f, 99.9%% %.6f, max %.6f"
          % (med, p999, imax))
    print("  boundary step  median %.6f, worst %.6f  (threshold %.6f)"
          % (float(np.median(at_edge)), worst, thresh))

    failed = bad > 0
    if bad:
        print("  step check     %d/%d boundaries discontinuous  <-- BROKEN"
              % (bad, len(at_edge)))
    else:
        print("  step check     clean")

    # ---- phase advance consistency ------------------------------------
    # A whole dropped BD leaves every LOCAL step small -- the samples either
    # side are both valid, just not adjacent -- so the step check can miss it
    # entirely. It does shift the tone's phase by a fixed amount at that one
    # boundary, so track the per-BD phase of the tone and look for a jump.
    if abs(peak_hz) > 1.0:
        ref = np.exp(-2j * math.pi * peak_hz * np.arange(n) / args.samp_rate)
        y = x * ref
        ph = [cmath.phase(np.sum(y[i * args.bd_samples:(i + 1) * args.bd_samples]))
              for i in range(nbd)]
        dph = np.abs(np.diff(np.unwrap(ph)))
        pmax = float(np.max(dph))
        pbad = pmax > 0.5
        failed = failed or pbad
        print("  phase check    max %.3f rad between adjacent BDs%s"
              % (pmax, "  <-- BROKEN, a slot was dropped or reordered"
                 if pbad else "   clean"))
    else:
        print("  phase check    skipped, no tone to track "
              "(pass --tone-rf and inject one)")

    if failed:
        print("  RESULT         DISCONTINUOUS -- the ring is splicing, "
              "repeating or dropping slots.")
        print("                 fau_source's own counters cannot see this: a "
              "BD that retires with the")
        print("                 right length and SOF/EOF is 'good' by every "
              "test the block can make.")
        return 1

    print("  RESULT         continuous across every BD boundary")
    return 0


if __name__ == "__main__":
    sys.exit(main())
