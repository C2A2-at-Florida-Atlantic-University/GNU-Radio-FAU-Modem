#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Reading a `.grc` as YAML, and the preflight gates that run on it.

A `.grc` is plain YAML with a stable, small shape:

    options:     {parameters: {generate_options: qt_gui, ...}, states: {...}}
    blocks:      [{name: <instance id>, id: <block type>,
                   parameters: {...}, states: {state: enabled|disabled|...}}]
    connections: [[src_name, src_port, dst_name, dst_port], ...]
    metadata:    {file_format: 1}

Note the inversion that trips everyone up once: a block's ``id`` is its
*type* (``qtgui_time_sink_x``) and its ``name`` is the *instance* the
connection list refers to. Everything here follows the file's naming, so
``Block.id`` is the type and ``Block.name`` is the instance.

Parsing the YAML rather than going through ``gnuradio.grc.core`` is
deliberate (see docs/plans/grc-deployer-plan.md, "Headless transform"):
strip, flip and constant-swap are all pure-YAML operations, and the lighter
coupling means preflight still runs — and stays testable — on a machine
where GNU Radio itself is not importable. Only ``grcc`` needs the real
install, and that is one subprocess at the end (core/generate.py).

PyYAML is imported lazily by ``load()``. The transport half of this package
is stdlib-only apart from pyserial and deploys a pre-generated ``.py``
perfectly well without ever touching this module; a desktop that can run
``grcc`` at all necessarily has PyYAML, since GNU Radio itself needs it.
"""

import os

# The one file_format this module has been read against. GRC writes it into
# `metadata`. A bump means the schema moved under us, and mis-parsing a
# flowgraph silently is much worse than refusing it -- the failure would
# land as a stripped block or a frozen variable nobody asked for.
FILE_FORMAT_SUPPORTED = 1

FAU_SOURCE = "fau_modem_fau_source"
FAU_SINK = "fau_modem_fau_sink"

# Roles, matching the mode keys in bitstreams.json. Which board a flowgraph
# belongs on is decided by the FAU block it contains, because TX and RX are
# two physically separate boards: fau_sink drives the DAC (7010), fau_source
# reads the ADC (7020).
TARGET_TX = "tx"
TARGET_RX = "rx"

# `states.state` values GRC writes. Anything not in this set means the block
# is excluded from generation, so preflight and the transform must both skip
# it -- a disabled qtgui sink is not a reason to refuse, and a disabled
# fau_sink does not make a flowgraph a TX flowgraph.
_ENABLED_STATES = (True, "enabled", "true", "True")


class GrcError(ValueError):
    """A `.grc` that cannot be used. The message is written to be shown to
    an operator as-is, so callers should not wrap or re-word it."""


class Block:
    """One entry of the `blocks:` list, with the raw dict kept alongside.

    The raw dict is the one the transform mutates: keeping it means a
    round trip through this module preserves every key GRC wrote, including
    the `states` coordinates, so a derived .grc still opens in GRC and reads
    as a recognisable copy of the original rather than a reconstruction.
    """

    def __init__(self, raw):
        self.raw = raw
        self.name = str(raw.get("name", ""))
        self.id = str(raw.get("id", ""))
        self.parameters = raw.get("parameters") or {}

    @property
    def enabled(self):
        states = self.raw.get("states") or {}
        return states.get("state", "enabled") in _ENABLED_STATES

    def param(self, key, default=""):
        value = self.parameters.get(key, default)
        return "" if value is None else str(value)

    def __repr__(self):
        return "Block(%s=%s)" % (self.name, self.id)


class Connection:
    """One `connections:` entry: [src_name, src_port, dst_name, dst_port].

    Ports are strings in the file: '0' for the first stream port, but a
    name like 'pdu' or 'in' for a message port. Nothing here converts them
    to ints, because `is_message` is the only distinction that matters and
    a numeric-looking string is exactly what tells you it is a stream.
    """

    def __init__(self, raw):
        self.raw = raw
        self.src, self.src_port, self.dst, self.dst_port = (
            str(x) for x in raw[:4])

    @property
    def is_message(self):
        return not (self.src_port.isdigit() and self.dst_port.isdigit())

    def as_list(self):
        return [self.src, self.src_port, self.dst, self.dst_port]

    def __repr__(self):
        return "Connection(%s:%s -> %s:%s)" % (
            self.src, self.src_port, self.dst, self.dst_port)


class Flowgraph:
    """A parsed `.grc`, plus the raw document it came from."""

    def __init__(self, doc, path=None):
        self.doc = doc
        self.path = path
        self.blocks = [Block(b) for b in (doc.get("blocks") or [])]
        self.connections = [Connection(c) for c in (doc.get("connections") or [])
                            if isinstance(c, (list, tuple)) and len(c) >= 4]

    # -- options ------------------------------------------------------
    @property
    def options(self):
        opts = self.doc.get("options")
        return opts if isinstance(opts, dict) else {}

    @property
    def option_params(self):
        params = self.options.get("parameters")
        return params if isinstance(params, dict) else {}

    @property
    def generate_options(self):
        return str(self.option_params.get("generate_options", "qt_gui"))

    @property
    def run_options(self):
        return str(self.option_params.get("run_options", "prompt"))

    @property
    def flowgraph_id(self):
        """The `id` options parameter -- what grcc names the generated file
        (`<id>.py`) and the top_block class. Falls back to the file stem,
        which is what GRC itself defaults to for an unnamed flowgraph."""
        got = str(self.option_params.get("id", "")).strip()
        if got:
            return got
        if self.path:
            return os.path.splitext(os.path.basename(self.path))[0]
        return "top_block"

    @property
    def title(self):
        return str(self.option_params.get("title", "")).strip()

    # -- lookups ------------------------------------------------------
    @property
    def enabled_blocks(self):
        return [b for b in self.blocks if b.enabled]

    def by_name(self, name):
        for b in self.blocks:
            if b.name == name:
                return b
        return None

    def of_type(self, *type_ids):
        wanted = set(type_ids)
        return [b for b in self.enabled_blocks if b.id in wanted]


def load(path):
    """Parse `path` into a Flowgraph, checking the file_format first.

    Raises GrcError for anything an operator could have caused: a missing
    file, YAML that does not parse, a document that is not a flowgraph, or
    a file_format this module has not been read against.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover -- needs a yaml-less host
        raise GrcError(
            "reading a .grc needs PyYAML, which is not importable here (%s). "
            "Generate the .py yourself with `grcc -o DIR %s` and deploy that "
            "instead." % (exc, os.path.basename(path)))

    try:
        with open(path, "r") as fh:
            doc = yaml.safe_load(fh)
    except OSError as exc:
        raise GrcError("cannot read %s: %s" % (path, exc))
    except yaml.YAMLError as exc:
        raise GrcError("%s is not valid YAML, so it is not a readable .grc: %s"
                       % (os.path.basename(path), exc))

    if not isinstance(doc, dict) or "blocks" not in doc:
        raise GrcError("%s does not look like a GRC flowgraph (no `blocks:` "
                       "section)" % os.path.basename(path))

    check_file_format(doc, path)
    return Flowgraph(doc, path)


