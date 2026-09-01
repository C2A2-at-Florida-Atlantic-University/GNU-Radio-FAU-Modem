#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Desktop-side wire protocol: frame construction, sentinel parsing, and the
random IDs used to scope a transfer.

Every constant here is re-exported from board/receiver.py, which is the
single source of truth (see that file's docstring) -- this module never
redefines a wire value, only builds/parses lines using the shared ones.

Unlike the board-side receiver, this module treats ALL matching as substring
search over noisy console output, never anchored: boot spew, MOTD text and
kernel printk share the wire with the protocol, and the only way to tolerate
that is to never assume a matched line is the *whole* line.
"""

import random
import re
import zlib

from ..board.receiver import (  # noqa: F401  (re-exported for callers)
    PROTO_VERSION,
    CHUNK_B64_DEFAULT,
    LINE_MAX,
    DEST_DEFAULT,
    IDLE_TIMEOUT_DEFAULT,
    SENT_READY,
    SENT_BEGIN,
    SENT_END,
    SENT_ABORT,
    SENT_OK,
    CHUNK_FMT,
    ACK_FMT,
    NAK_FMT,
    NAK_CRC,
    NAK_B64,
    NAK_LEN,
    NAK_RANGE,
    FAIL_BAD_HEADER,
    FAIL_MISSING_CHUNKS,
    FAIL_LEN_MISMATCH,
    FAIL_SHA_MISMATCH,
    FAIL_GUNZIP,
    FAIL_UNTAR,
    FAIL_WRITE,
    FAIL_ABORTED,
    FAIL_IDLE_TIMEOUT,
    format_fail,
)

# Strips both CSI sequences (\x1b[...<letter>) and OSC sequences
# (\x1b]...BEL), which is what a bash prompt with bracketed-paste mode and
# LS_COLORS actually emits around otherwise-plain lines.
RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")

# Loose (substring-search) regexes for the sentinels the BOARD emits. These
# deliberately have no ^/$ anchors -- a boot-spew or printk line can share
# the same terminal line as a protocol sentinel (kernel messages have no
# concept of "wait for a quiet moment"), so recognizing the sentinel
# anywhere in the line is what makes that survivable. ACK/NAK use \b word
# boundaries rather than full-line anchors for the same reason.
RE_READY = re.compile(r"===FAU-RECV-READY (\S+) (\S+) dest=(\S+)===")
RE_OK = re.compile(r"===FAU-OK (\S+) (\S+) (\d+) (\d+)===")
# detail is non-greedy so it stops at the first "===" it finds -- the
# closing one, in practice, since a detail string containing a literal
# "===" would be a very strange exception message.
RE_FAIL = re.compile(r"===FAU-FAIL (\S+) (\S+) (\S+)(?: (.*?))?===")
RE_ACK = re.compile(r"\bA (\d{5})\b")
RE_NAK = re.compile(r"\bN (\d{5}) (\S+)\b")

# Seen in the noise stream, these mean the board is not where we left it --
# a reboot mid-transfer, or a console that never made it past U-Boot. Worth
# recognizing as noise arrives rather than only timing out chunk-by-chunk.
REBOOT_MARKERS = ("login:", "Login:", "U-Boot", "zynq-uboot>",
                  "Hit any key to stop autoboot")


def strip_ansi(s):
    return RE_ANSI.sub("", s)


def new_xfer_id():
    """8 lowercase hex chars -- enough to make a stale frame from an aborted
    prior attempt vanishingly unlikely to collide with the current one."""
    return "%08x" % random.getrandbits(32)


def new_nonce():
    """6 lowercase hex chars, used to tag run-phase sentinels so flowgraph
    stdout can never collide with them (out of scope for the transport-only
    slice of this tool, kept here since it is the same primitive)."""
    return "%06x" % random.getrandbits(24)


def looks_like_reboot(line):
    return any(marker in line for marker in REBOOT_MARKERS)


def build_ready(dest, self_sha16):
    return SENT_READY % (PROTO_VERSION, self_sha16, dest)


def build_begin(xfer_id, nchunks, b64_len, sha_hex, gz_len):
    return SENT_BEGIN % (PROTO_VERSION, xfer_id, nchunks, b64_len, sha_hex, gz_len)


def build_chunk(seq, b64_slice):
    """One 'D <seq5> <crc32> <b64>' line. CRC is over the base64 TEXT, so it
    validates before the receiver ever attempts to decode -- a bit flip that
    still happens to decode as valid base64 is exactly the case the
    end-of-transfer sha256 alone would miss until the whole transfer had
    already been re-sent."""
    crc = zlib.crc32(b64_slice.encode("ascii")) & 0xFFFFFFFF
    return CHUNK_FMT % (seq, crc, b64_slice)


def build_end(xfer_id):
    return SENT_END % (PROTO_VERSION, xfer_id)


def build_abort(xfer_id):
    return SENT_ABORT % (PROTO_VERSION, xfer_id)


def split_b64(b64_text, chunk_size):
    """Split into chunk_size-character slices. chunk_size must be a multiple
    of 4 so every slice is independently valid base64 -- useful for manual
    diagnostics (each chunk can be decoded standalone) and is enforced by
    the CLI, not here, so this stays a pure function."""
    return [b64_text[i:i + chunk_size] for i in range(0, len(b64_text), chunk_size)]


def parse_line(line):
    """Classify one already ANSI/CR-stripped line of board output.

    Returns (kind, fields_dict) for recognized protocol traffic, or None to
    mean "not protocol traffic -- display it under [board] and move on".
    `kind` is one of: 'ready', 'ok', 'fail', 'ack', 'nak'.
    """
    m = RE_READY.search(line)
    if m:
        version, sha16, dest = m.groups()
        return "ready", {"version": version, "sha16": sha16, "dest": dest}

    m = RE_OK.search(line)
    if m:
        version, xfer_id, nfiles, gz_len = m.groups()
        return "ok", {"version": version, "xfer_id": xfer_id,
                      "nfiles": int(nfiles), "gz_len": int(gz_len)}

    m = RE_FAIL.search(line)
    if m:
        version, xfer_id, code, detail = m.groups()
        return "fail", {"version": version, "xfer_id": xfer_id,
                        "code": code, "detail": detail or ""}

    m = RE_ACK.search(line)
    if m:
        return "ack", {"seq": int(m.group(1))}

    m = RE_NAK.search(line)
    if m:
        return "nak", {"seq": int(m.group(1)), "reason": m.group(2)}

    return None
