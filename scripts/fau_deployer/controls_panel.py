#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""The deployer's live control panel: `ui_spec.json` rendered as Tk widgets.

One widget per entry in the spec, wired to a callback that writes a SET or
PULSE line to the board. Kept out of gui.py because it is self-contained --
it needs a parent frame and two callbacks, and knows nothing about
sessions, payloads or the deploy state machine -- which also makes it
testable on its own against a withdrawn root window.

**Values are coalesced, not streamed.** A dragged `ttk.Scale` fires its
command on every pixel; sending one line each would put hundreds of SETs on
a console that also carries the flowgraph's output. Each widget instead
holds the latest value and flushes it on a timer at SEND_HZ. The point is
not bandwidth -- ~30 bytes at 15 Hz is well under 1 KB/s against an
11.5 KB/s console -- but keeping the log readable and not hammering a
setter hundreds of times for one gesture. The final value of a drag is
always sent: release flushes immediately rather than waiting for the tick.

Every widget shows its own last acknowledged value, and an ERR marks that
widget rather than only printing into the log -- a control that silently
did not take is the failure this panel most has to avoid.
"""

import tkinter as tk
from tkinter import ttk

from .core import controls as controls_mod
from .core.controls import ControlError

SEND_HZ = 15.0
SEND_INTERVAL_MS = int(1000.0 / SEND_HZ)

# How long a Push Button's PULSE holds, in milliseconds. Fixed rather than
# taken from how long the operator held the mouse: over a serial console
# that duration is not something they control, and a button wired to a
# reset wants a width that means something.
PULSE_MS = 50

OK_COLOR = "#2e7d32"
ERR_COLOR = "#b00020"
IDLE_COLOR = "#555555"


class _Row:
    """One control: its label, its widget(s), and its acknowledgement.

    Subclasses build the widget into `body` and call `self.stage(value)`
    when the operator changes something. Everything to do with coalescing,
    sending and acknowledging lives here so each widget only has to say
    what its current value is.
    """

    def __init__(self, panel, control, parent, row):
        self.panel = panel
        self.control = control
        self.pending = None
        self.has_pending = False
        self.timer = None
        self.widgets = []

        label = ttk.Label(parent, text=control.label, width=18, anchor="w")
        label.grid(row=row, column=0, sticky="w", padx=(8, 6), pady=3)
        self.body = ttk.Frame(parent)
        self.body.grid(row=row, column=1, sticky="ew", pady=3)
        self.status = ttk.Label(parent, text="", width=22, anchor="w",
                                foreground=IDLE_COLOR)
        self.status.grid(row=row, column=2, sticky="w", padx=(6, 8))

        self.build()
        self.note(control.default, sent=False)
        if control.note:
            # The conversion was lossy in a way the operator should know
            # about without reading the Process log.
            self.status.config(text=_shorten(control.note))

    # -- to override -------------------------------------------------
    def build(self):
        raise NotImplementedError

    def set_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for widget in self.widgets:
            try:
                widget.config(state=state)
            except tk.TclError:
                pass

    # -- sending -----------------------------------------------------
    def stage(self, value):
        """Record a new value and make sure it gets sent soon."""
        self.pending = value
        self.has_pending = True
        if self.timer is None:
            self.timer = self.body.after(SEND_INTERVAL_MS, self._tick)

    def flush(self, _event=None):
        """Send the staged value now -- for the end of a drag, where
        waiting out the tick would leave the last value unsent for up to
        SEND_INTERVAL_MS after the operator let go."""
        self._cancel()
        self._send()

    def _tick(self):
        self.timer = None
        if self.has_pending:
            self._send()
            # Keep ticking while a drag is in progress: the next motion
            # event stages another value, and this timer is what paces it.
            self.timer = self.body.after(SEND_INTERVAL_MS, self._tick)

    def _send(self):
        if not self.has_pending:
            return
        value, self.has_pending = self.pending, False
        self.panel.send(self.control, value)

    def _cancel(self):
        if self.timer is not None:
            try:
                self.body.after_cancel(self.timer)
            except tk.TclError:
                pass
            self.timer = None

    def destroy(self):
        self._cancel()

    # -- acknowledgement ---------------------------------------------
    def note(self, value, sent=True, error=None):
        if error:
            self.status.config(text=_shorten(error), foreground=ERR_COLOR)
        elif sent:
            self.status.config(text="= %s" % _shorten(repr(value)),
                               foreground=OK_COLOR)
        else:
            self.status.config(text="= %s" % _shorten(repr(value)),
                               foreground=IDLE_COLOR)


class _RangeRow(_Row):
    def build(self):
        c = self.control
        start, stop = c.extra["start"], c.extra["stop"]
        step = c.extra["step"] or 1
        self.is_int = c.dtype == "int"
        self.var = tk.DoubleVar(value=float(c.default))
        self._syncing = False

        widget = c.extra.get("widget", "counter_slider")
        self.body.columnconfigure(0, weight=1)

        if widget != "counter":
            scale = ttk.Scale(self.body, from_=start, to=stop,
                              variable=self.var, command=self._on_scale)
            scale.grid(row=0, column=0, sticky="ew", padx=(0, 6))
            # A drag ends on button release; flush there so the value the
            # operator actually chose is not left waiting for a tick.
            scale.bind("<ButtonRelease-1>", self.flush)
            self.widgets.append(scale)

        if widget in ("counter_slider", "counter", "dial"):
            spin = ttk.Spinbox(self.body, from_=start, to=stop,
                               increment=abs(step) or 1, width=12,
                               textvariable=self.var, command=self._on_spin)
            spin.grid(row=0, column=1, sticky="e")
            spin.bind("<Return>", lambda _e: self._on_spin())
            spin.bind("<FocusOut>", lambda _e: self._on_spin())
            self.widgets.append(spin)

    def _quantize(self, value):
        c = self.control
        start, stop = c.extra["start"], c.extra["stop"]
        step = c.extra["step"]
        value = min(max(value, start), stop)
        if step:
            # Snap to the grid the block declared, so the deployer offers
            # the same value set the Qt widget would have.
            value = start + round((value - start) / step) * step
            value = min(max(value, start), stop)
        return int(round(value)) if self.is_int else float(value)

    def _on_scale(self, _text):
        if self._syncing:
            return
        self.stage(self._quantize(self.var.get()))

    def _on_spin(self):
        try:
            raw = float(self.var.get())
        except (tk.TclError, ValueError):
            return
        value = self._quantize(raw)
        self._syncing = True
        try:
            self.var.set(value)
        finally:
            self._syncing = False
        self.stage(value)
        self.flush()


class _EntryRow(_Row):
    """A free text box, for an Entry and for anything that degraded.

    Deliberately send-on-Return rather than send-on-keystroke: half-typed
    text is not a value, and "1" on the way to "1000" is a real number the
    flowgraph would act on.
    """

    def build(self):
        self.var = tk.StringVar(value=_as_text(self.control.default))
        self.body.columnconfigure(0, weight=1)
        entry = ttk.Entry(self.body, textvariable=self.var)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        entry.bind("<Return>", lambda _e: self._submit())
        button = ttk.Button(self.body, text="Set", width=6,
                            command=self._submit)
        button.grid(row=0, column=1)
        self.widgets.extend([entry, button])

    def _submit(self):
        try:
            value = controls_mod.coerce_value(self.control, self.var.get())
        except ControlError as exc:
            self.panel.report(self.control.id, str(exc))
            return
        self.stage(value)
        self.flush()


class _BoolEntryRow(_Row):
    """A QT GUI Entry of type Boolean.

    Stock GRC converts this box's text with `bool(str)`, which is True for
    any non-empty string -- typing "False" sets it True. A checkbox cannot
    express that, which is the point: better behaviour, reported as a
    difference rather than passed off as fidelity.
    """

    def build(self):
        self.var = tk.BooleanVar(value=bool(self.control.default))
        box = ttk.Checkbutton(self.body, variable=self.var,
                              command=self._toggled)
        box.grid(row=0, column=0, sticky="w")
        self.widgets.append(box)

    def _toggled(self):
        self.stage(bool(self.var.get()))
        self.flush()


class _ChooserRow(_Row):
    def build(self):
        c = self.control
        self.options = list(c.extra["options"])
        labels = list(c.extra["labels"])
        self.body.columnconfigure(0, weight=1)

        if c.extra.get("widget") == "radio_buttons" and len(self.options) <= 6:
            self.var = tk.IntVar(value=self._index_of(c.default))
            for i, text in enumerate(labels):
                button = ttk.Radiobutton(self.body, text=text, value=i,
                                         variable=self.var,
                                         command=self._picked)
                button.grid(row=0, column=i, sticky="w", padx=(0, 8))
                self.widgets.append(button)
        else:
            self.var = tk.StringVar(value=labels[self._index_of(c.default)]
                                    if labels else "")
            combo = ttk.Combobox(self.body, textvariable=self.var,
                                 values=labels, state="readonly")
            combo.grid(row=0, column=0, sticky="ew")
            combo.bind("<<ComboboxSelected>>",
                       lambda _e: self._picked(combo.current()))
            self.widgets.append(combo)

    def _index_of(self, value):
        try:
            return self.options.index(value)
        except ValueError:
            return 0

    def _picked(self, index=None):
        if index is None:
            index = self.var.get()
        if 0 <= index < len(self.options):
            self.stage(self.options[index])
            self.flush()

    def set_enabled(self, enabled):
        for widget in self.widgets:
            try:
                if isinstance(widget, ttk.Combobox):
                    # A combobox must go back to readonly, not normal, or
                    # re-enabling it would let the operator type a value
                    # that is not one of the options.
                    widget.config(state="readonly" if enabled else "disabled")
                else:
                    widget.config(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass


class _CheckBoxRow(_Row):
    def build(self):
        c = self.control
        self.on = c.extra["true_value"]
        self.off = c.extra["false_value"]
        self.var = tk.BooleanVar(value=(c.default == self.on))
        box = ttk.Checkbutton(self.body, variable=self.var,
                              command=self._toggled,
                              text="%s / %s" % (_shorten(repr(self.on), 12),
                                                _shorten(repr(self.off), 12)))
        box.grid(row=0, column=0, sticky="w")
        self.widgets.append(box)

    def _toggled(self):
        self.stage(self.on if self.var.get() else self.off)
        self.flush()


class _PushButtonRow(_Row):
    """Momentary, sent as one PULSE rather than two SETs.

    The board does pressed -> sleep -> released locally, so the edge width
    is PULSE_MS and not however long the console took. `Hold` is there for
    the hold-to-enable case, where the operator's own timing IS the intent.
    """

    def build(self):
        press = ttk.Button(self.body, text="Pulse", width=8,
                           command=self._pulse)
        press.grid(row=0, column=0, padx=(0, 6))
        hold = ttk.Button(self.body, text="Hold", width=8)
        hold.grid(row=0, column=1)
        hold.bind("<ButtonPress-1>", lambda _e: self._edge("pressed"))
        hold.bind("<ButtonRelease-1>", lambda _e: self._edge("released"))
        self.widgets.extend([press, hold])

    def _pulse(self):
        self.panel.pulse(self.control, PULSE_MS)

    def _edge(self, which):
        self.panel.send(self.control, self.control.extra[which])


def _row_class(control):
    if control.free_entry:
        return _EntryRow
    if control.kind == controls_mod.KIND_RANGE:
        return _RangeRow
    if control.kind == controls_mod.KIND_CHOOSER:
        return _ChooserRow
    if control.kind == controls_mod.KIND_CHECK_BOX:
        return _CheckBoxRow
    if control.kind == controls_mod.KIND_PUSH_BUTTON:
        return _PushButtonRow
    if control.kind == controls_mod.KIND_ENTRY and control.dtype == "bool":
        return _BoolEntryRow
    return _EntryRow


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return repr(value)


def _shorten(text, limit=22):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class ControlPanel(ttk.LabelFrame):
    """The panel itself. Empty and hidden until a spec arrives.

    `on_send(control, value)` and `on_pulse(control, ms)` do the writing;
    this class never touches a transport. They may raise, and a raise is
    shown against the widget rather than propagated -- a control that
    cannot be sent is a message, not a crash.
    """

    def __init__(self, parent, on_send, on_pulse, on_message=None, **kw):
        ttk.LabelFrame.__init__(self, parent, text="Controls", **kw)
        self._on_send = on_send
        self._on_pulse = on_pulse
        # The status column next to each widget is ~22 characters, which
        # is enough to show a value and nowhere near enough to show why a
        # setter refused one. So the widget gets the marker and the log
        # gets the sentence; a truncated error is barely better than none.
        self._on_message = on_message or (lambda _text: None)
        self._rows = {}
        self._enabled = False
        self.columnconfigure(1, weight=1)
        self._empty = ttk.Label(
            self, foreground=IDLE_COLOR, anchor="w",
            text="No controls -- process a flowgraph with QT GUI input "
                 "blocks to get a panel here.")
        self._empty.grid(row=0, column=0, columnspan=3, sticky="ew",
                         padx=8, pady=6)

    # -- content -----------------------------------------------------
    @property
    def ids(self):
        return tuple(self._rows)

    def set_controls(self, controls):
        """Rebuild the panel for `controls` (possibly empty)."""
        self.clear()
        if not controls:
            self._empty.grid()
            return
        self._empty.grid_remove()
        for i, control in enumerate(controls):
            self._rows[control.id] = _row_class(control)(self, control,
                                                         self, i + 1)
        self.set_enabled(self._enabled)

    def clear(self):
        for row in self._rows.values():
            row.destroy()
        for child in list(self.children.values()):
            if child is not self._empty:
                child.destroy()
        self._rows = {}

    def set_enabled(self, enabled):
        """Live only while a controllable flowgraph is running.

        Disabled is the default and the safe state: a SET written at any
        other time is a line typed at the board's shell prompt.
        """
        self._enabled = bool(enabled)
        for row in self._rows.values():
            row.set_enabled(self._enabled)

    def report(self, cid, error):
        """Mark a widget and say why, for a failure that never left the
        desktop (a value the operator typed that will not convert)."""
        self._note(cid, None, error=error)

    # -- traffic -----------------------------------------------------
    def send(self, control, value):
        if not self._enabled:
            return
        try:
            self._on_send(control, value)
        except Exception as exc:  # noqa: BLE001 -- shown, never raised on
            self._note(control.id, None, error=str(exc))

    def pulse(self, control, ms):
        if not self._enabled:
            return
        try:
            self._on_pulse(control, ms)
        except Exception as exc:  # noqa: BLE001
            self._note(control.id, None, error=str(exc))

    def acknowledge(self, reply):
        """Apply one FAU-CTL reply to the widget it names."""
        if reply is None or reply.kind == "READY":
            return
        if reply.ok:
            self._note(reply.id, reply.value)
        else:
            self._note(reply.id, None, error=reply.text)

    def _note(self, cid, value, error=None):
        row = self._rows.get(cid)
        if row is not None:
            row.note(value, error=error)
        if error:
            self._on_message("control %s: %s" % (cid, error))
