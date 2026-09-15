#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Run a deployed flowgraph on the board's console, stream its output, and
stop it safely.

No board-side run wrapper, and no `===FAU-HALTED===` sentinel (which the
plan doc originally proposed). Two reasons, both load-bearing:

- **The generated flowgraph already tears down correctly.** grcc emits
    def sig_handler(sig=None, frame=None):
        tb.stop(); tb.wait(); sys.exit(0)
    signal.signal(signal.SIGINT, sig_handler)
  and tb.wait() returning is what runs the block destructors, which carry
  the required DMACR.RS clear -> poll DMASR.Halted -> fabric reset. Nothing
  needs to be added around it.
- **The shell's exit status is stronger evidence than a sentinel printed
  from inside Python.** A `print("halted")` before the destructors run can
  be emitted and THEN wedge; `echo FAU-RC-<nonce>:$?` only appears once the
  process is genuinely reaped, destructors included. So the completion
  marker is the same FAU-RC mechanism core/session.py's run() uses, just
  nonce-tagged so flowgraph stdout cannot forge it.

Foreground only, deliberately. Backgrounding with `&` would move the
flowgraph out of the console's foreground process group, so Ctrl-C would no
longer reach it and stopping it would mean hunting a PID and sending
signals by hand -- strictly more ways to orphan a live DMA burst.

**The flowgraph runs under sudo.** fau_source/fau_sink open /dev/mem and
take a lock under /run/lock, so a non-root run dies in the constructor:

    RuntimeError: fau_modem: cannot open lock file
    /run/lock/fau-dma-40400000.lock: Permission denied

`sudo -n` (non-interactive), never `sudo -S` with the password piped in --
see build_command() for why that would silently break the run outright.

**Ctrl-C still reaches the flowgraph through the sudo hop**, in either of
sudo's execution modes, which matters because that signal is what runs the
DMA teardown. From sudo(8), "Signal handling": SIGINT is "only relayed when
the command is being run in a new pty or when the signal was sent by a user
process, not the kernel. This prevents the command from receiving SIGINT
twice each time the user enters control-C." So:

- **No new pty**: the command shares our tty and foreground process group,
  so the kernel's line discipline delivers SIGINT to it directly; sudo
  deliberately does not relay, which would double it.
- **New pty (use_pty)**: the command is on a different pty and would not
  see the kernel's signal, so sudo relays it.

Either way exactly one SIGINT arrives. Verified as documented behaviour, not
on hardware; tests/test_runner.py::TestRealSudo checks it for real wherever
passwordless sudo exists, and skips rather than faking it elsewhere -- a
stub sudo would only prove the stub relays signals.

**NOTHING MAY BE WRITTEN TO THE CONSOLE WHILE A FLOWGRAPH IS RUNNING.**
grcc's no_gui template blocks in `input('Press Enter to quit: ')` after
tb.start(), so the flowgraph's stdin IS the serial console: a single stray
newline ends the run, and an 0x04 would end it too (the template catches
EOFError and falls through to its own tb.stop()/tb.wait()). Ctrl-C is
unaffected -- the line discipline turns 0x03 into SIGINT rather than
handing it over as data -- which is another reason it, and not a newline,
is the stop mechanism: it means the same thing whether or not a particular
flowgraph template happens to be sitting in input().

