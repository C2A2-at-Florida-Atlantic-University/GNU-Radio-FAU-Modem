#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Payload assembly: discover a flowgraph's local sibling dependencies,
then tar + gzip + hash + base64 + chunk them into one deterministic bundle.

Multi-file is the default case, not an edge case -- e.g.
components/layers/meta-fau-modem/gr-fau_modem/examples/tx_sine.py does
`from fau_tx_common import (...)`, a sibling module. Shipping the flowgraph
alone would transfer cleanly and then fail with ImportError on the board,
which is a worse failure than a slow one: it looks like success. So
dependency discovery is not optional plumbing here, it is the point.
"""

import ast
import base64
import dataclasses
import gzip
import hashlib
import io
import tarfile
from pathlib import Path
from typing import List, Tuple

from .protocol import CHUNK_B64_DEFAULT, new_xfer_id, split_b64

MAX_BYTES_DEFAULT = 4 * 1024 * 1024  # 4 MiB; see --max-bytes


class PayloadError(ValueError):
    """Raised for anything wrong with the requested file set -- a bad path,
    a symlink, a name collision, or exceeding --max-bytes. Always carries a
    message meant to be shown to the operator as-is."""


def _local_import_names(path):
    """Best-effort list of module names `path` imports that MIGHT be local
    sibling scripts. Only undotted names are considered -- a dotted import
    (e.g. `from gnuradio import fau_modem`) can never resolve to a sibling
    `.py` file by the naming scheme this repo's flowgraphs use, so it is
    filtered here rather than left for the caller's is_file() check to
    silently absorb.

    A file that fails to parse (e.g. it is not actually Python, or uses a
    newer grammar than this interpreter) contributes no names rather than
    aborting the whole collection -- it will still be shipped itself, just
    without its own transitive dependencies discovered.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []

    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "." not in alias.name:
                    names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and "." not in node.module:
                names.append(node.module)
    return names


def collect_files(main, extra=(), max_bytes=MAX_BYTES_DEFAULT,
                  search_dirs=()):
    """Resolve `main` (the flowgraph to run) plus every local sibling module
    it imports, transitively, plus any `extra` paths named explicitly.

    Returns an ordered list of (Path, arcname) tuples, `main` first. Raises
    PayloadError on a directory, a symlink, a duplicate arcname pointing at
    two different files, or a total size over `max_bytes`.

    `search_dirs` are extra directories to look for those siblings in,
    searched after `main`'s own. The Process phase needs this: a generated
    flowgraph lands in a build directory, but the `fau_tx_common.py` it
    imports still sits beside the `.grc` it came from, so "sibling" has to
    mean sibling of the *source* as well as of the generated file.
    """
    main_path = Path(main)
    if not main_path.is_file():
        raise PayloadError("--flowgraph %s: not a file" % main)
    if main_path.is_symlink():
        raise PayloadError("--flowgraph %s: refusing to follow a symlink" % main)

    src_dir = main_path.resolve().parent
    # Ordered, de-duplicated: main's own directory first, so a module that
    # exists in both wins where the flowgraph itself lives.
    lookup_dirs = [src_dir]
    for d in search_dirs:
        resolved = Path(d).resolve()
        if resolved.is_dir() and resolved not in lookup_dirs:
            lookup_dirs.append(resolved)
    seen = {}   # arcname -> resolved Path
    order = []  # resolved Paths, in discovery order

    def add(path):
        path = Path(path)
        if not path.is_file():
            raise PayloadError("%s: not a file" % path)
        if path.is_symlink():
            raise PayloadError("%s: refusing to include a symlink" % path)
        resolved = path.resolve()
        arcname = resolved.name
        if arcname in seen and seen[arcname] != resolved:
            raise PayloadError(
                "duplicate arcname %r from two different files: %s vs %s"
                % (arcname, seen[arcname], resolved))
        if arcname not in seen:
            seen[arcname] = resolved
            order.append(resolved)
        return resolved

    main_resolved = add(main_path)
    for e in extra:
        add(e)

    # Breadth-first over sibling imports: a file added because it was
    # imported may itself import a further sibling (fau_tx_common.py-style
    # chains), so keep walking the growing queue, not just the seed set.
    queue = list(order)
    i = 0
    while i < len(queue):
        path = queue[i]
        i += 1
        for name in _local_import_names(path):
            for directory in lookup_dirs:
                candidate = directory / (name + ".py")
                if not candidate.is_file():
                    continue
                if candidate.resolve() in seen.values():
                    break
                queue.append(add(candidate))
                break

    total = sum(p.stat().st_size for p in order)
    if total > max_bytes:
        raise PayloadError(
            "payload would be %d bytes, over --max-bytes %d -- pass a "
            "larger --max-bytes if this is intentional" % (total, max_bytes))

    return [(p, p.name) for p in order], main_resolved.name


