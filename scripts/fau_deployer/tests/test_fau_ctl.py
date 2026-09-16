#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Tests for board/fau_ctl.py, the board-side control dispatcher.

`fau_ctl` runs on the board, but nothing in it is board-specific: it reads
lines off a stream and calls `set_<id>` on whatever object it was handed.
So it is exercised here for real -- a stub top block with the same setters
grcc generates, a real os.pipe() for the console, and the real reader
thread -- rather than being mocked out.

TestGeneratedSnippet goes one step further and runs the dispatcher through
the snippet text `grcc` actually emitted, because the one thing a unit test
of fau_ctl cannot catch is the snippet calling it wrongly. That is not
hypothetical: a snippet body is emitted as `def snipfcn_<name>(self)` and
called as `snipfcn_<name>(tb)`, so a body saying `fau_ctl.start(tb)` --
which the plan doc's draft said -- raises NameError on the board.
"""

import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest

BOARD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "board")
if BOARD_DIR not in sys.path:
    sys.path.insert(0, BOARD_DIR)

import fau_ctl  # noqa: E402  -- imported the way the board imports it

from ..core import controls  # noqa: E402

SETTLE = 2.0  # generous: these wait on a real thread, they do not sleep it


class StubTopBlock:
    """The setters grcc generates for a flowgraph's variables, and nothing
    else -- which is exactly the surface fau_ctl is allowed to touch."""

    def __init__(self):
        self.gain = 0.5
        self.mode = "a"
        self.armed = 0
        self.fire = 0
        self.history = []
        self.stopped = 0
        self.waited = 0

    def set_gain(self, value):
        self.gain = value
        self.history.append(("gain", value))

    def set_mode(self, value):
        self.mode = value
        self.history.append(("mode", value))

    def set_armed(self, value):
        self.armed = value
        self.history.append(("armed", value))

    def set_fire(self, value):
        self.fire = value
        self.history.append(("fire", value))

    def set_explodes(self, value):
        raise ValueError("this block does not like %r" % (value,))

    def stop(self):
        self.stopped += 1

    def wait(self):
        self.waited += 1


SPEC_CONTROLS = [
    {"id": "gain", "kind": "range", "label": "Gain", "dtype": "float",
     "default": 0.5, "free_entry": False, "start": 0.0, "stop": 1.0,
     "step": 0.01, "widget": "slider"},
    {"id": "mode", "kind": "chooser", "label": "Mode", "dtype": "str",
     "default": "a", "free_entry": False, "options": ["a", "b"],
     "labels": ["A", "B"], "widget": "combo_box"},
    {"id": "armed", "kind": "check_box", "label": "Armed", "dtype": "int",
     "default": 0, "free_entry": False, "true_value": 1, "false_value": 0},
    {"id": "fire", "kind": "push_button", "label": "Fire", "dtype": "int",
     "default": 0, "free_entry": False, "pressed": 1, "released": 0},
    {"id": "explodes", "kind": "entry", "label": "Boom", "dtype": "int",
     "default": 0, "free_entry": False},
]


def a_dispatcher(tb=None, nonce="abc123", replies=None):
    tb = tb or StubTopBlock()
    out = replies if replies is not None else []
    disp = fau_ctl.Dispatcher(tb, SPEC_CONTROLS, nonce, emit=out.append)
    return tb, disp, out


class TestDispatcher(unittest.TestCase):
    def test_set_applies_and_acks(self):
        tb, disp, out = a_dispatcher()
        self.assertTrue(disp.handle("SET gain 0.75"))
        self.assertEqual(tb.gain, 0.75)
        self.assertEqual(out, ["FAU-CTL-abc123 OK gain 0.75"])

    def test_reply_is_parseable_by_the_desktop_half(self):
        _tb, disp, out = a_dispatcher()
        disp.handle("SET mode 'b'")
        reply = controls.parse_reply(out[-1], "abc123")
        self.assertTrue(reply.ok)
        self.assertEqual((reply.id, reply.value), ("mode", "b"))

    def test_unknown_id_is_refused_with_the_whitelist(self):
        tb, disp, out = a_dispatcher()
        self.assertTrue(disp.handle("SET nonsense 1"))
        self.assertEqual(tb.history, [])
        self.assertIn("ERR nonsense", out[-1])
        self.assertIn("gain", out[-1])  # says what IS settable

    def test_an_expression_is_refused_not_evaluated(self):
        # The reason this is literal_eval and not eval. A corrupted or
        # hostile line must be able to fail, never to execute.
        tb, disp, out = a_dispatcher()
        disp.handle("SET gain __import__('os').system('touch /tmp/pwned')")
        self.assertEqual(tb.history, [])
        self.assertIn("not a Python literal", out[-1])
        self.assertFalse(os.path.exists("/tmp/pwned"))

    def test_chooser_options_are_validated_on_the_board_too(self):
        tb, disp, out = a_dispatcher()
        disp.handle("SET mode 'z'")
        self.assertEqual(tb.mode, "a")
        self.assertIn("not one of this chooser's options", out[-1])

    def test_a_setter_that_raises_becomes_an_ERR_not_a_crash(self):
        tb, disp, out = a_dispatcher()
        self.assertTrue(disp.handle("SET explodes 3"))
        self.assertIn("ERR explodes", out[-1])
        self.assertIn("ValueError", out[-1])
        # ...and the channel still works afterwards.
        disp.handle("SET gain 0.25")
        self.assertEqual(tb.gain, 0.25)

    def test_a_missing_setter_is_reported_as_a_spec_mismatch(self):
        class Bare:
            pass
        _tb, disp, out = a_dispatcher(tb=Bare())
        disp.handle("SET gain 0.5")
        self.assertIn("does not match what is deployed", out[-1])

    def test_err_reasons_are_kept_to_one_line(self):
        class Multi:
            def set_gain(self, value):
                raise ValueError("line one\nline two")
        _tb, disp, out = a_dispatcher(tb=Multi())
        disp.handle("SET gain 1.0")
        self.assertEqual(len(out), 1)
        self.assertNotIn("\n", out[0])

    def test_non_control_lines_are_not_consumed(self):
        _tb, disp, out = a_dispatcher()
        for line in ("", "underruns: 0", "Traceback (most recent call last):",
                     "settings gain", "SETTLE gain 1"):
            self.assertFalse(disp.handle(line), line)
        self.assertEqual(out, [])

    def test_malformed_control_lines_are_consumed_and_reported(self):
        _tb, disp, out = a_dispatcher()
        self.assertTrue(disp.handle("SET"))
        self.assertTrue(disp.handle("SET gain"))
        self.assertEqual(len(out), 2)
        self.assertTrue(all("ERR" in line for line in out))

    def test_verb_is_case_insensitive(self):
        tb, disp, _out = a_dispatcher()
        disp.handle("set gain 0.9")
        self.assertEqual(tb.gain, 0.9)

    def test_string_values_with_spaces_survive(self):
        spec = [{"id": "gain", "kind": "entry", "label": "G", "dtype": "str",
                 "default": "", "free_entry": False}]
        tb = StubTopBlock()
        out = []
        disp = fau_ctl.Dispatcher(tb, spec, "abc123", emit=out.append)
        disp.handle("SET gain 'hello there'")
        self.assertEqual(tb.gain, "hello there")


class TestPulse(unittest.TestCase):
    def test_pulse_presses_then_releases(self):
        tb, disp, out = a_dispatcher()
        t0 = time.monotonic()
        disp.handle("PULSE fire 40")
        elapsed = time.monotonic() - t0
        self.assertEqual([v for k, v in tb.history if k == "fire"], [1, 0])
        self.assertGreaterEqual(elapsed, 0.035)
        self.assertIn("OK fire", out[-1])

    def test_pulse_rests_at_released(self):
        tb, disp, _out = a_dispatcher()
        disp.handle("PULSE fire 1")
        self.assertEqual(tb.fire, 0)

    def test_pulse_needs_pressed_released_values(self):
        _tb, disp, out = a_dispatcher()
        disp.handle("PULSE gain 10")
        self.assertIn("use SET", out[-1])

    def test_absurd_width_is_refused(self):
        tb, disp, out = a_dispatcher()
        disp.handle("PULSE fire 999999")
        self.assertEqual(tb.history, [])
        self.assertIn("outside 0..", out[-1])

    def test_negative_width_is_refused(self):
        _tb, disp, out = a_dispatcher()
        disp.handle("PULSE fire -5")
        self.assertIn("outside 0..", out[-1])

    def test_non_numeric_width_is_refused(self):
        _tb, disp, out = a_dispatcher()
        disp.handle("PULSE fire soon")
        self.assertIn("milliseconds", out[-1])

    def test_a_pulse_is_not_interleaved_with_a_concurrent_set(self):
        """The hold blocks the channel on purpose: a value landing between
        the press and the release would make the edge mean nothing."""
        tb, disp, _out = a_dispatcher()
        done = threading.Event()

        def other():
            time.sleep(0.02)
            disp.handle("SET fire 99")
            done.set()

        thread = threading.Thread(target=other)
        thread.start()
        disp.handle("PULSE fire 120")
        done.wait(SETTLE)
        thread.join(SETTLE)
        fires = [v for k, v in tb.history if k == "fire"]
        self.assertEqual(fires[:2], [1, 0])
        self.assertEqual(fires[-1], 99)


class TestSpecAndNonceFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fau_ctl_files_")

    def _write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_loads_controls_from_a_spec(self):
        path = self._write(fau_ctl.SPEC_FILENAME, json.dumps(
            {"version": 1, "flowgraph": "x", "controls": SPEC_CONTROLS}))
        got = fau_ctl.load_controls(path)
        self.assertEqual([c["id"] for c in got],
                         [c["id"] for c in SPEC_CONTROLS])

    def test_a_missing_spec_yields_no_controls_rather_than_raising(self):
        # An absent sidecar must not take down a deployed run.
        self.assertEqual(
            fau_ctl.load_controls(os.path.join(self.dir, "nope.json")), [])

    def test_a_corrupt_spec_yields_no_controls(self):
        path = self._write(fau_ctl.SPEC_FILENAME, "{not json")
        self.assertEqual(fau_ctl.load_controls(path), [])

    def test_no_controls_means_every_set_is_refused(self):
        tb = StubTopBlock()
        out = []
        disp = fau_ctl.Dispatcher(tb, [], "abc123", emit=out.append)
        disp.handle("SET gain 1.0")
        self.assertEqual(tb.history, [])
        self.assertIn("ERR gain", out[-1])

    def test_nonce_is_read_from_the_file(self):
        path = self._write(fau_ctl.NONCE_FILENAME, "deadbe\n")
        self.assertEqual(fau_ctl.load_nonce(path), "deadbe")

    def test_a_missing_nonce_falls_back_rather_than_failing(self):
        self.assertEqual(fau_ctl.load_nonce(os.path.join(self.dir, "no")),
                         fau_ctl.FALLBACK_NONCE)

    def test_a_nonce_with_whitespace_in_it_is_rejected(self):
        # It ends up inside a reply line; a space would split the reply and
        # leave an untagged fragment looking like flowgraph output.
        path = self._write(fau_ctl.NONCE_FILENAME, "dead be")
        self.assertEqual(fau_ctl.load_nonce(path), fau_ctl.FALLBACK_NONCE)

    def test_an_overlong_nonce_is_rejected(self):
        path = self._write(fau_ctl.NONCE_FILENAME, "a" * 64)
        self.assertEqual(fau_ctl.load_nonce(path), fau_ctl.FALLBACK_NONCE)


class TestStart(unittest.TestCase):
    """start() against a real pipe and the real reader thread."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fau_ctl_start_")
        self.spec_path = os.path.join(self.dir, fau_ctl.SPEC_FILENAME)
        with open(self.spec_path, "w") as fh:
            json.dump({"version": 1, "flowgraph": "x",
                       "controls": SPEC_CONTROLS}, fh)
        self.read_fd, self.write_fd = os.pipe()
        # Kept so tearDown can close it: the reader thread owns the
        # wrapper otherwise and CPython complains at collection time.
        self.stream = None
        self.captured = io.StringIO()
        self._stdout = sys.stdout
        sys.stdout = self.captured
        self._sigint = signal.getsignal(signal.SIGINT)
        self._sigterm = signal.getsignal(signal.SIGTERM)

    def tearDown(self):
        sys.stdout = self._stdout
        signal.signal(signal.SIGINT, self._sigint)
        signal.signal(signal.SIGTERM, self._sigterm)
        try:
            os.close(self.write_fd)
        except OSError:
            pass
        if self.stream is not None:
            try:
                self.stream.close()   # closes read_fd with it
            except OSError:
                pass
        else:
            try:
                os.close(self.read_fd)
            except OSError:
                pass

    def _open(self):
        self.stream = os.fdopen(self.read_fd)
        return self.stream

    def _start(self, tb, spec_path=None):
        return fau_ctl.start(tb, spec_path=spec_path or self.spec_path,
                             stream=self._open(), nonce="abc123")

    def _send(self, text):
        os.write(self.write_fd, text.encode("utf-8"))

    def _await(self, predicate):
        end = time.monotonic() + SETTLE
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_announces_ready_with_its_ids(self):
        tb = StubTopBlock()
        self._start(tb)
        line = self.captured.getvalue().strip()
        reply = controls.parse_reply(line, "abc123")
        self.assertEqual(reply.kind, "READY")
        self.assertIn("gain", reply.text)

    def test_applies_lines_arriving_on_the_stream(self):
        tb = StubTopBlock()
        self._start(tb)
        self._send("SET gain 0.25\n")
        self.assertTrue(self._await(lambda: tb.gain == 0.25))

    def test_survives_a_line_that_is_not_ours(self):
        tb = StubTopBlock()
        self._start(tb)
        self._send("some board noise\nSET gain 0.125\n")
        self.assertTrue(self._await(lambda: tb.gain == 0.125))

    def test_no_controls_means_no_reader_thread_and_no_output(self):
        empty = os.path.join(self.dir, "empty.json")
        with open(empty, "w") as fh:
            json.dump({"version": 1, "controls": []}, fh)
        tb = StubTopBlock()
        disp = self._start(tb, spec_path=empty)
        self.assertIsNone(disp.thread)
        self.assertEqual(self.captured.getvalue(), "")

    def test_reader_is_a_daemon_so_it_cannot_hold_up_teardown(self):
        disp = self._start(StubTopBlock())
        self.assertTrue(disp.thread.daemon)

    def test_sigint_is_reinstalled_as_stop_only(self):
        """The re-entrancy fix. grcc's handler is
        `tb.stop(); tb.wait(); sys.exit(0)`, and under `run_options: run`
        the main thread is ALREADY inside tb.wait() -- every call spawns a
        fresh _top_block_waiter thread, so the handler's second wait would
        run concurrently with the first during the DMA teardown.
        """
        tb = StubTopBlock()
        self._start(tb)
        handler = signal.getsignal(signal.SIGINT)
        self.assertTrue(callable(handler))
        handler(signal.SIGINT, None)
        self.assertEqual(tb.stopped, 1)
        self.assertEqual(tb.waited, 0)  # the point: no second wait

    def test_the_handler_does_not_exit_the_interpreter(self):
        # sys.exit() from inside a signal handler during teardown would
        # raise SystemExit in whatever the main thread was doing.
        tb = StubTopBlock()
        self._start(tb)
        try:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        except SystemExit:
            self.fail("the handler exited the interpreter")

    def test_sigterm_gets_the_same_treatment(self):
        tb = StubTopBlock()
        self._start(tb)
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        self.assertEqual(tb.stopped, 1)
        self.assertEqual(tb.waited, 0)

    def test_a_real_sigint_tears_down_once(self):
        """Not the handler called by hand -- an actual delivered signal,
        which is what Runner.terminate()'s 0x03 becomes on the board."""
        tb = StubTopBlock()
        self._start(tb)
        os.kill(os.getpid(), signal.SIGINT)
        self.assertTrue(self._await(lambda: tb.stopped == 1))
        self.assertEqual(tb.waited, 0)


