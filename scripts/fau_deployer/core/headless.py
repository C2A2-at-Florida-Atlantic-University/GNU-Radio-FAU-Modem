#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Turn a GUI flowgraph into a headless one, on a derived copy.

The board image is headless: there is no X, no Qt and no `gr-qtgui`, so a
`qt_gui` flowgraph's generated `.py` dies on `from PyQt5 import Qt` before
it reaches a single block. "Make it headless" and "remove every GUI block"
are the same operation, not two -- the `no_gui` generator emits no widget
parent, so a leftover `qtgui_*` block fails code generation outright.

**The user's `.grc` is never touched.** Everything here works on a
deep-copied document written out as a separate `*.headless.grc`, which is
kept as an audit artifact: when a run behaves unlike it did in GRC, that
file is the thing to open and diff.

Three categories of GUI block, and they are not interchangeable (see
docs/plans/grc-deployer-plan.md):

1. **Sinks and decoration** -- pure consumers. Delete them and their
   connections. Removing a consumer can leave an upstream *output* port with
   nothing on it, which GNU Radio's topology check rejects outright, so any
   port orphaned this way gets a `blocks_null_sink` spliced onto it.
2. **Controls that define a variable** (`variable_qtgui_range` and friends).
   Other blocks reference their `id`, so they cannot be deleted; each is
   replaced by a plain `variable` block holding its current value. The
   operator-adjustability is genuinely lost, so every one is *reported*
   rather than silently swapped.
3. **Controls that emit messages** (`variable_qtgui_msg_push_button`,
   `qtgui_edit_box_msg`, the toggles). There is no constant to freeze a
   message into; headless, the message simply never fires, which is a
   behaviour change no default can be right about. These **refuse** unless
   the caller passes `allow_message_controls`.

