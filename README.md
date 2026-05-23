# GBemu

GBemu is a DMG-only Game Boy emulator written in Python. It now runs real commercial ROMs well enough for interactive testing: Pokemon Red is the primary gameplay regression target, Dr. Mario is a visual smoke target, and the CPU/PPU/APU subsystems have automated regression gates.

Current verified status: May 23, 2026.

## Current Scope

- `.gb` ROM loading with cartridge header reporting, optional user-supplied DMG boot ROM mapping, and one-way `FF50` boot ROM unmapping.
- Header-driven cartridge mapper dispatch for ROM-only, ROM+RAM, MBC1, MBC1M, MBC2, MBC3 with RTC sidecar state, MBC5 including rumble-control behavior, and HuC1 basic banking/IR state. Unsupported cartridge profiles are recognized and warned about.
- SM83/LR35902 CPU core covering documented non-CB and CB-prefixed opcodes, stack/control flow, interrupts, HALT bug behavior, STOP wake/freeze behavior, and Blargg `cpu_instrs` verification.
- Cycle-aware bus timing for DIV/TIMA edge ticking, delayed TIMA reloads, serial transfer timing, OAM DMA, PPU access restrictions, hardware-style I/O read masks, and inert CGB-only registers on DMG.
- PPU timing and rendering for DMG background/window/sprite output, LCD modes, `LY`/`STAT`, line-153 behavior, DMG STAT write quirk, mode-3 penalties, selected mid-scanline register effects, OAM DMA sprite hiding, frame dumps, and the strict selected external PPU gate.
- Joypad input through `FF00`, CLI held buttons, STOP wake through selected joypad lines, and Tkinter keyboard controls.
- APU register/channel model with pulse, wave, noise, length, envelope, sweep, DAC gating, mixer, initial high-pass filtering, sample buffering, WAV dumps, live Windows audio playback, deterministic WAV identity checks, and Blargg `dmg_sound` regression coverage.
- Tkinter window mode with frame pacing, live audio, pause/reset/trace toggles, save-RAM lifecycle, and rolling frame/audio spike profiling.
- Pokemon Red performance hot paths for heavy MBC3/LCD-off copy/fill/decompression frames, with exact-vs-fast unit coverage for shadow-OAM/object helpers, PyBoy visual/OAM oracles for current sprite regressions, and an automated headless performance gate.

## Quick Start

Run a ROM headlessly until a frame count is reached:

```powershell
python main.py path\to\rom.gb --max-instructions 0 --frames 600
```

Run with a Tkinter window:

```powershell
python main.py path\to\rom.gb --window --scale 3 --max-instructions 0
```

Run with live audio:

```powershell
python main.py path\to\rom.gb --window --audio --max-instructions 0
```

Run Pokemon Red, the current real-ROM gameplay target:

```powershell
python main.py .\roms\PRed.gb --window --audio --max-instructions 0
```

Run Dr. Mario, the current visual smoke target:

```powershell
python main.py .\roms\DrMario.gb --window --max-instructions 0
```

The embedded verification runtime does not include Tkinter. Use system `python` for `--window`; use `.\.tools\python-3.12.4-embed-amd64\python.exe` for headless tests and verifiers.

## Controls

Default Tkinter controls:

- D-pad: arrow keys
- A/B: `z` / `x`
- Start/Select: `Enter` / `BackSpace` or `Space`
- Pause: `p`
- Reset: `r`
- Trace toggle: `t`
- Audio toggle: `m`
- Quit: `Escape`

## Common Commands

Run with a user-supplied DMG boot ROM:

```powershell
python main.py path\to\rom.gb --boot-rom path\to\dmg_boot.bin --max-instructions 0
```

Write a CPU trace:

```powershell
python main.py path\to\rom.gb --trace-file traces\run.log --max-instructions 200000
```

Dump the current framebuffer:

```powershell
python main.py path\to\rom.gb --frames 1 --dump-frame traces\frame.ppm
python main.py path\to\rom.gb --frames 1 --dump-frame-bmp traces\frame.bmp
```

Dump generated APU output to stereo 16-bit WAV:

```powershell
python main.py path\to\rom.gb --max-instructions 0 --frames 600 --dump-audio traces\audio.wav
```

Capture live-window generated audio to WAV:

```powershell
python main.py path\to\rom.gb --window --audio --max-instructions 0 --frames 600 --capture-live-audio traces\live-audio.wav
```

Run with held buttons:

```powershell
python main.py path\to\rom.gb --max-instructions 0 --frames 3 --buttons a,start --dump-frame traces\frame.ppm
```

Load and save cartridge RAM and MBC3 RTC sidecar state:

```powershell
python main.py path\to\rom.gb --save-file saves\game.sav --max-instructions 0 --frames 60
```

For MBC3 timer cartridges, `game.sav` remains raw cartridge RAM bytes and RTC state is saved beside it as `game.sav.rtc`.

## Profiling

Window profiling reports rolling run, draw, audio, CPU, bus, PPU, APU, queue, and spike diagnostics:

