#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Noticing that the flowgraph on disk has moved under us.

The deploy loop in practice is: edit in GRC, Ctrl-S, switch to the
deployer, deploy, watch the board. The step that goes wrong is the switch
-- nothing on the deployer's side says whether the `.py` it is about to
ship came from the `.grc` as it stands now or as it stood four saves ago,
and a stale deploy looks exactly like a successful one right up until the
board behaves like the old flowgraph. That is the bug this module exists to
make impossible to hit silently.

Polling `stat()`, not inotify: the interval that matters here is human
(a second is imperceptible next to a serial transfer measured in tens of
seconds), one `stat()` per tick is nothing, and it keeps the package
stdlib-only and identical on any filesystem -- inotify does not fire on
NFS, and GRC users do keep flowgraphs on network shares.

**Saves are not atomic from the watcher's point of view.** GRC writes the
file in place, so a poll can land mid-write and see a half-written YAML.
`Watcher` therefore reports a change only once the stamp has held still for
`settle`, which turns "the file changed" into "the file finished changing".
"""

import os
import time

# "no candidate change in flight". It cannot be None, because None is a
# perfectly real stamp -- it is what a deleted file reads as -- and using
# it as the sentinel made a deletion look like a settled non-change and
# then subtract from a _pending_since that had never been set.
_NOTHING = object()


def stamp(path):
    """(mtime_ns, size) for `path`, or None if it is not there.

    Same token core/generate.py records with a Generated, and deliberately
    the same shape: "has this changed since we generated" and "has this
    changed since we last looked" are the same question asked by two
    callers, and giving them two different notions of identity is how they
    end up disagreeing.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class Watcher:
    """Tracks one path, reporting a change once it has stopped moving.

    Not a thread. `poll()` is called from whatever loop the front-end
    already has -- Tk's `after()` in the GUI -- because a watcher thread
    would have to hand its findings back to that loop anyway, and the only
    thing it would add is a second place for the path to change.
    """

    def __init__(self, path=None, settle=0.4, clock=time.monotonic):
        self._clock = clock
        self.settle = settle
        self.path = None
        self.stamp = None
        self.changed = False       # sticky until acknowledge()
        self.changed_at = None
        self._pending = _NOTHING
        self._pending_since = None
        self.set_path(path)

    # -- configuration ------------------------------------------------
    def set_path(self, path):
        """Point the watcher at a new file, taking its current state as the
        baseline. Switching files is not itself a change: the operator
        selecting a different flowgraph is not the same event as the
        flowgraph they selected being edited."""
        self.path = path or None
        self.stamp = stamp(self.path) if self.path else None
        self.changed = False
        self.changed_at = None
        self._pending = _NOTHING
        self._pending_since = None

    def acknowledge(self):
        """Clear the sticky change flag -- the front-end has acted on it."""
        self.changed = False
        self.changed_at = None

    def rebaseline(self):
        """Take the file as it is now as the new baseline, and clear the
        flag. What a front-end calls after regenerating from the file: the
        version on disk and the version it holds now agree again."""
        self.stamp = stamp(self.path) if self.path else None
        self._pending = _NOTHING
        self._pending_since = None
        self.acknowledge()

    # -- the loop -----------------------------------------------------
    def poll(self):
        """One tick. Returns True on the tick a settled change is first seen.

        The return value is the edge; `self.changed` is the level, and stays
        set until acknowledge() or rebaseline(). A front-end that only wants
        to act once uses the edge; one that wants to keep showing "out of
        date" in a status bar uses the flag.
        """
        if not self.path:
            return False

        current = stamp(self.path)
        if current == self.stamp:
            # Back to the baseline (or never left it). A save that rewrote
            # identical bytes with the same mtime is indistinguishable from
            # no save, which is the correct answer anyway.
            self._pending = _NOTHING
            self._pending_since = None
            return False

        now = self._clock()
        if current != self._pending:
            # Still moving: restart the settle timer on the new state.
            self._pending = current
            self._pending_since = now
            return False

        if now - self._pending_since < self.settle:
            return False

        self.stamp = current
        self._pending = _NOTHING
        self._pending_since = None
        self.changed_at = now
        first = not self.changed
        self.changed = True
        return first

    # -- description --------------------------------------------------
    @property
    def exists(self):
        return self.stamp is not None

    def describe(self, now=None):
        """A short status string for a front-end to show verbatim."""
        if not self.path:
            return "not tracking"
        name = os.path.basename(self.path)
        if self.stamp is None:
            return "%s is missing" % name
        if not self.changed:
            return "tracking %s" % name
        age = (now if now is not None else self._clock()) - (self.changed_at or 0)
        if age < 1.0:
            return "%s changed just now" % name
        if age < 90:
            return "%s changed %ds ago" % (name, int(age))
        return "%s changed %dm ago" % (name, int(age // 60))
