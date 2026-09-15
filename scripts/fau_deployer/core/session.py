#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""The board login/prompt state machine: get from "port just opened" to "a
shell prompt we can run commands against and reliably parse the output of",
over a console that might already be logged in, mid-login, sitting in
U-Boot, or stuck in an orphaned program.

Deliberately never replaces the shell's own prompt. An earlier version set a
custom PS1 (a distinctive marker string) so it had an unambiguous sentinel to
search for. That's a real cost for a console engineers routinely connect to
by hand: it leaves an ugly, protocol-looking prompt sitting there instead of
the normal one. It also turned out to be unnecessary -- a prompt still has
to be recognized (both to confirm a command is truly done, in run(), and to
confirm a foreign process has released the console after Ctrl-C, in
wait_for_idle_prompt()), but a generic "ends in $ or #" match against
whatever the shell already shows works just as well as a custom marker, and
every POSIX shell's default prompt has that shape.

Always starts from a FRESH LOGIN. If the probe finds the console already
logged in (or stranded mid-login), the session is "grounded" -- driven back
to a `login:` prompt with Ctrl-D and logged in again -- rather than adopting
whatever shell was already sitting there. Adopting it was the older
behaviour and it meant every session inherited an unknown amount of state
somebody else had left behind: a changed cwd, exported variables, a nested
subshell, an editor's alternate screen. Re-logging in costs one round trip
and makes every session identical. See ground() for why Ctrl-C strictly
precedes Ctrl-D, which is a hardware-safety ordering, not a nicety.

