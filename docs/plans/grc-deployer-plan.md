# Plan: FAU GRC Deployer (desktop → board over UART)

Status: **feature-complete as designed (2026-09-15): the whole pipeline
plus the live control channel.** Nothing has run against real hardware
yet, and the two things still unbuilt from the design below are **Gate 2**
(enumerating the board's actual block set) and serial auto-detect. Everything below this line is the original
design record; see the "Implementation status" sections immediately below
for what actually exists in `scripts/fau_deployer/` today and how it
deviates from (or confirms) this doc. Companion to the root `CLAUDE.md`
(which covers the blocks/PetaLinux side); this doc covers the
**desktop-side deployment tooling**.

## Implementation status (2026-09-15, later) — the live control channel

Built, and the pipeline is now feature-complete as designed. The section
"Live flowgraph controls" further down is the design; this is what it
became, and where it was wrong.

New files: `core/controls.py` (the five parsers, the `ui_spec.json` shape,
the wire format and the desktop-side value conversion),
`board/fau_ctl.py` (the board-side dispatcher, shipped per deploy),
`controls_panel.py` (the Tk panel), `json/schemas/ui_spec.schema.json`.
Changed: `core/headless.py` (extract before freezing, inject the snippet),
`core/generate.py` (write/remove the spec, name the payload sidecars),
`core/runner.py` (control writes, `FAU-CTL` routing, the nonce stamp),
`cli.py` (`--list-controls`, `--set`), `gui.py` (the panel and its gate).
Both FAU `.block.yml`s lost their `callbacks:` lists, as designed.

**The three open decisions are settled**: (A) the C++ setters stay -- QA and
`apps/` use them, they are simply unreachable from a flowgraph; (B) `PULSE`
ships in v1; (C) a control whose fields will not evaluate degrades to a
free-entry box and reports, rather than failing the flowgraph.

488 tests green, including the control channel driven end to end over a
real pty into a real foreground process running the real `fau_ctl.py`.
**Still nothing run on a board.**

### The snippet body must say `self`, not `tb`

The design's draft snippet was `import fau_ctl` / `fau_ctl.start(tb)`.
**That raises `NameError` the instant the flowgraph starts.** A snippet is
emitted as the body of

    def snipfcn_<name>(self):

and called as `snipfcn_<name>(tb)` from `snippets_main_after_start(tb)`
(`grc/core/FlowGraph.py:116-117`). The top block therefore arrives bound to
`self`; `tb` is only the caller's name for it, and it is a local of
`main()`, not a global. GRC's own Snippet documentation says as much ("to
reference a block, it should be identified as `self.block`").

Caught by reading the `.py` grcc produced rather than by inspection, which
is the general lesson: the transform's output is cheap to generate and
cheap to read, and every claim about what grcc emits should be checked
against one. `tests/test_fau_ctl.py::TestGeneratedSnippet` now runs the
dispatcher through the emitted snippet text and pins the `tb` form as a
failure, so this cannot come back.

### The run nonce travels in a file, not an environment variable

`fau_ctl` needs the run's nonce to tag its replies, and there is no
reliable way to hand an environment variable through `sudo`: it scrubs the
environment, and both `sudo VAR=v cmd` and `--preserve-env=VAR` require a
sudoers privilege (SETENV / `env_keep`) this tool cannot assume is granted.
So `build_command()` prepends `printf <nonce> > .fau_ctl_nonce && ` when --
and only when -- the run has controls, and `fau_ctl.load_nonce()` reads it
from beside its own `__file__`. A directory the deploy just wrote to needs
no privilege at all.

Chained with `&&` like the `cd`, so a nonce that cannot be written stops
the run rather than starting a flowgraph whose replies nothing can match.
A missing or malformed nonce file falls back to `000000` rather than
refusing to start: losing reply routing is cosmetic, refusing to run is
not. The file is only added to the command when controls exist, so a run
without them produces byte-for-byte the command it always did.

### `fau_ctl.py` must be imported from the deploy directory

`load_controls()` looks for `ui_spec.json` beside `fau_ctl.py`'s own file.
That is right on the board -- both are payload siblings in the deploy
directory, and the flowgraph runs with that directory as `sys.path[0]` --
but it means a test that puts the repo's copy on `sys.path` finds no spec
and silently starts no control channel. Which is what the first run of
`test_runner.py::TestControlChannel` did. The test now imports the
deployed copy, like the board.

### The console echoes every line we write

The board's tty echoes what is written to it, so each `SET` comes back as
apparent flowgraph output. Without a filter a dragged slider fills the log
with its own traffic. `controls.is_control_echo()` matches on the verb plus
a known id rather than on the exact text sent, because the console corrupts
a character now and then and a garbled echo logged as board output is more
confusing than a dropped one.

### The payload sidecars have to be named, not discovered

`payload.py` finds a flowgraph's siblings by walking its imports with
`ast`. The generated `.py` imports `fau_ctl` **inside** the injected
snippet function, which a walk of the top level never sees -- so
`Generated.extra_files` names `fau_ctl.py` and `ui_spec.json` explicitly.
Same class of bug as the `search_dirs` one in the Process phase: the
transfer succeeds and the board dies on `ImportError`, which looks like
success.

### A stale `ui_spec.json` is worse than none

The build directory is stable across regenerations, so a spec left behind
after the operator deletes the last slider from their flowgraph would ship
a panel for controls the running flowgraph no longer has, and every `SET`
would come back `ERR` -- reading as a broken control channel rather than as
a stale file. `_write_spec()` therefore **removes** the file when a
flowgraph has no controls, and returns None.

### The status column cannot hold an error

Each widget has a ~22-character status label, which is right for a value
and useless for "set_gain(1.0) raised ValueError: ...". The widget gets the
marker (red, truncated) and the log gets the sentence, via an `on_message`
callback. A truncated explanation is barely better than none.

### Smaller decisions worth not re-deriving

- **Engineering notation applies to an Entry and not to a Range.** The
  Entry block's stock `conv` really is `eng_notation.str_to_num`, so "1.5M"
  must mean 1.5e6 there; a Range is numeric input from a slider or spinbox,
  and reading "1m" off one as a milli-unit would be a surprise GRC does not
  spring either. `controls.str_to_num` mirrors the twelve SI constants
  rather than importing gnuradio into a Tk process, and a test checks the
  two agree wherever gnuradio is importable.
- **A `raw` control is parsed with `literal_eval` where stock GRC uses
  `eval`.** A deliberate divergence, on both ends. It costs `raw` entries
  holding a real expression; it buys that nothing typed, and nothing
  arriving corrupted off a shared serial line, can execute.
- **Values are restricted to what survives BOTH a JSON and a
  `repr`/`literal_eval` round trip.** A tuple is the realistic exclusion:
  JSON hands it back as a list, which would never compare equal to the
  chooser option the flowgraph holds. Such a control degrades to free entry
  and says so.
- **Slider values are coalesced at ~15 Hz, and a release flushes
  immediately.** The point is not bandwidth -- 30 bytes at 15 Hz is well
  under 1 KB/s against an 11.5 KB/s console -- but keeping the log readable
  and not hammering a setter hundreds of times for one gesture. Flushing on
  `<ButtonRelease-1>` is what guarantees the value the operator actually
  chose is the one that lands.
- **A PULSE holds on the board, blocking the dispatcher for its duration.**
  That is the feature: a value landing between the press and the release
  would make the edge mean nothing.
- **The panel renders at Process, not at Run**, disabled. Seeing what a
  flowgraph will offer is useful before deploying it, and the widgets stay
  dead until the board sends `READY`.
- **`Runner._running` closes the write gate in a `finally`.** Once `run()`
  returns there is no foreground process, so a late write from the UI
  thread would be typed at the board's shell prompt. `gui.py` clears
  `self.runner` in a `finally` for the same reason.
- A Range whose value sits outside its own start/stop (which GRC's own
  assert would have refused, so: a hand-edited `.grc`) has the *widget*
  clamped and reported. The flowgraph still starts at the original value;
  only the slider differs, until it is moved.

## Implementation status (2026-09-15) — the Process phase, live tracking, copyable log

The last unbuilt piece of the pipeline is built: `--flowgraph` now accepts
a `.grc`, and both front-ends compile one automatically rather than asking
the operator to run `grcc` by hand.

New modules, all in `core/`:

- **`grcfile.py`** — a `.grc` is plain YAML, so it is read as YAML rather
  than through `gnuradio.grc.core` (the lighter coupling the doc's
  "Headless transform" section already argued for; it also means preflight
  runs, and stays testable, on a machine without GNU Radio). Carries the
  `file_format` version guard, Gate 1 (a FAU block must be present), and
  target inference. **Both a `fau_source` and a `fau_sink` in one flowgraph
  is refused** — TX and RX are two physically separate boards. A *disabled*
  FAU block gets its own message, because "no FAU block" sends the operator
  looking for one the flowgraph visibly has.
- **`headless.py`** — the transform, on a deep copy; the operator's file is
  never touched and the derived `<id>.headless.grc` is kept as an audit
  artifact next to the generated `.py`. GUI blocks are classified from the
  `value:` key and port domains GRC's own block YAML declares for each, so
  the split follows the block definitions: sinks/decoration are deleted,
  variable-defining controls are frozen to `variable` blocks at their
  current value and reported, message-emitting controls **refuse** unless
  explicitly allowed. An unrecognised `qtgui_*`/`variable_qtgui_*` id is
  refused rather than waved through — waving it through means `grcc` emits
  code that imports Qt, which is the exact failure the transform exists to
  prevent. Throttles are flagged, not stripped.
- **`generate.py`** — preflight, transform, `grcc` as a subprocess (never
  `-r`), into a stable per-flowgraph build directory under
  `$XDG_CACHE_HOME/fau_deployer/build`, keyed by the `.grc`'s absolute path
  so two projects' `tx.grc` cannot overwrite each other.
- **`watch.py`** — polls `stat()` and reports a change only once the stamp
  has held still, so a poll landing mid-save does not hand a half-written
  YAML to the parser. Not a thread: the front-end's own loop calls `poll()`.

### Decisions made while building it (not in the original doc)

- **`run_options` is forced to `run`, and that is a safety property, not a
  preference.** `prompt` generates `input('Press Enter to quit: ')`, and
  the flowgraph's stdin *is* the serial console the deployer is holding —
  console noise or a stray newline would quit it out from under a live
  DMA. `run` emits `tb.start(); tb.wait()`, leaving Ctrl-C (which
  `core/runner.py` sends, and which the generated SIGINT handler turns into
  `tb.stop()`) as the only way to stop it. This is the same `run_options:
  run` the "Live flowgraph controls" section needs in order to free stdin,
  so the two agree. Its known consequence — the generated handler calling
  `tb.wait()` while the main thread is already in one, spawning a second
  `_top_block_waiter` — is carried here too: a wart, not a hazard, since
  the teardown is in the handler's `tb.stop()` and the shell's `FAU-RC`
  status still proves the process was reaped. The fix lives with
  `fau_ctl.start()` in that section.
- **`grcc` runs with the `.grc`'s directory as its cwd and on its
  `PYTHONPATH`.** Found the hard way: an `import` block is *evaluated*
  during code generation, so a flowgraph that does `import fau_tx_common`
  fails to compile at all when the headless copy is compiled from a build
  directory the helper is nowhere near. It reads as "Flowgraph invalid",
  i.e. as a problem with the flowgraph rather than with where it was
  compiled from.
- **`payload.collect_files()` gained `search_dirs`.** The generated `.py`
  lives in the build directory but the sibling module it imports never left
  the source directory; without this the transfer succeeds and the board
  dies on `ImportError`, which is the worst shape of failure because it
  looks like success.
- **The target-inference gate is checked against the selected *mode*, not
  the board.** A board's role is set by the bitstream in its PL, so the
  Mode menu (which names that bitstream) is the only thing that can
  contradict the flowgraph. A non-role mode key like `bootstrap` is
  advisory only.
- **Live tracking does not auto-deploy, only auto-*process*.** Sending a
  new flowgraph to a board that is running one is a decision, not a reflex;
  the GUI also refuses to regenerate at all while a transfer or run is in
  flight. The CLI's `--watch` is therefore tied to `--process-only`.
- **Deploy chains through `job_done`, not through the "processed" event.**
  `job_done` is the one place that resets the GUI's state, so dispatching
  the chained Deploy any earlier would have that reset land on top of
  `DEPLOYING` and re-enable every button mid-transfer.
- **The log pane needed explicit focus to become copyable.** A `-state
  disabled` Tk text widget selects under the mouse, but Tk's own
  `<Button-1>` binding refuses to focus it, so the class-level `<<Copy>>`
  never fires: selecting worked and Ctrl-C did nothing. Fixed with
  `takefocus`, an explicit `focus_set` on click, and its own Ctrl-C /
  Ctrl-A / context-menu / Log-menu bindings. `see("end")` is now
  conditional on the view already being at the bottom, so scrolling back to
  read or select something is not undone by the next board line.

Still absent, unchanged: **Gate 2** (enumerating the board's actual block
set) and serial auto-detect. The null-sink splice is pure-YAML as the doc
suggested starting; it takes the item type from the removed sink's own
declared `type`, which is both correct and loudly wrong when it is not — a
mismatched item size is an immediate `ValueError` from GNU Radio's
`connect()`, not a silent corruption.

Verified end to end against a real `grcc` (GNU Radio 3.10.1.1) on a
flowgraph with a GUI range control, a tab widget, three GUI sinks, an
orphaned branch and a sibling helper module: the generated `.py` imports no
Qt, carries `blocks.null_sink(gr.sizeof_float*1)` on the orphaned port, the
frozen variable at its GRC value, and `tb.start(); tb.wait()`. Still
nothing run on a board.

## Implementation status (2026-08-31)

Scope actually built: **robust transport of a generated flowgraph (+ its
local sibling imports) from desktop to board over the serial console** --
payload assembly, the wire protocol, login/prompt state machine, chunked
send with retransmit, the board-side receiver, and self-healing bootstrap.
Deliberately NOT built in this pass: running the flowgraph on the board /
Ctrl-C teardown, the desktop no-op shim blocks, `.grc` preflight gates and
the headless transform, GUI mode, board-block enumeration (Gate 2), serial
auto-detect. `cli.py` has no `--grcc`/`.grc` front-end yet either; input is
a `.py` file today.

Layout: `scripts/fau_deployer/{cli.py, core/{report,protocol,payload,
transport,session,bootstrap,sender}.py, board/receiver.py, tests/}`. 69
tests (stdlib `unittest`, no pytest on this machine), all green, including a
real pty + real receiver subprocess (`tests/fake_board.py`), a real
forkpty'd shell for the login state machine (`tests/fake_shell.py` +
`tests/shell_stub.py`), and fault injection (`tests/lossy.py`: bit flips,
dropped lines, kernel-printk noise, simulated reboot) proving the
retransmit path actually recovers rather than being untested dead code.
Run: `python3 -m unittest discover -s scripts/fau_deployer/tests -p
'test_*.py' -t .` from the repo root.

