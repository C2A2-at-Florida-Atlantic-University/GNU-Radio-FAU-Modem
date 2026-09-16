#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Core, transport-agnostic pieces of the FAU deployer, shared by both
front-ends:

    report      console/GUI output, with a redirectable sink
    grcfile     reading a .grc as YAML, and the preflight gates
    headless    stripping the GUI blocks onto a derived headless copy
    generate    the Process phase: .grc -> preflight -> transform -> grcc
    watch       noticing the flowgraph on disk changed under us
    boards      the boards and their loadable bitstreams (bitstreams.json),
                plus their optional logins (credentials.json)
    payload     file collection, tar+gzip, chunking, hashing
    protocol    wire framing and sentinel parsing
    transport   the serial seam, and lines-vs-prompt reading
    session     login, Ctrl-D grounding, and running shell commands
    bootstrap   getting the board-side receiver installed and READY
    sender      the BEGIN/chunk/END send loop with retransmit
    params      the flowgraph params field: argv splitting and validation
    runner      running a flowgraph on the console, and stopping it safely
    fpga        loading a pre-staged bitstream with fpgautil

Nothing here knows about a GUI or a terminal; `report`'s sink is the only
place a front-end shows up.

Everything is stdlib-only apart from pyserial, with one exception:
`grcfile`/`headless` need PyYAML and `generate` shells out to `grcc`. Those
three are the Process phase, they are imported lazily where it matters, and
a desktop that can compile a .grc at all necessarily has both -- deploying
an already-generated .py never touches them.
"""
