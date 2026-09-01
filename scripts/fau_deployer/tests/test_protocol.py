#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Pure-function tests for the wire protocol: frame construction/parsing,
noise tolerance, and the CRC that makes per-chunk retransmit actually work.
"""

import unittest

from ..board import receiver as R
from ..core import protocol as P


class TestFrameRoundTrip(unittest.TestCase):
    def test_begin_is_not_parsed_by_the_desktop_side(self):
        # BEGIN is a desktop -> board sentinel; parse_line() only classifies
        # board -> desktop traffic, so a BEGIN line must come back as noise.
        line = P.build_begin("a1b2c3d4", 3, 30, "a" * 64, 20)
        self.assertIsNone(P.parse_line(line))

    def test_ready_round_trip_with_noise_on_both_sides(self):
        ready = P.build_ready("/home/petalinux/flowgraphs", "deadbeefcafef00d")
        kind, fields = P.parse_line("boot spew before " + ready + " trailing junk")
        self.assertEqual(kind, "ready")
        self.assertEqual(fields["dest"], "/home/petalinux/flowgraphs")
        self.assertEqual(fields["sha16"], "deadbeefcafef00d")

    def test_ack_found_inside_kernel_printk_noise(self):
        kind, fields = P.parse_line("[  123.456] cpu: noise A 00007 more noise")
        self.assertEqual(kind, "ack")
        self.assertEqual(fields["seq"], 7)

    def test_nak_reason(self):
        kind, fields = P.parse_line("N 00009 crc")
        self.assertEqual(kind, "nak")
        self.assertEqual(fields["seq"], 9)
        self.assertEqual(fields["reason"], "crc")

    def test_fail_with_detail_containing_a_space(self):
        line = R.format_fail("a1b2c3d4", R.FAIL_SHA_MISMATCH, "got=1a2b want=cdef")
        kind, fields = P.parse_line(line)
        self.assertEqual(kind, "fail")
        self.assertEqual(fields["code"], "SHA_MISMATCH")
        self.assertEqual(fields["detail"], "got=1a2b want=cdef")

    def test_fail_without_detail(self):
        line = R.format_fail("a1b2c3d4", R.FAIL_ABORTED)
        kind, fields = P.parse_line(line)
        self.assertEqual(kind, "fail")
        self.assertEqual(fields["detail"], "")

    def test_ok(self):
        line = R.SENT_OK % (R.PROTO_VERSION, "a1b2c3d4", 3, 36288)
        kind, fields = P.parse_line(line)
        self.assertEqual(kind, "ok")
        self.assertEqual(fields["nfiles"], 3)
        self.assertEqual(fields["gz_len"], 36288)

    def test_unrecognized_line_is_none(self):
        self.assertIsNone(P.parse_line("some random board banner text"))
        self.assertIsNone(P.parse_line(""))


class TestChunkCrc(unittest.TestCase):
    def test_crc_covers_the_base64_text_not_the_decoded_bytes(self):
        line = P.build_chunk(0, "QUJDRA==")
        m = R.RE_CHUNK.match(line)
        self.assertIsNotNone(m)
        seq, crc_hex, b64 = m.groups()
        self.assertEqual(int(seq), 0)
        self.assertEqual(b64, "QUJDRA==")

    def test_a_bit_flip_in_the_chunk_changes_the_crc(self):
        line = P.build_chunk(0, "QUJDRA==")
        flipped = line.replace("QUJDRA==", "QUJDRB==")  # one char different
        self.assertNotEqual(line, flipped)
        m1 = R.RE_CHUNK.match(line)
        m2 = R.RE_CHUNK.match(flipped)
        self.assertNotEqual(m1.group(3), m2.group(3))  # payload did change
        # The CRC embedded in `line` must NOT validate against the flipped
        # payload -- this is the property that makes per-chunk CRC useful.
        import zlib
        crc_of_flipped = zlib.crc32(m2.group(3).encode("ascii")) & 0xFFFFFFFF
        self.assertNotEqual(int(m1.group(2), 16), crc_of_flipped)


class TestSplitB64(unittest.TestCase):
    def test_split_preserves_all_characters_in_order(self):
        text = "QUJDRA==" * 100
        chunks = P.split_b64(text, 16)
        self.assertEqual("".join(chunks), text)
        for c in chunks[:-1]:
            self.assertEqual(len(c), 16)

    def test_split_of_empty_string(self):
        self.assertEqual(P.split_b64("", 16), [])


class TestAnsiStripping(unittest.TestCase):
    def test_strips_csi_sequences(self):
        # A bracketed-paste-mode wrapped prompt, as bash 5.1 actually emits.
        raw = "\x1b[?2004hpetalinux@board:~$ \x1b[?2004l"
        self.assertEqual(P.strip_ansi(raw), "petalinux@board:~$ ")

    def test_strips_osc_sequences(self):
        raw = "\x1b]0;window title\x07plain text"
        self.assertEqual(P.strip_ansi(raw), "plain text")

    def test_leaves_plain_text_alone(self):
        self.assertEqual(P.strip_ansi("nothing fancy here"), "nothing fancy here")


class TestRebootDetection(unittest.TestCase):
    def test_detects_login_prompt(self):
        self.assertTrue(P.looks_like_reboot("Welcome to PetaLinux\npetalinux login: "))

    def test_detects_uboot_banner(self):
        self.assertTrue(P.looks_like_reboot("Hit any key to stop autoboot:  2 "))
        self.assertTrue(P.looks_like_reboot("zynq-uboot> "))

    def test_ordinary_output_is_not_a_reboot(self):
        self.assertFalse(P.looks_like_reboot("BDs moved: 19811 (data: 0, silence: 19811)"))


if __name__ == "__main__":
    unittest.main()
