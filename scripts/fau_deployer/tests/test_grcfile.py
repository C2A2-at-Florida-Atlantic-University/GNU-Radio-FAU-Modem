#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""core/grcfile.py: reading a .grc, and the preflight gates.

These gates are the cheap half of not deploying the wrong thing to the
wrong board, so they are tested for what they REFUSE at least as much as
for what they accept -- a gate that never fires is indistinguishable from
one that is not there.
"""

import shutil
import tempfile
import unittest

from . import grc_fixtures as fx
from ..core import grcfile
from ..core.grcfile import GrcError


@unittest.skipUnless(fx.HAVE_YAML, "PyYAML not installed")
class GrcFileTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fau_grcfile_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _load(self, document, name=None):
        return grcfile.load(fx.write(document, self.dir, name))


class TestLoad(GrcFileTestCase):
    def test_reads_options_blocks_and_connections(self):
        fg = self._load(fx.rx_with_gui())
        self.assertEqual(fg.flowgraph_id, "rx_demo")
        self.assertEqual(fg.generate_options, "qt_gui")
        self.assertEqual(fg.run_options, "prompt")
        self.assertEqual(len(fg.blocks), 8)
        self.assertEqual(len(fg.connections), 4)

    def test_block_id_is_the_type_and_name_is_the_instance(self):
        # The inversion that trips everyone up once, so it gets a test.
        fg = self._load(fx.rx_with_gui())
        fau = fg.by_name("fau_rx")
        self.assertEqual(fau.id, "fau_modem_fau_source")
        self.assertEqual(fau.name, "fau_rx")

    def test_a_missing_file_is_an_operator_error_not_a_traceback(self):
        with self.assertRaises(GrcError) as cm:
            grcfile.load(self.dir + "/nope.grc")
        self.assertIn("cannot read", str(cm.exception))

    def test_yaml_that_is_not_a_flowgraph_is_refused(self):
        path = self.dir + "/plain.grc"
        with open(path, "w") as fh:
            fh.write("just: a mapping\n")
        with self.assertRaises(GrcError) as cm:
            grcfile.load(path)
        self.assertIn("blocks", str(cm.exception))

    def test_unparseable_yaml_names_the_file(self):
        path = self.dir + "/bad.grc"
        with open(path, "w") as fh:
            fh.write("blocks: [\n  unclosed\n")
        with self.assertRaises(GrcError) as cm:
            grcfile.load(path)
        self.assertIn("bad.grc", str(cm.exception))


class TestVersionGuard(GrcFileTestCase):
    def test_a_future_file_format_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(GrcError) as cm:
            self._load(fx.doc([fx.fau_source()], file_format=2))
        self.assertIn("file_format", str(cm.exception))
        self.assertIn("2", str(cm.exception))

    def test_no_file_format_at_all_is_refused(self):
        document = fx.doc([fx.fau_source()])
        del document["metadata"]
        with self.assertRaises(GrcError):
            self._load(document)


class TestEnabledState(GrcFileTestCase):
    def test_grcs_true_and_enabled_both_mean_enabled(self):
        document = fx.doc([fx.block("a", "variable", {"value": "1"}, state=True),
                           fx.block("b", "variable", {"value": "2"},
                                    state="enabled")])
        fg = self._load(document)
        self.assertEqual(len(fg.enabled_blocks), 2)

    def test_disabled_blocks_are_excluded(self):
        document = fx.doc([fx.fau_source(),
                           fx.block("off", "variable", {"value": "1"},
                                    state="disabled")])
        fg = self._load(document)
        self.assertEqual([b.name for b in fg.enabled_blocks], ["fau_rx"])


class TestGate1(GrcFileTestCase):
    def test_a_flowgraph_with_a_fau_block_passes(self):
        fg = self._load(fx.rx_with_gui())
        sources, sinks = grcfile.check_fau_present(fg)
        self.assertEqual([b.name for b in sources], ["fau_rx"])
        self.assertEqual(sinks, [])

    def test_no_fau_block_is_refused_as_the_wrong_file(self):
        document = fx.doc([fx.block("v", "variable", {"value": "1"})])
        with self.assertRaises(GrcError) as cm:
            grcfile.check_fau_present(self._load(document))
        self.assertIn("no FAU Modem", str(cm.exception))

    def test_a_disabled_fau_block_says_so_specifically(self):
        # Otherwise the operator reads "no FAU block" and goes looking for
        # one in a flowgraph that visibly has it.
        document = fx.doc([fx.block("fau_rx", "fau_modem_fau_source", {},
                                    state="disabled")])
        with self.assertRaises(GrcError) as cm:
            grcfile.check_fau_present(self._load(document))
        self.assertIn("disabled", str(cm.exception))


class TestTargetInference(GrcFileTestCase):
    def test_a_source_makes_it_an_rx_flowgraph(self):
        self.assertEqual(grcfile.infer_target(self._load(fx.rx_with_gui())),
                         grcfile.TARGET_RX)

    def test_a_sink_makes_it_a_tx_flowgraph(self):
        fg = self._load(fx.doc([fx.fau_sink()]))
        self.assertEqual(grcfile.infer_target(fg), grcfile.TARGET_TX)

    def test_both_in_one_flowgraph_is_refused(self):
        # TX and RX are two physically separate boards, so this cannot run
        # anywhere -- better to say so here than half-fail on hardware.
        fg = self._load(fx.doc([fx.fau_source(), fx.fau_sink()]))
        with self.assertRaises(GrcError) as cm:
            grcfile.infer_target(fg)
        message = str(cm.exception)
        self.assertIn("fau_rx", message)
        self.assertIn("fau_tx", message)
        self.assertIn("separate boards", message)

    def test_a_disabled_sink_does_not_make_an_rx_flowgraph_ambiguous(self):
        fg = self._load(fx.doc([
            fx.fau_source(),
            fx.block("fau_tx", "fau_modem_fau_sink", {}, state="disabled")]))
        self.assertEqual(grcfile.infer_target(fg), grcfile.TARGET_RX)


class TestConnections(GrcFileTestCase):
    def test_numeric_ports_are_streams_and_named_ports_are_messages(self):
        fg = self._load(fx.doc([fx.fau_source()], [
            ("a", "0", "b", "0"),
            ("a", "pdu", "b", "in"),
        ]))
        self.assertFalse(fg.connections[0].is_message)
        self.assertTrue(fg.connections[1].is_message)


class TestDescribe(GrcFileTestCase):
    def test_describe_names_the_fau_blocks(self):
        pairs = dict(grcfile.describe(self._load(fx.rx_with_gui())))
        self.assertEqual(pairs["flowgraph"], "rx_demo")
        self.assertEqual(pairs["FAU blocks"], "fau_rx")


if __name__ == "__main__":
    unittest.main()
