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

Every line goes through _emit(), which a front-end can redirect with
set_sink() -- the GUI needs these lines in its log pane rather than on a
stdout nobody is looking at, and the alternative (threading a reporter
object down through session/bootstrap/sender and every one of their tests)
is a far larger change for no gain. The sink is process-global on purpose:
core is only ever driven from one thread at a time (the CLI's main thread,
or the GUI's single worker thread -- never both, and never two workers),
so there is nothing here to make thread-local.
"""

import sys

BANNER_WIDTH = 67  # matches scripts/extract_gnuradio.sh's 67 '=' separators

_sink = None


def set_sink(fn):
    """Route every subsequent report line through `fn(text, is_err)` instead
    of print(). Pass None to go back to stdout/stderr. Returns the previous
    sink, so a caller can restore it (the CLI never needs to; a test does).

    `text` is the fully formatted line WITHOUT a trailing newline, exactly
    as print() would have received it -- so a sink can prepend a timestamp
    or tag it without having to re-parse anything.
    """
    global _sink
    prev = _sink
    _sink = fn
    return prev


def _emit(text, is_err=False):
    if _sink is not None:
        _sink(text, is_err)
    elif is_err:
        print(text, file=sys.stderr)
    else:
        print(text)


def say(tag, msg):
    """print("[tag] msg") -- the standard line shape for progress output."""
    _emit("[%s] %s" % (tag, msg))


def say_err(tag, msg):
    _emit("[%s] %s" % (tag, msg), is_err=True)


def banner(title):
    """A '=== TITLE ===' section header, padded to BANNER_WIDTH like the
    extract_gnuradio.sh '=' separators this mirrors."""
    _emit("=" * BANNER_WIDTH)
    _emit(title)
    _emit("=" * BANNER_WIDTH)


def kv(key, value, width=22):
    """One column-aligned 'key : value' line inside a banner section."""
    _emit("  %-*s : %s" % (width, key, value))


def blank():
    """A blank separator line -- goes through the sink like everything else,
    so a GUI log pane gets the same spacing the console does."""
    _emit("")


def warn(msg):
    _emit("  WARNING: %s" % msg, is_err=True)


def error(msg):
    _emit("  ERROR: %s" % msg, is_err=True)


def die(msg, code=1):
    """Report and exit. CLI-only -- a GUI front-end must never call this
    (SystemExit from a worker thread would silently kill the operation and
    leave the port open); it catches the underlying exception instead."""
    error(msg)
    raise SystemExit(code)
