#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""core/runner.py: running a flowgraph on the console and stopping it.

Against a real pty and a real /bin/sh, with real child processes, because
every interesting case here is about tty signal delivery: whether 0x03
reaches the foreground process as a SIGINT, and what happens when the thing
holding the console declines to die. A stand-in stream would prove none of
it.

The stand-in "flowgraphs" below mirror the shape grcc actually emits -- a
SIGINT handler that stops and waits, then exits 0 -- so a clean stop looks
here exactly as it does on the board.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from ..core import runner
from ..core.runner import Runner
from ..core.session import BoardSession
from ..core.transport import LineReader
from .fake_shell import FakeShell

# Mirrors the generated flowgraph: SIGINT -> tb.stop()/tb.wait() -> exit 0.
FLOWGRAPH = '''\
import signal, sys, time

def sig_handler(sig=None, frame=None):
    print("HALTED", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, sig_handler)
print("STARTED", flush=True)
while True:
    print("tick", flush=True)
    time.sleep(0.2)
'''

# Ignores SIGINT: stands in for a flowgraph wedged in a block destructor.
# Self-terminates so a failed test cannot leave a process holding the pty.
STUBBORN = '''\
import signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
print("STARTED", flush=True)
time.sleep(20)
'''

FAST = '''\
print("one"); print("two"); print("three")
'''

FAIL = '''\
import sys
print("about to fail")
sys.exit(3)
'''

ARGV = '''\
import sys
for a in sys.argv[1:]:
    print("ARG:%s" % a)
'''

# Reports the effective uid, so "did sudo actually take" is checked rather
# than assumed from the command text.
WHOAMI = '''\
import os
print("EUID:%d" % os.geteuid())
'''


def _passwordless_sudo():
    """True only if `sudo -n` actually runs something. Used to SKIP the
    real-sudo tests rather than fake them: a stub cannot reproduce sudo's
    signal relay, which is the property those tests exist to check."""
    if shutil.which("sudo") is None:
        return False
    try:
        return subprocess.run(["sudo", "-n", "true"], timeout=10,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


HAVE_SUDO = _passwordless_sudo()


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="fau_run_")
        shell = FakeShell("login")
        shell.start()
        self.addCleanup(shell.stop)
        self.transport = shell.transport()
        self.reader = LineReader(self.transport)
        self.session = BoardSession(self.transport, self.reader)
        self.session.connect()

    def _script(self, text, name="fg.py"):
        path = os.path.join(self.dest, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return name

    def _runner(self, main, **kwargs):
        # sudo off unless a test asks for it: these run against a real
        # /bin/sh on this machine, not a board.
        kwargs.setdefault("sudo", False)
        return Runner(self.session, self.transport, self.reader, self.dest,
                      main, **kwargs)


class TestBuildCommand(unittest.TestCase):
    def test_sudo_is_on_by_default(self):
        # The blocks open /dev/mem and lock under /run/lock; without root
        # the constructor raises before any DMA is touched.
        cmd, _ = runner.build_command("/d", "fg.py")
        self.assertIn("sudo -n python3 -u ./fg.py", cmd)

    def test_sudo_can_be_turned_off(self):
        cmd, _ = runner.build_command("/d", "fg.py", sudo=False)
        self.assertNotIn("sudo", cmd)
        self.assertIn("python3 -u ./fg.py", cmd)

    def test_sudo_is_non_interactive_never_stdin(self):
        # -S with a piped password would make the flowgraph's stdin the
        # password pipe, which is at EOF -- grcc's template catches the
        # resulting EOFError from input() and exits immediately, so the run
        # would appear to start and instantly stop.
        cmd, _ = runner.build_command("/d", "fg.py")
        self.assertIn("sudo -n", cmd)
        self.assertNotIn("-S", cmd)
        self.assertNotIn("|", cmd)

    def test_cd_still_decides_what_dot_slash_means(self):
        # sudo does not change directory, so the cd has to come first.
        cmd, _ = runner.build_command("/some/dir", "fg.py")
        self.assertLess(cmd.index("cd /some/dir"), cmd.index("sudo"))

    def test_params_are_quoted_per_token(self):
        cmd, nonce = runner.build_command(
            "/home/petalinux/fau", "fg.py",
            ["--label", "two words", "--glob", "*.bin"], nonce="abc123")
        self.assertIn("'two words'", cmd)
        self.assertIn("'*.bin'", cmd)
        self.assertIn('echo "FAU-RC-abc123:$?"', cmd)
        self.assertEqual(nonce, "abc123")

    def test_dest_is_quoted(self):
        cmd, _ = runner.build_command("/tmp/dir with spaces", "fg.py")
        self.assertIn("cd '/tmp/dir with spaces'", cmd)

    def test_unbuffered_and_relative_to_dest(self):
        cmd, _ = runner.build_command("/d", "fg.py")
        self.assertIn("python3 -u ./fg.py", cmd)

    def test_cd_failure_still_reports_a_status(self):
        # `cd X && prog; echo RC:$?` -- the `;` before the echo is what
        # makes a bad dest a reported failure instead of a silent wait for
        # a marker that never comes.
        cmd, _ = runner.build_command("/d", "fg.py")
        self.assertIn("; echo", cmd)


class TestNormalExit(RunnerTestCase):
    def test_output_is_streamed_and_status_reported(self):
        main = self._script(FAST)
        seen = []
        result = self._runner(main).run(on_line=seen.append)
        self.assertEqual(result.rc, 0)
        self.assertFalse(result.terminated)
        self.assertFalse(result.wedged)
        self.assertTrue(result.prompt_returned)
        self.assertEqual([s for s in seen if s.strip()],
                         ["one", "two", "three"])
        self.assertTrue(result.clean)

    def test_nonzero_status_is_reported_and_still_clean(self):
        # A flowgraph that raised is a flowgraph problem, not a console or
        # DMA problem -- the console came back, so this is 'clean'.
        main = self._script(FAIL)
        result = self._runner(main).run()
        self.assertEqual(result.rc, 3)
        self.assertTrue(result.clean)

    def test_params_reach_the_flowgraph_intact(self):
        main = self._script(ARGV)
        seen = []
        result = self._runner(
            main, params=["--label", "two words", "--price", "$5"]
        ).run(on_line=seen.append)
        self.assertEqual(result.rc, 0)
        self.assertIn("ARG:two words", seen)
        self.assertIn("ARG:$5", seen)  # not expanded by the board's shell

    def test_console_is_usable_afterwards(self):
        main = self._script(FAST)
        self._runner(main).run()
        rc, out = self.session.run("echo after-run")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "after-run")


