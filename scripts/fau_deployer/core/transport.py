#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""The byte-transport seam.

`Transport` is deliberately the only thing that knows about a real serial
port. Everything above it (LineReader, the login state machine, the sender)
talks to a plain read/write/close interface, which is what lets the whole
protocol be exercised in tests over a pty pair with a real receiver
subprocess on the other end, instead of only against real hardware.
"""

import os
import select
import time


class TransportError(RuntimeError):
    """A transport-level failure: port wouldn't open, unexpected close, etc.
    Distinct from a protocol-level failure (a bad CRC, a timeout waiting for
    a sentinel), which is the caller's concern, not the transport's."""


class Transport:
    """Abstract byte transport. `read` and `drain_input` never raise on a
    plain timeout -- they return what they have (possibly nothing); only a
    genuine I/O failure raises TransportError."""

    name = "transport"

    def write(self, data):
        raise NotImplementedError

    def read(self, maxlen, timeout):
        """Read up to maxlen bytes, waiting at most timeout seconds for the
        first byte to arrive. Returns b"" on timeout, never blocks past it."""
        raise NotImplementedError

    def drain_input(self, settle=0.2):
        """Read and discard whatever arrives for `settle` seconds, then
        return it. Used to clear stale buffered input (e.g. echoed
        keystrokes, a half-printed banner) before a fresh PROBE."""
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class Transcript:
    """Timestamped byte log of both directions of a session, for debugging a
    lossy console after the fact -- without one, a corrupted transfer is
    guesswork about what was actually on the wire."""

    def __init__(self, path):
        self._f = open(path, "ab", buffering=0)
        self._t0 = time.monotonic()

    def note(self, direction, data):
        """direction is '>' (desktop->board) or '<' (board->desktop)."""
        if not data:
            return
        ts = time.monotonic() - self._t0
        header = ("[%10.3f] %s " % (ts, direction)).encode("ascii")
        self._f.write(header + repr(data).encode("ascii", "replace") + b"\n")

    def close(self):
        try:
            self._f.close()
        except OSError:
            pass


class _FdTransportBase(Transport):
    """Shared os.read/select/os.write plumbing for anything backed by a
    plain file descriptor: a real serial port opened by pyserial (which
    exposes .fd), or a pty master fd in tests. Concrete subclasses only need
    to set self._fd (read) and self._wfd (write; usually the same fd) and
    implement close()."""

    _fd = None
    _wfd = None
    _transcript = None

    def write(self, data):
        if not data:
            return
        written = 0
        while written < len(data):
            n = os.write(self._wfd, data[written:])
            written += n
        if self._transcript is not None:
            self._transcript.note(">", data)

    def read(self, maxlen, timeout):
        r, _w, _x = select.select([self._fd], [], [], timeout)
        if self._fd not in r:
            return b""
        try:
            data = os.read(self._fd, maxlen)
        except OSError:
            return b""
        if self._transcript is not None and data:
            self._transcript.note("<", data)
        return data

    def drain_input(self, settle=0.2):
        out = []
        deadline = time.monotonic() + settle
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = self.read(65536, remaining)
            if not chunk:
                break
            out.append(chunk)
            # Keep draining as long as bytes keep arriving; a chatty boot
            # banner can easily outlast a single `settle` window.
            deadline = time.monotonic() + settle
        return b"".join(out)


class SerialTransport(_FdTransportBase):
    """A real serial port, via pyserial."""

    def __init__(self, port, baud=115200, transcript=None):
        # Imported lazily so importing this module (e.g. from tests that
        # only use FdTransport) never requires pyserial to be installed.
        import serial

        self.name = port
        self._transcript = transcript
        try:
            self._serial = serial.Serial(
                port=port, baudrate=baud, timeout=0,
                write_timeout=5.0)
        except serial.SerialException as exc:
            raise TransportError("%s: %s" % (port, exc)) from exc
        self._fd = self._serial.fileno()
        self._wfd = self._fd

    def close(self):
        try:
            self._serial.close()
        except Exception:
            pass


class FdTransport(_FdTransportBase):
    """A raw file descriptor pair -- a pty master in tests, or (in principle)
    any other fd-based channel. `wfd` defaults to `fd` (as it would for a
    pty master, which is bidirectional)."""

    def __init__(self, fd, wfd=None, transcript=None, name="fd"):
        self.name = name
        self._fd = fd
        self._wfd = wfd if wfd is not None else fd
        self._transcript = transcript
        self._owns_fd = True

    def close(self):
        for fd in {self._fd, self._wfd}:
            try:
                os.close(fd)
            except OSError:
                pass


class LineReader:
    """Turns a Transport's byte stream into lines, with both a line-by-line
    view and a substring "wait for this text" view -- the latter matters
    because a bash PS1 prompt has no trailing newline, so anything waiting
    for a prompt can never rely on `lines()` alone.

    ANSI escape sequences and trailing '\\r' are stripped before anything
    sees a line, so callers never have to think about bracketed-paste mode
    or CRLF translation.
    """

    def __init__(self, transport, line_max=4096):
        from .protocol import strip_ansi
        self._transport = transport
        self._line_max = line_max
        self._strip_ansi = strip_ansi
        self._buf = ""  # decoded, not yet consumed as a line or matched

    def _pull(self, timeout):
        data = self._transport.read(65536, timeout)
        if not data:
            return False
        text = self._strip_ansi(data.decode("utf-8", "replace"))
        self._buf += text
        if len(self._buf) > self._line_max and "\n" not in self._buf:
            # Unbounded growth guard: a line that never terminates is
            # indistinguishable from a firehose of garbage. Keep only the
            # tail, on the theory that whatever prompt/sentinel we are
            # waiting for is more likely to show up at the end.
            self._buf = self._buf[-self._line_max:]
        return True

    def poll(self, timeout):
        """Pull whatever is available within `timeout` into the buffer,
        once. Does not block waiting for a specific line."""
        self._pull(timeout)

    def lines(self, timeout):
        """Yield complete ('\\r' and ANSI already stripped) lines available
        within `timeout` total. Does not yield the trailing partial line
        (e.g. an unterminated prompt) -- see partial() for that."""
        deadline = time.monotonic() + timeout
        while True:
            nl = self._buf.find("\n")
            while nl != -1:
                line = self._buf[:nl].rstrip("\r")
                self._buf = self._buf[nl + 1:]
                yield line
                nl = self._buf.find("\n")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if not self._pull(min(remaining, 1.0)):
                # No data this tick; only actually stop once the deadline
                # has passed, so a slow trickle of input isn't mistaken for
                # "nothing more is coming".
                if time.monotonic() >= deadline:
                    return

    def wait_for(self, needle, timeout, on_line=None):
        """Wait up to `timeout` seconds for `needle` to appear anywhere in
        the buffer (a completed line, or the unterminated tail -- e.g. a
        prompt). Returns the matched text (whatever was buffered at match
        time) or None on timeout. `on_line` is called with each complete
        line seen while waiting, so a caller can log/inspect traffic that
        turns out not to be the thing it was waiting for.
        """
        deadline = time.monotonic() + timeout
        while True:
            # Drain and dispatch every COMPLETE line first. Checking the
            # whole buffer for the needle before this loop would return the
            # entire multi-line burst (and skip on_line for the earlier
            # lines) whenever more than one line arrives in a single read --
            # a single-chunk boot banner followed by the sentinel on its own
            # line is exactly that case.
            nl = self._buf.find("\n")
            while nl != -1:
                line = self._buf[:nl].rstrip("\r")
                self._buf = self._buf[nl + 1:]
                if on_line is not None:
                    on_line(line)
                if needle in line:
                    return line
                nl = self._buf.find("\n")
            # No more complete lines. The needle may still be sitting in an
            # unterminated tail -- e.g. a bash prompt, which never ends in
            # '\n'. Consume up through the match (not the whole buffer --
            # anything after the needle is left for the next caller) so a
            # matched-but-unterminated prompt doesn't sit in the buffer and
            # get prepended to whatever arrives next.
            idx = self._buf.find(needle)
            if idx != -1:
                end = idx + len(needle)
                matched = self._buf[:end]
                self._buf = self._buf[end:]
                return matched
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._pull(min(remaining, 1.0))

    def drain_to(self, needle, timeout):
        """Like wait_for, but returns everything collected (all lines plus
        any partial tail) rather than just the matching fragment -- useful
        when the caller wants to classify the whole burst of output, not
        just confirm the needle showed up."""
        collected = []
        matched = self.wait_for(needle, timeout, on_line=collected.append)
        if matched is None:
            tail = self._buf
            return "\n".join(collected) + (("\n" + tail) if tail else "")
        if collected and collected[-1] == matched:
            # `matched` was a complete line, and wait_for already passed it
            # to on_line (so it's the last entry in `collected`) before
            # returning it -- don't append it a second time.
            return "\n".join(collected)
        return "\n".join(collected + [matched])

    def partial(self):
        """Whatever is buffered but not yet a complete line -- e.g. a
        prompt with no trailing newline."""
        return self._buf

    def reset(self):
        self._buf = ""

    def collect_for(self, seconds):
        """Pull output for up to `seconds`, consuming every complete line
        into a list. Returns (lines, tail) where `tail` is whatever remains
        unterminated (e.g. a prompt with no trailing newline) once the
        window closes.

        Used for PROBE-style classification, where the caller wants to look
        at everything that arrived in a burst -- boot banner, login
        prompt, shell prompt -- rather than wait for one specific needle.
        """
        lines = []
        deadline = time.monotonic() + seconds
        while True:
            nl = self._buf.find("\n")
            while nl != -1:
                line = self._buf[:nl].rstrip("\r")
                self._buf = self._buf[nl + 1:]
                lines.append(line)
                nl = self._buf.find("\n")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._pull(min(remaining, 1.0))
        return lines, self._buf

    def collect_until(self, predicate, timeout):
        """Like collect_for, but returns as soon as `predicate(lines, tail)`
        is true rather than always waiting out the full window -- used
        where the caller has more than one possible "done" condition (e.g.
        either a rejection message or a prompt) and a fixed-duration wait
        would needlessly stall every call for the full timeout.
        """
        lines = []
        deadline = time.monotonic() + timeout
        while True:
            nl = self._buf.find("\n")
            while nl != -1:
                line = self._buf[:nl].rstrip("\r")
                self._buf = self._buf[nl + 1:]
                lines.append(line)
                if predicate(lines, self._buf):
                    return lines, self._buf
                nl = self._buf.find("\n")
            if predicate(lines, self._buf):
                return lines, self._buf
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return lines, self._buf
            self._pull(min(remaining, 0.2))
