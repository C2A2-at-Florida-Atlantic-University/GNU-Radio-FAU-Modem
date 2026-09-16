#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""bitstreams.json: which boards there are, and the bitstream binaries each
one can be loaded with live. Plus credentials.json, the optional gitignored
companion holding their serial-console logins.

    {
        "S10": {
            "bootstrap": "/home/petalinux/firmware/Radio_Top_v2_wrapper.bit.bin",
            "tx":        "/home/petalinux/S10_dac.bit.bin",
            "rx":        "/home/petalinux/S10_adc.bit.bin"
        }
    }

That is the whole format: a board name, and named board-side paths under it.
**Nothing about how to REACH a board belongs here** -- no serial port, no
baud, no destination directory. Those are per-session facts an engineer sets
from the Port menu or `--port`; a port recorded in a file is a port that
goes stale and deploys to the wrong board. Login secrets are the same kind
of thing and live in credentials.json, which is gitignored.

Bitstream keys are free-form. `bootstrap`, `tx` and `rx` are the
conventional ones. Paths are board-side (absolute, or relative to the login
user's home -- run() never changes directory) and should be the
bootgen-processed `.bit.bin` form that fpgautil's FPGA-manager path wants,
not a raw Vivado `.bit`.

**`bootstrap` is loaded first.** It is the base design a board is brought up
on, so asking for `tx` or `rx` loads `bootstrap` and then that one --
see Board.load_sequence(), which is where that ordering lives.

A mode a board does not have is simply an absent key; there are no nulls
and no placeholders to keep in step.

Conventions kept from ~/repos/Unified-FAU-Modem-Test-Tooling, which is the
reference for how bench definitions are handled here: secrets in a separate
gitignored `credentials.json` with an `example_` copy to work from, JSON
Schemas under `json/schemas/` (draft 2020-12, `additionalProperties: false`
where the keys are fixed), and `--credentials` as the flag. Its role-keyed
`tx_board`/`rx_board` device layout does NOT apply here: a board name is the
identity, and one board can carry both a tx and an rx bitstream.
"""

import dataclasses
import json
import os
from typing import Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)

BITSTREAMS_PATH_DEFAULT = os.path.join(_PKG, "bitstreams.json")
CREDENTIALS_PATH_DEFAULT = os.path.join(_PKG, "credentials.json")
EXAMPLE_CREDENTIALS_PATH = os.path.join(_PKG, "json", "example_credentials.json")

# The base design a board is brought up on. Loaded FIRST whenever another
# mode is asked for -- see Board.load_sequence(). Also leads the mode list
# wherever modes are offered, so a menu reads in the order things happen.
BOOTSTRAP_KEY = "bootstrap"

BAUD_DEFAULT = 115200
USER_DEFAULT = "petalinux"
PASSWORD_DEFAULT = "1234"


class BoardsError(ValueError):
    """bitstreams.json or credentials.json is unusable. The message names
    the file and the exact key -- these are hand-edited, so it nearly
    always needs to say what to go fix."""


@dataclasses.dataclass
class Board:
    name: str
    bitstreams: Dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def modes(self) -> List[str]:
        """Loadable bitstream names, `bootstrap` first then the rest
        alphabetically -- a stable order, so a menu does not reshuffle
        depending on how somebody typed the file."""
        rest = sorted(k for k in self.bitstreams if k != BOOTSTRAP_KEY)
        return ([BOOTSTRAP_KEY] if BOOTSTRAP_KEY in self.bitstreams else []) + rest

    def bitstream(self, mode):
        try:
            return self.bitstreams[mode]
        except KeyError:
            raise BoardsError(
                "board %r has no %r bitstream -- it has: %s"
                % (self.name, mode, ", ".join(self.modes) or "(none)"))

    @property
    def has_bootstrap(self):
        return BOOTSTRAP_KEY in self.bitstreams

    def load_sequence(self, mode):
        """The bitstreams to load, IN ORDER, to put this board in `mode`.
        Returns a list of (name, board-side path).

        `bootstrap` is the base design a board is brought up on, so asking
        for anything else yields two steps: bootstrap, then the mode. Asking
        for bootstrap itself is one step -- it is the destination, not a
        prelude to itself.

        A board with no bootstrap entry yields just the mode. That is left
        to the caller to notice and warn about rather than being an error
        here: bitstream keys are free-form and absence is how the format
        says "this board has no such thing", so a board whose design really
        is self-contained must stay expressible.

        Deduped by PATH, not by name: if bootstrap and the mode point at the
        same file, loading it twice would be a second full PL
        reconfiguration for no change.
        """
        target = (mode, self.bitstream(mode))  # raises if unknown
        if mode == BOOTSTRAP_KEY or not self.has_bootstrap:
            return [target]
        boot = (BOOTSTRAP_KEY, self.bitstreams[BOOTSTRAP_KEY])
        if boot[1] == target[1]:
            return [boot]
        return [boot, target]

    @property
    def default_mode(self):
        """The only mode, when there is one -- nothing to choose, so a
        front-end should not ask. None when there are several."""
        modes = self.modes
        return modes[0] if len(modes) == 1 else None


@dataclasses.dataclass
class BoardCreds:
    user: str = USER_DEFAULT
    password: str = PASSWORD_DEFAULT


@dataclasses.dataclass
class BitstreamFile:
    path: str
    boards: Dict[str, Board] = dataclasses.field(default_factory=dict)

    @property
    def names(self):
        """Board names in FILE order, not sorted: the order they are written
        in is the order they are offered, so the first one listed is the
        default selection."""
        return list(self.boards)

    def board(self, name):
        try:
            return self.boards[name]
        except KeyError:
            raise BoardsError(
                "no board named %r in %s -- it defines: %s"
                % (name, self.path, ", ".join(self.names) or "(none)"))


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        try:
            raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise BoardsError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(raw, dict):
        raise BoardsError("%s: top level must be a JSON object" % path)
    return raw


def load_bitstreams(path=None):
    """Read and validate bitstreams.json. Raises BoardsError on anything
    wrong."""
    path = path or BITSTREAMS_PATH_DEFAULT
    try:
        raw = _load_json(path)
    except FileNotFoundError:
        raise BoardsError(
            "no bitstream file at %s -- it is tracked in the repo, so a "
            "missing one usually means the path is wrong rather than that "
            "it needs creating" % path)

    if not raw:
        raise BoardsError("%s defines no boards" % path)

    boards = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise BoardsError(
                "%s: board %r must be an object mapping a bitstream name to "
                "a board-side path, got %r" % (path, name, entry))
        if not entry:
            raise BoardsError(
                "%s: board %r lists no bitstreams -- a board with none can "
                "never be loaded" % (path, name))
        for mode, target in entry.items():
            if not isinstance(target, str) or not target:
                # No nulls and no placeholders: a mode a board does not have
                # is an absent key, so there is nothing to keep in step.
                raise BoardsError(
                    "%s: %s.%s must be a non-empty string (a board-side path "
                    "to a .bit.bin); omit the key entirely if this board has "
                    "no %s bitstream" % (path, name, mode, mode))
        boards[name] = Board(name=name, bitstreams=dict(entry))

    return BitstreamFile(path=path, boards=boards)


def load_credentials(path=None, required=False):
    """Read credentials.json: board name -> {user, password}.

    Returns a dict keyed the same way as bitstreams.json, or an EMPTY dict
    when the file is absent and `required` is false -- it is gitignored, so
    a fresh checkout has none and the tool has to work anyway, falling back
    to the documented petalinux/1234 and letting either front-end override
    per session. Absent is a normal state; malformed never is.
    """
    path = path or CREDENTIALS_PATH_DEFAULT
    try:
        raw = _load_json(path)
    except FileNotFoundError:
        if required:
            raise BoardsError(
                "no credentials file at %s -- copy %s and edit it"
                % (path, EXAMPLE_CREDENTIALS_PATH))
        return {}

    creds = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise BoardsError(
                "%s: %s must be an object with 'user' and 'password'"
                % (path, name))
        unknown = sorted(set(entry) - {"user", "password"})
        if unknown:
            # Catches being handed the reference tooling's own
            # credentials.json, which also carries ntfy and psu sections.
            raise BoardsError(
                "%s: %s has unknown field(s) %s -- allowed: password, user"
                % (path, name, ", ".join(repr(k) for k in unknown)))
        for field in ("user", "password"):
            if not isinstance(entry.get(field), str):
                raise BoardsError(
                    "%s: %s.%s must be a string" % (path, name, field))
        creds[name] = BoardCreds(user=entry["user"], password=entry["password"])
    return creds


def creds_for(creds, name):
    """The credentials for board `name`, or the documented defaults when
    credentials.json says nothing about it."""
    return creds.get(name, BoardCreds())
