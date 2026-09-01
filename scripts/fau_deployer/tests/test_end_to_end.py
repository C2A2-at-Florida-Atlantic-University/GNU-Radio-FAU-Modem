#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""End-to-end transport tests against a REAL receiver subprocess on a real
pty -- everything except the Zynq. This is what makes "robust transport" a
checked property rather than an aspiration: without fault injection here,
the NAK/retransmit path is dead code that would first get exercised during
a live demo.
"""

import os
import shutil
import tempfile
import unittest

from ..core import payload as P
from ..core.sender import Sender, TransferError
from ..core.transport import LineReader, Transport
from .fake_board import FakeBoard
from .lossy import LossyTransport

EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))),
    "components", "layers", "meta-fau-modem", "gr-fau_modem", "examples")


def _tx_sine_payload(chunk_size=512, xfer_id=None):
    entries, main_arc = P.collect_files(os.path.join(EXAMPLES_DIR, "tx_sine.py"))
    return entries, P.build_payload(entries, main_arc, chunk_size=chunk_size,
                                    xfer_id=xfer_id)


class TransportEndToEndTestCase(unittest.TestCase):
    """Base class: spins up a FakeBoard against a fresh dest dir per test."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="fau_dest_")
        self.addCleanup(shutil.rmtree, self.dest, ignore_errors=True)
        self.board = FakeBoard(self.dest, receiver_args=["--idle-timeout", "20"])
        self.board.start_receiver()
        self.addCleanup(self.board.stop)
        self.transport = self.board.transport()
        self.reader = LineReader(self.transport)
        ready = self.reader.wait_for("===FAU-RECV-READY", 5.0)
        self.assertIsNotNone(ready, "receiver never announced READY")

    def _assert_dest_matches(self, entries):
        got = sorted(os.listdir(self.dest))
        want = sorted(arc for _, arc in entries)
        self.assertEqual(got, want)
        for path, arc in entries:
            with open(os.path.join(self.dest, arc), "rb") as f:
                deployed = f.read()
            self.assertEqual(path.read_bytes(), deployed)


class TestCleanTransfer(TransportEndToEndTestCase):
    def test_multi_file_flowgraph_arrives_byte_identical(self):
        entries, pl = _tx_sine_payload()
        sender = Sender(self.transport, self.reader, pl, self.dest,
                        window=1, progress=False)
        stats = sender.send()
        self.assertEqual(stats.chunks, len(pl.chunks))
        self.assertEqual(stats.retransmits, 0)
        self._assert_dest_matches(entries)

    def test_redeploy_of_unchanged_files_is_a_byte_identical_noop(self):
        entries, pl1 = _tx_sine_payload(xfer_id="aaaaaaaa")
        Sender(self.transport, self.reader, pl1, self.dest, progress=False).send()
        first_sha = pl1.sha256

        # Re-run the receiver for a second transfer (the same one accepts
        # more than one BEGIN in its serve loop).
        entries2, pl2 = _tx_sine_payload(xfer_id="bbbbbbbb")
        self.assertEqual(pl2.sha256, first_sha,
                         "same inputs must produce a byte-identical archive")
        Sender(self.transport, self.reader, pl2, self.dest, progress=False).send()
        self._assert_dest_matches(entries2)


class TestLossyTransfer(TransportEndToEndTestCase):
    def test_survives_bit_flips_and_dropped_lines(self):
        entries, pl = _tx_sine_payload(chunk_size=256)  # more chunks, more chances to hit a fault
        lossy = LossyTransport(self.transport, bit_flip_every=5,
                               drop_line_every=7, seed=1234)
        # Route the reader through the same lossy wrapper so injected
        # corruption is visible end-to-end, exactly as it would be for a
        # real flaky USB-serial adapter.
        reader = LineReader(lossy)
        sender = Sender(lossy, reader, pl, self.dest,
                        window=1, chunk_timeout=1.0, chunk_retries=8,
                        progress=False)
        stats = sender.send()
        self.assertGreater(stats.retransmits, 0,
                           "the lossy transport should have forced at least "
                           "one retransmit -- otherwise this test isn't "
                           "actually exercising the recovery path")
        self._assert_dest_matches(entries)

    def test_kernel_printk_noise_does_not_break_framing(self):
        entries, pl = _tx_sine_payload(chunk_size=256)
        lossy = LossyTransport(self.transport, printk_every=6)
        reader = LineReader(lossy)
        sender = Sender(lossy, reader, pl, self.dest, progress=False)
        sender.send()
        self._assert_dest_matches(entries)

    def test_giving_up_after_retry_budget_raises_transfer_error(self):
        # Every write drops -- nothing will ever arrive, so this must fail
        # cleanly rather than hang.
        entries, pl = _tx_sine_payload(chunk_size=512)
        lossy = LossyTransport(self.transport, drop_line_every=1)
        reader = LineReader(lossy)
        sender = Sender(lossy, reader, pl, self.dest,
                        chunk_timeout=0.3, chunk_retries=2, progress=False)
        with self.assertRaises(TransferError):
            sender.send()


class _NoiseThenAckTransport(Transport):
    """A minimal in-memory Transport double for testing Sender's reboot
    detection in isolation, without needing the real receiver to actually
    reboot (which fake_board.py has no way to simulate)."""

    name = "noise-then-reboot"

    def __init__(self, noise_lines):
        self._pending = "".join(l + "\n" for l in noise_lines).encode("ascii")
        self.aborted = False

    def write(self, data):
        if b"FAU-ABORT" in data:
            self.aborted = True

    def read(self, maxlen, timeout):
        if self._pending:
            chunk, self._pending = self._pending, b""
            return chunk
        return b""

    def drain_input(self, settle=0.2):
        return b""

    def close(self):
        pass


class TestRebootDetection(unittest.TestCase):
    """Sender-only (no real board needed): confirms a login/U-Boot banner
    seen mid-transfer is treated as a reboot, not chased through the full
    chunk-retry budget."""

    def test_login_banner_aborts_immediately(self):
        with tempfile.TemporaryDirectory() as dest:
            entries, main_arc = P.collect_files(
                os.path.join(EXAMPLES_DIR, "tx_sine.py"))
            pl = P.build_payload(entries, main_arc, chunk_size=512)
            transport = _NoiseThenAckTransport(
                ["Welcome to PetaLinux", "petalinux login: "])
            reader = LineReader(transport)
            sender = Sender(transport, reader, pl, dest,
                            chunk_timeout=0.2, chunk_retries=100, progress=False)
            with self.assertRaises(TransferError) as ctx:
                sender.send()
            self.assertIn("rebooted", str(ctx.exception))
            self.assertTrue(transport.aborted,
                            "sender should tell the board to abort once a "
                            "reboot is detected, not leave it waiting")


if __name__ == "__main__":
    unittest.main()
