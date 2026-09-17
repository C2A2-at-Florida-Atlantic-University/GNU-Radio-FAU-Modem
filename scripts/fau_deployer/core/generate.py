#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""The Process phase: `.grc` in, a board-ready `.py` out.

    ingest -> preflight -> headless transform -> grcc -> locate the .py

This is the one module in `core/` that needs a real GNU Radio install on
the desktop, and only for the last step: `grcc` is run as a subprocess
rather than imported. Importing `gnuradio.grc` would tie the deployer to
one GRC version's internals -- the same coupling the plan rejected for the
Executor patch -- whereas the command line (`grcc -o DIR FILE`) has been
stable across every version this repo has met, and a subprocess cannot take
the tool down with it when code generation raises.

**Never `grcc -r`.** It compiles *and runs* the flowgraph, here on the
desktop, where `fau_source`/`fau_sink` would open `/dev/mem` on the wrong
machine.

Output goes to a cache directory keyed by the `.grc`'s absolute path, not
next to the user's file and not to a fresh temp dir each time: the derived
`.headless.grc` is an audit artifact worth being able to open after the
fact, and a stable location is what makes "regenerate only if the source
changed" cheap for the live-tracking path (see core/watch.py).
"""

import hashlib
import os
import shutil
import subprocess

from . import controls as controls_mod
from . import grcfile, headless, report
from .grcfile import GrcError

GRCC_DEFAULT = "grcc"
GRCC_TIMEOUT = 180.0

# A .grc with a hier block pulls in blocks generated from other .grc files;
# grcc can spend a while on those. 180 s is not a performance target, it is
# the point past which something is wrong and hanging the GUI's worker
# thread forever is worse than reporting it.


class GenerateError(RuntimeError):
    """Code generation failed. Message is written to be shown as-is."""


class Generated:
    """The result of one Process run, and everything the deploy needs next.

    `source_dir` matters as much as `py_path`: the generated file lands in
    the cache directory, but any sibling module the flowgraph imports (a
    `fau_tx_common.py` next to the `.grc`) still lives beside the source.
    Payload assembly has to search both, so it is recorded here rather than
    rediscovered.
    """

    def __init__(self, grc_path, py_path, headless_grc, build_dir, target,
                 transform_report, stamp, spec_path=None):
        self.grc_path = grc_path
        self.py_path = py_path
        self.headless_grc = headless_grc
        self.build_dir = build_dir
        self.target = target
        self.transform_report = transform_report
        self.stamp = stamp
        self.spec_path = spec_path
        """The ui_spec.json for this build, or None if the flowgraph has no
        QT GUI input blocks. Its presence is what the front-ends gate the
        control panel on -- see `extra_files`."""

    @property
    def controls(self):
        return self.transform_report.controls if self.transform_report else []

    @property
    def extra_files(self):
        """The non-flowgraph files that must ride along in the payload.

        `fau_ctl.py` and its spec are siblings of the generated `.py` on
        the board, not part of the image: nothing is installed, so the
        board-side half of the control channel can be iterated as fast as
        Deploy can be pressed. They are named explicitly here rather than
        found by payload.py's import scanner, because the generated `.py`
        does not import fau_ctl at module level -- the injected Snippet
        imports it inside a function, which an AST walk of the top level
        would never see.
        """
        if not self.spec_path:
            return ()
        return (fau_ctl_source(), self.spec_path)

    @property
    def source_dir(self):
        return os.path.dirname(os.path.abspath(self.grc_path))

    @property
    def main_name(self):
        return os.path.basename(self.py_path)

    def is_stale(self):
        """True if the `.grc` has changed since this was generated, or the
        generated file has gone away (or was never written, as after a
        --transform-only run)."""
        if not self.py_path or not os.path.isfile(self.py_path):
            return True
        return stamp_of(self.grc_path) != self.stamp


def stamp_of(path):
    """A cheap change token for `path`: (mtime_ns, size).

    Deliberately not a content hash. This runs on a timer while the operator
    edits in GRC, and the question being asked -- "did the file move under
    us" -- is one the metadata answers for free. A save that produces
    byte-identical content still regenerates, which costs a second and is
    the safe direction to be wrong in.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def build_dir_for(grc_path):
    """A stable per-flowgraph build directory under the user's cache.

    Keyed by the absolute path's hash as well as its stem, so two
    flowgraphs called `tx.grc` in different projects do not overwrite each
    other's generated `.py` -- which would be the deploy-the-wrong-file bug
    this whole tool exists to avoid.
    """
    abs_path = os.path.abspath(grc_path)
    stem = os.path.splitext(os.path.basename(abs_path))[0]
    digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:8]
    root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(root, "fau_deployer", "build",
                        "%s-%s" % (stem, digest))


