#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""fau-deploy: ship a generated GNU Radio flowgraph (and the local sibling
modules it imports) to a Zynq board over its serial console, and confirm it
landed intact.

Scope: transport only. This does NOT run the flowgraph on the board, parse
or transform a .grc file, or offer a GUI -- see docs/plans/grc-deployer-plan.md
for the full design and what's deliberately deferred past this pass.

Needs root: no. The board-side login user is a regular (non-root) account;
nothing here touches /dev/mem or the DMA window itself.
"""

import argparse
import os
import sys

from .core import bootstrap, payload, report
from .core.bootstrap import BootstrapError
from .core.payload import PayloadError
from .core.protocol import (
    CHUNK_B64_DEFAULT,
    DEST_DEFAULT,
    IDLE_TIMEOUT_DEFAULT,
)
from .core.sender import Sender, TransferError
from .core.session import BoardSession, LoginError
from .core.transport import LineReader, SerialTransport, Transcript, TransportError

_HERE = os.path.dirname(os.path.abspath(__file__))
RECEIVER_PATH = os.path.join(_HERE, "board", "receiver.py")
REMOTE_DIR_DEFAULT = bootstrap.REMOTE_DIR_DEFAULT

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_SESSION = 3
EXIT_TRANSFER = 4
EXIT_ABORT = 130


def _build_parser():
    p = argparse.ArgumentParser(
        prog="fau-deploy",
        description="Ship a generated flowgraph to a Zynq board over its "
                    "serial console.")

    p.add_argument("--flowgraph", required=True, metavar="PATH",
                   help="the generated .py to deploy; its local sibling "
                        "imports (e.g. a shared helper module next to it) "
                        "are discovered automatically -- see --extra to add "
                        "any this misses")
    p.add_argument("--extra", action="append", default=[], metavar="PATH",
                   help="an additional file to ship alongside --flowgraph; "
                        "repeatable")

    p.add_argument("--port", required=True, metavar="DEV",
                   help="serial device, e.g. /dev/ttyUSB0 (no auto-detect "
                        "in this version -- must be given explicitly)")
    p.add_argument("--baud", type=int, default=115200, metavar="BAUD",
                   help="serial baud rate (default: %(default)d)")

    p.add_argument("--user", default="petalinux", metavar="USER",
                   help="board login user (default: %(default)s)")
    p.add_argument("--password", default="1234", metavar="PASS",
                   help="board login password (default: %(default)s)")

    p.add_argument("--dest", default=DEST_DEFAULT, metavar="DIR",
                   help="board-side directory the flowgraph is written into "
                        "(default: %(default)s)")
    p.add_argument("--remote-dir", default=REMOTE_DIR_DEFAULT, metavar="DIR",
                   help="board-side directory the receiver tooling itself "
                        "lives in, kept separate from --dest so a future "
                        "'clean the deployed flowgraphs' can't delete it "
                        "(default: %(default)s)")

    p.add_argument("--chunk-size", type=int, default=CHUNK_B64_DEFAULT,
                   metavar="CHARS",
                   help="base64 characters per transfer chunk; must be a "
                        "positive multiple of 4 so every chunk decodes "
                        "standalone for diagnostics (default: %(default)d)")
    p.add_argument("--window", type=int, default=1, metavar="N",
                   help="chunks kept in flight at once before waiting for "
                        "an ACK; 1 is strict stop-and-wait (default: "
                        "%(default)d)")
    p.add_argument("--chunk-timeout", type=float, default=3.0, metavar="SEC",
                   help="seconds to wait for an ACK/NAK before resending a "
                        "chunk (default: %(default)g)")
    p.add_argument("--chunk-retries", type=int, default=5, metavar="N",
                   help="resends allowed per chunk before giving up on the "
                        "whole transfer (default: %(default)d)")
    p.add_argument("--finish-timeout", type=float, default=30.0, metavar="SEC",
                   help="seconds to wait for OK/FAIL after the last chunk "
                        "is sent -- covers the board hashing, "
                        "decompressing and staging a large payload "
                        "(default: %(default)g)")

    p.add_argument("--login-timeout", type=float, default=30.0, metavar="SEC",
                   help="seconds to wait for each step of the login "
                        "exchange (default: %(default)g)")
    p.add_argument("--handshake-timeout", type=float, default=15.0,
                   metavar="SEC",
                   help="seconds to wait for the receiver to announce "
                        "readiness after it's launched (default: %(default)g)")
    p.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT_DEFAULT,
                   metavar="SEC",
                   help="board-side receiver idle timeout -- how long it "
                        "waits with no protocol traffic before giving up "
                        "and returning the console (default: %(default)g)")

    p.add_argument("--max-bytes", type=int, default=payload.MAX_BYTES_DEFAULT,
                   metavar="N",
                   help="refuse to build a payload larger than this many "
                        "bytes (default: %(default)d)")
    p.add_argument("--eta-warn", type=float, default=60.0, metavar="SEC",
                   help="require --yes if the estimated transfer time "
                        "exceeds this many seconds, so a huge payload is "
                        "caught before it's 90 minutes in (default: "
                        "%(default)g)")
    p.add_argument("--yes", action="store_true",
                   help="proceed even if the --eta-warn threshold is "
                        "exceeded")

    p.add_argument("--force-bootstrap", action="store_true",
                   help="push the receiver unconditionally, skipping the "
                        "already-current check")
    p.add_argument("--transcript", metavar="PATH",
                   help="write a timestamped byte log of both directions "
                        "of the session to PATH, for debugging a lossy "
                        "console after the fact")
    p.add_argument("--no-progress", action="store_true",
                   help="don't print the rewriting transfer-progress line "
                        "(useful when output is piped or logged)")
    p.add_argument("--verbose", action="store_true",
                   help="print extra [session]/[bootstrap] diagnostic lines")
    p.add_argument("--dry-run", action="store_true",
                   help="build the payload and print the manifest/ETA, but "
                        "never open the serial port")

    return p


def _validate(parser, args):
    if args.chunk_size <= 0 or args.chunk_size % 4 != 0:
        parser.error("--chunk-size must be a positive multiple of 4 (got %r)"
                    % args.chunk_size)
    if args.window < 1:
        parser.error("--window must be >= 1")
    if args.baud <= 0:
        parser.error("--baud must be > 0")
    if args.max_bytes <= 0:
        parser.error("--max-bytes must be > 0")


def _prepare_payload(args):
    """Collect + build the payload, print the manifest/ETA banner. Returns
    the Payload, or exits with EXIT_USAGE on a PayloadError."""
    try:
        entries, main_arc = payload.collect_files(
            args.flowgraph, extra=args.extra, max_bytes=args.max_bytes)
    except PayloadError as exc:
        report.die(str(exc), code=EXIT_USAGE)

    pl = payload.build_payload(entries, main_arc, chunk_size=args.chunk_size)

    report.banner("PAYLOAD")
    for line in payload.format_manifest(pl):
        print("  %s" % line)
    report.kv("total files", len(pl.entries))
    report.kv("raw bytes", pl.raw_bytes)
    report.kv("compressed bytes", pl.gz_bytes)
    report.kv("chunks", len(pl.chunks))
    report.kv("sha256", pl.sha256)

    eta = payload.eta_seconds(pl, args.baud, args.window)
    report.kv("estimated transfer time", "%.1fs" % eta)
    if eta > args.eta_warn and not args.yes:
        report.die(
            "estimated transfer time %.0fs exceeds --eta-warn %.0fs -- pass "
            "--yes to proceed anyway, or raise --eta-warn" % (eta, args.eta_warn),
            code=EXIT_USAGE)

    return pl


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate(parser, args)

    pl = _prepare_payload(args)
    if args.dry_run:
        report.say("cli", "--dry-run: not opening the port")
        return EXIT_OK

    transcript = Transcript(args.transcript) if args.transcript else None
    try:
        transport = SerialTransport(args.port, args.baud, transcript=transcript)
    except TransportError as exc:
        report.die(str(exc), code=EXIT_USAGE)

    reader = LineReader(transport)
    session = None
    try:
        report.banner("BOARD")
        session = BoardSession(transport, reader, user=args.user,
                              password=args.password,
                              login_timeout=args.login_timeout,
                              verbose=args.verbose)
        session.connect()
        report.say("cli", "logged in as %s" % args.user)

        bootstrap.ensure_receiver(
            session, transport, reader, RECEIVER_PATH,
            remote_dir=args.remote_dir, dest=args.dest,
            idle_timeout=args.idle_timeout, chunk_size=args.chunk_size,
            handshake_timeout=args.handshake_timeout,
            force=args.force_bootstrap)

        report.banner("TRANSFER")
        sender = Sender(transport, reader, pl, args.dest, window=args.window,
                        chunk_timeout=args.chunk_timeout,
                        chunk_retries=args.chunk_retries,
                        finish_timeout=args.finish_timeout,
                        progress=not args.no_progress)
        stats = sender.send()

        report.banner("DONE")
        report.kv("chunks sent", stats.chunks)
        report.kv("retransmits", stats.retransmits)
        report.kv("elapsed", "%.1fs" % stats.elapsed)
        report.say("cli", "deployed to %s:%s" % (args.port, args.dest))
        return EXIT_OK

    except LoginError as exc:
        report.error("login/session failed: %s" % exc)
        return EXIT_SESSION
    except BootstrapError as exc:
        report.error("receiver bootstrap failed: %s" % exc)
        return EXIT_SESSION
    except TransferError as exc:
        report.error("transfer failed: %s" % exc)
        return EXIT_TRANSFER
    except KeyboardInterrupt:
        report.error("aborted by operator")
        return EXIT_ABORT
    finally:
        if session is not None:
            try:
                session.disconnect()
            except Exception:
                pass
        transport.close()
        if transcript is not None:
            transcript.close()


if __name__ == "__main__":
    sys.exit(main())
