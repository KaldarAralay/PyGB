# GBemu Next Stages

Date: 2026-05-23

This document is the active roadmap. Historical Stage 1 CPU evidence is preserved in `docs\stage1-audit.md`; current compatibility evidence is in `docs\compatibility.md`; the Pan Docs subsystem inventory is in `docs\pandocs-inventory.md`.

## Current State

GBemu is now a playable DMG-first emulator for the current primary real-ROM target, Pokemon Red, with Super Mario Land added as a quick early-action performance smoke target. It has a verified CPU core, common mapper support, strict selected PPU regression coverage, live Tkinter display, live Windows audio, deterministic WAV capture, rolling frame/audio profiling, and a minimal CGB foundation with forced CGB-only startup identity plus Crystal first-frame/window-startup/render smoke coverage.

The project is not cycle-perfect and not yet a broad commercial compatibility emulator. The strongest next work is to keep adding evidence while improving accuracy and tail-latency behavior.

Latest inventory update: the codebase has been compared against the major Pan Docs areas. Current DMG execution, common memory/cartridge behavior, selected PPU behavior, input, runtime, functional audio, and CGB foundation startup/registers/banking/window ordering plus first-pass BG/window palette/tile-attribute rendering and OBJ palette/priority rendering are in place. The largest remaining gaps are full pixel FIFO completeness, stricter APU suite/analog accuracy, broader commercial compatibility, CGB timing/DMA/boot behavior, and real link/SGB/peripheral behavior.

## Completed Milestones

### Stage 1: CPU, Bus, And Test Harness

Status: complete and preserved as a regression gate.

Done:

- ROM loading, cartridge header parsing, optional boot ROM mapping, and one-way `FF50` unmapping.
- Full documented SM83 opcode coverage for non-CB and CB-prefixed instructions.
- Stack/control flow, interrupts, HALT bug, STOP wake behavior, STOP divider/timer freeze behavior, and serial output for Blargg-style tests.
- Cycle-aware bus integration for opcode/immediate fetches, memory reads/writes, internal cycles, stack operations, interrupts, timers, serial, and OAM DMA.
- `scripts\verify_cpu.py` for repeatable CPU verification.

### Stage 2: PPU And Framebuffer

Status: strong selected-gate coverage, not complete FIFO hardware emulation.

Done:

- LCD modes, `LY`/`STAT`, VBlank, line-153 wrap, DMG STAT write quirk, and LCD enable/disable behavior.
- DMG background, window, and sprite rendering with palette mapping, scroll wrapping, sprite priority, 8x16 selection, flips, OBP selection, and 10-sprites-per-line selection.
- First-pass CGB BG/window rendering with BG palette RAM colors, tile-map palette attributes, tile VRAM-bank selection, X/Y flips, RGB framebuffer output, and Tk/PPM/BMP RGB export support.
- First-pass CGB OBJ rendering with OBJ palette RAM colors, OBJ palette attributes, OBJ tile VRAM-bank selection, CGB OAM-order priority, `FF6C`/OPRI DMG-style priority selection, BG priority attributes, and LCDC.0 priority behavior.
- PPU-mode CPU access restrictions for VRAM/OAM.
- OAM DMA bus blocking and line/sprite visibility effects.
- Selected mode-3 timing model for SCX, SCY, WX, window enable/disable, LCDC source changes, OBJ enable/disable, OBJ size changes, palette writes, sprite/window stalls, and tile-data fetch-boundary cases.
- Frame dumps through PPM/BMP and live Tkinter display output.
- `dmg-acid2` reference-image regression and strict selected external PPU gate through `scripts\verify_ppu.py --strict`.
- Pokemon Red PyBoy visual/OAM oracles for Oak's Lab encyclopedia sprites and the sprite-heavy saved-game scene.

Remaining:

- Full per-dot FIFO behavior.
- Broader raster-effect edge cases outside the selected strict gate.
- Additional PPU ROM suites beyond current Mooneye/Mealybug coverage.

### Stage 3: Runtime, Input, Display, And Pacing

Status: usable for real gameplay.

Done:

- `Emulator` orchestration for cartridge, bus, CPU, PPU, APU, buttons, frame stepping, reset, and save state lifecycle.
- CLI frame/instruction limits, stop conditions, static held buttons, frame/audio dumps, and trace output.
- Joypad `FF00` matrix, selected-line interrupts, held-button non-retriggering, and STOP wake.
- Tkinter window mode with keyboard input, pause/reset/trace/audio toggles, frame pacing, and live audio.
- Tkinter startup now presents the host window before long first-frame emulation work, with optional `--profile-startup` diagnostics for CGB startup debugging.
- Rolling window profiler with run/draw/audio timing, CPU/bus/PPU/APU stats, audio queue range, worst-frame spike fields, and coarse spike cause attribution.
- Automated Pokemon Red headless performance gate for fixed text, sprite-heavy, and sprite-heavy-with-audio scenarios, including parsed metrics and deterministic frame/instruction/cycle drift checks.
- Super Mario Land early-1-1 action performance gate with fixed headless metrics, headless audio sample-drop checks, and live `window-profile` capture validation.
- Pokemon Red frame pacing round 1: worst non-startup live windows now clear 50 fps with zero audio underruns/drops in the latest profile.

