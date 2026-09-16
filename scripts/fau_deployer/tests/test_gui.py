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

Three more things here are behaviour rather than layout and are tested as
such: the log pane must be copyable (a read-only Tk text widget is not, by
default -- see App._bind_log), the file watcher must invalidate a stale
generated .py, and Deploy on a .grc must process BEFORE it sends.

Skipped without a display. No mainloop is entered and the window is
withdrawn, so nothing appears on screen; widget construction still happens
for real, which is what catches a typo'd option that would otherwise only
show up when somebody opens the tool.
"""

import os
import shutil
import tempfile
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

    def test_log_pane_is_read_only_and_accepts_lines(self):
        self.app._log("hello")
        self.app._log("bad", True)
        self.assertEqual(_state(self.app.log), "disabled")
        contents = self.app.log.get("1.0", "end")
        self.assertIn("hello", contents)
        self.assertIn("bad", contents)


class TestProcessButton(GuiTestCase):
    def test_nothing_selected_means_nothing_to_process(self):
        self.app.var_path.set("")
        self.app._set_state(gui.IDLE)
        self.assertEqual(_state(self.app.btn_process), "disabled")

    def test_a_generated_py_has_nothing_to_process(self):
        self.app.var_path.set("/tmp/fg.py")
        self.app._set_state(gui.IDLE)
        self.assertEqual(_state(self.app.btn_process), "disabled")

    def test_a_grc_enables_it_with_no_port_at_all(self):
        # Process is entirely local: compiling a flowgraph must not require
        # a board to be plugged in.
        self.app.var_port_override.set("")
        self.app.var_path.set("/tmp/fg.grc")
        self.app._set_state(gui.IDLE)
        self.assertEqual(_state(self.app.btn_process), "normal")

    def test_it_is_disabled_and_labelled_while_processing(self):
        self.app.var_path.set("/tmp/fg.grc")
        self.app._set_state(gui.PROCESSING)
        self.assertEqual(_state(self.app.btn_process), "disabled")
        self.assertEqual(_text(self.app.btn_process), "Processing...")

    def test_nothing_else_may_run_while_processing(self):
        self._select_ready()
        self.app.var_path.set("/tmp/fg.grc")
        self.app._set_state(gui.PROCESSING)
        self.assertEqual(_state(self.app.btn_deploy), "disabled")
        self.assertEqual(_state(self.app.btn_run), "disabled")
        self.assertEqual(_state(self.app.btn_browse), "disabled")


class TestLogCopy(GuiTestCase):
    """A -state disabled Tk text widget selects under the mouse but refuses
    keyboard focus, so the class-level <<Copy>> never fires: it looks
    copyable and is not. These pin the explicit bindings that fix it."""

    def setUp(self):
        super().setUp()
        # Construction already logged which bitstream file was loaded, and
        # these tests index from "1.0".
        self.app._clear_log()
        self.app._log("first line")
        self.app._log("second line")

    def _select(self, start, end):
        self.app.log.tag_add("sel", start, end)

    def _spy_on_see(self, at_bottom=True):
        """Record see() calls instead of performing them, and say where the
        view is.

        The window is withdrawn, so the text widget has no geometry and
        yview() reports nonsense -- _at_bottom() is unavoidably a question
        about a real mapped widget. What _log DECIDES from it is not, and
        that decision (chase the tail only if the reader was already at it)
        is the behaviour worth pinning.
        """
        calls = []
        self.app.log.see = calls.append
        self.app._at_bottom = lambda: at_bottom
        return calls

    def test_the_log_can_take_focus_despite_being_read_only(self):
        self.assertTrue(bool(self.app.log.cget("takefocus")))

    def test_button_1_is_wired_to_take_focus(self):
        # The fix for "selecting works but Ctrl-C does nothing": Tk's own
        # <Button-1> binding refuses to focus a -state disabled text
        # widget, so the widget must ask for focus itself. X focus cannot
        # be asserted against a withdrawn window, so what is pinned here is
        # the wiring and that the handler calls focus_set.
        self.assertIn("_on_log_click", self.app.log.bind("<Button-1>"))
        focused = []
        self.app.log.focus_set = lambda: focused.append(True)
        self.app._on_log_click(None)
        self.assertEqual(focused, [True])

    def test_copying_a_selection_puts_it_on_the_clipboard(self):
        self._select("1.0", "1.5")
        self.app._copy_selection()
        self.assertEqual(self.root.clipboard_get(), "first")

    def test_select_all_then_copy_takes_the_whole_log(self):
        self.app._select_all()
        self.app._copy_selection()
        self.assertIn("first line", self.root.clipboard_get())
        self.assertIn("second line", self.root.clipboard_get())

    def test_copy_everything_needs_no_selection(self):
        self.app._copy_all()
        self.assertIn("second line", self.root.clipboard_get())

    def test_copying_nothing_says_so_instead_of_raising(self):
        # sel.first raises when there is no selection; an operator pressing
        # Ctrl-C with nothing selected must get a line in the log, not a
        # traceback on a stdout they are not watching.
        self.app.log.tag_remove("sel", "1.0", "end")
        self.app._copy_selection()
        self.assertIn("nothing selected", self.app.log.get("1.0", "end"))

    def test_ctrl_c_is_bound_on_the_widget_itself(self):
        self.assertTrue(self.app.log.bind("<Control-c>"))
        self.assertTrue(self.app.log.bind("<Control-a>"))

    def test_clearing_empties_it_and_resumes_following(self):
        self.app.var_follow.set(False)
        self.app._clear_log()
        self.assertEqual(self.app.log.get("1.0", "end").strip(), "")
        self.assertTrue(self.app.var_follow.get())

    def test_new_output_does_not_scroll_away_from_a_reader(self):
        # Scrolling up to read or select something must survive the next
        # board line, or the pane is unusable exactly when it matters.
        self.app.var_follow.set(False)
        calls = self._spy_on_see(at_bottom=True)
        self.app._log("and another")
        self.assertEqual(calls, [])

    def test_a_view_scrolled_off_the_bottom_is_left_alone(self):
        # The automatic half: no checkbox needed, having scrolled up IS the
        # request to stop moving.
        self.app.var_follow.set(True)
        calls = self._spy_on_see(at_bottom=False)
        self.app._log("and another")
        self.assertEqual(calls, [])

    def test_following_chases_the_tail(self):
        self.app.var_follow.set(True)
        calls = self._spy_on_see(at_bottom=True)
        self.app._log("and another")
        self.assertEqual(calls, ["end"])

    def test_turning_following_back_on_jumps_to_the_end(self):
        self.app.var_follow.set(False)
        calls = self._spy_on_see()
        self.app.var_follow.set(True)
        self.app._on_follow_toggled()
        self.assertEqual(calls, ["end"])


class FakeGenerated:
    """Stands in for core.generate.Generated.

    A real one needs grcc; what the GUI actually consumes from it is a
    handful of attributes and a staleness answer, so that is what this
    provides.
    """

    def __init__(self, grc_path, py_path, stale=False, source_dir=None,
                 controls=(), spec_path=None):
        self.grc_path = os.path.abspath(grc_path)
        self.py_path = py_path
        self.headless_grc = py_path + ".headless.grc"
        self.build_dir = os.path.dirname(py_path)
        self.target = "rx"
        self.transform_report = None
        self.stale = stale
        self._source_dir = source_dir or os.path.dirname(self.grc_path)
        self.controls = list(controls)
        self.spec_path = spec_path

    @property
    def extra_files(self):
        return (self.spec_path,) if self.spec_path else ()

    @property
    def source_dir(self):
        return self._source_dir

    @property
    def main_name(self):
        return os.path.basename(self.py_path)

    def is_stale(self):
        return self.stale


class FlowgraphTestCase(GuiTestCase):
    """A real .grc and a real generated .py on disk, and a worker whose
    submit() only records -- nothing here should run grcc or open a port."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="fau_gui_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.grc = os.path.join(self.dir, "rx_demo.grc")
        with open(self.grc, "w") as fh:
            fh.write("options: {}\nblocks: []\n")
        self.py = os.path.join(self.dir, "rx_demo.py")
        with open(self.py, "w") as fh:
            fh.write("# generated\n")

        self.jobs = []
        self.app.worker.submit = (
            lambda name, fn, settings: self.jobs.append((name, fn, settings)))

    def _fresh(self):
        return FakeGenerated(self.grc, self.py, stale=False)

    def _stale(self):
        return FakeGenerated(self.grc, self.py, stale=True)

    def _touch(self, path):
        st = os.stat(path)
        bumped = st.st_mtime_ns + 10_000_000
        os.utime(path, ns=(bumped, bumped))


