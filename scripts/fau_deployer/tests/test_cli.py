#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""CLI orchestration tests: the full desktop pipeline (parse -> payload ->
login -> bootstrap -> transfer) run against a real forkpty'd shell/receiver,
with SerialTransport monkeypatched to an in-process FdTransport instead of a
real serial device. This is the closest thing to "run the actual tool" that
doesn't need a board.
"""

import os
import shutil
import signal
import sys
import tempfile
import unittest

from .. import cli
from ..core.transport import FdTransport

EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))),
    "components", "layers", "meta-fau-modem", "gr-fau_modem", "examples")
TX_SINE = os.path.join(EXAMPLES_DIR, "tx_sine.py")
SHELL_STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shell_stub.py")


class CliTestCase(unittest.TestCase):
    """Spawns a real shell (via forkpty) as the fake "board", monkeypatches
    SerialTransport to hand back an FdTransport over that pty, and restores
    the original class on teardown."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fau_cli_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dest = os.path.join(self.tmp, "flowgraphs")
        self.remote_dir = os.path.join(self.tmp, ".fau")
        self._orig_serial_transport = cli.SerialTransport
        self.addCleanup(setattr, cli, "SerialTransport", self._orig_serial_transport)

    def _spawn_shell(self, mode="login", user="petalinux", password="1234"):
        pid, master_fd = os.forkpty()
        if pid == 0:
            os.execvp(sys.executable,
                     [sys.executable, SHELL_STUB, "--mode", mode,
                      "--user", user, "--password", password])
            os._exit(127)
        self.addCleanup(self._kill, pid)
        self.addCleanup(self._close_quietly, master_fd)

        # cli.main()'s own `finally` closes the transport's fd at the end of
        # every real invocation (correct: a real run owns its serial port
        # for its own process lifetime and closes it on exit). A test that
        # calls cli.main() more than once against the "same board" would
        # otherwise have the first call's close() take out the fd the
        # second call needs -- dup() gives each invocation its own fd over
        # the same underlying pty, so closing one doesn't affect the other.
        class FakeSerialTransport(FdTransport):
            def __init__(self, port, baud=115200, transcript=None):
                super().__init__(os.dup(master_fd), transcript=transcript, name=port)

        cli.SerialTransport = FakeSerialTransport
        return pid

    @staticmethod
    def _close_quietly(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    @staticmethod
    def _kill(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _base_argv(self, **overrides):
        argv = {
            "--flowgraph": TX_SINE,
            "--port": "/dev/fake0",
            "--dest": self.dest,
            "--remote-dir": self.remote_dir,
            "--user": "petalinux",
            "--password": "1234",
            "--handshake-timeout": "8",
            "--login-timeout": "10",
        }
        argv.update(overrides)
        flat = []
        for k, v in argv.items():
            flat += [k, str(v)]
        return flat


class TestDryRun(unittest.TestCase):
    def test_dry_run_never_touches_the_port(self):
        # cli.SerialTransport is untouched -- if --dry-run tried to open a
        # port, this would fail immediately since /dev/does-not-exist can't
        # be opened.
        rc = cli.main([
            "--flowgraph", TX_SINE,
            "--port", "/dev/does-not-exist-anywhere",
            "--dry-run",
        ])
        self.assertEqual(rc, cli.EXIT_OK)

    def test_bad_chunk_size_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main([
                "--flowgraph", TX_SINE,
                "--port", "/dev/x",
                "--chunk-size", "13",
                "--dry-run",
            ])
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_missing_flowgraph_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main([
                "--flowgraph", "/no/such/flowgraph.py",
                "--port", "/dev/x",
                "--dry-run",
            ])
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)


class TestFullDeploy(CliTestCase):
    def test_deploys_tx_sine_end_to_end(self):
        self._spawn_shell("login")
        rc = cli.main(self._base_argv())
        self.assertEqual(rc, cli.EXIT_OK)

        deployed = sorted(os.listdir(self.dest))
        self.assertEqual(deployed, ["fau_tx_common.py", "tx_sine.py"])
        with open(TX_SINE, "rb") as f:
            want = f.read()
        with open(os.path.join(self.dest, "tx_sine.py"), "rb") as f:
            got = f.read()
        self.assertEqual(got, want)

    def test_redeploy_reuses_the_already_current_receiver(self):
        self._spawn_shell("login")
        rc1 = cli.main(self._base_argv())
        self.assertEqual(rc1, cli.EXIT_OK)

        # Same "board" process is still up (the receiver returned to idle
        # after finishing the transfer and is serving again). A second
        # deploy against it must not need to re-bootstrap.
        rc2 = cli.main(self._base_argv())
        self.assertEqual(rc2, cli.EXIT_OK)


class TestLoginFailureExitCode(CliTestCase):
    def test_wrong_password_exits_3_not_generic(self):
        self._spawn_shell("login", password="the-real-password")
        rc = cli.main(self._base_argv(**{"--password": "a-wrong-guess"}))
        self.assertEqual(rc, cli.EXIT_SESSION)
        # And nothing should have been deployed.
        self.assertFalse(os.path.exists(self.dest))


class TestUBootExitCode(CliTestCase):
    def test_uboot_console_exits_3(self):
        self._spawn_shell("uboot")
        rc = cli.main(self._base_argv())
        self.assertEqual(rc, cli.EXIT_SESSION)


if __name__ == "__main__":
    unittest.main()
