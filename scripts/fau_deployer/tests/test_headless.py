#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""core/headless.py: turning a GUI flowgraph into one the board can run.

The transform is the only part of the tool that rewrites the operator's
work, so the properties worth pinning down are the ones that would be
silent if they broke: the original document is untouched, a frozen variable
keeps its value, and an output port that loses its only consumer gets a
null sink -- without which GNU Radio refuses the topology outright and the
whole exercise fails on the board rather than here.
"""

import copy
import shutil
import tempfile
import unittest

from . import grc_fixtures as fx
from ..core import grcfile, headless
from ..core.grcfile import GrcError


@unittest.skipUnless(fx.HAVE_YAML, "PyYAML not installed")
class HeadlessTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fau_headless_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _fg(self, document):
        return grcfile.load(fx.write(document, self.dir))

    def _transform(self, document, **kw):
        doc, rep = headless.transform(self._fg(document), **kw)
        return grcfile.Flowgraph(doc), rep

    @staticmethod
    def _names(fg):
        return [b.name for b in fg.blocks]

    @staticmethod
    def _types(fg):
        return {b.name: b.id for b in fg.blocks}


class TestClassification(unittest.TestCase):
    def _classify(self, type_id):
        return headless.classify(grcfile.Block({"name": "x", "id": type_id}))

    def test_the_four_known_categories(self):
        self.assertEqual(self._classify("qtgui_time_sink_x"), "sink")
        self.assertEqual(self._classify("qtgui_tab_widget"), "decoration")
        self.assertEqual(self._classify("variable_qtgui_range"), "variable")
        self.assertEqual(self._classify("variable_qtgui_msg_push_button"),
                         "message_only")

    def test_an_ordinary_block_is_not_a_gui_block(self):
        self.assertIsNone(self._classify("blocks_file_sink"))
        self.assertIsNone(self._classify("fau_modem_fau_source"))

    def test_an_unknown_qt_widget_is_caught_by_its_name(self):
        # Waving it through would mean grcc emits code importing Qt, which
        # the board image does not have -- the exact failure the transform
        # exists to prevent.
        self.assertEqual(self._classify("qtgui_something_new"), "unknown_gui")
        self.assertEqual(self._classify("variable_qtgui_whatever"),
                         "unknown_gui")

    def test_a_label_freezes_rather_than_being_deleted(self):
        # It looks like decoration but it still defines an id other blocks
        # can reference.
        self.assertEqual(self._classify("variable_qtgui_label"), "variable")


class TestOptions(HeadlessTestCase):
    def test_generate_options_becomes_no_gui(self):
        fg, rep = self._transform(fx.rx_with_gui())
        self.assertEqual(fg.generate_options, "no_gui")
        self.assertTrue(any(c.block == "generate_options" for c in rep.changes))

    def test_run_options_is_forced_to_run(self):
        # 'prompt' generates input('Press Enter to quit'), and the
        # flowgraph's stdin is the serial console the deployer is holding:
        # console noise would quit it out from under a live DMA.
        fg, rep = self._transform(fx.rx_with_gui())
        self.assertEqual(fg.run_options, "run")
        change = [c for c in rep.changes if c.block == "run_options"][0]
        self.assertIn("stdin", change.detail)

    def test_an_already_headless_flowgraph_reports_no_option_changes(self):
        document = fx.doc([fx.fau_source()], generate_options="no_gui",
                          run_options="run")
        _fg, rep = self._transform(document)
        self.assertEqual(rep.changes, [])


class TestStrip(HeadlessTestCase):
    def test_gui_sinks_and_decoration_are_removed(self):
        fg, _rep = self._transform(fx.rx_with_gui())
        names = self._names(fg)
        for gone in ("time_sink", "num_sink", "tabs"):
            self.assertNotIn(gone, names)

    def test_real_blocks_survive(self):
        fg, _rep = self._transform(fx.rx_with_gui())
        names = self._names(fg)
        for kept in ("fau_rx", "file_sink", "to_mag", "samp_rate"):
            self.assertIn(kept, names)

    def test_connections_to_removed_blocks_go_with_them(self):
        fg, _rep = self._transform(fx.rx_with_gui())
        for c in fg.connections:
            self.assertNotIn(c.dst, ("time_sink", "num_sink"))

    def test_the_source_document_is_not_mutated(self):
        # The operator's file is the one thing that must come out the other
        # side identical; the derived copy is what gets rewritten.
        document = fx.rx_with_gui()
        original = copy.deepcopy(document)
        self._transform(document)
        self.assertEqual(document, original)


class TestFreeze(HeadlessTestCase):
    def test_a_gui_range_becomes_a_variable_at_its_current_value(self):
        fg, rep = self._transform(fx.rx_with_gui())
        nco = fg.by_name("nco")
        self.assertEqual(nco.id, "variable")
        self.assertEqual(nco.param("value"), "120e3")
        self.assertEqual([c.block for c in rep.frozen], ["nco"])

    def test_the_freeze_is_reported_with_the_value_it_froze_at(self):
        _fg, rep = self._transform(fx.rx_with_gui())
        self.assertIn("120e3", rep.frozen[0].detail)

    def test_the_comment_records_what_it_used_to_be(self):
        # The generated .py carries this comment, so somebody reading the
        # deployed file can see the knob was frozen and did not vanish.
        fg, _rep = self._transform(fx.rx_with_gui())
        self.assertIn("variable_qtgui_range",
                      fg.by_name("nco").param("comment"))

    def test_a_frozen_variable_keeps_its_name_so_references_still_resolve(self):
        fg, _rep = self._transform(fx.rx_with_gui())
        self.assertIsNotNone(fg.by_name("nco"))


class TestNullSinkSplice(HeadlessTestCase):
    def test_a_port_that_loses_its_only_consumer_gets_a_null_sink(self):
        fg, rep = self._transform(fx.rx_with_gui())
        spliced = [c for c in rep.changes if c.kind == "spliced"]
        self.assertEqual(len(spliced), 1)
        name = spliced[0].block
        self.assertEqual(self._types(fg)[name], "blocks_null_sink")
        self.assertIn([("to_mag", "0", name, "0")],
                      [[tuple(c.as_list())] for c in fg.connections])

    def test_the_null_sinks_type_comes_from_the_sink_it_replaces(self):
        # A wrong item size is an immediate ValueError from GNU Radio's
        # connect(), so getting it from the removed sink's own declared
        # type is what keeps that from happening at all.
        fg, _rep = self._transform(fx.rx_with_gui())
        null = [b for b in fg.blocks if b.id == "blocks_null_sink"][0]
        self.assertEqual(null.param("type"), "float")

    def test_a_port_with_a_surviving_consumer_gets_nothing(self):
        # fau_rx also feeds file_sink, so stripping time_sink orphans
        # nothing there.
        fg, _rep = self._transform(fx.rx_with_gui())
        fed_by_fau = [c.dst for c in fg.connections if c.src == "fau_rx"]
        self.assertNotIn("blocks_null_sink",
                         [self._types(fg)[d] for d in fed_by_fau])

    def test_two_gui_sinks_on_one_port_splice_only_one(self):
        document = fx.doc([
            fx.fau_source(),
            fx.time_sink("t1"),
            fx.time_sink("t2"),
        ], [("fau_rx", "0", "t1", "0"), ("fau_rx", "0", "t2", "0")])
        fg, rep = self._transform(document)
        self.assertEqual(len([c for c in rep.changes if c.kind == "spliced"]), 1)
        self.assertEqual(
            len([b for b in fg.blocks if b.id == "blocks_null_sink"]), 1)

    def test_a_message_connection_needs_no_splice(self):
        # An unconnected message port is legal in GNU Radio; only stream
        # ports have to be filled.
        document = fx.doc([
            fx.fau_source(),
            fx.block("probe", "blocks_message_debug", {}),
            fx.time_sink("t1"),
        ], [("probe", "pdu", "t1", "in")])
        _fg, rep = self._transform(document)
        self.assertEqual([c for c in rep.changes if c.kind == "spliced"], [])

    def test_the_spliced_name_does_not_collide_with_an_existing_block(self):
        document = fx.doc([
            fx.fau_source(),
            fx.block("fau_null_sink_fau_rx_0", "variable", {"value": "1"}),
            fx.time_sink("t1"),
        ], [("fau_rx", "0", "t1", "0")])
        fg, _rep = self._transform(document)
        names = self._names(fg)
        self.assertEqual(len(names), len(set(names)))


class TestMessageControls(HeadlessTestCase):
    def _doc(self):
        return fx.doc([fx.fau_source(), fx.msg_button()])

    def test_a_message_control_refuses_by_default(self):
        with self.assertRaises(GrcError) as cm:
            self._transform(self._doc())
        message = str(cm.exception)
        self.assertIn("ping", message)
        self.assertIn("never fire", message)

    def test_with_consent_it_is_stripped_and_warned_about(self):
        fg, rep = self._transform(self._doc(), allow_message_controls=True)
        self.assertNotIn("ping", self._names(fg))
        self.assertTrue(any("ping" in w for w in rep.warnings))

    def test_a_control_that_is_both_variable_and_message_keeps_its_value(self):
        # variable_qtgui_toggle_switch defines an id other blocks reference
        # AND emits messages: the variable freezes, the messages cannot.
        document = fx.doc([
            fx.fau_source(),
            fx.block("sw", "variable_qtgui_toggle_switch",
                     {"label": "On", "type": "int", "value": "1",
                      "pressed": "1", "released": "0"}),
        ])
        fg, rep = self._transform(document, allow_message_controls=True)
        sw = fg.by_name("sw")
        self.assertIsNotNone(sw)
        self.assertEqual(sw.id, "variable")
        self.assertEqual(sw.param("value"), "1")
        self.assertTrue(any("sw" in w for w in rep.warnings))

    def test_a_disabled_message_control_does_not_trigger_the_refusal(self):
        document = fx.doc([
            fx.fau_source(),
            fx.block("ping", "variable_qtgui_msg_push_button", {},
                     state="disabled"),
        ])
        self._transform(document)  # must not raise


class TestUnknownGuiBlock(HeadlessTestCase):
    def test_an_unrecognised_qt_widget_stops_the_transform(self):
        document = fx.doc([fx.fau_source(),
                           fx.block("w", "qtgui_brand_new_widget", {})])
        with self.assertRaises(GrcError) as cm:
            self._transform(document)
        self.assertIn("qtgui_brand_new_widget", str(cm.exception))


class TestThrottle(HeadlessTestCase):
    def test_a_throttle_is_flagged_but_left_alone(self):
        document = fx.doc([fx.fau_source(),
                           fx.block("thr", "blocks_throttle",
                                    {"samples_per_second": "200e3"})])
        fg, rep = self._transform(document)
        self.assertIn("thr", self._names(fg))
        self.assertTrue(any("Throttle" in w for w in rep.warnings))


class TestWrite(HeadlessTestCase):
    def test_the_derived_file_reloads_as_a_flowgraph(self):
        doc, _rep = headless.transform(self._fg(fx.rx_with_gui()))
        path = headless.write(doc, self.dir + "/out/derived.headless.grc")
        again = grcfile.load(path)
        self.assertEqual(again.generate_options, "no_gui")
        self.assertIsNotNone(again.by_name("fau_rx"))


if __name__ == "__main__":
    unittest.main()