def check_file_format(doc, path=None):
    """Version guard. Fails loudly on an unexpected file_format instead of
    parsing a schema this module has never seen."""
    meta = doc.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    got = meta.get("file_format", doc.get("file_format"))
    name = os.path.basename(path) if path else "flowgraph"
    if got is None:
        raise GrcError(
            "%s declares no `metadata: file_format:`. Either it is not a "
            ".grc or it predates the format this tool reads (%d); open and "
            "re-save it in GRC." % (name, FILE_FORMAT_SUPPORTED))
    if got != FILE_FORMAT_SUPPORTED:
        raise GrcError(
            "%s is GRC file_format %s; this tool has only been read against "
            "%d. Refusing rather than guessing -- mis-parsing a flowgraph "
            "would quietly strip or freeze the wrong thing. Update "
            "core/grcfile.py once the new format has been checked."
            % (name, got, FILE_FORMAT_SUPPORTED))


def fau_blocks(fg):
    """(sources, sinks) -- the enabled FAU Modem blocks, by instance."""
    return (fg.of_type(FAU_SOURCE), fg.of_type(FAU_SINK))


def check_fau_present(fg):
    """Gate 1. A flowgraph with no FAU Modem block has no reason to be on a
    board: the deployer exists to ship flowgraphs that drive the DMA, and
    anything else is far more likely a wrong-file mistake than an intent.

    Returns the (sources, sinks) it found so a caller can reuse them.
    """
    sources, sinks = fau_blocks(fg)
    if sources or sinks:
        return sources, sinks
    disabled = [b.id for b in fg.blocks
                if b.id in (FAU_SOURCE, FAU_SINK) and not b.enabled]
    if disabled:
        raise GrcError(
            "the only FAU Modem block in this flowgraph (%s) is disabled, so "
            "the generated .py would not touch the DMA at all. Enable it in "
            "GRC, or deploy a different flowgraph." % ", ".join(sorted(set(disabled))))
    raise GrcError(
        "no FAU Modem Source or Sink in this flowgraph. The deployer ships "
        "flowgraphs that drive the board's AXI DMA; one without a FAU block "
        "would run just as well on this desktop, so this is almost certainly "
        "the wrong file.")


def infer_target(fg):
    """Which board role this flowgraph is for, from its FAU blocks.

    fau_sink drives the DAC and fau_source reads the ADC, and those live on
    two physically separate boards -- so a flowgraph holding both cannot run
    anywhere and is rejected here rather than half-failing on the board.
    """
    sources, sinks = check_fau_present(fg)
    if sources and sinks:
        raise GrcError(
            "this flowgraph has both a FAU Source (%s) and a FAU Sink (%s). "
            "TX and RX are two separate boards, so no single board can run "
            "it -- split it into a TX flowgraph and an RX flowgraph."
            % (", ".join(b.name for b in sources),
               ", ".join(b.name for b in sinks)))
    return TARGET_RX if sources else TARGET_TX


def describe(fg):
    """Short one-line-per-fact summary, for the preflight banner."""
    sources, sinks = fau_blocks(fg)
    enabled = fg.enabled_blocks
    return [
        ("flowgraph", fg.flowgraph_id),
        ("title", fg.title or "(none)"),
        ("generate_options", fg.generate_options),
        ("run_options", fg.run_options),
        ("blocks", "%d enabled, %d total"
                   % (len(enabled), len(fg.blocks))),
        ("connections", str(len(fg.connections))),
        ("FAU blocks", ", ".join(b.name for b in sources + sinks) or "(none)"),
    ]
