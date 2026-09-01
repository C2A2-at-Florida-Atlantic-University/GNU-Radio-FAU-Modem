#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Board-side receiver for the FAU deployer's UART transport.

Reads a framed, chunked, CRC-checked transfer off stdin (fd 0), reassembles
it, validates it, and stages it into --dest. This file is the SINGLE SOURCE
OF TRUTH for the wire protocol constants: scripts/fau_deployer/core/protocol.py
on the desktop imports them from here (see board/__init__.py) rather than
keeping a second copy that could drift.

Deliberately standalone: no imports beyond the stdlib, and every board-
specific call (termios, fd 0) lives inside main()/Receiver so this file can
also be imported on the desktop (e.g. by tests, under Python 3.10) without
touching a real tty. Must stay valid Python 3.8+ -- the board runs 3.12, the
desktop that imports this for testing runs 3.10, and neither should need a
newer grammar than the other supports.

Wire protocol (see docs/plans/grc-deployer-plan.md for the full rationale):

    desktop -> board:
        ===FAU-BEGIN v1 <xfer_id> <nchunks> <b64_len> <sha256_hex> <gz_len>===
        D <seq5> <crc32_8hex> <b64_chunk>
        ===FAU-END v1 <xfer_id>===
        ===FAU-ABORT v1 <xfer_id>===

    board -> desktop:
        ===FAU-RECV-READY v1 <self_sha16> dest=<path>===
        A <seq5>
        N <seq5> <reason>              reason in {crc, b64, len, range}
        # <free-text log line, never parsed>
        ===FAU-OK v1 <xfer_id> <nfiles> <gz_len>===
        ===FAU-FAIL v1 <xfer_id> <code> [<detail>]===

FAIL codes: BAD_HEADER, MISSING_CHUNKS, LEN_MISMATCH, SHA_MISMATCH, GUNZIP,
UNTAR, WRITE, ABORTED, IDLE_TIMEOUT.