`run_options` is forced to `run` as well, and that one is not cosmetic --
see FORCED_RUN_OPTIONS below.
"""

import copy
import os

from . import controls as controls_mod
from . import grcfile, report
from .grcfile import GrcError

# Block type ids, by what the transform has to do with them. Derived from
# the `value:` key and port domains that GRC's own block YAML declares for
# each, so the split follows the block definitions rather than a guess.
#
# A pure consumer: delete, and splice a null sink onto anything it orphans.
SINKS = frozenset([
    "qtgui_time_sink_x", "qtgui_freq_sink_x", "qtgui_waterfall_sink_x",
    "qtgui_const_sink_x", "qtgui_histogram_sink_x", "qtgui_number_sink",
    "qtgui_vector_sink_f", "qtgui_sink_x", "qtgui_time_raster_sink_x",
    "qtgui_eye_sink_x", "qtgui_auto_correlator_sink", "qtgui_bercurve_sink",
    "qtgui_compass", "qtgui_dialgauge", "qtgui_levelgauge",
    "qtgui_ledindicator", "qtgui_graphicitem",
    "variable_qtgui_azelplot", "variable_qtgui_distanceradar",
])

# Widgets with no ports at all -- layout and backdrop. Delete, splice
# nothing.
DECORATIONS = frozenset([
    "qtgui_tab_widget", "qtgui_grbackground", "qtgui_appbackground",
])

# Define a variable and nothing else: freeze to a `variable` block.
VARIABLE_CONTROLS = frozenset([
    "variable_qtgui_range", "variable_qtgui_chooser", "variable_qtgui_entry",
    "variable_qtgui_check_box", "variable_qtgui_push_button",
    # A label is decoration on screen but it still defines an id other
    # blocks can reference, so it freezes rather than being deleted.
    "variable_qtgui_label",
])

# Define a variable AND emit messages. The variable freezes fine; the
# messages are the part that cannot.
MESSAGE_VARIABLE_CONTROLS = frozenset([
    "variable_qtgui_toggle_switch", "variable_qtgui_toggle_button_msg",
    "variable_qtgui_msgcheckbox", "variable_qtgui_dial_control",
    "qtgui_msgdigitalnumbercontrol",
])

# Emit messages and define nothing: there is no value to keep.
MESSAGE_ONLY_CONTROLS = frozenset([
    "variable_qtgui_msg_push_button", "qtgui_edit_box_msg",
    "qtgui_graphicoverlay",
])

# Stream type of the port a sink consumes, for the ones whose block
# definition carries no `type` parameter to read it off. Anything absent
# from both falls back to complex with a warning -- a wrong item size is an
# immediate, explicit error from GNU Radio's connect(), not a silent
# corruption, so guessing loudly beats refusing to generate.
_SINK_DEFAULT_TYPE = {
    "qtgui_auto_correlator_sink": "complex",
    "qtgui_bercurve_sink": "float",
    "qtgui_compass": "float",
    "qtgui_vector_sink_f": "float",
}

# qtgui `type` parameter value -> blocks_null_sink `type` value. The
# `msg_*` variants describe a message port, which never needs a splice.
_NULL_SINK_TYPES = {
    "complex": "complex", "float": "float", "int": "int",
    "short": "short", "byte": "byte",
}

# A Throttle exists to stop a GUI flowgraph eating a core while a human
# watches it. On the board the DMA sets the rate, so a Throttle can only
# fight it -- at best wasted work, at worst an underrun. Flagged, not
# stripped: removing a block the operator put there on purpose is a bigger
# assumption than telling them about it.
THROTTLE_TYPES = frozenset(["blocks_throttle", "blocks_throttle2"])

HEADLESS_GENERATE_OPTIONS = "no_gui"

# `run_options: prompt` generates `input('Press Enter to quit: ')`. The
# flowgraph's stdin IS the serial console the deployer is holding, so any
# byte that lands there -- console noise, a stray newline, the operator
# typing -- would quit the flowgraph out from under a live DMA. `run` emits
# `tb.start(); tb.wait()` instead, leaving Ctrl-C (which core/runner.py
# sends, and which the generated handler turns into the tb.stop() teardown)
# as the only way to stop it. Not a preference.
#
# Known consequence, recorded so it is not rediscovered: under `run` the
# main thread is already inside top_block.wait() when SIGINT arrives, and
# grcc's generated handler calls tb.wait() again -- and each call spawns a
# fresh _top_block_waiter thread (gnuradio/gr/top_block.py). The teardown
# itself is unaffected, because it happens in the handler's tb.stop() ->
# each block's stop() override, and the shell's FAU-RC exit status still
# proves the process was reaped. It is a wart, not a hazard. The planned
# fix belongs to the live-flowgraph-controls work, whose fau_ctl.start()
# re-installs SIGINT as tb.stop() only -- see the plan doc.
FORCED_RUN_OPTIONS = "run"

NULL_SINK_ID = "blocks_null_sink"
VARIABLE_ID = "variable"
SNIPPET_ID = "snippet"

# The two lines that turn a frozen flowgraph back into a controllable one.
# Deliberately two: `fau_ctl` resolves everything by name at runtime
# (`getattr(tb, "set_" + id)`), so there is no generated mapping table and
# nothing in the .py knows the control channel exists beyond this. Logic
# kept out of generated code on purpose -- generated code cannot be unit
# tested, reads badly in a traceback, and needs a re-transform to change.
CTL_SNIPPET_NAME = "fau_ctl_snippet"
CTL_SNIPPET_SECTION = "main_after_start"

# `self`, NOT `tb`. A snippet body is emitted as the body of
#
#     def snipfcn_<name>(self):
#
# and called as `snipfcn_<name>(tb)` from `snippets_main_after_start(tb)`
# (grc/core/FlowGraph.py:116-117). So the top block arrives bound to the
# parameter `self`; `tb` is only the caller's name for it and is a local
# of main(), not a global. Writing `fau_ctl.start(tb)` -- which is what the
# plan doc's draft said -- generates code that compiles fine and raises
# NameError the instant the flowgraph starts on the board. GRC's own
# Snippet documentation says the same thing ("to reference a block, it
# should be identified as self.block"); this cost one grcc run to find and
# would have cost a board round trip to find later.
CTL_SNIPPET_CODE = "import fau_ctl\nfau_ctl.start(self)"


class Change:
    """One thing the transform did, as a category plus a sentence.

    Kept as data rather than printed on the spot so that a --dry-run, the
    GUI's confirmation dialog and the log can all render the same list
    without the transform knowing which of them is asking.
    """

    def __init__(self, kind, block, detail):
        self.kind = kind
        self.block = block
        self.detail = detail

    def __str__(self):
        return "%-9s %-24s %s" % (self.kind, self.block, self.detail)


class TransformReport:
    def __init__(self):
        self.changes = []
        self.warnings = []
        self.controls = []
        """The QT GUI input blocks found, as core/controls.Control.

        Carried on the report rather than returned alongside the document
        so that adding it did not change transform()'s arity. They are
        read off the ORIGINAL blocks, before the freeze destroys their
        bounds and labels -- which is the whole reason the spec file
        exists.
        """

    def change(self, kind, block, detail):
        self.changes.append(Change(kind, block, detail))

    def warn(self, text):
        self.warnings.append(text)

    @property
    def frozen(self):
        return [c for c in self.changes if c.kind == "froze"]

    def emit(self):
        """Print the whole report through core/report.py's sink."""
        if not self.changes and not self.warnings:
            report.say("headless", "nothing to change -- already headless")
            return
        for c in self.changes:
            report.say("headless", str(c))
        for w in self.warnings:
            report.warn(w)


