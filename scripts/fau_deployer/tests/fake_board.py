#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""A real pty with an actual `python3 board/receiver.py` subprocess attached
as its controlling terminal, so tests exercise real termios, real line
discipline, real MAX_CANON behavior and real base64/gzip/tar handling --
everything the transport and protocol depend on except the Zynq itself. A
pure in-process mock would not exercise any of the parts that are hard to
reason about here.

Uses os.forkpty() rather than pty.openpty() + subprocess.Popen(stdin=slave_fd,
...): the latter only dup2's an already-open fd onto the child's stdio,
which never establishes the pty as that process's CONTROLLING terminal (that
requires setsid() + ioctl(TIOCSCTTY), which forkpty() does for you). Without
a controlling terminal, job-control signals (a real SIGINT from Ctrl-C) are
never generated -- irrelevant for the receiver itself, but exactly what
fake_shell.py's orphan-recovery test needs.
"""

import os
import signal
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
RECEIVER_PATH = os.path.join(os.path.dirname(_HERE), "board", "receiver.py")


class FakeBoard:
    def __init__(self, dest, receiver_args=(), python=None):
        self.master_fd = None
        self._pid = None
        self._dest = dest
        self._python = python or sys.executable
        self._receiver_args = list(receiver_args)

    def start_receiver(self):
        pid, master_fd = os.forkpty()
        if pid == 0:
            cmd = ([self._python, RECEIVER_PATH, "--dest", str(self._dest)]
                  + self._receiver_args)
            try:
                os.execvp(cmd[0], cmd)
            finally:
                os._exit(127)  # only reached if execvp itself failed
        self.master_fd = master_fd
        self._pid = pid
        return pid

    def transport(self, transcript=None):
        from ..core.transport import FdTransport
        return FdTransport(self.master_fd, transcript=transcript,
                          name="fake-board")

    def wait(self, timeout=10.0):
        return _waitpid_timeout(self._pid, timeout)

    def poll(self):
        if self._pid is None:
            return None
        pid, status = os.waitpid(self._pid, os.WNOHANG)
        return status if pid == self._pid else None

    def stop(self):
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if _waitpid_timeout(self._pid, 5.0) is None:
                try:
                    os.kill(self._pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _waitpid_timeout(self._pid, 5.0)
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()


def _waitpid_timeout(pid, timeout):
    """os.waitpid() has no timeout parameter; poll it instead."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            done_pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if done_pid == pid:
            return status
        time.sleep(0.05)
    return None