Scope: this module gets a session ready to run commands (needed by
core/bootstrap.py to check for and install the receiver, and by the CLI to
launch it) -- it deliberately does not include anything from the run/
teardown phase (sudo escalation, ISIG verification, signal handling), which
is out of scope for this transport-focused pass.
"""

import re
import time

RC_MARKER = "FAU-RC:"

# Applied once per connect(), regardless of what prompt the shell already
# shows: PS2='' avoids getting stuck on a continuation prompt if a command's
# quoting is ever malformed; unset PROMPT_COMMAND stops a custom prompt hook
# from injecting extra text that could break wait_for_idle_prompt()'s
# trailing-prompt match; unset HISTFILE keeps this session's commands (which
# can include base64 payload chunks) from persisting into a human's bash
# history file; LC_ALL=C and unset LS_COLORS keep command output locale- and
# color-code-free so it parses the same regardless of what the board's login
# shell defaults to. Idempotent, so re-running it on an already-configured
# session (e.g. a second BoardSession over the same still-open transport)
# costs one harmless extra round trip, not a correctness problem.
#
# Deliberately NOT `set +o history` (bash-only, no POSIX `sh` equivalent):
# on a shell that rejects it as an illegal option (confirmed against dash,
# and the board's own login shell is not guaranteed to be bash either --
# see connect()'s docstring), a `set` syntax error aborts the rest of THAT
# command list outright, silently skipping everything chained after it --
# including the FAU-RC: echo run() appends to every command, which reads as
# "the shell died" rather than the real cause. unset HISTFILE alone already
# covers the goal that matters (nothing written to disk); the in-session
# recall `set +o history` would additionally have suppressed is not worth
# that fragility.
_SHELL_HYGIENE_CMD = (
    "PS2=''; unset PROMPT_COMMAND; unset HISTFILE; "
    "export LC_ALL=C; unset LS_COLORS"
)

_UBOOT_MARKERS = ("zynq-uboot>", "Hit any key to stop autoboot")
_LOGIN_MARKERS = ("login:", "Login:")

# 0x03 SIGINT (via the tty line discipline), 0x04 EOF/logout. Order matters
# -- see ground().
_INTR = b"\x03"
_EOT = b"\x04"
_RC_RE = re.compile(r"^FAU-RC:(\d+)$")
_PROMPT_TAIL_RE = re.compile(r"[$#]\s*$")


class LoginError(RuntimeError):
    """Session setup failed in a way that should not be silently retried
    (e.g. a rejected password) or could not be recovered automatically."""


class BoardSession:
    def __init__(self, transport, reader, user="petalinux", password="1234",
                login_timeout=30.0, probe_retries=3, verbose=False,
                ground=True, ground_attempts=3, ground_timeout=10.0):
        self._transport = transport
        self._reader = reader
        self._user = user
        self._password = password
        self._login_timeout = login_timeout
        self._probe_retries = probe_retries
        self._verbose = verbose
        self._ground = ground
        self._ground_attempts = ground_attempts
        self._ground_timeout = ground_timeout

    def _log(self, msg):
        if self._verbose:
            from . import report
            report.say("session", msg)

    def _looks_like_prompt(self, tail):
        return bool(tail) and bool(_PROMPT_TAIL_RE.search(tail))

    def _probe(self):
        """Classify what the console is showing. Returns one of: 'ready'
        (an idle shell prompt, ours from an earlier connect() or any other
        login shell already sitting there), 'login', 'password', 'uboot',
        'silent'.

        Deliberately reads PASSIVELY first, with nothing written: a login
        banner is printed unprompted by getty, so it is normally already
        sitting there. Only if nothing is pending does this fall back to
        sending a bare CR to nudge a response -- safe at an actual shell
        prompt (redraws it), but NOT safe at a fresh 'login:' prompt, where
        it would submit a blank username. That fallback is a last resort,
        not the default, for exactly that reason.
        """
        lines, tail = self._reader.collect_for(0.5)
        combined = "\n".join(lines) + ("\n" + tail if tail else "")
        if not combined:
            self._transport.write(b"\r")
            time.sleep(0.05)
            lines, tail = self._reader.collect_for(2.0)
            combined = "\n".join(lines) + ("\n" + tail if tail else "")

        if any(marker in combined for marker in _UBOOT_MARKERS):
            state = "uboot"
        elif "Password:" in combined:
            state = "password"
        elif "login:" in combined or "Login:" in combined:
            state = "login"
        elif self._looks_like_prompt(tail):
            state = "ready"
        else:
            state = "silent"

        # collect_for() only consumes COMPLETE lines; an unterminated tail
        # (a login/password/shell prompt, none of which end in '\n') is
        # left sitting in the reader's buffer. Its meaning has now been
        # classified, so discard it here -- otherwise it lingers and gets
        # silently prepended to whatever this state's next read sees (e.g.
        # a stale prompt glued onto the front of a command's real output).
        self._reader.reset()
        return state

    def connect(self):
        """Get from "port just opened" to a freshly logged-in, verified
        prompt. Raises LoginError on anything that shouldn't be retried
        blindly.

        Deliberately does NOT drain/discard pending input before probing --
        see _probe()'s docstring: a login banner sitting unread on the wire
        is exactly what the first probe needs to see, not noise to throw
        away.

        Any state that is NOT a fresh `login:` prompt is grounded first
        (ground()), so a non-fresh console -- someone else's shell, a
        half-finished login -- becomes a fresh one instead of being adopted
        with unknown state attached. Construct with ground=False to keep the
        old adopt-what-is-there behaviour; the only caller that wants it is
        a second BoardSession deliberately sharing one already-configured
        console, which costs a needless logout/login otherwise.
        """
        for attempt in range(self._probe_retries + 1):
            state = self._probe()
            self._log("probe -> %s" % state)

            if state == "ready":
                if self._ground:
                    self._ground_then_login()
                else:
                    self._configure_shell()
                    self._verify()
                return
            if state == "uboot":
                raise LoginError(
                    "the board is sitting in U-Boot, not Linux -- interrupt "
                    "the bootloader manually or power-cycle into Linux "
                    "before deploying")
            if state == "password":
                # A bare `Password:` does not say WHICH username is pending
                # -- answering it blind authenticates our password against
                # someone else's login attempt, which fails as "incorrect"
                # and (by _do_password()'s deliberate no-retry rule) aborts
                # the whole connect over what is really a recoverable state.
                # Grounding restarts the exchange from a known `login:`.
                if self._ground:
                    self._ground_then_login()
                else:
                    self._do_password()
                    self._configure_shell()
                    self._verify()
                return
            if state == "login":
                self._do_login()
                self._configure_shell()
                self._verify()
                return
            # 'silent': try to recover an orphaned/stuck console, then retry.
            if attempt < self._probe_retries and self._recover_stale():
                continue
            raise LoginError(
                "no response from the board on this port after %d attempts "
                "-- check --port, and that nothing else (minicom/screen) "
                "has it open" % (attempt + 1))

    @staticmethod
    def _saw_login(lines, tail):
        blob = "\n".join(lines) + (("\n" + tail) if tail else "")
        return any(marker in blob for marker in _LOGIN_MARKERS)

    def ground(self, attempts=None, timeout=None):
        """Drive a console that is NOT at a fresh `login:` prompt back to
        one. Returns True if a login prompt appeared, False if it never did.

        Ctrl-C STRICTLY BEFORE Ctrl-D, and that ordering is a hardware-
        safety requirement rather than tidiness:

        - If a foreground process owns the console, Ctrl-D is not a logout
          at all -- it is EOF on that process's stdin, which a flowgraph
          ignores. Ctrl-C first is what retires it, and for a flowgraph
          that means SIGINT -> the generated handler's tb.stop()/tb.wait()
          -> the block destructors' DMACR.RS clear -> poll DMASR.Halted ->
          fabric reset, i.e. the exact teardown discipline the root
          CLAUDE.md requires.
        - A logout that lands while a flowgraph is still alive delivers
          SIGHUP instead, killing it without any of that -- which orphans a
          live AXI burst on the PS HP port, and ~30 of those wedge the
          board until a power cycle.

        This covers a FOREGROUND flowgraph, which is the only kind this tool
        ever starts (core/runner.py never backgrounds anything, precisely so
        the line discipline owns the signal path). Something a human
        backgrounded by hand is outside what Ctrl-C can reach and would
        still be SIGHUPed by the logout.

        Bounded retries rather than one shot because each Ctrl-D only exits
        ONE shell: a nested subshell somebody left behind eats the first and
        the login prompt appears on the second or third. Exhausting them is
        a real failure (a shell with IGNOREEOF set never logs out this way),
        so it is reported as one rather than retried forever.
        """
        attempts = self._ground_attempts if attempts is None else attempts
        timeout = self._ground_timeout if timeout is None else timeout

        self._log("grounding: interrupting any foreground process")
        self._transport.write(_INTR)
        lines, tail = self._reader.collect_until(
            lambda ls, t: self._saw_login(ls, t) or self._looks_like_prompt(t),
            timeout)
        already = self._saw_login(lines, tail)
        self._reader.reset()
        if already:
            # The interrupt alone got us there (e.g. it aborted a pending
            # login) -- no logout needed, and sending one anyway would just
            # bounce getty for nothing.
            self._log("grounding: already at a login prompt after Ctrl-C")
            return True

        for attempt in range(1, attempts + 1):
            self._log("grounding: Ctrl-D logout (attempt %d/%d)"
                      % (attempt, attempts))
            self._transport.write(_EOT)
            lines, tail = self._reader.collect_until(self._saw_login, timeout)
            saw = self._saw_login(lines, tail)
            self._reader.reset()
            if saw:
                return True
        return False

    def _ground_then_login(self):
        """ground() + a full fresh login + shell setup + verification --
        the path every non-fresh console takes into connect()'s normal
        post-login state."""
        if not self.ground():
            raise LoginError(
                "the console did not return to a login prompt after %d "
                "Ctrl-D logout attempts -- something is holding the "
                "session open (a shell with IGNOREEOF set, or a foreground "
                "program that survived Ctrl-C). Log out on the console by "
                "hand, or power-cycle the board, then retry"
                % self._ground_attempts)
        self._do_login()
        self._configure_shell()
        self._verify()

    def _recover_stale(self):
        """Best-effort recovery for a console stuck inside an orphaned
        program: Ctrl-C, wait, re-probe; twice; then try 'q' (many pagers/
        REPLs exit on it) before giving up. Never sends 0x1a (SUSP) or 0x04
        (EOF/logout) -- see root CLAUDE.md on why suspending a live
        flowgraph is unsafe; this module doesn't know what's running on the
        other end, so it stays conservative."""
        self._log("console silent -- attempting recovery")
        for _ in range(2):
            self._transport.write(b"\x03")
            time.sleep(0.5)
            lines, tail = self._reader.collect_for(1.0)
            if lines or tail:
                return True
        self._transport.write(b"q\r")
        time.sleep(0.5)
        lines, tail = self._reader.collect_for(1.0)
        return bool(lines or tail)

    def _do_login(self):
        self._transport.write((self._user + "\r").encode("ascii"))
        matched = self._reader.wait_for("Password:", self._login_timeout)
        if matched is None:
            raise LoginError(
                "sent the username but never saw a Password: prompt")
        self._do_password()

    def _do_password(self):
        self._transport.write((self._password + "\r").encode("ascii"))

        def done(lines, tail):
            if any("incorrect" in line.lower() for line in lines):
                return True
            return self._looks_like_prompt(tail)

        lines, tail = self._reader.collect_until(done, self._login_timeout)
        if any("incorrect" in line.lower() for line in lines):
            # Fail immediately -- do not retry-loop a wrong password, which
            # risks an account lockout on repeated automated attempts.
            raise LoginError(
                "login incorrect for user %r -- refusing to retry "
                "automatically" % self._user)
        if not self._looks_like_prompt(tail):
            raise LoginError(
                "no shell prompt appeared after login within %.0fs"
                % self._login_timeout)
        # As in _probe(): the shell's own prompt is sitting in the buffer as
        # an unterminated tail. _configure_shell() runs next and doesn't
        # care what's glued onto the front of its first line, but clearing
        # it here keeps this method's contract the same as _probe()'s --
        # "classified, and consumed" -- rather than leaving that distinction
        # to whichever caller happens to run next.
        self._reader.reset()

    def _configure_shell(self):
        self.run_ok(_SHELL_HYGIENE_CMD)

    def _verify(self):
        """Confirm both directions actually work, not just that a command
        appeared to run."""
        rc, out = self.run('echo FAU-""ECHO-OK')
        if rc != 0 or "FAU-ECHO-OK" not in out:
            raise LoginError(
                "echo verification failed after login (rc=%r, out=%r) -- "
                "the console may be echoing unexpected content" % (rc, out))

    def run(self, cmd, timeout=15.0):
        """Run `cmd` and return (returncode, output). `output` excludes the
        echoed command line and the FAU-RC: marker line.

        Waits for the FAU-RC: line AND the prompt that follows it -- the RC
        line alone already proves `cmd` is done (it only ever appears once
        the shell has moved on to the appended `echo`), but the prompt is
        printed asynchronously after that and needs to be waited for and
        consumed too, or it arrives later and corrupts the next run() call.
        This never depends on what a particular prompt is actually
        configured to say, just the generic "ends in $ or #" shape every
        POSIX shell's default prompt has.
        """
        sent = cmd + '; echo "%s$?"' % RC_MARKER
        self._transport.write((sent + "\r").encode("utf-8"))

        def rc_line_then_prompt(lines, tail):
            # Both conditions, not just the first: the FAU-RC: line proves
            # the command is done, but the shell's next prompt is printed
            # asynchronously afterward and may not have reached the buffer
            # yet at that exact instant. Returning as soon as only the RC
            # line shows up (a real bug caught by
            # test_repeated_commands_do_not_desync) leaves that prompt to
            # arrive later, unconsumed, and glue onto the front of the NEXT
            # run()'s echoed command -- collect_until() re-checks this
            # predicate against the growing tail on every tick even when no
            # new complete line has arrived, so waiting for both here costs
            # nothing when the prompt is already there and just waits out
            # the race when it isn't.
            return (bool(lines) and _RC_RE.match(lines[-1].strip()) is not None
                    and self._looks_like_prompt(tail))

        lines, _tail = self._reader.collect_until(rc_line_then_prompt, timeout)
        # Whatever's left in the buffer now is exactly the prompt the
        # predicate above just confirmed (or, on a timeout, whatever
        # incomplete fragment was sitting there) -- discard it so it can't
        # glue onto the front of the next run()'s output, the same
        # "classified, and consumed" discipline _probe()/_do_password() use.
        self._reader.reset()

        rc = None
        output_lines = []
        for line in lines:
            if line.strip() == sent:
                continue  # the console's own echo of what we sent
            m = _RC_RE.match(line.strip())
            if m:
                rc = int(m.group(1))
                continue
            output_lines.append(line)

        if rc is None:
            raise LoginError(
                "command %r did not report its exit status within %.0fs -- "
                "echo may be off, or the shell died" % (cmd, timeout))
        return rc, "\n".join(output_lines)

    def run_ok(self, cmd, timeout=15.0):
        """Like run(), but raises LoginError if the command's own exit
        status was nonzero."""
        rc, out = self.run(cmd, timeout=timeout)
        if rc != 0:
            raise LoginError("command %r exited %d: %s" % (cmd, rc, out))
        return out

    def wait_for_idle_prompt(self, timeout):
        """Wait until the console looks like an idle shell prompt again --
        used after sending a raw control byte (e.g. Ctrl-C) to something
        that isn't a run()-issued command, where there is no FAU-RC: line to
        wait for instead (core/bootstrap.py retiring a stale receiver;
        cli.py shutting one down after a successful transfer -- see
        interrupt_and_wait()). Returns True if a prompt-like tail appeared
        within `timeout`, False on timeout. Consumes whatever was buffered
        either way.
        """
        _lines, tail = self._reader.collect_until(
            lambda _lines, t: self._looks_like_prompt(t), timeout)
        self._reader.reset()
        return self._looks_like_prompt(tail)

    def interrupt_and_wait(self, timeout=5.0):
        """Send Ctrl-C (0x03) and wait for the console to settle back to an
        idle shell prompt -- for retiring a foreign foreground process (a
        stale/still-running board-side receiver), not for interrupting one
        of this session's own run() commands (which never need it -- see
        run()'s docstring). Returns True if a prompt reappeared within
        `timeout`, False on timeout.
        """
        self._transport.write(b"\x03")
        return self.wait_for_idle_prompt(timeout)
