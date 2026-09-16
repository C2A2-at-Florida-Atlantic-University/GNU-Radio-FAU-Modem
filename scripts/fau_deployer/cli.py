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

Does transport (--flowgraph), running (--run), and loading a pre-staged
bitstream (--load-bitstream), against a board from bitstreams.json
(--board) reached over an explicit --port. Composable in one invocation, in the order
they have to happen: bitstream, then deploy, then run.

Still NOT here: parsing or transforming a .grc file (the "Process" phase --
input is a generated .py). See docs/plans/grc-deployer-plan.md. The
interactive front-end over the same core is gui.py.

Needs root: no. The board-side login user is a regular (non-root) account;
nothing here touches /dev/mem or the DMA window itself.
"""

import argparse
import os
import signal
import sys
import time

from .core import boards as boards_mod, bootstrap, fpga, generate
from .core import params as params_mod, payload, report, watch
from .core.boards import BoardsError
from .core.bootstrap import BootstrapError
from .core.fpga import FpgaError
from .core.generate import GenerateError
from .core.grcfile import GrcError
from .core.params import ParamError
from .core.payload import PayloadError
from .core.protocol import (
    CHUNK_B64_DEFAULT,
    DEST_DEFAULT,
    IDLE_TIMEOUT_DEFAULT,
)
from .core.runner import Runner
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
EXIT_BITSTREAM = 5
EXIT_RUN = 6
# Distinct from every other failure on purpose: it means a flowgraph was
# stopped and never confirmed it halted, so the board may still have DMA
# live. A CI job must be able to tell that apart from an ordinary failure.
EXIT_WEDGED = 7
EXIT_ABORT = 130

RECEIVER_SHUTDOWN_TIMEOUT = 5.0


def _build_parser():
    p = argparse.ArgumentParser(
        prog="fau-deploy",
        description="Ship a generated flowgraph to a Zynq board over its "
                    "serial console.")

    p.add_argument("--flowgraph", metavar="PATH",
                   help="the flowgraph to deploy and/or run: either a .grc, "
                        "which is preflighted, made headless and compiled "
                        "with grcc first, or an already-generated .py, which "
                        "is sent as it is. Local sibling imports (e.g. a "
                        "shared helper module next to it) are discovered "
                        "automatically -- see --extra to add any this "
                        "misses. Required unless the only action asked for "
                        "is --load-bitstream or --list-boards")
    p.add_argument("--extra", action="append", default=[], metavar="PATH",
                   help="an additional file to ship alongside --flowgraph; "
                        "repeatable")

    p.add_argument("--board", metavar="NAME",
                   help="which board in bitstreams.json (e.g. S10) -- "
                        "selects the bitstreams --load-bitstream can reach "
                        "(see --mode) and, with --credentials, the login. "
                        "It says nothing about how to reach the board: pass "
                        "--port for that")
    p.add_argument("--bitstreams", metavar="PATH",
                   default=boards_mod.BITSTREAMS_PATH_DEFAULT,
                   help="bitstream file to read (default: %(default)s)")
    p.add_argument("--credentials", metavar="PATH",
                   default=boards_mod.CREDENTIALS_PATH_DEFAULT,
                   help="credentials file for board logins (default: "
                        "%(default)s). Optional and gitignored -- absent, "
                        "the documented " + boards_mod.USER_DEFAULT + "/"
                        + boards_mod.PASSWORD_DEFAULT + " is used")
    p.add_argument("--mode", metavar="NAME",
                   help="which of the board's bitstreams to load (e.g. "
                        "bootstrap, tx, rx); only omittable when the board "
                        "lists exactly one")
    p.add_argument("--list-boards", action="store_true",
                   help="print the bitstream file and exit")

    # These four default to None rather than a value, so "the operator gave
    # this explicitly" stays distinguishable from "nobody said" -- which is
    # what lets --board fill them in without silently overriding a flag.
    p.add_argument("--port", required=False, metavar="DEV",
                   help="serial device, e.g. /dev/ttyUSB0. Always required "
                        "to talk to a board: port assignments are not "
                        "static, so nothing records or guesses one. Prefer "
                        "a /dev/serial/by-id path, since ttyUSB numbering "
                        "swaps between boards on a replug")
    p.add_argument("--baud", type=int, metavar="BAUD",
                   help="serial baud rate (default: %d)"
                        % boards_mod.BAUD_DEFAULT)

    p.add_argument("--user", metavar="USER",
                   help="board login user (default: from --credentials, "
                        "else %s)" % boards_mod.USER_DEFAULT)
    p.add_argument("--password", metavar="PASS",
                   help="board login password (default: from --credentials, "
                        "else %s)" % boards_mod.PASSWORD_DEFAULT)

    p.add_argument("--dest", metavar="DIR",
                   help="board-side directory the flowgraph is written into "
                        "(default: %s)" % DEST_DEFAULT)
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

    p.add_argument("--load-bitstream", action="store_true",
                   help="load the pre-staged bitstream for --board/--mode "
                        "before anything else. This replaces the PL, so it "
                        "runs first and only with nothing else in flight")
    p.add_argument("--no-deploy", action="store_true",
                   help="skip the transfer -- for running or re-running a "
                        "flowgraph already on the board")
    p.add_argument("--run", action="store_true",
                   help="run the flowgraph on the board after deploying and "
                        "stream its output. Ctrl-C stops it the safe way "
                        "(SIGINT to the flowgraph, which tears the DMA down "
                        "cleanly) rather than aborting this tool")
    p.add_argument("--params", metavar="ARGS", default="",
                   help="arguments to pass to the flowgraph, as one quoted "
                        "string (e.g. --params '--nco-freq 2e6'). Split "
                        "here and re-quoted per token, so shell "
                        "metacharacters are passed through as data. "
                        "Checked against the flowgraph's own options first")
    p.add_argument("--no-sudo", action="store_true",
                   help="run the flowgraph without sudo. The blocks open "
                        "/dev/mem and lock under /run/lock, so this only "
                        "makes sense when the login user is already root")
    p.add_argument("--no-ground", action="store_true",
                   help="adopt an already-logged-in console instead of "
                        "logging out (Ctrl-D) and back in for a known-clean "
                        "session")

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

    g = p.add_argument_group(
        "process (.grc -> .py)",
        "Only meaningful when --flowgraph names a .grc. Compiling it needs "
        "GNU Radio on THIS machine; the board never sees a .grc.")
    g.add_argument("--process-only", action="store_true",
                   help="preflight, transform and compile the .grc, print "
                        "where the .py landed, and stop. Opens no port")
    g.add_argument("--no-process", action="store_true",
                   help="refuse to compile: fail if --flowgraph is a .grc. "
                        "For a CI job that wants to be sure it is shipping a "
                        "reviewed .py and not one generated on the spot")
    g.add_argument("--allow-message-controls", action="store_true",
                   help="proceed even though the flowgraph has GUI controls "
                        "that send messages. Headless nobody presses them, "
                        "so those messages never fire -- a change in what "
                        "the flowgraph does, which is why it is not the "
                        "default")
    g.add_argument("--build-dir", metavar="DIR",
                   help="where the headless copy and the generated .py go "
                        "(default: a per-flowgraph directory under "
                        "$XDG_CACHE_HOME/fau_deployer/build)")
    g.add_argument("--grcc", default=generate.GRCC_DEFAULT, metavar="EXE",
                   help="the GRC compiler to run (default: %(default)s)")
    g.add_argument("--transform-only", action="store_true",
                   help="write the headless .grc and print what changed, but "
                        "do not run grcc. The derived file is the audit "
                        "artifact -- diff it against the original")
    g.add_argument("--watch", action="store_true",
                   help="with --process-only, stay running and recompile "
                        "every time the .grc is saved. Deploying is "
                        "deliberately NOT automatic: sending a new flowgraph "
                        "to a board that is running one is a decision, not a "
                        "reflex")

    return p


def _validate(parser, args):
    if args.chunk_size <= 0 or args.chunk_size % 4 != 0:
        parser.error("--chunk-size must be a positive multiple of 4 (got %r)"
                    % args.chunk_size)
    if args.window < 1:
        parser.error("--window must be >= 1")
    if args.baud is not None and args.baud <= 0:
        parser.error("--baud must be > 0")
    if args.max_bytes <= 0:
        parser.error("--max-bytes must be > 0")
    if args.watch and not args.process_only:
        parser.error("--watch only makes sense with --process-only: a watch "
                     "that redeployed on every save would ship a flowgraph "
                     "to a board nobody is looking at")
    if args.process_only and args.no_process:
        parser.error("--process-only and --no-process contradict each other")
    if args.no_deploy and not (args.run or args.load_bitstream):
        parser.error("--no-deploy leaves nothing to do -- add --run and/or "
                    "--load-bitstream")


def _print_boards(bits, creds):
    report.banner("BOARDS")
    report.kv("bitstream file", bits.path)
    for name in bits.names:
        board = bits.board(name)
        c = boards_mod.creds_for(creds, name)
        report.blank()
        report.kv("board", board.name)
        report.kv("  login", "%s%s" % (
            c.user, "" if name in creds else " (default, no credentials file)"))
        for mode in board.modes:
            seq = board.load_sequence(mode)
            suffix = "" if len(seq) == 1 else "   (after %s)" % ", ".join(
                n for n, _ in seq[:-1])
            report.kv("  %s" % mode, board.bitstream(mode) + suffix)


def _resolve_target(parser, args):
    """Fill in port/baud/user/password/dest from --board (and the
    credentials file) where the operator did not say, and pick the mode. An
    explicit flag always wins: --board is a set of defaults, not an
    override.

    Returns the selected Mode (or None when no bitstream work was asked
    for), and mutates args in place -- everything downstream then reads one
    fully-resolved args and never has to know a board was involved.
    """
    board = None
    creds = {}
    if args.board or args.list_boards:
        try:
            bits = boards_mod.load_bitstreams(args.bitstreams)
            creds = boards_mod.load_credentials(args.credentials)
        except BoardsError as exc:
            report.die(str(exc), code=EXIT_USAGE)
        if args.list_boards:
            _print_boards(bits, creds)
            return None, True
        try:
            board = bits.board(args.board)
        except BoardsError as exc:
            report.die(str(exc), code=EXIT_USAGE)

    board_creds = boards_mod.creds_for(creds, args.board) if args.board \
        else boards_mod.BoardCreds()

    if args.baud is None:
        args.baud = boards_mod.BAUD_DEFAULT
    if args.user is None:
        args.user = board_creds.user
    if args.password is None:
        args.password = board_creds.password
    if args.dest is None:
        args.dest = DEST_DEFAULT

    # --process-only is entirely local, like --dry-run: it compiles a
    # .grc and stops, so demanding a port would mean plugging a board in
    # to do code generation.
    if not args.port and not (args.dry_run or args.process_only):
        # Never inferred from --board: bitstreams.json records nothing about
        # how to reach a board, because a recorded port goes stale and
        # deploys to the wrong one.
        parser.error("no serial port -- pass --port (e.g. --port "
                    "/dev/ttyUSB0). Port assignments are not static, so "
                    "nothing records or guesses one")

    steps = None
    if args.load_bitstream:
        if board is None:
            parser.error("--load-bitstream needs --board NAME -- the "
                        "bitstream to load is looked up in bitstreams.json, "
                        "not passed by hand")
        mode = args.mode or board.default_mode
        if mode is None:
            parser.error(
                "board %r lists several bitstreams (%s) -- say which with "
                "--mode" % (board.name, ", ".join(board.modes)))
        try:
            steps = board.load_sequence(mode)
        except BoardsError as exc:
            report.die(str(exc), code=EXIT_USAGE)
        if mode != boards_mod.BOOTSTRAP_KEY and not board.has_bootstrap:
            report.warn(
                "board %r has no %r entry, so %r is being loaded on its own. "
                "If this board needs a base design first, add one to %s"
                % (board.name, boards_mod.BOOTSTRAP_KEY, mode, args.bitstreams))
    return steps, False


def _process(args):
    """Turn a .grc into a .py, if that is what --flowgraph named.

    Rewrites args.flowgraph to the generated file and records the source
    directory in args.search_dirs, so everything downstream -- payload
    assembly, --params checking, the name the board runs -- works on the
    generated Python and never has to know a .grc was involved.

    A .py --flowgraph passes straight through, which is what keeps the CI
    path unchanged.
    """
    args.search_dirs = ()
    args.generated = None
    if not args.flowgraph or not generate.looks_like_grc(args.flowgraph):
        if args.process_only:
            report.die("--process-only needs a .grc; %s is already generated "
                       "Python" % args.flowgraph, code=EXIT_USAGE)
        return

    if args.no_process:
        report.die(
            "--flowgraph %s is a .grc and --no-process was given. Compile it "
            "yourself (grcc -o DIR %s) and pass the .py."
            % (args.flowgraph, args.flowgraph), code=EXIT_USAGE)

    try:
        gen = generate.process(
            args.flowgraph, expect_target=args.mode,
            allow_message_controls=args.allow_message_controls,
            build_dir=args.build_dir, grcc=args.grcc,
            dry_run=args.transform_only)
    except (GrcError, GenerateError) as exc:
        report.die(str(exc), code=EXIT_USAGE)

    args.generated = gen
    if gen.py_path is None:  # --transform-only
        return
    args.flowgraph = gen.py_path
    args.search_dirs = (gen.source_dir,)


def _watch_process(args):
    """--process-only --watch: recompile on every save until Ctrl-C.

    Deliberately does not deploy. The point of watching is to keep the
    generated .py honest while the flowgraph is being edited; pushing each
    save to a board -- possibly one mid-run -- is a separate decision, and
    one a tool should not make on its own.
    """
    w = watch.Watcher(args.flowgraph)
    report.say("cli", "watching %s -- Ctrl-C to stop" % args.flowgraph)
    try:
        while True:
            time.sleep(0.25)
            if not w.poll():
                continue
            w.acknowledge()
            report.blank()
            report.say("cli", "%s changed -- reprocessing"
                       % os.path.basename(args.flowgraph))
            try:
                generate.process(
                    args.flowgraph, expect_target=args.mode,
                    allow_message_controls=args.allow_message_controls,
                    build_dir=args.build_dir, grcc=args.grcc,
                    dry_run=args.transform_only)
            except (GrcError, GenerateError) as exc:
                # Keep watching. A flowgraph saved mid-edit is routinely
                # invalid for a few seconds, and exiting on the first bad
                # save would make the mode useless exactly when it helps.
                report.error(str(exc))
    except KeyboardInterrupt:
        report.blank()
        report.say("cli", "stopped watching")
    return EXIT_OK


def _prepare_payload(args):
    """Collect + build the payload, print the manifest/ETA banner. Returns
    the Payload, or exits with EXIT_USAGE on a PayloadError."""
    try:
        entries, main_arc = payload.collect_files(
            args.flowgraph, extra=args.extra, max_bytes=args.max_bytes,
            search_dirs=getattr(args, "search_dirs", ()))
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


def _shutdown_receiver(session):
    """Retire the board-side receiver once a transfer is done, the same way
    core/bootstrap.py retires a stale one before pushing a fresh copy: 0x03
    (ShutdownRequested on the board side), then wait for the shell to
    actually reclaim the console.

    Deliberate choice: this CLI is single-shot (one payload per invocation),
    so there is nothing to keep the receiver alive for -- leaving it running
    just has it idle until --idle-timeout elapses on its own for no benefit.
    A fresh launch's READY handshake is sub-second, so there's no real cost
    to tearing down every time; the next invocation just bootstraps again.
    Best-effort: the deploy already succeeded by the time this runs, so a
    failure here is a warning, not a fatal error.
    """
    if not session.interrupt_and_wait(RECEIVER_SHUTDOWN_TIMEOUT):
        report.warn(
            "receiver did not release the console within %.0fs of being "
            "sent Ctrl-C after a successful transfer -- it will time out "
            "on its own (--idle-timeout) instead" % RECEIVER_SHUTDOWN_TIMEOUT)



def _run_flowgraph(session, transport, reader, args, main_name):
    """Run the flowgraph in the foreground and stream its output.

    Ctrl-C at the terminal must NOT abort this tool -- it must stop the
    flowgraph. Aborting here would leave a flowgraph running on the board
    with nobody holding its console, and the operator's next instinct
    (re-run, or unplug) is how a live DMA burst gets orphaned. So SIGINT is
    caught for the duration of the run and turned into the Runner's stop
    request, which sends 0x03 to the board and then waits for the halt to
    be confirmed. Repeated Ctrl-C is deliberately just as gentle: there is
    no harder signal this tool is willing to send (see runner.terminate()).
    """
    try:
        tokens = params_mod.check(args.params, args.flowgraph)
    except ParamError as exc:
        report.die(str(exc), code=EXIT_USAGE)

    r = Runner(session, transport, reader, args.dest, main_name,
               params=tokens, sudo=not args.no_sudo)
    report.banner("RUN")
    report.say("run", r.command)
    report.say("run", "Ctrl-C stops the flowgraph (it does not abort "
                      "fau-deploy)")

    stop = {"asked": False}

    def on_sigint(_sig, _frame):
        stop["asked"] = True

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        result = r.run(on_line=lambda line: report.say("board", line),
                       should_stop=lambda: stop["asked"])
    finally:
        signal.signal(signal.SIGINT, previous)

    report.banner("RUN FINISHED")
    report.kv("exit status", result.rc)
    report.kv("stopped by operator", result.terminated)
    report.kv("output lines", result.lines)
    report.kv("elapsed", "%.1fs" % result.elapsed)

    if result.wedged:
        return EXIT_WEDGED
    if result.rc:
        return EXIT_RUN
    return EXIT_OK


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate(parser, args)

    steps, listed_only = _resolve_target(parser, args)
    if listed_only:
        return EXIT_OK

    deploy = not args.no_deploy and not args.process_only
    if (deploy or args.run) and not args.flowgraph:
        parser.error("--flowgraph is required to deploy or run "
                    "(pass --no-deploy with neither to only load a "
                    "bitstream)")

    # Before anything opens a port: compiling a .grc is local, and failing
    # here costs nothing, whereas failing after login leaves a session to
    # tear down for no reason.
    _process(args)
    if args.process_only:
        if args.watch:
            return _watch_process(args)
        return EXIT_OK

    pl = _prepare_payload(args) if deploy else None
    main_name = os.path.basename(args.flowgraph) if args.flowgraph else None
    if pl is not None:
        main_name = pl.main_arcname
    if args.dry_run:
        report.say("cli", "--dry-run: not opening the port")
        return EXIT_OK

    transcript = Transcript(args.transcript) if args.transcript else None
    try:
        transport = SerialTransport(args.port, args.baud, transcript=transcript)
    except TransportError as exc:
        report.die(str(exc), code=EXIT_USAGE)

    reader = LineReader(transport)
    try:
        report.banner("BOARD")
        session = BoardSession(transport, reader, user=args.user,
                              password=args.password,
                              login_timeout=args.login_timeout,
                              verbose=args.verbose,
                              ground=not args.no_ground)
        session.connect()
        report.say("cli", "logged in as %s" % args.user)

        # Bitstream first: it replaces the PL, so everything after it sees
        # the hardware it is meant to run against, and nothing is in flight
        # when the fabric goes away.
        if steps is not None:
            report.banner("BITSTREAM")
            fpga.load_sequence(session, steps, password=args.password)

        if pl is not None:
            bootstrap.ensure_receiver(
                session, transport, reader, RECEIVER_PATH,
                remote_dir=args.remote_dir, dest=args.dest,
                idle_timeout=args.idle_timeout, chunk_size=args.chunk_size,
                handshake_timeout=args.handshake_timeout,
                force=args.force_bootstrap)

            report.banner("TRANSFER")
            sender = Sender(transport, reader, pl, args.dest,
                            window=args.window,
                            chunk_timeout=args.chunk_timeout,
                            chunk_retries=args.chunk_retries,
                            finish_timeout=args.finish_timeout,
                            progress=not args.no_progress)
            stats = sender.send()
            # Before any run: the receiver owns the console while it is
            # alive, so a flowgraph launched under it would have its output
            # eaten as protocol noise.
            _shutdown_receiver(session)

            report.banner("DONE")
            report.kv("chunks sent", stats.chunks)
            report.kv("retransmits", stats.retransmits)
            report.kv("elapsed", "%.1fs" % stats.elapsed)
            report.say("cli", "deployed to %s:%s" % (args.port, args.dest))

        if args.run:
            return _run_flowgraph(session, transport, reader, args, main_name)
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
    except FpgaError as exc:
        report.error("bitstream load failed: %s" % exc)
        return EXIT_BITSTREAM
    except KeyboardInterrupt:
        report.error("aborted by operator")
        return EXIT_ABORT
    finally:
        transport.close()
        if transcript is not None:
            transcript.close()


if __name__ == "__main__":
    sys.exit(main())
