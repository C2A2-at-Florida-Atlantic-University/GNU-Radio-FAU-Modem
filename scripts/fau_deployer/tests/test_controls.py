#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Tests for core/controls.py: the five block parsers and the wire format.

These need no GNU Radio and no board -- a control spec is read off YAML the
same way the rest of the Process phase reads a .grc. The one thing they do
check against the real install is that the parsers agree with the block
definitions GRC actually ships, which TestAgainstInstalledBlocks does by
reading /usr/share/gnuradio/grc/blocks and skipping where it is absent.
"""

import json
import os
import unittest

from ..core import controls
from ..core.controls import ControlError

try:
    import jsonschema
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "json", "schemas", "ui_spec.schema.json")

GRC_BLOCK_DIR = "/usr/share/gnuradio/grc/blocks"


class FakeBlock:
    """The bit of grcfile.Block the parsers touch."""

    def __init__(self, name, type_id, parameters, enabled=True):
        self.name = name
        self.id = type_id
        self.parameters = dict(parameters)
        self.enabled = enabled

    def param(self, key, default=""):
        value = self.parameters.get(key, default)
        return "" if value is None else str(value)


class FakeFlowgraph:
    def __init__(self, blocks):
        self.blocks = list(blocks)

    @property
    def enabled_blocks(self):
        return [b for b in self.blocks if b.enabled]


def a_range(name="gain", **over):
    params = {"label": "Gain", "rangeType": '"float"', "value": "50",
              "start": "0", "stop": "100", "step": "0.5",
              "widget": "counter_slider"}
    params.update(over)
    return FakeBlock(name, "variable_qtgui_range", params)


def an_entry(name="freq", **over):
    params = {"label": "Freq", "type": "real", "value": "1e6"}
    params.update(over)
    return FakeBlock(name, "variable_qtgui_entry", params)


def a_chooser(name="mode", **over):
    params = {"label": "Mode", "type": "string", "num_opts": "0",
              "options": "['a', 'b', 'c']", "labels": "['A', 'B', 'C']",
              "value": "'b'", "widget": "combo_box"}
    params.update(over)
    return FakeBlock(name, "variable_qtgui_chooser", params)


def a_check_box(name="armed", **over):
    params = {"label": "Armed", "type": "int", "value": "0",
              "true": "1", "false": "0"}
    params.update(over)
    return FakeBlock(name, "variable_qtgui_check_box", params)


def a_button(name="fire", **over):
    params = {"label": "Fire", "type": "int", "value": "0",
              "pressed": "1", "released": "0"}
    params.update(over)
    return FakeBlock(name, "variable_qtgui_push_button", params)


def extract_one(block):
    found, warnings = controls.extract(FakeFlowgraph([block]))
    return found[0], warnings


class TestRange(unittest.TestCase):
    def test_reads_bounds_and_widget(self):
        c, warnings = extract_one(a_range())
        self.assertEqual(warnings, [])
        self.assertEqual((c.id, c.kind, c.label, c.dtype),
                         ("gain", "range", "Gain", "float"))
        self.assertEqual(c.default, 50.0)
        self.assertEqual(c.extra["start"], 0.0)
        self.assertEqual(c.extra["stop"], 100.0)
        self.assertEqual(c.extra["step"], 0.5)
        self.assertEqual(c.extra["widget"], "counter_slider")
        self.assertFalse(c.free_entry)

    def test_int_range_stays_int(self):
        c, _ = extract_one(a_range(rangeType='"int"', value="5", step="1"))
        self.assertEqual(c.dtype, "int")
        self.assertIsInstance(c.default, int)
        self.assertIsInstance(c.extra["start"], int)

    def test_unquoted_range_type_also_works(self):
        # GRC writes '"float"' but a hand-edited file may hold a bare word.
        c, _ = extract_one(a_range(rangeType="int"))
        self.assertEqual(c.dtype, "int")

    def test_expression_bound_degrades_to_free_entry(self):
        c, warnings = extract_one(a_range(stop="samp_rate"))
        self.assertTrue(c.free_entry)
        self.assertNotIn("stop", c.extra)
        self.assertTrue(any("cannot evaluate" in w for w in warnings))
        # The control is still offered -- degrade and report, never drop.
        self.assertEqual(c.id, "gain")

    def test_value_outside_bounds_is_clamped_and_reported(self):
        c, warnings = extract_one(a_range(value="500"))
        self.assertEqual(c.default, 100.0)
        self.assertTrue(any("outside its own" in w for w in warnings))

    def test_reversed_bounds_are_swapped_and_reported(self):
        c, warnings = extract_one(a_range(start="100", stop="0", value="50"))
        self.assertEqual((c.extra["start"], c.extra["stop"]), (0.0, 100.0))
        self.assertTrue(any("start > stop" in w for w in warnings))

    def test_unknown_widget_falls_back(self):
        c, _ = extract_one(a_range(widget="hologram"))
        self.assertEqual(c.extra["widget"], "counter_slider")

    def test_blank_label_falls_back_to_the_id(self):
        c, _ = extract_one(a_range(label=""))
        self.assertEqual(c.label, "gain")

    def test_quoted_label_is_unwrapped(self):
        c, _ = extract_one(a_range(label="'Gain (dB)'"))
        self.assertEqual(c.label, "Gain (dB)")


class TestEntry(unittest.TestCase):
    def test_real_entry(self):
        c, warnings = extract_one(an_entry())
        self.assertEqual(warnings, [])
        self.assertEqual((c.kind, c.dtype, c.default),
                         ("entry", "float", 1e6))

    def test_bool_entry_warns_about_the_stock_conversion(self):
        c, warnings = extract_one(an_entry(type="bool", value="True"))
        self.assertEqual(c.dtype, "bool")
        self.assertTrue(any("bool(str)" in w for w in warnings))
        self.assertIn("checkbox", c.note)

    def test_raw_entry(self):
        c, _ = extract_one(an_entry(type="raw", value="[1, 2]"))
        self.assertEqual(c.dtype, "raw")
        self.assertEqual(c.default, [1, 2])

    def test_expression_default_degrades(self):
        c, _ = extract_one(an_entry(value="samp_rate / 4"))
        self.assertTrue(c.free_entry)
        self.assertIsNone(c.default)


class TestChooser(unittest.TestCase):
    def test_list_mode(self):
        c, warnings = extract_one(a_chooser())
        self.assertEqual(warnings, [])
        self.assertEqual(c.extra["options"], ["a", "b", "c"])
        self.assertEqual(c.extra["labels"], ["A", "B", "C"])
        self.assertEqual(c.default, "b")

    def test_numbered_mode(self):
        c, _ = extract_one(a_chooser(
            num_opts="3", option0="10", option1="20", option2="30",
            label0="Low", label1="Mid", label2="High", type="int",
            value="20"))
        self.assertEqual(c.extra["options"], [10, 20, 30])
        self.assertEqual(c.extra["labels"], ["Low", "Mid", "High"])
        self.assertEqual(c.default, 20)

    def test_blank_label_becomes_the_option(self):
        c, _ = extract_one(a_chooser(labels="['A', '', 'C']"))
        self.assertEqual(c.extra["labels"], ["A", "b", "C"])

    def test_missing_labels_become_the_options(self):
        c, _ = extract_one(a_chooser(labels="[]"))
        self.assertEqual(c.extra["labels"], ["a", "b", "c"])

    def test_default_not_in_options_is_reported(self):
        c, warnings = extract_one(a_chooser(value="'z'"))
        self.assertEqual(c.default, "a")
        self.assertTrue(any("not one of its options" in w for w in warnings))

    def test_tuple_options_degrade_rather_than_lie(self):
        # A tuple would come back from JSON as a list and never compare
        # equal to what the flowgraph holds.
        c, warnings = extract_one(a_chooser(options="[(1, 2), (3, 4)]",
                                            value="(1, 2)", labels="[]"))
        self.assertTrue(c.free_entry)
        self.assertTrue(any("JSON" in w for w in warnings))


class TestCheckBoxAndButton(unittest.TestCase):
    def test_check_box_carries_both_values(self):
        c, warnings = extract_one(a_check_box())
        self.assertEqual(warnings, [])
        self.assertEqual((c.extra["true_value"], c.extra["false_value"]),
                         (1, 0))

    def test_check_box_values_need_not_be_boolean(self):
        c, _ = extract_one(a_check_box(type="string", value="'off'",
                                       **{"true": "'on'", "false": "'off'"}))
        self.assertEqual(c.extra["true_value"], "on")
        self.assertEqual(c.default, "off")

    def test_check_box_default_matching_neither_is_reported(self):
        c, warnings = extract_one(a_check_box(value="7"))
        self.assertEqual(c.default, 0)
        self.assertTrue(any("neither its checked" in w for w in warnings))

    def test_push_button(self):
        c, warnings = extract_one(a_button())
        self.assertEqual(warnings, [])
        self.assertEqual((c.extra["pressed"], c.extra["released"]), (1, 0))
        self.assertEqual(c.default, 0)


class TestExtract(unittest.TestCase):
    def test_order_follows_the_flowgraph(self):
        fg = FakeFlowgraph([a_button(), a_range(), a_chooser()])
        found, _ = controls.extract(fg)
        self.assertEqual([c.id for c in found], ["fire", "gain", "mode"])

    def test_disabled_blocks_are_skipped(self):
        block = a_range()
        block.enabled = False
        found, _ = controls.extract(FakeFlowgraph([block]))
        self.assertEqual(found, [])

    def test_non_control_blocks_are_ignored(self):
        other = FakeBlock("samp_rate", "variable", {"value": "200e3"})
        label = FakeBlock("title", "variable_qtgui_label",
                          {"label": "T", "value": "'x'"})
        found, _ = controls.extract(FakeFlowgraph([other, label, a_range()]))
        self.assertEqual([c.id for c in found], ["gain"])

    def test_duplicate_ids_are_refused(self):
        # grcc would emit one set_gain for both, so which one a widget
        # drives is undefined.
        with self.assertRaises(ControlError) as cm:
            controls.extract(FakeFlowgraph([a_range(), a_range()]))
        self.assertIn("both define the variable", str(cm.exception))


class TestEngNotation(unittest.TestCase):
    def test_matches_the_examples_in_gnuradios_docstring(self):
        self.assertEqual(controls.str_to_num("15m"), 15e-3)
        self.assertEqual(controls.str_to_num("1.5M"), 1.5e6)
        self.assertEqual(controls.str_to_num("2k"), 2000.0)
        self.assertEqual(controls.str_to_num("400000"), 400000.0)

    def test_rejects_junk(self):
        for bad in ("", "1.5Mx", "abc", "M"):
            with self.assertRaises(ValueError):
                controls.str_to_num(bad)

    @unittest.skipUnless(
        os.path.isdir(GRC_BLOCK_DIR), "GNU Radio block definitions not here")
    def test_agrees_with_the_installed_eng_notation(self):
        try:
            from gnuradio import eng_notation
        except ImportError:
            self.skipTest("gnuradio not importable")
        for text in ("15m", "1.5M", "2k", "400000", "1e6", "-3.5u"):
            self.assertEqual(controls.str_to_num(text),
                             eng_notation.str_to_num(text), text)


class TestCoerce(unittest.TestCase):
    def test_entry_float_takes_engineering_notation(self):
        c, _ = extract_one(an_entry())
        self.assertEqual(controls.coerce_value(c, "1.5M"), 1.5e6)

    def test_range_float_does_not(self):
        # A slider is numeric input; reading "1m" off one as a milli-unit
        # would be a surprise, and GRC does not do it either.
        c, _ = extract_one(a_range())
        with self.assertRaises(ControlError):
            controls.coerce_value(c, "1m")
        self.assertEqual(controls.coerce_value(c, "12.5"), 12.5)

    def test_int_accepts_hex(self):
        c, _ = extract_one(a_range(rangeType='"int"', value="5", stop="4096"))
        self.assertEqual(controls.coerce_value(c, "0x20"), 32)

    def test_bool_words(self):
        c, _ = extract_one(an_entry(type="bool", value="True"))
        self.assertIs(controls.coerce_value(c, "false"), False)
        self.assertIs(controls.coerce_value(c, "on"), True)
        with self.assertRaises(ControlError):
            controls.coerce_value(c, "maybe")

    def test_raw_refuses_an_expression(self):
        c, _ = extract_one(an_entry(type="raw", value="1"))
        self.assertEqual(controls.coerce_value(c, "[1, 'a']"), [1, "a"])
        with self.assertRaises(ControlError) as cm:
            controls.coerce_value(c, "__import__('os').system('x')")
        self.assertIn("not a Python literal", str(cm.exception))

    def test_chooser_rejects_a_value_off_the_list(self):
        c, _ = extract_one(a_chooser())
        self.assertEqual(controls.coerce_value(c, "a"), "a")
        with self.assertRaises(ControlError):
            controls.coerce_value(c, "z")


class TestWireFormat(unittest.TestCase):
    def test_set_line_round_trips_through_the_board_parser(self):
        import ast
        for value in (1, 2.5, "text", True, None, [1, 2], -0.0):
            line = controls.wire_set("gain", value)
            verb, cid, rest = line.split(None, 2)
            self.assertEqual((verb, cid), ("SET", "gain"))
            self.assertEqual(ast.literal_eval(rest), value)

    def test_pulse_line(self):
        self.assertEqual(controls.wire_pulse("fire", 50), "PULSE fire 50")

    def test_parse_reply_requires_the_right_nonce(self):
        line = "FAU-CTL-abc123 OK gain 0.5"
        self.assertIsNone(controls.parse_reply(line, "zzzzzz"))
        reply = controls.parse_reply(line, "abc123")
        self.assertTrue(reply.ok)
        self.assertEqual((reply.id, reply.value), ("gain", 0.5))

    def test_parse_reply_err(self):
        reply = controls.parse_reply(
            "FAU-CTL-abc123 ERR gain set_gain(0.5) raised ValueError: bad",
            "abc123")
        self.assertEqual(reply.kind, "ERR")
        self.assertEqual(reply.id, "gain")
        self.assertIn("ValueError", reply.text)

    def test_parse_reply_ready(self):
        reply = controls.parse_reply("FAU-CTL-abc123 READY gain mode",
                                     "abc123")
        self.assertEqual(reply.kind, "READY")
        self.assertEqual(reply.text, "gain mode")

    def test_parse_reply_tolerates_a_prefix(self):
        # The console prepends whatever was in flight when the line landed.
        reply = controls.parse_reply(
            "noise FAU-CTL-abc123 OK gain 1", "abc123")
        self.assertTrue(reply.ok)

    def test_plain_output_is_not_a_reply(self):
        self.assertIsNone(controls.parse_reply("underruns: 0", "abc123"))

    def test_echo_detection(self):
        ids = frozenset(["gain", "mode"])
        self.assertTrue(controls.is_control_echo("SET gain 0.5", ids))
        self.assertTrue(controls.is_control_echo("PULSE mode 50", ids))
        self.assertFalse(controls.is_control_echo("SET other 1", ids))
        self.assertFalse(controls.is_control_echo("setting gain to 5", ids))


class TestSpecFile(unittest.TestCase):
    def setUp(self):
        fg = FakeFlowgraph([a_range(), an_entry(), a_chooser(),
                            a_check_box(), a_button()])
        self.found, _ = controls.extract(fg)
        self.spec = controls.build_spec("demo", self.found)

    def test_round_trip_through_json(self):
        text = json.dumps(self.spec)
        fg_id, back = controls.parse_spec(json.loads(text))
        self.assertEqual(fg_id, "demo")
        self.assertEqual([c.id for c in back], [c.id for c in self.found])
        for before, after in zip(self.found, back):
            self.assertEqual(before.to_dict(), after.to_dict())

    def test_a_wrong_version_is_refused(self):
        spec = dict(self.spec, version=99)
        with self.assertRaises(ControlError) as cm:
            controls.parse_spec(spec)
        self.assertIn("version", str(cm.exception))

    def test_an_unknown_kind_is_refused(self):
        spec = json.loads(json.dumps(self.spec))
        spec["controls"][0]["kind"] = "hologram"
        with self.assertRaises(ControlError):
            controls.parse_spec(spec)

    def test_write_and_load(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(prefix="fau_spec_"),
                            controls.SPEC_FILENAME)
        controls.write_spec(self.spec, path)
        fg_id, back = controls.load_spec(path)
        self.assertEqual(fg_id, "demo")
        self.assertEqual(len(back), 5)

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_validates_against_the_schema(self):
        with open(SCHEMA_PATH) as fh:
            schema = json.load(fh)
        jsonschema.validate(self.spec, schema)

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_a_free_entry_control_also_validates(self):
        with open(SCHEMA_PATH) as fh:
            schema = json.load(fh)
        found, _ = controls.extract(FakeFlowgraph([a_range(stop="x")]))
        jsonschema.validate(controls.build_spec("demo", found), schema)

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_the_schema_is_itself_valid(self):
        with open(SCHEMA_PATH) as fh:
            jsonschema.Draft202012Validator.check_schema(json.load(fh))

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_the_schema_rejects_a_bad_id(self):
        with open(SCHEMA_PATH) as fh:
            schema = json.load(fh)
        spec = json.loads(json.dumps(self.spec))
        spec["controls"][0]["id"] = "not an identifier"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(spec, schema)


class TestAgainstInstalledBlocks(unittest.TestCase):
    """The parsers read fields off GRC's own block definitions. If a field
    this module names is not in the shipped YAML, the parser is reading
    something that does not exist and every value it produces is a default.
    """

    def _params(self, filename):
        path = os.path.join(GRC_BLOCK_DIR, filename)
        if not os.path.isfile(path):
            self.skipTest("%s not installed" % filename)
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        with open(path) as fh:
            doc = yaml.safe_load(fh)
        return doc, {p["id"] for p in doc.get("parameters", [])}

    def test_range_fields_exist(self):
        doc, ids = self._params("qtgui_range.block.yml")
        self.assertEqual(doc["id"], "variable_qtgui_range")
        for field in ("label", "rangeType", "value", "start", "stop",
                      "step", "widget"):
            self.assertIn(field, ids)
        widget = next(p for p in doc["parameters"] if p["id"] == "widget")
        self.assertEqual(tuple(widget["options"]), controls.RANGE_WIDGETS)

    def test_entry_fields_exist(self):
        doc, ids = self._params("qtgui_entry.block.yml")
        for field in ("label", "type", "value"):
            self.assertIn(field, ids)
        kinds = next(p for p in doc["parameters"] if p["id"] == "type")
        for option in kinds["options"]:
            self.assertIn(option, controls.DTYPES, option)

    def test_chooser_fields_exist(self):
        doc, ids = self._params("qtgui_chooser.block.yml")
        for field in ("options", "labels", "num_opts", "value", "widget",
                      "option0", "label0", "option4", "label4"):
            self.assertIn(field, ids)
        widget = next(p for p in doc["parameters"] if p["id"] == "widget")
        self.assertEqual(tuple(widget["options"]), controls.CHOOSER_WIDGETS)

    def test_check_box_fields_exist(self):
        _doc, ids = self._params("qtgui_check_box.block.yml")
        for field in ("label", "type", "value", "true", "false"):
            self.assertIn(field, ids)

    def test_push_button_fields_exist(self):
        _doc, ids = self._params("qtgui_push_button.block.yml")
        for field in ("label", "type", "value", "pressed", "released"):
            self.assertIn(field, ids)

    def test_every_in_scope_block_compiles_to_one_setter_call(self):
        """The fact the whole design rests on: these five do exactly
        `self.set_<id>(...)` and nothing else, so driving one remotely is
        the stock code path rather than an emulation."""
        for filename in ("qtgui_range.block.yml", "qtgui_entry.block.yml",
                         "qtgui_chooser.block.yml",
                         "qtgui_check_box.block.yml",
                         "qtgui_push_button.block.yml"):
            doc, _ids = self._params(filename)
            make = doc["templates"]["make"]
            self.assertIn("self.set_${id}", make, filename)


if __name__ == "__main__":
    unittest.main()
