# GBemu Next Stages

Date: 2026-05-22

This document is the active roadmap. Historical Stage 1 CPU evidence is preserved in `docs\stage1-audit.md`; current compatibility evidence is in `docs\compatibility.md`.

## Current State

GBemu is now a playable DMG emulator for the current primary real-ROM target, Pokemon Red. It has a verified CPU core, common mapper support, strict selected PPU regression coverage, live Tkinter display, live Windows audio, deterministic WAV capture, and rolling frame/audio profiling.

The project is not cycle-perfect and not yet a broad commercial compatibility emulator. The strongest next work is to keep adding evidence while improving accuracy and tail-latency behavior.

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
- PPU-mode CPU access restrictions for VRAM/OAM.
- OAM DMA bus blocking and line/sprite visibility effects.
- Selected mode-3 timing model for SCX, SCY, WX, window enable/disable, LCDC source changes, OBJ enable/disable, OBJ size changes, palette writes, sprite/window stalls, and tile-data fetch-boundary cases.
- Frame dumps through PPM/BMP and live Tkinter display output.
- `dmg-acid2` reference-image regression and strict selected external PPU gate through `scripts\verify_ppu.py --strict`.

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
- Rolling window profiler with run/draw/audio timing, CPU/bus/PPU/APU stats, audio queue range, worst-frame spike fields, and coarse spike cause attribution.
- Pokemon Red frame pacing round 1: worst non-startup live windows now clear 50 fps with zero audio underruns/drops in the latest profile.

Remaining:

- Add automated capture for window-profile summaries so performance gates can run without manual log inspection.
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

Remaining:

- Hardware-accurate analog filtering.
- APU ROM-suite compatibility.
- More edge-case coverage for obscure sweep/envelope/trigger interactions.
- Latency tuning options after stability remains proven.

## Active Regression Gates

Run these before treating a compatibility or timing change as safe:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B -m unittest discover -v
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_ppu.py --strict --max-steps 3000000
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_pokemon_red.py
```

For audio-sensitive changes, also compare a fixed headless/live WAV capture:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B main.py .\roms\PRed.gb --max-instructions 0 --frames 600 --dump-audio qa-output\pred-headless-600.wav
python -B main.py .\roms\PRed.gb --window --audio --max-instructions 0 --frames 600 --capture-live-audio qa-output\pred-live-600.wav
```

For performance-sensitive changes, profile Pokemon Red:

```powershell
python -B main.py .\roms\PRed.gb --window --audio --max-instructions 0 --frames 1800 --profile-window --profile-window-interval 60
```

Current target thresholds:

- Steady gameplay: mostly near 60 fps.
- Worst non-startup 60-frame windows: 50+ fps.
- Audio counters: `audio_underruns=0`, `audio_dropped=0`, `apu_dropped_samples=0`.
- Audio queue during known heavy windows: should stay comfortably above the danger zone, ideally above 80-100 ms.

## Recommended Next Goals

### 1. Make Pokemon Red A First-Class Automated Performance Gate

The profiler already emits the right information. The next step is a script that runs a fixed Pokemon Red window/profile scenario, parses the output, and fails if:

- Any non-startup 60-frame window drops below the chosen FPS target.
- Audio queue dips below the configured threshold.
- Any audio underrun/drop counter increments.
- Instruction/cycle/frame totals unexpectedly drift for fixed headless slices.

This would turn the current manual performance evidence into a repeatable CI-style gate.

### 2. Add APU ROM-Suite Coverage

Audio is now audible and deterministic enough to test more seriously. The next accuracy work should add APU-specific ROM suites and compare serial pass/fail output or reference PCM where practical.

Suggested focus:

- Sweep/envelope trigger edge cases.
- CH3 wave RAM/playback quirks.
- Length counter edge cases around DIV-APU falling edges.
- Mixer/filter behavior after a stable digital baseline exists.

### 3. Expand Commercial ROM Coverage

Pokemon Red is the current primary target. Add one new title at a time and define objective checks for each:

- Boot/title screen frames.
- Save-RAM behavior if applicable.
- Known menu/input path.
- 600-1800 frame performance sample.
- Optional WAV identity sample if audio is relevant.

Dr. Mario is the next obvious candidate because it already has user-reported visual smoke success.

### 4. Continue PPU FIFO/Raster Work

The selected PPU gate is green, but full hardware compatibility needs broader FIFO behavior. Keep the strict gate stable while using candidate Mealybug cases and additional suites to drive targeted improvements.

Do not promote candidate image tests into the strict gate until the expected image source is appropriate for DMG mode or a CGB path exists.

### 5. Keep Optimizations Guarded And Verified

The current Pokemon Red speedups are guarded by exact ROM byte patterns, LCD state, cycle safety, and direct-memory checks. Keep that standard:

- Exact-match the code path being batched.
- Preserve instruction and cycle totals.
- Fall back to normal interpretation for uncommon branches.
- Verify against tests, strict PPU, fixed headless slices, live profile, and WAV identity when audio output is active.