Deltas from the design below (all found by actually building and testing
against a real pty, not just reasoning about it):

- **Per-chunk CRC32 is mandatory**, not left as an open question -- without
  it a bit flip landing inside the base64 alphabet decodes cleanly, gets
  ACKed, and is only caught by the end-of-transfer sha256, forcing exactly
  the full re-send chunking exists to avoid.
- **V1 ships stop-and-wait (`--window 1` behavior) only.** The credit-window
  optimization discussed below is deferred; the retransmit loop is written
  to support a window but nothing yet drives it above 1.
- **The bootstrap push needed a way to tell a stale receiver to stop.** It
  deliberately clears `ISIG` in raw mode, so Ctrl-C is just a data byte, not
  a signal -- added `ShutdownRequested` in `receiver.py` (0x03 anywhere in
  the input stream means "exit cleanly"), which the bootstrap logic in
  `core/bootstrap.py` relies on to retire a stale copy before repushing.
- **Payload input is multi-file by default, not an afterthought** --
  `core/payload.py` walks the flowgraph's imports with `ast` and recursively
  collects local sibling modules (confirmed against the repo's own
  `examples/tx_sine.py` -> `fau_tx_common.py`), since a flowgraph shipped
  alone would transfer successfully and then fail with `ImportError` on the
  board, which is a worse failure than a slow one.
- **`LineReader` (core/transport.py) had three real bugs**, all caught by
  tests against a real pty rather than by inspection: (1) `wait_for()`
  checked the whole buffer for a match before draining complete lines,
  so a multi-line burst arriving in one read returned everything instead of
  just the match and skipped the `on_line` callback for earlier lines; (2)
  a matched *unterminated* tail (a bash prompt has no trailing `\n`) was
  returned but never removed from the buffer, so it got prepended to
  whatever arrived on the *next* call -- this broke every second command
  against the same session; (3) `BoardSession._probe()`'s classification
  had the same "tail read but not consumed" bug via `collect_for()`. All
  three are fixed and covered by regression tests in `test_transport.py`.
- **`_probe()` cannot blindly send `\r` to elicit a response.** That's safe
  at a shell prompt (redraws it) but not at a fresh `login:` prompt, where
  it submits a blank username -- the fix reads passively first and only
  falls back to sending `\r` if nothing is pending.
- **A race in the stale-receiver retirement path**: sending `0x03` and
  immediately issuing the next shell command risked the still-dying
  receiver reading those bytes as protocol noise and discarding them before
  it exited -- they'd never reach the shell at all. Fixed by waiting for an
  idle shell prompt to actually reappear before proceeding (originally via a
  custom PS1 marker; see "PS1 prompt replacement removed" below for why
  that was dropped in favor of generic prompt detection).
- **`_do_password()`'s fixed 8-second wait was a real (if non-fatal)
  performance bug** -- `collect_for()` always blocks for its whole window;
  added `collect_until(predicate, timeout)` for early-exit waits where more
  than one condition (rejection message vs. prompt) can end the wait.
