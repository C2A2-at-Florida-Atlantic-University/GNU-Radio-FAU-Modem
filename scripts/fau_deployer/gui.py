#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2026 CAAI.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""fau-deploy-gui: interactive front-end for the FAU flowgraph deployer.

Run from the repo root:

    python3 -m scripts.fau_deployer.gui

Same core as cli.py -- this module contains no protocol, no serial handling
and no board knowledge. It owns exactly three things the CLI does not need:

1. **A worker thread.** Every core operation blocks for seconds to minutes
   on a serial console. Tk's event loop must never block, so all of it runs
   on one worker thread that solely owns the transport/session; the UI
   thread touches neither and communicates only through two queues.
   ONE worker, not a pool: there is one console and it cannot be shared.

2. **A state machine.** The console is a single exclusive resource, so
   "deploy while the flowgraph is running" or "reload the PL while DMA is
   live" have to be impossible rather than merely discouraged -- the second
   of those can wedge the board (see core/fpga.py). Every button's enabled
   state is derived from one state variable in _set_state(), so there is no
   way to add a control that forgets a case.

3. **A log pane.** core/report.py's output is redirected into it with
   report.set_sink() so board output, progress and diagnostics land
   somewhere visible instead of on a stdout nobody is looking at.

Tkinter rather than Qt or GTK: the deployer is stdlib-only apart from
pyserial (stdlib unittest, bare print, no logging module), and a plain form
like this one is not what a heavier toolkit buys you.

