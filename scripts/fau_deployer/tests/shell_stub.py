#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""A minimal getty+login emulator for testing core/session.py's PROBE state
machine over a real pty, without a board. Handles only the login banner/
credential exchange itself; on success it execs into a REAL shell (/bin/sh)
so PS1 setup, command echo and run() are all exercised against genuine shell
behavior rather than a second, possibly-unfaithful mock of one.

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


def _exec_shell():
    sys.stdout.flush()
    os.execvp("/bin/sh", ["/bin/sh"])


def mode_prompt(args):
    # Already "logged in" -- just hand straight to a real shell, exercising
    # the 'prompt' PROBE branch (no login: banner at all).
    _exec_shell()


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
            _exec_shell()
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
    MODES[args.mode](args)


if __name__ == "__main__":
    main()
