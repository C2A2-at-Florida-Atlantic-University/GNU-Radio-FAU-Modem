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

import json
import os
import shutil
import signal
import sys
import tempfile
import unittest

from .. import cli
from ..core import boards as boards_mod
from ..core import report
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
            # None means a bare flag (--run, --no-deploy) rather than an
            # option with a value.
            flat += [k] if v is None else [k, str(v)]
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

    def test_back_to_back_redeploy_against_the_same_shell(self):
        self._spawn_shell("login")
        rc1 = cli.main(self._base_argv())
        self.assertEqual(rc1, cli.EXIT_OK)

        # cli.main() shuts the receiver down after a successful transfer
        # (see _shutdown_receiver), so this is a fresh bootstrap, not reuse
        # of a still-running receiver -- it must still succeed against the
        # same "board" shell right away, with no leftover receiver state or
        # stuck prompt marker from the first invocation getting in the way.
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


FLOWGRAPH_QUICK = """\
from argparse import ArgumentParser
import sys

def argument_parser():
    p = ArgumentParser()
    p.add_argument("--label", default="none")
    return p

opts = argument_parser().parse_args()
print("RAN label=%s" % opts.label)
"""

FLOWGRAPH_FAILS = """\
from argparse import ArgumentParser
import sys
print("boom")
sys.exit(4)
"""


class TestArgumentErrors(unittest.TestCase):
    """Usage errors that must be caught before a port is ever opened."""

    def test_no_deploy_with_nothing_else_to_do(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--flowgraph", TX_SINE, "--port", "/dev/x",
                     "--no-deploy"])
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_load_bitstream_requires_a_board(self):
        # The bitstream to load is looked up in bitstreams.json,
        # not passed by hand.
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--port", "/dev/x", "--no-deploy", "--load-bitstream"])
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_deploy_without_a_flowgraph(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--port", "/dev/x"])
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_no_port_and_no_board(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--flowgraph", TX_SINE])
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_an_unknown_board_names_what_is_available(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--flowgraph", TX_SINE, "--board", "no-such-board",
                     "--port", "/dev/x", "--dry-run"])
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_list_boards_needs_no_port(self):
        self.assertEqual(cli.main(["--list-boards"]), cli.EXIT_OK)

    def test_board_alone_never_supplies_a_port(self):
        # bitstreams.json records nothing about how to reach a board, so
        # --board can never stand in for --port. A recorded port would go
        # stale and deploy to the wrong board.
        bits = boards_mod.load_bitstreams()
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--flowgraph", TX_SINE, "--board", bits.names[0]])
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)