Remaining:

- Add more saved window-profile fixtures and scripted input/playback scenarios for live gameplay.
- Add more scripted input/playback scenarios for menus and gameplay.
- Consider optional alternate frontends later if Tkinter becomes the limiting factor.

### Stage 4: Cartridge Hardware

Status: common mapper base is solid.

Done:

- ROM-only and ROM+RAM.
- MBC1, including RAM enable/disable, RAM banking mode, large-ROM fixed-bank behavior, advanced-mode lower ROM area banking, and MBC1M detection/banking.
- MBC2 ROM banking and internal 512x4-bit RAM.
- MBC3 ROM/RAM banking, 0-7 RAM bank selection, 64 KiB RAM support, smaller-RAM wrapping, RTC latch/timekeeping/halt/day carry behavior, and `.sav.rtc` sidecar persistence.
- MBC5 ROM/RAM banking and rumble-control bit behavior.
- HuC1 ROM/RAM banking and basic IR state.
- Header-driven mapper dispatch and unsupported cartridge warnings.

Remaining:

- Specialty mappers/hardware: MMM01, MBC6, MBC7 sensor behavior, Pocket Camera, Bandai TAMA5, HuC3.
- More commercial ROM mapper/save regression cases.

### Stage 5: APU And Audio

Status: functional audio exists and is regression-tested for determinism; analog accuracy is still early.

Done:

- `NR52` power control, register model, DAC-gated activity, trigger handling, length counters, envelopes, CH1 sweep, pulse/wave/noise timers, CH3 wave RAM access behavior, CH3 playback delay, CH4 clock-shift stop behavior, raw channel samples, signed DAC output, `NR50`/`NR51` mixing, initial high-pass filtering, bounded sample buffering, and WAV writing.
- Live Windows audio output through waveOut.
- Live audio capture via `--capture-live-audio`.
- Pokemon Red 600-frame headless/live WAV identity verified byte-for-byte.
- Default live audio queue tuned for gameplay stability rather than minimum latency.
- Blargg `dmg_sound` APU ROM-suite lane through `scripts\verify_apu.py`; current baseline passes all 12 single ROMs, including CH3 wave-RAM read/retrigger/write edge cases.

Remaining:

- Hardware-accurate analog filtering.
- Stricter APU-suite expansion beyond Blargg `dmg_sound`.
- More edge-case coverage for obscure sweep/envelope/trigger interactions.
- Latency tuning options after stability remains proven.

### Stage 6: CGB Foundation

Status: foundation, first-pass BG/window rendering, and first-pass OBJ palette/priority rendering started; not a compatibility mode yet.

Done:

- CGB-enhanced and CGB-only header detection.
- Explicit emulator mode selection with default DMG behavior preserved for DMG/CGB-enhanced ROMs, forced CGB mode for CGB-only ROMs, and `--mode dmg|cgb|auto` available in the CLI.
- Basic CGB post-boot CPU identity, including `A=$11`.
- DMG-mode CGB-only IO remains inert.
- CGB-mode foundations for `FF4F` VRAM bank select, `FF70` WRAM bank select, `FF68`-`FF6C` palette RAM/indexing and OPRI, and KEY1 double-speed placeholder state.
- First-pass CGB BG/window renderer support for BG palette RAM colors, tile-map palette attributes, tile VRAM-bank attributes, and X/Y flip attributes.
- First-pass CGB OBJ renderer support for OBJ palette RAM colors, OBJ palette attributes, OBJ tile VRAM-bank attributes, CGB OAM-order priority, `FF6C`/OPRI DMG-style priority mode, BG priority attributes, and LCDC.0 priority behavior.
- RGB framebuffer encoding plus Tk/PPM/BMP output support while preserving the DMG shade fast path.
- Unit coverage for default DMG behavior and exposed CGB behavior.
- `scripts\verify_cgb_foundation.py` synthetic smoke verifier, including a local Pokemon Crystal header/startup check when `roms\crystal.gbc` exists.
- `scripts\verify_crystal_window_startup.py` headless/window smoke verifier, confirming Crystal reaches the first frame in CGB mode and Tk presents before first-frame emulation begins.
- `scripts\verify_crystal_cgb_render.py` render smoke verifier, including synthetic CGB BG palette/bank/flip checks, synthetic CGB OBJ palette/bank/priority checks, and Pokemon Crystal RGB render checks.

Remaining:

- HDMA/GDMA.
- Double-speed timing model.
- Broader CGB render oracles and exact CGB raster/FIFO timing.
- Full CGB boot behavior and real CGB ROM compatibility gates.

## Active Regression Gates

