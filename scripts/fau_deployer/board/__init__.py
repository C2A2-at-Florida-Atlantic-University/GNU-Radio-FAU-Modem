#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Board-side deployer pieces.

receiver.py is deliberately standalone-runnable (`python3 receiver.py`) on
the board's bare Python 3.12 -- it must not depend on this package being
importable there. This __init__.py exists only so the desktop side can do
`from ..board.receiver import ...` (see core/protocol.py) to share the wire
constants without a second copy to drift.
"""
