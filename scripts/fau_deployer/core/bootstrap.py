#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Get board/receiver.py onto the board and running, self-healing on every
deploy rather than relying on a one-time manual copy.

Root CLAUDE.md records exactly the failure mode this avoids: hand-scp'd
board copies going stale and costing three debugging rounds on an earlier
bug, because the deployed code silently stopped matching what was being
tested against. A sha comparison on every run makes that structurally
impossible here, at a cost of about half a second when nothing changed.
"""

import base64
import gzip
import hashlib
import posixpath
import shlex

from . import report
from .protocol import PROTO_VERSION, parse_line, split_b64
from .session import LoginError

REMOTE_DIR_DEFAULT = "/home/petalinux/.fau"
CHUNK_SIZE_DEFAULT = 512
HANDSHAKE_TIMEOUT_DEFAULT = 15.0

_DECODE_TEMPLATE = (
    'python3 -c "'
    "import base64,gzip,hashlib,os;"
    "d=base64.b64decode(open('{b64_path}','rb').read());"
    "raw=gzip.decompress(d);"
    "open('{dest_path}','wb').write(raw);"
    "os.remove('{b64_path}');"
    "print('FAU-BOOTSHA:'+hashlib.sha256(raw).hexdigest()[:16])"
    '"'
)


class BootstrapError(RuntimeError):
    """Pushing or verifying the receiver failed in a way that should stop
    the deploy rather than proceed against a possibly-broken board copy."""


def local_sha16(path):
    with open(path, "rb") as f:
        data = f.read()
    return hashlib.sha256(data).hexdigest()[:16]


def push_file(session, local_path, remote_path, chunk_size=CHUNK_SIZE_DEFAULT):
    """Push local_path's bytes onto the board at remote_path, using the
    shell prompt itself as flow control -- there is no receiver to talk the
    real protocol to yet; that's exactly the problem this function solves.

    Returns the sha16 the BOARD computed from what it actually wrote (not
    just "the copy exited 0"), so the caller can compare it against the
    local file's own hash rather than trust the transfer blindly.
    """
    with open(local_path, "rb") as f:
        data = f.read()
    gz = gzip.compress(data, compresslevel=9, mtime=0)
    b64 = base64.b64encode(gz).decode("ascii")

    remote_dir = posixpath.dirname(remote_path)
    b64_path = remote_path + ".b64"
    session.run_ok("mkdir -p %s && rm -f %s"
                   % (shlex.quote(remote_dir), shlex.quote(b64_path)))

    chunks = split_b64(b64, chunk_size)
    for i, chunk in enumerate(chunks):
        report.say("bootstrap", "pushing %s: chunk %d/%d"
                   % (posixpath.basename(remote_path), i + 1, len(chunks)))
        # printf '%s' <chunk> (not `printf <chunk>`) so a chunk that happens
        # to contain a literal '%' is never interpreted as its own format
        # directive -- it's always the ARGUMENT to a fixed format string.
        cmd = "printf '%%s' %s >> %s" % (shlex.quote(chunk), shlex.quote(b64_path))
        session.run_ok(cmd)

    decode_cmd = _DECODE_TEMPLATE.format(b64_path=b64_path, dest_path=remote_path)
    out = session.run_ok(decode_cmd, timeout=15.0)
    prefix = "FAU-BOOTSHA:"
    idx = out.find(prefix)
    if idx == -1:
        raise BootstrapError(
            "board-side decode of %s did not report a sha (output: %r)"
            % (remote_path, out))
    got_sha = out[idx + len(prefix):idx + len(prefix) + 16]

    want_sha = local_sha16(local_path)
    if got_sha != want_sha:
        raise BootstrapError(
            "pushed %s but the board's sha (%s) does not match the local "
            "file's (%s) -- corrupted in transit during the bootstrap push"
            % (remote_path, got_sha, want_sha))
    return got_sha


def _try_launch_and_read_ready(transport, reader, launch_cmd, timeout):
    """Send the launch command directly on the raw transport (NOT through
    BoardSession.run() -- the receiver never prints our PS1 marker, it
    prints READY and then stays running, so the shell-command abstraction
    doesn't apply once this line is sent). Returns the parsed ('ready',
    fields) tuple, or None if no READY sentinel arrived in time.

    On any failure to get READY, the launch attempt itself (e.g. "No such
    file") has almost certainly already returned control to the shell,
    which means a fresh, unterminated prompt is now sitting in the reader's
    buffer -- reset() discards it so the CALLER's next BoardSession.run()
    isn't handed that stale prompt instead of its own command's real reply
    (the same "unconsumed tail bleeds into the next read" hazard fixed in
    BoardSession._probe()/_do_password(), at the seam where this module
    hands the reader back to shell-command use).
    """
    transport.write((launch_cmd + "\r").encode("utf-8"))
    matched = reader.wait_for("===FAU-RECV-READY", timeout)
    if matched is None:
        reader.reset()
        return None
    parsed = parse_line(matched)
    if parsed is None or parsed[0] != "ready":
        reader.reset()
        return None
    return parsed


def ensure_receiver(session, transport, reader, local_receiver_path,
                    remote_dir=REMOTE_DIR_DEFAULT, dest=None,
                    idle_timeout=None, chunk_size=CHUNK_SIZE_DEFAULT,
                    handshake_timeout=HANDSHAKE_TIMEOUT_DEFAULT,
                    force=False):
    """Get the receiver running on the board and confirmed current, then
    return. After this returns, `transport`/`reader` are mid-conversation
    with the receiver (it has already announced READY) -- the caller hands
    them straight to Sender, not back to BoardSession.

    `session` must already be connected (BoardSession.connect() called) --
    this only uses it for the shell commands needed to push/verify a fresh
    copy, never to log in.
    """
    from .protocol import DEST_DEFAULT, IDLE_TIMEOUT_DEFAULT
    dest = dest or DEST_DEFAULT
    idle_timeout = idle_timeout if idle_timeout is not None else IDLE_TIMEOUT_DEFAULT

    remote_path = posixpath.join(remote_dir, "receiver.py")
    want_sha = local_sha16(local_receiver_path)
    launch_cmd = "python3 %s --dest %s --idle-timeout %s" % (
        shlex.quote(remote_path), shlex.quote(dest), idle_timeout)

    if not force:
        parsed = _try_launch_and_read_ready(
            transport, reader, launch_cmd, handshake_timeout)
        if parsed is not None:
            _kind, fields = parsed
            if fields["version"] != PROTO_VERSION:
                raise BootstrapError(
                    "board receiver speaks protocol %s, this deployer "
                    "speaks %s -- refusing to guess at compatibility"
                    % (fields["version"], PROTO_VERSION))
            if fields["sha16"] == want_sha:
                report.say("bootstrap",
                          "receiver already current (sha %s)" % want_sha)
                return
            report.say("bootstrap",
                      "receiver present but stale (board sha %s, local "
                      "sha %s) -- retiring it and pushing a fresh copy"
                      % (fields["sha16"], want_sha))
            transport.write(b"\x03")  # ShutdownRequested on the board side
            # Wait for the shell to actually reclaim the console (its PS1
            # reappears once the receiver it was running exits) before
            # sending anything else. Sending the next shell command too
            # soon risks the still-dying receiver reading those bytes as
            # protocol noise and discarding them before it exits -- they
            # would never reach the shell at all, and the command that
            # follows would silently never have run.
            reclaimed = reader.wait_for(session.ps1_marker, 5.0)
            if reclaimed is None:
                raise BootstrapError(
                    "the stale receiver did not release the console within "
                    "5s of being sent Ctrl-C -- it may be wedged")
            reader.reset()
        else:
            report.say("bootstrap",
                      "no receiver response within %.0fs -- pushing a "
                      "fresh copy" % handshake_timeout)

    session.run_ok("mkdir -p %s" % shlex.quote(remote_dir))
    got_sha = push_file(session, local_receiver_path, remote_path, chunk_size)
    report.say("bootstrap", "pushed receiver.py (sha %s)" % got_sha)

    parsed = _try_launch_and_read_ready(
        transport, reader, launch_cmd, handshake_timeout)
    if parsed is None:
        raise BootstrapError(
            "receiver did not announce READY after a fresh bootstrap push")
    _kind, fields = parsed
    if fields["sha16"] != want_sha:
        raise BootstrapError(
            "receiver still reports sha %s after a fresh push of a file "
            "whose local sha is %s" % (fields["sha16"], want_sha))
    report.say("bootstrap", "receiver ready (sha %s)" % want_sha)
