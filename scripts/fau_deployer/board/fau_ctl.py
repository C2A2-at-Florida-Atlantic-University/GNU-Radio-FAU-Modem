#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Board-side control channel: read SET/PULSE lines, call tb.set_<id>().

This runs ON THE BOARD, inside the generated flowgraph's own process. It is
started by two lines the headless transform injects as a GRC Snippet at
`main_after_start`:

    import fau_ctl
    fau_ctl.start(tb)

and it ships **in the payload**, as an ordinary sibling module of the
generated `.py` -- no RPM, no PetaLinux rebuild, no image change. That is
deliberate: in a project where a stale hand-copied file on the board has
already cost several rounds of debugging, the board-side half should be
iterable as fast as Deploy can be pressed.

Why a Snippet and not an Embedded Python Block: an `epy_block` is a
`gr.basic_block` instance with no reference to the top block and no
supported way to get one, so it can never call `tb.set_<id>()`. The
dispatcher has to live where `tb` is in scope, and `main_after_start` is
handed `tb` by grcc's own template (`flow_graph.py.mako:411`).

Stdlib only, and only modules a headless PetaLinux image certainly has:
ast, json, os, signal, sys, threading, time.

Two things here are load-bearing for safety:

- **`ast.literal_eval`, never `eval`.** Everything arriving on this socket
  is a line off a serial console, which is shared, lossy and echoes
  everything. A corrupted line must be able to fail, not to execute.
- **Ids are whitelisted from `ui_spec.json`**, so no line -- corrupted,
  spoofed by flowgraph output, or simply wrong -- can name an arbitrary
  attribute of the top block. Combined with the FAU blocks having no
  `callbacks:` at all (their parameters are frozen at construction), the
  worst a control can reach is a stock GNU Radio setter. `set_samp_rate`
  was the only control-reachable setter that touches the DMA, and it is
  not reachable any more: this channel is structurally incapable of
  re-arming, halting or reallocating anything on the DMA path.

Replies are tagged with the run nonce the deployer already uses for its
`FAU-RC` completion marker, so flowgraph stdout cannot forge one and the
deployer can route them out of the log pane:

    FAU-CTL-<nonce> OK  <id> <repr>
    FAU-CTL-<nonce> ERR <id> <reason>

