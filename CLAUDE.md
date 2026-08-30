# GNU-Radio-FAU-Modem — project notes for Claude

This file tracks implementation state across sessions for the `fau_source`/
`fau_sink` GNU Radio blocks and the PetaLinux build that hosts them. Read
this before touching `components/layers/meta-fau-modem/` or the DMA/device-tree
side of `build_7010/`/`build_7020/`.

## Repo shape

- **Two independent PetaLinux 2024.2 (Yocto scarthgap) projects**, one per
  board: `build_7010/petalinux_7010_os` (Zynq-7010, TX/DAC) and
  `build_7020/petalinux_7020_os` (Zynq-7020, RX/ADC).
- **`components/layers/meta-fau-modem/`** is a **git submodule**
  (`GNU-Radio-FAU-Modem-Blocks`, remote on GitHub) containing the Yocto layer
  and, inside it, `gr-fau_modem/` — the actual GNU Radio OOT module source.
  All C++/Python block work happens inside this submodule; commit there
  first, then bump the gitlink in the superproject.
- **`modem_reference/scripts/`** is the ground-truth Python reference driver
  (bare `/dev/mem` + `mmap`, no kernel driver) that the C++ blocks are ported
  from. Treat it as the spec, not as code to keep running. **A newer copy
  lives outside this repo at `/home/gabeg/repos/UWM-dev/scripts/{tx,rx}/`** --
  prefer it; the DMA-layer files are identical but the higher layers are
  ahead (see the 2026-08-25 re-sync section below).
