#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""core/session.py's grounding: driving a non-fresh console back to a
`login:` prompt with Ctrl-C then Ctrl-D.

Run against a real pty and a real /bin/sh (tests/shell_stub.py), because the
whole mechanism is about tty line-discipline behaviour -- who receives 0x03
as a signal versus 0x04 as EOF -- which a mocked stream cannot model.
"""

import time
import unittest

from ..core.session import BoardSession, LoginError
from ..core.transport import LineReader
from .fake_shell import FakeShell


class GroundTestCase(unittest.TestCase):
    def _connect_parts(self, mode, **kwargs):
        shell = FakeShell(mode, **kwargs)
        shell.start()
        self.addCleanup(shell.stop)
        transport = shell.transport()
        return transport, LineReader(transport)

    def _session(self, mode, **kwargs):
        transport, reader = self._connect_parts(mode)
        return BoardSession(transport, reader, ground_timeout=2.0,
                            ground_attempts=2, **kwargs), transport, reader


class TestConnectGrounds(GroundTestCase):
    def test_already_logged_in_console_is_grounded_and_relogged_in(self):
        sess, _t, _r = self._session("prompt")
        sess.connect()
        rc, out = sess.run("echo grounded")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "grounded")

    def test_console_that_never_returns_to_login_is_a_clear_failure(self):
        # 'norelogin' hands out one shell and then never offers a login
        # prompt again -- what a shell with IGNOREEOF set looks like from
        # the outside. That this FAILS is the proof grounding was actually
        # attempted: with ground=False the very same console connects fine
        # (see below).
        sess, _t, _r = self._session("norelogin")
        with self.assertRaises(LoginError) as ctx:
            sess.connect()
        msg = str(ctx.exception)
        self.assertIn("Ctrl-D", msg)
        self.assertIn("IGNOREEOF", msg)

    def test_ground_false_adopts_the_existing_shell(self):
        sess, _t, _r = self._session("norelogin", ground=False)
        sess.connect()
        rc, out = sess.run("echo adopted")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "adopted")

    def test_two_sessions_over_one_transport_with_ground_false(self):
        # The documented escape hatch: a second BoardSession deliberately
        # sharing one already-configured console should not pay for a
        # logout/login round trip.
        transport, reader = self._connect_parts("prompt")
        BoardSession(transport, reader, ground=False).connect()
        second = BoardSession(transport, reader, ground=False)
        second.connect()
        rc, out = second.run("echo still-fine")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "still-fine")


class TestGroundOrdering(GroundTestCase):
    def test_foreground_process_is_interrupted_before_the_logout(self):
        # THE ordering test. `sleep` holds the console as a foreground
        # process; 0x04 sent to it is merely EOF on its stdin and does
        # nothing at all, so a Ctrl-D-first implementation would burn every
        # attempt and fail. Reaching a login prompt proves Ctrl-C ran first
        # and retired it.
        #
        # This is the same ordering that keeps a running flowgraph safe: it
        # gets SIGINT (-> tb.stop()/tb.wait() -> the blocks' DMA halt
        # sequence) instead of the SIGHUP a logout would have delivered.
        sess, transport, reader = self._session("prompt", ground=False)
        sess.connect()
        transport.write(b"sleep 300\r")
        time.sleep(0.5)
        reader.reset()
        self.assertTrue(sess.ground(),
                        "never reached a login prompt -- was Ctrl-C sent "
                        "before Ctrl-D?")
        sess._do_login()
        sess._configure_shell()
        rc, out = sess.run("echo after-interrupt")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "after-interrupt")


class TestGroundRetries(GroundTestCase):
    def _enter_subshell(self, sess, transport, reader):
        transport.write(b"sh\r")
        time.sleep(0.5)
        reader.reset()

    def test_nested_subshell_needs_more_than_one_ctrl_d(self):
        # Each Ctrl-D exits exactly ONE shell, so a subshell somebody left
        # behind eats the first one. This is why ground() retries.
        sess, transport, reader = self._session("prompt", ground=False)
        sess.connect()
        self._enter_subshell(sess, transport, reader)
        self.assertTrue(sess.ground(attempts=3, timeout=2.0))

    def test_retries_are_bounded_rather_than_looping_forever(self):
        # One attempt against a nested subshell exits only the subshell, so
        # no login prompt appears -- and ground() must give up and say so
        # rather than spin.
        sess, transport, reader = self._session("prompt", ground=False)
        sess.connect()
        self._enter_subshell(sess, transport, reader)
        self.assertFalse(sess.ground(attempts=1, timeout=2.0))


if __name__ == "__main__":
    unittest.main()
