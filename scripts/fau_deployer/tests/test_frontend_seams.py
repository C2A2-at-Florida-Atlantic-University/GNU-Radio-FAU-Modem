#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""The seams a non-console front-end needs from core, tested against the
real receiver so they are exercised on the same path a real deploy takes:

- core/report.py's redirectable sink (gui.py routes every core line into
  its log pane)
- Sender's on_progress callback (drives the progress bar)
- Sender's should_stop callback (the Cancel button)

These exist only because a GUI needs them, so without tests they would be
verified for the first time by someone clicking a button during a demo.
"""

import os
import shutil
import tempfile
import unittest

from ..core import payload as P
from ..core import report
from ..core.sender import Sender, TransferError
from ..core.transport import LineReader
from .fake_board import FakeBoard

EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))),
    "components", "layers", "meta-fau-modem", "gr-fau_modem", "examples")


class TestReportSink(unittest.TestCase):
    def setUp(self):
        self.captured = []
        prev = report.set_sink(
            lambda text, is_err: self.captured.append((text, is_err)))
        self.addCleanup(report.set_sink, prev)

    def test_every_helper_goes_through_the_sink(self):
        report.say("tag", "hello")
        report.banner("SECTION")
        report.kv("key", "value")
        report.blank()
        report.warn("careful")
        report.error("broken")
        report.say_err("tag", "to stderr")
        texts = [t for t, _ in self.captured]
        self.assertIn("[tag] hello", texts)
        self.assertIn("SECTION", texts)
        self.assertTrue(any(t.startswith("  key") and "value" in t
                            for t in texts))
        self.assertIn("", texts)
        self.assertTrue(any("careful" in t for t in texts))

    def test_error_streams_are_flagged_not_merged(self):
        # The GUI colours these differently, so "which stream" has to
        # survive the redirect rather than being flattened to text.
        report.say("tag", "normal")
        report.error("bad")
        report.warn("iffy")
        report.say_err("tag", "also bad")
        flags = dict((t, e) for t, e in self.captured)
        self.assertFalse(flags["[tag] normal"])
        self.assertTrue(all(is_err for text, is_err in self.captured
                            if "bad" in text or "iffy" in text))

    def test_setting_a_sink_returns_the_previous_one(self):
        # The contract addCleanup relies on: set_sink hands back what was
        # installed, so a caller can restore it instead of clobbering it.
        def a(*_args):
            pass

        def b(*_args):
            pass

        was = report.set_sink(a)          # `was` is setUp's capturing sink
        self.assertIs(report.set_sink(b), a)
        self.assertIs(report.set_sink(was), b)

    def test_none_restores_stdout(self):
        report.set_sink(None)
        report.say("tag", "goes to stdout, not the list")
        self.assertEqual(self.captured, [])


class SenderSeamTestCase(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="fau_dest_")
        self.addCleanup(shutil.rmtree, self.dest, ignore_errors=True)
        self.board = FakeBoard(self.dest, receiver_args=["--idle-timeout", "20"])
        self.board.start_receiver()
        self.addCleanup(self.board.stop)
        self.transport = self.board.transport()
        self.reader = LineReader(self.transport)
        self.assertIsNotNone(
            self.reader.wait_for("===FAU-RECV-READY", 5.0),
            "receiver never announced READY")

    def _payload(self, chunk_size=256):
        entries, main_arc = P.collect_files(
            os.path.join(EXAMPLES_DIR, "tx_sine.py"))
        return P.build_payload(entries, main_arc, chunk_size=chunk_size)


class TestProgressCallback(SenderSeamTestCase):
    def test_progress_reaches_the_callback_and_completes(self):
        pl = self._payload()
        seen = []
        sender = Sender(self.transport, self.reader, pl, self.dest,
                        progress=False,
                        on_progress=lambda acked, total, info: seen.append(
                            (acked, total, info)))
        sender.send()
        self.assertTrue(seen, "on_progress was never called")
        for _acked, total, _info in seen:
            self.assertEqual(total, len(pl.chunks))
        # Monotonic and finishing at 100% -- a progress bar that goes
        # backwards or stops short is worse than none.
        acked = [a for a, _t, _i in seen]
        self.assertEqual(acked, sorted(acked))
        self.assertEqual(acked[-1], len(pl.chunks))

    def test_callback_info_carries_the_counters_the_ui_shows(self):
        pl = self._payload()
        seen = []
        Sender(self.transport, self.reader, pl, self.dest, progress=False,
               on_progress=lambda a, t, info: seen.append(info)).send()
        for key in ("retransmits", "naks", "timeouts", "bytes_on_wire",
                    "elapsed", "rate"):
            self.assertIn(key, seen[-1])

    def test_console_progress_and_callback_are_independent(self):
        # Not alternatives: progress= writes a \r line to a terminal,
        # on_progress hands over numbers. A GUI started from a terminal can
        # reasonably want both.
        pl = self._payload()
        seen = []
        Sender(self.transport, self.reader, pl, self.dest, progress=False,
               on_progress=lambda a, t, i: seen.append(a)).send()
        self.assertTrue(seen)


class TestCancel(SenderSeamTestCase):
    def test_cancel_stops_the_transfer_with_a_clear_error(self):
        pl = self._payload(chunk_size=64)  # many chunks, so cancel lands mid-flight
        state = {"acked": 0}

        def note(acked, _total, _info):
            state["acked"] = acked

        sender = Sender(self.transport, self.reader, pl, self.dest,
                        progress=False, on_progress=note,
                        should_stop=lambda: state["acked"] >= 3)
        with self.assertRaises(TransferError) as ctx:
            sender.send()
        msg = str(ctx.exception)
        self.assertIn("cancelled", msg)
        # And it must say the board wasn't left half-written: the receiver
        # only stages files once the whole payload validates.
        self.assertIn("nothing on the board was overwritten", msg)
        self.assertEqual(os.listdir(self.dest), [])

    def test_not_cancelling_is_unaffected(self):
        pl = self._payload()
        sender = Sender(self.transport, self.reader, pl, self.dest,
                        progress=False, should_stop=lambda: False)
        stats = sender.send()
        self.assertEqual(stats.chunks, len(pl.chunks))
        self.assertIn("tx_sine.py", os.listdir(self.dest))


if __name__ == "__main__":
    unittest.main()