def preflight(fg, expect_target=None):
    """Run the gates that do not need a board, and report what they found.

    Returns the inferred target role ("tx"/"rx"). Raises GrcError on any
    gate that fails.

    `expect_target` is the role the operator has selected (the Mode menu,
    i.e. the bitstream actually loaded on the board). A mismatch is refused,
    not warned about: which board role a flowgraph belongs to is decided by
    the bitstream in the PL, so shipping an RX flowgraph to a board running
    the TX design fails on the board in a way that reads as a DMA fault
    rather than as the mix-up it is.
    """
    report.banner("PREFLIGHT")
    for key, value in grcfile.describe(fg):
        report.kv(key, value)

    target = grcfile.infer_target(fg)
    report.kv("target role", target)

    if expect_target and expect_target in (grcfile.TARGET_TX, grcfile.TARGET_RX):
        if expect_target != target:
            raise GrcError(
                "this flowgraph is a %s flowgraph (it has a FAU %s), but the "
                "selected mode is %r. The loaded bitstream is what makes a "
                "board TX or RX, so this would run against the wrong PL "
                "design. Pick the %r mode, or deploy the other flowgraph."
                % (target.upper(),
                   "Source" if target == grcfile.TARGET_RX else "Sink",
                   expect_target, target))
    elif expect_target:
        report.say("preflight",
                   "mode %r is not a tx/rx role, so the target check is "
                   "advisory only -- this is a %s flowgraph"
                   % (expect_target, target.upper()))
    return target


