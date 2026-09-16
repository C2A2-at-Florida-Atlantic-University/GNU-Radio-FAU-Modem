#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""The desktop-side send loop: BEGIN, chunks with selective-repeat
retransmit, END, then wait for OK/FAIL.

Assumes the receiver is already running and READY has already been seen --
that handshake is core/bootstrap.py's job, not this module's. This module
only owns the BEGIN..END/OK/FAIL exchange for one payload.
"""

import dataclasses
import time

from . import report
from .protocol import (
    build_begin,
    build_chunk,
    build_end,
    build_abort,
    parse_line,
    looks_like_reboot,
)

POLL_INTERVAL = 0.2  # how often the loop checks for input while a window is outstanding


class TransferError(RuntimeError):
    """The transfer did not complete. Message is meant to be shown as-is."""


@dataclasses.dataclass
class SendStats:
    chunks: int
    retransmits: int
    naks: int
    timeouts: int
    noise_lines: int
    bytes_on_wire: int
    elapsed: float


class Sender:
    def __init__(self, transport, reader, payload, dest, window=1,
                chunk_timeout=3.0, chunk_retries=5, finish_timeout=30.0,
                progress=True, on_progress=None, should_stop=None):
        self._transport = transport
        self._reader = reader
        self._payload = payload
        self._dest = dest
        self._window = max(1, window)
        self._chunk_timeout = chunk_timeout
        self._chunk_retries = chunk_retries
        self._finish_timeout = finish_timeout
        self._progress = progress
        self._on_progress = on_progress
        self._should_stop = should_stop
        self._aborted = False

    def _send_line(self, line):
        self._transport.write((line + "\n").encode("ascii"))

    def _report_progress(self, acked, total, stats):
        """Emit transfer progress to whichever front-ends asked for it: the
        CLI's \r-rewritten console line, and/or a structured callback.

        Two separate knobs rather than one, because they are not
        alternatives -- `progress` writes a control character to a real
        terminal and is meaningless in a GUI, while `on_progress` hands over
        numbers with no presentation attached. A caller wanting both (a GUI
        launched from a terminal) gets both.
        """
        if self._on_progress is not None:
            elapsed = time.monotonic() - stats["t0"]
            self._on_progress(acked, total, {
                "retransmits": stats["retransmits"],
                "naks": stats["naks"],
                "timeouts": stats["timeouts"],
                "bytes_on_wire": stats["bytes_on_wire"],
                "elapsed": elapsed,
                "rate": stats["bytes_on_wire"] / elapsed if elapsed > 0 else 0.0,
            })
        if not self._progress:
            return
        elapsed = time.monotonic() - stats["t0"]
        rate = stats["bytes_on_wire"] / elapsed if elapsed > 0 else 0.0
        pct = 100 * acked // total if total else 100
        print("\r[uart] %04d/%04d  %3d%%  %6.1f B/s  retx %d  naks %d"
              % (acked, total, pct, rate, stats["retransmits"], stats["naks"]),
              end="", flush=True)

    def abort(self):
        """Best-effort: tell the board to give up on this transfer. Used on
        an unrecoverable local error (retry budget exhausted, a detected
        reboot) so the receiver doesn't sit waiting on a transfer that will
        never complete."""
        self._aborted = True
        try:
            self._send_line(build_abort(self._payload.xfer_id))
        except Exception:
            pass

    def send(self):
        """Run BEGIN -> chunks -> END -> OK/FAIL. Raises TransferError on
        any unrecoverable failure; returns a SendStats on success."""
        payload = self._payload
        xfer_id = payload.xfer_id
        nchunks = len(payload.chunks)
        t0 = time.monotonic()
        stats = {"retransmits": 0, "naks": 0, "timeouts": 0,
                 "noise_lines": 0, "bytes_on_wire": 0, "t0": t0}

        header = build_begin(xfer_id, nchunks, len(payload.b64),
                             payload.sha256, payload.gz_bytes)
        self._send_line(header)
        stats["bytes_on_wire"] += len(header) + 1

        unacked = {}  # seq -> [line, sent_at, tries]
        acked = set()
        next_to_send = 0

        def send_chunk(seq):
            line = build_chunk(seq, payload.chunks[seq])
            self._send_line(line)
            stats["bytes_on_wire"] += len(line) + 1
            return line

        while len(acked) < nchunks:
            # Cancellation is checked here, at the top of the loop, rather
            # than only between chunks: this is the one place guaranteed to
            # be reached within POLL_INTERVAL no matter which branch below
            # ran last, so a GUI's Cancel takes effect in well under a
            # second even mid-retransmit. abort() first, so the receiver
            # stops waiting on a transfer that is never going to finish
            # instead of idling until --idle-timeout.
            if self._should_stop is not None and self._should_stop():
                self.abort()
                raise TransferError(
                    "transfer cancelled by the operator after %d/%d chunks "
                    "-- nothing on the board was overwritten (the receiver "
                    "only stages files once the whole payload validates)"
                    % (len(acked), nchunks))

            # Fill the window.
            while len(unacked) < self._window and next_to_send < nchunks:
                if next_to_send not in acked:
                    line = send_chunk(next_to_send)
                    unacked[next_to_send] = [line, time.monotonic(), 1]
                next_to_send += 1

            # Read whatever shows up for one short tick.
            self._reader.poll(POLL_INTERVAL)
            for raw_line in list(self._reader.lines(0.0)):
                parsed = parse_line(raw_line)
                if parsed is None:
                    if looks_like_reboot(raw_line):
                        self.abort()
                        raise TransferError(
                            "board rebooted mid-transfer (saw %r in the "
                            "console output) -- re-run once it's back up; "
                            "nothing on the board was overwritten" % raw_line)
                    stats["noise_lines"] += 1
                    report.say("board", raw_line)
                    continue
                kind, fields = parsed
                if kind == "ack":
                    seq = fields["seq"]
                    if seq in unacked:
                        del unacked[seq]
                    acked.add(seq)
                elif kind == "nak":
                    seq = fields["seq"]
                    stats["naks"] += 1
                    if seq in unacked:
                        entry = unacked[seq]
                        entry[1] = time.monotonic()
                        entry[2] += 1
                        stats["retransmits"] += 1
                        if entry[2] > self._chunk_retries:
                            self.abort()
                            raise TransferError(
                                "chunk %05d NAKed (%s) %d times, giving up"
                                % (seq, fields["reason"], entry[2] - 1))
                        send_chunk(seq)
                # 'ready'/'ok'/'fail' seen mid-transfer would be a protocol
                # confusion (a leftover reply from a previous transfer);
                # ignored here deliberately -- OK/FAIL are only meaningful
                # once END has been sent, below.

            # Resend anything that has timed out.
            now = time.monotonic()
            for seq, entry in list(unacked.items()):
                if now - entry[1] > self._chunk_timeout:
                    stats["timeouts"] += 1
                    entry[2] += 1
                    if entry[2] > self._chunk_retries:
                        self.abort()
                        raise TransferError(
                            "chunk %05d timed out %d times, giving up"
                            % (seq, entry[2] - 1))
                    stats["retransmits"] += 1
                    entry[0] = send_chunk(seq)
                    entry[1] = now

            self._report_progress(len(acked), nchunks, stats)

        if self._progress:
            print()  # newline after the final \r-rewritten progress line

        self._send_line(build_end(xfer_id))

        result = self._await_result(xfer_id)
        stats["elapsed"] = time.monotonic() - t0
        if result[0] == "fail":
            code, detail = result[1]["code"], result[1]["detail"]
            raise TransferError(
                "board reported %s%s" % (code, (": " + detail) if detail else ""))
        if result[0] is None:
            raise TransferError(
                "no OK/FAIL from the board within %.0fs of sending END -- "
                "the board may still be validating a large payload, or the "
                "console went quiet" % self._finish_timeout)

        return SendStats(
            chunks=nchunks,
            retransmits=stats["retransmits"],
            naks=stats["naks"],
            timeouts=stats["timeouts"],
            noise_lines=stats["noise_lines"],
            bytes_on_wire=stats["bytes_on_wire"],
            elapsed=stats["elapsed"],
        )

    def _await_result(self, xfer_id):
        """Wait for OK or FAIL matching this transfer's xfer_id, ignoring
        noise and replies for any other xfer_id (a stale reply still
        draining out of the tty buffer from an aborted earlier attempt)."""
        deadline = time.monotonic() + self._finish_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return (None, None)
            for raw_line in self._reader.lines(min(remaining, 1.0)):
                parsed = parse_line(raw_line)
                if parsed is None:
                    report.say("board", raw_line)
                    continue
                kind, fields = parsed
                if kind in ("ok", "fail") and fields.get("xfer_id") == xfer_id:
                    return (kind, fields)
                # anything else (a stray ack/nak, a reply for a different
                # xfer_id) is not what we are waiting for; keep waiting.
