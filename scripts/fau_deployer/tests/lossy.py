#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Fault injection wrapping any Transport, so the NAK/retransmit path is
exercised by a test instead of being dead code discovered broken during a
demo.

Faults are applied only to the desktop -> board direction. That is enough
to exercise the sender's retransmit logic; corrupting board -> desktop
traffic would mostly re-test the same code paths from the other side, and
the receiver has no need to be hardened against a hostile sender -- only
this deployer ever talks to it.
"""

import random
import time

from ..core.transport import Transport


class LossyTransport(Transport):
    def __init__(self, inner, bit_flip_every=0, drop_line_every=0,
                printk_every=0, latency=0.0, truncate_after=None,
                seed=None):
        self._inner = inner
        self.name = "lossy(%s)" % inner.name
        self._bit_flip_every = bit_flip_every
        self._drop_line_every = drop_line_every
        self._printk_every = printk_every
        self._latency = latency
        self._truncate_after = truncate_after
        self._rng = random.Random(seed)
        self._write_count = 0
        self._bytes_written = 0
        self._truncated = False

    def write(self, data):
        if self._truncated:
            return
        self._write_count += 1

        if (self._truncate_after is not None
                and self._bytes_written >= self._truncate_after):
            self._truncated = True
            return  # simulate a reboot: everything from here vanishes

        if self._drop_line_every and self._write_count % self._drop_line_every == 0:
            self._bytes_written += len(data)
            return  # the whole line never leaves the desktop

        if self._bit_flip_every and self._write_count % self._bit_flip_every == 0:
            data = _flip_one_bit(data, self._rng)

        if self._latency:
            time.sleep(self._latency)

        self._inner.write(data)
        self._bytes_written += len(data)

        if self._printk_every and self._write_count % self._printk_every == 0:
            self._inner.write(b"[  123.456789] cpu cpu0: some kernel noise\n")

    def read(self, maxlen, timeout):
        return self._inner.read(maxlen, timeout)

    def drain_input(self, settle=0.2):
        return self._inner.drain_input(settle)

    def close(self):
        self._inner.close()


def _flip_one_bit(data, rng):
    if not data:
        return data
    idx = rng.randrange(len(data))
    b = bytearray(data)
    b[idx] ^= 0x01
    return bytes(b)