class TestGeneratedSnippet(unittest.TestCase):
    """Drive fau_ctl through the snippet text grcc really emits.

    A unit test of fau_ctl cannot catch the snippet calling it wrongly, and
    that is the failure this project actually hit: the top block arrives in
    a snippet bound to `self`, not `tb`.
    """

    SNIPPET = ("def snipfcn_fau_ctl_snippet(self):\n"
               "    import fau_ctl\n"
               "    fau_ctl.start(self)\n")

    def test_the_transform_emits_a_body_that_binds_the_top_block(self):
        from ..core import headless
        # Whatever the transform injects has to work when grcc wraps it in
        # `def snipfcn_<name>(self):` -- so it must not name `tb`, which is
        # only the caller's local.
        body = headless.CTL_SNIPPET_CODE
        self.assertIn("self", body)
        self.assertNotIn("(tb)", body)

    def test_the_snippet_runs_and_starts_the_channel(self):
        namespace = {}
        exec(compile(self.SNIPPET, "<generated>", "exec"), namespace)
        directory = tempfile.mkdtemp(prefix="fau_ctl_snip_")
        with open(os.path.join(directory, fau_ctl.SPEC_FILENAME), "w") as fh:
            json.dump({"version": 1, "controls": SPEC_CONTROLS}, fh)
        with open(os.path.join(directory, fau_ctl.NONCE_FILENAME), "w") as fh:
            fh.write("abc123")

        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd)
        tb = StubTopBlock()
        captured = io.StringIO()
        old_stdout, old_int, old_term = (
            sys.stdout, signal.getsignal(signal.SIGINT),
            signal.getsignal(signal.SIGTERM))
        real_start = fau_ctl.start
        fau_ctl.start = lambda t: real_start(
            t, spec_path=os.path.join(directory, fau_ctl.SPEC_FILENAME),
            nonce=fau_ctl.load_nonce(
                os.path.join(directory, fau_ctl.NONCE_FILENAME)),
            stream=stream)
        sys.stdout = captured
        try:
            namespace["snipfcn_fau_ctl_snippet"](tb)
            os.write(write_fd, b"SET gain 0.875\n")
            end = time.monotonic() + SETTLE
            while time.monotonic() < end and tb.gain != 0.875:
                time.sleep(0.01)
        finally:
            fau_ctl.start = real_start
            sys.stdout = old_stdout
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
            try:
                os.close(write_fd)
            except OSError:
                pass
            try:
                stream.close()
            except OSError:
                pass

        self.assertEqual(tb.gain, 0.875)
        self.assertIn("READY", captured.getvalue())
        self.assertIn("OK gain", captured.getvalue())

    def test_a_body_naming_tb_would_have_raised(self):
        """Why the assertion above exists, pinned as a test."""
        bad = ("def snipfcn_x(self):\n"
               "    return tb\n")
        namespace = {}
        exec(compile(bad, "<generated>", "exec"), namespace)
        with self.assertRaises(NameError):
            namespace["snipfcn_x"](StubTopBlock())


if __name__ == "__main__":
    unittest.main()
