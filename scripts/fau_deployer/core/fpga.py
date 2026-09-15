#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Load a PL bitstream on the board with `fpgautil`.

Bitstreams are PRE-STAGED on the board (see core/boards.py) -- nothing is
transferred here. The whole operation is one shell command whose output is
then judged, which is why this module is small.

SUCCESS IS A POSITIVE MARKER, NEVER THE ABSENCE OF AN ERROR. Observed on
the hardware:

    $ sudo fpgautil -b dummy
    Error: User provided bitstream file doesn't exist

    $ sudo fpgautil firmware/Radio_Top_v2_wrapper.bit.bin
    $                                     <-- note: NOTHING. -b omitted,
                                              no error, nothing loaded.

    $ sudo fpgautil -b firmware/Radio_Top_v2_wrapper.bit.bin
    fpga_manager fpga0: writing Radio_Top_v2_wrapper.bit.bin to Xilinx Zynq FPGA Manager
    Time taken to load BIN is 47.000000 Milli Seconds
    BIN FILE loaded through FPGA manager successfully

That middle case is the whole reason for the rule: a plausible invocation
succeeded (rc 0) and did nothing at all. Anything that is not the explicit
confirmation line is treated as failure.

Format: `.bit.bin`, not `.bit`. The fpga_manager sysfs path wants the
bootgen-processed (header-stripped, bit-swapped) form; a raw Vivado `.bit`
is a different file.