class TestPathTracking(FlowgraphTestCase):
    def test_selecting_a_file_points_the_watcher_at_it(self):
        self.app.var_path.set(self.grc)
        self.assertEqual(self.app.watcher.path, self.grc)

    def test_switching_flowgraphs_drops_the_previous_generated_python(self):
        # Otherwise Deploy would send the .py built from the file that was
        # selected a moment ago -- the exact wrong-file bug.
        self.app.var_path.set(self.grc)
        self.app.generated = self._fresh()
        other = os.path.join(self.dir, "other.grc")
        with open(other, "w") as fh:
            fh.write("options: {}\nblocks: []\n")
        self.app.var_path.set(other)
        self.assertIsNone(self.app.generated)

    def test_a_generated_result_for_a_different_grc_is_not_fresh(self):
        self.app.var_path.set(self.grc)
        self.app.generated = FakeGenerated("/elsewhere/other.grc", self.py)
        self.assertIsNone(self.app._fresh_generated())

    def test_a_stale_result_is_not_fresh(self):
        self.app.var_path.set(self.grc)
        self.app.generated = self._stale()
        self.assertIsNone(self.app._fresh_generated())

    def test_the_tracking_line_says_what_it_is_watching(self):
        self.app.var_path.set(self.grc)
        self.assertIn("rx_demo.grc", self.app.var_track.get())
        self.assertIn("not processed yet", self.app.var_track.get())

    def test_the_tracking_line_says_when_the_python_is_out_of_date(self):
        self.app.var_path.set(self.grc)
        self.app.generated = self._stale()
        self.app._refresh_track()
        self.assertIn("out of date", self.app.var_track.get())

    def test_the_tracking_line_names_the_generated_file_when_current(self):
        self.app.var_path.set(self.grc)
        self.app.generated = self._fresh()
        self.app._refresh_track()
        self.assertIn("rx_demo.py", self.app.var_track.get())
        self.assertIn("up to date", self.app.var_track.get())

    def test_a_py_is_described_as_needing_no_processing(self):
        self.app.var_path.set(self.py)
        self.assertIn("already generated", self.app.var_track.get())

    def test_a_changed_py_says_it_will_be_re_read(self):
        self.app.var_path.set(self.py)
        self.app.watcher.changed = True
        self.app._refresh_track()
        self.assertIn("re-reads it", self.app.var_track.get())

    def test_a_missing_file_is_called_out(self):
        self.app.var_path.set(os.path.join(self.dir, "gone.grc"))
        self.assertIn("does not exist", self.app.var_track.get())


