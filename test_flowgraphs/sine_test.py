#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Generate a 120 kHz sine wave for 10 seconds and write it to a file.

Flowgraph:

    analog.sig_source_f  ->  blocks.head  ->  blocks.file_sink

Plain host-side test, no fau_modem hardware blocks involved. Output is
raw float32 samples; inspect with e.g.:

    python3 -c "import numpy as np; import matplotlib.pyplot as plt; \
        x = np.fromfile('sine_120k.bin', dtype=np.float32); \
        plt.plot(x[:2000]); plt.show()"
"""

import sys

from gnuradio import analog
from gnuradio import blocks
from gnuradio import gr


class sine_test(gr.top_block):
    def __init__(self, samp_rate, freq, amplitude, duration, out_file):
        gr.top_block.__init__(self, "Sine Test", catch_exceptions=True)

        nsamples = int(duration * samp_rate)

        self.src = analog.sig_source_f(
            samp_rate, analog.GR_SIN_WAVE, freq, amplitude)
        self.head = blocks.head(gr.sizeof_float, nsamples)
        self.sink = blocks.file_sink(gr.sizeof_float, out_file)

        self.connect(self.src, self.head, self.sink)


def main():
    samp_rate = 4e6
    freq = 120e3
    amplitude = 0.5
    duration = 10.0
    out_file = "sine_120k.bin"

    print("generating %g s of a %g Hz sine at %g sps -> %s"
          % (duration, freq, samp_rate, out_file))

    tb = sine_test(samp_rate, freq, amplitude, duration, out_file)
    tb.run()

    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
