#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Flowgraph documents for the Process-phase tests.

Built as dicts and written out with yaml rather than kept as text blobs: a
test that needs "the same flowgraph but with a second FAU block" should say
that, not re-indent forty lines of YAML. The shapes here are copied from
real files GRC wrote (GNU Radio 3.10), including the `states.state: true`
that GRC writes for most blocks where the schema suggests `enabled`.
"""

import os
import tempfile

try:
    import yaml
    HAVE_YAML = True
except ImportError:  # pragma: no cover
    HAVE_YAML = False


def states(x=8, y=8, state=True):
    return {"bus_sink": False, "bus_source": False, "bus_structure": None,
            "coordinate": [x, y], "rotation": 0, "state": state}


def options(fg_id="demo", generate_options="qt_gui", run_options="prompt"):
    return {
        "parameters": {
            "author": "bench", "catch_exceptions": "True",
            "category": "[GRC Hier Blocks]", "comment": "",
            "generate_options": generate_options, "id": fg_id,
            "output_language": "python", "run": "True",
            "run_options": run_options, "sizing_mode": "fixed",
            "title": "Demo",
        },
        "states": states(),
    }


def block(name, type_id, parameters=None, state=True):
    return {"name": name, "id": type_id, "parameters": dict(parameters or {}),
            "states": states(state=state)}


def fau_source(name="fau_rx"):
    return block(name, "fau_modem_fau_source", {
        "samp_rate": "200e3", "nco_freq": "120e3", "bd_samples": "8192",
        "num_bds": "16", "allow_unreserved": "False", "verbose": "False",
        "poll_timeout": "20.0"})


def fau_sink(name="fau_tx"):
    return block(name, "fau_modem_fau_sink", {
        "samp_rate": "200e3", "nco_freq": "120e3", "bd_samples": "8192",
        "num_bds": "16", "tx_scale": "1.0", "allow_unreserved": "False",
        "verbose": "False", "poll_timeout": "20.0"})


def time_sink(name="time_sink", type_name="complex"):
    return block(name, "qtgui_time_sink_x",
                 {"type": type_name, "name": '"Time"', "srate": "samp_rate",
                  "nconnections": "1", "gui_hint": ""})


def gui_range(name="gain", value="0.5"):
    return block(name, "variable_qtgui_range",
                 {"label": "Gain", "rangeType": "float", "value": value,
                  "start": "0", "stop": "1", "step": "0.01",
                  "widget": "counter_slider", "gui_hint": ""})


def msg_button(name="ping"):
    return block(name, "variable_qtgui_msg_push_button",
                 {"label": "Ping", "type": "string", "msgName": "pressed",
                  "value": '"go"', "gui_hint": ""})


def usrp_sink(name="usrp_tx", type_name="fc32", state=True):
    """A USRP sink as GRC writes it, trimmed to the parameters that matter
    here. The real block carries 32 copies of every per-channel parameter;
    none of them change what the transform does."""
    return block(name, "uhd_usrp_sink",
                 {"type": type_name, "dev_addr": '"addr=192.168.11.13"',
                  "nchan": "1", "samp_rate": "480e3",
                  "center_freq0": "center_freq", "gain0": "0"},
                 state=state)


def usrp_source(name="usrp_rx", type_name="fc32", state=True):
    return block(name, "uhd_usrp_source",
                 {"type": type_name, "dev_addr": '"addr=192.168.11.13"',
                  "nchan": "1", "samp_rate": "480e3",
                  "center_freq0": "center_freq", "gain0": "0"},
                 state=state)


def tx_with_usrp(fg_id="tx_demo", state=True):
    """The bench case this gate exists for: one chirp generator feeding the
    board's DAC *and* a USRP, so the same waveform goes out of both. The
    USRP half cannot follow the flowgraph onto the board."""
    return doc([
        block("samp_rate", "variable", {"value": "480e3"}),
        block("chirp", "analog_sig_source_x",
              {"type": "complex", "freq": "1e3", "amp": "0.5"}),
        fau_sink(),
        usrp_sink(state=state),
    ], [
        ("chirp", "0", "fau_tx", "0"),
        ("chirp", "0", "usrp_tx", "0"),
    ], fg_id=fg_id)


def doc(blocks, connections=(), fg_id="demo", file_format=1,
        generate_options="qt_gui", run_options="prompt"):
    d = {
        "options": options(fg_id, generate_options, run_options),
        "blocks": list(blocks),
        "connections": [list(c) for c in connections],
        "metadata": {"file_format": file_format, "grc_version": "3.10.1.1"},
    }
    return d


def rx_with_gui(fg_id="rx_demo"):
    """The canonical case: a source feeding one real sink and two GUI ones,
    a GUI variable, and a branch whose ONLY consumer is a GUI sink -- which
    is the branch that must come back with a null sink spliced on."""
    return doc([
        block("samp_rate", "variable", {"value": "200e3"}),
        gui_range("nco", "120e3"),
        block("tabs", "qtgui_tab_widget", {"num_tabs": "2"}),
        fau_source(),
        block("file_sink", "blocks_file_sink",
              {"type": "complex", "file": "/tmp/rx.iq", "vlen": "1"}),
        time_sink(),
        block("to_mag", "blocks_complex_to_mag", {"vlen": "1"}),
        block("num_sink", "qtgui_number_sink",
              {"type": "float", "nconnections": "1"}),
    ], [
        ("fau_rx", "0", "file_sink", "0"),
        ("fau_rx", "0", "time_sink", "0"),
        ("fau_rx", "0", "to_mag", "0"),
        ("to_mag", "0", "num_sink", "0"),
    ], fg_id=fg_id)


def write(document, directory=None, name=None):
    """Write `document` out as a .grc and return its path."""
    directory = directory or tempfile.mkdtemp(prefix="fau_grc_")
    name = name or (document["options"]["parameters"]["id"] + ".grc")
    path = os.path.join(directory, name)
    with open(path, "w") as fh:
        yaml.safe_dump(document, fh, default_flow_style=False, sort_keys=False)
    return path