class TestEchoFiltering(RunnerTestCase):
    def test_the_echoed_command_is_not_reported_as_output(self):
        main = self._script(FAST)
        seen = []
        self._runner(main).run(on_line=seen.append)
        self.assertFalse([s for s in seen if "FAU-RC-" in s], seen)

    def test_a_corrupted_echo_is_still_recognised(self):
        # Seen for real on a serial console: the echo came back as
        # "./test_chirp.p.py", so an exact match against the sent command
        # failed and the whole line was logged as board output. The
        # unexpanded "$?" in the marker is what still identifies it.
        r = self._runner(self._script(FAST))
        corrupted = r.command.replace("./fg.py", "./f.g.py")
        self.assertIn(r._echo_marker, corrupted)
        self.assertNotEqual(corrupted, r.command)


class TestSudoDenied(RunnerTestCase):
    def test_a_password_prompt_refusal_is_called_out(self):
        # Stands in for `sudo -n` refusing on a board without NOPASSWD.
        # The generic "exit status 1" sends an operator looking at the
        # flowgraph when the fix is on the board.
        main = self._script(
            'import sys\n'
            'print("sudo: a password is required", file=sys.stderr)\n'
            'sys.exit(1)\n')
        seen = []
        result = self._runner(main).run(on_line=seen.append)
        self.assertTrue(result.sudo_denied)
        self.assertEqual(result.rc, 1)

    def test_a_normal_failure_is_not_mistaken_for_it(self):
        result = self._runner(self._script(FAIL)).run()
        self.assertFalse(result.sudo_denied)


@unittest.skipUnless(HAVE_SUDO, "no passwordless sudo on this machine")
class TestRealSudo(RunnerTestCase):
    """The one property a stub cannot check: that Ctrl-C still reaches the
    flowgraph through the extra sudo hop, and that its SIGINT handler runs
    to completion.

    This matters more than the usual signal test. That handler is what calls
    tb.stop()/tb.wait(), which is what runs the blocks' DMA halt sequence --
    if sudo swallowed SIGINT, Terminate would look like it worked while the
    DMA stayed live. Skipped rather than faked wherever passwordless sudo
    is unavailable, because a fake sudo would only prove the fake relays
    signals.
    """

    def test_sudo_actually_elevates(self):
        main = self._script(WHOAMI)
        seen = []
        result = self._runner(main, sudo=True).run(on_line=seen.append)
        self.assertEqual(result.rc, 0)
        self.assertIn("EUID:0", seen)

    def test_ctrl_c_reaches_the_flowgraph_through_sudo(self):
        main = self._script(FLOWGRAPH)
        seen = []
        result = self._runner(main, sudo=True).run(
            on_line=seen.append,
            should_stop=lambda: any("tick" in s for s in seen))
        self.assertTrue(result.terminated)
        self.assertFalse(result.wedged)
        self.assertTrue(any("HALTED" in s for s in seen), seen)
        self.assertEqual(result.rc, 0)

    def test_the_console_is_usable_after_a_sudo_run(self):
        main = self._script(FAST)
        self._runner(main, sudo=True).run()
        rc, out = self.session.run("echo after-sudo-run")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "after-sudo-run")


