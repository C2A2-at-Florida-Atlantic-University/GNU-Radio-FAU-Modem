#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Payload assembly tests: sibling-import discovery, the rejections that
keep a payload sane, and determinism of the built archive."""

import base64
import gzip
import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from ..core import payload as P

EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))),
    "components", "layers", "meta-fau-modem", "gr-fau_modem", "examples")


class TestCollectRealExamples(unittest.TestCase):
    """Against the repo's own tx_sine.py -- the exact multi-file case this
    design exists for."""

    def test_discovers_the_sibling_module(self):
        entries, main_arc = P.collect_files(
            os.path.join(EXAMPLES_DIR, "tx_sine.py"))
        names = {arc for _, arc in entries}
        self.assertEqual(main_arc, "tx_sine.py")
        self.assertIn("fau_tx_common.py", names)
        self.assertEqual(names, {"tx_sine.py", "fau_tx_common.py"})

    def test_build_payload_round_trips_byte_identical(self):
        entries, main_arc = P.collect_files(
            os.path.join(EXAMPLES_DIR, "tx_sine.py"))
        pl = P.build_payload(entries, main_arc)

        gz = base64.b64decode(pl.b64)
        self.assertEqual(hashlib.sha256(gz).hexdigest(), pl.sha256)
        raw = gzip.decompress(gz)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
            members = {m.name: m for m in tf.getmembers()}
            self.assertEqual(set(members), {arc for _, arc in entries})
            for path, arc in entries:
                extracted = tf.extractfile(members[arc]).read()
                self.assertEqual(extracted, path.read_bytes())

    def test_deterministic_across_rebuilds(self):
        entries, main_arc = P.collect_files(
            os.path.join(EXAMPLES_DIR, "tx_sine.py"))
        pl1 = P.build_payload(entries, main_arc, xfer_id="fixed00")
        pl2 = P.build_payload(entries, main_arc, xfer_id="fixed00")
        self.assertEqual(pl1.sha256, pl2.sha256)
        self.assertEqual(pl1.gz_bytes, pl2.gz_bytes)


class TestCollectFilesRejections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, name, content=b"pass\n"):
        p = self.dir / name
        p.write_bytes(content)
        return p

    def test_missing_file_rejected(self):
        with self.assertRaises(P.PayloadError):
            P.collect_files(str(self.dir / "does_not_exist.py"))

    def test_directory_rejected(self):
        with self.assertRaises(P.PayloadError):
            P.collect_files(str(self.dir))

    def test_symlink_main_rejected(self):
        real = self._write("real.py")
        link = self.dir / "link.py"
        link.symlink_to(real)
        with self.assertRaises(P.PayloadError):
            P.collect_files(str(link))

    def test_symlink_extra_rejected(self):
        main = self._write("main.py")
        real = self._write("real.py")
        link = self.dir / "link.py"
        link.symlink_to(real)
        with self.assertRaises(P.PayloadError):
            P.collect_files(str(main), extra=[str(link)])

    def test_max_bytes_enforced(self):
        main = self._write("main.py", b"x" * 1000)
        with self.assertRaises(P.PayloadError):
            P.collect_files(str(main), max_bytes=100)

    def test_transitive_sibling_chain(self):
        # main -> a -> b: discovery must not stop at depth 1.
        self._write("b.py", b"VALUE = 1\n")
        self._write("a.py", b"import b\n")
        main = self._write("main.py", b"import a\n")
        entries, _ = P.collect_files(str(main))
        names = {arc for _, arc in entries}
        self.assertEqual(names, {"main.py", "a.py", "b.py"})

    def test_dotted_imports_are_not_treated_as_local(self):
        # `from gnuradio import fau_modem` must never be mistaken for a
        # sibling `gnuradio.py` -- it isn't one, and shouldn't be looked for.
        main = self._write("main.py", b"from gnuradio import fau_modem\n")
        entries, _ = P.collect_files(str(main))
        names = {arc for _, arc in entries}
        self.assertEqual(names, {"main.py"})

    def test_extra_file_included(self):
        main = self._write("main.py", b"pass\n")
        extra = self._write("data.txt", b"not python, just extra data\n")
        entries, _ = P.collect_files(str(main), extra=[str(extra)])
        names = {arc for _, arc in entries}
        self.assertEqual(names, {"main.py", "data.txt"})


class TestBuildPayloadRejections(unittest.TestCase):
    def test_chunk_size_must_be_multiple_of_four(self):
        with tempfile.TemporaryDirectory() as d:
            main = Path(d) / "main.py"
            main.write_text("pass\n")
            entries = [(main, "main.py")]
            with self.assertRaises(P.PayloadError):
                P.build_payload(entries, "main.py", chunk_size=13)

    def test_chunk_size_must_be_positive(self):
        with tempfile.TemporaryDirectory() as d:
            main = Path(d) / "main.py"
            main.write_text("pass\n")
            entries = [(main, "main.py")]
            with self.assertRaises(P.PayloadError):
                P.build_payload(entries, "main.py", chunk_size=0)


if __name__ == "__main__":
    unittest.main()