There is no ACK/retransmit machinery, unlike the payload transfer. A lost
control line is not worth recovering: the operator sees no OK and moves the
slider again, and the next value is more useful than the one that got lost.
"""

import ast
import json
import os
import signal
import sys
import threading
import time

PROTO = "FAU-CTL"
SPEC_FILENAME = "ui_spec.json"

# Where the run's nonce is read from. The deployer writes it next to the
# flowgraph with a shell redirect immediately before launching, because
# there is no reliable way to hand an environment variable through `sudo`:
# sudo scrubs the environment, and both `sudo VAR=v cmd` and
# `--preserve-env=VAR` need a sudoers privilege (SETENV/env_keep) this tool
# cannot assume. A file in a directory the deploy just wrote to needs no
# privilege at all.
NONCE_FILENAME = ".fau_ctl_nonce"
FALLBACK_NONCE = "000000"

# Longest line this will even look at. A SET line is ~30 bytes; anything
# approaching this is console noise or a runaway, and reading it into
# memory serves nobody.
MAX_LINE = 4096

# Ceiling on PULSE's hold time. A pulse blocks the reader thread for its
# duration (deliberately -- see _pulse), so an absurd value would make the
# channel look wedged.
MAX_PULSE_MS = 10000


def _repr(value):
    """A canonical Python literal for `value`, for the reply line.

    repr() on the types the spec allows is always literal_eval-able, which
    is what lets the desktop parse a reply back into the value it sent and
    compare them.
    """
    return repr(value)


class Dispatcher:
    """Parses control lines and applies them to a top block.

    Separated from the reader thread so it can be tested without a serial
    console, a flowgraph or a board: hand it any object with `set_<id>`
    methods and call `handle()` with a line.
    """

    def __init__(self, tb, controls, nonce, emit=None):
        self._tb = tb
        self._controls = {c["id"]: c for c in controls}
        self._nonce = nonce
        self._emit = emit or self._print
        self._lock = threading.Lock()

    @property
    def ids(self):
        return sorted(self._controls)

    def _print(self, text):
        # The one place this module writes to stdout. Everything else it
        # might want to say goes into an ERR reply, because an untagged
        # line would land in the deployer's log looking like flowgraph
        # output.
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def ok(self, cid, value):
        self._emit("%s-%s OK %s %s" % (PROTO, self._nonce, cid, _repr(value)))

    def err(self, cid, reason):
        # Newlines would split one reply into two lines, the second of
        # which is untagged and reads as flowgraph output.
        reason = " ".join(str(reason).split())
        self._emit("%s-%s ERR %s %s" % (PROTO, self._nonce, cid, reason))

    def handle(self, line):
        """Apply one control line. Never raises.

        Returns True if the line was one of ours (and so must not be
        treated as anything else), False if it was not addressed to us at
        all -- console noise, the shell's echo, a line of flowgraph output
        that happens to arrive on stdin.
        """
        try:
            return self._handle(line)
        except Exception as exc:  # noqa: BLE001 -- see the module docstring
            # A control must not be able to take the flowgraph down. This
            # is the backstop for a bug in the parsing below; the setter
            # itself is caught separately and more precisely in _apply.
            try:
                self.err("?", "internal error handling a control line: %r"
                              % (exc,))
            except Exception:
                pass
            return True

    def _handle(self, line):
        text = line.strip()
        if not text:
            return False
        parts = text.split(None, 2)
        verb = parts[0].upper()
        if verb not in ("SET", "PULSE"):
            return False
        if len(parts) < 3:
            self.err("?", "malformed %s line (expected `%s <id> <value>`)"
                          % (verb, verb))
            return True

        cid, rest = parts[1], parts[2].strip()
        spec = self._controls.get(cid)
        if spec is None:
            # The whitelist. Deliberately says what IS settable: the
            # realistic cause is a spec that no longer matches the
            # flowgraph running on the board, and the id list is what
            # makes that obvious.
            self.err(cid, "not a control of this flowgraph; settable ids "
                          "are: %s" % (", ".join(self.ids) or "(none)"))
            return True

        if verb == "SET":
            self._set(spec, rest)
        else:
            self._pulse(spec, rest)
        return True

    def _parse_value(self, spec, text):
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError, MemoryError,
                RecursionError):
            raise ValueError("%r is not a Python literal" % text)

        options = spec.get("options")
        if options is not None and value not in options:
            raise ValueError("%r is not one of this chooser's options (%s)"
                             % (value, ", ".join(_repr(o) for o in options)))
        return value

    def _set(self, spec, text):
        cid = spec["id"]
        try:
            value = self._parse_value(spec, text)
        except ValueError as exc:
            self.err(cid, str(exc))
            return
        self._apply(cid, value)

    def _apply(self, cid, value):
        setter = getattr(self._tb, "set_" + cid, None)
        if setter is None or not callable(setter):
            self.err(cid, "the running flowgraph has no set_%s(); the "
                          "control spec does not match what is deployed"
                     % cid)
            return
        try:
            # Serialised against other control lines. grcc's setters are
            # not written to be re-entrant, and two arriving together
            # would otherwise interleave a read-modify-write.
            with self._lock:
                setter(value)
        except Exception as exc:  # noqa: BLE001
            # A stock GNU Radio setter raising is an ordinary outcome of a
            # value a block does not like. Report it against the widget
            # and keep the flowgraph running.
            self.err(cid, "set_%s(%s) raised %s: %s"
                     % (cid, _repr(value), type(exc).__name__, exc))
            return
        self.ok(cid, value)

    def _pulse(self, spec, text):
        """A momentary press: pressed -> hold -> released, timed here.

        The hold happens on the board rather than as two lines from the
        desktop because the console's latency is not something the
        operator controls: a 50 ms press could arrive as anything from 50
        to several hundred ms, which matters for a button wired to a reset
        or a command burst. Blocking the reader thread for the duration is
        the point -- it is what stops a second control line landing in the
        middle of the pulse.
        """
        cid = spec["id"]
        if "pressed" not in spec or "released" not in spec:
            self.err(cid, "PULSE needs pressed/released values and this "
                          "control has none; use SET")
            return
        try:
            ms = float(text)
        except ValueError:
            self.err(cid, "%r is not a number of milliseconds" % text)
            return
        if not (0 <= ms <= MAX_PULSE_MS):
            self.err(cid, "pulse width %g ms is outside 0..%d"
                     % (ms, MAX_PULSE_MS))
            return

        setter = getattr(self._tb, "set_" + cid, None)
        if setter is None or not callable(setter):
            self.err(cid, "the running flowgraph has no set_%s()" % cid)
            return

        try:
            with self._lock:
                setter(spec["pressed"])
                time.sleep(ms / 1000.0)
                setter(spec["released"])
        except Exception as exc:  # noqa: BLE001
            self.err(cid, "pulsing raised %s: %s" % (type(exc).__name__, exc))
            return
        self.ok(cid, spec["released"])


def load_controls(path=None):
    """The control list out of `ui_spec.json`, or [] if there is none.

    Looks beside this module, which is where the payload puts both it and
    the generated flowgraph. An absent or unreadable spec is not fatal:
    the flowgraph runs perfectly well without a control channel, and
    killing a deployed run over a missing sidecar would be the wrong
    trade. With no controls the whitelist is empty, so every SET is
    refused -- which is the safe direction.
    """
    if path is None:
        path = _here(SPEC_FILENAME)
    try:
        with open(path, "r") as fh:
            spec = json.load(fh)
    except (OSError, ValueError):
        return []
    controls = spec.get("controls") if isinstance(spec, dict) else None
    if not isinstance(controls, list):
        return []
    return [c for c in controls if isinstance(c, dict) and "id" in c]


def _here(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def load_nonce(path=None):
    """The run nonce, or FALLBACK_NONCE if there is none to read.

    Falling back rather than failing is deliberate: an untagged-but-wrong
    nonce costs the deployer its ability to route replies out of the log
    pane, which is cosmetic, whereas refusing to start the control channel
    over a missing sidecar would take a working feature away from a run
    that is otherwise fine.
    """
    if path is None:
        path = _here(NONCE_FILENAME)
    try:
        with open(path, "r") as fh:
            got = fh.read().strip()
    except OSError:
        return FALLBACK_NONCE
    # Whatever is in the file ends up inside a reply line, so it has to be
    # one token of plain text -- a stray newline or space would split the
    # reply and leave an untagged fragment looking like flowgraph output.
    if got and got.isalnum() and len(got) <= 32:
        return got
    return FALLBACK_NONCE


def _reader(dispatcher, stream, stop):
    while not stop.is_set():
        try:
            line = stream.readline(MAX_LINE)
        except (OSError, ValueError):
            return
        if not line:
            return  # EOF: the console went away
        dispatcher.handle(line)


def start(tb, nonce=None, spec_path=None, stream=None):
    """Start the control channel for the running top block `tb`.

    Called from the injected Snippet at `main_after_start`. Returns the
    Dispatcher (for tests); the reader runs on a daemon thread so it can
    never hold up interpreter exit during a teardown.

    **This also re-installs SIGINT**, and that is not incidental. grcc's
    generated handler is `tb.stop(); tb.wait(); sys.exit(0)`, which is
    fine under `run_options: prompt` where the main thread is parked in
    input(). Under `run_options: run` -- which the transform forces, so
    that stdin is free for this channel -- the main thread is already
    inside `tb.wait()`, and every call to `top_block.wait()` spawns a
    fresh `_top_block_waiter` thread (gnuradio/gr/top_block.py). The
    handler's second `tb.wait()` would therefore start a second concurrent
    `top_block_wait_unlocked` on the same flowgraph, during exactly the
    DMA teardown the deployer exists to protect.

    So: handler becomes `tb.stop()` and nothing else. The teardown runs in
    the blocks' `stop()` overrides, which `top_block.stop()` calls; the
    `tb.wait()` already in progress on the main thread then observes
    completion and returns, and main() falls through its normal path --
    including `snippets_main_after_stop`. One wait, no re-entrancy, and no
    sys.exit() from inside a signal handler.

    Ordering works because the template emits `snippets_main_after_start`
    *after* its own `signal.signal(...)` calls, so this installs last.
    """
    nonce = nonce or load_nonce()
    controls = load_controls(spec_path)
    dispatcher = Dispatcher(tb, controls, nonce)

    def on_sigint(signum, frame):
        tb.stop()

    try:
        signal.signal(signal.SIGINT, on_sigint)
        signal.signal(signal.SIGTERM, on_sigint)
    except (ValueError, OSError):
        # Not the main thread, or a platform without them. The stock
        # handler stays in place, which still tears down correctly -- it
        # just does the redundant second wait.
        pass

    stop = threading.Event()
    dispatcher.stop_event = stop

    if controls:
        thread = threading.Thread(
            target=_reader, args=(dispatcher, stream or sys.stdin, stop),
            name="fau_ctl", daemon=True)
        thread.start()
        dispatcher.thread = thread
        dispatcher._emit("%s-%s READY %s"
                         % (PROTO, nonce, " ".join(dispatcher.ids)))
    else:
        dispatcher.thread = None

    return dispatcher
