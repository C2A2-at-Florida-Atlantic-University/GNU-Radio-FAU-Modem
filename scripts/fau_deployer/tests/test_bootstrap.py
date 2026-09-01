#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Receiver bootstrap tests: pushing onto a virgin board, the fast already-
current path, and re-pushing over both a broken and a stale-but-valid
remote copy. Runs the login flow against a real shell (via FakeShell) with
the "board" filesystem standing in as a local temp directory -- the pushed
file really is written and really is a working receiver.py, just reached
via the local machine's shell instead of a serial console.
"""

import os
import shutil
import tempfile
import time
import unittest

from ..core import bootstrap as B
from ..core.session import BoardSession
from ..core.transport import LineReader
from .fake_shell import FakeShell

_HERE = os.path.dirname(os.path.abspath(__file__))
RECEIVER_PATH = os.path.join(os.path.dirname(_HERE), "board", "receiver.py")


class BootstrapTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fau_bootstrap_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote_dir = os.path.join(self.tmp, ".fau")
        self.dest = os.path.join(self.tmp, "flowgraphs")
        self.remote_receiver = os.path.join(self.remote_dir, "receiver.py")

        self.shell = FakeShell("login")
        self.shell.start()
        self.addCleanup(self.shell.stop)
        self.transport = self.shell.transport()
        self.reader = LineReader(self.transport)
        self.session = BoardSession(self.transport, self.reader)
        self.session.connect()

    def _stop_receiver_and_reconnect(self):
        """Retire whatever's currently running (receiver or otherwise) and
        get back to a usable shell session, the way ensure_receiver's own
        stale-retirement path does."""
        self.transport.write(b"\x03")
        time.sleep(0.3)
        self.reader.reset()
        sess = BoardSession(self.transport, self.reader)
        sess.connect()
        self.session = sess

    def _ensure(self):
        B.ensure_receiver(self.session, self.transport, self.reader,
                          RECEIVER_PATH, remote_dir=self.remote_dir,
                          dest=self.dest, idle_timeout=20,
                          handshake_timeout=8)


class TestFreshBootstrap(BootstrapTestCase):
    def test_pushes_a_byte_identical_copy(self):
        self.assertFalse(os.path.exists(self.remote_receiver))
        self._ensure()
        self.assertTrue(os.path.isfile(self.remote_receiver))
        with open(RECEIVER_PATH, "rb") as f:
            want = f.read()
        with open(self.remote_receiver, "rb") as f:
            got = f.read()
        self.assertEqual(got, want)


class TestAlreadyCurrent(BootstrapTestCase):
    def test_second_call_does_not_repush(self):
        self._ensure()
        self._stop_receiver_and_reconnect()
        mtime_before = os.path.getmtime(self.remote_receiver)
        time.sleep(0.05)
        self._ensure()
        mtime_after = os.path.getmtime(self.remote_receiver)
        self.assertEqual(mtime_before, mtime_after,
                         "an already-current receiver must not be rewritten")


class TestStaleReceiver(BootstrapTestCase):
    def test_repushes_over_a_broken_remote_copy(self):
        self._ensure()
        self._stop_receiver_and_reconnect()
        self.session.run_ok('printf "garbage" > %s' % self.remote_receiver)

        self._ensure()
        with open(RECEIVER_PATH, "rb") as f:
            want = f.read()
        with open(self.remote_receiver, "rb") as f:
            got = f.read()
        self.assertEqual(got, want)

    def test_repushes_over_a_stale_but_still_valid_copy(self):
        # A remote copy that is valid Python and WILL run (unlike the
        # "garbage" case above) but reports a different sha -- this is the
        # actual "receiver present but stale" branch, distinct from "no
        # receiver response at all".
        self._ensure()
        self._stop_receiver_and_reconnect()
        with open(RECEIVER_PATH, "r") as f:
            older_version = f.read() + "\n# an old trailing comment\n"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
            tf.write(older_version)
            older_path = tf.name
        self.addCleanup(os.remove, older_path)
        self.session.run_ok("cp %s %s" % (older_path, self.remote_receiver))

        self._ensure()
        with open(RECEIVER_PATH, "rb") as f:
            want = f.read()
        with open(self.remote_receiver, "rb") as f:
            got = f.read()
        self.assertEqual(got, want,
                         "must restore the CURRENT local copy, not keep "
                         "the stale-but-runnable one")


if __name__ == "__main__":
    unittest.main()