def classify(block):
    """Which transform category `block` falls into, or None if it is not a
    GUI block at all.

    Known ids come from the tables above. An id this module has never seen
    but which is plainly a GUI block (the `qtgui_`/`variable_qtgui_` naming
    GRC uses) is classified from its shape instead of being waved through:
    guessing wrong here is recoverable and visible, whereas treating an
    unknown Qt widget as an ordinary block means `grcc` emits code that
    imports Qt and the whole point is lost.
    """
    bid = block.id
    if bid in SINKS:
        return "sink"
    if bid in DECORATIONS:
        return "decoration"
    if bid in VARIABLE_CONTROLS:
        return "variable"
    if bid in MESSAGE_VARIABLE_CONTROLS:
        return "message_variable"
    if bid in MESSAGE_ONLY_CONTROLS:
        return "message_only"
    if bid.startswith("qtgui_") or bid.startswith("variable_qtgui_"):
        return "unknown_gui"
    return None


def _null_sink_type(sink_block, report_obj):
    """The blocks_null_sink `type` for whatever fed `sink_block`."""
    declared = sink_block.param("type", "").strip()
    if declared.startswith("msg_"):
        return None  # a message port: nothing to splice
    if declared in _NULL_SINK_TYPES:
        return _NULL_SINK_TYPES[declared]
    default = _SINK_DEFAULT_TYPE.get(sink_block.id)
    if default:
        return default
    report_obj.warn(
        "%s (%s) declares no stream type this tool recognises (%r); the "
        "null sink spliced in its place assumes complex. If the item sizes "
        "disagree the flowgraph will say so on the board the moment it "
        "starts." % (sink_block.name, sink_block.id, declared))
    return "complex"


def _make_null_sink(name, type_name, vlen, coordinate):
    return {
        "name": name,
        "id": NULL_SINK_ID,
        "parameters": {
            "affinity": "",
            "alias": "",
            "bus_structure_sink": "[[0,],]",
            "comment": "spliced by fau_deployer: the GUI sink here was "
                       "stripped for headless",
            "num_inputs": "1",
            "type": type_name,
            "vlen": str(vlen),
        },
        "states": {
            "bus_sink": False,
            "bus_source": False,
            "bus_structure": None,
            "coordinate": list(coordinate),
            "rotation": 0,
            "state": "enabled",
        },
    }