Process (.grc -> preflight -> headless transform -> grcc) is a visible
disabled placeholder: none of that exists in core yet. See
docs/plans/grc-deployer-plan.md.
"""

import glob
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import boards as boards_mod, bootstrap, fpga
from .core import params as params_mod, payload, report
from .core.boards import BoardsError
from .core.fpga import FpgaError
from .core.params import ParamError
from .core.payload import PayloadError
from .core.protocol import CHUNK_B64_DEFAULT, DEST_DEFAULT, IDLE_TIMEOUT_DEFAULT
from .core.runner import Runner
from .core.sender import Sender, TransferError
from .core.session import BoardSession
from .core.transport import LineReader, SerialTransport

# LoginError/BootstrapError/TransportError are deliberately NOT imported to
# be caught individually: _Worker.run()'s blanket handler reports any
# exception as "<Type>: <message>", and these all carry messages already
# written to be shown to an operator as-is. Adding per-type handlers here
# would only restate them.

_HERE = os.path.dirname(os.path.abspath(__file__))
RECEIVER_PATH = os.path.join(_HERE, "board", "receiver.py")

TITLE = "FAU Modem GNU Radio Flowgraph Deployment Tool"
ETA_CONFIRM_SECONDS = 60.0
LOG_MAX_LINES = 5000

# --- states -----------------------------------------------------------------
# WEDGED is sticky and deliberately has no path back except reconnecting:
# it means a flowgraph was sent Ctrl-C and never confirmed it stopped, so
# the board may still have DMA live and NOTHING else may touch it. See
# core/runner.py's terminate() for why there is no "force kill" way out.
IDLE = "idle"            # connected, console ours, nothing running
OFFLINE = "offline"      # no session open
BUSY = "busy"            # a short operation holds the console
DEPLOYING = "deploying"  # cancellable transfer
RUNNING = "running"      # flowgraph live on the console
STOPPING = "stopping"    # Ctrl-C sent, waiting for the halt to confirm
WEDGED = "wedged"


class _Worker(threading.Thread):
    """Owns the transport, the session and every blocking core call.

    Jobs are submitted as callables and run strictly one at a time, which is
    the whole concurrency model: the console is one resource, so a queue of
    one worker is not a simplification of something better, it is the only
    correct shape.
    """

    def __init__(self, events):
        super().__init__(daemon=True)
        self._jobs = queue.Queue()
        self._events = events
        self._stop_flag = threading.Event()
        self.transport = None
        self.reader = None
        self.session = None
        self.settings = {}

    # -- called from the UI thread --
    def submit(self, name, fn, settings):
        """Queue a job together with the settings it should run against.

        The settings ride along rather than being assigned to the worker
        from the UI thread: buttons are disabled while a job runs, so there
        is no real race today, but "the UI thread mutates state the worker
        reads" is the kind of thing that becomes one the moment somebody
        adds a control that stays live. Handing them over with the job
        means the worker only ever reads its own thread's writes.
        """
        self._stop_flag.clear()
        self._jobs.put((name, fn, settings))

    def request_stop(self):
        """Ask the running job to wind up. Meaning is per-job: cancel a
        transfer, Ctrl-C a flowgraph."""
        self._stop_flag.set()

    @property
    def stop_requested(self):
        return self._stop_flag.is_set()

    def shutdown(self):
        self._jobs.put(None)

    # -- worker side --
    def emit(self, *event):
        """Post an event to the UI thread. Public because the job functions
        (App._job_*) run ON this thread and this is their only legal way to
        say anything -- they must never touch a widget."""
        self._events.put(event)

    def run(self):
        # Process-global, set once here: core is only ever driven from this
        # thread, so there is nothing to make thread-local.
        report.set_sink(lambda text, is_err: self.emit("log", text, is_err))
        while True:
            job = self._jobs.get()
            if job is None:
                break
            name, fn, settings = job
            if settings is not None:
                self.settings = settings
            try:
                fn(self)
            except Exception as exc:  # noqa: BLE001 -- see below
                # A job is one operator action; an exception in it must
                # become a message, never a dead worker. If this thread
                # died the UI would keep looking alive with every button
                # permanently greyed and no explanation.
                self.emit("failed", name,
                          "%s: %s" % (type(exc).__name__, exc))
            finally:
                self.emit("job_done", name)
        self._close()

    def ensure_session(self):
        """Open the port and log in, unless that has already happened. The
        session is held across operations on purpose -- the console is
        stateful and re-logging in per action would cost a Ctrl-D/login
        round trip every time."""
        if self.session is not None:
            return self.session
        s = self.settings
        report.banner("BOARD")
        report.kv("port", s["port"])
        report.kv("baud", s["baud"])
        self.transport = SerialTransport(s["port"], s["baud"])
        self.reader = LineReader(self.transport)
        self.session = BoardSession(
            self.transport, self.reader, user=s["user"],
            password=s["password"], verbose=s["verbose"])
        self.session.connect()
        report.say("gui", "logged in as %s" % s["user"])
        self.emit("connected", s["port"])
        return self.session

    def _close(self):
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
        self.transport = self.reader = self.session = None

    def disconnect(self):
        self._close()
        self.emit("disconnected")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(TITLE)
        self.root.minsize(760, 560)

        self.events = queue.Queue()
        self.worker = _Worker(self.events)
        self.worker.start()

        self.state = OFFLINE
        self.connected = False
        self.bitstreams = None
        self.bitstreams_error = None
        self.credentials = {}
        # Per-session credential overrides, keyed by board name. Never
        # written to disk -- credentials.json is for anything durable.
        self.cred_overrides = {}

        self.var_path = tk.StringVar()
        self.var_params = tk.StringVar()
        self.var_board = tk.StringVar()
        self.var_mode = tk.StringVar()
        self.var_port_override = tk.StringVar(value="")
        self.var_baud_override = tk.StringVar(value="")
        self.var_progress = tk.DoubleVar(value=0.0)
        self.var_progress_text = tk.StringVar(value="idle")
        self.var_status = tk.StringVar(value="")
        self.var_verbose = tk.BooleanVar(value=False)
        # On by default: the blocks open /dev/mem and lock under /run/lock,
        # so a non-root run dies in the block constructor.
        self.var_sudo = tk.BooleanVar(value=True)

        self._build_menus()
        self._build_body()
        self._load_bitstreams(initial=True)
        self._set_state(OFFLINE)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._drain_events)

    # ---------------------------------------------------------------- menus
    def _build_menus(self):
        bar = tk.Menu(self.root)

        self.menu_boards = tk.Menu(bar, tearoff=0)
        bar.add_cascade(label="Boards", menu=self.menu_boards)

        self.menu_mode = tk.Menu(bar, tearoff=0)
        bar.add_cascade(label="Mode", menu=self.menu_mode)

        self.menu_port = tk.Menu(bar, tearoff=0)
        bar.add_cascade(label="Port", menu=self.menu_port)

        about = tk.Menu(bar, tearoff=0)
        about.add_command(label="About...", command=self._show_about)
        bar.add_cascade(label="About", menu=about)

        self.root.config(menu=bar)

    def _rebuild_boards_menu(self):
        m = self.menu_boards
        m.delete(0, "end")
        if self.bitstreams is None:
            m.add_command(label="(bitstreams.json failed to load)",
                          state="disabled")
        else:
            for name in self.bitstreams.names:
                m.add_radiobutton(label=name, value=name,
                                  variable=self.var_board,
                                  command=self._on_board_changed)
        m.add_separator()
        m.add_command(label="Credentials...", command=self._edit_credentials)
        m.add_command(label="Reload bitstreams.json",
                      command=self._load_bitstreams)
        m.add_separator()
        m.add_checkbutton(label="Run flowgraph with sudo",
                          variable=self.var_sudo)
        m.add_checkbutton(label="Verbose session logging",
                          variable=self.var_verbose)
        m.add_command(label="Disconnect", command=self._do_disconnect)

    def _rebuild_mode_menu(self):
        m = self.menu_mode
        m.delete(0, "end")
        board = self.current_board
        if board is None:
            m.add_command(label="(no board selected)", state="disabled")
            return
        for name in board.modes:
            m.add_radiobutton(label=name, value=name, variable=self.var_mode,
                              command=self._on_mode_changed)
        m.add_separator()
        m.add_command(label="Load this bitstream",
                      command=self._do_load_bitstream)

    def _rebuild_port_menu(self):
        m = self.menu_port
        m.delete(0, "end")
        m.add_radiobutton(label="Use the selected board's port", value="",
                          variable=self.var_port_override,
                          command=self._refresh_status)
        found = self._detect_ports()
        if found:
            m.add_separator()
            for dev in found:
                m.add_radiobutton(label=dev, value=dev,
                                  variable=self.var_port_override,
                                  command=self._refresh_status)
        m.add_separator()
        m.add_command(label="Rescan for serial devices",
                      command=self._rebuild_port_menu)
        m.add_command(label="Set baud...", command=self._edit_baud)

    @staticmethod
    def _detect_ports():
        """Prefer /dev/serial/by-id: ttyUSB numbering is assignment order,
        so it swaps between two boards on a replug and silently points a
        deploy at the wrong one."""
        by_id = sorted(glob.glob("/dev/serial/by-id/*"))
        raw = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        return by_id + raw

    # ----------------------------------------------------------------- body
    def _build_body(self):
        pad = {"padx": 8, "pady": 4}
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        # -- target flowgraph
        row = ttk.Frame(outer)
        row.grid(row=0, column=0, sticky="ew", **pad)
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text="Flowgraph").grid(row=0, column=0, sticky="w")
        self.entry_path = ttk.Entry(row, textvariable=self.var_path)
        self.entry_path.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        self.btn_browse = ttk.Button(row, text="Browse", width=12,
                                     command=self._browse)
        self.btn_browse.grid(row=0, column=2)

        # -- params
        row = ttk.Frame(outer)
        row.grid(row=1, column=0, sticky="ew", **pad)
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text="Params").grid(row=0, column=0, sticky="w")
        self.entry_params = ttk.Entry(row, textvariable=self.var_params)
        self.entry_params.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        # -- action buttons
        row = ttk.Frame(outer)
        row.grid(row=2, column=0, sticky="ew", **pad)
        for i in range(4):
            row.columnconfigure(i, weight=1)
        self.btn_process = ttk.Button(row, text="Process", state="disabled",
                                      command=self._process_placeholder)
        self.btn_process.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_deploy = ttk.Button(row, text="Deploy", command=self._on_deploy)
        self.btn_deploy.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_run = ttk.Button(row, text="Run", command=self._on_run)
        self.btn_run.grid(row=0, column=2, sticky="ew", padx=4)
        self.btn_bitstream = ttk.Button(row, text="Load Bitstream",
                                        command=self._do_load_bitstream)
        self.btn_bitstream.grid(row=0, column=3, sticky="ew", padx=(4, 0))

        # -- progress
        box = ttk.LabelFrame(outer, text="Deployment progress")
        box.grid(row=3, column=0, sticky="ew", **pad)
        box.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(box, variable=self.var_progress,
                                        maximum=100.0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        ttk.Label(box, textvariable=self.var_progress_text).grid(
            row=1, column=0, sticky="w", padx=8, pady=(0, 6))

        # -- log
        box = ttk.LabelFrame(outer, text="Log")
        box.grid(row=4, column=0, sticky="nsew", **pad)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.log = tk.Text(box, height=18, wrap="none", state="disabled",
                           font=("TkFixedFont",))
        self.log.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar = ttk.Scrollbar(box, orient="horizontal", command=self.log.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.log.config(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.log.tag_config("err", foreground="#b00020")
        self.log.tag_config("banner", foreground="#555555")
        self.log.tag_config("gui", foreground="#00518f")

        ttk.Label(outer, textvariable=self.var_status, anchor="w",
                  relief="sunken").grid(row=5, column=0, sticky="ew")

    # -------------------------------------------------------- device file
    def _load_bitstreams(self, initial=False):
        try:
            self.bitstreams = boards_mod.load_bitstreams()
            self.credentials = boards_mod.load_credentials()
            self.bitstreams_error = None
        except BoardsError as exc:
            self.bitstreams = None
            self.bitstreams_error = str(exc)
            self._log("cannot load the bitstream file: %s" % exc, True)
        self._rebuild_boards_menu()
        if self.bitstreams is not None and self.bitstreams.names:
            if self.var_board.get() not in self.bitstreams.names:
                self.var_board.set(self.bitstreams.names[0])
            self._on_board_changed()
        else:
            self._rebuild_mode_menu()
            self._rebuild_port_menu()
            self._refresh_status()
        if initial and self.bitstreams is not None:
            self._log("[gui] bitstreams: %s" % self.bitstreams.path)
            self._log("[gui] credentials: %s" % (
                boards_mod.CREDENTIALS_PATH_DEFAULT if self.credentials
                else "none found -- using the documented %s/%s (Boards > "
                     "Credentials to override for this session)"
                     % (boards_mod.USER_DEFAULT, boards_mod.PASSWORD_DEFAULT)))
            self._log("[gui] pick a serial port from the Port menu -- port "
                      "assignments are not static, so none is recorded")

    @property
    def current_board(self):
        if self.bitstreams is None:
            return None
        try:
            return self.bitstreams.board(self.var_board.get())
        except BoardsError:
            return None

    @property
    def current_mode(self):
        """The selected bitstream name, or None."""
        board = self.current_board
        if board is None:
            return None
        name = self.var_mode.get()
        if name and name in board.bitstreams:
            return name
        return board.default_mode

    @property
    def current_bitstream(self):
        """The board-side path for the selected mode, or None."""
        board = self.current_board
        mode = self.current_mode
        return board.bitstreams.get(mode) if board and mode else None

    @property
    def current_sequence(self):
        """What Load Bitstream would actually do: the ordered
        (name, path) steps, normally bootstrap then the selected mode."""
        board = self.current_board
        mode = self.current_mode
        if board is None or mode is None:
            return []
        try:
            return board.load_sequence(mode)
        except BoardsError:
            return []

    @property
    def current_creds(self):
        """Per-session override if the operator set one, else
        credentials.json, else the documented defaults."""
        role = self.var_board.get()
        if role in self.cred_overrides:
            return self.cred_overrides[role]
        return boards_mod.creds_for(self.credentials, role)

    def _on_board_changed(self):
        board = self.current_board
        if board is not None:
            names = board.modes
            if self.var_mode.get() not in names:
                self.var_mode.set(names[0] if names else "")
        self._rebuild_mode_menu()
        self._rebuild_port_menu()
        if self.worker.session is not None:
            self._log("board selection changed -- Disconnect (Boards menu) "
                      "to move the open session to it", True)
        self._refresh_status()

    def _on_mode_changed(self):
        self._refresh_status()
        self._set_state(self.state)

    def _effective_port(self):
        """The port to open, or "" when nobody has chosen one.

        The ONLY source is the Port menu. Nothing on disk records a port and
        nothing infers one from the board: assignments are not static, and a
        guessed port is how a deploy lands silently on the wrong board.
        """
        return self.var_port_override.get() or ""

    def _effective_baud(self):
        override = self.var_baud_override.get()
        if override:
            try:
                return int(override)
            except ValueError:
                pass
        return boards_mod.BAUD_DEFAULT

    # --------------------------------------------------------------- dialogs
    def _browse(self):
        chosen = filedialog.askopenfilename(
            title="Select a generated flowgraph",
            filetypes=[("Generated flowgraph", "*.py"),
                       ("GRC flowgraph (needs Process)", "*.grc"),
                       ("All files", "*")])
        if chosen:
            self.var_path.set(chosen)
            self._set_state(self.state)

    def _edit_credentials(self):
        role = self.var_board.get()
        current = self.current_creds
        win = tk.Toplevel(self.root)
        win.title("Credentials for %s" % (role or "board"))
        win.transient(self.root)
        win.grab_set()
        source = ("credentials.json" if role in self.credentials
                  else "defaults (no credentials.json)")
        ttk.Label(win, text="%s -- currently from %s" % (role, source)).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 6))
        ttk.Label(win, text="User").grid(row=1, column=0, sticky="w",
                                         padx=10, pady=4)
        user_var = tk.StringVar(value=current.user)
        ttk.Entry(win, textvariable=user_var, width=24).grid(
            row=1, column=1, sticky="ew", padx=(0, 10), pady=4)
        ttk.Label(win, text="Password").grid(row=2, column=0, sticky="w",
                                             padx=10, pady=4)
        pw_var = tk.StringVar(value=current.password)
        entry = ttk.Entry(win, textvariable=pw_var, show="*", width=24)
        entry.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=4)
        entry.focus_set()
        ttk.Label(win, text="Overrides this session only, for this board. "
                            "Put anything durable in credentials.json\n"
                            "(gitignored -- copy json/example_credentials.json).",
                  foreground="#555555", justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=10)

        def ok():
            self.cred_overrides[role] = boards_mod.BoardCreds(
                user=user_var.get(), password=pw_var.get())
            win.destroy()

        btns = ttk.Frame(win)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", padx=10, pady=10)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="OK", command=ok).pack(side="right", padx=6)
        win.bind("<Return>", lambda _e: ok())

    def _edit_baud(self):
        current = str(self._effective_baud())
        win = tk.Toplevel(self.root)
        win.title("Baud rate")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="Baud (blank = use the board's)").grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 2))
        var = tk.StringVar(value=self.var_baud_override.get() or current)
        ttk.Entry(win, textvariable=var, width=12).grid(
            row=1, column=0, sticky="w", padx=10, pady=6)

        def ok():
            text = var.get().strip()
            if text:
                try:
                    if int(text) <= 0:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("Baud rate",
                                         "%r is not a positive integer" % text,
                                         parent=win)
                    return
            self.var_baud_override.set(text)
            self._refresh_status()
            win.destroy()

        ttk.Button(win, text="OK", command=ok).grid(row=2, column=0,
                                                    sticky="e", padx=10,
                                                    pady=(0, 10))
        win.bind("<Return>", lambda _e: ok())

    def _show_about(self):
        board = self.current_board
        mode = self.current_mode
        lines = [
            TITLE,
            "",
            "Ships a generated GNU Radio flowgraph to a Zynq board over its",
            "serial console and runs it there. The FAU Modem blocks open",
            "/dev/mem and drive AXI DMA, so they can only execute on the",
            "board -- authoring and code generation happen on the desktop.",
            "",
            "Design record: docs/plans/grc-deployer-plan.md",
            "Hardware notes: CLAUDE.md",
            "",
            "Bitstreams: %s" % (self.bitstreams.path if self.bitstreams
                                else "(failed to load)"),
            "Board     : %s" % (board.name if board else "(none)"),
            "Mode      : %s" % (mode or "(none)"),
            "Receiver  : %s" % RECEIVER_PATH,
            "",
            "Process (.grc -> preflight -> headless transform -> grcc) is",
            "not implemented yet; feed it the generated .py for now.",
        ]
        messagebox.showinfo("About", "\n".join(lines), parent=self.root)

    def _process_placeholder(self):
        messagebox.showinfo(
            "Process",
            "Not implemented yet.\n\n"
            "This will ingest a .grc, run the preflight gates, strip the GUI "
            "blocks into a headless copy and call grcc to generate the .py.\n\n"
            "Until then, generate it yourself (grcc -o DIR flowgraph.grc) and "
            "select the resulting .py.",
            parent=self.root)

    # ----------------------------------------------------------------- state
    def _set_state(self, state):
        self.state = state
        have_path = bool(self.var_path.get().strip())
        have_port = bool(self._effective_port())
        mode = self.current_mode

        def on(widget, enabled):
            widget.config(state="normal" if enabled else "disabled")

        idle_like = state in (IDLE, OFFLINE)
        on(self.entry_path, idle_like)
        on(self.entry_params, state in (IDLE, OFFLINE, RUNNING))
        on(self.btn_browse, idle_like)

        # Deploy doubles as Cancel mid-transfer -- one button, because the
        # two are never both meaningful.
        if state == DEPLOYING:
            self.btn_deploy.config(text="Cancel", state="normal")
        else:
            self.btn_deploy.config(
                text="Deploy",
                state="normal" if (idle_like and have_path and have_port)
                else "disabled")

        if state == RUNNING:
            self.btn_run.config(text="Terminate", state="normal")
        elif state == STOPPING:
            self.btn_run.config(text="Stopping...", state="disabled")
        else:
            self.btn_run.config(
                text="Run",
                state="normal" if (idle_like and have_path and have_port)
                else "disabled")

        on(self.btn_bitstream, idle_like and have_port and mode is not None)
        self._refresh_status()

    def _short_port(self):
        """A by-id path is the right thing to OPEN (stable across replugs)
        and much too long to display -- resolve it to the tty it currently
        points at, which is what an operator recognises."""
        port = self._effective_port()
        if not port:
            return "-"
        try:
            return os.path.basename(os.path.realpath(port))
        except OSError:
            return os.path.basename(port)

    def _refresh_status(self):
        board = self.current_board
        mode = self.current_mode
        bits = [
            "state: %s" % self.state,
            "board: %s" % (board.name if board else "-"),
            "mode: %s" % (mode or "-"),
            "port: %s" % self._short_port(),
            "%d baud" % self._effective_baud(),
        ]
        self.var_status.set("   |   ".join(bits))

    # ------------------------------------------------------------------- log
    def _log(self, text, is_err=False):
        tag = "err" if is_err else None
        if not is_err:
            if text.startswith("=" * 8):
                tag = "banner"
            elif text.startswith("[gui]"):
                tag = "gui"
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag)
        # Trim from the front rather than growing without bound: a long
        # session streaming flowgraph stdout would otherwise keep every line
        # forever.
        excess = int(self.log.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if excess > 0:
            self.log.delete("1.0", "%d.0" % (excess + 1))
        self.log.see("end")
        self.log.config(state="disabled")

    def _drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)

    def _handle_event(self, event):
        kind = event[0]
        if kind == "log":
            self._log(event[1], event[2])
        elif kind == "progress":
            acked, total, info = event[1], event[2], event[3]
            pct = (100.0 * acked / total) if total else 0.0
            self.var_progress.set(pct)
            self.var_progress_text.set(
                "chunk %d/%d   %3.0f%%   %.1f B/s   retransmits %d   naks %d"
                % (acked, total, pct, info["rate"], info["retransmits"],
                   info["naks"]))
        elif kind == "state":
            self._set_state(event[1])
        elif kind == "connected":
            self.connected = True
            self._set_state(IDLE)
        elif kind == "disconnected":
            self.connected = False
            self._set_state(OFFLINE)
        elif kind == "failed":
            self._log("  %s failed -- %s" % (event[1], event[2]), True)
        elif kind == "job_done":
            # THE state reset, and the only one. Every job ends here
            # whatever happened inside it, including an exception the
            # worker turned into a "failed" event -- so a job that dies in
            # ensure_session() cannot leave the UI stuck showing
            # "Terminate" with nothing running, which is exactly what an
            # earlier version did by having each job reset its own state on
            # its success path.
            #
            # WEDGED is the one state that must survive: it means a
            # flowgraph never confirmed it stopped, so nothing may touch
            # the board until the operator has looked at it.
            if self.state != WEDGED:
                self._set_state(IDLE if self.connected else OFFLINE)
        elif kind == "wedged":
            self._set_state(WEDGED)
            messagebox.showerror(
                "Board may be unsafe",
                "The flowgraph did not confirm it stopped.\n\n"
                "It may still be running with DMA live. This tool will not "
                "escalate to a kill: killing a process mid-DMA-burst orphans "
                "an AXI transaction, and enough of those wedge the board "
                "until it is power-cycled.\n\n"
                "Check the board's console by hand. Everything here stays "
                "disabled until you Disconnect (Boards menu).",
                parent=self.root)

    # -------------------------------------------------------------- actions
    def _settings(self):
        # No board lookup: bitstreams.json records nothing about how to
        # reach a board, so everything here comes from the Port menu, the
        # credentials for the selected board, or a constant.
        creds = self.current_creds
        return {
            "port": self._effective_port(),
            "baud": self._effective_baud(),
            "user": creds.user,
            "password": creds.password,
            "dest": DEST_DEFAULT,
            "sudo": bool(self.var_sudo.get()),
            "verbose": bool(self.var_verbose.get()),
        }

    def _guard(self):
        """Common preconditions for anything that talks to the board."""
        if self.state == WEDGED:
            messagebox.showerror(
                "Board may be unsafe",
                "A previous run never confirmed it stopped. Disconnect "
                "(Boards menu) once you have checked the board by hand.",
                parent=self.root)
            return False
        if not self._effective_port():
            messagebox.showerror(
                "No serial port",
                "Pick a serial port from the Port menu.\n\n"
                "Port assignments are not static, so nothing records or "
                "guesses one -- a stale port would deploy to the wrong "
                "board.",
                parent=self.root)
            return False
        return True

    def _on_deploy(self):
        if self.state == DEPLOYING:
            self.worker.request_stop()
            self._log("[gui] cancel requested -- finishing the current chunk")
            return
        if not self._guard():
            return

        path = self.var_path.get().strip()
        if path.endswith(".grc"):
            messagebox.showerror(
                "Not a generated flowgraph",
                "%s is a GRC source file. Generating the .py from it is the "
                "Process step, which is not implemented yet.\n\n"
                "Run `grcc -o DIR %s` and select the .py it writes."
                % (os.path.basename(path), os.path.basename(path)),
                parent=self.root)
            return

        # Payload assembly is local, fast and the only thing that can ask a
        # question, so it happens here on the UI thread -- the worker never
        # needs to prompt, which keeps it free of any Tk contact.
        settings = self._settings()
        try:
            entries, main_arc = payload.collect_files(path)
            pl = payload.build_payload(entries, main_arc,
                                       chunk_size=CHUNK_B64_DEFAULT)
        except PayloadError as exc:
            messagebox.showerror("Payload", str(exc), parent=self.root)
            return

        eta = payload.eta_seconds(pl, settings["baud"], 1)
        if eta > ETA_CONFIRM_SECONDS and not messagebox.askyesno(
                "Large payload",
                "This payload is %d chunks (%d compressed bytes) and should "
                "take about %.0f seconds at %d baud.\n\nGo ahead?"
                % (len(pl.chunks), pl.gz_bytes, eta, settings["baud"]),
                parent=self.root):
            return

        self.var_progress.set(0.0)
        self.var_progress_text.set("starting...")
        self._set_state(DEPLOYING)
        self.worker.submit("deploy", lambda w: self._job_deploy(w, pl),
                           settings)

    def _on_run(self):
        if self.state == RUNNING:
            self.worker.request_stop()
            self._set_state(STOPPING)
            return
        if not self._guard():
            return
        path = self.var_path.get().strip()
        try:
            tokens = params_mod.check(self.var_params.get(), path)
        except ParamError as exc:
            messagebox.showerror("Flowgraph params", str(exc), parent=self.root)
            return
        settings = self._settings()
        # Derived from the CURRENT path and the CURRENT board every time.
        # An earlier version cached the last successful deploy's
        # (dest, name) and preferred it, which meant selecting a different
        # flowgraph and pressing Run without deploying first silently ran
        # the previous one. Deploy derives dest the same way, so there was
        # nothing to cache anyway.
        main_name = os.path.basename(path)
        self._set_state(RUNNING)
        self.worker.submit(
            "run", lambda w: self._job_run(w, settings["dest"], main_name,
                                           tokens, settings["sudo"]),
            settings)

    def _do_load_bitstream(self):
        if self.state not in (IDLE, OFFLINE):
            messagebox.showerror(
                "Busy",
                "The console is busy. A bitstream load replaces the PL out "
                "from under anything using it, so it only runs with nothing "
                "else in flight.",
                parent=self.root)
            return
        if not self._guard():
            return
        board = self.current_board
        mode = self.current_mode
        steps = self.current_sequence
        if board is None or mode is None or not steps:
            messagebox.showerror(
                "No bitstream selected",
                "Pick a board and one of its bitstreams from the Boards and "
                "Mode menus. If the one you want is missing, add it to "
                "bitstreams.json.",
                parent=self.root)
            return

        # Spell the whole sequence out. Asking for 'tx' loading two
        # bitstreams is not something to discover afterwards in the log.
        warning = ""
        if mode != boards_mod.BOOTSTRAP_KEY and not board.has_bootstrap:
            warning = ('%s has no "%s" entry, so %s is being loaded on its '
                       "own. If this board needs a base design first, add "
                       "one to bitstreams.json."
                       % (board.name, boards_mod.BOOTSTRAP_KEY, mode))
        if not self._confirm_load(board, mode, steps, warning):
            return
        self._set_state(BUSY)
        self.worker.submit("load-bitstream",
                           lambda w: self._job_load_bitstream(w, steps),
                           self._settings())

    def _confirm_load(self, board, mode, steps, warning):
        """Modal confirmation for a bitstream load.

        A hand-built dialog rather than messagebox.askyesno: that wraps at a
        fixed narrow width and breaks mid-token, which turns a board-side
        path into "/home/petalinux/firmw are/Radio_Top...". The paths are
        the entire point of this dialog -- they are what the operator is
        checking before replacing the PL -- so they get a monospace block
        that is never wrapped.
        """
        win = tk.Toplevel(self.root)
        win.title("Load bitstream")
        win.transient(self.root)
        win.resizable(False, False)
        answer = {"ok": False}

        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, wraplength=560, justify="left",
                  text="Put %s in %s mode by loading %d bitstream%s, in "
                       "this order:" % (board.name, mode, len(steps),
                                        "" if len(steps) == 1 else "s")
                  ).pack(anchor="w")

        listing = ttk.Frame(body)
        listing.pack(anchor="w", pady=(10, 10), padx=(6, 0))
        for i, (name, path) in enumerate(steps):
            ttk.Label(listing, text="%d.  %s" % (i + 1, name),
                      font=("TkDefaultFont", 0, "bold")).grid(
                row=i * 2, column=0, sticky="w")
            ttk.Label(listing, text=path, font=("TkFixedFont",)).grid(
                row=i * 2 + 1, column=0, sticky="w", padx=(22, 0),
                pady=(0, 6))

        ttk.Label(body, wraplength=560, justify="left",
                  text="Each one replaces the PL. Every AXI GPIO returns to "
                       "zero, and any mapping taken before now points at "
                       "different hardware.").pack(anchor="w")

        if warning:
            ttk.Label(body, wraplength=560, justify="left", text=warning,
                      foreground="#b00020").pack(anchor="w", pady=(10, 0))

        btns = ttk.Frame(body)
        btns.pack(anchor="e", pady=(16, 0))

        def confirm():
            answer["ok"] = True
            win.destroy()

        load_btn = ttk.Button(btns, text="Load", command=confirm)
        load_btn.pack(side="right", padx=(6, 0))
        cancel = ttk.Button(btns, text="Cancel", command=win.destroy)
        cancel.pack(side="right")

        # Cancel takes focus, not Load: a stray Return on a dialog that
        # replaces the PL should do nothing.
        cancel.focus_set()
        win.bind("<Escape>", lambda _e: win.destroy())
        win.grab_set()
        self.root.wait_window(win)
        return answer["ok"]

    def _do_disconnect(self):
        if self.state in (RUNNING, STOPPING, DEPLOYING):
            messagebox.showerror(
                "Busy",
                "Stop what is running before disconnecting.",
                parent=self.root)
            return
        self.worker.submit("disconnect", lambda w: w.disconnect(), None)

    # ----------------------------------------------------------------- jobs
    # All of these run ON THE WORKER THREAD. They may only touch core and
    # w.emit(); never a widget, never a Tk variable.
    def _job_deploy(self, w, pl):
        session = w.ensure_session()
        report.banner("PAYLOAD")
        for line in payload.format_manifest(pl):
            report.say("payload", line)
        report.kv("chunks", len(pl.chunks))
        report.kv("sha256", pl.sha256)

        bootstrap.ensure_receiver(
            session, w.transport, w.reader, RECEIVER_PATH,
            dest=w.settings["dest"], idle_timeout=IDLE_TIMEOUT_DEFAULT,
            chunk_size=CHUNK_B64_DEFAULT)

        report.banner("TRANSFER")
        sender = Sender(
            w.transport, w.reader, pl, w.settings["dest"],
            progress=False,
            on_progress=lambda acked, total, info: w.emit(
                "progress", acked, total, info),
            should_stop=lambda: w.stop_requested)
        try:
            stats = sender.send()
        except TransferError as exc:
            report.error(str(exc))
            return
        finally:
            # Hand the console back either way: on success there is nothing
            # left to keep the receiver alive for, and on failure it would
            # otherwise sit holding the console until its idle timeout.
            if not session.interrupt_and_wait(5.0):
                report.warn("the receiver did not release the console within "
                            "5s of Ctrl-C -- it will time out on its own")

        report.banner("DONE")
        report.kv("chunks sent", stats.chunks)
        report.kv("retransmits", stats.retransmits)
        report.kv("elapsed", "%.1fs" % stats.elapsed)
        report.say("gui", "deployed to %s" % w.settings["dest"])

    def _job_run(self, w, dest, main_name, tokens, sudo):
        session = w.ensure_session()
        r = Runner(session, w.transport, w.reader, dest, main_name,
                   params=tokens, sudo=sudo)
        report.banner("RUN")
        report.say("run", r.command)
        result = r.run(on_line=lambda line: report.say("board", line),
                       should_stop=lambda: w.stop_requested)
        report.banner("RUN FINISHED")
        report.kv("exit status", result.rc)
        report.kv("stopped by operator", result.terminated)
        report.kv("output lines", result.lines)
        report.kv("elapsed", "%.1fs" % result.elapsed)
        if result.wedged:
            w.emit("wedged")

    def _job_load_bitstream(self, w, steps):
        session = w.ensure_session()
        report.banner("BITSTREAM")
        try:
            fpga.load_sequence(session, steps,
                               password=w.settings["password"])
        except FpgaError as exc:
            report.error(str(exc))

    # ----------------------------------------------------------------- close
    def _on_close(self):
        if self.state in (RUNNING, STOPPING):
            messagebox.showwarning(
                "Flowgraph still running",
                "A flowgraph is still running on the board. Terminate it "
                "first so it tears the DMA down cleanly -- closing this "
                "window would leave it running with nobody watching it.",
                parent=self.root)
            return
        if self.state == DEPLOYING and not messagebox.askyesno(
                "Transfer in progress",
                "A transfer is in progress. Abandon it and quit?",
                parent=self.root):
            return
        self.worker.shutdown()
        self.root.destroy()


def main(argv=None):
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