class TestAutoProcessOnChange(FlowgraphTestCase):
    def _change_and_settle(self):
        self._touch(self.grc)
        self.app.watcher.poll()
        self.app.watcher._pending_since -= 10.0   # skip the settle window
        return self.app.watcher.poll()

    def test_a_saved_grc_queues_a_process_job(self):
        self.app.var_path.set(self.grc)
        self.app._set_state(gui.IDLE)
        self.assertTrue(self._change_and_settle())
        self.app._on_file_changed()
        self.assertEqual([name for name, _fn, _s in self.jobs], ["process"])
        self.assertEqual(self.app.state, gui.PROCESSING)

    def test_with_auto_processing_off_it_only_says_so(self):
        self.app.var_autoprocess.set(False)
        self.app.var_path.set(self.grc)
        self.app._set_state(gui.IDLE)
        self.app._on_file_changed()
        self.assertEqual(self.jobs, [])
        self.assertIn("auto-processing is off", self.app.log.get("1.0", "end"))

    def test_nothing_is_regenerated_while_a_flowgraph_is_running(self):
        # Swapping the generated file under a live run is not a thing to do
        # quietly, whatever the file on disk did.
        self.app.var_path.set(self.grc)
        self.app._set_state(gui.RUNNING)
        self.app._on_file_changed()
        self.assertEqual(self.jobs, [])
        self.assertIn("will not re-process", self.app.log.get("1.0", "end"))

    def test_a_changed_py_is_noted_but_nothing_is_generated(self):
        self.app.var_path.set(self.py)
        self.app._set_state(gui.IDLE)
        self.app._on_file_changed()
        self.assertEqual(self.jobs, [])
        self.assertIn("re-read from disk", self.app.log.get("1.0", "end"))

    def test_processing_rebaselines_the_watcher(self):
        self.app.var_path.set(self.grc)
        self._change_and_settle()
        self.assertTrue(self.app.watcher.changed)
        self.app._handle_event(("processed", self._fresh()))
        self.assertFalse(self.app.watcher.changed)