class TestBoardResolution(unittest.TestCase):
    """--board selects bitstreams and (via credentials.json) a login, and
    nothing else. An explicit flag always wins."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fau_bits_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bits = os.path.join(self.tmp, "bitstreams.json")
        with open(self.bits, "w", encoding="utf-8") as fh:
            json.dump({
                "S10": {"bootstrap": "/fw/boot.bit.bin",
                        "tx": "/fw/S10_dac.bit.bin",
                        "rx": "/fw/S10_adc.bit.bin"},
                "S20": {"rx": "/fw/S20_adc.bit.bin"},
            }, fh)
        self.creds = os.path.join(self.tmp, "credentials.json")
        with open(self.creds, "w", encoding="utf-8") as fh:
            json.dump({"S10": {"user": "boarduser",
                               "password": "boardpass"}}, fh)

    def _resolve(self, *extra):
        parser = cli._build_parser()
        args = parser.parse_args(
            ["--flowgraph", TX_SINE, "--board", "S10",
             "--bitstreams", self.bits, "--credentials", self.creds,
             "--port", "/dev/x", "--dry-run"] + list(extra))
        bitstream, _listed = cli._resolve_target(parser, args)
        return args, bitstream

    def test_credentials_file_supplies_the_login(self):
        args, _bit = self._resolve()
        self.assertEqual(args.user, "boarduser")
        self.assertEqual(args.password, "boardpass")

    def test_a_board_absent_from_credentials_falls_back_to_defaults(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            ["--flowgraph", TX_SINE, "--board", "S20",
             "--bitstreams", self.bits, "--credentials", self.creds,
             "--port", "/dev/x", "--dry-run"])
        cli._resolve_target(parser, args)
        self.assertEqual(args.user, boards_mod.USER_DEFAULT)
        self.assertEqual(args.password, boards_mod.PASSWORD_DEFAULT)

    def test_a_missing_credentials_file_is_not_an_error(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            ["--flowgraph", TX_SINE, "--board", "S10",
             "--bitstreams", self.bits,
             "--credentials", os.path.join(self.tmp, "absent.json"),
             "--port", "/dev/x", "--dry-run"])
        cli._resolve_target(parser, args)
        self.assertEqual(args.user, boards_mod.USER_DEFAULT)

    def test_the_board_never_supplies_connection_details(self):
        # Defaults come from constants, not from the file.
        args, _bit = self._resolve()
        self.assertEqual(args.baud, boards_mod.BAUD_DEFAULT)
        self.assertEqual(args.dest, cli.DEST_DEFAULT)

    def test_explicit_flags_win(self):
        args, _bit = self._resolve("--baud", "9600", "--user", "me",
                                   "--password", "pw", "--dest", "/my/dest")
        self.assertEqual(args.baud, 9600)
        self.assertEqual(args.user, "me")
        self.assertEqual(args.password, "pw")
        self.assertEqual(args.dest, "/my/dest")

    def test_several_bitstreams_require_choosing_one(self):
        parser = cli._build_parser()
        args = parser.parse_args(
            ["--board", "S10", "--bitstreams", self.bits, "--no-deploy",
             "--load-bitstream", "--port", "/dev/x", "--dry-run"])
        with self.assertRaises(SystemExit) as ctx:
            cli._resolve_target(parser, args)
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def _steps(self, *extra):
        parser = cli._build_parser()
        args = parser.parse_args(
            ["--bitstreams", self.bits, "--no-deploy", "--load-bitstream",
             "--port", "/dev/x", "--dry-run"] + list(extra))
        steps, _listed = cli._resolve_target(parser, args)
        return steps

    def test_a_mode_resolves_to_bootstrap_then_that_mode(self):
        self.assertEqual(self._steps("--board", "S10", "--mode", "rx"),
                         [("bootstrap", "/fw/boot.bit.bin"),
                          ("rx", "/fw/S10_adc.bit.bin")])

    def test_bootstrap_alone_is_one_step(self):
        self.assertEqual(self._steps("--board", "S10", "--mode", "bootstrap"),
                         [("bootstrap", "/fw/boot.bit.bin")])

    def test_a_board_without_bootstrap_loads_the_mode_alone(self):
        # S20 has only an rx entry, so there is nothing to chain.
        self.assertEqual(self._steps("--board", "S20"),
                         [("rx", "/fw/S20_adc.bit.bin")])

    def test_a_missing_bootstrap_is_warned_about(self):
        seen = []
        prev = report.set_sink(lambda text, is_err: seen.append(text))
        self.addCleanup(report.set_sink, prev)
        self._steps("--board", "S20")
        joined = "\n".join(seen)
        self.assertIn("has no 'bootstrap' entry", joined)

    def test_a_single_bitstream_needs_no_choosing(self):
        self.assertEqual(self._steps("--board", "S20"),
                         [("rx", "/fw/S20_adc.bit.bin")])

    def test_an_unknown_mode_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            self._steps("--board", "S10", "--mode", "bootloader")
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)


class TestRun(CliTestCase):
    """--run against a real shell, with a stand-in flowgraph already in the
    destination directory (--no-deploy), so this exercises the run path
    without also re-testing the transfer.

    Always --no-sudo: the "board" here is this machine's own /bin/sh, and
    the run path defaults to sudo because the real blocks need /dev/mem.
    Real sudo is covered in test_runner.py::TestRealSudo, which skips
    where passwordless sudo is unavailable.
    """

    def _argv(self, **overrides):
        overrides.setdefault("--no-sudo", None)
        return self._base_argv(**overrides)

    def _place(self, text, name="fg.py"):
        os.makedirs(self.dest, exist_ok=True)
        path = os.path.join(self.dest, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_runs_and_reports_success(self):
        path = self._place(FLOWGRAPH_QUICK)
        self._spawn_shell("login")
        rc = cli.main(self._argv(**{
            "--flowgraph": path, "--no-deploy": None, "--run": None,
        }))
        self.assertEqual(rc, cli.EXIT_OK)

    def test_params_are_passed_through(self):
        # Quoted INSIDE --params, so it is one argument. Split by shlex on
        # the desktop and re-quoted per token for the board's shell, which
        # is what keeps the space (and a `$`) from being reinterpreted
        # there. Asserting on the flowgraph's own echo rather than just the
        # exit code -- rc 0 alone would also hold if the value arrived
        # mangled.
        path = self._place(FLOWGRAPH_QUICK)
        self._spawn_shell("login")
        seen = []
        prev = report.set_sink(lambda text, is_err: seen.append(text))
        self.addCleanup(report.set_sink, prev)
        rc = cli.main(self._argv(**{
            "--flowgraph": path, "--no-deploy": None, "--run": None,
            "--params": '--label "two words"',
        }))
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertTrue(any("RAN label=two words" in line for line in seen),
                        seen)

    def test_a_value_that_is_not_a_flag_still_reaches_argparse(self):
        # unknown_flags() only checks dash tokens -- it cannot tell a
        # positional from a value without the flowgraph's real parser. So a
        # stray bare word passes desktop validation and is rejected by
        # argparse on the board, which shows up as a nonzero run.
        path = self._place(FLOWGRAPH_QUICK)
        self._spawn_shell("login")
        rc = cli.main(self._argv(**{
            "--flowgraph": path, "--no-deploy": None, "--run": None,
            "--params": "--label two words",
        }))
        self.assertEqual(rc, cli.EXIT_RUN)

    def test_a_bad_param_is_caught_before_the_run(self):
        path = self._place(FLOWGRAPH_QUICK)
        self._spawn_shell("login")
        with self.assertRaises(SystemExit) as ctx:
            cli.main(self._argv(**{
                "--flowgraph": path, "--no-deploy": None, "--run": None,
                "--params": "--not-an-option 1",
            }))
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_a_failing_flowgraph_gets_its_own_exit_code(self):
        path = self._place(FLOWGRAPH_FAILS)
        self._spawn_shell("login")
        rc = cli.main(self._argv(**{
            "--flowgraph": path, "--no-deploy": None, "--run": None,
        }))
        self.assertEqual(rc, cli.EXIT_RUN)

    def test_sudo_is_the_default_for_a_run(self):
        # The complement of every other test here: without --no-sudo the
        # command really does carry it, so the default cannot silently
        # regress to a non-root run that dies in the block constructor.
        parser = cli._build_parser()
        args = parser.parse_args(["--flowgraph", TX_SINE, "--port", "/dev/x",
                                 "--run", "--no-deploy"])
        self.assertFalse(args.no_sudo)

    def test_deploy_then_run_in_one_invocation(self):
        self._spawn_shell("login")
        src = tempfile.mkdtemp(prefix="fau_src_")
        self.addCleanup(shutil.rmtree, src, ignore_errors=True)
        path = os.path.join(src, "fg.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(FLOWGRAPH_QUICK)
        rc = cli.main(self._argv(**{
            "--flowgraph": path, "--run": None,
        }))
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("fg.py", os.listdir(self.dest))


if __name__ == "__main__":
    unittest.main()