**A bitstream load replaces the hardware every /dev/mem mapping is pointed
at.** Doing that under a running flowgraph is the same class of failure as
DMACR.Reset mid-burst -- an orphaned AXI transaction, and the root
CLAUDE.md's ~30-leaks-until-power-cycle path. Callers must ensure nothing is
running; this module cannot tell. It is also why the AXI GPIOs come back
zeroed afterwards (their s_axi_aresetn tracks FCLK_RESET0_N), so NCO/CIC/
frame_len must be reprogrammed by whatever arms next.
"""

import dataclasses
import re
import shlex

from . import report

FPGAUTIL = "fpgautil"
SUCCESS_MARKER = "loaded through FPGA manager successfully"
EXPECTED_SUFFIX = ".bit.bin"

RE_TIMING = re.compile(r"Time taken to load \S+ is ([0-9.]+)\s*Milli Seconds")
# `sudo -n` refusing for want of a password, across sudo versions.
RE_NEEDS_PASSWORD = re.compile(
    r"sudo:.*password is required|a terminal is required|no askpass",
    re.IGNORECASE)

LOAD_TIMEOUT_DEFAULT = 60.0


class FpgaError(RuntimeError):
    """The bitstream was not loaded. Message is meant to be shown as-is."""


@dataclasses.dataclass
class LoadResult:
    bitstream: str
    millis: float = None
    output: str = ""


def _run_fpgautil(session, path, timeout, password=None, use_stdin_pw=False):
    quoted = shlex.quote(path)
    if use_stdin_pw:
        # -S reads the password from stdin, -p '' suppresses the prompt
        # text. The password appears in the command line, so it also lands
        # in a --transcript if one is being written; HISTFILE is already
        # unset for the session (core/session.py), so nothing persists to
        # the board's disk.
        cmd = "printf '%%s\\n' %s | sudo -S -p '' %s -b %s" % (
            shlex.quote(password or ""), FPGAUTIL, quoted)
    else:
        # `sudo -n` (non-interactive) so a board that does want a password
        # fails in a second with a clear message, instead of hanging until
        # the command timeout at a prompt nothing is going to answer. The
        # caller retries with the -S form only if sudo actually asked.
        cmd = "sudo -n %s -b %s" % (FPGAUTIL, quoted)
    return session.run(cmd, timeout=timeout)


def load_bitstream(session, path, password=None, timeout=LOAD_TIMEOUT_DEFAULT,
                  warn_pl_replaced=True):
    """Load `path` (a board-side path, absolute or relative to the login
    user's home -- `run()` never changes directory, so a relative path
    means the same thing it does when typed by hand). Returns a LoadResult;
    raises FpgaError on any failure.

    `warn_pl_replaced` exists for load_sequence(): in a bootstrap-then-mode
    sequence the intermediate PL is superseded seconds later, so warning
    about it twice is noise that trains people to skip the warning that
    does matter -- the one after the final load.
    """
    if not path:
        raise FpgaError("no bitstream configured for this board/mode -- fill "
                        "in its \"bitstream\" field in bitstreams.json")
    if not path.endswith(EXPECTED_SUFFIX):
        report.warn(
            "%s does not end in %s -- fpgautil's FPGA-manager path expects "
            "the bootgen-processed form, and a raw Vivado .bit will not "
            "load. Continuing, since only the board can settle it."
            % (path, EXPECTED_SUFFIX))

    rc, out = session.run("test -r %s" % shlex.quote(path), timeout=15.0)
    if rc != 0:
        raise FpgaError(
            "%s is not readable on the board -- check the \"bitstream\" path "
            "in bitstreams.json against what is actually staged there "
            "(paths without a leading / are relative to the login user's "
            "home)" % path)

    report.say("fpga", "loading %s" % path)
    rc, out = _run_fpgautil(session, path, timeout, password)
    if RE_NEEDS_PASSWORD.search(out) and password:
        report.say("fpga", "sudo wants a password -- retrying with it on stdin")
        rc, out = _run_fpgautil(session, path, timeout, password,
                                use_stdin_pw=True)

    for line in out.splitlines():
        if line.strip():
            report.say("board", line.rstrip())

    if SUCCESS_MARKER not in out:
        if RE_NEEDS_PASSWORD.search(out):
            raise FpgaError(
                "sudo on the board requires a password that was not "
                "accepted. Give the login password, or grant the login user "
                "passwordless sudo for %s" % FPGAUTIL)
        if "not found" in out or rc == 127:
            raise FpgaError(
                "%s is not installed on this board. It comes from the "
                "fpga-manager-script recipe, which PetaLinux only stages "
                "when CONFIG_SUBSYSTEM_FPGA_MANAGER is set" % FPGAUTIL)
        detail = out.strip() or "(no output at all)"
        raise FpgaError(
            "fpgautil did not confirm the load (exit status %d). Expected a "
            "%r line. Got: %s" % (rc, SUCCESS_MARKER, detail))

    millis = None
    m = RE_TIMING.search(out)
    if m:
        millis = float(m.group(1))
    result = LoadResult(bitstream=path, millis=millis, output=out)
    report.say("fpga", "loaded %s%s" % (
        path, " in %.0f ms" % millis if millis is not None else ""))
    if warn_pl_replaced:
        report.warn(
            "the PL was replaced -- every AXI GPIO's data register is back "
            "to zero, so NCO/CIC/frame_len are reprogrammed by the next "
            "arm, and any /dev/mem mapping taken before now points at "
            "different hardware")
    return result


def load_sequence(session, steps, password=None, timeout=LOAD_TIMEOUT_DEFAULT):
    """Load each (name, path) in `steps` in order -- normally bootstrap
    then the requested mode, from Board.load_sequence(). Returns the list of
    LoadResults; raises FpgaError at the first failure.

    Stopping at the first failure is the only safe thing to do: after a
    failed load the PL is in an unknown state, and layering another full
    reconfiguration on top of that would destroy the evidence of what went
    wrong. When earlier steps DID succeed, the error says so -- "nothing
    happened" and "the board is now running the base design but not the
    mode you asked for" are very different situations to walk back into.
    """
    results = []
    total = len(steps)
    for i, (name, path) in enumerate(steps):
        last = (i == total - 1)
        if total > 1:
            report.say("fpga", "step %d/%d: %s" % (i + 1, total, name))
        try:
            results.append(load_bitstream(
                session, path, password=password, timeout=timeout,
                warn_pl_replaced=last))
        except FpgaError as exc:
            done = ", ".join(n for n, _ in steps[:i])
            if done:
                raise FpgaError(
                    "%s -- this was step %d of %d, so the board is now "
                    "running %s and NOT %s"
                    % (exc, i + 1, total, done, name))
            raise
    return results
