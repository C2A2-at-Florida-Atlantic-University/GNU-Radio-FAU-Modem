#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Console output helpers, matching the house style used throughout this
repo's reference drivers and examples (see e.g.
components/layers/meta-fau-modem/gr-fau_modem/examples/fau_tx_common.py's
step()): bare print(), bracketed subsystem tags, '=== SECTION ===' banners
with column-aligned key : value lines. Deliberately no `logging` module.
"""

import sys

BANNER_WIDTH = 67  # matches scripts/extract_gnuradio.sh's 67 '=' separators


def say(tag, msg):
    """print("[tag] msg") -- the standard line shape for progress output."""
    print("[%s] %s" % (tag, msg))


def say_err(tag, msg):
    print("[%s] %s" % (tag, msg), file=sys.stderr)


def banner(title):
    """A '=== TITLE ===' section header, padded to BANNER_WIDTH like the
    extract_gnuradio.sh '=' separators this mirrors."""
    print("=" * BANNER_WIDTH)
    print(title)
    print("=" * BANNER_WIDTH)


def kv(key, value, width=22):
    """One column-aligned 'key : value' line inside a banner section."""
    print("  %-*s : %s" % (width, key, value))


def warn(msg):
    print("  WARNING: %s" % msg, file=sys.stderr)


def error(msg):
    print("  ERROR: %s" % msg, file=sys.stderr)


def die(msg, code=1):
    error(msg)
    raise SystemExit(code)
