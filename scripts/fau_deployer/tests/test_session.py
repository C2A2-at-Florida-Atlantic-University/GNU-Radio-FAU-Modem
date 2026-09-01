#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Login/prompt state machine tests against a real pty and a real shell
(via shell_stub.py + os.forkpty()) -- no board needed. This is the fiddliest
part of the transport (a login prompt, unlike a shell prompt, cannot be
safely probed the same way -- see BoardSession._probe()'s docstring), so it
gets exercised against real termios/tty behavior rather than only reasoned
about.
"""

import unittest

from ..core.transport import LineReader
from ..core.session import BoardSession, LoginError
from .fake_shell import FakeShell


class SessionTestCase(unittest.TestCase):
    def _connect(self, mode, **shell_kwargs):
        shell = FakeShell(mode, **shell_kwargs)
        shell.start()
        self.addCleanup(shell.stop)
        transport = shell.transport()
        reader = LineReader(transport)
        return transport, reader


class TestAlreadyAtPrompt(SessionTestCase):
    def test_no_login_banner_at_all(self):
        transport, reader = self._connect("prompt")
        sess = BoardSession(transport, reader)
        sess.connect()
        rc, out = sess.run("echo already-a-prompt")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "already-a-prompt")


class TestLoginFlow(SessionTestCase):
    def test_correct_credentials(self):
        transport, reader = self._connect("login", user="petalinux",
                                          password="1234")
        sess = BoardSession(transport, reader, user="petalinux",
                           password="1234")
        sess.connect()
        rc, out = sess.run("echo hello world")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "hello world")

    def test_nonzero_exit_status_reported(self):
        transport, reader = self._connect("login")
        sess = BoardSession(transport, reader)
        sess.connect()
        rc, out = sess.run("false")
        self.assertEqual(rc, 1)

    def test_run_ok_raises_on_nonzero(self):
        transport, reader = self._connect("login")
        sess = BoardSession(transport, reader)
        sess.connect()
        with self.assertRaises(LoginError):
            sess.run_ok("false")

    def test_repeated_commands_do_not_desync(self):
        # Regression: an unterminated prompt match used to be left in the
        # reader's buffer and get glued onto the front of the NEXT
        # command's output -- this only showed up on the second (or later)
        # exchange against the same session.
        transport, reader = self._connect("login")
        sess = BoardSession(transport, reader)
        sess.connect()
        for i in range(5):
            rc, out = sess.run("echo round-%d" % i)
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "round-%d" % i)

    def test_wrong_password_fails_without_retry_loop(self):
        transport, reader = self._connect("login", password="the-real-password")
        sess = BoardSession(transport, reader, password="a-guess")
        with self.assertRaises(LoginError) as ctx:
            sess.connect()
        self.assertIn("incorrect", str(ctx.exception))


class TestUBoot(SessionTestCase):
    def test_distinct_failure_not_a_login_timeout(self):
        transport, reader = self._connect("uboot")
        sess = BoardSession(transport, reader)
        with self.assertRaises(LoginError) as ctx:
            sess.connect()
        self.assertIn("U-Boot", str(ctx.exception))


class TestReconnect(SessionTestCase):
    def test_second_session_reuses_the_already_configured_prompt(self):
        transport, reader = self._connect("prompt")
        BoardSession(transport, reader).connect()
        # A second BoardSession over the SAME transport/reader must land in
        # the 'ready' branch immediately rather than trying to log in again.
        sess2 = BoardSession(transport, reader)
        sess2.connect()
        rc, out = sess2.run("echo still-fine")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "still-fine")


class TestStaleRecovery(SessionTestCase):
    def test_orphaned_program_recovered_with_ctrl_c(self):
        transport, reader = self._connect("orphan")
        sess = BoardSession(transport, reader)
        sess.connect()  # must recover via _recover_stale() and then log in
        rc, out = sess.run("echo after-recovery")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "after-recovery")


if __name__ == "__main__":
    unittest.main()