Run these before treating a compatibility or timing change as safe:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B -m unittest discover -v
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_ppu.py --strict --max-steps 3000000
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_apu.py --json-output qa-output\apu-dmg-sound.json
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_cgb_foundation.py --json-output qa-output\cgb-foundation.json
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_crystal_window_startup.py --json-output qa-output\crystal-window-startup-headless.json
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_crystal_cgb_render.py --json-output qa-output\crystal-cgb-render.json
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_pokemon_red.py
python -B scripts\verify_oak_encyclopedia_oracle.py
python -B scripts\verify_pokemon_red_sprite_scene_oracle.py
python -B scripts\verify_pokemon_red_performance.py --json-output qa-output\pokemon-red-performance-gate.json
python -B scripts\verify_super_mario_land_performance.py --json-output qa-output\super-mario-land-performance-gate.json
```

For audio-sensitive changes, also compare a fixed headless/live WAV capture:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B main.py .\roms\PRed.gb --max-instructions 0 --frames 600 --dump-audio qa-output\pred-headless-600.wav
python -B main.py .\roms\PRed.gb --window --audio --max-instructions 0 --frames 600 --capture-live-audio qa-output\pred-live-600.wav
```

For performance-sensitive changes, profile Pokemon Red:

```powershell
python -B main.py .\roms\PRed.gb --window --audio --max-instructions 0 --frames 1800 --profile-window --profile-window-interval 60
python -B scripts\verify_pokemon_red_performance.py --window-profile-log qa-output\pokemon-red-window-profile.log
python -B scripts\verify_super_mario_land_performance.py --scenario action-audio --run-live-window --json-output qa-output\super-mario-land-live-performance-gate.json
```

For CGB window-startup/display-ordering changes, also run:

```powershell
python -B scripts\verify_crystal_window_startup.py --run-window --json-output qa-output\crystal-window-startup.json
```

For CGB renderer changes, also run the longer Crystal attribute sample when time allows:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_crystal_cgb_render.py --frames 2400 --require-crystal-attributes --json-output qa-output\crystal-cgb-render-attrs.json
```

Current target thresholds:

- Steady gameplay: mostly near 60 fps.
- Worst non-startup 60-frame windows: 50+ fps.
- Audio counters: `audio_underruns=0`, `audio_dropped=0`, `apu_dropped_samples=0`.
- Audio queue during known heavy windows: should stay comfortably above the danger zone, ideally above 80-100 ms.

## Recommended Next Goals

### 1. Save Live-Window Profile Fixtures

The headless performance gates now run fixed Pokemon Red text/sprite/audio scenarios and a Super Mario Land early-action scenario. The SML gate can also open a live window, capture `window-profile` lines, and fail on FPS, audio queue, underrun/drop, or APU sample-drop regressions.

The remaining workflow work is keeping representative live-window logs as fixtures. The parser already accepts saved `window-profile` logs and fails if:

- Any non-startup 60-frame window drops below the chosen FPS target.
- Audio queue dips below the configured threshold.
- Any audio underrun/drop counter increments.

The next step is to store a small set of representative live logs for Pokemon Red and Super Mario Land so parser validation can run without opening a window every time.

### 2. Expand APU Accuracy Beyond Blargg `dmg_sound`

Audio is now audible, deterministic, and covered by a repeatable APU ROM-suite lane with all 12 single Blargg `dmg_sound` ROMs passing. The next accuracy work should add stricter APU timing/oracle coverage one family at a time.

Suggested focus:

- SameSuite/Mealybug-style APU timing cases.
- Sweep/envelope trigger edge cases not covered by `dmg_sound`.
- CH3 wave RAM/playback quirks under stricter oracle timing.
- Length counter edge cases around DIV-APU falling edges.
- Mixer/filter behavior after a stable digital baseline exists.

### 3. Expand Commercial ROM Coverage

Pokemon Red is the current primary target, and Super Mario Land is now the quick action/performance smoke target. Add one new title at a time and define objective checks for each:

- Boot/title screen frames.
- Save-RAM behavior if applicable.
- Known menu/input path.
- 600-1800 frame performance sample.
- Optional WAV identity sample if audio is relevant.

Dr. Mario is the next obvious candidate for a scripted gate because it already has user-reported visual smoke success.

### 4. Continue PPU FIFO/Raster Work

The selected PPU gate is green, but full hardware compatibility needs broader FIFO behavior. Keep the strict gate stable while using candidate Mealybug cases and additional suites to drive targeted improvements.

Do not promote candidate image tests into the strict gate until the expected image source is appropriate for DMG mode or a CGB path exists.

### 5. Keep Optimizations Guarded And Verified

The current Pokemon Red speedups are guarded by exact ROM byte patterns, LCD state, cycle safety, and direct-memory checks. Keep that standard:

- Exact-match the code path being batched.
- Preserve instruction and cycle totals.
- Fall back to normal interpretation for uncommon branches.
- Verify against tests, strict PPU, fixed headless slices, live profile, and WAV identity when audio output is active.

### 6. Grow CGB From The Foundation

CGB is a large cross-cutting feature, not a small rendering option. Keep the current foundation narrow and verified, then add one subsystem at a time: HDMA/GDMA, double-speed timing, broader CGB render oracles, exact CGB raster/FIFO timing, and CGB boot behavior.
