#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""gui.py's state machine, and that the widget tree builds at all.

The state machine is the part worth testing rather than the layout: the
console is one exclusive resource, so "Deploy while the flowgraph is
running" and "reload the PL while DMA is live" have to be impossible, not
merely discouraged. The second of those can wedge the board (core/fpga.py),
which makes button gating a safety property.

Skipped without a display. No mainloop is entered and the window is
withdrawn, so nothing appears on screen; widget construction still happens
for real, which is what catches a typo'd option that would otherwise only
show up when somebody opens the tool.
"""

import os
import unittest

HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

if HAVE_DISPLAY:
    import tkinter as tk
    from .. import gui


def _state(widget):
    return str(widget.cget("state"))


def _text(widget):
    return str(widget.cget("text"))


@unittest.skipUnless(HAVE_DISPLAY, "no display available")
class GuiTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = gui.App(self.root)
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.app.worker.shutdown)

    def _first_board(self):
        if not (self.app.bitstreams and self.app.bitstreams.names):
            self.skipTest("the tracked bitstream file defines no boards")
        return self.app.bitstreams.board(self.app.bitstreams.names[0])

    def _select(self, board):
        self.app.var_board.set(board.name)
        self.app._on_board_changed()

    def _select_ready(self):
        """Select a board AND stand in for the operator choosing a port.

        The port has to be set explicitly in every test that needs one:
        nothing on disk records a port and nothing infers one, because
        assignments are not static and a guessed port deploys to the wrong
        board. The Port menu is the only source.
        """
        board = self._first_board()
        self._select(board)
        self.app.var_port_override.set("/dev/ttyUSB-test")
        return board


class TestConstruction(GuiTestCase):
    def test_the_window_builds_and_reads_the_bitstream_file(self):
        self.assertEqual(self.root.title(), gui.TITLE)
        self.assertIsNotNone(self.app.bitstreams)
        self.assertIsNone(self.app.bitstreams_error)
        self.assertIn(self.app.var_board.get(), self.app.bitstreams.names)

    def test_process_is_a_visible_disabled_placeholder(self):
        # Present so the pipeline reads as three phases, disabled because
        # none of it exists in core yet.
        self.assertEqual(_state(self.app.btn_process), "disabled")
        self.assertEqual(_text(self.app.btn_process), "Process")

    def test_log_pane_is_read_only_and_accepts_lines(self):
        self.app._log("hello")
        self.app._log("bad", True)
        self.assertEqual(_state(self.app.log), "disabled")
        contents = self.app.log.get("1.0", "end")
        self.assertIn("hello", contents)
        self.assertIn("bad", contents)


class TestActionGating(GuiTestCase):
    def test_no_flowgraph_means_nothing_to_deploy_or_run(self):
        self._select_ready()
        self.app.var_path.set("")
        self.app._set_state(gui.IDLE)
        self.assertEqual(_state(self.app.btn_deploy), "disabled")
        self.assertEqual(_state(self.app.btn_run), "disabled")

    def test_a_flowgraph_and_a_port_enable_deploy_and_run(self):
        self._select_ready()
        self.app.var_path.set("/tmp/fg.py")
        self.app._set_state(gui.IDLE)
        self.assertEqual(_state(self.app.btn_deploy), "normal")
        self.assertEqual(_state(self.app.btn_run), "normal")

    def test_out_of_the_box_no_port_means_nothing_touches_the_board(self):
        # Nothing records or guesses a port, so a freshly opened tool must
        # refuse every board action until the operator picks one.
        # Auto-selecting is how a deploy silently lands on the wrong board.
        self._select(self._first_board())
        self.app.var_port_override.set("")
        self.app.var_path.set("/tmp/fg.py")
        self.app._set_state(gui.IDLE)
        self.assertEqual(self.app._effective_port(), "")
        self.assertEqual(_state(self.app.btn_deploy), "disabled")
        self.assertEqual(_state(self.app.btn_run), "disabled")
        self.assertEqual(_state(self.app.btn_bitstream), "disabled")


class TestExclusiveConsole(GuiTestCase):
    def setUp(self):
        super().setUp()
        self._select_ready()
        self.app.var_path.set("/tmp/fg.py")

    def test_deploy_becomes_cancel_and_locks_everything_else(self):
        self.app._set_state(gui.DEPLOYING)
        self.assertEqual(_text(self.app.btn_deploy), "Cancel")
        self.assertEqual(_state(self.app.btn_deploy), "normal")
        self.assertEqual(_state(self.app.btn_run), "disabled")
        self.assertEqual(_state(self.app.btn_bitstream), "disabled")
        self.assertEqual(_state(self.app.entry_path), "disabled")

    def test_running_offers_terminate_and_blocks_deploy(self):
        # Deploy during a run would put the receiver on a console the
        # flowgraph owns; the receiver would eat its output as protocol
        # noise.
        self.app._set_state(gui.RUNNING)
        self.assertEqual(_text(self.app.btn_run), "Terminate")
        self.assertEqual(_state(self.app.btn_run), "normal")
        self.assertEqual(_state(self.app.btn_deploy), "disabled")

    def test_running_blocks_the_bitstream_load(self):
        # THE safety case: replacing the PL under a live DMA burst is the
        # DMACR.Reset-mid-burst failure, which can wedge the board until a
        # power cycle.
        self.app._set_state(gui.RUNNING)
        self.assertEqual(_state(self.app.btn_bitstream), "disabled")

    def test_stopping_is_not_re_triggerable(self):
        # One Ctrl-C is all this tool will ever send; a second click must
        # not queue another stop or suggest escalation is available.
        self.app._set_state(gui.STOPPING)
        self.assertEqual(_state(self.app.btn_run), "disabled")
        self.assertIn("Stopping", _text(self.app.btn_run))

    def test_busy_locks_the_board_actions(self):
        self.app._set_state(gui.BUSY)
        for widget in (self.app.btn_deploy, self.app.btn_run,
                       self.app.btn_bitstream):
            self.assertEqual(_state(widget), "disabled")


class TestStateResetSurvivesFailure(GuiTestCase):
    """Every job ends in a "job_done" event whatever happened inside it,
    and that -- not each job's own success path -- is where the state
    resets. Regression guard: an earlier version reset state only on
    success, so a job that raised in ensure_session() (port busy, board
    unplugged) left the UI showing "Terminate" with nothing running and no
    way back.
    """

    def test_a_failed_job_returns_to_idle(self):
        self.app.connected = True
        self.app._set_state(gui.RUNNING)
        self.app._handle_event(("failed", "run", "TransportError: port busy"))
        self.app._handle_event(("job_done", "run"))
        self.assertEqual(self.app.state, gui.IDLE)

    def test_a_failed_job_with_no_session_returns_to_offline(self):
        self.app.connected = False
        self.app._set_state(gui.DEPLOYING)
        self.app._handle_event(("job_done", "deploy"))
        self.assertEqual(self.app.state, gui.OFFLINE)

    def test_wedged_survives_job_done(self):
        # The one state that must NOT be cleared: DMA may still be live.
        self.app.connected = True
        self.app._set_state(gui.WEDGED)
        self.app._handle_event(("job_done", "run"))
        self.assertEqual(self.app.state, gui.WEDGED)

    def test_a_successful_job_returns_to_idle(self):
        self.app.connected = True
        self.app._set_state(gui.BUSY)
        self.app._handle_event(("job_done", "load-bitstream"))
        self.assertEqual(self.app.state, gui.IDLE)


class TestCredentials(GuiTestCase):
    """Secrets come from credentials.json (gitignored) or a per-session
    override -- never from bitstreams.json, which is tracked."""

    def test_defaults_when_no_credentials_file(self):
        if self.app.credentials:
            self.skipTest("this machine has a credentials.json")
        creds = self.app.current_creds
        self.assertEqual(creds.user, gui.boards_mod.USER_DEFAULT)
        self.assertEqual(creds.password, gui.boards_mod.PASSWORD_DEFAULT)

    def test_a_session_override_wins_and_is_per_board(self):
        board = self._select_ready()
        self.app.cred_overrides[board.name] = gui.boards_mod.BoardCreds(
            user="someone", password="else")
        self.assertEqual(self.app.current_creds.user, "someone")
        self.assertEqual(self.app._settings()["password"], "else")

        other = [n for n in self.app.bitstreams.names if n != board.name]
        if other:
            self.app.var_board.set(other[0])
            self.assertNotEqual(self.app.current_creds.user, "someone")


class TestWedgedIsSticky(GuiTestCase):
    def test_nothing_can_touch_the_board_while_wedged(self):
        # A flowgraph was stopped and never confirmed it halted, so DMA may
        # still be live. The only way out is Disconnect -- deliberately not
        # a force-kill.
        self._select_ready()
        self.app.var_path.set("/tmp/fg.py")
        self.app._set_state(gui.WEDGED)
        for widget in (self.app.btn_deploy, self.app.btn_run,
                       self.app.btn_bitstream):
            self.assertEqual(_state(widget), "disabled")


class TestBitstreamGating(GuiTestCase):
    def test_a_selected_bitstream_can_be_loaded_when_idle(self):
        board = self._select_ready()
        self.app.var_mode.set(board.modes[0])
        self.app._set_state(gui.IDLE)
        self.assertEqual(_state(self.app.btn_bitstream), "normal")
        self.assertEqual(self.app.current_bitstream,
                         board.bitstream(board.modes[0]))

    def test_every_mode_of_a_board_resolves_to_its_own_path(self):
        board = self._select_ready()
        for mode in board.modes:
            self.app.var_mode.set(mode)
            self.assertEqual(self.app.current_mode, mode)
            self.assertEqual(self.app.current_bitstream,
                             board.bitstream(mode))

    def test_bootstrap_leads_the_mode_list_when_present(self):
        board = self._first_board()
        if "bootstrap" not in board.bitstreams:
            self.skipTest("no bootstrap entry in the tracked file")
        self.assertEqual(board.modes[0], "bootstrap")

    def test_selecting_a_mode_yields_a_bootstrap_first_sequence(self):
        # What Load Bitstream will actually do, which the confirmation
        # dialog spells out step by step.
        board = self._select_ready()
        if "bootstrap" not in board.bitstreams:
            self.skipTest("no bootstrap entry in the tracked file")
        for mode in board.modes:
            if mode == "bootstrap":
                continue
            self.app.var_mode.set(mode)
            self.assertEqual(
                [n for n, _ in self.app.current_sequence],
                ["bootstrap", mode])

    def test_selecting_bootstrap_is_a_single_step(self):
        board = self._select_ready()
        if "bootstrap" not in board.bitstreams:
            self.skipTest("no bootstrap entry in the tracked file")
        self.app.var_mode.set("bootstrap")
        self.assertEqual([n for n, _ in self.app.current_sequence],
                         ["bootstrap"])


class TestPortSelection(GuiTestCase):
    def test_the_port_menu_is_the_only_source(self):
        self._select(self._first_board())
        self.app.var_port_override.set("")
        self.assertEqual(self.app._effective_port(), "")
        self.app.var_port_override.set("/dev/ttyUSB9")
        self.assertEqual(self.app._effective_port(), "/dev/ttyUSB9")

    def test_selecting_a_different_board_does_not_change_the_port(self):
        # The port belongs to the session, not to the board -- switching
        # boards must not silently retarget an already-chosen port.
        self._select(self._first_board())
        self.app.var_port_override.set("/dev/ttyUSB9")
        for name in self.app.bitstreams.names:
            self.app.var_board.set(name)
            self.app._on_board_changed()
            self.assertEqual(self.app._effective_port(), "/dev/ttyUSB9")

    def test_baud_override_falls_back_on_junk(self):
        self._select(self._first_board())
        self.app.var_baud_override.set("not-a-number")
        self.assertEqual(self.app._effective_baud(),
                         gui.boards_mod.BAUD_DEFAULT)
        self.app.var_baud_override.set("9600")
        self.assertEqual(self.app._effective_baud(), 9600)

    def test_detected_ports_prefer_stable_by_id_paths(self):
        found = gui.App._detect_ports()
        by_id = [p for p in found if "/by-id/" in p]
        if not by_id:
            self.skipTest("no /dev/serial/by-id entries on this machine")
        # by-id first: ttyUSB numbering is assignment order and swaps
        # between two boards on a replug.
        self.assertTrue(found[0].startswith("/dev/serial/by-id/"))


if __name__ == "__main__":
    unittest.main()
