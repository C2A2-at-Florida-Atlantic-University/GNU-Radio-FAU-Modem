#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""FAU GRC deployer: ship a generated GNU Radio flowgraph from the desktop
to a Zynq board over its serial console, run it there, and load the board's
pre-staged PL bitstream.

Two front-ends over one core:

    python3 -m scripts.fau_deployer.cli --help    # headless, dev/CI
    python3 -m scripts.fau_deployer.gui           # interactive

The `.grc` "Process" phase (preflight gates, headless transform, grcc) is
not built yet -- input is a generated `.py`.

See docs/plans/grc-deployer-plan.md for the full design and every decision's
reasoning, and scripts/fau_deployer/tests/ for the harness that exercises
all of it -- protocol, login/grounding, run and bitstream load -- against
real ptys and real subprocesses, with no board attached.
"""
