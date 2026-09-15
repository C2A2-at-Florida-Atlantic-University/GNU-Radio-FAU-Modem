#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""LineReader tests against an in-memory fake Transport -- no fd, no pty.
These exercise the buffering/matching logic in isolation; test_end_to_end.py
covers the same LineReader driven by a real pty against a real receiver.
"""

import unittest

from ..core.protocol import parse_line
from ..core.transport import Transport, LineReader


class FakeTransport(Transport):
    """Replays a fixed sequence of byte chunks, one per read() call, with no
    real timing -- just enough to drive LineReader deterministically."""

    name = "fake"

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.written = []

    def write(self, data):
        self.written.append(data)

    def read(self, maxlen, timeout):
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def drain_input(self, settle=0.2):
        out = []
        while self._chunks:
            out.append(self._chunks.pop(0))
        return b"".join(out)

    def close(self):
        pass


class TestLines(unittest.TestCase):
    def test_splits_multiple_lines_in_one_chunk(self):
        t = FakeTransport([b"line one\nline two\nline three\n"])
        r = LineReader(t)
        got = list(r.lines(1.0))
        self.assertEqual(got, ["line one", "line two", "line three"])

    def test_partial_line_not_yielded_until_terminated(self):
        t = FakeTransport([b"partial line, no newline yet"])
        r = LineReader(t)
        got = list(r.lines(0.01))
        self.assertEqual(got, [])
        self.assertEqual(r.partial(), "partial line, no newline yet")

    def test_carriage_return_stripped(self):
        t = FakeTransport([b"windows style\r\nline\r\n"])
        r = LineReader(t)
        got = list(r.lines(1.0))
        self.assertEqual(got, ["windows style", "line"])

    def test_ansi_stripped_from_lines(self):
        t = FakeTransport([b"\x1b[?2004hpetalinux@board:~$ \x1b[?2004l\n"])
        r = LineReader(t)
        got = list(r.lines(1.0))
        self.assertEqual(got, ["petalinux@board:~$ "])


class TestWaitFor(unittest.TestCase):
    def test_finds_needle_in_an_unterminated_prompt(self):
        # A bash PS1 prompt has no trailing newline -- this is exactly why
        # wait_for looks at the raw buffer, not just complete lines.
        t = FakeTransport([b"===FAU-PS1==="])
        r = LineReader(t)
        matched = r.wait_for("===FAU-PS1===", 1.0)
        self.assertIsNotNone(matched)
        self.assertIn("===FAU-PS1===", matched)

    def test_finds_needle_split_across_chunks(self):
        t = FakeTransport([b"===FAU-", b"RECV-READY", b" v1 abc dest=/x==="])
        r = LineReader(t)
        matched = r.wait_for("===FAU-RECV-READY", 1.0)
        self.assertIsNotNone(matched)

    def test_on_line_callback_sees_every_complete_line_including_the_match(self):
        t = FakeTransport([b"boot message one\nboot message two\nTARGET\n"])
        r = LineReader(t)
        seen = []
        matched = r.wait_for("TARGET", 1.0, on_line=seen.append)
        self.assertEqual(matched, "TARGET")
        # All three lines arrived in a single read(); on_line must still see
        # each one individually (in order) rather than the match short-
        # circuiting before the earlier lines are dispatched.
        self.assertEqual(seen, ["boot message one", "boot message two", "TARGET"])

    def test_timeout_returns_none(self):
        t = FakeTransport([b"nothing relevant\n"])
        r = LineReader(t)
        matched = r.wait_for("NEVER APPEARS", 0.05)
        self.assertIsNone(matched)

    def test_matched_unterminated_tail_is_consumed_not_left_in_the_buffer(self):
        # Regression: a matched prompt (no trailing '\n') used to be left
        # sitting in the buffer, so the NEXT wait_for() call would see it
        # prepended to whatever arrived after -- e.g. a second prompt match
        # coming back as "===MARKER===echo something" instead of cleanly
        # separating the two. This is exactly what happens across two
        # consecutive shell commands against the same PS1 marker.
        t = FakeTransport([b"===MARKER===", b"echo hi\nhi\n===MARKER==="])
        r = LineReader(t)
        first = r.wait_for("===MARKER===", 1.0)
        self.assertEqual(first, "===MARKER===")
        self.assertEqual(r.partial(), "", "the match must be consumed")

        second = r.wait_for("===MARKER===", 1.0)
        self.assertEqual(second, "===MARKER===")


class TestWaitForLine(unittest.TestCase):
    """A protocol sentinel is parsed after it is matched, so matching a
    partial tail is worse than not matching at all. These reproduce the
    real-serial-port framing that a pty never produces.
    """

    # What board/receiver.py actually emits, split the way a UART delivers
    # it: the first read stops after the needle but before the '\n'.
    READY = ("===FAU-RECV-READY v1 fbf3bd1a15804846 "
             "dest=/home/petalinux/flowgraphs===")

    def test_wait_for_truncates_a_fragmented_sentinel_at_the_needle(self):
        # Documents WHY wait_for() must not be used for a parsed sentinel:
        # it reports a match, but one stripped of every field after the
        # needle -- which parse_line() then rejects, making a receiver that
        # answered instantly look like it never answered at all.
        t = FakeTransport([b"===FAU-RECV-READY v1 fbf3bd",
                           b"1a15804846 dest=/home/petalinux/flowgraphs===\n"])
        r = LineReader(t)
        matched = r.wait_for("===FAU-RECV-READY", 1.0)
        self.assertEqual(matched, "===FAU-RECV-READY")
        self.assertIsNone(parse_line(matched))

    def test_wait_for_line_returns_the_whole_line_when_fragmented(self):
        t = FakeTransport([b"===FAU-RECV-READY v1 fbf3bd",
                           b"1a15804846 dest=/home/petalinux/flowgraphs===\n"])
        r = LineReader(t)
        matched = r.wait_for_line("===FAU-RECV-READY", 1.0)
        self.assertEqual(matched, self.READY)

        parsed = parse_line(matched)
        self.assertIsNotNone(parsed)
        kind, fields = parsed
        self.assertEqual(kind, "ready")
        self.assertEqual(fields["version"], "v1")
        self.assertEqual(fields["sha16"], "fbf3bd1a15804846")
        self.assertEqual(fields["dest"], "/home/petalinux/flowgraphs")

    def test_wait_for_line_survives_a_byte_at_a_time_trickle(self):
        # The pathological end of the same problem: 115200 baud with a
        # nearly-empty FIFO can hand back a single byte per read.
        payload = (self.READY + "\n").encode("ascii")
        t = FakeTransport([payload[i:i + 1] for i in range(len(payload))])
        r = LineReader(t)
        self.assertEqual(r.wait_for_line("===FAU-RECV-READY", 1.0), self.READY)

    def test_wait_for_line_ignores_an_unterminated_tail(self):
        # No trailing '\n' means the line is still arriving; matching it now
        # is the bug this method exists to avoid.
        t = FakeTransport([self.READY.encode("ascii")])
        r = LineReader(t)
        self.assertIsNone(r.wait_for_line("===FAU-RECV-READY", 0.05))

    def test_wait_for_line_skips_preceding_noise_and_command_echo(self):
        t = FakeTransport([
            b"python3 /home/petalinux/.fau/receiver.py --dest x\r\n",
            b"[  123.45] random kernel printk\n",
            (self.READY + "\n").encode("ascii"),
        ])
        r = LineReader(t)
        seen = []
        matched = r.wait_for_line("===FAU-RECV-READY", 1.0, on_line=seen.append)
        self.assertEqual(matched, self.READY)
        self.assertIn("[  123.45] random kernel printk", seen)


class TestDrainTo(unittest.TestCase):
    def test_no_duplicate_when_match_is_a_complete_line(self):
        t = FakeTransport([b"one\ntwo\nTARGET\n"])
        r = LineReader(t)
        got = r.drain_to("TARGET", 1.0)
        self.assertEqual(got, "one\ntwo\nTARGET")

    def test_match_in_unterminated_tail_not_duplicated_either(self):
        t = FakeTransport([b"one\n===FAU-PS1==="])
        r = LineReader(t)
        got = r.drain_to("===FAU-PS1===", 1.0)
        self.assertEqual(got, "one\n===FAU-PS1===")


class TestOversizedLineGuard(unittest.TestCase):
    def test_does_not_grow_unbounded_without_a_newline(self):
        # A garbage-spewing peer that never sends '\n' must not be allowed
        # to grow the buffer forever.
        t = FakeTransport([b"x" * 10000])
        r = LineReader(t, line_max=100)
        r.poll(1.0)
        self.assertLessEqual(len(r.partial()), 100)


if __name__ == "__main__":
    unittest.main()
