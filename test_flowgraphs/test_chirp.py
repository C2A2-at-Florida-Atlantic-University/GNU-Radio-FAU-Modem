#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Not titled yet
# GNU Radio version: 3.10.1.1

from gnuradio import analog
from gnuradio import fau_modem
from gnuradio import gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import sys
import signal
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
import math




class test_chirp(gr.top_block):

    def __init__(self):
        gr.top_block.__init__(self, "Not titled yet", catch_exceptions=True)

        ##################################################
        # Variables
        ##################################################
        self.samp_rate = samp_rate = 200_000
        self.nco = nco = 120_000
        self.chirp_speed = chirp_speed = 10
        self.chirp_freq = chirp_freq = 30_000

        ##################################################
        # Blocks
        ##################################################
        self.fau_modem_fau_sink_0 = fau_modem.fau_sink(samp_rate, nco - (chirp_freq //2), 8192, 16, 4, True, True, False, 0.1, 20.0)
        self.analog_sig_source_x_0 = analog.sig_source_f(samp_rate, analog.GR_SAW_WAVE, chirp_speed, 1, 0, 0)
        self.analog_frequency_modulator_fc_0 = analog.frequency_modulator_fc((2 * math.pi * chirp_freq) / (samp_rate))


        ##################################################
        # Connections
        ##################################################
        self.connect((self.analog_frequency_modulator_fc_0, 0), (self.fau_modem_fau_sink_0, 0))
        self.connect((self.analog_sig_source_x_0, 0), (self.analog_frequency_modulator_fc_0, 0))


    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.analog_frequency_modulator_fc_0.set_sensitivity((2 * math.pi * self.chirp_freq) / (self.samp_rate))
        self.analog_sig_source_x_0.set_sampling_freq(self.samp_rate)
        self.fau_modem_fau_sink_0.set_samp_rate(self.samp_rate)

    def get_nco(self):
        return self.nco

    def set_nco(self, nco):
        self.nco = nco
        self.fau_modem_fau_sink_0.set_nco_freq(self.nco - (self.chirp_freq //2))

    def get_chirp_speed(self):
        return self.chirp_speed

    def set_chirp_speed(self, chirp_speed):
        self.chirp_speed = chirp_speed
        self.analog_sig_source_x_0.set_frequency(self.chirp_speed)

    def get_chirp_freq(self):
        return self.chirp_freq

    def set_chirp_freq(self, chirp_freq):
        self.chirp_freq = chirp_freq
        self.analog_frequency_modulator_fc_0.set_sensitivity((2 * math.pi * self.chirp_freq) / (self.samp_rate))
        self.fau_modem_fau_sink_0.set_nco_freq(self.nco - (self.chirp_freq //2))




def main(top_block_cls=test_chirp, options=None):
    tb = top_block_cls()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    tb.start()

    try:
        input('Press Enter to quit: ')
    except EOFError:
        pass
    tb.stop()
    tb.wait()


if __name__ == '__main__':
    main()