@dataclasses.dataclass
class Payload:
    xfer_id: str
    main_arcname: str
    entries: List[Tuple[str, int]]  # (arcname, size), sorted by arcname
    raw_bytes: int
    gz_bytes: int
    sha256: str
    b64: str
    chunks: List[str]


def build_payload(file_entries, main_arcname, chunk_size=CHUNK_B64_DEFAULT,
                  xfer_id=None):
    """Build a deterministic tar+gzip bundle from `file_entries` (as
    returned by collect_files): sorted names, mtime=0, uid=gid=0, so
    redeploying unchanged files produces byte-identical output -- a
    redeploy of nothing is then visibly a no-op rather than a mystery
    "why did the hash change" moment.
    """
    if chunk_size <= 0 or chunk_size % 4 != 0:
        raise PayloadError(
            "chunk_size must be a positive multiple of 4 (got %r) -- so "
            "every chunk decodes as valid base64 on its own, which matters "
            "for manual diagnostics" % chunk_size)
    if xfer_id is None:
        xfer_id = new_xfer_id()

    ordered = sorted(file_entries, key=lambda t: t[1])

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for path, arcname in ordered:
            data = path.read_bytes()
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    raw = tar_buf.getvalue()

    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gzf:
        gzf.write(raw)
    gz = gz_buf.getvalue()

    sha_hex = hashlib.sha256(gz).hexdigest()
    b64 = base64.b64encode(gz).decode("ascii")
    chunks = split_b64(b64, chunk_size)

    entries = [(arcname, path.stat().st_size) for path, arcname in ordered]

    return Payload(
        xfer_id=xfer_id,
        main_arcname=main_arcname,
        entries=entries,
        raw_bytes=len(raw),
        gz_bytes=len(gz),
        sha256=sha_hex,
        b64=b64,
        chunks=chunks,
    )


def eta_seconds(payload, baud, window=1):
    """Rough transfer-time estimate for the banner/--eta-warn gate.

    Effective throughput at 115200 baud is roughly baud/10 bytes/sec on the
    wire (8N1 framing); base64 then costs 33% more raw bytes than the
    payload it carries, and window>1 pipelines that latency away almost
    entirely once more than one chunk's round trip is in flight -- modeled
    here as a flat throughput multiplier, not a queueing simulation, which
    is precise enough for a preflight estimate.
    """
    raw_bps = baud / 10.0
    b64_bytes = len(payload.b64)
    multiplier = min(max(window, 1), 8)  # generous cap; not a real model past this
    return b64_bytes / (raw_bps * multiplier)


def format_manifest(payload):
    """Human-readable '[payload] ...' lines listing exactly what will ship,
    so inclusion is never silent -- the whole reason collect_files() walks
    imports automatically instead of requiring every dependency to be named
    by hand.
    """
    lines = []
    for arcname, size in payload.entries:
        marker = " (main)" if arcname == payload.main_arcname else ""
        lines.append("%10d  %s%s" % (size, arcname, marker))
    return lines
