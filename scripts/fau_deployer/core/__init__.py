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
"""