```powershell
python main.py .\roms\PRed.gb --window --audio --max-instructions 0 --frames 1800 --profile-window --profile-window-interval 60
```

Important fields:

- `run_ms`, `draw_ms`, `audio_ms`: per-frame averages inside the reporting window.
- `wall_fps`: observed wall-clock FPS for the reporting window.
- `audio_queue_range_ms`: min/max live audio queue in the reporting window.
- `spike_ms`, `spike_run_ms`, `spike_audio_queue_ms`: worst single-frame spike details.
- `spike_cause`: rough attribution such as `ppu-sprites`, `ppu-window-sprites`, `bus-slow`, `audio-queue-low`, or `apu-events`.

Latest Pokemon Red 1800-frame live profile evidence:

- Worst non-startup 60-frame transition window: `wall_fps=50.87`.
- Known heavy gameplay window around frame 1500: `wall_fps=53.55`.
- Heavy-window queue range: `153.5-323.4 ms`.
- `audio_underruns=0`, `audio_dropped=0`, `apu_dropped_samples=0`.

## Verification

Run the full unit suite:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B -m unittest discover -v
```

Latest full suite result:

```text
Ran 338 tests in 0.518s
OK
```

Run the CPU milestone verifier:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_cpu.py
```

Run the strict PPU gate:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_ppu.py --strict --max-steps 3000000
```

Run the Pokemon Red real-ROM regression gate:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_pokemon_red.py
```

Run the Blargg `dmg_sound` APU ROM-suite lane:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_apu.py --json-output qa-output\apu-dmg-sound.json
```

The default APU lane runs all 12 single `dmg_sound` ROMs. Current baseline: `01-registers`, `02-len ctr`, `03-trigger`, `04-sweep`, `05-sweep details`, and `06-overflow on trigger` pass; the remaining length/power and CH3 wave-RAM edge cases are tracked as `XFAIL`. Use `--expected-pass-only` for the strict passing subset, or `--strict` when working toward a full-suite pass.

Run the Pokemon Red PyBoy visual/OAM oracles:

```powershell
python -B scripts\verify_oak_encyclopedia_oracle.py
python -B scripts\verify_pokemon_red_sprite_scene_oracle.py
```

Run the automated Pokemon Red performance gate:

```powershell
python -B scripts\verify_pokemon_red_performance.py --json-output qa-output\pokemon-red-performance-gate.json
```

The gate runs fixed text, sprite-heavy, and sprite-heavy-with-audio headless scenarios. It parses benchmark output, enforces FPS thresholds, checks deterministic frame/instruction/cycle totals, and fails if headless APU output drops samples. It can also validate captured live `window-profile` logs:

```powershell
python -B scripts\verify_pokemon_red_performance.py --window-profile-log qa-output\pokemon-red-window-profile.log
```

Latest sprite-scene oracle and performance-gate evidence:

- Oak's Lab encyclopedia crop: `diff_pixels=0`; OAM tiles `7C 7D 7E 7F 7C 7D 7E 7F`.
- Sprite-heavy saved-game scene: full-screen `diff_pixels=0`; 28 visible OAM entries match PyBoy for y, x, tile, and attributes.
- Blargg `dmg_sound`: 6 passing ROMs, 6 tracked `XFAIL` ROMs, no unexpected failures in the default lane.
- Performance gate: text `run_fps=95.92`; sprites `run_fps=76.86`; sprites with headless audio output `run_fps=65.60`, `apu_dropped_samples=0`.

Verify headless/live WAV identity for a fixed Pokemon Red run:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B main.py .\roms\PRed.gb --max-instructions 0 --frames 600 --dump-audio qa-output\pred-headless-600.wav
python -B main.py .\roms\PRed.gb --window --audio --max-instructions 0 --frames 600 --capture-live-audio qa-output\pred-live-600.wav
```

Latest WAV identity result:

```text
PCM equal: True
SHA-256: 6575f192cdea8ed0bf84c1ee775add94035c7e556a36c2a094a1dbb2f052b10b
```

## Current Limitations

- This is still DMG-only; CGB mode is not implemented.
- PPU coverage is strong for the selected strict gate, but the emulator still does not model a complete per-dot pixel FIFO or every possible mid-scanline raster edge case.
- APU/audio is functional, deterministic for current PCM identity checks, and covered by a Blargg `dmg_sound` lane, but mature analog filtering and full APU ROM-suite compatibility are still pending.
- Commercial compatibility is early. Pokemon Red is the primary tested gameplay target; Dr. Mario is a visual smoke target. Other games should be treated as exploratory until they are added to the compatibility matrix.
- Performance includes several real-ROM-specific hot paths. They preserve current instruction/cycle/audio identity for the covered Pokemon Red windows, but broader optimization should continue to be measured with verification gates on.

Detailed compatibility evidence lives in `docs\compatibility.md`; the Pan Docs inventory lives in `docs\pandocs-inventory.md`; the active roadmap lives in `docs\next-stages.md`.