The desktop side treats all sentinel matching as substring search, never
anchored -- that is what lets boot spew, MOTD text and kernel printk share
the console with the protocol. This receiver returns the favor when parsing
what the desktop sends: BEGIN/END/ABORT are matched with `re.match` and no
end anchor, so trailing noise on the same line can't break framing. `D`
lines are the exception -- they are matched strictly, since anything that
isn't a well-formed chunk line is, from this side, indistinguishable from
console noise and is silently ignored either way.
"""

import argparse
import base64
import gzip
import hashlib
import io
import os
import re
import select
import shutil
import sys
import tarfile
import time
import zlib

# --- WIRE PROTOCOL: single source of truth, imported by core/protocol.py ---
PROTO_VERSION = "v1"
CHUNK_B64_DEFAULT = 512
LINE_MAX = 4096
DEST_DEFAULT = "/home/petalinux/flowgraphs"
IDLE_TIMEOUT_DEFAULT = 60.0

SENT_READY = "===FAU-RECV-READY %s %s dest=%s==="
SENT_BEGIN = "===FAU-BEGIN %s %s %d %d %s %d==="
SENT_END = "===FAU-END %s %s==="
SENT_ABORT = "===FAU-ABORT %s %s==="
SENT_OK = "===FAU-OK %s %s %d %d==="
CHUNK_FMT = "D %05d %08x %s"
ACK_FMT = "A %05d"
NAK_FMT = "N %05d %s"

#
# These four are used by THIS file to parse what the desktop sends, so they
# stay strict (anchored) -- a line that isn't a well-formed BEGIN/END/
# ABORT/D is, from the receiver's point of view, indistinguishable from
# console noise and is simply ignored (see _await_begin / _handle_transfer).
# The regexes for parsing what THIS file emits (READY/OK/FAIL/ACK/NAK) live
# on the desktop side in core/protocol.py instead, deliberately loose
# (substring search, no anchors), because the desktop has to pick protocol
# traffic out of boot spew and kernel printk sharing the same console.
#
RE_BEGIN = re.compile(
    r"===FAU-BEGIN (\S+) (\S+) (\d+) (\d+) ([0-9a-f]{64}) (\d+)===")
RE_END = re.compile(r"===FAU-END (\S+) (\S+)===")
RE_ABORT = re.compile(r"===FAU-ABORT (\S+) (\S+)===")
RE_CHUNK = re.compile(r"^D (\d{5}) ([0-9a-f]{8}) (\S+)$")

FAIL_BAD_HEADER = "BAD_HEADER"
FAIL_MISSING_CHUNKS = "MISSING_CHUNKS"
FAIL_LEN_MISMATCH = "LEN_MISMATCH"
FAIL_SHA_MISMATCH = "SHA_MISMATCH"
FAIL_GUNZIP = "GUNZIP"
FAIL_UNTAR = "UNTAR"
FAIL_WRITE = "WRITE"
FAIL_ABORTED = "ABORTED"
FAIL_IDLE_TIMEOUT = "IDLE_TIMEOUT"

NAK_CRC = "crc"
NAK_B64 = "b64"
NAK_LEN = "len"
NAK_RANGE = "range"

_B64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")
# --------------------------------------------------------------------------


def format_fail(xfer_id, code, detail=""):
    """===FAU-FAIL=== with an optional trailing detail field."""
    if detail:
        return "===FAU-FAIL %s %s %s %s===" % (PROTO_VERSION, xfer_id, code, detail)
    return "===FAU-FAIL %s %s %s===" % (PROTO_VERSION, xfer_id, code)


def self_sha16():
    """First 16 hex chars of sha256 over this file's own source.

    Computed from __file__ rather than a precomputed constant, so the value
    is always honest about what is actually running on the board -- the
    desktop compares this against its local copy in the READY line and
    re-bootstraps on any mismatch instead of trusting a stale receiver (see
    root CLAUDE.md: a hand-scp'd stale board copy cost three debugging
    rounds on an earlier bug; this is what makes that structurally
    impossible here).
    """
    try:
        with open(__file__, "rb") as f:
            data = f.read()
    except OSError:
        return "0" * 16
    return hashlib.sha256(data).hexdigest()[:16]


def _looks_like_b64(s):
    return bool(_B64_RE.match(s))


def _reject_unsafe_member(member):
    """The fallback path for Python < 3.12, which has no
    `tarfile.extractall(filter=...)`. Reproduces the part of the "data"
    filter that matters here: no absolute paths, no '..' traversal, no
    symlinks/hardlinks pointing outside the stage directory."""
    name = member.name
    if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
        raise ValueError("unsafe member path in tar: %r" % name)
    if member.issym() or member.islnk():
        raise ValueError("refusing to extract link member: %r" % name)


def _set_raw(fd):
    """Put fd into raw mode for the transfer: no echo, no line buffering, no
    signal generation from control characters, no XON/XOFF. Returns the
    saved termios attrs (or None on a non-tty fd, e.g. under test with a
    plain pipe) so the caller can restore them in a finally.

    termios is imported lazily so this module can be imported on the desktop
    (fd 0 there is not the board's console) without requiring a tty at
    import time.
    """
    try:
        import termios
    except ImportError:
        return None
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return None
    raw = termios.tcgetattr(fd)
    raw[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IXON)
    termios.tcsetattr(fd, termios.TCSANOW, raw)
    return saved


def _restore(fd, saved):
    if saved is None:
        return
    try:
        import termios
        termios.tcsetattr(fd, termios.TCSANOW, saved)
    except Exception:
        pass


class ShutdownRequested(Exception):
    """Raised out of _readline() when a raw 0x03 (Ctrl-C) byte is seen on
    the wire. In raw mode this receiver deliberately clears ISIG (see
    _set_raw), so Ctrl-C is never delivered as a real SIGINT -- it's just a
    data byte like any other. That means something has to decide what it
    means at the application level, and "stop this receiver" is the natural
    choice: it's the same key the run phase uses to stop a flowgraph, and a
    stale receiver otherwise has no way to be told a fresh deploy is about
    to replace it (see core/bootstrap.py, which sends this to retire a
    receiver whose self_sha16() no longer matches the desktop's copy).
    0x03 cannot appear in legitimate protocol traffic -- every sentinel,
    chunk and ACK/NAK line is printable ASCII -- so treating it specially
    can't misfire on real data.
    """


class Receiver:
    """Reads framed transfer requests off `fin`, writes protocol replies to
    `fout`, and stages validated payloads under `dest`.

    Uses os.read()/select() rather than sys.stdin: stdin's line buffering
    would delay bytes reaching the ACK/NAK logic, stalling the sender's flow
    control. Every reply line is written unbuffered via os.write() for the
    same reason.
    """

    def __init__(self, fin, fout, dest=DEST_DEFAULT,
                 idle_timeout=IDLE_TIMEOUT_DEFAULT, verbose=False):
        self.fin = fin
        self.fout = fout
        self.dest = dest
        self.idle_timeout = idle_timeout
        self.verbose = verbose
        self._buf = b""

    def _emit(self, line):
        os.write(self.fout, (line + "\n").encode("utf-8", "replace"))

    def _log(self, msg):
        if self.verbose:
            self._emit("# [recv] %s" % msg)

    def _readline(self, timeout):
        """Return one complete line (without the trailing newline/CR), or
        None on timeout or EOF. A line that grows past LINE_MAX without a
        newline is dropped with a warning -- the only place a garbage-
        spewing peer could cause unbounded memory growth here."""
        deadline = time.monotonic() + timeout
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1:
                line = self._buf[:nl]
                self._buf = self._buf[nl + 1:]
                return line.rstrip(b"\r").decode("utf-8", "replace")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            r, _w, _x = select.select([self.fin], [], [], min(remaining, 1.0))
            if self.fin not in r:
                continue
            chunk = os.read(self.fin, 65536)
            if not chunk:
                return None  # EOF
            if b"\x03" in chunk:
                raise ShutdownRequested()
            self._buf += chunk
            if len(self._buf) > LINE_MAX and b"\n" not in self._buf:
                self._log("dropped oversized line (%d bytes, no newline)"
                          % len(self._buf))
                self._buf = b""

    def serve(self):
        """Announce readiness, then serve transfers until idle-timeout or
        EOF. Returns a process exit code."""
        saved_termios = _set_raw(self.fin)
        try:
            os.makedirs(self.dest, exist_ok=True)
            self._emit(SENT_READY % (PROTO_VERSION, self_sha16(), self.dest))
            while True:
                try:
                    hdr = self._await_begin()
                    if hdr is None:
                        return 0  # idle timeout or EOF -- clean shutdown
                    self._handle_transfer(hdr)
                except ShutdownRequested:
                    self._log("0x03 received -- shutting down")
                    return 0
        finally:
            _restore(self.fin, saved_termios)

    def _await_begin(self):
        """Discard every line until a well-formed BEGIN header for this
        protocol version arrives. Returns a dict, or None on idle timeout /
        EOF."""
        while True:
            line = self._readline(self.idle_timeout)
            if line is None:
                return None
            m = RE_BEGIN.match(line)
            if not m:
                continue  # console noise, or a line this receiver doesn't care about
            version, xfer_id, nchunks, b64_len, sha_hex, gz_len = m.groups()
            if version != PROTO_VERSION:
                self._emit(format_fail(xfer_id, FAIL_BAD_HEADER,
                                      "version=%s" % version))
                continue
            return {
                "xfer_id": xfer_id,
                "nchunks": int(nchunks),
                "b64_len": int(b64_len),
                "sha_hex": sha_hex,
                "gz_len": int(gz_len),
            }

    def _handle_transfer(self, hdr):
        xfer_id = hdr["xfer_id"]
        nchunks = hdr["nchunks"]
        chunks = [None] * nchunks
        got = 0

        while got < nchunks:
            line = self._readline(self.idle_timeout)
            if line is None:
                self._emit(format_fail(xfer_id, FAIL_IDLE_TIMEOUT))
                return 4

            end = RE_END.match(line)
            if end and end.group(2) == xfer_id:
                self._emit(format_fail(xfer_id, FAIL_MISSING_CHUNKS,
                                      "%d/%d" % (got, nchunks)))
                return 4

            abort = RE_ABORT.match(line)
            if abort and abort.group(2) == xfer_id:
                self._emit(format_fail(xfer_id, FAIL_ABORTED))
                return 3

            m = RE_CHUNK.match(line)
            if not m:
                continue  # console noise, or a stray line from a stale/other xfer

            seq = int(m.group(1))
            crc_hex = m.group(2)
            b64 = m.group(3)

            if seq >= nchunks:
                self._emit(NAK_FMT % (seq, NAK_RANGE))
                continue
            if chunks[seq] is not None:
                # Duplicate: the sender is missing an ACK, not another copy
                # of the data. ACK it and drop the data -- NAKing a
                # duplicate would only trigger a retransmit storm.
                self._emit(ACK_FMT % seq)
                continue
            if len(b64) % 4 != 0 or not _looks_like_b64(b64):
                self._emit(NAK_FMT % (seq, NAK_B64))
                continue
            if (zlib.crc32(b64.encode("ascii")) & 0xFFFFFFFF) != int(crc_hex, 16):
                self._emit(NAK_FMT % (seq, NAK_CRC))
                continue

            chunks[seq] = b64
            got += 1
            self._emit(ACK_FMT % seq)

        # All chunks in hand -- drain until END, tolerating a retransmitted
        # ACK the sender never saw or trailing noise, up to idle_timeout.
        while True:
            line = self._readline(self.idle_timeout)
            if line is None:
                self._emit(format_fail(xfer_id, FAIL_IDLE_TIMEOUT))
                return 4
            end = RE_END.match(line)
            if end and end.group(2) == xfer_id:
                break
            abort = RE_ABORT.match(line)
            if abort and abort.group(2) == xfer_id:
                self._emit(format_fail(xfer_id, FAIL_ABORTED))
                return 3
            # anything else here is noise; keep waiting for END

        return self._finish(hdr, chunks)

    def _finish(self, hdr, chunks):
        xfer_id = hdr["xfer_id"]
        b64_all = "".join(chunks)

        if len(b64_all) != hdr["b64_len"]:
            self._emit(format_fail(xfer_id, FAIL_LEN_MISMATCH,
                                  "b64 got=%d want=%d" % (len(b64_all), hdr["b64_len"])))
            return 4

        try:
            gz = base64.b64decode(b64_all)
        except Exception as exc:
            self._emit(format_fail(xfer_id, FAIL_BAD_HEADER,
                                  "b64decode: %s" % exc))
            return 4

        if len(gz) != hdr["gz_len"]:
            self._emit(format_fail(xfer_id, FAIL_LEN_MISMATCH,
                                  "gz got=%d want=%d" % (len(gz), hdr["gz_len"])))
            return 4

        # Validate BEFORE decompressing: a corrupt payload must report
        # SHA_MISMATCH, not an unhelpful gzip CRC error.
        got_sha = hashlib.sha256(gz).hexdigest()
        if got_sha != hdr["sha_hex"]:
            self._emit(format_fail(xfer_id, FAIL_SHA_MISMATCH, "got=%s" % got_sha))
            return 4

        try:
            raw = gzip.decompress(gz)
        except Exception as exc:
            self._emit(format_fail(xfer_id, FAIL_GUNZIP, str(exc)))
            return 4

        stage = os.path.join(self.dest, ".fau_stage_%s" % xfer_id)
        nfiles = 0
        try:
            os.makedirs(stage, exist_ok=True)
            try:
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
                    try:
                        tf.extractall(stage, filter="data")
                    except TypeError:
                        # Python < 3.12: no `filter` kwarg. Apply the same
                        # path-traversal / link guard by hand first.
                        for member in tf.getmembers():
                            _reject_unsafe_member(member)
                        tf.extractall(stage)
            except Exception as exc:
                self._emit(format_fail(xfer_id, FAIL_UNTAR, str(exc)))
                return 4

            try:
                for root, _dirs, files in os.walk(stage):
                    for name in files:
                        src = os.path.join(root, name)
                        rel = os.path.relpath(src, stage)
                        dst = os.path.join(self.dest, rel)
                        dst_dir = os.path.dirname(dst)
                        if dst_dir:
                            os.makedirs(dst_dir, exist_ok=True)
                        os.replace(src, dst)  # atomic per file
                        nfiles += 1
            except OSError as exc:
                self._emit(format_fail(xfer_id, FAIL_WRITE, str(exc)))
                return 4
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        self._log("staged %d files, %d bytes" % (nfiles, hdr["gz_len"]))
        self._emit(SENT_OK % (PROTO_VERSION, xfer_id, nfiles, hdr["gz_len"]))
        return 0


def _build_parser():
    p = argparse.ArgumentParser(
        description="Board-side receiver for the FAU deployer's UART transport.")
    p.add_argument("--dest", default=DEST_DEFAULT, metavar="DIR",
                   help="directory validated payloads are staged into "
                        "(default: %(default)s)")
    p.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT_DEFAULT,
                   metavar="SEC",
                   help="give up and exit if no protocol traffic arrives for "
                        "this long, in seconds (default: %(default)g). "
                        "Protects against a desktop that died mid-transfer "
                        "stranding this receiver on the console forever")
    p.add_argument("--verbose", action="store_true",
                   help="emit '# [recv] ...' log lines (never parsed by the "
                        "desktop side, always safe to ignore)")
    return p


def main():
    parser = _build_parser()
    args = parser.parse_args()
    if args.idle_timeout <= 0:
        parser.error("--idle-timeout must be > 0")
    recv = Receiver(fin=0, fout=1, dest=args.dest,
                    idle_timeout=args.idle_timeout, verbose=args.verbose)
    return recv.serve()


if __name__ == "__main__":
    sys.exit(main())
