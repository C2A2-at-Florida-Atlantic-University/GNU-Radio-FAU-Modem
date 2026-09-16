#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Reading the five QT GUI input blocks into a `ui_spec.json`.

The headless transform freezes a `variable_qtgui_range` into a plain
`variable`, which is what lets the flowgraph compile without Qt -- and
which throws away the bounds, the step, the labels and the widget style,
because a `variable` has nowhere to put them. **This module exists because
that conversion is lossy.** It reads those fields off the original block
before the freeze and writes them out beside the generated `.py`, so the
deployer can rebuild an equivalent control on the desktop.

Scope is exactly the five blocks that define a variable and do nothing
else. All five compile to one thing -- `self.set_<id>(value)` -- and grcc
generates `set_<id>` for *every* variable whether or not a widget ever
existed (`flow_graph.py.mako:265-285`). So driving one of these remotely is
not an emulation of a Qt widget; it is the stock code path with a serial
line spliced in where the signal/slot connection would be.

QT GUI *sinks* are not in scope and are not a deferred feature: a time sink
at 400 ksps complex is ~3.2 MB/s against a console that carries ~11.5 KB/s.

Values are read with `ast.literal_eval`, never `eval`. A GRC parameter can
hold any Python expression (`samp_rate/2` is ordinary), and this module
runs on the desktop against a file the operator may have been handed, so
the parser must not be able to execute it. An expression that will not
literal_eval is not an error -- the control degrades to a free-entry box
and says so (plan doc, open decision C). The flowgraph itself is
unaffected either way: the frozen `variable` keeps the original expression
text, so GRC evaluates it at build time exactly as it always did.
"""

import ast
import json
import os

SPEC_VERSION = 1
SPEC_FILENAME = "ui_spec.json"

KIND_RANGE = "range"
KIND_ENTRY = "entry"
KIND_CHOOSER = "chooser"
KIND_CHECK_BOX = "check_box"
KIND_PUSH_BUTTON = "push_button"

# The block type ids this module can read, and what each becomes. Kept
# separate from core/headless.py's VARIABLE_CONTROLS on purpose: that set
# is "what can be frozen safely", which is a superset. A
# `variable_qtgui_label` freezes fine and is not a control -- there is
# nothing for an operator to adjust.
CONTROL_KINDS = {
    "variable_qtgui_range": KIND_RANGE,
    "variable_qtgui_entry": KIND_ENTRY,
    "variable_qtgui_chooser": KIND_CHOOSER,
    "variable_qtgui_check_box": KIND_CHECK_BOX,
    "variable_qtgui_push_button": KIND_PUSH_BUTTON,
}

# GRC's `type`/`rangeType` enum -> what the value actually is. GRC calls a
# float "real"; the spec uses Python's own names so the board side can
# check against them without a second table.
DTYPES = {
    "real": "float", "float": "float",
    "int": "int",
    "string": "str",
    "bool": "bool",
    "raw": "raw",
}

RANGE_WIDGETS = ("counter_slider", "counter", "slider", "dial")
CHOOSER_WIDGETS = ("combo_box", "radio_buttons")

# gnuradio.eng_notation.scale_factor, mirrored rather than imported. A QT
# GUI Entry of type `real` converts what the operator types with
# `eng_notation.str_to_num`, so "1.5M" has to mean 1.5e6 in the deployer
# too or the same keystrokes produce different numbers in the two places
# the same flowgraph runs. Mirrored because importing gnuradio pulls the
# whole runtime into a Tk process that otherwise needs none of it, and
# because this is twelve constants that have not moved in the life of the
# project.
ENG_SUFFIXES = {
    "E": 1e18, "P": 1e15, "T": 1e12, "G": 1e9, "M": 1e6, "k": 1e3,
    "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18,
}


# Sentinel for "this parameter would not literal_eval". None cannot serve:
# a GRC parameter is allowed to literally be None, and a control whose
# default is None is a different thing from one whose default is an
# expression nobody here can evaluate.
_MISSING = object()


class ControlError(ValueError):
    """A control spec that cannot be built. Message is shown as-is."""


def str_to_num(text):
    """`gnuradio.eng_notation.str_to_num`, reimplemented.

    Same shape as the original, including that only the *last* character is
    considered a suffix, so "1.5M" is 1.5e6 and "1.5Mx" is an error.
    """
    if not isinstance(text, str):
        raise ValueError("value must be a string, got %r" % (text,))
    text = text.strip()
    try:
        if text and text[-1] in ENG_SUFFIXES:
            return float(text[:-1]) * ENG_SUFFIXES[text[-1]]
        return float(text)
    except (IndexError, ValueError):
        raise ValueError("%r is not a number in engineering notation" % text)


def literal(text, default=None):
    """`ast.literal_eval(text)`, or `default` if it is not a literal.

    Never `eval`. A GRC parameter is an arbitrary Python expression, and
    this runs on a file the operator did not necessarily write.
    """
    if text is None:
        return default
    if not isinstance(text, str):
        return text
    text = text.strip()
    if not text:
        return default
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return default


_JSON_SCALARS = (bool, int, float, str, type(None))


def json_safe(value):
    """True if `value` survives a JSON round trip unchanged.

    The spec is JSON and the wire carries `repr()`, so anything outside
    this set cannot be represented in one and read back as itself on the
    other. A tuple is the realistic case: JSON would return it as a list,
    and a chooser silently comparing a list against a tuple option would
    never match.
    """
    if isinstance(value, _JSON_SCALARS):
        # bool is a subclass of int; both are fine. Reject the float
        # values JSON cannot express, which would come back as the bare
        # words Infinity/NaN that a strict parser refuses.
        if isinstance(value, float) and (value != value or value in (
                float("inf"), float("-inf"))):
            return False
        return True
    if isinstance(value, list):
        return all(json_safe(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and json_safe(v)
                   for k, v in value.items())
    return False


def _text(block, key, default=""):
    return block.param(key, default)


def _label_of(block):
    """The operator-facing label, falling back to the variable id.

    GRC stores a `string` parameter either bare (`Gain`) or quoted
    (`'Gain'`) depending on how it was entered; the block templates call
    `no_quotes()` on it for exactly that reason. Unwrap the quoted form so
    the deployer's label matches what Qt would have shown.
    """
    raw = _text(block, "label").strip()
    if not raw:
        return block.name
    unwrapped = literal(raw)
    if isinstance(unwrapped, str):
        return unwrapped or block.name
    return raw


def _dtype_of(block, key="type", default="raw"):
    got = _text(block, key, default).strip()
    unwrapped = literal(got)
    if isinstance(unwrapped, str):
        got = unwrapped
    return DTYPES.get(got, "raw")


class Control:
    """One settable control, as the spec records it.

    `free_entry` means at least one of this control's fields would not
    literal_eval, so the deployer must render a plain text box instead of
    the widget the block asked for. It is carried per-control rather than
    failing the whole flowgraph: one unevaluable bound should not cost the
    operator every other slider.
    """

    def __init__(self, cid, kind, label, dtype, default,
                 free_entry=False, note=None, **extra):
        self.id = cid
        self.kind = kind
        self.label = label
        self.dtype = dtype
        self.default = default
        self.free_entry = bool(free_entry)
        self.note = note
        self.extra = extra

    def to_dict(self):
        out = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "dtype": self.dtype,
            "default": self.default,
            "free_entry": self.free_entry,
        }
        if self.note:
            out["note"] = self.note
        out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, data):
        known = ("id", "kind", "label", "dtype", "default", "free_entry",
                 "note")
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(data["id"], data["kind"], data.get("label", data["id"]),
                   data.get("dtype", "raw"), data.get("default"),
                   data.get("free_entry", False), data.get("note"), **extra)

    def __repr__(self):
        return "Control(%s:%s)" % (self.id, self.kind)


def _parse_range(block, label, warn):
    dtype = _dtype_of(block, "rangeType", "float")
    if dtype not in ("float", "int"):
        dtype = "float"

    fields = {}
    degraded = []
    for key in ("value", "start", "stop", "step"):
        got = literal(_text(block, key), _MISSING)
        if got is _MISSING or not isinstance(got, (int, float)) \
                or isinstance(got, bool):
            degraded.append(key)
            fields[key] = None
        else:
            fields[key] = int(got) if dtype == "int" else float(got)

    widget = _text(block, "widget", "counter_slider").strip() \
        or "counter_slider"
    if widget not in RANGE_WIDGETS:
        widget = "counter_slider"

    if degraded:
        warn("%s (QT GUI Range) has %s that this tool cannot evaluate "
             "without running the flowgraph (they are expressions, not "
             "literals), so it is rendered as a plain entry box instead of "
             "a slider. Its value still reaches the board the same way."
             % (block.name, " and ".join(degraded)))
        return Control(block.name, KIND_RANGE, label, dtype,
                       fields["value"], free_entry=True,
                       note="bounds unavailable: %s" % ", ".join(degraded))

    if fields["start"] > fields["stop"]:
        warn("%s (QT GUI Range) has start > stop (%s > %s); the deployer "
             "swaps them so the slider is usable, which is what GRC's own "
             "assert would have refused."
             % (block.name, fields["start"], fields["stop"]))
        fields["start"], fields["stop"] = fields["stop"], fields["start"]

    value = fields["value"]
    if not (fields["start"] <= value <= fields["stop"]):
        # GRC's own `${start <= value <= stop}` assert would have refused
        # this flowgraph, so it can only reach here from a hand-edited
        # .grc. Clamp rather than refuse: the flowgraph still compiles and
        # runs with the original value (the frozen variable keeps it), and
        # taking the whole deploy away over a slider's starting position
        # would be out of proportion.
        clamped = min(max(value, fields["start"]), fields["stop"])
        warn("%s (QT GUI Range) starts at %s, outside its own %s..%s "
             "range; the deployer's slider starts at %s instead. The "
             "flowgraph itself still starts at %s -- only the widget "
             "differs, until you move it."
             % (block.name, value, fields["start"], fields["stop"],
                clamped, value))
        value = clamped

    return Control(block.name, KIND_RANGE, label, dtype, value,
                   start=fields["start"], stop=fields["stop"],
                   step=fields["step"], widget=widget)


def _parse_entry(block, label, warn):
    dtype = _dtype_of(block)
    default = literal(_text(block, "value"), _MISSING)
    free = default is _MISSING
    note = None
    if free:
        default = None

    if dtype == "bool":
        # Stock GRC converts this one with Python's `bool(str)`, which is
        # True for any non-empty string -- typing "False" into it sets the
        # variable True. A checkbox cannot express that bug, so the
        # deployer renders one and says so: better behaviour, but still a
        # difference from what the same flowgraph does under Qt.
        warn("%s (QT GUI Entry, type Boolean) is rendered as a checkbox. "
             "Stock GRC converts this box's text with bool(str), which is "
             "True for ANY non-empty text -- typing False sets it True. "
             "The checkbox sends real booleans instead."
             % block.name)
        note = "rendered as a checkbox; stock GRC's bool(str) conversion " \
               "cannot express False"

    return Control(block.name, KIND_ENTRY, label, dtype, default,
                   free_entry=free, note=note)


def _chooser_options(block, warn):
    """(options, labels) for a chooser, from whichever of its two parse
    paths `num_opts` selects.

    `num_opts: 0` is GRC's "List" mode and reads the `options`/`labels`
    list parameters; 1-5 read the numbered `option0..4`/`label0..4`
    fields. Both shapes are in the wild and neither is a fallback for the
    other.
    """
    raw_n = _text(block, "num_opts", "3").strip()
    try:
        num = int(literal(raw_n, raw_n))
    except (TypeError, ValueError):
        num = 0

    if num > 0:
        options, labels = [], []
        for i in range(min(num, 5)):
            got = literal(_text(block, "option%d" % i), _MISSING)
            if got is _MISSING:
                return None, None
            options.append(got)
            lab = _text(block, "label%d" % i).strip()
            unwrapped = literal(lab)
            labels.append(unwrapped if isinstance(unwrapped, str) else lab)
        return options, labels

    options = literal(_text(block, "options"), _MISSING)
    labels = literal(_text(block, "labels"), [])
    if options is _MISSING or not isinstance(options, list):
        return None, None
    if not isinstance(labels, list):
        labels = []
    return options, labels


def _parse_chooser(block, label, warn):
    dtype = _dtype_of(block)
    default = literal(_text(block, "value"), _MISSING)
    options, labels = _chooser_options(block, warn)

    widget = _text(block, "widget", "combo_box").strip() or "combo_box"
    if widget not in CHOOSER_WIDGETS:
        widget = "combo_box"

    if options is None or default is _MISSING:
        warn("%s (QT GUI Chooser) has options or a default this tool "
             "cannot evaluate without running the flowgraph, so it is "
             "rendered as a plain entry box rather than a list to pick "
             "from." % block.name)
        return Control(block.name, KIND_CHOOSER, label, dtype,
                       None if default is _MISSING else default,
                       free_entry=True, note="options unavailable")

    if not all(json_safe(o) for o in options):
        warn("%s (QT GUI Chooser) has options that cannot be carried "
             "as JSON (a tuple, most likely). It is rendered as a plain "
             "entry box; a list that came back from JSON would not compare "
             "equal to the tuple the flowgraph expects." % block.name)
        return Control(block.name, KIND_CHOOSER, label, dtype, None,
                       free_entry=True, note="options are not JSON values")

    # GRC replaces a blank label with the option itself, and drops the
    # label list entirely when it is empty. Do the same so the deployer's
    # list reads like the combo box would have.
    if len(labels) != len(options):
        labels = [str(o) for o in options]
    else:
        labels = [lab if lab else str(options[i])
                  for i, lab in enumerate(labels)]

    if default not in options:
        warn("%s (QT GUI Chooser) has a default (%r) that is not one of "
             "its options; the deployer starts it on the first option. "
             "GRC's own assert would have refused this flowgraph."
             % (block.name, default))
        default = options[0] if options else None

    return Control(block.name, KIND_CHOOSER, label, dtype, default,
                   options=options, labels=labels, widget=widget)


def _parse_check_box(block, label, warn):
    dtype = _dtype_of(block)
    default = literal(_text(block, "value"), _MISSING)
    # The two sides are arbitrary values of the declared type, not
    # necessarily booleans -- the block's own make template builds
    # {True: <true>, False: <false>} and sets the variable to one of them.
    on = literal(_text(block, "true", "True"), _MISSING)
    off = literal(_text(block, "false", "False"), _MISSING)

    if _MISSING in (default, on, off) or not (json_safe(on)
                                              and json_safe(off)):
        warn("%s (QT GUI Check Box) has values this tool cannot evaluate "
             "without running the flowgraph, so it is rendered as a plain "
             "entry box rather than a checkbox." % block.name)
        return Control(block.name, KIND_CHECK_BOX, label, dtype,
                       None if default is _MISSING else default,
                       free_entry=True, note="checked/unchecked values "
                                             "unavailable")

    if default not in (on, off):
        warn("%s (QT GUI Check Box) starts at %r, which is neither its "
             "checked (%r) nor its unchecked (%r) value; the deployer "
             "starts it unchecked."
             % (block.name, default, on, off))
        default = off

    return Control(block.name, KIND_CHECK_BOX, label, dtype, default,
                   true_value=on, false_value=off)


def _parse_push_button(block, label, warn):
    dtype = _dtype_of(block)
    default = literal(_text(block, "value"), _MISSING)
    pressed = literal(_text(block, "pressed", "1"), _MISSING)
    released = literal(_text(block, "released", "0"), _MISSING)

    if _MISSING in (pressed, released) or not (json_safe(pressed)
                                               and json_safe(released)):
        warn("%s (QT GUI Push Button) has pressed/released values this "
             "tool cannot evaluate without running the flowgraph, so it is "
             "rendered as a plain entry box rather than a button."
             % block.name)
        return Control(block.name, KIND_PUSH_BUTTON, label, dtype,
                       None if default is _MISSING else default,
                       free_entry=True,
                       note="pressed/released values unavailable")

    return Control(block.name, KIND_PUSH_BUTTON, label, dtype,
                   released if default is _MISSING else default,
                   pressed=pressed, released=released)


_PARSERS = {
    KIND_RANGE: _parse_range,
    KIND_ENTRY: _parse_entry,
    KIND_CHOOSER: _parse_chooser,
    KIND_CHECK_BOX: _parse_check_box,
    KIND_PUSH_BUTTON: _parse_push_button,
}


def is_control(block):
    return block.id in CONTROL_KINDS


def extract(fg):
    """(controls, warnings) for every enabled QT GUI input block in `fg`.

    `fg` is not modified. Order follows the flowgraph's own block order,
    which is the order GRC wrote them, so the deployer's panel is stable
    across regenerations of the same file rather than reshuffling.
    """
    controls = []
    warnings = []

    def warn(text):
        warnings.append(text)

    for block in fg.enabled_blocks:
        kind = CONTROL_KINDS.get(block.id)
        if kind is None:
            continue
        label = _label_of(block)
        controls.append(_PARSERS[kind](block, label, warn))

    seen = {}
    for c in controls:
        if c.id in seen:
            raise ControlError(
                "two blocks in this flowgraph both define the variable %r "
                "(%s and %s). GRC would generate one set_%s for both, so "
                "which one a control drives is undefined -- rename one."
                % (c.id, seen[c.id], c.kind, c.id))
        seen[c.id] = c.kind

    return controls, warnings


def build_spec(flowgraph_id, controls):
    return {
        "version": SPEC_VERSION,
        "flowgraph": flowgraph_id,
        "controls": [c.to_dict() for c in controls],
    }


def write_spec(spec, path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(spec, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return path


def load_spec(path):
    """Read a `ui_spec.json` back into (flowgraph_id, [Control]).

    Used by the GUI to build its panel and by the CLI for
    --list-controls. Validates enough to fail with a sentence rather than
    a KeyError on a file that is not one of ours.
    """
    try:
        with open(path, "r") as fh:
            spec = json.load(fh)
    except OSError as exc:
        raise ControlError("cannot read %s: %s" % (path, exc))
    except ValueError as exc:
        raise ControlError("%s is not valid JSON: %s" % (path, exc))
    return parse_spec(spec, path)


def parse_spec(spec, path="ui_spec.json"):
    if not isinstance(spec, dict) or "controls" not in spec:
        raise ControlError("%s is not a control spec (no `controls` key)"
                           % path)
    version = spec.get("version")
    if version != SPEC_VERSION:
        raise ControlError(
            "%s is control-spec version %r; this tool writes and reads %d. "
            "Regenerate it by processing the flowgraph again."
            % (path, version, SPEC_VERSION))
    controls = []
    for entry in spec["controls"]:
        if not isinstance(entry, dict) or "id" not in entry \
                or "kind" not in entry:
            raise ControlError("%s has a control entry with no id/kind: %r"
                               % (path, entry))
        if entry["kind"] not in _PARSERS:
            raise ControlError("%s names a control kind this tool does not "
                               "know: %r" % (path, entry["kind"]))
        controls.append(Control.from_dict(entry))
    return spec.get("flowgraph", ""), controls


# ---------------------------------------------------------------------
# The wire format, and the desktop half of the value conversion.
#
# Two verbs, desktop to board, one line each, sent only while a flowgraph
# is running:
#
#     SET <id> <python-literal>
#     PULSE <id> <milliseconds>
#
# and three replies, nonce-tagged so flowgraph stdout cannot forge one:
#
#     FAU-CTL-<nonce> READY <id> <id> ...
#     FAU-CTL-<nonce> OK <id> <python-literal>
#     FAU-CTL-<nonce> ERR <id> <reason>
#
# There is no ACK/retransmit machinery, unlike the payload transfer. A
# lost control line is not worth recovering: the operator sees no OK and
# moves the slider again, and the next value is more useful than the one
# that got lost. A corrupted line either fails the id whitelist or fails
# literal_eval on the board, and both come back as ERR.
# ---------------------------------------------------------------------

PROTO = "FAU-CTL"
VERB_SET = "SET"
VERB_PULSE = "PULSE"


def coerce_value(control, text):
    """Turn what the operator typed into the value to send.

    Mirrors the conversion the block's own Qt widget would have applied,
    which is why a QT GUI Entry of type Float accepts "1.5M": its stock
    `conv` is `eng_notation.str_to_num`. Raises ControlError with a
    sentence meant for a message box.

    A `raw` control is parsed with `ast.literal_eval`, never `eval` --
    stock GRC uses `eval` there, and that difference is deliberate. What
    it costs is `raw` entries holding a real expression; what it buys is
    that nothing the operator types, or that arrives corrupted, can
    execute on either side of the link.
    """
    if not isinstance(text, str):
        value = text
    else:
        text = text.strip()
        dtype = control.dtype
        if dtype == "float":
            kind = getattr(control, "kind", None)
            try:
                # Only the Entry block uses engineering notation; a Range
                # is numeric input from a slider or spinbox, and reading
                # "1m" off one as a milli-unit would be a surprise.
                value = (str_to_num(text) if kind == KIND_ENTRY
                         else float(text))
            except ValueError as exc:
                raise ControlError("%s: %s" % (control.id, exc))
        elif dtype == "int":
            try:
                value = int(text, 0) if text[:2].lower() in ("0x", "0b") \
                    else int(text)
            except (ValueError, IndexError):
                raise ControlError("%s: %r is not a whole number"
                                   % (control.id, text))
        elif dtype == "str":
            value = text
        elif dtype == "bool":
            lowered = text.lower()
            if lowered in ("1", "true", "yes", "on"):
                value = True
            elif lowered in ("0", "false", "no", "off"):
                value = False
            else:
                raise ControlError("%s: %r is not true/false"
                                   % (control.id, text))
        else:
            value = literal(text, _MISSING)
            if value is _MISSING:
                raise ControlError(
                    "%s: %r is not a Python literal. This control's type is "
                    "Any, so the deployer sends whatever literal you type "
                    "-- a number, a quoted string, a list. It will not "
                    "evaluate an expression, on either side of the link."
                    % (control.id, text))

    if not json_safe(value):
        raise ControlError("%s: %r cannot be sent over the control channel"
                           % (control.id, value))

    options = control.extra.get("options")
    if options is not None and value not in options:
        raise ControlError(
            "%s: %r is not one of its options (%s)"
            % (control.id, value, ", ".join(repr(o) for o in options)))
    return value


def wire_set(cid, value):
    """The SET line for `cid`, without its terminator.

    `repr` because the board parses with `ast.literal_eval`: the desktop
    canonicalises once, here, so the two ends never disagree about what
    the operator's text meant.
    """
    return "%s %s %s" % (VERB_SET, cid, repr(value))


def wire_pulse(cid, ms):
    return "%s %s %g" % (VERB_PULSE, cid, float(ms))


def is_control_echo(line, ids):
    """True if `line` is the console echoing a control line back at us.

    The board's tty echoes everything written to it, so every SET appears
    in the output stream as well. Matched on the verb and a known id
    rather than on the exact text we sent: the console corrupts a
    character now and then, and a garbled echo logged as board output is
    more confusing than a dropped one.
    """
    parts = line.strip().split(None, 2)
    return (len(parts) >= 2 and parts[0].upper() in (VERB_SET, VERB_PULSE)
            and parts[1] in ids)


class Reply:
    """One parsed `FAU-CTL-<nonce> ...` line."""

    def __init__(self, kind, cid=None, value=None, text=""):
        self.kind = kind       # "READY" | "OK" | "ERR"
        self.id = cid
        self.value = value     # the acknowledged value, for OK
        self.text = text       # the reason, for ERR; the id list for READY

    @property
    def ok(self):
        return self.kind == "OK"

    def __repr__(self):
        return "Reply(%s %s %r)" % (self.kind, self.id, self.text)


def parse_reply(line, nonce):
    """A Reply, or None if `line` is not a control reply for this run.

    Nonce-matched, so a flowgraph that prints something FAU-CTL-shaped
    cannot be mistaken for the control channel and cannot make a widget
    claim a value it never got.
    """
    tag = "%s-%s " % (PROTO, nonce)
    at = line.find(tag)
    if at < 0:
        return None
    body = line[at + len(tag):].strip()
    parts = body.split(None, 2)
    if not parts:
        return None
    kind = parts[0].upper()
    if kind == "READY":
        # Every remaining token, not just the first: READY carries the
        # whole id list, and split(None, 2) would have stopped at one.
        return Reply("READY", text=body[len(parts[0]):].strip())
    if kind not in ("OK", "ERR") or len(parts) < 2:
        return None
    cid = parts[1]
    rest = parts[2] if len(parts) > 2 else ""
    if kind == "OK":
        # literal() falling back to None is unambiguous here: a control
        # whose value really is None round-trips as the text "None", which
        # literal_evals back to None anyway.
        return Reply("OK", cid, literal(rest, None), rest)
    return Reply("ERR", cid, None, rest)