Two consequences worth knowing: the "Press Enter to quit: " prompt has no
trailing newline, so it stays in the LineReader's tail and is never
reported as an output line; and a flowgraph exiting on its own via that
prompt is indistinguishable here from one that ran to completion, which is
fine -- both produce a real exit status.
"""

import dataclasses
import re
import shlex
import time

from . import report
from .protocol import new_nonce

POLL_INTERVAL = 0.25  # how long each read tick waits for output

# sudo declining to run without a password, across versions/configurations.
# Worth recognizing specifically: the generic failure ("exit status 1") sends
# an operator looking at the flowgraph, when the fix is on the board.
RE_SUDO_DENIED = re.compile(
    r"sudo:.*(password is required|no tty present|a terminal is required)",
    re.IGNORECASE)

# Long by default: tb.wait() has to drain the DMA ring and run the block
# destructors' halt-and-reset sequence, which is not instant, and cutting it
# short here would achieve nothing anyway -- there is no second, harder
# signal this module is willing to send. See terminate()/RunResult.wedged.
TERMINATE_TIMEOUT_DEFAULT = 30.0

# Time allowed for the shell's prompt to reappear after the FAU-RC line.
PROMPT_SETTLE_TIMEOUT = 5.0


class RunError(RuntimeError):
    """The run could not be started. Message is meant to be shown as-is."""


@dataclasses.dataclass
class RunResult:
    rc: int = None
    """The flowgraph's exit status, or None if it never reported one."""

    terminated: bool = False
    """True if we sent Ctrl-C (i.e. the operator stopped it, rather than the
    flowgraph exiting on its own)."""

    sudo_denied: bool = False
    """True if sudo refused for want of a password. The flowgraph never
    started, so the board is in no danger -- but nothing ran, which an exit
    status alone does not make obvious."""

    wedged: bool = False
    """True if Ctrl-C was sent and no exit status ever came back. THE BOARD
    IS NOT KNOWN TO BE SAFE in this state: a flowgraph that never completed
    tb.wait() may still have DMA live. Front-ends must surface this as a
    distinct, sticky failure -- not as a generic timeout -- and must not
    offer to escalate to SIGKILL (see terminate())."""

    prompt_returned: bool = False
    """True if a shell prompt reappeared after the exit status, i.e. the
    console is usable for further commands."""

    elapsed: float = 0.0
    lines: int = 0

    @property
    def clean(self):
        """Ran and stopped with the console handed back, whether it exited
        on its own or we stopped it. rc != 0 is still 'clean' in this sense
        -- a flowgraph that raised is a flowgraph problem, not a console or
        DMA problem."""
        return self.rc is not None and self.prompt_returned and not self.wedged


def build_command(dest, main_name, params=(), python="python3", nonce=None,
                 sudo=True):
    """The one line sent to the console, and the nonce its completion marker
    is tagged with. Returns (command_text, nonce).

    **sudo -n, and deliberately not `sudo -S` with the password on stdin.**
    The -S form works fine for a one-shot command (core/fpga.py uses it for
    fpgautil) but would break a run outright: it makes the flowgraph's stdin
    the password pipe, which is at EOF the moment the password is read.
    grcc's no_gui template blocks in `input('Press Enter to quit: ')`, so an
    EOF there raises EOFError, the template catches it and runs straight
    into its own tb.stop()/tb.wait() -- the flowgraph would appear to start
    and then exit instantly for no visible reason. -n instead fails fast
    with a message run() recognizes and explains.

    `-u` unbuffers the flowgraph's stdout. A tty is line-buffered already,
    so this changes nothing in the normal case -- it matters when the
    flowgraph's own output is redirected or when it writes without newlines,
    where block buffering would hold log lines back in 4 KiB batches and
    make a live log pane useless.

    `cd ... && python3 ...; echo` rather than `&&` throughout on purpose: if
    the cd fails, the `&&` short-circuits and the echo still reports cd's
    nonzero status, so a bad --dest is a reported failure instead of a
    silent hang waiting for a marker that is never coming.
    """
    nonce = nonce or new_nonce()
    argv = [python, "-u", "./" + main_name] + list(params)
    # sudo does not change directory, so the preceding `cd` still decides
    # what "./" means.
    prefix = "sudo -n " if sudo else ""
    cmd = "cd %s && %s%s; echo \"FAU-RC-%s:$?\"" % (
        shlex.quote(dest), prefix,
        " ".join(shlex.quote(a) for a in argv), nonce)
    return cmd, nonce


