#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""A minimal getty+login emulator for testing core/session.py's PROBE state
machine over a real pty, without a board. Handles only the login banner/
credential exchange itself; on success it runs a REAL shell (/bin/sh) so
shell hygiene setup, command echo and run() are all exercised against
genuine shell behavior (including its own real default prompt) rather than
a second, possibly-unfaithful mock of one.

The shell is FORKED and waited for, not exec'd into. Exec was simpler but
made the stub a one-shot: once the shell exited there was nothing left to
present another login prompt, so it could not model the thing
BoardSession.ground() depends on -- a real console where Ctrl-D logs out
and getty immediately respawns `login:`. Forking makes the logout/login
cycle testable and costs only the SIGINT/SIGTERM plumbing below (the parent
has to ignore SIGINT so a Ctrl-C aimed at the foreground shell does not
also take out the "getty" standing behind it, exactly as init does not die
when a user interrupts their shell).

Not part of the deployer -- test infrastructure only.
"""

import argparse
import os
import signal
import sys
import time

BANNER = "Welcome to PetaLinux\n"


def _write(s):
    sys.stdout.write(s)
    sys.stdout.flush()


def _readline():
    line = sys.stdin.readline()
    if not line:
        return None
    return line.rstrip("\r\n")


_shell_pid = None


def _kill_shell(_sig=None, _frame=None):
    if _shell_pid is not None:
        try:
            os.kill(_shell_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    os._exit(0)


def _shell_session():
    """Run /bin/sh in the foreground until it exits (Ctrl-D, or `exit`),
    then return so the caller can present a fresh login prompt."""
    global _shell_pid
    sys.stdout.flush()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, _kill_shell)
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            os.execvp("/bin/sh", ["/bin/sh"])
        finally:
            os._exit(127)
    _shell_pid = pid
    os.waitpid(pid, 0)
    _shell_pid = None


def mode_prompt(args):
    # Already "logged in": a shell prompt and no login banner at all, which
    # is the console state ground() exists for. Once that shell is logged
    # out of, behave like getty and offer a login prompt again.
    _shell_session()
    return mode_login(args)


def mode_norelogin(args):
    # A console that hands out one shell and then NEVER offers a login
    # prompt again -- the failure ground() has to report rather than retry
    # forever (a shell with IGNOREEOF set looks like this from outside).
    _shell_session()
    while True:
        time.sleep(3600)


def mode_login(args):
    _write(BANNER)
    while True:
        _write("%s login: " % args.hostname)
        user = _readline()
        if user is None:
            return
        _write("Password: ")
        password = _readline()
        if password is None:
            return
        if user == args.user and password == args.password:
            _write("\n")
            _shell_session()
            # Logged out -- loop round and offer the login prompt again,
            # the way getty respawns.
            continue
        _write("Login incorrect\n\n")


def mode_uboot(args):
    _write("U-Boot 2022.01 (Jan 01 2026 - 00:00:00 +0000)\n")
    _write("Hit any key to stop autoboot:  0 \n")
    _write("zynq-uboot> ")
    # Never produces a login prompt; just idles, echoing nothing further.
    while True:
        line = _readline()
        if line is None:
            return
        _write("zynq-uboot> ")


def mode_silent(args):
    # Never responds to anything, ever -- simulates a genuinely dead console.
    while True:
        time.sleep(3600)


def mode_orphan(args):
    # Simulates a console stuck inside an orphaned foreground program: no
    # output at all until Ctrl-C recovers it, then falls through to the
    # normal login flow -- exercising BoardSession._recover_stale().
    #
    # A real pty in canonical/ISIG mode delivers Ctrl-C (0x03) as a SIGINT
    # to the whole foreground process group, not as a data byte -- so a
    # single process trying to os.read() it back would just get killed
    # itself. This mirrors what actually happens with a real shell: fork a
    # child that hangs (default SIGINT disposition -- it dies), while this
    # process (standing in for the shell) ignores SIGINT and waits for the
    # child, then falls through to the login flow once it's gone.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        while True:
            time.sleep(3600)
    os.waitpid(pid, 0)
    _write("\n")
    return mode_login(args)


MODES = {
    "prompt": mode_prompt,
    "norelogin": mode_norelogin,
    "login": mode_login,
    "uboot": mode_uboot,
    "silent": mode_silent,
    "orphan": mode_orphan,
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=sorted(MODES), required=True)
    p.add_argument("--user", default="petalinux")
    p.add_argument("--password", default="1234")
    p.add_argument("--hostname", default="board")
    args = p.parse_args()
    try:
        MODES[args.mode](args)
    except OSError:
        # The test closed the pty master; writing to the slave now raises
        # EIO. That is a normal end of life for this process, not a
        # failure worth a traceback into a closed terminal.
        pass


if __name__ == "__main__":
    main()
