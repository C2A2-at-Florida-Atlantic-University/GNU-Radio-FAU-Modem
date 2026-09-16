#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""controls_panel.py: the widgets, the coalescing, and the write gate.

Two of these are safety properties rather than UI polish and are tested as
such: a disabled panel must send NOTHING (a SET written while no flowgraph
is running is a line typed at the board's shell prompt), and a value the
operator moves past during a drag must not each become a line on a shared
serial console.

Skipped without a display. No mainloop is entered and the root is
withdrawn, so nothing appears on screen -- but the widgets are built for
real, which is what catches an option Tk will not accept.
"""

import os
import unittest

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

if HAVE_DISPLAY:
    import tkinter as tk
    from .. import controls_panel as panel_mod

from ..core import controls as controls_mod
from ..core.controls import Control


def a_range(cid="gain", **over):
    kw = dict(start=0.0, stop=100.0, step=1.0, widget="counter_slider")
    kw.update(over)
    return Control(cid, controls_mod.KIND_RANGE, "Gain", "float", 50.0, **kw)


def a_chooser(cid="mode", widget="combo_box"):
    return Control(cid, controls_mod.KIND_CHOOSER, "Mode", "str", "b",
                   options=["a", "b", "c"], labels=["A", "B", "C"],
                   widget=widget)


def a_check_box(cid="armed"):
    return Control(cid, controls_mod.KIND_CHECK_BOX, "Armed", "int", 0,
                   true_value=1, false_value=0)


def a_button(cid="fire"):
    return Control(cid, controls_mod.KIND_PUSH_BUTTON, "Fire", "int", 0,
                   pressed=1, released=0)


def an_entry(cid="freq", dtype="float"):
    return Control(cid, controls_mod.KIND_ENTRY, "Freq", dtype, 1e6)


def a_free_entry(cid="half"):
    return Control(cid, controls_mod.KIND_RANGE, "Half", "float", None,
                   free_entry=True, note="bounds unavailable: stop")


@unittest.skipUnless(HAVE_DISPLAY, "no display available")
class PanelTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.sent = []
        self.pulsed = []
        self.messages = []
        self.panel = panel_mod.ControlPanel(
            self.root,
            lambda c, v: self.sent.append((c.id, v)),
            lambda c, ms: self.pulsed.append((c.id, ms)),
            on_message=self.messages.append)
        self.panel.pack()
        self.addCleanup(self.root.destroy)

    def pump(self, times=3):
        """Let Tk run queued `after` callbacks -- the coalescing timer."""
        for _ in range(times):
            self.root.update_idletasks()
            self.root.update()

    def wait_for_flush(self):
        import time
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            self.pump()
            if self.sent:
                return
            time.sleep(0.01)

    def row(self, cid):
        return self.panel._rows[cid]


class TestBuilding(PanelTestCase):
    def test_empty_spec_says_so(self):
        self.panel.set_controls([])
        self.assertEqual(self.panel.ids, ())

    def test_builds_one_row_per_control(self):
        self.panel.set_controls([a_range(), a_chooser(), a_check_box(),
                                 a_button(), an_entry()])
        self.assertEqual(self.panel.ids,
                         ("gain", "mode", "armed", "fire", "freq"))

    def test_each_kind_picks_its_widget(self):
        cases = [
            (a_range(), panel_mod._RangeRow),
            (a_chooser(), panel_mod._ChooserRow),
            (a_check_box(), panel_mod._CheckBoxRow),
            (a_button(), panel_mod._PushButtonRow),
            (an_entry(), panel_mod._EntryRow),
            (an_entry("flag", "bool"), panel_mod._BoolEntryRow),
            (a_free_entry(), panel_mod._EntryRow),
        ]
        self.panel.set_controls([c for c, _ in cases])
        for control, expected in cases:
            self.assertIsInstance(self.row(control.id), expected,
                                  control.id)

    def test_a_free_entry_range_becomes_a_text_box_not_a_slider(self):
        # Degrade and report: the control is still offered.
        self.panel.set_controls([a_free_entry()])
        self.assertIsInstance(self.row("half"), panel_mod._EntryRow)
        self.assertIn("bounds", str(self.row("half").status.cget("text")))

    def test_rebuilding_replaces_the_previous_panel(self):
        self.panel.set_controls([a_range()])
        self.panel.set_controls([a_chooser()])
        self.assertEqual(self.panel.ids, ("mode",))

    def test_starts_showing_each_default(self):
        self.panel.set_controls([a_range()])
        self.assertIn("50.0", str(self.row("gain").status.cget("text")))


class TestWriteGate(PanelTestCase):
    """A disabled panel must send nothing at all.

    Not politeness: outside a run there is no foreground process on the
    board, so a SET line lands at the shell prompt and is executed as a
    command.
    """

    def test_disabled_by_default(self):
        self.panel.set_controls([a_range(), a_check_box(), a_button()])
        self.row("gain").stage(1.0)
        self.row("gain").flush()
        self.row("armed")._toggled()
        self.row("fire")._pulse()
        self.pump()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pulsed, [])

    def test_enabled_sends(self):
        self.panel.set_controls([a_range()])
        self.panel.set_enabled(True)
        self.row("gain").stage(12.0)
        self.row("gain").flush()
        self.assertEqual(self.sent, [("gain", 12.0)])

    def test_disabling_again_stops_sending(self):
        self.panel.set_controls([a_range()])
        self.panel.set_enabled(True)
        self.panel.set_enabled(False)
        self.row("gain").stage(3.0)
        self.row("gain").flush()
        self.assertEqual(self.sent, [])

    def test_widgets_are_greyed_out_when_disabled(self):
        self.panel.set_controls([a_range(), a_chooser(), a_button()])
        self.panel.set_enabled(False)
        for cid in ("gain", "mode", "fire"):
            for widget in self.row(cid).widgets:
                self.assertEqual(str(widget.cget("state")), "disabled", cid)

    def test_a_combobox_re_enables_readonly_not_normal(self):
        # Otherwise the operator could type a value off the option list.
        self.panel.set_controls([a_chooser()])
        self.panel.set_enabled(False)
        self.panel.set_enabled(True)
        combo = self.row("mode").widgets[0]
        self.assertEqual(str(combo.cget("state")), "readonly")

    def test_a_raising_callback_is_shown_not_propagated(self):
        def boom(_c, _v):
            raise RuntimeError("nothing is running")
        p = panel_mod.ControlPanel(self.root, boom, boom,
                                   on_message=self.messages.append)
        self.addCleanup(p.destroy)
        p.set_controls([a_range()])
        p.set_enabled(True)
        p._rows["gain"].stage(1.0)
        p._rows["gain"].flush()          # must not raise
        self.assertIn("nothing is running",
                      str(p._rows["gain"].status.cget("text")))


class TestCoalescing(PanelTestCase):
    def setUp(self):
        PanelTestCase.setUp(self)
        self.panel.set_controls([a_range()])
        self.panel.set_enabled(True)

    def test_a_drag_sends_far_fewer_lines_than_it_has_values(self):
        row = self.row("gain")
        for value in range(0, 100):
            row.stage(float(value))
        self.wait_for_flush()
        self.assertLess(len(self.sent), 20, self.sent)
        self.assertGreaterEqual(len(self.sent), 1)

    def test_the_last_value_of_a_drag_is_the_one_that_sticks(self):
        row = self.row("gain")
        for value in range(0, 50):
            row.stage(float(value))
        row.flush()   # what <ButtonRelease-1> does
        self.assertEqual(self.sent[-1], ("gain", 49.0))

    def test_flush_with_nothing_staged_sends_nothing(self):
        self.row("gain").flush()
        self.assertEqual(self.sent, [])

    def test_a_flushed_value_is_not_sent_twice_by_the_timer(self):
        row = self.row("gain")
        row.stage(7.0)
        row.flush()
        self.pump(5)
        self.assertEqual(self.sent, [("gain", 7.0)])


class TestValues(PanelTestCase):
    def setUp(self):
        PanelTestCase.setUp(self)
        self.panel.set_controls([
            a_range(), a_range("count", start=0, stop=10, step=1),
            a_chooser(), a_chooser("radio", widget="radio_buttons"),
            a_check_box(), a_button(), an_entry(),
            an_entry("flag", "bool"), a_free_entry()])
        self.panel.set_enabled(True)

    def test_range_snaps_to_its_step(self):
        row = self.row("gain")
        self.assertEqual(row._quantize(12.4), 12.0)
        self.assertEqual(row._quantize(12.6), 13.0)

    def test_range_clamps_to_its_bounds(self):
        row = self.row("gain")
        self.assertEqual(row._quantize(-5.0), 0.0)
        self.assertEqual(row._quantize(1e9), 100.0)

    def test_an_int_range_sends_ints(self):
        control = Control("count", controls_mod.KIND_RANGE, "N", "int", 5,
                          start=0, stop=10, step=1, widget="counter_slider")
        self.panel.set_controls([control])
        self.panel.set_enabled(True)
        row = self.row("count")
        row.stage(row._quantize(7.4))
        row.flush()
        self.assertEqual(self.sent, [("count", 7)])
        self.assertIsInstance(self.sent[0][1], int)

    def test_chooser_sends_the_option_not_the_label(self):
        row = self.row("mode")
        row._picked(2)
        self.assertEqual(self.sent[-1], ("mode", "c"))

    def test_radio_chooser_sends_the_option(self):
        row = self.row("radio")
        row.var.set(0)
        row._picked()
        self.assertEqual(self.sent[-1], ("radio", "a"))

    def test_check_box_sends_the_declared_values_not_booleans(self):
        row = self.row("armed")
        row.var.set(True)
        row._toggled()
        self.assertEqual(self.sent[-1], ("armed", 1))
        row.var.set(False)
        row._toggled()
        self.assertEqual(self.sent[-1], ("armed", 0))

    def test_push_button_pulses_rather_than_sending_two_values(self):
        self.row("fire")._pulse()
        self.assertEqual(self.pulsed, [("fire", panel_mod.PULSE_MS)])
        self.assertEqual(self.sent, [])

    def test_hold_sends_the_two_edges(self):
        row = self.row("fire")
        row._edge("pressed")
        row._edge("released")
        self.assertEqual(self.sent, [("fire", 1), ("fire", 0)])

    def test_entry_uses_engineering_notation(self):
        row = self.row("freq")
        row.var.set("2.5M")
        row._submit()
        self.assertEqual(self.sent[-1], ("freq", 2.5e6))

    def test_entry_reports_a_bad_value_against_the_widget(self):
        row = self.row("freq")
        row.var.set("not a number")
        row._submit()
        self.assertEqual(self.sent, [])
        self.assertEqual(str(row.status.cget("foreground")),
                         panel_mod.ERR_COLOR)

    def test_the_full_reason_goes_to_the_log_not_just_the_widget(self):
        # The status column is ~22 characters; a truncated explanation is
        # barely better than none.
        row = self.row("freq")
        row.var.set("not a number")
        row._submit()
        self.assertTrue(any("engineering" in m.lower()
                            for m in self.messages), self.messages)

    def test_bool_entry_is_a_real_checkbox(self):
        row = self.row("flag")
        row.var.set(True)
        row._toggled()
        self.assertIs(self.sent[-1][1], True)

    def test_free_entry_still_sends(self):
        row = self.row("half")
        row.var.set("123.5")
        row._submit()
        self.assertEqual(self.sent[-1], ("half", 123.5))


class TestAcknowledgement(PanelTestCase):
    def setUp(self):
        PanelTestCase.setUp(self)
        self.panel.set_controls([a_range(), a_chooser()])
        self.panel.set_enabled(True)

    def test_ok_shows_the_acknowledged_value(self):
        self.panel.acknowledge(
            controls_mod.parse_reply("FAU-CTL-abc123 OK gain 42.0", "abc123"))
        self.assertIn("42.0", str(self.row("gain").status.cget("text")))
        self.assertEqual(str(self.row("gain").status.cget("foreground")),
                         panel_mod.OK_COLOR)

    def test_err_marks_that_widget_not_just_the_log(self):
        self.panel.acknowledge(controls_mod.parse_reply(
            "FAU-CTL-abc123 ERR gain set_gain(1.0) raised ValueError: no",
            "abc123"))
        self.assertEqual(str(self.row("gain").status.cget("foreground")),
                         panel_mod.ERR_COLOR)

    def test_an_err_on_one_widget_leaves_the_others_alone(self):
        self.panel.acknowledge(controls_mod.parse_reply(
            "FAU-CTL-abc123 OK mode 'a'", "abc123"))
        self.panel.acknowledge(controls_mod.parse_reply(
            "FAU-CTL-abc123 ERR gain nope", "abc123"))
        self.assertEqual(str(self.row("mode").status.cget("foreground")),
                         panel_mod.OK_COLOR)

    def test_ready_is_not_an_acknowledgement_of_anything(self):
        self.panel.acknowledge(
            controls_mod.parse_reply("FAU-CTL-abc123 READY gain mode",
                                     "abc123"))
        # No widget should claim a value it was never told about.
        self.assertIn("50.0", str(self.row("gain").status.cget("text")))

    def test_a_reply_for_an_unknown_id_is_ignored(self):
        self.panel.acknowledge(
            controls_mod.parse_reply("FAU-CTL-abc123 OK ghost 1", "abc123"))

    def test_none_is_ignored(self):
        self.panel.acknowledge(None)


if __name__ == "__main__":
    unittest.main()