class TestTerminate(RunnerTestCase):
    def test_ctrl_c_stops_it_and_the_halt_is_confirmed(self):
        main = self._script(FLOWGRAPH)
        seen = []

        def on_line(line):
            seen.append(line)

        # Stop once it has actually started producing output, i.e. the same
        # moment an operator would press Terminate.
        result = self._runner(main).run(
            on_line=on_line,
            should_stop=lambda: any("tick" in s for s in seen))
        self.assertTrue(result.terminated)
        self.assertFalse(result.wedged)
        self.assertEqual(result.rc, 0)
        self.assertTrue(result.prompt_returned)
        # The stand-in's own halt path ran -- the equivalent of tb.wait()
        # returning, which is what runs the blocks' DMA teardown.
        self.assertTrue(any("HALTED" in s for s in seen), seen)

    def test_console_is_usable_after_a_terminate(self):
        main = self._script(FLOWGRAPH)
        seen = []
        self._runner(main).run(
            on_line=seen.append,
            should_stop=lambda: any("tick" in s for s in seen))
        rc, out = self.session.run("echo after-terminate")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "after-terminate")

    def test_a_flowgraph_that_ignores_ctrl_c_is_reported_wedged(self):
        # No escalation: no SIGTERM, no kill. Killing a process holding a
        # live DMA burst orphans an AXI transaction, and enough of those
        # wedge the board until a power cycle -- so this is reported as a
        # distinct unsafe state instead of forced.
        main = self._script(STUBBORN)
        seen = []
        result = self._runner(main, terminate_timeout=2.0).run(
            on_line=seen.append,
            should_stop=lambda: any("STARTED" in s for s in seen))
        self.assertTrue(result.terminated)
        self.assertTrue(result.wedged)
        self.assertIsNone(result.rc)
        self.assertFalse(result.clean)

    def test_terminate_is_idempotent(self):
        r = self._runner(self._script(FAST))
        r.terminate()
        r.terminate()  # must not raise or double-send


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------
# The live control channel.
#
# Run against the same real pty and real /bin/sh as everything else here,
# with the REAL board/fau_ctl.py as the flowgraph's dispatcher, because the
# properties worth checking are all about the console being one shared,
# echoing, lossy channel: that a SET reaches a foreground process's stdin
# at all, that its echo does not get logged as flowgraph output, and that
# a reply comes back tagged with this run's nonce and no other.
# ---------------------------------------------------------------------

BOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "board")

# Stands in for a generated flowgraph with the injected snippet: it starts
# the real fau_ctl against a stub top block and then blocks the way
# `run_options: run` makes the real one block in tb.wait().
CTL_FLOWGRAPH = '''\
import signal, sys, time
# No sys.path games: fau_ctl is imported from the deploy directory, the
# way the board does it. That is load-bearing, not incidental --
# load_controls() looks for ui_spec.json beside fau_ctl.py's own file, so
# importing the repo's copy instead of the deployed one finds no spec and
# silently starts no control channel. Which is exactly what this test hit.
import fau_ctl

class TB:
    def __init__(self):
        self.gain = 0.5
    def set_gain(self, value):
        self.gain = value
        print("gain now %r" % (value,), flush=True)
    def set_boom(self, value):
        raise ValueError("no")
    def stop(self):
        print("STOPPING", flush=True)
        global running
        running = False

tb = TB()
running = True
fau_ctl.start(tb)
print("STARTED", flush=True)
while running:
    time.sleep(0.05)
print("HALTED", flush=True)
'''

CTL_SPEC = {
    "version": 1,
    "flowgraph": "ctl",
    "controls": [
        {"id": "gain", "kind": "range", "label": "Gain", "dtype": "float",
         "default": 0.5, "free_entry": False, "start": 0.0, "stop": 1.0,
         "step": 0.01, "widget": "slider"},
        {"id": "boom", "kind": "entry", "label": "Boom", "dtype": "int",
         "default": 0, "free_entry": False},
    ],
}