def _unique_name(taken, stem):
    if stem not in taken:
        taken.add(stem)
        return stem
    n = 1
    while "%s_%d" % (stem, n) in taken:
        n += 1
    name = "%s_%d" % (stem, n)
    taken.add(name)
    return name


def transform(fg, allow_message_controls=False, enable_controls=True):
    """Return (headless_doc, TransformReport) for the Flowgraph `fg`.

    `fg` is not modified: the document is deep-copied first, so the caller's
    parsed flowgraph -- and the file behind it -- stay exactly as read.

    With `enable_controls`, the QT GUI input blocks are read into
    `report.controls` *before* they are frozen, and a Snippet is injected
    that starts the board-side control channel. Extraction has to come
    first: the freeze replaces the block's whole parameter dict with a
    single `value`, so a slider's bounds are gone by the time
    _strip_and_freeze has run.
    """
    doc = copy.deepcopy(fg.doc)
    work = grcfile.Flowgraph(doc, fg.path)
    rep = TransformReport()

    _force_options(work, rep)
    blocked = _check_message_controls(work, rep, allow_message_controls)
    if enable_controls:
        _extract_controls(work, rep)
    removed = _strip_and_freeze(work, rep, blocked)
    _splice_null_sinks(work, rep, removed)
    _flag_throttles(work, rep)
    if enable_controls and rep.controls:
        _inject_control_snippet(work, rep)

    doc["blocks"] = [b.raw for b in work.blocks]
    doc["connections"] = [c.as_list() for c in work.connections]
    return doc, rep


def _extract_controls(fg, rep):
    """Read the five QT GUI input blocks into rep.controls.

    Runs before the freeze, and only reads -- core/controls.py is a parser,
    not a mutator, so a flowgraph whose controls cannot be understood still
    transforms and still deploys. It just deploys without a panel for the
    ones it could not read, each of which says why.
    """
    found, warnings = controls_mod.extract(fg)
    rep.controls = found
    for text in warnings:
        rep.warn(text)


def _inject_control_snippet(fg, rep):
    """Add the Snippet that starts fau_ctl on the board.

    A Snippet, not an Embedded Python Block: an epy_block is a
    gr.basic_block with no reference to the top block and no supported way
    to get one, so it could never call tb.set_<id>(). `main_after_start` is
    handed `tb` by grcc's own template.

    Appended last, at the default priority. Snippets in a section are
    emitted in DESCENDING priority (`FlowGraph.get_snippets_dict`), and
    Python's sort is stable, so among the priority-0 snippets the one added
    last is called last -- which is what we want, since `fau_ctl.start()`
    re-installs SIGINT and should be the install that sticks. Beating the
    grcc template's own `signal.signal` is not in question either way: the
    template emits that before `snippets_main_after_start` regardless.
    """
    existing = {b.name for b in fg.blocks}
    name = _unique_name(set(existing), CTL_SNIPPET_NAME)
    fg.blocks.append(grcfile.Block({
        "name": name,
        "id": SNIPPET_ID,
        "parameters": {
            "alias": "",
            "code": CTL_SNIPPET_CODE,
            "comment": "injected by fau_deployer: starts the live control "
                       "channel that drives this flowgraph's frozen "
                       "variables from the deployer",
            "priority": "0",
            "section": CTL_SNIPPET_SECTION,
        },
        "states": {
            "bus_sink": False,
            "bus_source": False,
            "bus_structure": None,
            "coordinate": [8, 8],
            "rotation": 0,
            "state": "enabled",
        },
    }))
    rep.change("control", name,
               "snippet at %s starting fau_ctl for %d control%s: %s"
               % (CTL_SNIPPET_SECTION, len(rep.controls),
                  "" if len(rep.controls) == 1 else "s",
                  ", ".join(c.id for c in rep.controls)))


