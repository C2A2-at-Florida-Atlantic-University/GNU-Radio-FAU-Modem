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

Scope: this module gets a session ready to run commands (needed by
core/bootstrap.py to check for and install the receiver, and by the CLI to
launch it) -- it deliberately does not include anything from the run/
teardown phase (sudo escalation, ISIG verification, signal handling), which
is out of scope for this transport-focused pass.
"""

import re
import time

PS1_MARKER = "===FAU-PS1==="
RC_MARKER = "FAU-RC:"

# The split-literal trick: the RAW SOURCE the shell echoes back (before it
# evaluates anything) is `PS1='===FAU-'"PS1"'==='`, which does NOT contain
# the substring `===FAU-PS1===` -- the quote characters interrupt it. Only
# the EXPANDED prompt printed afterward does. That is what lets wait_for()
# tell "the command that sets the prompt" apart from "the prompt it set".
_PS1_SETUP_FMT = (
    "PS1='===FAU-'\"PS1\"'==='; PS2=''; unset PROMPT_COMMAND; "
    "unset HISTFILE; set +o history; export LC_ALL=C; unset LS_COLORS"
)

_UBOOT_MARKERS = ("zynq-uboot>", "Hit any key to stop autoboot")
_RC_RE = re.compile(r"^FAU-RC:(\d+)$")
_PROMPT_TAIL_RE = re.compile(r"[$#]\s*$")


class LoginError(RuntimeError):
    """Session setup failed in a way that should not be silently retried
    (e.g. a rejected password) or could not be recovered automatically."""


class BoardSession:
    def __init__(self, transport, reader, user="petalinux", password="1234",
                ps1_marker=PS1_MARKER, login_timeout=30.0, probe_retries=3,
                verbose=False):
        self._transport = transport
        self._reader = reader
        self._user = user
        self._password = password
        self._ps1_marker = ps1_marker
        self._login_timeout = login_timeout
        self._probe_retries = probe_retries
        self._verbose = verbose

    @property
    def ps1_marker(self):
        return self._ps1_marker

    def _log(self, msg):
        if self._verbose:
            from . import report
            report.say("session", msg)

    def _looks_like_prompt(self, tail):
        return bool(tail) and bool(_PROMPT_TAIL_RE.search(tail))

    def _probe(self):
        """Classify what the console is showing. Returns one of: 'ready'
        (already at our own prompt), 'login', 'password', 'uboot',
        'prompt' (a shell prompt we haven't configured yet), 'silent'.

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

        if self._ps1_marker in combined:
            state = "ready"
        elif any(marker in combined for marker in _UBOOT_MARKERS):
            state = "uboot"
        elif "Password:" in combined:
            state = "password"
        elif "login:" in combined or "Login:" in combined:
            state = "login"
        elif self._looks_like_prompt(tail):
            state = "prompt"
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
        """Get from "port just opened" to a configured, verified prompt.
        Raises LoginError on anything that shouldn't be retried blindly.

        Deliberately does NOT drain/discard pending input before probing --
        see _probe()'s docstring: a login banner sitting unread on the wire
        is exactly what the first probe needs to see, not noise to throw
        away.
        """
        for attempt in range(self._probe_retries + 1):
            state = self._probe()
            self._log("probe -> %s" % state)

            if state == "ready":
                return
            if state == "uboot":
                raise LoginError(
                    "the board is sitting in U-Boot, not Linux -- interrupt "
                    "the bootloader manually or power-cycle into Linux "
                    "before deploying")
            if state == "password":
                self._do_password()
                self._set_prompt()
                self._verify()
                return
            if state == "login":
                self._do_login()
                self._set_prompt()
                self._verify()
                return
            if state == "prompt":
                self._set_prompt()
                self._verify()
                return
            # 'silent': try to recover an orphaned/stuck console, then retry.
            if attempt < self._probe_retries and self._recover_stale():
                continue
            raise LoginError(
                "no response from the board on this port after %d attempts "
                "-- check --port, and that nothing else (minicom/screen) "
                "has it open" % (attempt + 1))

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
        # As in _probe(): the shell's own (uninteresting) prompt is sitting
        # in the buffer as an unterminated tail. _set_prompt() runs next and
        # doesn't care what's glued onto the front of its first line, but
        # clearing it here keeps this method's contract the same as
        # _probe()'s -- "classified, and consumed" -- rather than leaving
        # that distinction to whichever caller happens to run next.
        self._reader.reset()

    def _set_prompt(self):
        self._transport.write((_PS1_SETUP_FMT + "\r").encode("ascii"))
        matched = self._reader.wait_for(self._ps1_marker, self._login_timeout)
        if matched is None:
            raise LoginError(
                "prompt marker never appeared after PS1 setup -- the shell "
                "may not be bash/POSIX-sh compatible")

    def _verify(self):
        """Confirm both directions actually work, not just that a marker
        showed up once."""
        rc, out = self.run('echo FAU-""ECHO-OK')
        if rc != 0 or "FAU-ECHO-OK" not in out:
            raise LoginError(
                "echo verification failed after login (rc=%r, out=%r) -- "
                "the console may be echoing unexpected content" % (rc, out))

    def run(self, cmd, timeout=15.0):
        """Run `cmd` and return (returncode, output). `output` excludes the
        echoed command line, the FAU-RC: marker line, and the prompt that
        follows -- just what the command itself printed."""
        sent = cmd + '; echo "%s$?"' % RC_MARKER
        collected = []
        self._transport.write((sent + "\r").encode("utf-8"))
        matched = self._reader.wait_for(self._ps1_marker, timeout,
                                       on_line=collected.append)
        if matched is None:
            raise LoginError(
                "command %r did not return to a prompt within %.0fs"
                % (cmd, timeout))

        rc = None
        output_lines = []
        for line in collected:
            if line.strip() == sent:
                continue  # the console's own echo of what we sent
            m = _RC_RE.match(line.strip())
            if m:
                rc = int(m.group(1))
                continue
            output_lines.append(line)

        if rc is None:
            raise LoginError(
                "command %r: no %s marker seen in the reply -- echo may be "
                "off, or the shell died" % (cmd, RC_MARKER))
        return rc, "\n".join(output_lines)

    def run_ok(self, cmd, timeout=15.0):
        """Like run(), but raises LoginError if the command's own exit
        status was nonzero."""
        rc, out = self.run(cmd, timeout=timeout)
        if rc != 0:
            raise LoginError("command %r exited %d: %s" % (cmd, rc, out))
        return out

    def disconnect(self):
        """Best-effort cleanup: restore the original prompt so a human
        reconnecting to the same console isn't left staring at our marker."""
        try:
            self._transport.write(b'unset PS1\r')
        except Exception:
            pass
