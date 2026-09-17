#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""core/watch.py: noticing a flowgraph moved under us.

Driven by a fake clock rather than by sleeping. The settle window is the
whole point of the module -- GRC writes in place, so a poll can land
mid-write -- and a test that proves it by waiting 0.4 s proves it slowly
and flakily; one that steps a clock proves it exactly.
"""

import os
import shutil
import tempfile
import unittest

from ..core import watch


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class WatchTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fau_watch_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "fg.grc")
        self.mtime_ns = 1_700_000_000_000_000_000
        self._write("one")
        self.clock = FakeClock()
        self.w = watch.Watcher(self.path, settle=0.4, clock=self.clock)

    def _write(self, text):
        with open(self.path, "w") as fh:
            fh.write(text)
        # Stamp a monotonically increasing mtime rather than trusting the
        # clock. Two writes inside one filesystem timestamp tick really are
        # indistinguishable -- which is correct behaviour, and exactly what
        # would otherwise make this suite flaky -- so the test supplies the
        # distinctness it means to be testing with.
        self.mtime_ns += 10_000_000
        os.utime(self.path, ns=(self.mtime_ns, self.mtime_ns))

    def _settle(self):
        """Poll, let the settle window pass, poll again."""
        self.w.poll()
        self.clock.advance(1.0)
        return self.w.poll()


class TestQuiescence(WatchTestCase):
    def test_an_untouched_file_never_reports_a_change(self):
        for _ in range(5):
            self.assertFalse(self.w.poll())
            self.clock.advance(1.0)
        self.assertFalse(self.w.changed)

    def test_watching_nothing_is_not_an_error(self):
        w = watch.Watcher(None, clock=self.clock)
        self.assertFalse(w.poll())
        self.assertEqual(w.describe(), "not tracking")


class TestChange(WatchTestCase):
    def test_a_settled_write_reports_once_on_its_edge(self):
        self._write("two")
        self.assertTrue(self._settle())
        self.clock.advance(1.0)
        # Still changed (the level), but not a new edge.
        self.assertFalse(self.w.poll())
        self.assertTrue(self.w.changed)

    def test_a_write_is_not_reported_until_it_settles(self):
        self._write("two")
        self.assertFalse(self.w.poll())          # first sighting
        self.clock.advance(0.1)
        self.assertFalse(self.w.poll())          # too soon
        self.clock.advance(0.5)
        self.assertTrue(self.w.poll())

    def test_a_file_still_being_written_keeps_restarting_the_timer(self):
        # A long save seen in progress must not be reported half-written:
        # the flowgraph would fail to parse and the operator would see a
        # YAML error for a file that is fine.
        for chunk in ("a", "ab", "abc"):
            self._write(chunk)
            self.w.poll()
            self.clock.advance(0.2)
            self.assertFalse(self.w.poll())
        self.clock.advance(1.0)
        self.assertTrue(self.w.poll())

    def test_a_write_that_reverts_to_the_baseline_is_not_a_change(self):
        # A save that puts the file back exactly as the watcher last saw
        # it is indistinguishable from no save, and that is the right
        # answer: there is nothing to regenerate.
        baseline = watch.stamp(self.path)
        self._write("two")
        self.w.poll()
        with open(self.path, "w") as fh:
            fh.write("one")
        os.utime(self.path, ns=(baseline[0], baseline[0]))
        self.clock.advance(1.0)
        self.assertFalse(self.w.poll())
        self.assertFalse(self.w.changed)


class TestAcknowledge(WatchTestCase):
    def test_acknowledge_clears_the_level_and_a_later_edit_fires_again(self):
        self._write("two")
        self.assertTrue(self._settle())
        self.w.acknowledge()
        self.assertFalse(self.w.changed)
        self._write("three")
        self.assertTrue(self._settle())

    def test_rebaseline_after_regenerating_leaves_it_quiet(self):
        self._write("two")
        self.w.rebaseline()
        self.clock.advance(1.0)
        self.assertFalse(self.w.poll())
        self.assertFalse(self.w.changed)


class TestSetPath(WatchTestCase):
    def test_switching_files_is_not_itself_a_change(self):
        # Selecting a different flowgraph is not the same event as the one
        # you selected being edited, and conflating them would auto-process
        # on every keystroke in the path field.
        other = os.path.join(self.dir, "other.grc")
        with open(other, "w") as fh:
            fh.write("x")
        self.w.set_path(other)
        self.assertFalse(self.w.changed)
        self.clock.advance(1.0)
        self.assertFalse(self.w.poll())

    def test_switching_clears_a_pending_change(self):
        self._write("two")
        self.assertTrue(self._settle())
        self.w.set_path(self.path)
        self.assertFalse(self.w.changed)


class TestMissing(WatchTestCase):
    def test_a_deleted_file_reports_as_a_change_then_says_it_is_missing(self):
        os.unlink(self.path)
        self.assertTrue(self._settle())
        self.assertFalse(self.w.exists)
        self.assertIn("missing", self.w.describe())

    def test_a_file_that_reappears_is_another_change(self):
        os.unlink(self.path)
        self._settle()
        self.w.acknowledge()
        self._write("back")
        self.assertTrue(self._settle())


class TestDescribe(WatchTestCase):
    def test_quiet_describes_what_it_is_tracking(self):
        self.assertEqual(self.w.describe(), "tracking fg.grc")

    def test_a_change_is_described_with_its_age(self):
        self._write("two")
        self._settle()
        self.clock.advance(30.0)
        self.assertIn("30s ago", self.w.describe())
        self.clock.advance(300.0)
        self.assertIn("m ago", self.w.describe())


if __name__ == "__main__":
    unittest.main()
