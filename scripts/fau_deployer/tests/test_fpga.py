#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""core/fpga.py: loading a pre-staged bitstream with fpgautil.

The stub below reproduces the three behaviours observed on the real board,
because the middle one is the whole reason this module judges output rather
than exit status:

    $ sudo fpgautil -b dummy
    Error: User provided bitstream file doesn't exist
    $ sudo fpgautil firmware/Radio_Top_v2_wrapper.bit.bin
    $                                    <-- -b omitted: rc 0, did nothing
    $ sudo fpgautil -b firmware/Radio_Top_v2_wrapper.bit.bin
    fpga_manager fpga0: writing Radio_Top_v2_wrapper.bit.bin to Xilinx Zynq FPGA Manager
    Time taken to load BIN is 47.000000 Milli Seconds
    BIN FILE loaded through FPGA manager successfully

Runs against a real /bin/sh over a real pty, so PATH, sudo argument
handling and the stdin-password retry are all exercised as shell behaviour
rather than asserted about a mock.
"""

import os
import stat
import tempfile
import unittest

from ..core import fpga, report
from ..core.fpga import FpgaError
from ..core.session import BoardSession
from ..core.transport import LineReader
from .fake_shell import FakeShell

FPGAUTIL_STUB = r'''#!/bin/sh
bit=""
while [ $# -gt 0 ]; do
    case "$1" in
        -b) bit="$2"; shift 2 ;;
        *) shift ;;
    esac
done
case "${FAU_FAKE_FPGAUTIL:-success}" in
success)
    # No -b: exactly the observed silent no-op.
    [ -z "$bit" ] && exit 0
    if [ ! -f "$bit" ]; then
        echo "Error: User provided bitstream file doesn't exist"
        exit 1
    fi
    [ -n "$FAU_FAKE_LOG" ] && echo "$bit" >> "$FAU_FAKE_LOG"
    echo "fpga_manager fpga0: writing $(basename "$bit") to Xilinx Zynq FPGA Manager"
    echo "Time taken to load BIN is 47.000000 Milli Seconds"
    echo "BIN FILE loaded through FPGA manager successfully"
    ;;
failsecond)
    # Succeeds for the first load it sees, fails for every one after --
    # so a bootstrap-then-mode sequence gets through step 1 and dies on
    # step 2, which is the partial state worth reporting precisely.
    [ -n "$FAU_FAKE_LOG" ] && echo "$bit" >> "$FAU_FAKE_LOG"
    if [ -s "$FAU_FAKE_SEEN" ]; then
        echo "Error: User provided bitstream file doesn't exist"
        exit 1
    fi
    echo seen > "$FAU_FAKE_SEEN"
    echo "fpga_manager fpga0: writing $(basename "$bit") to Xilinx Zynq FPGA Manager"
    echo "BIN FILE loaded through FPGA manager successfully"
    ;;
silent)
    exit 0
    ;;
error)
    echo "Error: User provided bitstream file doesn't exist"
    exit 1
    ;;
esac
'''

SUDO_STUB = r'''#!/bin/sh
noninteractive=""
while [ $# -gt 0 ]; do
    case "$1" in
        -n) noninteractive=1; shift ;;
        -S) shift ;;
        -p) shift 2 ;;
        *) break ;;
    esac
done
if [ -n "$noninteractive" ] && [ -n "$FAU_FAKE_SUDO_NEEDPW" ]; then
    echo "sudo: a password is required"
    exit 1
fi
exec "$@"
'''


def _install(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP
             | stat.S_IXOTH)
    return path


class FpgaTestCase(unittest.TestCase):
    def setUp(self):
        self.bindir = tempfile.mkdtemp(prefix="fau_bin_")
        _install(self.bindir, "fpgautil", FPGAUTIL_STUB)
        _install(self.bindir, "sudo", SUDO_STUB)
        self.sudo_only = tempfile.mkdtemp(prefix="fau_bin_nofpga_")
        _install(self.sudo_only, "sudo", SUDO_STUB)

        self.staged = tempfile.mkdtemp(prefix="fau_fw_")
        self.bitstream = os.path.join(self.staged, "Radio_Top_v2_wrapper.bit.bin")
        with open(self.bitstream, "wb") as fh:
            fh.write(b"\x00" * 64)
        self.mode_bitstream = os.path.join(self.staged, "S10_dac.bit.bin")
        with open(self.mode_bitstream, "wb") as fh:
            fh.write(b"\x00" * 64)
        self.loadlog = os.path.join(self.staged, "loads.txt")
        self.seen = os.path.join(self.staged, "seen.txt")

        shell = FakeShell("login")
        shell.start()
        self.addCleanup(shell.stop)
        transport = shell.transport()
        self.session = BoardSession(transport, LineReader(transport))
        self.session.connect()
        self._path(self.bindir)
        self._env("FAU_FAKE_LOG", self.loadlog)
        self._env("FAU_FAKE_SEEN", self.seen)

        self.logged = []
        prev = report.set_sink(lambda text, is_err: self.logged.append(text))
        self.addCleanup(report.set_sink, prev)

    def _path(self, directory):
        self.session.run_ok("export PATH=%s:$PATH" % directory)

    def _env(self, name, value):
        self.session.run_ok("export %s=%s" % (name, value))

    def _loaded(self):
        """The bitstream paths fpgautil was actually handed, in order."""
        if not os.path.exists(self.loadlog):
            return []
        with open(self.loadlog, encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]


class TestSuccess(FpgaTestCase):
    def test_success_is_recognised_and_timing_parsed(self):
        result = fpga.load_bitstream(self.session, self.bitstream)
        self.assertEqual(result.bitstream, self.bitstream)
        self.assertEqual(result.millis, 47.0)
        self.assertIn(fpga.SUCCESS_MARKER, result.output)

    def test_the_pl_replacement_warning_is_emitted(self):
        # Callers need to know the AXI GPIOs are back to zero and any
        # existing /dev/mem mapping now points at different hardware.
        fpga.load_bitstream(self.session, self.bitstream)
        joined = "\n".join(self.logged)
        self.assertIn("PL was replaced", joined)


class TestFailureModes(FpgaTestCase):
    def test_silent_success_is_treated_as_failure(self):
        # THE case this module exists for: rc 0, no output, nothing loaded.
        self._env("FAU_FAKE_FPGAUTIL", "silent")
        with self.assertRaises(FpgaError) as ctx:
            fpga.load_bitstream(self.session, self.bitstream)
        msg = str(ctx.exception)
        self.assertIn("did not confirm", msg)
        self.assertIn("no output at all", msg)

    def test_explicit_error_is_reported(self):
        self._env("FAU_FAKE_FPGAUTIL", "error")
        with self.assertRaises(FpgaError) as ctx:
            fpga.load_bitstream(self.session, self.bitstream)
        self.assertIn("did not confirm", str(ctx.exception))

    def test_unreadable_path_is_caught_before_running_anything(self):
        with self.assertRaises(FpgaError) as ctx:
            fpga.load_bitstream(self.session,
                                os.path.join(self.staged, "absent.bit.bin"))
        self.assertIn("not readable", str(ctx.exception))

    def test_missing_bitstream_field_is_a_clear_message(self):
        with self.assertRaises(FpgaError) as ctx:
            fpga.load_bitstream(self.session, None)
        self.assertIn("bitstreams.json", str(ctx.exception))

    def test_fpgautil_not_installed(self):
        self._path(self.sudo_only)
        # Put a directory WITHOUT fpgautil ahead of the one with it by
        # rebuilding PATH from scratch, so the stub cannot be found.
        self.session.run_ok("PATH=%s:/usr/bin:/bin" % self.sudo_only)
        with self.assertRaises(FpgaError) as ctx:
            fpga.load_bitstream(self.session, self.bitstream)
        self.assertIn("not installed", str(ctx.exception))


class TestLoadSequence(FpgaTestCase):
    """bootstrap first, then the mode -- asserted on what reached fpgautil,
    in order, not on what the caller believes it asked for."""

    def _steps(self):
        return [("bootstrap", self.bitstream), ("tx", self.mode_bitstream)]

    def test_both_load_in_order(self):
        results = fpga.load_sequence(self.session, self._steps())
        self.assertEqual(len(results), 2)
        self.assertEqual(self._loaded(), [self.bitstream, self.mode_bitstream])

    def test_a_single_step_still_works(self):
        fpga.load_sequence(self.session, [("tx", self.mode_bitstream)])
        self.assertEqual(self._loaded(), [self.mode_bitstream])

    def test_the_pl_warning_is_emitted_once_not_per_step(self):
        # Warning twice about an intermediate PL that is superseded seconds
        # later trains people to skip the one that matters.
        fpga.load_sequence(self.session, self._steps())
        warnings = [t for t in self.logged if "PL was replaced" in t]
        self.assertEqual(len(warnings), 1)

    def test_a_failure_in_step_one_never_runs_step_two(self):
        # After a failed load the PL is in an unknown state; layering
        # another full reconfiguration on top destroys the evidence.
        self._env("FAU_FAKE_FPGAUTIL", "error")
        with self.assertRaises(FpgaError):
            fpga.load_sequence(self.session, self._steps())
        self.assertEqual(self._loaded(), [])

    def test_a_failure_in_step_two_reports_the_partial_state(self):
        # "Nothing happened" and "the board is running the base design but
        # not the mode you asked for" are different situations to walk back
        # into, so the message has to distinguish them.
        self._env("FAU_FAKE_FPGAUTIL", "failsecond")
        with self.assertRaises(FpgaError) as ctx:
            fpga.load_sequence(self.session, self._steps())
        msg = str(ctx.exception)
        self.assertIn("step 2 of 2", msg)
        self.assertIn("now running bootstrap", msg)
        self.assertIn("NOT tx", msg)
        self.assertEqual(self._loaded(), [self.bitstream, self.mode_bitstream])

    def test_step_progress_is_announced_for_a_multi_step_load(self):
        fpga.load_sequence(self.session, self._steps())
        joined = "\n".join(self.logged)
        self.assertIn("step 1/2: bootstrap", joined)
        self.assertIn("step 2/2: tx", joined)

    def test_no_step_noise_for_a_single_step(self):
        fpga.load_sequence(self.session, [("tx", self.mode_bitstream)])
        self.assertFalse([t for t in self.logged if "step 1/1" in t])


class TestSudoPassword(FpgaTestCase):
    def test_retries_with_the_password_on_stdin(self):
        # `sudo -n` refuses; the retry feeds the login password in and the
        # load goes through. Without the retry this would be a hard failure
        # on any board that does not have passwordless sudo.
        self._env("FAU_FAKE_SUDO_NEEDPW", "1")
        result = fpga.load_bitstream(self.session, self.bitstream,
                                     password="1234")
        self.assertIn(fpga.SUCCESS_MARKER, result.output)

    def test_no_password_available_says_so(self):
        self._env("FAU_FAKE_SUDO_NEEDPW", "1")
        with self.assertRaises(FpgaError) as ctx:
            fpga.load_bitstream(self.session, self.bitstream, password=None)
        self.assertIn("passwordless sudo", str(ctx.exception))


class TestExtensionWarning(FpgaTestCase):
    def test_raw_bit_warns_but_proceeds(self):
        # A raw Vivado .bit is a different file from the bootgen-processed
        # .bit.bin the FPGA-manager path wants -- worth flagging, but only
        # the board can actually settle it.
        raw = os.path.join(self.staged, "design.bit")
        with open(raw, "wb") as fh:
            fh.write(b"\x00" * 64)
        fpga.load_bitstream(self.session, raw)
        self.assertTrue(
            any(".bit.bin" in line and "WARNING" in line
                for line in self.logged), self.logged)


if __name__ == "__main__":
    unittest.main()