- **`os.forkpty()`, not `pty.openpty()` + `subprocess.Popen(stdin=slave_fd,
  ...)`**, for the test harness (`tests/fake_board.py`, `tests/fake_shell.py`).
  The latter only dup2's an already-open fd onto the child's stdio, which
  never establishes the pty as that process's *controlling* terminal
  (needs `setsid()` + `TIOCSCTTY`, which only `forkpty()`/`pty.fork()` do
  automatically) -- without it, `Ctrl-C` never generates a real `SIGINT`,
  which matters for the orphan-recovery test (`tests/shell_stub.py`'s
  `mode_orphan` forks a child that a real SIGINT can kill, mirroring how a
  real shell survives Ctrl-C while its foreground child doesn't).

## PS1 prompt replacement removed (2026-09-02)

The login/prompt state machine (`core/session.py`) no longer sets a custom
`PS1`. The original rationale below ("Set a **unique `PS1`** for unambiguous
prompt detection") turned out to be unnecessary and actively unfriendly:
engineers connect to these boards' serial consoles directly and by hand
often enough that leaving a protocol-looking `===FAU-PS1===` string sitting
as the prompt after a deploy was a real cost, not a cosmetic nit.

Two changes made this droppable, both keyed off a generic "ends in `$` or
`#`" regex against whatever the shell's real prompt already is, rather than
a literal marker string:

- **`BoardSession.run()` waits for the `FAU-RC:<code>` line it appends to
  every command, AND the prompt that follows it** -- the RC line alone
  already proves the command is done (it only appears once the shell has
  moved on to the appended `echo`), but the prompt is printed
  asynchronously after that; returning as soon as only the RC line showed
  up left that prompt to arrive later, unconsumed, and glue onto the front
  of the *next* run() call's echoed command. Caught by
  `test_repeated_commands_do_not_desync` -- the exact regression class that
  test was written to guard against, just via a new path to the same bug.
  Both conditions are one `collect_until()` predicate, so this costs
  nothing extra when the prompt is already there and simply waits out the
  race when it isn't.
- **The one place waiting for a prompt with no command/RC line to key off
  at all** -- confirming a foreign process (a stale board-side receiver)
  has released the console after Ctrl-C -- is
  `BoardSession.wait_for_idle_prompt()` / `interrupt_and_wait()`, using the
  same regex. It already existed as `_probe()`'s fallback classification
  for "some other shell prompt we haven't configured"; it's now the only
  mechanism, not a fallback.

This also retired `BoardSession.disconnect()`, which used to send
`unset PS1` to try to restore a normal-looking prompt on the way out. That
was worse than doing nothing: bash does not fall back to a default `PS1`
when it's unset interactively, it goes blank, which broke the very regex
above on the next `connect()` against the same console. With no custom PS1
to begin with, there's nothing left for `disconnect()` to clean up.

**Also found while testing this against a real `/bin/sh` (dash, more
strict than the board's assumed bash):** the shell-hygiene command run once
per `connect()` included `set +o history` (suppress in-session command
recall, on top of `unset HISTFILE` for "don't persist to disk"). dash
doesn't implement `-o history` at all, and treats the resulting illegal-
option error as fatal to the *rest of that `;`-chained command list* --
every earlier design never noticed because the PS1 assignment used to run
*first* in that same chain, so its effect had already taken hold before the
abort. Once `run()` started depending on a trailing echo that same chain
appends, the abort meant that echo silently never ran either, on every
single `connect()`. Dropped `set +o history` entirely -- `unset HISTFILE`
already covers the goal that matters (nothing written to disk), and the
extra suppression isn't worth depending on a bash-only option that may not
even be there on the board's actual login shell.

## GUI, run phase, boards/mode/bitstream, and Ctrl-D grounding (2026-09-03)

Scope added this pass: the **interactive front-end** (`gui.py`, Tkinter),
the **run phase** (`core/runner.py`), a **tracked board inventory**
(`bitstreams.json` + `credentials.json` + `core/boards.py`) with
**pre-staged bitstream loading**
(`core/fpga.py`), the **flowgraph params field** (`core/params.py`), and
**Ctrl-D grounding** in `core/session.py`. The CLI got the same features
(`--board`, `--bitstreams`, `--credentials`, `--mode`, `--load-bitstream`,
`--run`, `--params`, `--no-deploy`, `--no-ground`, `--list-boards`) since
they are the same core.

Still NOT built: the **Process phase** (`.grc` ingest, preflight gates,
headless transform, `grcc`). It is a visible **disabled** button in the GUI
with a dialog saying what it will do and what to do instead; input is still
a generated `.py`. Gate 2 (board-block enumeration) and serial auto-detect
are also still absent -- the Port menu lists devices but never picks one.

Tests: 240 total (was 74; 3 skipped without passwordless sudo), all green, `python3 -m unittest discover -s
scripts/fau_deployer/tests -p 'test_*.py' -t .` from the repo root. New
files: `test_ground.py`, `test_runner.py`, `test_fpga.py`,
`test_boards.py`, `test_params.py`, `test_frontend_seams.py`,
`test_gui.py`. **Nothing has run against a real board.**

### Settled open decisions (from the list further down)

- **#2 GUI toolkit: Tkinter/ttk.** The deployer is stdlib-only apart from
  pyserial (stdlib `unittest`, bare `print()`, no `logging`), and a plain
  form is not what a heavier toolkit buys. PyQt5 and GTK3 are both present
  on the dev desktop; neither earned the dependency.
- **#1 serial specifics: per session, never recorded, never guessed.** No
  auto-detect and nothing on disk. The Port menu / `--port` is the only
  source, because assignments are not static. The menu lists
  `/dev/serial/by-id/` entries first (`ttyUSB` numbering is assignment
  order and swaps on a replug) and the GUI resolves the symlink for display
  only.
- **#1 credentials: a separate gitignored file, or per session.**
  `credentials.json` (board name -> user/password,
  `json/example_credentials.json` to copy) as the reference tooling does it;
  the GUI's Credentials dialog overrides per board for the session only, and
  `--user`/`--password` do the same. `bitstreams.json` never holds a secret.
  With no credentials file at all, the documented `petalinux`/`1234` is
  used.
- **#3 file-watch on the `.grc`: not done, and blocked on Process anyway.**

### `core/runner.py`: no run wrapper, and no `===FAU-HALTED===`

Both were in the design above; both turned out to be unnecessary, and the
second is actively worse than what replaced it.

- **grcc already emits the correct teardown.** Confirmed in a real
  generated flowgraph (`test_flowgraphs/test_chirp.py:90-96`):
  `sig_handler` does `tb.stop(); tb.wait(); sys.exit(0)`, wired to SIGINT
  and SIGTERM. `tb.wait()` returning is what runs the teardown carrying the
  required `DMACR.RS` clear -> poll `DMASR.Halted` -> fabric reset. There is
  nothing to add around it. **Corrected 2026-09-15:** that teardown is in the
  blocks' `stop()` overrides (`fau_sink_impl.cc:421`), not in their
  destructors, which are empty (`fau_sink_impl.cc:173`,
  `fau_source_impl.cc:133`). Same conclusion, and `core/runner.py`'s module
  docstring needs the same wording fix.
- **The shell's exit status is stronger evidence than a printed sentinel.**
  A `print("halted")` can be emitted and *then* wedge in a destructor;
  `echo "FAU-RC-<nonce>:$?"` only appears once the process is genuinely
  reaped, destructors included. So the run reuses the same FAU-RC mechanism
  `session.run()` uses, nonce-tagged (`protocol.new_nonce()`) so flowgraph
  stdout cannot forge it. **Drop `===FAU-HALTED===` from the design.**

### Trap found while building it: the flowgraph reads the console

> **Relaxed, conditionally, by the 2026-09-15 control-channel design below:**
> the transform sets `run_options: run`, which removes the `input()` call
> entirely, and `fau_ctl.py` then owns stdin. Writing to the console while a
> flowgraph runs is legal **only** for a payload built that way -- see "The
> console-write gate". For every other payload, including a hand-written
> `.py`, the rule below stands exactly as written.

grcc's `no_gui` template blocks in `input('Press Enter to quit: ')` after
`tb.start()`. **The flowgraph's stdin IS the serial console**, so:

- **Nothing may be written to the console while a flowgraph is running.**
  One stray newline ends the run. So would an 0x04 (the template catches
  `EOFError` and falls through to its own `tb.stop()`/`tb.wait()`).
- Ctrl-C is unaffected -- the line discipline turns 0x03 into SIGINT rather
  than handing it over as data. Another reason it, and not a newline, is
  the stop mechanism: it means the same thing whether or not a particular
  template happens to be sitting in `input()`.
- That prompt has no trailing `\n`, so it stays in the `LineReader` tail and
  is never reported as an output line.

Also: a flowgraph with **no GRC Parameter blocks** imports `ArgumentParser`
and never builds a parser, so extra argv is **silently ignored** -- no
error, no effect. That is what makes desktop-side param validation worth
having rather than a nicety (`core/params.py`), and why
`flowgraph_options()` must return an empty set (real answer: takes no
options) distinctly from `None` (could not read it).

### The flowgraph runs under sudo (2026-09-03, found on hardware)

The first real GUI run against a board failed in the block constructor:

    RuntimeError: fau_modem: cannot open lock file
    /run/lock/fau-dma-40400000.lock: Permission denied

`fau_source`/`fau_sink` open `/dev/mem` and take a lock under `/run/lock`,
so the run needs root. `core/runner.py` now prefixes `sudo -n`
(`--no-sudo` / a Boards-menu checkbox turn it off for an already-root
login).

**`sudo -n`, and deliberately NOT `sudo -S` with the password piped in**,
which is what `core/fpga.py` falls back to for fpgautil. The difference is
stdin: fpgautil does not read it, but grcc's no_gui template blocks in
`input('Press Enter to quit: ')`. Piping a password would make that stdin
the password pipe, at EOF the instant it is read -- the template catches the
EOFError and runs straight into its own `tb.stop()`/`tb.wait()`, so the
flowgraph would appear to start and then exit instantly for no visible
reason. `-n` fails fast instead, and `RunResult.sudo_denied` turns the
generic "exit status 1" into a message naming the fix (NOPASSWD, or a root
login), since otherwise an operator goes looking at the flowgraph.

**Ctrl-C still reaches the flowgraph through the extra hop** -- the property
that matters, since that signal is what runs the DMA teardown. sudo(8)
"Signal handling": SIGINT is "only relayed when the command is being run in
a new pty or when the signal was sent by a user process, not the kernel.
This prevents the command from receiving SIGINT twice each time the user
enters control-C." No new pty -> the command shares our tty and foreground
process group and the kernel delivers SIGINT directly; new pty -> sudo
relays. Exactly one SIGINT either way. **Documented behaviour, not measured
on hardware**; `TestRealSudo` checks it for real wherever passwordless sudo
exists and skips otherwise, because a stub sudo would only prove the stub
relays signals.

Also fixed from the same screenshot: the console's echo of the run command
came back corrupted (`./test_chirp.p.py`), so the exact-match echo filter
missed it and the whole line was logged as board output. The filter now
keys on the marker with `$?` still unexpanded, which the real status line
never contains.

### Terminate has no escalation ladder, by design

Stop is `0x03` and only `0x03`. No SIGTERM follow-up, no `kill -9`, and
none should be added later. Killing a process holding a live DMA burst
orphans an AXI transaction on the PS HP port, and per the root `CLAUDE.md`
~30 of those wedge the board until a power cycle -- forcing it is the one
action guaranteed to make the situation worse.

A stop that does not confirm is therefore **reported, not forced**:
`RunResult.wedged`, which the GUI turns into a sticky `WEDGED` state (every
board action disabled, only Disconnect available, explicit dialog saying the
board may still have DMA live) and the CLI into its own exit code
(`EXIT_WEDGED = 7`), so CI can tell it apart from an ordinary failure.
**There is deliberately no force-kill button.**

### Ctrl-D grounding: ordering is a hardware-safety requirement

`connect()` now drives any console that is not at a fresh `login:` back to
one and logs in again, instead of adopting whatever shell was sitting there
with an unknown amount of somebody else's state attached (cwd, exports, a
nested subshell). `BoardSession(ground=False)` / `--no-ground` keeps the old
behaviour; the only legitimate user is a second session deliberately sharing
one already-configured console.

**Ctrl-C strictly precedes Ctrl-D, and that is not tidiness:**

- If a foreground process owns the console, 0x04 is not a logout at all --
  it is EOF on that process's stdin. Ctrl-C first is what retires it, and
  for a flowgraph that means the clean SIGINT teardown above.
- A logout that lands while a flowgraph is still alive delivers **SIGHUP**
  instead, killing it with none of that -- the orphaned-burst path again.

Covered by `test_ground.py::test_foreground_process_is_interrupted_before_
the_logout`, which holds the console with `sleep` and asserts a login prompt
is still reached: a Ctrl-D-first implementation burns every attempt and
fails.

Retries are bounded (3) because **each Ctrl-D exits exactly one shell** -- a
nested subshell eats the first. Exhausting them is a real failure (a shell
with `IGNOREEOF` set never logs out this way) and is reported as one.

**Residual risk, not handled:** this covers a *foreground* flowgraph, which
is the only kind the tool ever starts (`runner.py` never backgrounds
anything). Something a human backgrounded by hand is out of Ctrl-C's reach
and would still be SIGHUPed by the logout.

### `bitstreams.json`: a board name and its loadable bitstreams, nothing else

**Settled 2026-09-03**, after two wrong turns. The first cut invented a
`board_types` x `boards` structure with a `schema` version. The second
copied `~/repos/Unified-FAU-Modem-Test-Tooling`'s role-keyed `devices.json`
wholesale (`tx_board`/`rx_board`, `serial_port`, `baud_rate`). Both are
gone. The format is:

```json
{
    "S10": {
        "bootstrap": "/home/petalinux/firmware/Radio_Top_v2_wrapper.bit.bin",
        "tx":        "/home/petalinux/S10_dac.bit.bin",
        "rx":        "/home/petalinux/S10_adc.bit.bin"
    }
}
```

A board name, and named board-side paths under it. That is all.

**Nothing about how to REACH a board belongs in it** -- no serial port, no
baud, no destination directory, no login. Port assignments are not static
and an engineer sets one per session; a port recorded in a file is a port
that goes stale and deploys to the wrong board. So:

- The Port menu / `--port` is the **only** source of a port. Nothing on disk
  records one, nothing infers one from the board, and **every board action
  stays disabled until one is chosen** -- `--board` can never stand in for
  `--port`. Pinned by
  `test_gui.py::test_out_of_the_box_no_port_means_nothing_touches_the_board`,
  `test_cli.py::test_board_alone_never_supplies_a_port` and
  `test_boards.py::test_records_nothing_about_reaching_a_board`, which
  fails if a connection key ever reappears in the tracked file.
- Baud and dest come from constants (`--baud`, `--dest` to override).
- Logins stay in the separate, gitignored `credentials.json`, now keyed by
  the same board names.

**Bitstream keys are free-form.** `bootstrap`, `tx` and `rx` are the
conventional ones; `bootstrap` is listed first wherever modes are offered
and the rest alphabetically, so a menu reads in the order things happen. A
mode a board does not have is simply an **absent key**: no nulls, no
placeholders, nothing to keep in step. One board can carry both a `tx` and
an `rx` bitstream, which is also why the earlier role-keyed layout was the
wrong shape -- `lib/hw/board_map.h` compiles in both address maps and
selects per block.

**`bootstrap` loads first** (confirmed 2026-09-03). It is the base design a
board is brought up on, so asking for `tx` or `rx` loads bootstrap and then
that one; asking for `bootstrap` is a single step, since it is the
destination rather than a prelude to itself. The ordering lives in
`Board.load_sequence()`, which returns the ordered `(name, path)` steps, and
`fpga.load_sequence()` performs them. Four details worth keeping:

- **Deduped by PATH, not by name.** If `bootstrap` and the mode point at the
  same file, the second load is dropped -- a full PL reconfiguration that
  changes nothing.
- **A board with no `bootstrap` entry loads the mode alone, and both
  front-ends say so** (a `report.warn` in the CLI, a NOTE in the GUI's
  confirmation dialog). Not an error: absence is how this format says "no
  such thing", so a genuinely self-contained design has to stay
  expressible.
- **The first failure stops the sequence.** After a failed load the PL is in
  an unknown state, and layering another full reconfiguration on top of it
  destroys the evidence. When earlier steps *did* succeed the error says so
  -- "nothing happened" and "the board is now running the base design but
  not the mode you asked for" are very different things to walk back into.
- **The PL-replacement warning fires once, after the last step**, not per
  load. Warning twice about an intermediate PL that is superseded seconds
  later trains people to skip the warning that matters.

The GUI's confirmation dialog spells the whole sequence out numbered, with
each path, because "load tx" quietly loading two bitstreams is not something
to discover afterwards in the log.

Kept from the reference tooling, which remains the reference for bench
definitions generally: secrets in a separate gitignored `credentials.json`
with an `example_` copy to work from, JSON Schemas under `json/schemas/`
(draft 2020-12), and `--credentials` as the flag. Its role-keyed device
layout does not apply here.

**Validation is hand-written, not Pydantic.** The reference generates its
schemas from Pydantic models; this package is deliberately stdlib-only apart
from pyserial (the same reason the GUI is Tkinter), and a deploy tool that
fails to import is worse than one with hand-written checks. The schemas stay
the shared contract: `test_boards.py` validates the tracked files against
them with `jsonschema` **when importable** and skips otherwise, plus a
sampled both-ways agreement test, so the two cannot drift silently.

### `core/fpga.py`: success is a positive marker, never absence of error

Bitstreams are **pre-staged on the board**; nothing is transferred. The load
is one command, `sudo -n fpgautil -b <path>`, whose output is then judged.
Observed on the hardware:

    $ sudo fpgautil -b dummy
    Error: User provided bitstream file doesn't exist
    $ sudo fpgautil firmware/Radio_Top_v2_wrapper.bit.bin
    $                          <-- -b omitted: rc 0, NOTHING, nothing loaded
    $ sudo fpgautil -b firmware/Radio_Top_v2_wrapper.bit.bin
    fpga_manager fpga0: writing Radio_Top_v2_wrapper.bit.bin to Xilinx Zynq FPGA Manager
    Time taken to load BIN is 47.000000 Milli Seconds
    BIN FILE loaded through FPGA manager successfully

The middle case is the whole rule: a plausible invocation succeeded and did
nothing. Anything that is not `loaded through FPGA manager successfully` is
a failure, whatever the exit status says.

`sudo -n` (non-interactive) first, so a board that wants a password fails in
a second with a clear message instead of hanging at an invisible prompt; if
sudo actually asks, one retry feeds the login password on stdin.

Format is **`.bit.bin`** (bootgen-processed, header stripped, bit-swapped),
not a raw Vivado `.bit`; a path not ending in `.bit.bin` warns and
continues, since only the board can settle it.

**A load replaces the hardware every `/dev/mem` mapping points at**, so it
is gated on nothing running (the same class of failure as `DMACR.Reset`
mid-burst) and it warns afterwards that every AXI GPIO's data register is
back to zero -- NCO/CIC/`frame_len` must be reprogrammed by the next arm.

Transfer cost was measured before choosing pre-staging, in case it is ever
wanted: the `.bit` files gzip 11x/18x (2.0 MB -> 181 KB, 4.0 MB -> 225 KB),
so base64 over 115200 baud would be **~28 s / ~35 s** -- viable, not the
minutes it looks like. Note `payload.MAX_BYTES_DEFAULT` is 4 MiB and the
7020 `.bit` is 4,045,673 B, 3% under the cap.

Environment, confirmed in this repo's build trees rather than assumed: both
kernels have `CONFIG_FPGA=y`, `CONFIG_FPGA_MGR_ZYNQ_FPGA=y`,
`CONFIG_FPGA_BRIDGE=y`, `CONFIG_FPGA_REGION=y`, while both PetaLinux project
configs have `CONFIG_SUBSYSTEM_FPGA_MANAGER is not set` -- so `fpgautil` is
not staged into an image built from *this* tree (only its `.c` sits in
meta-xilinx). It *is* present on the bench board, so that board's image is
not the one these projects build. Worth reconciling before relying on a
fresh image having it.

### What the GUI adds over the CLI, and what it needed from core

Three things, none of them protocol: one **worker thread** that solely owns
the transport (the console is one resource, so a queue of one worker is the
only correct shape), one **state machine** deriving every button's enabled
state so exclusivity is structural rather than remembered, and a **log
pane**.

Core changes it forced, all small and all used by the CLI too:

- `report.set_sink()` -- redirect every report line into the log pane. A
  process-global sink, because core is only ever driven from one thread.
  The alternative (thread a reporter object through session/bootstrap/
  sender and every test) is far larger for no gain.
- `Sender(on_progress=...)` -- structured progress for the bar, separate
  from `progress=` (which writes `\r` to a terminal). Not alternatives: a
  GUI launched from a terminal can want both.
- `Sender(should_stop=...)` -- Cancel. Checked at the top of the send loop,
  the one place reached within `POLL_INTERVAL` on every path, so it lands
  in well under a second even mid-retransmit; it `abort()`s first so the
  receiver stops waiting instead of idling to its timeout.

`test_frontend_seams.py` covers all three against the real receiver, and
`test_gui.py` covers the state machine (skipped without a display; no
mainloop, window withdrawn).


## Live flowgraph controls: QT GUI inputs on the deployer (2026-09-15, design)

**This is the design; it is now built** -- see "Implementation status
(2026-09-15, later)" above for what it became and the four places it was
wrong (the snippet must bind `self` not `tb`, the nonce cannot travel
through `sudo` in the environment, the tty echoes our own writes back, and
the payload sidecars have to be named rather than discovered). Kept as
written because the reasoning behind each decision is the part worth
having. It is the design for rendering a flowgraph's **QT GUI input
blocks** as widgets in the deployer and pushing their values to the
flowgraph running on the board, over the same serial console everything
else uses.

**Scope is the five variable-defining input blocks and nothing else:**

| GRC block | id | What it compiles to |
|---|---|---|
| QT GUI Range | `variable_qtgui_range` | `RangeWidget(..., self.set_<id>, ...)` |
| QT GUI Entry | `variable_qtgui_entry` | `returnPressed -> self.set_<id>(conv(text))` |
| QT GUI Chooser | `variable_qtgui_chooser` | combo/radio -> `self.set_<id>(choice)` |
| QT GUI Check Box | `variable_qtgui_check_box` | `stateChanged -> self.set_<id>(choices[bool(i)])` |
| QT GUI Push Button | `variable_qtgui_push_button` | `pressed/released -> self.set_<id>(...)` |

**QT GUI *sinks* are explicitly out of scope and are not a deferred
feature -- they do not fit the wire.** A time sink at 400 ksps complex is
~3.2 MB/s against a console that carries ~11.5 KB/s. Scopes, FFTs and
waterfalls stay on the desktop with a locally-run copy of the flowgraph.
Low-rate scalar telemetry (a probe at a few Hz, or the blocks' own
`underruns()`/`clipped()`/`bds_moved()` counters) *would* fit and is a
plausible follow-on, but it is a different mechanism -- board-to-desktop,
periodic push, no `set_` involved -- and is not designed here.

### The one fact the design rests on

All five blocks do exactly one thing: call `tb.set_<id>(value)`. Nothing
else. Verified by reading the installed block definitions in
`/usr/share/gnuradio/grc/blocks/qtgui_{range,entry,chooser,check_box,
push_button}.block.yml` -- every `make:` template wires its Qt signal
straight to `self.set_${id}`.

And `set_<id>` is generated for **every** variable, whether or not a Qt
widget ever existed: `flow_graph.py.mako:265-285` emits `get_`/`set_` for
`parameters + variables`, and the setter body is the `callbacks:` list of
every block that depends on that variable. That is the entire live-update
machinery of GNU Radio, and it is already generated for us.

So a remote control is not an emulation of a Qt widget. It is **the same
code path with a serial line spliced in where the Qt signal/slot connection
would be**:

```
deployer widget -> "SET gain 0.42\n" over UART
  -> fau_ctl reader thread (whitelist check, ast.literal_eval)
  -> tb.set_gain(0.42)             # grcc-generated
  -> downstream blocks' callbacks  # from their .block.yml
  -> "FAU-CTL-<nonce> OK gain 0.42" back up the wire
```

These are all desktop-side generator facts, checked against the desktop's
own GNU Radio. They are not subject to the "3.10 in `build_*/` vs 3.11 on
the board" trap in the root `CLAUDE.md`: `grcc` runs on the desktop, and
only the generated `.py` crosses to the board.

### Decision: FAU Source/Sink parameters are frozen at construction

`fau_modem_fau_sink.block.yml` declares `callbacks:` for `set_nco_freq`,
`set_samp_rate` and `set_tx_scale`; `fau_modem_fau_source.block.yml` for
`set_nco_freq` and `set_samp_rate`. **Delete all five.** With no `callbacks:`
entry, GRC still evaluates whatever expression feeds the parameter when the
flowgraph is built and passes it as a constructor argument, and nothing can
change it afterwards.

This is enforcement at the source of truth rather than a rule the deployer
has to remember, and it buys a structural safety property that is worth
more than the feature it removes: **`set_samp_rate` was the only setter
reachable from a control that touches the DMA** (it re-arms the engine, ~1.2 s
per call -- see root `CLAUDE.md`). With it unreachable, no widget on the
deployer can re-arm, halt, or reallocate anything on the DMA path. The worst
a control can now do is make a stock GNU Radio block raise, which `fau_ctl`
catches and reports as an `ERR` reply. **The control channel is structurally
incapable of wedging the board.**

That in turn deletes a whole subsystem that an earlier draft of this design
needed: a per-variable "cheap / expensive / re-arming" cost classification,
with commit-on-release behaviour for the expensive ones. There are no
expensive ones any more.

The C++ `set_nco_freq`/`set_samp_rate`/`set_tx_scale` methods **stay** as
public API -- the QA tests and the `apps/` bring-up tools use them. They
simply become unreachable from any flowgraph.

### Decision: controls that do nothing still render

If a variable feeds only a constructor argument and no downstream block
declares a `callbacks:` entry for it, `set_<id>` updates an attribute and
nothing observable happens. **This is not detected and not reported.** An
engineer who wires up a control that has no live effect has made the same
mistake they would have made running the flowgraph on the desktop, and it
behaves the same way in both places.

Consequence for the build: the transform needs **no callback-graph analysis
and no cross-referencing of what a variable feeds**. Spec extraction is a
flat, per-block parse of five YAML schemas. This was the fiddliest part of
the earlier draft and it is gone.

### Where each piece lives

| Piece | Where it runs | Roughly |
|---|---|---|
| Five block parsers + `variable` substitution | transform, desktop | 150-250 lines |
| `ui_spec.json` | build artifact | data |
| `fau_ctl.py` | **ships in the payload**, runs on the board | 80-120 lines |
| Injected snippet | generated `.py` | **2 lines** |
| Control panel | `gui.py`, desktop | ~150 lines of Tk |
| Control writes + ack routing | `core/runner.py` | 60-100 lines |

**Nothing is installed on the board.** `fau_ctl.py` is an ordinary Python
module that rides along as a sibling of the generated flowgraph;
`core/payload.py:71`'s `collect_files(main, extra=())` already takes an
explicit extras list, so the transform hands it over by name rather than
relying on the import scanner finding it. No RPM, no PetaLinux rebuild, no
image change, and the board-side half can be iterated as fast as Deploy can
be pressed -- which matters in a project where a stale hand-scp'd copy on
the board has already cost multiple rounds of debugging (root `CLAUDE.md`,
"Process lessons from this bug").

### Rejected alternatives (do not re-litigate without new evidence)

1. **A `fau_remote_*` palette of our own GRC input blocks.** Rejected. The
   stock blocks give a property worth protecting: **one flowgraph, two run
   modes.** The engineer presses Run in GRC and gets real Qt sliders on the
   desktop; they press Deploy and get the same controls in the deployer, with
   the desktop run standing as the reference for what the flowgraph should
   do. Our own blocks would have to reimplement `Range`/`RangeWidget` just to
   stay locally runnable, need installing on every desktop, and would be a
   second thing to keep in sync with upstream. The coupling we take on
   instead -- reading five `.block.yml` parameter schemas, stable at
   `file_format: 1` -- is far smaller, and gets the same version guard as the
   `.grc` format itself.

2. **An Embedded Python Block as the dispatcher. Not possible.** This one is
   forced, not a preference. An `epy_block` is a `gr.basic_block` instance: it
   has no reference to the top block and no supported way to obtain one (its
   constructor arguments are evaluated in the flowgraph namespace, which holds
   the variables but not `tb`). It therefore cannot call `tb.set_<id>()`,
   which is the only thing that makes a variable change propagate. The
   dispatcher must live where `tb` is in scope.

3. **A wrapper `main` that imports the generated module.** Works, but it is a
   second entry point that has to re-do argparse and re-install signal
   handling, and it puts us in the business of tracking whatever grcc's
   template does next release. The stock **Snippet** block is the supported
   seam for exactly this, and `main_after_start` is emitted as
   `snippets_main_after_start(tb)` (`flow_graph.py.mako:411`) with `tb`
   already in hand.

### What the transform emits

On the derived `flowgraph.headless.grc` (the user's file is never touched --
same discipline as the rest of the transform):

- each of the five QT GUI input blocks -> a plain `variable` block with the
  **same `id`** and its default value;
- `generate_options: no_gui`;
- **`run_options: run`** -- this is what removes `input('Press Enter to
  quit: ')` and frees stdin for the control channel;
- one `snippet` block, section `main_after_start`, body:
  `import fau_ctl` / `fau_ctl.start(tb)`;
- plus the already-planned sink strip, null-sink splice and throttle flag.

Note there is **no generated mapping table**. The variables *are* the
mapping: `fau_ctl` resolves by name with `getattr(tb, "set_" + id)`, so
nothing in the generated `.py` knows the control channel exists beyond
those two injected lines. Keeping logic out of generated code is deliberate
-- generated code cannot be unit-tested, reads badly in a traceback, and
needs a re-transform to change.

And two side artifacts:

- **`ui_spec.json`** -- one entry per control: `id`, `kind`, `label`,
  `dtype`, `default`, and the per-kind widget fields (bounds/step/style for
  a range, the option/label pairs for a chooser, the two values for a check
  box, pressed/released for a push button). It exists **because the
  conversion is lossy**: once a `qtgui_range` is a `variable`, its bounds and
  labels are gone from the flowgraph. It has three consumers -- the GUI
  renders from it, `fau_ctl` uses it as the id whitelist, and the CLI reads
  it to list what is settable -- so it gets a JSON Schema in
  `json/schemas/ui_spec.schema.json` and a test validating a generated
  sample against it, matching how `bitstreams.json`/`credentials.json` are
  handled. Unlike those two it is a **build artifact, not a config file**;
  it is written into the build dir and shipped, never hand-edited.
- the derived `.grc`, as an audit record.

### `run_options: run` brings a SIGINT re-entrancy trap, and the fix

grcc's generated SIGINT handler is `tb.stop(); tb.wait(); sys.exit(0)`. Under
the current `prompt` path that is harmless, because the main thread is
sitting in `input()` when the signal arrives. **Under `run_options: run` the
main thread is already inside `tb.wait()`**, and `top_block.wait()` spawns a
fresh `_top_block_waiter` thread on *every* call
(`gnuradio/gr/top_block.py:22-65` -- the kludge that makes Ctrl-C
interruptible at all, by polling a 100 ms event in the caller's thread while
a helper thread does the blocking C++ wait). So the handler's `tb.wait()`
would start a **second concurrent `top_block_wait_unlocked`** on the same
flowgraph, during the exact teardown this whole tool is built to protect.

**Fix, one line inside `fau_ctl.start()`:** re-install SIGINT as `tb.stop()`
only. Ordering works because the snippet runs from `main_after_start`, which
the template emits *after* its own `signal.signal(...)` calls, so ours wins.
Then Ctrl-C does: handler calls `stop()` -> returns -> the **existing**
`tb.wait()` poll loop observes completion and returns -> `main()` returns.
One wait, no re-entrancy, and no `sys.exit()` from inside a signal handler.

Everything the deployer already relies on is untouched: ISIG stays on, so
`Runner.terminate()`'s raw `0x03` still reaches the foreground process group,
and `echo "FAU-RC-<nonce>:$?"` still proves the process was reaped.

### Correction: the DMA teardown is in `stop()`, not in the destructors

The 2026-09-03 section above, and `core/runner.py`'s module docstring, both
say `tb.wait()` returning is what runs "the block destructors, which carry
the required `DMACR.RS` clear". **The destructors are empty**
(`fau_sink_impl.cc:173`, `fau_source_impl.cc:133`). The teardown lives in the
`stop()` overrides (`fau_sink_impl.cc:421` and its `fau_source_impl`
counterpart), which GNU Radio calls as part of halting the flowgraph.

The conclusion drawn from it is unchanged -- Ctrl-C still produces a clean
teardown, and the exit status is still the proof -- but the mechanism is
worth naming correctly, because it is what makes the fix above safe: teardown
completes *before* `tb.wait()` returns, so it does not depend on interpreter
shutdown or on when `tb` is collected. Fix the wording in both places when
this is built.

### Wire protocol additions

Two verbs, desktop to board, one line each, sent only while a flowgraph is
running:

- `SET <id> <python-literal>`
- `PULSE <id> <ms>` -- the momentary push button, see below.

Replies are nonce-tagged with the same run nonce the `FAU-RC` marker uses,
so flowgraph stdout cannot forge one and `Runner` can route them out of the
log pane: `FAU-CTL-<nonce> OK <id> <value>` / `FAU-CTL-<nonce> ERR <id>
<reason>`.

Rules, all of them load-bearing:

- **`ast.literal_eval`, never `eval`**, on both ends. The desktop parses what
  the operator typed and sends a canonical `repr`; the board parses that
  again. A `raw`-typed Entry whose text is not a literal is refused on the
  desktop with a clear message rather than shipped.
- **Ids are whitelisted from `ui_spec.json`**, so a corrupted line cannot
  name an arbitrary attribute of `tb`.
- `fau_ctl` **never writes to stdout except its own tagged replies**, and
  catches every exception out of a setter and turns it into `ERR`. A control
  must not be able to take the flowgraph down.
- No ACK/retransmit machinery. Unlike the payload transfer, a lost or
  corrupted control line is not worth recovering: the operator sees no `OK`
  and moves the slider again, and the *next* value is more useful than the
  one that got lost anyway. A corrupted line either fails the whitelist or
  fails `literal_eval`, and both produce `ERR`.

### The console-write gate

`core/runner.py:60` currently states, in capitals, that **nothing may be
written to the console while a flowgraph is running** -- because the stock
template sits in `input('Press Enter to quit: ')` and one stray newline ends
the run. This feature relaxes that invariant, and the relaxation must be
gated, not assumed:

**Control writes are permitted only when the running payload was built by the
Process phase and carries a `ui_spec.json`.** Deploy a hand-written `.py`
(which the tool still supports, and which still has `input()` in it), enable
a slider, and the first `SET` line would end the run. The GUI must therefore
derive the control panel's existence from the payload, not from the state
machine alone: no spec in the payload, no panel, no writes.

The write itself follows the pattern `Runner.terminate()` already
establishes (`core/runner.py:211`): one `transport.write()` of one complete
line, safe from the UI thread, never interleaved mid-line.

### Per-block notes for the parsers and the Tk renderers

- **Range** -- `start`/`stop`/`step`/`value` plus `widget` (counter_slider,
  counter, slider, dial) and `rangeType` (float/int). Tk: `ttk.Scale` +
  `ttk.Spinbox`.
- **Entry** -- carries a `type` of real/int/string/bool/raw with a converter
  attached (`eng_notation.str_to_num`, `int`, `str`, `bool`, `eval`). Mirror
  `eng_notation.str_to_num` on the desktop so `1.5M` means what it means in
  GRC (it is importable there). Stock `bool` conv is Python `bool(str)`,
  which is `True` for any non-empty string including `"False"`; render a real
  checkbox instead and **put that in the report** -- it is a better
  behaviour but it is still a semantic change.
- **Chooser** -- options come from either the `options`/`labels` lists or the
  numbered `option0..4`/`label0..4` fields depending on `num_opts`; two parse
  paths, both trivial. Best-behaved of the five: a closed value set means the
  board can validate exactly.
- **Check Box** -- the `true`/`false` values are arbitrary values of the
  declared type, not necessarily booleans (`self._x_choices = {True: <true>,
  False: <false>}`). Carry both in the spec.
- **Push Button** -- momentary, so it is *two* `set_` calls with the operator's
  press duration between them. Over the serial console that width becomes
  nondeterministic, which is bad for anything driving a reset or a command
  burst. Hence `PULSE <id> <ms>`: the board does pressed -> sleep -> released
  locally so the edge width does not depend on the link. Faithful
  press/release stays available for hold-to-enable buttons.

**Bounds and defaults can be expressions** (`samp_rate/2`), which pure-YAML
parsing cannot evaluate. `ast.literal_eval` covers the overwhelming majority
of real flowgraphs; where it fails, fall back to a free-entry box for that
control and say so in the report. Do **not** reimplement GRC's evaluator for
this.

### The look-alike blocks that are not in scope

gr-qtgui also ships a family that looks like these in the block browser --
**Toggle Switch, Toggle Button, MsgPushButton, MsgCheckBox, DialControl**
(`variable_qtgui_toggle_switch`, `variable_qtgui_toggle_button_msg`, ...).
Those are hybrids: they define a variable **and** declare a message output
port. The variable half would work through this mechanism; the message half
has no headless equivalent at all. The transform must detect `outputs:` with
`domain: message` on a qtgui control and **warn that half its behaviour is
being dropped**, rather than silently converting it as if it were a plain
Check Box. This is the surviving piece of the original "category 3" rule in
the Headless transform section below.

### Deployer GUI

A control panel built from `ui_spec.json` at deploy time, live only in the
`RUNNING` state and only when a spec came with the payload. Values are
per-widget debounced/coalesced -- a dragged slider sends the latest value at
a fixed cadence (~10-20 Hz), not one line per pixel. At ~30 bytes a line that
is well under 1 KB/s against an 11.5 KB/s console, so the control channel is
not a meaningful load next to flowgraph stdout; the coalescing is there to
keep the *log* readable and the setters from being hammered, not to save
bandwidth. Each widget shows its last acknowledged value, and an `ERR` reply
marks that widget rather than just printing into the log.

The CLI gets the read-only half: `--list-controls` prints the spec, and
`--set id=value` applies initial values at launch. Live control from the CLI
is not planned -- that is what the GUI is for.

### Build order — followed as written, all six done

1. Process phase (prerequisite -- `.grc` ingest, preflight, transform, grcc).
2. The two `callbacks:` deletions in the FAU block ymls. Stands alone;
   can land at any time.
3. Spec extraction + `variable` substitution in the transform, with the
   schema and its validation test.
4. `fau_ctl.py` + the snippet injection. Testable end to end against
   `tests/fake_board.py`, which already drives a real pty with a real
   subprocess -- including the Ctrl-C path, which is the part that must not
   be taken on trust.
5. `Runner` control writes + `FAU-CTL` routing; the payload gate.
6. The Tk panel.

Step 4's insistence on a real pty earned its keep twice: it is what caught
the snippet's `tb`/`self` binding and the deploy-directory import.

### Open decisions for this feature — all three settled

- **A. Settled: yes, the C++ setters stay.** QA and `apps/` use them; they
  are simply unreachable from any flowgraph now that the `callbacks:` are
  gone.
- **B. Settled: `PULSE` ships in v1** (user's call). A Push Button sends
  `PULSE <id> <ms>` and the board does pressed -> sleep -> released
  locally, so the edge width does not depend on the link. Faithful
  press/release stays available as the panel's `Hold` button, for
  hold-to-enable.
- **C. Settled: degrade and report**, as recommended -- consistent with how
  the rest of the transform treats a lossy conversion, and carried
  per-control so one unevaluable bound does not cost the operator every
  other slider.


Original status line below ("design agreed, not yet implemented") is
superseded by the above; kept for history.

Status: **design agreed, not yet implemented.** Discussion-only so far; no code
written. This file is the durable record of the design so it can be picked up
later. Companion to the root `CLAUDE.md` (which covers the blocks/PetaLinux
side); this doc covers the **desktop-side deployment tooling**.

## Goal

Let a developer author a flowgraph in **GNU Radio Companion on the desktop**
using the `FAU Modem Source`/`FAU Modem Sink` blocks, then **build on the
desktop and deploy to the board automatically** — where the flowgraph actually
runs, because the blocks open `/dev/mem` and drive AXI DMA and can only
instantiate on the Zynq hardware.

## The three-phase reframe (the mental model)

The request conflates three things that happen in different places. Separating
them is what makes the design fall out:

| Phase | Needs | Where |
|---|---|---|
| **1. Block *presence*** in GRC (see/wire/set params) | just the two `.block.yml` | Desktop — pure YAML, no hardware |
| **2. *Generate* ("build")** the flowgraph → `.py` | `grcc` | Desktop — codegen never imports/instantiates the block |
| **3. *Execute*** the flowgraph | real `fau_modem` module + `/dev/mem` + bitstream | **Board only** — ctor opens `/dev/mem` in the member-init list, so it can't even be *constructed* off-board |

"Build on desktop, export to board" = **author+generate on desktop, deploy+run
on board.** Real DMA execution can never happen on the desktop for these
blocks as written (confirmed: `fau_source_impl` members `d_window`/`d_dma`
open `/dev/mem` in the ctor init list → `hw/mmio.cc:27`).

**Revised 2026-08-31 (supersedes the original "desktop simulation explicitly
out of scope" call above):** a desktop build variant of `fau_source`/
`fau_sink` is now in scope, decided as **no-op pass-through**, not the
rejected null-hardware backend and not a construct-only stub:

- Desktop ctor does **not** open `/dev/mem` — parameters/wiring configure
  freely with no hardware probing.
- `start()` succeeds on desktop (unlike the "refuse cleanly at start()"
  alternative that was considered and passed over).
- `work()` is genuinely no-op: `fau_source` emits zeros/silence, `fau_sink`
  discards — no fake DMA ring, no simulated register/timing behavior. This
  lets a flowgraph run structurally on desktop (wiring/rate/type checks)
  without pretending to model the real hardware.

**Implemented 2026-09-01, and simpler than the line above assumed:** no C++
host build / backend switch — a standalone **pure-Python** `gnuradio.fau_modem`
package, `gr-fau_modem/desktop_noop/`, matching the real blocks' constructor
signature and public method surface (getters/setters/stats all present,
stats hardwired to zero since there's no hardware to fault). Installs via
`pip install -e gr-fau_modem/desktop_noop` into whatever desktop Python runs
GNU Radio; relies on `gnuradio/__init__.py`'s stock `pkgutil.extend_path`
OOT mechanism, so `from gnuradio import fau_modem` resolves to this package
on desktop and the real compiled one on the board with zero code
differences in the generated flowgraph. Verified end-to-end in an isolated
venv: both blocks construct, run through a `gr.top_block`, `fau_source`
emits all-zero samples, `fau_sink` counts consumed samples into
`bds_moved()`, and runtime setters (`set_nco_freq`, etc.) work. See
`gr-fau_modem/desktop_noop/README.md` for install/usage and the explicit
warning not to deploy this package to a board (it would shadow the real
one). **Not done yet:** wiring this into `fau-deploy`'s own preflight (e.g.
warning if the desktop no-op package is what's on path when generating for
board deploy) — no such check exists today.

**Future idea, not yet scoped for V1:** a whitelist of source/sink blocks
allowed to "stick around" when deploying to the board — in practice only
software sources/sinks (`fau_source`/`fau_sink`, file/vector/null blocks,
etc.); any other SDR-hardware source/sink (UHD, RTL-SDR, etc.) would need to
be disabled and stripped from the flowgraph before deploy, the same way GUI
blocks are stripped in the headless transform below. Likely belongs as a new
preflight gate alongside Gate 1/2, not yet designed.

## Phase 1 — blocks in desktop GRC (trivial, prerequisite)

GRC discovers blocks by scanning `.block.yml` on its block path
(`$prefix/share/gnuradio/grc/blocks`, `~/.local/share/...`, `GRC_BLOCKS_PATH`).
It reads YAML; it does **not** import `fau_modem`. So:

- Point `GRC_BLOCKS_PATH` at the submodule's `gr-fau_modem/grc/` dir (or symlink
  the two YAMLs into `~/.local/share/gnuradio/grc/blocks/`). **Symlink at the
  repo checkout — single source of truth**, so the desktop copy can't drift
  from the constructor signature.
- `asserts` in the YAML are evaluated by GRC's own Python at edit time — real
  design-time validation, works on desktop.

Two YAML fixes to make while here:
- `flags: [ python, cpp ]` advertises C++ codegen but there are no
  `cpp_templates` — a C++ generate would break. Drop `cpp` or add templates.
- Add a `documentation:` line stating "executes on the board only" so a Run on
  the desktop's failure is self-explanatory.

## Architecture decision: a standalone, decoupled deployer app

**Decided:** a **separate application** that runs alongside GRC, does **not**
tie into GRC internals. Its entire coupling to GNU Radio is: *invoke the `grcc`
CLI* and *parse the versioned `.grc` YAML*. Both are public and stable
(`.grc` carries `file_format: 1`; `grcc -o DIR file.grc` is a documented CLI).

Front-ends over **one shared core**:
- **CLI mode** — for dev/CI/scripting. Robust, has a real tty.
- **GUI mode** — for demo/interactive. Same core, interactive front-end
  (host/port picker, preflight report, progress, log pane, Stop button).

The "CLI + GUI, both" decision collapses into one app with two modes sharing
everything by construction.

### Rejected alternatives (do not re-litigate without new evidence)

1. **Per-flowgraph `run_command` override** (Options block `Run Command` =
   `fau-deploy {filename}`, verified real at `options.block.yml:122` /
   `FlowGraph.get_run_command`). Zero internal coupling, but a per-flowgraph
   field to manage and weaker teardown control (GRC's Stop = `terminate()` on a
   local wrapper; you'd have to translate that into a remote clean signal).
2. **Patch/replace GRC's `Executor.ExecFlowGraphThread`** via a launcher-shim
   monkeypatch (verified seam: `Application.py:742` constructs it via the module
   attribute *after* `FLOW_GRAPH_GEN()` writes the `.py`; Stop routes through
   `page.process.terminate()` at `Application.py:759`). Gives a real in-GRC
   deploy dialog and directly-owned Stop/teardown, **but couples to a GRC
   internal class** with no stability guarantee. Rejected in favor of full
   decoupling.

The standalone app wins on: lowest coupling (survives GNU Radio upgrades),
best teardown ownership (no GRC lifecycle contract to satisfy), and the CLI+GUI
collapse. **Cost accepted:** no fused-in-GRC "press Run → deploys" button; it's
two windows (GRC authors, the app deploys). GRC's own Run button will try to
run locally and fail — a non-issue; just don't use it (optional signpost: a
`run_command` that prints "use the FAU deployer").

## Transport: text-based UART, not SSH

**Hard constraint:** the board is reached over its **serial console (UART)**,
not a network/ssh. ZModem exists on the board but invoking it from scripts has
proven unreliable; the chosen approach is **base64 chunks + hash validation**
over the console. This swaps only the transport module (a pyserial state
machine) — everything upstream is unchanged. No existing serial/transfer
tooling in the repo (greenfield).

### The defining issue: one shared, lossy channel

Control, payload, runtime stdout, and teardown signals all share **one wire**.
This drives most decisions below (foreground run, in-band teardown, protocol
framing).

## End-to-end pipeline (desktop side)

1. **Ingest** the user `.grc` (copy; never mutate the original).
2. **Preflight validation** (gates below) on the `.grc` YAML.
3. **Headless transform** → derived `flowgraph.headless.grc` in the build dir,
   plus `ui_spec.json` for whatever QT GUI input blocks it converted (2026-09-15
   section).
4. **Generate**: `grcc -o build/ flowgraph.headless.grc` → `.py` (+ hier deps).
   (`grcc -r` runs locally — do **not** use it; we run remotely.)
5. **Compress** the generated set — the `.py` and its local imports, plus
   `fau_ctl.py` and `ui_spec.json` when the transform produced controls
   (gzip; small text, portable `gzip.decompress`
   on board; xz/zstd only if confirmed present). Compression materially cuts
   UART transfer time — see throughput note.
6. **Hash** the *compressed* payload (sha256).
7. **UART transfer** (framed, chunked, ACK'd — protocol below).
8. **Board validates** reconstructed compressed bytes against hash **before
   decompressing**, decompresses, places files.
9. **Run** foreground on the console; stream output; **Stop = `0x03`** + wait
   for halt sentinel.

## Preflight gates (shared core, used by CLI + GUI)

- **Gate 1 — FAU block present.** Scan `.grc` for `fau_modem_fau_source` /
  `fau_modem_fau_sink`. Absent → refuse (policy gate; easy to relax).
- **Gate 2 — every block available on the board.** The board runs a *headless*
  image that likely omits `gr-qtgui`/`gr-audio`/`gr-uhd`/maybe `gr-digital` —
  so "core is safe" is **false**; must check against the image's actual block
  set. Source of truth: **enumerate the board's installed block YAMLs on
  connect** (natural now that we already talk to the board) during dev; later a
  build-time manifest emitted by the PetaLinux build.
- **Target inference.** `fau_sink` → 7010 (TX); `fau_source` → 7020 (RX) — two
  *physically separate* boards. Both present in one flowgraph → **reject**
  (can't run on a single board). Override via explicit port/host selection.
- **generate_options check.** A `qt_gui` flowgraph generates a `.py` importing
  `qtgui`, absent on the headless board. Handle in the transform (below) or
  reject before generating.
- **Version guard** on `.grc` `file_format` so a future format bump fails
  loudly rather than mis-parsing.

## Headless transform (on a *derived copy*, reported)

**Discipline:** transform a copy → `flowgraph.headless.grc`; the user's file
stays pristine; the derived file is an **audit artifact**. Operate at the YAML
level (connections are `[block_id, src_port, block_id, dst_port]` lists —
filter by id). Offer a `--dry-run` that prints the diff and stops.

Headless and "no GUI blocks" are the **same operation**: the `no_gui` generator
has no Qt widget parent, so any leftover `qtgui_*` block breaks generation.
"Make headless" ⟹ set `generate_options: no_gui` **and** remove every `qtgui`
block. Three categories:

1. **GUI sinks — automatic, safe.** `qtgui_time_sink_x`, `freq_sink`,
   `waterfall`, `const_sink`, `number_sink`, `vector_sink`, all-in-one
   `qtgui_sink_x`, layout blocks (`qtgui_tab_widget`). Delete block + its
   connections. A sink is a pure consumer, so removal can leave an **upstream
   output dangling**, which GNU Radio's topology check rejects → **splice a
   `blocks_null_sink`** onto any output the strip orphaned. (Dead `gui_hint`
   params on survivors are ignored under `no_gui`.)
2. **GUI controls that define a variable — automatic + report.** `qtgui_range`
   (slider), `qtgui_chooser`, `qtgui_entry`, `qtgui_check_box`,
   `qtgui_push_button`. They define an
   `id` other blocks reference; can't delete. **Replace each with a plain
   `variable` block at its default value** (per-type field for "current value").
   Don't change semantics silently.

   **Superseded in part (2026-09-15):** the substitution is unchanged, but
   live-adjustability is *not* lost any more -- the replaced control is also
   written into `ui_spec.json` and re-rendered as a widget in the deployer,
   which pushes values to `tb.set_<id>()` on the running board. See "Live
   flowgraph controls" above. The report line changes from "froze range
   'gain' → 0.5, was operator-adjustable" to naming it as a live remote
   control.
3. **GUI controls that emit *messages* — surface, don't auto-guess.**
   `qtgui_msg_push_button` etc. feed a *message port*, not a variable; no
   constant to freeze into. Headless equivalent is "message never fires" — a
   behavior change. **Warn/refuse and let the user decide.** This is the
   "sources are more nuanced" line: variable-defining converts cleanly,
   message-emitting cannot.

Implementation surface: pure-YAML covers strip / flip / constant-swap. The
**null-sink splice** is the one spot wanting port/type awareness — do it
heuristically, or lean on `gnuradio.grc.core` (same lib `grcc` uses; lighter,
versioned coupling than the rejected Executor patch) *only* for correct
rewiring. Start pure-YAML.

Adjacent freebie: GUI flowgraphs usually carry a **Throttle** to spare CPU; on
the board the DMA sets the real rate, so a Throttle is redundant/harmful —
flag it (strip or warn) in the same pass.

After transform: re-run preflight + `grcc` on the derived file; surface any
`grcc` error instead of pushing a broken `.py`.

## UART transfer protocol (harden for a lossy text channel)

End-to-end hash alone forces a **full re-send** on one bad byte — painful at
UART speeds. Add framing so failures are bounded/recoverable:

- **Sentinel framing**: `===FAU-BEGIN <name> <nchunks> <sha256> <total_len>===`
  … chunks … `===FAU-END===`. Lets the receiver ignore console noise
  (boot spew, MOTD) and detect truncation.
- **Per-chunk sequence + ACK**: each chunk carries an index; receiver replies
  `ACK <n>` / `NAK <n>`. Doubles as **flow control** (wait for ACK before next
  chunk → never overrun the tty) **and** single-chunk retransmit. Given
  UART unreliability is the whole reason for this scheme, per-chunk recovery
  earns its complexity.
- **Keep the end-hash** and validate the reconstructed *compressed* bytes
  **before** decompressing (corrupt data → less-useful gzip error otherwise).
- **Disable echo during transfer** (`stty -echo` / receiver puts tty in raw
  mode) — else the deployer parses its own echo tangled with ACKs and doubles
  channel traffic.
- **Don't rely on tty flow control** (XON/XOFF, RTS/CTS often unwired). The
  per-chunk ACK *is* the flow control.
- **Chunk size** well under the tty canonical-line limit (`MAX_CANON` ~4096) —
  512–1024 base64 chars/line.
- **Login/prompt state machine**: from "port open" → detect `login:` → send
  creds → detect shell prompt. Set a **unique `PS1`** for unambiguous prompt
  detection; timeouts + retries. Small but the fiddliest part.

**Throughput note (sets expectations + justifies compress):** 115200 baud ≈
11.5 KB/s raw; base64 inflates 33% → ~8.6 KB/s effective payload. 50 KB `.py`
→ gzip ~10 KB → base64 ~13 KB → **~1.5 s**; uncompressed ~6 s + more chunks to
drop. Raising console baud is possible but means touching bootargs — probably
not worth it.

## Board-side receiver (make it Python)

Python is already on the board (GNU Radio needs it), and `hashlib`, `base64`,
`gzip` are **stdlib** — so a **Python receiver sidesteps every busybox-applet
availability question** (`sha256sum`/`base64`/`gunzip`) and gives real
framing/parsing. Biggest reliability upgrade over a shell receiver.

**Keep its job narrow:** receive → validate → decompress → place files → exit
to shell. Then run via a plain foreground command. Two simple pieces beat one
console-owning daemon that multiplexes protocol + child output + signals. (An
integrated console-owning agent is a possible *future* streamlining but
complicates the clean Ctrl-C teardown below — the child becomes a grandchild
and the agent must forward the signal itself.)

## Run + teardown (the hardware-safety critical path)

> **Superseded in part (2026-09-03)** -- see "GUI, run phase, ..." above.
> Implemented as described except the `===FAU-HALTED===` sentinel, which was
> dropped: the shell's own `echo "FAU-RC-<nonce>:$?"` proves the process was
> reaped (destructors included), which a sentinel printed from inside Python
> does not. No run wrapper was needed either -- grcc already emits the
> correct `tb.stop()`/`tb.wait()` SIGINT handler.
>
> **Amended 2026-09-15:** once the transform sets `run_options: run` for the
> control channel, grcc's handler is no longer correct as-is -- it would call
> `tb.wait()` while the main thread is already inside one. `fau_ctl.start()`
> re-installs SIGINT as `tb.stop()` only. See "Live flowgraph controls"
> above; Ctrl-C as the stop mechanism is unchanged.

Single shared channel makes **foreground run** the right call, and it makes
teardown *more* reliable than ssh, not less:

- Run the flowgraph **foreground** on the console.
- **Stop = send raw `0x03` (Ctrl-C)** → kernel line discipline delivers SIGINT
  to the foreground process group → GNU Radio handler → `top_block`
  stop→wait→dtor. This is the teardown discipline the blocks require (clear
  `DMACR.RS` → poll `DMASR.Halted` → SLCR fabric reset; **never** `DMACR.Reset`
  — see root `CLAUDE.md`). Orphaning a live DMA burst wedges the board (~30
  leaks → power cycle), so this path must be exact.
- **Confirm completion over the lossy channel**: the run wrapper prints a
  sentinel `===FAU-HALTED===` *after* `tb.wait()` returns; the deployer waits
  for that line before declaring the board safe. Don't trust "sent Ctrl-C" ==
  "stopped."
- Background run (`&`) loses the line-discipline signal path (needs PID +
  `kill -INT`, more orphan risk) — **don't**, unless the integrated-agent model
  is adopted later with explicit signal forwarding.

## Board-side prerequisites / assumptions

- **`gr-fau-modem` module already installed on the board** (the `.so` + pybind
  + YAML). Today via `scripts/extract_gnuradio.sh` tarball; **to be
  slipstreamed into the PetaLinux image** later (dev-phase separate install is
  fine). A deployed `.py` is inert without it.
- **Receiver script present on the board.** Bootstrap: one-time manual/paste
  placement during dev; rides the slipstream into the image later.
- The device-tree reserved-memory + XSA-import + 7020 layer work from root
  `CLAUDE.md` steps 1–4 are separate prerequisites for the blocks to `start()`
  at all.

## Forward-compatibility with the slipstream

Slipstreaming the module (and receiver) into the image **changes nothing** in
this deployer: it still ships+runs a `.py`. Only effects, both simplifications:
stop separately pushing the module tarball, and Gate 2's board-block set moves
from live-query to the image build's manifest.

## Open decisions to settle before coding

> **#1, #2 and #6 are settled** -- see "Settled open decisions" in the
> 2026-09-03 section above (Tkinter; explicit `by-id` ports from
> per-session ports with credentials in a separate gitignored file;
> per-chunk CRC32 was
> settled as mandatory back in the 2026-08-31 section). #3, #4 and #5 are
> still open, and #4/#5 are blocked behind the unbuilt Process phase.

1. **Serial specifics**: device path/baud selection UX (auto-detect vs
   explicit), and login credentials handling (prompt vs config vs key file).
2. **GUI toolkit** for the app (GTK to match GRC's stack? Qt? Simple Tk?).
3. **File-watch on the `.grc`** (save → re-preflight → enable Deploy) for a
   live loop, vs. manual "open file → Deploy."
4. **Board profile source** for Gate 2 now: live-enumerate on connect
   (recommended for dev) — confirm the enumeration command/paths.
5. **Null-sink splice**: pure-YAML heuristic vs `gnuradio.grc.core` for rewiring.
6. **Per-chunk hash/CRC** in addition to seq+ACK+end-hash, or is that overkill.

## Suggested layout (when implemented)

A single tool, e.g. `scripts/fau_deployer/` (or a top-level package):
- `core/` — ingest, preflight gates, headless transform, grcc driver,
  compress+hash, serial transport (framing/ACK/login state machine).
- `cli.py` — headless front-end (dev/CI).
- `gui.py` — interactive front-end (demo).  **Built 2026-09-03 (Tkinter).**
- `board/receiver.py` — the Python board-side receiver (shipped to the board;
  eventually into the image).
- `board/fau_ctl.py` — the board-side control dispatcher (2026-09-15 design;
  shipped **per deploy**, alongside the flowgraph, never into the image).
- `json/schemas/ui_spec.schema.json` — the contract between the transform,
  `gui.py` and `fau_ctl.py`.

Everything above `core/serial` is transport-agnostic, so a future network
transport is a drop-in.
