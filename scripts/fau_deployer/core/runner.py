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
  and it is `tb.stop()` that carries the required DMACR.RS clear -> poll
  DMASR.Halted -> reset: the teardown lives in the blocks' `stop()`
  overrides (fau_sink_impl.cc:421, fau_source_impl.cc:450), which
  top_block.stop() calls. Their **destructors are empty**
  (`~fau_sink_impl() {}`) -- an earlier version of this docstring credited
  them, which matters because it is the reason nothing needs to be added
  around the generated code. Nothing does.
- **The shell's exit status is stronger evidence than a sentinel printed
  from inside Python.** A `print("halted")` before stop() has finished can
  be emitted and THEN wedge; `echo FAU-RC-<nonce>:$?` only appears once the
  process is genuinely reaped. So the completion marker is the same FAU-RC
  mechanism core/session.py's run() uses, just nonce-tagged so flowgraph
  stdout cannot forge it.

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

**The one exception, and why it is safe: the control channel.** A payload
the Process phase built has `run_options: run` forced into it, so it has
no `input()` at all -- it goes straight to `tb.wait()` and its stdin is
free. Such a payload also carries a `ui_spec.json`, and set_control() will
only write a line naming an id that file declares. So the permission comes
from the payload, not from the run state: deploy a hand-written .py and
`controllable` is False, no panel appears and no write is possible. The
prohibition above still holds in full for every other byte.
"""

import dataclasses
import re
import shlex
import time

from . import controls as controls_mod
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

    control_acks: int = 0
    """FAU-CTL OK replies seen. Not an error count's mirror image: a
    control whose setter raised still comes back, as an ERR."""

    control_errors: int = 0
    """FAU-CTL ERR replies seen. Non-zero does not endanger the board --
    fau_ctl catches everything out of a setter -- but it means some
    widget did not do what the operator asked."""

    control_ready: bool = False
    """True once the board announced its control channel. A run with a
    spec that never goes ready means fau_ctl did not start: the usual
    cause is a payload missing fau_ctl.py or ui_spec.json."""

    @property
    def clean(self):
        """Ran and stopped with the console handed back, whether it exited
        on its own or we stopped it. rc != 0 is still 'clean' in this sense
        -- a flowgraph that raised is a flowgraph problem, not a console or
        DMA problem."""
        return self.rc is not None and self.prompt_returned and not self.wedged


# The board-side control dispatcher reads the run nonce out of this file,
# written by the run command itself. See NONCE_FILENAME in
# board/fau_ctl.py for why it is a file and not an environment variable.
NONCE_FILENAME = ".fau_ctl_nonce"


def build_command(dest, main_name, params=(), python="python3", nonce=None,
                 sudo=True, controls=False):
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
    # Only when there is a control channel, so a run without one produces
    # byte-for-byte the command it always did. Chained with && like the cd:
    # if the nonce cannot be written the run does not start, and the echo
    # still reports a status, rather than the flowgraph coming up with a
    # control channel whose replies nothing can match.
    stamp = ""
    if controls:
        stamp = "printf %s > %s && " % (
            shlex.quote(nonce), shlex.quote(NONCE_FILENAME))
    cmd = "cd %s && %s%s%s; echo \"FAU-RC-%s:$?\"" % (
        shlex.quote(dest), stamp, prefix,
        " ".join(shlex.quote(a) for a in argv), nonce)
    return cmd, nonce


class Runner:
    def __init__(self, session, transport, reader, dest, main_name,
                params=(), python="python3",
                terminate_timeout=TERMINATE_TIMEOUT_DEFAULT, nonce=None,
                sudo=True, control_ids=(), on_control=None):
        self._session = session
        self._transport = transport
        self._reader = reader
        self._dest = dest
        self._main = main_name
        self._params = list(params)
        self._python = python
        self._terminate_timeout = terminate_timeout
        # The ids the payload's ui_spec.json declared. Empty means this run
        # has NO control channel, and that is the gate on every console
        # write below -- see set_control().
        self._control_ids = frozenset(control_ids)
        self._on_control = on_control
        self._cmd, self._nonce = build_command(
            dest, main_name, self._params, python, nonce, sudo=sudo,
            controls=bool(self._control_ids))
        self._rc_re = re.compile(r"FAU-RC-%s:(\d+)" % re.escape(self._nonce))
        # The console's echo of what we sent contains the marker with $?
        # STILL UNEXPANDED, which the real status line never does. Matching
        # on that rather than on the whole command line is what makes the
        # echo recognizable when the console has corrupted a character
        # somewhere else in it -- seen for real, as
        # "./test_chirp.p.py", which then got logged as board output.
        self._echo_marker = "FAU-RC-%s:$?" % self._nonce
        self._interrupt_sent = False
        self._running = False

    @property
    def nonce(self):
        return self._nonce

    @property
    def controllable(self):
        """True if this run carries a control channel at all.

        Front-ends gate their control panel on this rather than on the
        run state alone. A hand-written .py -- which the tool still
        accepts -- has grcc's `input('Press Enter to quit: ')` in it, so
        the first SET line would END THE RUN rather than set anything.
        No spec in the payload, no panel, no writes.
        """
        return bool(self._control_ids)

    def set_control(self, cid, value):
        """Send one SET line. Safe to call from the UI thread.

        Writing to the console while a flowgraph runs is normally
        forbidden (see the module docstring), and this is the one gated
        exception: it is permitted only for an id the payload's
        ui_spec.json declared, which can only exist on a payload the
        Process phase built, which is the only kind that forced
        `run_options: run` and so has no `input()` waiting to eat the
        line.

        Like terminate(), one write of one complete line, never
        interleaved mid-line.
        """
        return self._write_control(controls_mod.wire_set(cid, value), cid)

    def pulse_control(self, cid, ms):
        """Send one PULSE line: press, hold `ms` on the board, release.

        The hold is timed on the board so the edge width does not depend
        on the console's latency, which matters for a button wired to a
        reset or a command burst.
        """
        return self._write_control(controls_mod.wire_pulse(cid, ms), cid)

    def _write_control(self, line, cid):
        if cid not in self._control_ids:
            raise RunError(
                "%r is not a control of the running flowgraph. The deployer "
                "will only write to the console for an id the deployed "
                "payload's ui_spec.json declares -- anything else could be "
                "a line that ends the run instead of setting a value." % cid)
        if not self._running:
            raise RunError(
                "nothing is running, so there is nothing to control.")
        self._transport.write((line + "\r").encode("utf-8"))
        return line

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

    def _route_control(self, line, result):
        """Take a FAU-CTL reply (or our own echoed SET) out of the output
        stream. True if the line was consumed.

        Both halves matter for a readable log. The replies are bookkeeping
        that belongs against a widget, not interleaved with flowgraph
        output; and the board's tty echoes every line we write, so without
        the echo filter a dragged slider would fill the log with its own
        SET lines.
        """
        if not self._control_ids:
            return False
        if controls_mod.is_control_echo(line, self._control_ids):
            return True
        reply = controls_mod.parse_reply(line, self._nonce)
        if reply is None:
            return False
        if reply.kind == "READY":
            result.control_ready = True
        elif reply.ok:
            result.control_acks += 1
        else:
            result.control_errors += 1
        if self._on_control is not None:
            self._on_control(reply)
        return True

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
        self._running = True
        self._transport.write((self._cmd + "\r").encode("utf-8"))

        result = RunResult()
        deadline = None  # set once Ctrl-C has been sent

        try:
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
                    if self._route_control(line, result):
                        continue
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
        finally:
            # Closes the control-write gate on every exit path, including
            # the wedged return above and any exception: once this returns
            # there is no foreground process to receive a line, so a late
            # write from a UI thread would land at the shell prompt and be
            # run as a command.
            self._running = False

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
