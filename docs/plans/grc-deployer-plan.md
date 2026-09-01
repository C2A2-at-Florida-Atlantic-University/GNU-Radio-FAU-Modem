# Plan: FAU GRC Deployer (desktop → board over UART)

Status: **transport-only V1 slice implemented and tested (2026-08-31), not
yet run against real hardware.** Everything below this line is the original
design record; see the "Implementation status" section immediately below for
what actually exists in `scripts/fau_deployer/` today and how it deviates
from (or confirms) this doc. Companion to the root `CLAUDE.md` (which covers
the blocks/PetaLinux side); this doc covers the **desktop-side deployment
tooling**.

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
  it exited -- they'd never reach the shell at all. Fixed by waiting for the
  shell's PS1 marker to reactually reappear before proceeding.
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
- Needs a real desktop build target for `gr-fau_modem` (host toolchain, no
  ARM sysroot) with a backend switch between the real `hw::` path and this
  no-op path — implementation TBD when this work item is picked up.

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
3. **Headless transform** → derived `flowgraph.headless.grc` in the build dir.
4. **Generate**: `grcc -o build/ flowgraph.headless.grc` → `.py` (+ hier deps).
   (`grcc -r` runs locally — do **not** use it; we run remotely.)
5. **Compress** the generated set (gzip; small text, portable `gzip.decompress`
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
   (slider), `qtgui_chooser`, `qtgui_entry`, `qtgui_check_box`. They define an
   `id` other blocks reference; can't delete. **Replace each with a plain
   `variable` block at its default value** (per-type field for "current value").
   Live-adjustability is inherently lost headless → **report** each ("froze
   range 'gain' → 0.5, was operator-adjustable"). Don't change semantics
   silently.
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
- `gui.py` — interactive front-end (demo).
- `board/receiver.py` — the Python board-side receiver (shipped to the board;
  eventually into the image).

Everything above `core/serial` is transport-agnostic, so a future network
transport is a drop-in.