class Runner:
    def __init__(self, session, transport, reader, dest, main_name,
                params=(), python="python3",
                terminate_timeout=TERMINATE_TIMEOUT_DEFAULT, nonce=None,
                sudo=True):
        self._session = session
        self._transport = transport
        self._reader = reader
        self._dest = dest
        self._main = main_name
        self._params = list(params)
        self._python = python
        self._terminate_timeout = terminate_timeout
        self._cmd, self._nonce = build_command(
            dest, main_name, self._params, python, nonce, sudo=sudo)
        self._rc_re = re.compile(r"FAU-RC-%s:(\d+)" % re.escape(self._nonce))
        # The console's echo of what we sent contains the marker with $?
        # STILL UNEXPANDED, which the real status line never does. Matching
        # on that rather than on the whole command line is what makes the
        # echo recognizable when the console has corrupted a character
        # somewhere else in it -- seen for real, as
        # "./test_chirp.p.py", which then got logged as board output.
        self._echo_marker = "FAU-RC-%s:$?" % self._nonce
        self._interrupt_sent = False

    @property
    def command(self):
        return self._cmd

    def terminate(self):
        """Send Ctrl-C. Idempotent; safe to call from another thread than
        the one in run() (it is one write of one byte, and the read side is
        untouched).

        This is the ONLY stop mechanism, on purpose. There is no SIGTERM
        follow-up and no `kill -9` escalation, now or later: killing a
        process that is holding a live DMA burst orphans an AXI transaction
        on the PS HP port, and per the root CLAUDE.md roughly 30 of those
        wedge the board until it is power-cycled. A stop that does not
        complete is therefore reported (RunResult.wedged) rather than
        forced, because forcing it is the one action guaranteed to make the
        situation worse.
        """
        if self._interrupt_sent:
            return
        self._interrupt_sent = True
        self._transport.write(b"\x03")

    def run(self, on_line=None, should_stop=None, poll=POLL_INTERVAL):
        """Start the flowgraph and block until it stops. Returns a
        RunResult; raises RunError only for a failure to start.

        `on_line(text)` receives each line of flowgraph output.
        `should_stop()` is polled every `poll` seconds; the first truthy
        result sends Ctrl-C once and then keeps reading, because the point
        of stopping is to observe the clean shutdown, not to walk away
        during it.
        """
        t0 = time.monotonic()
        self._transport.write((self._cmd + "\r").encode("utf-8"))

        result = RunResult()
        deadline = None  # set once Ctrl-C has been sent

        while True:
            for line in self._reader.lines(poll):
                if line.strip() == self._cmd or self._echo_marker in line:
                    continue  # the console's own echo of what we sent
                if RE_SUDO_DENIED.search(line):
                    result.sudo_denied = True
                m = self._rc_re.search(line)
                if m:
                    result.rc = int(m.group(1))
                    break
                result.lines += 1
                if on_line is not None:
                    on_line(line)
            if result.rc is not None:
                break

            if (should_stop is not None and should_stop()
                    and not self._interrupt_sent):
                report.say("run", "stopping the flowgraph (Ctrl-C) -- "
                                  "waiting for it to halt cleanly")
                self.terminate()
                result.terminated = True
                deadline = time.monotonic() + self._terminate_timeout

            if deadline is not None and time.monotonic() > deadline:
                result.wedged = True
                result.elapsed = time.monotonic() - t0
                report.error(
                    "the flowgraph did not report an exit status within "
                    "%.0fs of Ctrl-C. It may still be running with DMA "
                    "live. NOT escalating to a kill -- that would orphan "
                    "the AXI burst and can wedge the board until a power "
                    "cycle. Check the console by hand."
                    % self._terminate_timeout)
                return result

        # The exit status proves the process is reaped; the prompt that
        # follows is printed asynchronously after it, and has to be consumed
        # here or it glues onto the front of the next command's output --
        # the same discipline (and the same past bug) as session.run().
        result.prompt_returned = self._session.wait_for_idle_prompt(
            PROMPT_SETTLE_TIMEOUT)
        result.elapsed = time.monotonic() - t0
        if result.sudo_denied:
            report.error(
                "sudo on the board refused to run the flowgraph without a "
                "password, so nothing started. The blocks open /dev/mem and "
                "lock under /run/lock, so the run does need root: grant the "
                "login user passwordless sudo for python3, or deploy to a "
                "board whose login is already root. The password cannot be "
                "piped in -- that would take over the flowgraph's stdin.")
        if not result.prompt_returned:
            report.warn(
                "the flowgraph exited (status %s) but no shell prompt came "
                "back within %.0fs -- the console may need re-grounding "
                "before the next command" % (result.rc, PROMPT_SETTLE_TIMEOUT))
        return result
