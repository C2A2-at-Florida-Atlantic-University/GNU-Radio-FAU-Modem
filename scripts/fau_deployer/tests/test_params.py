#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""core/params.py: the Flowgraph Params field."""

import os
import tempfile
import unittest

from ..core import params
from ..core.params import ParamError

GENERATED_WITH_OPTIONS = '''\
from argparse import ArgumentParser

def argument_parser():
    parser = ArgumentParser()
    parser.add_argument("--nco-freq", dest="nco_freq", type=float, default=1e6)
    parser.add_argument("-s", "--samp-rate", dest="samp_rate", type=float)
    return parser
'''

# What grcc actually emits for a flowgraph with no GRC Parameter blocks:
# argparse is imported and then never used at all.
GENERATED_NO_OPTIONS = '''\
from argparse import ArgumentParser
import sys

def main(top_block_cls=None, options=None):
    pass
'''

NOT_A_FLOWGRAPH = "x = 1\n"
UNPARSEABLE = "def (:\n"


class TempPy(unittest.TestCase):
    def _write(self, text, name="fg.py"):
        d = tempfile.mkdtemp(prefix="fau_params_")
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path


class TestSplit(unittest.TestCase):
    def test_empty_is_no_args(self):
        self.assertEqual(params.split_params(""), [])
        self.assertEqual(params.split_params("   "), [])
        self.assertEqual(params.split_params(None), [])

    def test_quoted_value_stays_one_argument(self):
        self.assertEqual(params.split_params('--label "two words"'),
                         ["--label", "two words"])

    def test_shell_metacharacters_are_data_not_syntax(self):
        # These must survive as literal argv tokens. core/runner.py re-quotes
        # each one, so nothing here can be reinterpreted by the board's
        # shell -- which is the entire point of splitting on the desktop.
        self.assertEqual(
            params.split_params("--tag a;b --glob '*.bin' --price '$5'"),
            ["--tag", "a;b", "--glob", "*.bin", "--price", "$5"])

    def test_unbalanced_quote_is_a_param_error_not_a_valueerror(self):
        with self.assertRaises(ParamError) as ctx:
            params.split_params('--label "unclosed')
        self.assertIn("unbalanced quote", str(ctx.exception))


class TestFlowgraphOptions(TempPy):
    def test_reads_add_argument_flags(self):
        path = self._write(GENERATED_WITH_OPTIONS)
        self.assertEqual(params.flowgraph_options(path),
                         {"--nco-freq", "-s", "--samp-rate"})

    def test_argparse_imported_but_unused_means_no_options_not_unknown(self):
        # The distinction that matters: an EMPTY set (this flowgraph takes
        # no options, so any param is wrong) versus None (could not tell,
        # do not validate). grcc emits exactly this shape for a flowgraph
        # with no Parameter blocks.
        path = self._write(GENERATED_NO_OPTIONS)
        self.assertEqual(params.flowgraph_options(path), set())

    def test_no_argparse_at_all_is_unknown(self):
        path = self._write(NOT_A_FLOWGRAPH)
        self.assertIsNone(params.flowgraph_options(path))

    def test_unparseable_file_is_unknown_not_an_exception(self):
        path = self._write(UNPARSEABLE)
        self.assertIsNone(params.flowgraph_options(path))

    def test_missing_file_is_unknown_not_an_exception(self):
        self.assertIsNone(params.flowgraph_options("/nonexistent/fg.py"))


class TestUnknownFlags(unittest.TestCase):
    def test_none_known_disables_validation(self):
        self.assertEqual(params.unknown_flags(["--anything"], None), [])

    def test_flags_checked_values_ignored(self):
        known = {"--nco-freq"}
        self.assertEqual(
            params.unknown_flags(["--nco-freq", "1e6"], known), [])

    def test_equals_form_checks_the_name_only(self):
        self.assertEqual(
            params.unknown_flags(["--nco-freq=1e6"], {"--nco-freq"}), [])
        self.assertEqual(
            params.unknown_flags(["--nope=1"], {"--nco-freq"}), ["--nope"])

    def test_double_dash_ends_option_checking(self):
        self.assertEqual(
            params.unknown_flags(["--", "--not-a-flag"], set()), [])

    def test_bare_dash_is_not_a_flag(self):
        self.assertEqual(params.unknown_flags(["-"], set()), [])


class TestCheck(TempPy):
    def test_good_params_pass_through(self):
        path = self._write(GENERATED_WITH_OPTIONS)
        self.assertEqual(params.check("--nco-freq 2e6", path),
                         ["--nco-freq", "2e6"])

    def test_error_names_the_legal_options(self):
        path = self._write(GENERATED_WITH_OPTIONS)
        with self.assertRaises(ParamError) as ctx:
            params.check("--nco-frequency 2e6", path)
        msg = str(ctx.exception)
        self.assertIn("--nco-frequency", msg)
        self.assertIn("--nco-freq", msg)  # what it should have been

    def test_params_for_a_flowgraph_with_no_parameters_are_rejected(self):
        # Without this the flowgraph would silently ignore them: with no
        # parser built, extra argv never reaches argparse at all.
        path = self._write(GENERATED_NO_OPTIONS)
        with self.assertRaises(ParamError) as ctx:
            params.check("--nco-freq 2e6", path)
        self.assertIn("no GRC Parameter blocks", str(ctx.exception))

    def test_no_flowgraph_given_skips_validation(self):
        self.assertEqual(params.check("--whatever 1", None), ["--whatever", "1"])


if __name__ == "__main__":
    unittest.main()
