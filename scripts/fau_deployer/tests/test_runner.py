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