def _force_options(fg, rep):
    params = fg.option_params
    if not params:
        raise GrcError(
            "this flowgraph has no `options:` section, so there is no "
            "generate_options to switch to no_gui. Open and re-save it in "
            "GRC.")

    if params.get("generate_options") != HEADLESS_GENERATE_OPTIONS:
        rep.change("options", "generate_options",
                   "%s -> %s" % (params.get("generate_options"),
                                 HEADLESS_GENERATE_OPTIONS))
        params["generate_options"] = HEADLESS_GENERATE_OPTIONS

    if params.get("run_options") != FORCED_RUN_OPTIONS:
        rep.change("options", "run_options",
                   "%s -> %s (a prompt would read the serial console as "
                   "stdin and quit on console noise)"
                   % (params.get("run_options"), FORCED_RUN_OPTIONS))
        params["run_options"] = FORCED_RUN_OPTIONS


def _check_message_controls(fg, rep, allow):
    """Find the GUI controls whose behaviour cannot survive going headless.

    Returns the set of instance names to strip. Refuses unless the caller
    has said to go ahead, because "this message never fires again" is a
    change to what the flowgraph does, not to how it is displayed.
    """
    offenders = []
    for b in fg.enabled_blocks:
        kind = classify(b)
        if kind in ("message_variable", "message_only"):
            offenders.append((b, kind))

    if not offenders:
        return set()

    if not allow:
        lines = ", ".join("%s (%s)" % (b.name, b.id) for b, _ in offenders)
        raise GrcError(
            "this flowgraph has GUI controls that send messages: %s. "
            "Headless there is no operator to press them, so those messages "
            "would simply never fire -- a change in what the flowgraph "
            "does, not just how it looks, and no default here can be the "
            "right one. Remove them in GRC, or deploy anyway and accept it "
            "(--allow-message-controls on the CLI; the confirmation in the "
            "GUI)." % lines)

    blocked = set()
    for b, kind in offenders:
        blocked.add(b.name)
        if kind == "message_variable":
            rep.warn("%s (%s) both defines a variable and emits messages; "
                     "the variable is frozen below but its messages will "
                     "never fire on the board." % (b.name, b.id))
        else:
            rep.warn("%s (%s) only emits messages; nothing downstream of it "
                     "will ever be triggered on the board."
                     % (b.name, b.id))
    return blocked


def _strip_and_freeze(fg, rep, blocked):
    """Delete GUI sinks/decoration, freeze GUI variables in place.

    Returns {instance name: the Block it was} for everything deleted, which
    is what the null-sink splice needs to know the stream type of each port
    it has to fill.
    """
    removed = {}
    kept = []

    for b in fg.blocks:
        kind = classify(b) if b.enabled else None

        if kind in ("message_variable", "message_only") and b.name in blocked:
            # Consented to above. A message_variable still defines an id
            # other blocks reference, so it freezes like any other variable
            # -- only its message port disappears.
            if kind == "message_variable" and "value" in b.parameters:
                _freeze(b, rep, note=" (its message port is gone)")
                kept.append(b)
            else:
                removed[b.name] = b
                rep.change("stripped", b.name, "%s (message control)" % b.id)
            continue

        if kind in ("sink", "decoration"):
            removed[b.name] = b
            rep.change("stripped", b.name, b.id)
            continue

        if kind == "variable":
            _freeze(b, rep, live=any(c.id == b.name for c in rep.controls))
            kept.append(b)
            continue

        if kind == "unknown_gui":
            raise GrcError(
                "%s is a %s, which this tool does not know how to make "
                "headless. It is plainly a Qt widget, and leaving it in "
                "would make grcc emit code that imports Qt -- which the "
                "board image does not have. Remove it in GRC, or teach "
                "core/headless.py which of its categories it belongs to."
                % (b.name, b.id))

        kept.append(b)

    fg.blocks = kept
    fg.connections = [c for c in fg.connections
                      if c.src not in removed and c.dst not in removed]
    return removed


