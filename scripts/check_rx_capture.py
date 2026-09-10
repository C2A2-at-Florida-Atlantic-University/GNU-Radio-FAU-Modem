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
    p.add_argument("--chirp-center", type=float, default=None,
                   help="centre frequency of an injected chirp, Hz (RF, same "
                        "reference as --nco). Use with --chirp-bw.")
    p.add_argument("--chirp-bw", type=float, default=None,
                   help="chirp bandwidth in Hz. With --chirp-center this "
                        "checks how much energy actually lands in the band "
                        "the sweep is supposed to occupy.")
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
    rms = float(np.sqrt(np.mean(np.abs(x) ** 2)))
    peak = float(np.max(np.abs(x)))
    print("rms              %.4f full-scale (%.1f dBFS)"
          % (rms, 20 * math.log10(max(rms, 1e-12))))
    print("peak             %.4f full-scale (%.1f dBFS)"
          % (peak, 20 * math.log10(max(peak, 1e-12))))
    print("saturated        %d components (%.3g)%s"
          % (sat, frac, "   <-- CLIPPING" if frac > args.clip_warn else ""))
    dc = np.mean(x)
    print("dc offset        %.5f  (|.| = %.5f)" % (dc.real, abs(dc)))

    # An under-driven front end is the likeliest reason a capture "looks like
    # noise": the floor is not raised, the signal is simply down in it. The
    # AD9244 is 14-bit, so peak 0.01 full-scale is ~7 bits of the converter
    # actually in use and ~40 dB of dynamic range thrown away. Say so loudly,
    # because every downstream verdict in this script degrades with it.
    if peak < 0.05:
        print("                 ^^ UNDER-DRIVEN: only ~%.0f of 14 bits in use."
              % max(1.0, math.log2(max(peak, 1e-9) * (1 << 13))))
        print("                    Raise the AD8334 VGA gain "
              "(gain-control/dac7512.py --vga-gain-volts) until peak is "
              "0.3-0.5,")
        print("                    then re-run. Judge nothing else about this "
              "capture until you have.")

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
    # ---- where does the energy ACTUALLY sit -----------------------------
    # For anything wideband -- a chirp above all -- the argmax bin is
    # meaningless and wanders run to run. What answers "is my signal there?"
    # is the occupied band: the narrowest contiguous span holding most of the
    # power. Report it unconditionally, because reading it off a waterfall
    # screenshot is exactly how a frequency-plan error survives for hours.
    psd = spec ** 2
    order = np.argsort(psd)[::-1]
    csum = np.cumsum(psd[order])
    keep = order[:1 + int(np.searchsorted(csum, 0.9 * csum[-1]))]
    occ_lo, occ_hi = float(np.min(freqs[keep])), float(np.max(freqs[keep]))
    print("occupied band    %+.1f .. %+.1f kHz baseband (90%% of power, "
          "%.1f kHz wide)"
          % (occ_lo / 1e3, occ_hi / 1e3, (occ_hi - occ_lo) / 1e3))
    print("                 = %.1f .. %.1f kHz absolute"
          % ((args.nco + occ_lo) / 1e3, (args.nco + occ_hi) / 1e3))

    if args.chirp_center is not None and args.chirp_bw is not None:
        want_lo = args.chirp_center - args.chirp_bw / 2.0 - args.nco
        want_hi = args.chirp_center + args.chirp_bw / 2.0 - args.nco
        inb = (freqs >= want_lo) & (freqs <= want_hi)
        pin = float(np.sum(psd[inb]))
        pout = float(np.sum(psd[~inb])) or 1e-30
        print("expected band    %+.1f .. %+.1f kHz baseband"
              % (want_lo / 1e3, want_hi / 1e3))
        print("in-band / out    %.1f dB (%.1f%% of total power in band)"
              % (10 * math.log10(pin / pout),
                 100.0 * pin / (pin + pout)))
        if min(abs(want_lo), abs(want_hi)) < args.samp_rate / 2 \
                and max(abs(want_lo), abs(want_hi)) > args.samp_rate / 2:
            print("                 WARNING: part of the sweep is outside "
                  "+/-%.0f kHz and will alias"
                  % (args.samp_rate / 2e3))
        # Crossing DC is FINE here and putting the NCO at the sweep centre is
        # the right choice: this is complex I/Q baseband, where -f and +f are
        # distinct and both representable within +/-samp_rate/2. (An earlier
        # version warned about the lower half "folding onto" the upper half.
        # That is true of REAL sampling only, and it was wrong.) What does
        # deserve a mention is that DC offset and the I/Q-imbalance mirror now
        # sit inside the occupied band instead of off to one side -- and only
        # when they are actually large enough to matter.
        if want_lo < 0 < want_hi and abs(dc) > 0.02 * rms:
            print("                 NOTE: the sweep spans DC, so the %.1f dB "
                  "dc offset sits inside the signal band"
                  % (20 * math.log10(max(abs(dc), 1e-12) / max(rms, 1e-12))))

    tone_db = float(10 * np.log10(sig / noise))
    print("tone / rest      %.1f dB%s"
          % (tone_db, "   (no dominant CW component -- chirp, modulation or "
                      "noise)" if tone_db < 10.0 else ""))

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
    #
    # ONLY VALID FOR A STATIONARY TONE. It de-rotates by ONE frequency, so any
    # signal whose frequency moves -- a chirp above all -- produces large
    # per-BD phase steps that mean nothing at all. Ran this against a chirp
    # capture once and it reported 3.1 rad and "BROKEN" on a stream the step
    # check had just certified clean. Gate it on there actually being a
    # dominant CW component rather than merely an argmax bin, which every
    # spectrum has.
    tone_dominant = tone_db >= 10.0
    if abs(peak_hz) > 1.0 and tone_dominant:
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
    elif not tone_dominant:
        print("  phase check    skipped, no dominant CW tone (%.1f dB) -- "
              "meaningless for a chirp or modulated signal" % tone_db)
    else:
        print("  phase check    skipped, tone sits at DC")

    # ---- discontinuities ANYWHERE, not just at BD boundaries ----------
    # The checks above only ever look at indices that are multiples of
    # bd_samples, so they are blind to a glitch introduced anywhere else --
    # downstream buffering, the file writer, or the analog side. Scan the
    # whole record and report where the worst steps actually are, plus their
    # offset within a BD: clustering at one offset means the ring, scattering
    # means it is not the ring's fault.
    outliers = np.flatnonzero(d > thresh)
    print("\nGlitch scan      %d samples over the %.6f step threshold"
          % (len(outliers), thresh))
    if len(outliers):
        failed = True
        show = outliers[:8]
        for i in show:
            print("    sample %-9d t=%.4f s   step %.6f   offset %d/%d in BD"
                  % (i, i / args.samp_rate, d[i],
                     int(i % args.bd_samples), args.bd_samples))
        if len(outliers) > len(show):
            print("    ... and %d more" % (len(outliers) - len(show)))
        aligned = int(np.count_nonzero(
            (outliers % args.bd_samples) >= args.bd_samples - 1))
        print("    %d of %d land on a BD boundary -> %s"
              % (aligned, len(outliers),
                 "ring/reclaim problem" if aligned > len(outliers) // 2
                 else "NOT ring-aligned, look outside fau_source "
                      "(downstream buffering, file writer, or analog)"))

    # ---- additive bursts ----------------------------------------------
    # The checks above look for DISCONTINUITIES -- a jump between adjacent
    # samples. They are blind to an additive broadband burst, which is what
    # actually shows up as a horizontal streak across a waterfall: it raises
    # the power for a moment without stepping the waveform, and after the CIC
    # and FIR its slew is bounded by the passband anyway.
    #
    # Short-time power, robust outlier detection, and above all the PERIOD of
    # whatever is found -- because the period is what names the culprit.
    w = 512
    nw = len(x) // w
    if nw >= 16:
        pw = np.sum(np.abs(x[:nw * w].reshape(nw, w)) ** 2, axis=1) / w
        med = float(np.median(pw))
        # median absolute deviation: immune to the bursts themselves
        mad = float(np.median(np.abs(pw - med))) or 1e-30
        z = (pw - med) / (1.4826 * mad)
        hits = np.flatnonzero(z > 8.0)
        print("\nBurst scan       window %d samples (%.2f ms), %d windows"
              % (w, 1e3 * w / args.samp_rate, nw))
        print("  median power   %.3e   burst threshold %.3e"
              % (med, med + 8.0 * 1.4826 * mad))
        if len(hits) == 0:
            print("  RESULT         no impulsive bursts")
        else:
            print("  bursts         %d windows (%.2f%% of the record), "
                  "peak %.1f dB over median"
                  % (len(hits), 100.0 * len(hits) / nw,
                     10 * math.log10(float(np.max(pw[hits])) / med)))
            # Group adjacent windows into single events, then time them.
            ev = [hits[0]]
            for a, b in zip(hits, hits[1:]):
                if b - a > 1:
                    ev.append(b)
            ev = np.array(ev)
            print("  events         %d" % len(ev))
            if len(ev) >= 3:
                gap = np.diff(ev) * w
                gmed = float(np.median(gap))
                jit = float(np.std(gap))
                print("  period         %.1f ms median (%.1f Hz), "
                      "jitter %.1f ms"
                      % (1e3 * gmed / args.samp_rate,
                         args.samp_rate / gmed if gmed else 0.0,
                         1e3 * jit / args.samp_rate))
                in_bds = gmed / args.bd_samples
                near = abs(in_bds - round(in_bds))
                print("  vs BD period   %.2f BDs%s" % (in_bds,
                      "  <-- integer multiple, so it tracks the ring"
                      if near < 0.08 and in_bds >= 0.92
                      else "  (not a whole number of BDs -- unrelated to the "
                           "descriptor ring)"))
            print("  NOTE           bursts are additive energy, NOT a broken "
                  "stream. The step and glitch")
            print("                 checks above cover the ring; this does "
                  "not. Chase it by period.")

    if failed:
        print("\nRESULT           DISCONTINUOUS")
        if bad or (len(outliers) and
                   np.count_nonzero((outliers % args.bd_samples)
                                    >= args.bd_samples - 1) > len(outliers) // 2):
            print("                 Ring-aligned, so the reclaim path is "
                  "splicing, repeating or dropping")
            print("                 slots. fau_source's counters cannot see "
                  "this: a BD that retires with the")
            print("                 right length and SOF/EOF is 'good' by "
                  "every test the block can make.")
        else:
            print("                 NOT ring-aligned -- this is not "
                  "fau_source's descriptor handling.")
        return 1

    print("\nRESULT           continuous -- no discontinuity at any BD "
          "boundary or anywhere else")
    return 0


if __name__ == "__main__":
    sys.exit(main())
