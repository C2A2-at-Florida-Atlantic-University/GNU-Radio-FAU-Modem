#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""The "Flowgraph Params" field: turn one line of operator-typed text into
an argv list, and check it against the options the flowgraph actually has.

Why argv and not a shell string: the text ends up inside a command sent to
the board's shell, so `--label "two words"` has to survive as ONE argument
and a stray `$`, `*`, `;` or quote must not be reinterpreted there. Splitting
with shlex here and re-quoting per token in core/runner.py is what makes
that true. This is not a security boundary -- the operator types it on their
own machine -- it is that silent argument mangling is a genuinely nasty
thing to debug through a serial console.

Why validate: a wrong flag costs a full deploy-and-run round trip to
discover, and argparse's own error goes to the board's stderr where it is
tangled up with console noise. A no_gui flowgraph's options come from its
GRC *Parameter* blocks and land in the generated .py as add_argument()
calls, which is cheap to read back with ast -- the same trick
core/payload.py already uses on the same file to find its sibling imports.
"""

import ast
import shlex


class ParamError(ValueError):
    """Bad params text. Message is meant to be shown to the operator as-is."""


def split_params(text):
    """Split the field into argv tokens. Raises ParamError on unbalanced
    quotes rather than letting shlex's bare ValueError escape."""
    if text is None:
        return []
    text = text.strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError as exc:
        raise ParamError(
            "could not parse the flowgraph params %r: %s (check for an "
            "unbalanced quote)" % (text, exc))


def flowgraph_options(path):
    """The set of option strings the flowgraph accepts, read out of its
    argparse setup with ast.

    Returns None -- meaning "unknown, do not validate against this" --
    rather than an empty set when the file cannot be parsed, or when it
    builds its parser in a way this cannot read statically. An empty set is
    a real answer (a flowgraph with no Parameter blocks takes no options)
    and must stay distinguishable from "no idea", or validation would
    reject every param against a flowgraph it simply failed to read.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None

    opts = set()
    saw_parser = False
    for node in ast.walk(tree):
        # An argparse IMPORT is enough to call this a generated flowgraph,
        # because grcc emits `from argparse import ArgumentParser`
        # unconditionally and then only builds a parser if the flowgraph has
        # GRC Parameter blocks. So "imports argparse, never calls
        # add_argument" is not an unreadable file -- it is the very common
        # case of a flowgraph that takes NO options, and it has to come back
        # as an empty set rather than None. It is also the case most worth
        # catching: with no parser built, the flowgraph ignores extra argv
        # in silence, so a param typed for it does nothing at all and says
        # nothing about it.
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "argparse" for a in node.names):
                saw_parser = True
            continue
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "argparse":
                saw_parser = True
            continue
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name in ("ArgumentParser", "OptionParser"):
            saw_parser = True
            continue
        if name != "add_argument":
            continue
        saw_parser = True
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.startswith("-"):
                    opts.add(arg.value)
    if not saw_parser:
        # No parser at all in a generated flowgraph is more likely "this
        # isn't the file I think it is" than "it takes no options".
        return None
    return opts


def unknown_flags(tokens, known):
    """Which `--flag` tokens are not in `known`. Empty when `known` is None
    (unknown option set -- see flowgraph_options).

    Only leading-dash tokens are checked; a bare word is a positional or a
    value belonging to the flag before it, and this has no way to tell those
    apart without the flowgraph's actual parser. `--flag=value` is checked
    on the part before the '='. A lone `--` ends option parsing, so
    everything after it is left alone.
    """
    if known is None:
        return []
    bad = []
    for tok in tokens:
        if tok == "--":
            break
        if not tok.startswith("-") or tok == "-":
            continue
        name = tok.split("=", 1)[0]
        if name not in known:
            bad.append(name)
    return bad


def check(text, flowgraph_path=None):
    """Split `text` and, when the flowgraph's options can be read, verify
    every flag against them. Returns the argv token list. Raises ParamError
    with the legal option list included, since "unrecognized" is only
    actionable next to what WOULD be recognized.
    """
    tokens = split_params(text)
    if not tokens or flowgraph_path is None:
        return tokens
    known = flowgraph_options(flowgraph_path)
    bad = unknown_flags(tokens, known)
    if bad:
        legal = ", ".join(sorted(known)) if known else "(none -- this "\
            "flowgraph declares no GRC Parameter blocks)"
        raise ParamError(
            "%s does not accept %s. It accepts: %s"
            % (flowgraph_path, ", ".join(bad), legal))
    return tokens
