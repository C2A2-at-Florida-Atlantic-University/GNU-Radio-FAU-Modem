#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""core/boards.py: bitstreams.json and credentials.json.

Two jobs. The first is the validator's own behaviour. The second is keeping
the HAND-WRITTEN validator honest against the JSON Schemas: this package is
stdlib-only, so unlike ~/repos/Unified-FAU-Modem-Test-Tooling it does not
generate schemas from Pydantic models, and without a check the two would
drift. Those tests use `jsonschema` when importable and skip otherwise, so
it stays a development convenience rather than a runtime requirement.
"""

import json
import os
import tempfile
import unittest

from ..core import boards
from ..core.boards import BoardsError

try:
    import jsonschema
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False

_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_DIR = os.path.join(_PKG, "json", "schemas")
BITSTREAMS_SCHEMA = os.path.join(SCHEMA_DIR, "bitstreams.schema.json")
CREDENTIALS_SCHEMA = os.path.join(SCHEMA_DIR, "credentials.schema.json")
EXAMPLE_BITSTREAMS = os.path.join(_PKG, "json", "example_bitstreams.json")
EXAMPLE_CREDENTIALS = os.path.join(_PKG, "json", "example_credentials.json")

MINIMAL = {
    "S10": {
        "bootstrap": "/home/petalinux/firmware/Radio_Top_v2_wrapper.bit.bin",
        "tx": "/home/petalinux/S10_dac.bit.bin",
        "rx": "/home/petalinux/S10_adc.bit.bin",
    }
}


def _write(obj, name="bitstreams.json"):
    d = tempfile.mkdtemp(prefix="fau_bits_")
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    return path


def _copy():
    return json.loads(json.dumps(MINIMAL))


class TestTrackedFiles(unittest.TestCase):
    """The files shipped in the repo must load. A hand-edit that breaks them
    should fail here, not in front of a board."""

    def test_bitstreams_json_loads(self):
        bits = boards.load_bitstreams()
        self.assertTrue(bits.names)
        for name in bits.names:
            self.assertTrue(bits.board(name).modes)

    def test_records_nothing_about_reaching_a_board(self):
        # Ports, baud and destination directories are per-session facts. A
        # port in this file is a port that goes stale and deploys to the
        # wrong board, so the format has no place to put one.
        with open(boards.BITSTREAMS_PATH_DEFAULT, encoding="utf-8") as fh:
            raw = json.load(fh)
        forbidden = {"serial_port", "port", "baud", "baud_rate", "dest",
                     "user", "password"}
        for name, entry in raw.items():
            self.assertFalse(
                forbidden & set(entry),
                "%s carries connection/credential keys: %s"
                % (name, sorted(forbidden & set(entry))))

    def test_every_path_looks_like_a_loadable_binary(self):
        # fpgautil's FPGA-manager path wants the bootgen-processed form; a
        # raw Vivado .bit is a different file and will not load.
        bits = boards.load_bitstreams()
        for name in bits.names:
            board = bits.board(name)
            for mode in board.modes:
                self.assertTrue(
                    board.bitstream(mode).endswith(".bit.bin"),
                    "%s.%s is not a .bit.bin" % (name, mode))

    def test_example_files_load(self):
        boards.load_bitstreams(EXAMPLE_BITSTREAMS)
        creds = boards.load_credentials(EXAMPLE_CREDENTIALS)
        self.assertEqual(creds["S10"].user, "petalinux")

    def test_missing_credentials_file_is_a_normal_state(self):
        # credentials.json is gitignored, so a fresh checkout has none and
        # the tool still has to work.
        creds = boards.load_credentials("/nonexistent/credentials.json")
        self.assertEqual(creds, {})
        fallback = boards.creds_for(creds, "S10")
        self.assertEqual(fallback.user, boards.USER_DEFAULT)
        self.assertEqual(fallback.password, boards.PASSWORD_DEFAULT)

    def test_missing_credentials_file_can_be_demanded(self):
        with self.assertRaises(BoardsError) as ctx:
            boards.load_credentials("/nonexistent/credentials.json",
                                    required=True)
        self.assertIn("example_credentials.json", str(ctx.exception))


@unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
class TestSchemaConformance(unittest.TestCase):
    def _validate(self, doc_path, schema_path):
        with open(doc_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        with open(schema_path, encoding="utf-8") as fh:
            schema = json.load(fh)
        jsonschema.validate(doc, schema)

    def test_schemas_are_themselves_valid(self):
        for path in (BITSTREAMS_SCHEMA, CREDENTIALS_SCHEMA):
            with open(path, encoding="utf-8") as fh:
                jsonschema.Draft202012Validator.check_schema(json.load(fh))

    def test_tracked_bitstreams_matches_its_schema(self):
        self._validate(boards.BITSTREAMS_PATH_DEFAULT, BITSTREAMS_SCHEMA)

    def test_examples_match_their_schemas(self):
        self._validate(EXAMPLE_BITSTREAMS, BITSTREAMS_SCHEMA)
        self._validate(EXAMPLE_CREDENTIALS, CREDENTIALS_SCHEMA)

    def test_examples_also_pass_the_hand_written_validator(self):
        boards.load_bitstreams(EXAMPLE_BITSTREAMS)
        boards.load_credentials(EXAMPLE_CREDENTIALS)

    def test_the_schema_rejects_what_the_loader_rejects(self):
        # Sampled both-ways agreement on the cases most likely to drift.
        with open(BITSTREAMS_SCHEMA, encoding="utf-8") as fh:
            schema = json.load(fh)
        bad = [
            {},                                    # no boards
            {"S10": {}},                           # no bitstreams
            {"S10": {"tx": None}},                 # null instead of omitted
            {"S10": {"tx": ""}},                   # empty path
            {"S10": {"tx": 17}},                   # not a string
            {"S10": "path.bit.bin"},               # board is not an object
        ]
        for obj in bad:
            with self.subTest(obj=obj):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(obj, schema)
                with self.assertRaises(BoardsError):
                    boards.load_bitstreams(_write(obj))


class TestLoad(unittest.TestCase):
    def test_minimal_file(self):
        bits = boards.load_bitstreams(_write(MINIMAL))
        board = bits.board("S10")
        self.assertEqual(board.name, "S10")
        self.assertEqual(board.bitstream("tx"),
                         "/home/petalinux/S10_dac.bit.bin")

    def test_bootstrap_is_offered_first_then_the_rest_alphabetically(self):
        # A stable order, so a menu does not reshuffle depending on how
        # somebody happened to type the file.
        bits = boards.load_bitstreams(_write(MINIMAL))
        self.assertEqual(bits.board("S10").modes, ["bootstrap", "rx", "tx"])

    def test_order_is_stable_regardless_of_file_order(self):
        obj = {"S10": {"tx": "/a.bit.bin", "rx": "/b.bit.bin",
                       "bootstrap": "/c.bit.bin"}}
        bits = boards.load_bitstreams(_write(obj))
        self.assertEqual(bits.board("S10").modes, ["bootstrap", "rx", "tx"])

    def test_a_board_without_bootstrap_is_fine(self):
        obj = {"S10": {"tx": "/a.bit.bin"}}
        bits = boards.load_bitstreams(_write(obj))
        self.assertEqual(bits.board("S10").modes, ["tx"])

    def test_boards_keep_file_order(self):
        # The order they are written is the order they are offered, so the
        # first listed is the default selection.
        obj = {"S20": {"rx": "/a.bit.bin"}, "S10": {"tx": "/b.bit.bin"}}
        self.assertEqual(boards.load_bitstreams(_write(obj)).names,
                         ["S20", "S10"])

    def test_one_bitstream_is_the_default_mode(self):
        obj = {"S10": {"tx": "/a.bit.bin"}}
        bits = boards.load_bitstreams(_write(obj))
        self.assertEqual(bits.board("S10").default_mode, "tx")

    def test_several_bitstreams_have_no_default(self):
        bits = boards.load_bitstreams(_write(MINIMAL))
        self.assertIsNone(bits.board("S10").default_mode)

    def test_unknown_mode_names_what_is_available(self):
        bits = boards.load_bitstreams(_write(MINIMAL))
        with self.assertRaises(BoardsError) as ctx:
            bits.board("S10").bitstream("bootloader")
        self.assertIn("bootstrap", str(ctx.exception))

    def test_unknown_board_names_what_is_available(self):
        bits = boards.load_bitstreams(_write(MINIMAL))
        with self.assertRaises(BoardsError) as ctx:
            bits.board("S99")
        self.assertIn("S10", str(ctx.exception))


class TestLoadSequence(unittest.TestCase):
    """bootstrap is the base design a board is brought up on, so asking for
    anything else loads it first."""

    def setUp(self):
        self.bits = boards.load_bitstreams(_write(MINIMAL))
        self.board = self.bits.board("S10")

    def test_a_mode_loads_bootstrap_first(self):
        self.assertEqual(
            self.board.load_sequence("tx"),
            [("bootstrap", MINIMAL["S10"]["bootstrap"]),
             ("tx", MINIMAL["S10"]["tx"])])

    def test_the_same_holds_for_rx(self):
        names = [n for n, _ in self.board.load_sequence("rx")]
        self.assertEqual(names, ["bootstrap", "rx"])

    def test_bootstrap_itself_is_one_step(self):
        # It is the destination, not a prelude to itself.
        self.assertEqual(self.board.load_sequence("bootstrap"),
                         [("bootstrap", MINIMAL["S10"]["bootstrap"])])

    def test_a_board_without_bootstrap_loads_the_mode_alone(self):
        # Absence is how the format says "this board has no such thing", so
        # a self-contained design stays expressible. The CALLER warns.
        bits = boards.load_bitstreams(_write({"S10": {"tx": "/a.bit.bin"}}))
        board = bits.board("S10")
        self.assertFalse(board.has_bootstrap)
        self.assertEqual(board.load_sequence("tx"), [("tx", "/a.bit.bin")])

    def test_has_bootstrap_reports_the_truth(self):
        self.assertTrue(self.board.has_bootstrap)

    def test_an_identical_path_is_not_loaded_twice(self):
        # A second full PL reconfiguration with the same file changes
        # nothing and costs a reload.
        bits = boards.load_bitstreams(
            _write({"S10": {"bootstrap": "/same.bit.bin",
                            "tx": "/same.bit.bin"}}))
        self.assertEqual(bits.board("S10").load_sequence("tx"),
                         [("bootstrap", "/same.bit.bin")])

    def test_an_unknown_mode_still_raises(self):
        with self.assertRaises(BoardsError):
            self.board.load_sequence("bootloader")


class TestValidation(unittest.TestCase):
    def _expect(self, obj, needle):
        with self.assertRaises(BoardsError) as ctx:
            boards.load_bitstreams(_write(obj))
        self.assertIn(needle, str(ctx.exception))

    def test_missing_file(self):
        with self.assertRaises(BoardsError) as ctx:
            boards.load_bitstreams("/nonexistent/bitstreams.json")
        self.assertIn("no bitstream file", str(ctx.exception))

    def test_not_json(self):
        d = tempfile.mkdtemp(prefix="fau_bits_")
        path = os.path.join(d, "bitstreams.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(BoardsError) as ctx:
            boards.load_bitstreams(path)
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_no_boards(self):
        self._expect({}, "defines no boards")

    def test_board_with_no_bitstreams(self):
        self._expect({"S10": {}}, "can never be loaded")

    def test_board_that_is_not_an_object(self):
        self._expect({"S10": "/path.bit.bin"}, "must be an object")

    def test_null_path_says_to_omit_the_key_instead(self):
        # No nulls and no placeholders: absence is how a board says it has
        # no such mode, so there is nothing to keep in step.
        self._expect({"S10": {"tx": None}}, "omit the key entirely")

    def test_empty_path(self):
        self._expect({"S10": {"tx": ""}}, "non-empty string")

    def test_non_string_path(self):
        self._expect({"S10": {"tx": 17}}, "non-empty string")


class TestCredentialsValidation(unittest.TestCase):
    def _expect(self, obj, needle):
        path = _write(obj, "credentials.json")
        with self.assertRaises(BoardsError) as ctx:
            boards.load_credentials(path)
        self.assertIn(needle, str(ctx.exception))

    def test_missing_password(self):
        self._expect({"S10": {"user": "petalinux"}}, "password")

    def test_non_string_password(self):
        self._expect({"S10": {"user": "petalinux", "password": 1234}},
                     "must be a string")

    def test_unknown_key(self):
        # Catches being handed the reference tooling's own credentials.json,
        # which also carries ntfy and psu sections.
        self._expect({"S10": {"user": "u", "password": "p", "ntfy": "x"}},
                     "unknown field(s) 'ntfy'")

    def test_only_listed_boards_override_the_default(self):
        path = _write({"S10": {"user": "u", "password": "p"}},
                      "credentials.json")
        creds = boards.load_credentials(path)
        self.assertEqual(creds["S10"].user, "u")
        self.assertEqual(boards.creds_for(creds, "S20").user,
                         boards.USER_DEFAULT)


if __name__ == "__main__":
    unittest.main()