class TestProcessBeforeDeploy(FlowgraphTestCase):
    def setUp(self):
        super().setUp()
        self._select_ready()

    def test_deploying_a_grc_processes_first(self):
        self.app.var_path.set(self.grc)
        self.app._set_state(gui.IDLE)
        self.app._on_deploy()
        self.assertEqual([name for name, _fn, _s in self.jobs], ["process"])
        self.assertEqual(self.app.after_process, "deploy")

    def test_running_a_grc_processes_first(self):
        self.app.var_path.set(self.grc)
        self.app._set_state(gui.IDLE)
        self.app._on_run()
        self.assertEqual([name for name, _fn, _s in self.jobs], ["process"])
        self.assertEqual(self.app.after_process, "run")

    def test_an_up_to_date_grc_is_not_re_processed(self):
        self.app.var_path.set(self.grc)
        self.app.generated = self._fresh()
        self.app._set_state(gui.IDLE)
        self.app._on_deploy()
        self.assertEqual([name for name, _fn, _s in self.jobs], ["deploy"])

    def test_the_file_sent_for_a_grc_is_the_generated_python(self):
        self.app.var_path.set(self.grc)
        self.app.generated = self._fresh()
        source, dirs, _extra = self.app._deploy_source()
        self.assertEqual(source, self.py)
        # The .grc's directory has to be searched for sibling modules: the
        # generated file lives in the build dir, the helper it imports does
        # not.
        self.assertEqual(tuple(dirs), (self.dir,))

    def test_a_py_is_sent_as_it_is(self):
        self.app.var_path.set(self.py)
        source, dirs, _extra = self.app._deploy_source()
        self.assertEqual(source, self.py)
        self.assertEqual(tuple(dirs), ())

    def test_the_chained_action_runs_only_after_the_job_finishes(self):
        # The continuation has to wait for job_done, which is the one place
        # that resets the state -- dispatching from "processed" would have
        # the reset land on top of DEPLOYING and re-enable every button
        # mid-transfer.
        self.app.var_path.set(self.grc)
        self.app._set_state(gui.IDLE)
        self.app._on_deploy()
        self.jobs.clear()
        self.app._handle_event(("processed", self._fresh()))
        self.assertEqual(self.jobs, [])
        self.app._handle_event(("job_done", "process"))
        self.assertEqual([name for name, _fn, _s in self.jobs], ["deploy"])

    def test_a_failed_process_cancels_the_chained_deploy(self):
        # No "processed" event means no generated file; deploying anyway
        # would send whatever .py was in the build directory already.
        self.app.var_path.set(self.grc)
        self.app._set_state(gui.IDLE)
        self.app._on_deploy()
        self.jobs.clear()
        self.app._handle_event(("failed", "process", "grcc said no"))
        self.app._handle_event(("job_done", "process"))
        self.assertEqual(self.jobs, [])
        self.assertIn("cancelled", self.app.log.get("1.0", "end"))

    def test_the_chain_is_consumed_and_does_not_fire_twice(self):
        self.app.var_path.set(self.grc)
        self.app._set_state(gui.IDLE)
        self.app._on_deploy()
        self.app._handle_event(("processed", self._fresh()))
        self.app._handle_event(("job_done", "process"))
        self.jobs.clear()
        self.app._handle_event(("job_done", "process"))
        self.assertEqual(self.jobs, [])
        self.assertIsNone(self.app.after_process)


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