- **`xsas/`** holds the hardware description files. Current: `m10_dac_iq_v6.xsa`
  (TX, 7010) and `m20_dac_iq_v6.xsa` (RX, 7020 — despite the "dac" in the
  filename, it's the ADC/RX design; MM2S=0/S2MM=1). Older XSAs are in
  `xsas/old/` and do **not** match this hardware map (no SG DMA, different
  GPIO addresses) — never derive addresses from them or from `pl.dtsi`
  (stale, generated from an older XSA).
- **`scripts/extract_gnuradio.sh`** / **`extract_gnuradio_tests.sh`** package
  a headless GNU Radio + `gr-fau-modem` tarball per board from the RPM
  deploy pool, for upload to a live board. This is the deployment path —
  there is no petalinux-build-to-SD-card step in this repo currently.

## Verified hardware register map (from the XSAs, not `pl.dtsi`)

| | TX (`fau_sink`, 7010) | RX (`fau_source`, 7020) |
|---|---|---|
| AXI DMA | `0x40400000`, SG=1, MM2S only, len width 26 | `0x40400000`, SG=1, S2MM only, len width 26, DRE |
| `frame_len` | — (TLAST from BD EOF) | `0x41200000` |
| `tlast_gen_reset` | — | `0x41210000` |
| `dma_dds_select` | `0x41220000` (0=DMA, 1=DDS) | — |
| `axi_gpio_ADCstatus` | — | `0x41230000` (RO) |
| CIC rate GPIO | `0x41240000` (interp) | `0x41240000` (decim) |
| DDS PINC / TVALID | `0x42200000` / `0x42210000` | same |
| DMA masters | MM2S+SG on HP0 | S2MM on HP0, SG on HP1 |

All unfenced over `0x00000000`–`0x1FFFFFFF`. DMA window: **0x1F000000, 16 MiB** (the top 16 MiB, already outside System RAM — see step 2). **Neither bitstream connects the
DMA interrupt** (`mm2s_introut`/`s2mm_introut` have no `SIGNAME`) — polling is
mandatory, there is no dmaengine channel and no `/dev/uio*`.

Sample format: one `uint32` per complex sample, LE, `[15:0]`=I (signed Q15),
`[31:16]`=Q, scale `32767.0`.

This map lives as C++ `constexpr` in
`gr-fau_modem/lib/hw/board_map.h` — that header is the single source of
truth, keep it in sync if the bitstream changes.

---

## What was done this session

Full implementation plan: see the approved plan (this session created
`fau_source`/`fau_sink` from scratch — they were vanilla `gr_modtool` stubs
with `work()` that did nothing and even had the wrong `io_signature`).

### Design decisions (already made, don't re-litigate without new evidence)

- **Tail-bump (non-cyclic) SG ring, not cyclic BD mode**, in both
  directions. Cyclic mode has no hardware interlock at all — underrun
  silently retransmits stale/spliced data, overflow is undetectable.
  Tail-bump gives exact underrun/overrun detection via `DMASR.Idle` at the
  moment of a tail bump. This is the single riskiest unverified assumption
  in the design — see "Bring-up tests" below.
- **Poll inside `work()`**, bounded spin + `clock_nanosleep`
  (`hw/poll.h`). No I/O thread. Never return 0 immediately without a bound
  — that spins the scheduler.
- **TX silence-fill**: a dedicated pre-zeroed buffer stands in for real data
  when the flowgraph can't keep up, so the DAC never holds a stale sample as
  DC. Also solves the startup prefill problem.
- **`memcpy` device→cached staging, then VOLK on staging only.** Never VOLK
  directly against the `/dev/mem` mapping.
- **`work()` never throws** — an exception escaping `work()` calls
  `std::terminate` and leaves the DMA live. Fatal errors dump diagnostics
  and `return WORK_DONE`; only the constructor and `start()` throw.
- **Teardown discipline** (from the RX reference's hard-won lesson): clear
  `DMACR.RS` → poll `DMASR.Halted` → SLCR fabric reset. **Never
  `DMACR.Reset`** — resetting a live burst orphans an AXI transaction on the
  PS HP port, and it takes ~30 leaks to wedge the board until a power cycle.
- **`O_SYNC` on `/dev/mem` is mandatory** (`hw/mmio.h`). On ARM32 a
  `no-map` reserved region is still `pfn_valid()==1`, so without `O_SYNC`
  you get a silently-corrupting cached mapping.
- **`DMASR` error mask must include SG bits 8–10**, not just 4–6 — the
  reference TX driver's mask was missing these and the faults actually seen
  on this hardware are `SGSlvErr`/`SGDecErr`.
- **`DMACR[23:16]` (IRQThreshold) must not be left at 0** — write
  `DMACR_RS | (1u<<16)`, not bare `RS`.

### Files added/changed (all inside the `meta-fau-modem` submodule unless noted)

- `gr-fau_modem/lib/hw/` (new, **not installed/private**): `mmio`,
  `axi_gpio`, `dds_nco`, `cic_rate`, `slcr`, `axidma_regs` (header-only),
  `sg_ring`, `dma_window` (+ `reserved_window_ok()` /proc/iomem check and
  a cross-process `claim` flock), `poll.h`, `board_map.h`, `export.h`.
  `cic_rate`/`dds_nco` are explicitly given default visibility
  (`FAU_MODEM_HW_EXPORT`) so the QA test binaries can link their pure math
  across the `.so` boundary — everything else in `hw/` stays hidden
  (`-fvisibility=hidden` is GNU Radio's OOT default) since it's deliberately
  not public API.
- `gr-fau_modem/apps/fau_ringtest.cc`, `fau_membench.cc`, `fau_dtcheck.cc`
  (new) — standalone bring-up tools, built by `apps/CMakeLists.txt` but not
  packaged. See "Bring-up tests to run on hardware" below.
- `gr-fau_modem/include/gnuradio/fau_modem/fau_source.h`,
  `fau_sink.h` — real public API (was empty stub `make()`).
- `gr-fau_modem/lib/fau_source_impl.{h,cc}`,
  `fau_sink_impl.{h,cc}` — full implementation. `io_signature` was
  backwards on `fau_sink` in the original stub (0 in / 1 float out) — fixed
  to 1 `gr_complex` in / 0 out.
- `gr-fau_modem/grc/fau_modem_fau_{source,sink}.block.yml` — real
  parameters/ports/asserts (were placeholder stubs).
- `gr-fau_modem/python/fau_modem/bindings/fau_{source,sink}_python.cc` +
  `docstrings/*_pydoc_template.h` — hand-updated to match the new
  constructors. **The header-file MD5 hash comment
  (`BINDTOOL_HEADER_FILE_HASH`) must be kept in sync with the actual header**
  — the build's `GR_PYBIND_MAKE_OOT` macro hard-fails (`FATAL_ERROR`) if it
  doesn't match and `BINDTOOL_GEN_AUTOMATIC` is 0. Recompute with
  `md5sum include/gnuradio/fau_modem/fau_{source,sink}.h` after any header edit.
- `gr-fau_modem/lib/qa_fau_{source,sink}.cc` — replaced placeholder Boost
  tests with real tests of the pure `cic_rate`/`dds_nco` math (hardware
  can't be exercised at Yocto build time).
- `gr-fau_modem/python/fau_modem/qa_fau_{source,sink}.py` — hardware
  instantiation tests now gated behind `FAU_MODEM_HW_TEST=1` env var (the
  constructor opens `/dev/mem` for real; can't run in ordinary CI).
- `gr-fau_modem/lib/CMakeLists.txt` — added the missing
  `install(TARGETS gnuradio-fau_modem ...)` (this GNU Radio's `GR_LIBRARY_FOO`
  is registration-only, it does **not** install the library — this was
  blocker #1, see below), linked `Volk::volk`, added `hw/*.cc` sources, added
  `include_directories(${CMAKE_CURRENT_SOURCE_DIR})` so QA tests can
  `#include "hw/..."`.
- `gr-fau_modem/CMakeLists.txt` — added `find_package(Volk REQUIRED)`.
- `gr-fau_modem/COPYING` (new) — GPL-3.0-or-later text, needed once
  `LICENSE` stopped being `"CLOSED"`.
- `recipes-core/gr-fau-modem/gr-fau-modem_git.bb` — `PV="1.0.0"` (was
  unset, defaulting the RPM to `-git-`, which broke `extract_gnuradio.sh`'s
  version glob); `LICENSE`/`LIC_FILES_CHKSUM` fixed from `CLOSED`; replaced
  hardcoded `python3.12` paths with `${PYTHON_SITEPACKAGES_DIR}`; dropped
  `INSANE_SKIP += "file-rdeps"` (was masking blocker #1) and added real
  `RDEPENDS`; `EXTERNALSRC_SYMLINKS=""` to stop `oe-logs`/`oe-workdir`
  reappearing; **`DEBIAN_NOAUTONAME:${PN}` / `:${PN}-dev` = "1"** — found
  during verification: `debian.bbclass` (inherited by `cmake.bbclass`,
  applies to the RPM backend despite the name) auto-renames any package
  whose content is purely a shared library to `lib<name><soname-ver>`,
  which broke `extract_gnuradio.sh`'s package-name glob.
- Untracked `gr-fau_modem/oe-logs`/`oe-workdir` (dangling externalsrc
  build-dir symlinks) and added them to `.gitignore`.
- `MANIFEST.md`, `docs/README.fau_modem` — filled in / fixed (the README's
  import path was wrong: `from gnuradio import fau_modem`, not `import
  fau_modem`).
- **Superproject** `scripts/extract_gnuradio.sh` /
  `extract_gnuradio_tests.sh` — added an `OOT_PKGS=("gr-fau-modem")` array
  and a 4th extraction pass that's **fatal** (not just a warning) if missing,
  since a tarball without the modem blocks defeats the point.

### Two real blockers found and fixed (not hypothetical — hit in the actual build)

1. **`libgnuradio-fau_modem.so` was never installed.** This GNU Radio
   version's `GR_LIBRARY_FOO` CMake macro is a registration-only shim; the
   actual install rule moved to `GR_INSTALL_LIBRARY`. Fixed with an explicit
   `install(TARGETS ...)` in `lib/CMakeLists.txt`.
2. **RPM auto-renaming.** `debian.bbclass`'s `debian_package_name_hook`
   renames any package containing only a shared library (no binaries) to a
   Debian-style name — `gr-fau-modem` was becoming
   `libgnuradio-fau-modem1.0.0-*.rpm`. Fixed with `DEBIAN_NOAUTONAME`.

Both were caught by actually running `petalinux-build -c gr-fau-modem` and
`scripts/extract_gnuradio.sh` end-to-end (not just reading the CMake), then
`readelf`-verifying the shipped `.so`'s `SONAME` matches the Python
extension's `NEEDED` entry, and `nm`-verifying `fau_source::make`/
`fau_sink::make` are exported with the right signatures.

### Build verification performed this session

- Cross-syntax-checked every new `.cc`/`.h` file against the real
  `arm-xilinx-linux-gnueabi-g++` toolchain and the actual GNU Radio/pybind11/
  Boost/VOLK sysroot headers (recipe-sysroot for `gr-fau-modem`), using the
  exact flags recorded in a prior build log (`-mthumb -mfpu=neon
  -mfloat-abi=hard -mcpu=cortex-a9`, `-DSPDLOG_FMT_EXTERNAL` etc.) — **not**
  just `-fsyntax-only` on host.
- Ran `petalinux-build -c gr-fau-modem` for real (7010 project) through
  `do_package_qa`/`do_package_write_rpm`. Hit and fixed both blockers above.
- Extracted the built RPM directly (`rpm2cpio | cpio`) and confirmed
  contents: the `.so`, both `.block.yml`, the pybind `.so`, `__init__.py`.
- Ran `scripts/extract_gnuradio.sh` for real: 7010 packages `gr-fau-modem`
  cleanly into the tarball with a passing dependency-closure check; 7020
  correctly fails only on the known missing-layer issue (see below).
- `readelf -d` on the shipped Python extension confirms
  `NEEDED libgnuradio-fau_modem.so.1.0.0` exactly matches the shipped
  library's `SONAME`.
- `nm -DC` confirms `gr::fau_modem::fau_source::make(double, double, int,
  int, bool, bool)` and `fau_sink::make(double, double, int, int, int, bool,
  bool, bool)` are exported with the designed signatures.

**Not yet done: nothing has run on actual Zynq hardware.** All verification
above is host-side (compiling for ARM, not executing on it). Nothing has
been committed to git either (submodule or superproject) — the user did not
ask for a commit this session.

---

## Re-sync against the newer reference scripts (2026-08-25 session)

`/home/gabeg/repos/UWM-dev/scripts/{tx,rx}/` is a **newer copy** of the same
reference driver set as `modem_reference/scripts/`. Diffed both trees this
session: `dma_tx_sg_16m.py`, `dma_rx_sg_16m.py`, `bpsk_dma.py` and
`rx_iq_sg_capture_frame_ids.py` are byte-identical, so the verified register
map, arming order and teardown discipline above all still hold. The only
deltas were `--tx-scale` on the TX path and demod/EVM work on the RX path
(the latter lives above the DMA layer and is out of scope per the last
section). Prefer the `UWM-dev` copy as the reference from here on.

Everything below was cross-checked line by line against those scripts and is
built and packaged (`petalinux-build -c gr-fau-modem` through
`do_package_qa`/`do_package_write_rpm`, RPM extracted and symbol-checked).
**Still nothing has run on Zynq hardware.**

### Behaviour brought over from the reference

- **`fau_sink` gained `tx_scale`** (`--tx-scale` in
  `tx_iq_sg_cyclic_frame_ids.py` / `frame_ids_tx.py`): a linear gain folded
  into the Q15 conversion scale, so it costs nothing extra.
  `volk_32f_s32f_convert_16i` saturates, which is what makes `tx_scale > 1`
  a clip rather than a phase inversion, matching the reference's
  `np.clip` in `pack_q15`. Runtime-settable (`set_tx_scale()`); the block
  counts saturated Q15 components and exposes them as `clipped()`, the
  streaming equivalent of the reference's `clipped_frac`.
- **`fau_source` splits `malformed()` into `acquisition_drops()` +
  `stream_drops()`**, the same distinction `program_s2mm_capture()` makes:
  one short packet before the first good buffer is the ADC datapath being
  mid-frame at arm time and is benign; anything after that is a real
  frame_len/bd_samples divergence.
- **Stall watchdog on both blocks** (`poll_timeout`, default 20 s, the
  reference's `--poll-timeout`). Exists because the engine can wedge with
  *no* DMASR error bit set -- the error-bit check alone polls straight past
  that. Raised at arm time to at least 3 BD periods so a low sample rate
  with a large `bd_samples` can't false-positive. 0 disables. On trip:
  dump + `WORK_DONE`.
- **Ring-integrity watchdog on both blocks**, 250 ms cadence, matching the
  reference's mid-capture `_verify_ring()`. Unlike the reference (whose job
  was to diagnose) this one is **fatal**: a drifted `BUFADDR` means the
  engine is about to read/write memory it does not own.
- **`sg_ring::verify()` widened** from NXTDESC-only to NXTDESC +
  NXTDESC_MSB + a BUFADDR range check, and now runs **unconditionally at
  arm** (not just under `verbose`) as the reference's BASELINE check --
  which distinguishes a stale/incoherent descriptor mapping from a runtime
  stray write. `sg_ring::dump()` gained the reference's `CURDESC valid ring
  slot` verdict and per-BD `BAD_BUF` flag, and `fatal()` now dumps
  unconditionally (the state is frozen only at the fault instant).
- **`bd_samples * 4` is now validated against the 26-bit SG length field**,
  as `_write_bd()` does.
- `sg_ring::config` fields got default member initializers --
  `apps/fau_ringtest.cc` declares one and assigns field-by-field, so the new
  `buf_region_bytes` would otherwise have been stack garbage.

### Build-system trap fixed while verifying

`GR_PYBIND_MAKE_OOT` declares the docstring-template -> `*_pydoc.h` copy
with an OUTPUT of `docstring_status` and **no DEPENDS**, so once that file
exists the copy never runs again. Under `externalsrc` (build tree persists)
editing a `*_pydoc_template.h` silently does nothing and the *next* build
fails on an undeclared `__doc_*` symbol. Hit this for real. Fixed in
`python/fau_modem/bindings/CMakeLists.txt` with an
`add_custom_command(OUTPUT ... APPEND DEPENDS <templates>)`.

Remember `md5sum include/gnuradio/fau_modem/fau_{source,sink}.h` ->
`BINDTOOL_HEADER_FILE_HASH` after every header edit (both were updated).

### Three wrong hypotheses on the silent-DAC bug (kept so they are not retried)

All three were investigated on hardware and are DISPROVEN. The root cause is
the section below. Recorded because each looks plausible from the code.

1. **GPIO `TRI` reset out from under us.** Wrong. Verified against
   `xsas/m10_dac_iq_v6.xsa` (`SDUAM.hwh`): all four TX AXI GPIOs
   (`Phase_inc_reg_tdata`, `Phase_inc_reg_tvalid`, `dma_dds_select`,
   `interpolator_gpio`) expose **only `gpio_io_o`** -- no `gpio_io_t`, no
   `gpio_io_io`. There is no tristate buffer, so `TRI` is a no-op on this
   bitstream and the DATA register drives the fabric net directly.
   `cic_rate`/`dds_nco` still write TRI before DATA, because that is what the
   reference drivers do and it costs one register write, but it fixes nothing.

2. **The SLCR fabric reset wedging the DAC CDC FIFO.** Unproven and almost
   certainly not it -- removing the fabric reset from the TX path changed
   nothing on hardware. The observation behind it is real and worth keeping:
   `clk_wiz_DAC.resetn` is wired straight to `FCLK_RESET0_N`, so an SLCR
   fabric reset does cycle the whole 10 MHz DAC clock domain including the
   CDC FIFO into `AXIS_S_to_AD9764_0`. `fau_sink` therefore uses
   `DMACR.Reset` (what the TX reference has always done) rather than the
   fabric reset -- justified as the narrower hammer, NOT as a fix.

3. **"`set_output_multiple()` guarantees `work()` sees a full block."**
   Wrong, and this one WAS the bug -- see below.

Facts the XSA established along the way, worth not re-deriving:

- The GPIOs' `s_axi_aresetn` is `rst_ps7_0_100M_peripheral_aresetn` <-
  `FCLK_RESET0_N`, so a fabric reset DOES clear their DATA registers.
  `fau_source` still fabric-resets, so it MUST reprogram NCO/CIC/frame_len
  after every arm.
- There is **no `axis_tlast_gen` and no frame_len/tlast GPIO on the TX board
  at all** -- `fau_sink` is right not to touch 0x41200000/0x41210000. TLAST
  comes from the BD EOF flag.
- `axis_mux_2x1_0.sel <- dma_dds_select_gpio_io_o` (s0 = DMA, s1 = DDS), and
  `m_tready <- AND(cic_compiler_I/Q.s_axis_data_tready)`. The MM2S channel's
  backpressure comes from the CICs, **never from the DAC FIFO** -- so BDs
  retiring at the correct rate says nothing about whether anything reaches
  the DAC. This is why every DMA-side statistic looked perfect throughout.

### ROOT CAUSE (confirmed on hardware): fau_sink required a whole BD per work() call

Confirmed by the data/silence split, at the default `bd_samples=8192`:

    BDs moved: 19811 (data: 0, silence: 19811)   <- flat DAC
    BDs moved:  5320 (data: 5316, silence: 4)    <- --bd-samples 1024, sine on the scope

`work()` was being called continuously, but always with
`noutput_items < bd_samples`, so the real-data loop never ran one iteration
and the prefill top-up filled the ring with silence forever. Every DMA-side
statistic looked perfect while the DAC transmitted zeroes.

**The mistaken assumption:** that `set_output_multiple(bd_samples)`
guarantees `work()` sees a nonzero multiple of it. It does **not** for a
sink. GNU Radio applies that rounding to a block's own OUTPUT buffers; a
sink has none, and `block_executor`'s sink branch offers whatever the
upstream buffer happens to hold. (The 3.10.12 source in this tree *does*
round in the sink branch -- the board runs 3.11, which does not. Do not
verify runtime behaviour against the 3.10 tree in `build_*/`.)

**Fix:** `fau_sink::work()` now accepts ANY `noutput_items` and carries a
partial conversion across calls in `d_stage_fill`, posting a BD only when
`d_stage` is full. `set_output_multiple()` is gone from `fau_sink` (it never
constrained the sink and only oversized the upstream buffer). `d_stage_fill`
resets at every arm so a fragment of the old stream can't splice into the new
one. `fau_source` keeps `set_output_multiple` -- for a source it IS honoured
(`min_available_space()` rounds down to it and returns 0 -> BLKD_OUT rather
than calling `work()` short), so the source never had this bug.

**Never assume a sink's `work()` gets a full block. Accumulate.**

### Process lessons from this bug

What resolved it was instrumentation, not analysis. Splitting `bds_moved()`
into data vs silence turned an unfalsifiable symptom into a one-line answer.
Two things had made that impossible for three rounds:

- `bds_moved()` counted silence BDs, so "BDs moved: 1233, underruns: 0" was
  equally consistent with complete success and with total failure.
- `examples/` and `apps/` were never installed or packaged, so `tx_sine.py`
  could not be refreshed by redeploying (every board copy was a hand-scp,
  and a stale one reported through old code paths) and `fau_ringtest` -- the
  test this file names as the gate on the whole tail-bump design -- could not
  be run at all. Both now ship; see `apps/CMakeLists.txt`,
  `examples/CMakeLists.txt` and `FILES:${PN}` in the recipe.

**When a symptom is consistent with two opposite causes, add the measurement
that separates them before proposing a third hypothesis.**

**Do not verify GNU Radio runtime behaviour against the 3.10.12 source tree
in `build_*/`** -- the boards run 3.11, and the sink branch of
`block_executor` differs in exactly the way that mattered here.

### Still deliberately NOT done from the reference

TX-side `frame_len`/`tlast_gen` GPIOs (0x41200000/0x41210000) exist in
`bpsk_dma.py` but the IQ TX path never calls them -- TLAST comes from the BD
EOF flag, so `fau_sink` correctly does not touch them. The reference's
64 KiB `SG_SEGMENT_BYTES` split is its own choice, not a hardware limit; one
BD per `bd_samples` is equivalent as long as `bd_samples == frame_words`.
RX gain (`gain-control/dac7512.py`) is still item 5 below.

---

## Example scripts generate continuously, they do not replay buffers (2026-08-26)

`tx_chirp.py` and `tx_sine.py`'s phase path both used to precompute a whole
period into a `blocks.vector_source_c` and replay it. Both now synthesise on
the fly with stock C++ blocks, so memory is O(1) and no `--period` /
`--phase-period` bound is needed. **Do not reintroduce a precomputed buffer
into these scripts.**

- **Chirp**: `analog.sig_source_f(GR_SAW_WAVE)` carrying instantaneous
  frequency in **Hz** (amplitude = span, offset = the starting edge, so a
  negative span is a down-chirp with no special casing) ->
  `frequency_modulator_fc` with `sensitivity = 2*pi/samp_rate`, which is
  exactly what makes the sawtooth read as Hz -> `multiply_const_cc` for
  amplitude (the FM block always emits unit magnitude).
- `tx_chirp.py`'s CLI is **`--bandwidth` / `--direction {up,down}` /
  `--period`**, not a start/stop pair: the band is always centred on `--nco`,
  and `sweep_edges()` turns that into the `(f_start, f_stop)` baseband offsets
  the sawtooth needs. `--bandwidth` is capped at `--samp-rate` (the sweep
  reaches +/- bandwidth/2 at baseband), and the banner warns when
  `nco - bandwidth/2` goes below DC, where the low end folds back up as its
  mirror instead of continuing down.
- GR's saw is `offset + ampl*(phase/2pi) + ampl/2` and its NCO starts at
  phase 0, i.e. **mid-ramp**. `saw.set_phase(-math.pi)` puts the first sample
  at the starting edge. Verified present in the board's GR 3.11 pybind module;
  wrapped in `try/except AttributeError` anyway since it is cosmetic and only
  affects the first sweep.
- The FM phase accumulator never resets, so **the repeat seam is
  phase-continuous for free** -- only frequency steps there. This deletes the
  `wrap_phase_deg` warning the precompute version had to print.
- **Phase staircase** (`tx_sine --phase-shift`): `vector_source_c` now holds
  only the 2 or 4 distinct phasors, and `blocks.repeat(sizeof_gr_complex,
  hold)` stretches each across the hold. `--phase-period 2` at 400 ksps went
  from a 3.2 Msample / 25 MiB pattern to 4 stored complex numbers.

Measured on the host (GR 3.10.1.1; the block semantics used here are
unchanged in 3.11, and `repeat`, `frequency_modulator_fc`,
`sig_source<float>` and `set_phase` were all `nm`/binding-confirmed in the
board's `libgnuradio-{analog,blocks}.so.3.11.0git`):

- sweep span is exact every sweep (`min/max` land on the requested band
  edges), and 99.9% of samples are within ~0.5 Hz of the ideal ramp, checked
  up/down, from DC, at the full-rate band limit, and at two sample rates.
- `sig_source_f`'s float NCO makes one sweep 4000 or 4001 samples instead of
  exactly 4000 -- **~3 ppm of slip in when the sweep restarts** (6 samples
  over 2000 sweeps). The sweep itself is never distorted; only the repeat
  boundary walks. Harmless for a free-running chirp, but it means the closed
  form is still the right tool if a sweep ever has to align to an external
  time reference.

These scripts depend on nothing outside stock GNU Radio plus the already
shipped `fau_tx_common.py` and the unchanged `fau_sink` API, so a revised
`tx_chirp.py` can be dropped onto a board **without redeploying the tarball**
-- scp it next to `fau_tx_common.py` in
`/usr/share/gnuradio/fau_modem/examples/`.

WAV/file playback needs no new machinery either: `blocks.wavfile_source(...,
repeat=True)` is already streaming. `modem_reference`'s `wav_tx.py` frames +
BPSK-modulates above the DMA layer, which stays out of scope (see the last
section).

## What's left, in order

### 1. Bring-up tests on hardware (before trusting the blocks at all)

Three tools already exist in `gr-fau_modem/apps/`, built but not packaged
(not in any `FILES:${PN}`). Deploy the RPM, then run manually as root:

1. **`fau_dtcheck`** — checks whether the DMA window
   (`0x1E000000`, 32 MiB) is excluded from `/proc/iomem` as System RAM.
   Will report FAIL until the device-tree work in step 2 below is done.
2. **`fau_membench`** — `memcpy` throughput into/out of the DMA window.
   Must exceed 10 MB/s (4 B/sample × 2.5 MSPS worst case). This gates the
   whole Q15 conversion design.
3. **`fau_ringtest`** (TX board only) — **the test that gates the tail-bump
   design.** Posts a small ring, confirms `STATUS.Cmplt` is set by hardware
   for MM2S (not just S2MM, which the reference only used), confirms the
   engine reports `DMASR.Idle` and halts cleanly at the tail instead of
   re-fetching a completed BD, and confirms bumping `TAILDESC` again after
   reclaiming resumes transfer without a `DMACR.RS` toggle. **If this
   fails**, the fallback is cyclic BD mode with 2N shadow buffers — do not
   patch around a `fau_ringtest` failure, redesign around it.

All three have `allow_unreserved`-equivalent behavior built in (they warn
but continue if the DT reservation isn't there yet, since they need to run
*before* that prerequisite exists to prove it's needed).

### 2. Device tree: RESOLVED by moving the window, no DT change needed

Observed on the 7010 board (`sudo cat /proc/iomem`, and it MUST be sudo -- see
below):

    00000000-1effffff : System RAM

One System RAM range, ending at `0x1EFFFFFF`. So `0x1F000000-0x1FFFFFFF`, the
top 16 MiB, is already carved out of kernel-managed memory, and there is no
fragmentation anywhere else. That is byte-for-byte the window the RX reference
driver uses (`dma_rx_sg_16m.py`: `S2MM_BUF_PHYS = 0x1F000000`,
`S2MM_BD_PHYS = 0x1FF00000`) and calls "the only region actually carved out of
kernel RAM on this board".

`dma_layout` previously asked for **32 MiB at 0x1E000000**, which straddled the
boundary: its lower half sat inside System RAM, so `reserved_window_ok()`
refused to arm and every run needed `allow_unreserved` -- i.e. the DMA writing
into kernel-managed pages. **The window was simply in the wrong place.** It is
now `WINDOW_PHYS = 0x1F000000`, `WINDOW_SIZE = 0x01000000`, which makes the
derived offsets land the ring at `0x1FF00000` and buffers at `0x1F000000`,
identical to the reference. No `system-user.dtsi` edit, no device-tree rebuild,
no `BOOT.BIN` regeneration.

Budget: 15 MiB of buffer region. `fau_sink` at the defaults needs
`(16+1) * 8192 * 4` = 544 KiB, so this is not a constraint. The GRC asserts
were updated from 31 MiB to 15 MiB to match.

**UNVERIFIED, and the one thing left to confirm:** `/proc/device-tree/
reserved-memory/` contains a node named `buffer@0x0E00000`. Node names are
cosmetic (and this one is hand-written -- a real DT unit-address has no `0x`
prefix), so it may simply be mislabelled. If its `reg` really is at
`0x0E000000`, then something else trims the top 16 MiB and our window may be
sharing a region that node owns. `tx_sine.py`'s preflight now parses every
reserved-memory child's `reg`/`no-map` and says whether our window is inside
one, so the next run answers this. Watch for it.

**`/proc/iomem` must be read as root.** Linux zeroes every address for a reader
without `CAP_SYS_ADMIN`, so an unprivileged `cat` shows
`00000000-00000000 : System RAM` and looks like a clean window. That also used
to defeat `reserved_window_ok()` silently -- degenerate ranges overlap nothing,
so the guard passed. It now detects the all-zero case and fails as
unverifiable, and the "cannot open /proc/iomem" path went from fail-open to
fail-closed. Never name-match device-tree nodes either: the old preflight
looked for a node literally called `fau-dma`, reported "ABSENT", and was
technically true and completely useless.

If a future board genuinely needs a different window, the node to add to BOTH
boards' `system-user.dtsi` (and the U-Boot copy under
`meta-xilinx-tools/recipes-bsp/uboot-device-tree/`) is:

```dts
/ {
	reserved-memory {
		#address-cells = <1>;
		#size-cells = <1>;
		ranges;

		fau_dma_reserved: fau-dma@1f000000 {
			no-map;
			reg = <0x1f000000 0x01000000>;
		};
	};
};
```

`no-map` is correct: it keeps the kernel from creating a cached linear-map
alias (a mismatched-attribute violation on ARMv7), keeps the page allocator
out, and removes the range from `/proc/iomem` as System RAM. Do **not** use
`reusable` or `compatible = "shared-dma-pool"` -- those hand the region to a
CMA driver that does not exist here. `CONFIG_STRICT_DEVMEM` was confirmed off
on both boards.

### 3. Import the current XSAs into both PetaLinux projects

Not done this session. `project-spec/hw-description/system.xsa` in both
projects still reflects an **older** XSA generation than
`xsas/m10_dac_iq_v6.xsa`/`m20_dac_iq_v6.xsa` (the ones this session's
register map was verified against). `pl.dtsi` is correspondingly stale.

```
petalinux-config --get-hw-description=<abs path to .xsa> --silentconfig
petalinux-build -c device-tree -x do_cleansstate && petalinux-build -c device-tree
petalinux-build -c uboot-device-tree -x do_cleansstate
petalinux-build -c u-boot-xlnx -x do_cleansstate
petalinux-build -c fsbl-firmware -x do_cleansstate
petalinux-build && petalinux-package --boot --fsbl --fpga --u-boot --force
```

Do the reserved-memory dtsi edit (step 2) **before** this, so the
regenerated device tree picks it up in the same pass. Do **not**
`-x mrproper` — it destroys the sstate cache. `CONFIG_SUBSYSTEM_FPGA_MANAGER`
is unset on both boards, so the PL loads from `BOOT.BIN` via FSBL — a stale
`BOOT.BIN` on the SD card is the most likely way this silently doesn't take.

### 4. Fix the 7020 layer gap

Confirmed again this session: `build_7020/petalinux_7020_os/build/conf/bblayers.conf`
does not include `meta-fau-modem` (7010's does), even though 7020's
`petalinuxbsp.conf` already has `IMAGE_INSTALL:append = " gr-fau-modem"`.
This is exactly why `extract_gnuradio.sh` correctly failed to find
`gr-fau-modem` for board 7020 this session.

Fix: `build_7020/petalinux_7020_os/project-spec/configs/config`, set
`CONFIG_USER_LAYER_1` to the absolute path of `components/layers/meta-fau-modem`
(mirroring 7010's config), then `petalinux-config --silentconfig` to
regenerate `bblayers.conf` (don't hand-edit it, it's regenerated on every
configure).

### 5. RX gain (explicitly deferred this session, not started)

`fau_source` has no gain control today. The reference
(`modem_reference/scripts/gain-control/dac7512.py`) drives a DAC7512 on PS
SPI1 (`0xE0007000`) after ungating its clocks via SLCR, setting the AD8334
VGA gain voltage on all four ADC channels at once (0–1.0 V, ~50 dB/V slope).

When picked up, mirror `hw::slcr`/`hw::mmio` style: a small `hw::dac7512`
class (`set_vga_gain_volts(double)`, `power_down()`), SPI mode 1
(`CPOL=0, CPHA=1`), manual CS + manual start, 16-bit frame with CS held low
across both bytes. Expose as a runtime-settable `rx_gain` parameter on
`fau_source` (genuinely runtime-safe — one SPI transaction, no DMA
involvement, unlike `set_samp_rate`). Before wiring it in: **check the
reference's docstring says Zynq-7010, but gain logically belongs on the RX
board (7020)** — resolve that discrepancy against the actual schematic
before trusting the address. Also decide `gain_backend: {auto, devmem,
spidev}` — if a Cadence SPI kernel driver (`CONFIG_SPI_CADENCE`) ever gets
bound to SPI1, banging its registers from `/dev/mem` would race with it;
`auto` should prefer `/dev/spidev1.1` if it exists.

### 6. On-hardware acceptance tests (after 1–4 above)

- RX loopback: `fau_source → file_sink`, confirm `overruns()==0` and
  `malformed()==0`, cross-check the raw capture against
  `modem_reference/scripts/rx/rx_iq_sg_capture_frame_ids.py --analyze-only`.
- TX↔RX equivalence: replay the reference's `tx_iq_sg_cyclic_frame_ids.py`
  payload through `file_source → fau_sink` on the 7010, capture on the
  7020, confirm the same BER/frame-ID sequence as the pure-Python path.
- Restart robustness: `tb.start(); tb.stop(); tb.wait(); tb.start()` in one
  process, 30× in a loop — no `SGSlvErr`/`SGDecErr` escalation. This is the
  specific failure mode the teardown discipline (see above) exists to
  prevent, and it only shows up after many runs.

## Explicitly out of scope (do not implement without being asked)

The reference's `frame_ids` marker scheme, `bpsk_tx.py`/`bpsk_rx.py`
demod, preamble/CFO/FDE search, and CRC/BER analysis all belong above this
DMA layer as ordinary GNU Radio blocks or a separate flowgraph — not inside
`fau_source`/`fau_sink`.
