#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""FAU GRC deployer: ship a GNU Radio flowgraph from the desktop to a Zynq
board over its serial console, run it there, and load the board's
pre-staged PL bitstream.

Two front-ends over one core:

    python3 -m scripts.fau_deployer.cli --help    # headless, dev/CI
    python3 -m scripts.fau_deployer.gui           # interactive

Input is a `.grc` or an already-generated `.py`. A `.grc` goes through the
Process phase first -- preflight gates, headless transform, grcc -- and is
re-processed whenever it changes on disk, so what reaches the board is
always generated from the flowgraph as it stands now rather than from the
save before last. Only that phase needs GNU Radio installed on the desktop;
deploying a `.py` needs nothing but pyserial.

A flowgraph's **QT GUI input blocks** (Range, Entry, Chooser, Check Box,
Push Button) survive going headless as live controls: the transform records
them in a `ui_spec.json` before freezing each to a plain `variable`, ships
that and `board/fau_ctl.py` alongside the generated `.py`, and the deployer
renders them as widgets that drive the running flowgraph over the same
console. `--list-controls` prints them; `--set id=value` applies one at
launch. QT GUI *sinks* are permanently out of scope -- a time sink at
400 ksps is ~3.2 MB/s against a console that carries ~11.5 KB/s.

See docs/plans/grc-deployer-plan.md for the full design and every decision's
reasoning, and scripts/fau_deployer/tests/ for the harness that exercises
all of it -- protocol, login/grounding, run and bitstream load -- against
real ptys and real subprocesses, with no board attached.
"""