def fau_ctl_source():
    """Path to the board-side control dispatcher in this checkout.

    It ships per deploy as a payload sibling and is never installed into
    the board image, so this is the only copy and it is always the one
    that runs. A deployer that has been updated cannot be talking to a
    stale board-side half.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "board", "fau_ctl.py")


def process(grc_path, expect_target=None, allow_message_controls=False,
            build_dir=None, grcc=GRCC_DEFAULT, dry_run=False,
            enable_controls=True):
    """Ingest, check, transform and generate. Returns a Generated.

    With `dry_run`, everything up to and including writing the derived
    `.headless.grc` happens -- so the report is real and the audit artifact
    is there to diff -- but `grcc` is not run and `py_path` is None.
    """
    grc_path = os.path.abspath(grc_path)
    stamp = stamp_of(grc_path)

    report.banner("PROCESS")
    report.kv("flowgraph", grc_path)

    fg = grcfile.load(grc_path)
    target = preflight(fg, expect_target)

    doc, transform_report = headless.transform(
        fg, allow_message_controls=allow_message_controls,
        enable_controls=enable_controls)

    build = build_dir or build_dir_for(grc_path)
    os.makedirs(build, exist_ok=True)
    derived = os.path.join(build, "%s.headless.grc" % fg.flowgraph_id)

    report.blank()
    report.banner("HEADLESS TRANSFORM")
    transform_report.emit()
    headless.write(doc, derived)
    report.kv("derived .grc", derived)

    spec_path = _write_spec(build, fg, transform_report)

    if dry_run:
        report.say("process", "dry run -- grcc not invoked")
        return Generated(grc_path, None, derived, build, target,
                         transform_report, stamp, spec_path)

    py_path = run_grcc(derived, build, fg.flowgraph_id, grcc=grcc,
                       source_dir=os.path.dirname(grc_path))
    report.kv("generated", py_path)
    return Generated(grc_path, py_path, derived, build, target,
                     transform_report, stamp, spec_path)


def _write_spec(build, fg, transform_report):
    """Write ui_spec.json, or remove a stale one and return None.

    Removing matters as much as writing: the build directory is stable
    across regenerations, so a spec left behind after the operator deletes
    the last slider from their flowgraph would ship a panel for controls
    the running flowgraph no longer has. Every SET would come back ERR,
    which reads as a broken control channel rather than as a stale file.
    """
    path = os.path.join(build, controls_mod.SPEC_FILENAME)
    if not transform_report.controls:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    controls_mod.write_spec(
        controls_mod.build_spec(fg.flowgraph_id, transform_report.controls),
        path)
    report.kv("control spec", "%s (%d control%s)"
              % (path, len(transform_report.controls),
                 "" if len(transform_report.controls) == 1 else "s"))
    return path


def run_grcc(grc_path, out_dir, expect_id, grcc=GRCC_DEFAULT,
             source_dir=None):
    """Run `grcc -o out_dir grc_path` and return the `.py` it wrote.

    Never `-r`: that would run the flowgraph on this desktop, where the FAU
    blocks would try to open /dev/mem on the wrong machine.

    `source_dir` is the directory the ORIGINAL `.grc` lives in, and it is
    put on grcc's PYTHONPATH and used as its working directory. This is not
    a convenience: an `import` block is *evaluated* during code generation,
    so a flowgraph that does `import fau_tx_common` fails to compile at all
    unless that module is importable -- and the headless copy being
    compiled sits in the build directory, nowhere near it. Without this,
    every flowgraph with a helper module is a "Flowgraph invalid" error
    that reads as a problem with the flowgraph rather than with where it
    was compiled from.
    """
    exe = shutil.which(grcc)
    if exe is None:
        raise GenerateError(
            "%r is not on PATH, so a .grc cannot be compiled here. Either "
            "install GNU Radio on this desktop, or generate the .py "
            "elsewhere (`grcc -o DIR flowgraph.grc`) and select that file "
            "instead -- the deployer is perfectly happy with a .py."
            % grcc)

    expected = os.path.join(out_dir, "%s.py" % expect_id)
    before = _py_stamps(out_dir)

    env = None
    cwd = None
    if source_dir and os.path.isdir(source_dir):
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (source_dir + os.pathsep + existing) if existing \
            else source_dir
        cwd = source_dir

    report.say("grcc", "%s -o %s %s" % (exe, out_dir, os.path.basename(grc_path)))
    try:
        proc = subprocess.run([exe, "-o", out_dir, grc_path],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=GRCC_TIMEOUT, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise GenerateError(
            "grcc did not finish within %.0f s. Try it by hand (`grcc -o %s "
            "%s`) -- something in the flowgraph is hanging code generation."
            % (GRCC_TIMEOUT, out_dir, grc_path))
    except OSError as exc:
        raise GenerateError("could not run %s: %s" % (exe, exc))

    output = proc.stdout.decode("utf-8", "replace").strip()
    for line in output.splitlines():
        report.say("grcc", line)

    if proc.returncode != 0:
        raise GenerateError(
            "grcc failed (exit %d) on the headless copy at %s.\n\n%s\n\n"
            "The copy is left in place on purpose -- open it in GRC to see "
            "what the transform produced."
            % (proc.returncode, grc_path, output or "(no output)"))

    if os.path.isfile(expected):
        return expected

    # grcc names its output after the flowgraph's `id` option, not the file,
    # so `expected` is normally right. Fall back to whatever .py it actually
    # touched rather than failing on a naming assumption.
    written = [p for p, s in _py_stamps(out_dir).items() if before.get(p) != s]
    if len(written) == 1:
        return written[0]
    if written:
        raise GenerateError(
            "grcc wrote %d Python files into %s and none is the expected "
            "%s.py, so which one to run is ambiguous: %s"
            % (len(written), out_dir, expect_id,
               ", ".join(sorted(os.path.basename(p) for p in written))))
    raise GenerateError(
        "grcc reported success but wrote no Python file into %s. Expected "
        "%s.py.%s" % (out_dir, expect_id,
                      ("\n\n" + output) if output else ""))


def _py_stamps(directory):
    out = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".py"):
            continue
        path = os.path.join(directory, name)
        st = stamp_of(path)
        if st is not None:
            out[path] = st
    return out


def looks_like_grc(path):
    return str(path).strip().lower().endswith(".grc")
