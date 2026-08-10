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
  from. Treat it as the spec, not as code to keep running.
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

All unfenced over `0x00000000`–`0x1FFFFFFF`. **Neither bitstream connects the
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

### 2. Device tree: reserved-memory node (owned by the user, per this session's decision)

Not done this session — deliberately deferred as a "prerequisite you own."
Needed before `fau_source`/`fau_sink` can `start()` without
`allow_unreserved=true` (which is a deliberate escape hatch for exactly this
gap, not a substitute for doing it).

Add to **both** boards'
`project-spec/meta-user/recipes-bsp/device-tree/files/system-user.dtsi`,
**and** the U-Boot copies under
`project-spec/meta-user/meta-xilinx-tools/recipes-bsp/uboot-device-tree/files/system-user.dtsi`
(the U-Boot copy matters — the ~59 MB initrd can get relocated into this
window by `boot_ramdisk_high()` if `initrd_high` is unset):

```dts
/ {
	reserved-memory {
		#address-cells = <1>;
		#size-cells = <1>;
		ranges;

		fau_dma_reserved: fau-dma@1e000000 {
			no-map;
			reg = <0x1e000000 0x02000000>;   /* top 32 MiB of 512 MB DDR */
		};
	};
};
```

Plus, **Linux copies only** (not U-Boot — an unresolved label there is a
hard `dtc` error since the U-Boot DT assembly may not pull in `pl.dtsi`):

```dts
&axi_dma_0 { status = "disabled"; };
```

`no-map` is correct — not primarily for uncachedness (that's `O_SYNC` on the
`open()` call, already handled in `hw/mmio.cc`), but because it keeps the
kernel from creating a cached linear-map alias (a mismatched-attribute
violation on ARMv7), keeps the page allocator out, and removes the range
from `/proc/iomem` as System RAM (which is exactly what
`hw::reserved_window_ok()` checks for). Do **not** use `reusable` or
`compatible = "shared-dma-pool"` — those hand the region to a kernel CMA
driver that doesn't exist here.

`CONFIG_STRICT_DEVMEM` was confirmed **off** in both kernel configs this
session, so no kernel config change is strictly required, but pin it
defensively in `project-spec/meta-user/recipes-kernel/linux/linux-xlnx/bsp.cfg`
(currently 0 bytes, already wired into the bbappend) so a future kernel bump
can't silently break this:

```
CONFIG_DEVMEM=y
# CONFIG_STRICT_DEVMEM is not set
# CONFIG_IO_STRICT_DEVMEM is not set
CONFIG_OF_RESERVED_MEM=y
```

**Verify on target after flashing:**
```
grep -i "system ram" /proc/iomem          # must end at 0x1dffffff
ls /proc/device-tree/reserved-memory/     # fau-dma@1e000000 present
xxd /proc/device-tree/chosen/linux,initrd-start   # must be < 0x1E000000
```
If the initrd check fails, set `initrd_high=0x1dffffff` (and `fdt_high`
likewise) in the U-Boot environment.

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