class TestControlChannel(RunnerTestCase):
    def setUp(self):
        RunnerTestCase.setUp(self)
        import json
        import shutil as _shutil
        _shutil.copy(os.path.join(BOARD_DIR, "fau_ctl.py"), self.dest)
        with open(os.path.join(self.dest, "ui_spec.json"), "w") as fh:
            json.dump(CTL_SPEC, fh)
        self.main = self._script(CTL_FLOWGRAPH, "ctl_fg.py")
        self.replies = []
        self.lines = []

    def _run(self, drive, ids=("gain", "boom")):
        """Start the flowgraph, call `drive(runner)` once it is READY, then
        stop it. Returns the RunResult."""
        r = self._runner(self.main, control_ids=ids,
                         on_control=self.replies.append)
        state = {"driven": False}

        def on_line(line):
            self.lines.append(line)
            if "STARTED" in line and not state["driven"]:
                state["driven"] = True
                drive(r)
                state["at"] = time.monotonic()

        def should_stop():
            # Give the replies a moment to come back before Ctrl-C.
            return state.get("at") and time.monotonic() - state["at"] > 1.0

        return r.run(on_line=on_line, should_stop=should_stop)

    def test_a_set_reaches_the_flowgraph_and_is_acknowledged(self):
        result = self._run(lambda r: r.set_control("gain", 0.25))
        self.assertTrue(any("gain now 0.25" in line for line in self.lines),
                        self.lines)
        acks = [x for x in self.replies if x.ok]
        self.assertTrue(acks, self.replies)
        self.assertEqual((acks[-1].id, acks[-1].value), ("gain", 0.25))
        self.assertEqual(result.control_acks, len(acks))

    def test_the_board_announces_ready(self):
        self._run(lambda r: None)
        self.assertTrue(any(x.kind == "READY" for x in self.replies),
                        self.replies)

    def test_replies_are_kept_out_of_the_flowgraph_output(self):
        self._run(lambda r: r.set_control("gain", 0.75))
        self.assertFalse([line for line in self.lines if "FAU-CTL" in line],
                         self.lines)

    def test_the_consoles_echo_of_our_own_line_is_not_logged(self):
        # The board's tty echoes everything written to it; without the echo
        # filter a dragged slider fills the log with its own SET lines.
        self._run(lambda r: r.set_control("gain", 0.75))
        self.assertFalse([line for line in self.lines
                          if line.strip().startswith("SET ")], self.lines)

    def test_a_setter_that_raises_comes_back_as_an_error_not_a_crash(self):
        result = self._run(lambda r: r.set_control("boom", 1))
        errors = [x for x in self.replies if x.kind == "ERR"]
        self.assertTrue(errors, self.replies)
        self.assertEqual(errors[-1].id, "boom")
        self.assertEqual(result.control_errors, len(errors))
        # ...and the run still stops cleanly afterwards.
        self.assertEqual(result.rc, 0)

    def test_pulse_is_validated_on_the_board_not_only_here(self):
        result = self._run(lambda r: r.pulse_control("gain", 20))
        # gain has no pressed/released, so the board refuses it -- which is
        # the check: PULSE is validated there, not only here.
        self.assertTrue(any(x.kind == "ERR" and "use SET" in x.text
                            for x in self.replies), self.replies)
        self.assertEqual(result.rc, 0)

    def test_control_writes_are_refused_before_the_run_starts(self):
        r = self._runner(self.main, control_ids=("gain",))
        with self.assertRaises(runner.RunError):
            r.set_control("gain", 1.0)

    def test_control_writes_are_refused_after_the_run_ends(self):
        r = self._runner(self._script(FAST, "fast.py"), control_ids=("gain",))
        r.run()
        with self.assertRaises(runner.RunError):
            r.set_control("gain", 1.0)

    def test_an_id_not_in_the_payload_is_refused_before_anything_is_written(self):
        # The gate: the deployer will not put a line on the console for an
        # id the deployed payload's ui_spec.json does not declare, because
        # such a line could be one that ends the run instead.
        def drive(r):
            with self.assertRaises(runner.RunError):
                r.set_control("nonexistent", 1)
        self._run(drive)

    def test_a_run_without_controls_is_not_controllable(self):
        r = self._runner(self._script(FAST, "plain.py"))
        self.assertFalse(r.controllable)
        self.assertNotIn(runner.NONCE_FILENAME, r.command)

    def test_a_run_with_controls_stamps_the_nonce_file(self):
        r = self._runner(self.main, control_ids=("gain",))
        self.assertTrue(r.controllable)
        self.assertIn(runner.NONCE_FILENAME, r.command)
        self.assertIn(r.nonce, r.command)

    def test_the_nonce_file_is_written_where_fau_ctl_reads_it(self):
        self._run(lambda r: r.set_control("gain", 0.5))
        path = os.path.join(self.dest, runner.NONCE_FILENAME)
        self.assertTrue(os.path.isfile(path))

    def test_a_reply_tagged_with_another_nonce_is_ignored(self):
        # Flowgraph stdout cannot forge a reply for this run.
        r = self._runner(self.main, control_ids=("gain",),
                         on_control=self.replies.append)
        result = runner.RunResult()
        self.assertFalse(
            r._route_control("FAU-CTL-zzzzzz OK gain 9.0", result))
        self.assertEqual(self.replies, [])
        self.assertEqual(result.control_acks, 0)