def _freeze(block, rep, note="", live=False):
    """Replace a GUI control with a plain `variable` at its current value.

    `live` says this one was also captured into the control spec, so the
    deployer will render a widget for it and drive it over the console.
    The generated code is identical either way -- grcc emits set_<id> for
    every variable regardless -- but "frozen" is a misleading word to leave
    in the derived .grc for a control that is still adjustable, and that
    file is the thing an operator opens when a run behaves oddly.
    """
    value = block.param("value", "0")
    label = block.param("label") or block.name
    was = block.id
    block.raw["id"] = VARIABLE_ID
    block.id = VARIABLE_ID
    block.raw["parameters"] = {
        "comment": ("converted by fau_deployer: was %s (%s); still "
                    "adjustable live from the deployer's control panel"
                    if live else
                    "frozen by fau_deployer: was %s (%s), no longer "
                    "operator-adjustable") % (was, label),
        "value": value,
    }
    block.parameters = block.raw["parameters"]
    rep.change("froze", block.name,
               "%s -> variable = %s%s%s"
               % (was, value, " (live control)" if live else "", note))


def _splice_null_sinks(fg, rep, removed):
    """Fill every output port the strip left with nothing on it.

    GNU Radio's topology check requires a block's declared output ports to
    be connected, so a source whose only consumer was a stripped GUI sink
    would fail at construction. Only *stream* ports need this: an
    unconnected message port is legal.
    """
    if not removed:
        return

    # Ports that fed something we deleted, in first-seen order, remembering
    # which sink they fed so the item type can come off that sink.
    orphaned = []
    seen = set()
    for conn in _original_connections(fg, removed):
        if conn.dst not in removed or conn.is_message:
            continue
        if conn.src in removed:
            continue  # a deleted block feeding a deleted block
        key = (conn.src, conn.src_port)
        if key in seen:
            continue
        seen.add(key)
        orphaned.append((key, removed[conn.dst]))

    if not orphaned:
        return

    # A port that still has a surviving consumer needs nothing.
    still_used = {(c.src, c.src_port) for c in fg.connections}
    taken = {b.name for b in fg.blocks}

    for (src, port), sink_block in orphaned:
        if (src, port) in still_used:
            continue
        type_name = _null_sink_type(sink_block, rep)
        if type_name is None:
            continue
        vlen = sink_block.param("vlen", "1") or "1"
        coordinate = (sink_block.raw.get("states") or {}).get(
            "coordinate", [8, 8])
        name = _unique_name(taken, "fau_null_sink_%s_%s" % (src, port))
        raw = _make_null_sink(name, type_name, vlen, coordinate)
        fg.blocks.append(grcfile.Block(raw))
        fg.connections.append(
            grcfile.Connection([src, port, name, "0"]))
        rep.change("spliced", name,
                   "blocks_null_sink(%s, vlen=%s) onto %s:%s, which %s used "
                   "to consume" % (type_name, vlen, src, port,
                                   sink_block.name))


def _original_connections(fg, removed):
    """The connection list as it was before _strip_and_freeze filtered it.

    _strip_and_freeze drops the connections to deleted blocks, which is what
    has to end up in the output -- but the splice needs exactly those
    dropped edges to know which ports were orphaned. Rather than keep a
    second copy, the raw document still holds them: only fg.connections was
    rebuilt.
    """
    return [grcfile.Connection(c) for c in (fg.doc.get("connections") or [])
            if isinstance(c, (list, tuple)) and len(c) >= 4
            and (str(c[2]) in removed or str(c[0]) in removed)]


def _flag_throttles(fg, rep):
    for b in fg.enabled_blocks:
        if b.id in THROTTLE_TYPES:
            rep.warn(
                "%s (%s) is a Throttle. On the board the DMA sets the sample "
                "rate, so a Throttle can only fight it -- it is left in "
                "place, but consider removing it." % (b.name, b.id))


def write(doc, path):
    """Write a transformed document out as a `.grc`.

    default_flow_style=False and sort_keys=False keep it looking like a file
    GRC wrote, so `diff` against the original shows the transform and not a
    reformat.
    """
    import yaml

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as fh:
        yaml.safe_dump(doc, fh, default_flow_style=False, sort_keys=False,
                       width=4096)
    return path
