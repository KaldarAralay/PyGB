# Compatibility Matrix

Date: 2026-05-23

This matrix tracks evidence in this repository. `Pass` means the target is currently covered by an automated or repeatable regression check. `Playable smoke` means a commercial ROM is known to boot and run interactively but is not a compatibility oracle. `Partial` means the subsystem is useful but not hardware-complete. `Pending` means there is no current compatibility claim.

## Test Suites And Gates

| Target | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Unit test suite | Pass | `.\.tools\python-3.12.4-embed-amd64\python.exe -B -m unittest discover -v` | Latest run on 2026-05-23: 374 tests passed. Covers CPU, bus/timers, cartridge mappers, runtime, display/window profiling, joypad, PPU, APU/audio, CGB foundation/startup/render behavior, CGB OBJ palette/priority behavior, `dmg-acid2`, exact-vs-fast shadow-OAM helper coverage, performance-gate parsing, and APU verifier parsing. |
| Blargg `cpu_instrs` individual ROMs `01` through `11` | Pass | `scripts\verify_cpu.py` | Current verifier passes all individual CPU instruction ROMs. |
| Blargg combined `cpu_instrs.gb` | Pass | `scripts\verify_cpu.py` and direct `main.py --stop-on-serial-result` runs | Current run reaches the serial `Passed` result. |
| Blargg `dmg_sound` APU ROMs | Pass | `.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_apu.py --json-output qa-output\apu-dmg-sound.json` | Current baseline passes all 12 single `dmg_sound` ROMs, including CH3 wave-RAM read/retrigger/write edge cases `09`, `10`, and `12`. No known `XFAIL` cases remain in this lane. |
| `dmg-acid2` | Pass | Unit test and `scripts\verify_ppu.py --strict` | First completed frame matches Matt Currie's official DMG reference hash `2ba8286c29ae381838c71a88614302ce05f2b26102d1ed8dc51e25f83fcccc67`. |
| PPU external strict gate | Pass | `.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_ppu.py --strict --max-steps 3000000` | Latest run on 2026-05-22 passed `dmg-acid2`, current Mooneye acceptance/ppu ROMs, and selected Mealybug mode-3 image cases. |
| Mealybug candidate lane | XFail tracked | `scripts\verify_ppu.py --skip-dmg-acid2 --skip-mooneye --include-mealybug-candidates --max-steps 3000000` | Adjacent FIFO/tile-selection cases remain diagnostic only until a DMG oracle or a CGB PPU path is available for the reference-image ambiguity. |
| Pokemon Red real-ROM gate | Pass | `scripts\verify_pokemon_red.py`; live profiling command in README | Current MBC3 mapper, save-RAM, 600-frame smoke, live audio, and heavy-window frame pacing are repeatable enough to use as the primary real-ROM regression target. |
| Pokemon Red Oak encyclopedia PyBoy oracle | Pass | `python -B scripts\verify_oak_encyclopedia_oracle.py` | Latest run: crop `diff_pixels=0`; GBemu and PyBoy OAM tiles both `7C 7D 7E 7F 7C 7D 7E 7F`. |
| Pokemon Red sprite-heavy PyBoy oracle | Pass | `python -B scripts\verify_pokemon_red_sprite_scene_oracle.py` | Latest run: full-screen `diff_pixels=0`; 28 visible OAM entries match PyBoy for y, x, tile, and attributes. |
| Pokemon Red automated performance gate | Pass | `python -B scripts\verify_pokemon_red_performance.py --json-output qa-output\pokemon-red-performance-gate.json` | Latest run: text `run_fps=88.21`; sprites `run_fps=72.89`; sprites with headless audio output `run_fps=63.13`, `apu_dropped_samples=0`; deterministic frame/instruction/cycle totals matched exactly. |
| Pokemon Red 600-frame WAV identity | Pass | Headless `--dump-audio` vs live `--capture-live-audio` | Latest PCM payloads and WAV params are identical. SHA-256 of PCM: `6575f192cdea8ed0bf84c1ee775add94035c7e556a36c2a094a1dbb2f052b10b`. |
| Super Mario Land early-action performance gate | Pass | `python -B scripts\verify_super_mario_land_performance.py --json-output qa-output\super-mario-land-performance-gate.json` | Latest headless run: action `run_fps=77.86`; action with headless audio output `run_fps=66.61`, `apu_dropped_samples=0`; deterministic frame/instruction/cycle totals matched exactly. Latest live capture checked 10 non-startup windows with min `wall_fps=46.84`, min queue `33.5 ms`, and zero audio underruns/drops. |
| CGB foundation smoke | Pass | `.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_cgb_foundation.py --json-output qa-output\cgb-foundation.json` | Synthetic smoke verifies CGB header detection, explicit/auto mode selection, forced CGB-only startup, DMG inert behavior, `FF4F` VRAM bank select, `FF70` WRAM bank select, `FF68`-`FF6C` palette/OPRI registers, KEY1 placeholder state, and CGB post-boot `A=$11`. Local Crystal smoke detects `PM_CRYSTAL` as `CGB only` and confirms default and `--mode auto` CLI output `Mode: CGB`. This is not a CGB game compatibility claim. |
| Pokemon Crystal CGB startup/window smoke | Pass | `scripts\verify_crystal_window_startup.py`; `python -B main.py .\roms\crystal.gbc --window --max-instructions 0 --frames 1` | Headless Crystal reaches the first frame in CGB mode. The window smoke verifies Tk creates and presents the initial window before the long first-frame step, then reaches frame 1 without waiting until process exit. This is a startup/display-ordering gate, not a Crystal compatibility claim. |
| Pokemon Crystal CGB render smoke | Pass | `.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_crystal_cgb_render.py --json-output qa-output\crystal-cgb-render.json`; optional `--frames 2400 --require-crystal-attributes` | Quick lane reaches 60 Crystal frames with a full RGB framebuffer, 3 unique colors, and nonzero BG palette RAM. The longer lane reached 2400 frames with palette, tile-bank, X-flip, and Y-flip attributes present. Synthetic checks verify CGB BG palette/bank/flip behavior plus CGB OBJ palette selection, OBJ tile VRAM-bank selection, OAM-order priority, OPRI DMG-style priority, BG priority attributes, and LCDC.0 priority behavior. This is not a Crystal compatibility claim. |
| Dr. Mario | Playable smoke | User window run; visual smoke command in README | Interactive run has no obvious visual glitches. Keep as smoke coverage until a scripted regression is added. |
| Other commercial DMG games | Pending | Not yet part of the gate | Add titles one at a time with ROM-specific smoke criteria, save behavior, profiling windows, and audio checks. |

## Latest Pokemon Red Performance Evidence

Command:

```powershell
python -B main.py .\roms\PRed.gb --window --audio --max-instructions 0 --frames 1800 --profile-window --profile-window-interval 60
```

Latest relevant profile windows:

| Window | Evidence | Interpretation |
| --- | --- | --- |
| Scene-transition spike | `wall_fps=50.87`, `spike_ms=69.68`, `spike_cause=bus-slow`, queue `275.6-365.7 ms` | Worst non-startup transition now clears the 50 fps target. |
| Heavy gameplay/decompression window | `wall_fps=53.55`, `spike_ms=30.70`, queue `153.5-323.4 ms` | Former ~45 fps heavy window now clears target with healthy queue. |
| Audio counters | `audio_underruns=0`, `audio_dropped=0`, `apu_dropped_samples=0` | No live audio underrun/drop regression in the profiled run. |

Headless slices used during optimization:

- 1080-1140 transition slice: about 66-67 fps after LCD-off copy/fill loop batching.
- 1500-1560 heavy slice: about 58-59 fps after Pokemon Red hot-path batching.
- Current automated performance gate:
  - Text scene: 240 frames, `run_fps=88.21`, `cpu_instr=1564703`, `cpu_cycles=16853764`.
  - Sprite-heavy scene: 600 frames, `run_fps=72.89`, `cpu_instr=2717563`, `cpu_cycles=42134400`, `ppu_max_sprites=10`, `ppu_sprite_lines=19200`.
  - Sprite-heavy scene with headless audio output: 600 frames, `run_fps=63.13`, `apu_samples=443012`, `apu_dropped_samples=0`, same deterministic CPU totals.
  - The gate can also parse captured live `window-profile` logs and fail on non-startup FPS, audio queue, underrun, drop, or APU sample-drop regressions.

## Latest Super Mario Land Performance Evidence

Command:

```powershell
python -B scripts\verify_super_mario_land_performance.py --json-output qa-output\super-mario-land-performance-gate.json
```

Current scripted early-1-1 action gate:

- Headless action scene: 600 frames, `run_fps=77.86`, `cpu_instr=1343492`, `cpu_cycles=42379808`, `ppu_max_sprites=4`, `ppu_sprite_lines=9929`.
- Headless action scene with audio output: 600 frames, `run_fps=66.61`, `apu_samples=445592`, `apu_dropped_samples=0`, same deterministic CPU totals.
- Live action capture: `python -B scripts\verify_super_mario_land_performance.py --scenario action-audio --run-live-window --json-output qa-output\super-mario-land-live-performance-gate.json`.
- Latest live capture checked 10 non-startup 60-frame windows with min `wall_fps=46.84`, min `audio_queue_range_ms` low of `33.5 ms`, `audio_underruns=0`, `audio_dropped=0`, and `apu_dropped_samples=0`.
- This is an action/performance smoke gate, not a broad Super Mario Land compatibility claim.

## Subsystems

| Area | Status | Current Coverage | Remaining Risk |
| --- | --- | --- | --- |
| CPU instruction behavior | Pass | Documented non-CB and CB opcodes, flags, stack/control flow, interrupts, HALT bug, STOP wake/freeze behavior, per-access cycle accounting, and Blargg `cpu_instrs`. | Rare internal-cycle ordering should keep being checked as additional timing ROMs are added. |
| Memory bus and timers | Partial | DIV/TIMA edge ticking, delayed TIMA reload, STOP timer freeze, serial transfer timing, OAM DMA bus blocking and line visibility, boot ROM overlay, IO read masks, CGB-only DMG inert registers, forced CGB-only startup, and CGB foundation banking/register storage. | More hardware timing tests are needed for uncommon write-ordering and interrupt-boundary cases; CGB double-speed timing is not implemented. |
| PPU and framebuffer | Partial | LCD modes, `LY`/`STAT`, VBlank, line-153 wrap, DMG STAT quirk, DMG BG/window/OBJ rendering, first-pass CGB BG/window palette and tile-attribute rendering, first-pass CGB OBJ palette and priority rendering, RGB framebuffer export/display, palette handling, scroll wrapping, sprite priority, mode-3 penalties, selected segmented mid-line effects, OAM DMA sprite hiding, `dmg-acid2`, Mooneye acceptance/ppu, and selected Mealybug image cases. | Full per-dot FIFO behavior, every mid-scanline raster edge, broader PPU ROM suites, CGB raster timing, and broader CGB commercial-game visual oracles remain pending. |
| Joypad/input | Partial | Active-low matrix reads, selected high-to-low interrupts, held-button non-retriggering, STOP wake, CLI held buttons, and Tkinter keyboard input. | Host input has been exercised in real gameplay, but not yet as a broad automated game-menu/input regression suite. |
| Cartridge mappers | Partial | ROM-only, ROM+RAM, MBC1, MBC1M, MBC2, MBC3 with RTC/save sidecar, MBC5 with rumble-control behavior, HuC1 banking/IR state, save RAM helpers, and unsupported-mapper warnings. | Unsupported or unverified specialty hardware includes MMM01, MBC6, MBC7 sensor behavior, Pocket Camera, Bandai TAMA5, and HuC3. |
| APU/audio | Partial | Register model, `NR52`, DAC-gated activity, trigger handling, CH3 wave RAM behavior/playback delay, length counters, envelopes, CH1 sweep, pulse/wave/noise timers, mixer, high-pass filter, bounded sample buffering, WAV dumps, live Windows audio, deterministic live/headless PCM identity, and a fully passing Blargg `dmg_sound` single-ROM lane. | Mature analog filtering, SameSuite-style stricter APU coverage, broader audio oracles, and obscure trigger/sweep/envelope quirks remain pending. |
| Runtime/display | Partial | Frame stepping, stop conditions, save lifecycle, reset preserving cartridge state, frame dumps, Tkinter window mode, keyboard controls, live audio, trace toggle, frame pacing, rolling spike profiling, and CGB first-frame startup diagnostics. | Tkinter pacing is good enough for current Pokemon Red testing but remains host/runtime dependent. |
| Performance | Partial | Pokemon Red heavy windows are optimized with guarded hot paths and verified with instruction/cycle/audio identity checks for the covered runs. Super Mario Land now adds a quick early-action performance smoke gate with headless and live-profile evidence. | Broader ROMs may expose different hot paths; optimizations should remain guarded and verifier-backed. |

## Pan Docs Inventory

The current codebase was compared against the major Pan Docs hardware areas on 2026-05-23. The detailed inventory is in `docs\pandocs-inventory.md`.

Summary:

| Pan Docs area | Current status |
| --- | --- |
| CPU registers, flags, and documented instruction set | Done for current DMG test coverage. |
| Interrupts, timers, serial, joypad, and memory map | Useful and tested, but still partial for obscure timing/revision details and real link behavior. |
| PPU/LCD/OAM DMA/pixel FIFO | Strong selected DMG gate and real-ROM evidence, but not a complete per-dot FIFO implementation. |
| APU/audio | Functional and deterministic for current gates, with the Blargg `dmg_sound` single-ROM lane passing; stricter APU suite compatibility and analog accuracy remain pending. |
| Cartridge hardware | Common mappers are supported; specialty mappers/peripherals are pending. |
| CGB and SGB | CGB foundation plus first-pass BG/window palette/tile-attribute rendering and OBJ palette/priority rendering started; SGB not implemented. |

## Supported Cartridge Type Profiles

| Cartridge types | Mapper status |
| --- | --- |
| `00`, `08`, `09` | ROM-only and ROM+RAM supported. |
| `01`, `02`, `03` | MBC1 supported, including RAM/battery profiles and MBC1M detection. |
| `05`, `06` | MBC2 supported with internal 512x4-bit RAM behavior. |
| `0F`, `10`, `11`, `12`, `13` | MBC3 supported, including 0-7 RAM-bank selection, 64 KiB RAM images, RTC latch/timekeeping/save sidecar behavior where present. |
| `19`, `1A`, `1B`, `1C`, `1D`, `1E` | MBC5 supported, including RAM banking and rumble-control bit handling. |
| `FF` | HuC1 supported at the current banking/basic IR-state level. |
| `0B`, `0C`, `0D`, `20`, `22`, `FC`, `FD`, `FE`, unknown types | Recognized as unsupported or unknown; the CLI warns before execution. |

## Current Completion Estimate

The project has moved past the first "does a real ROM work?" threshold. CPU correctness, common cartridge support, selected PPU compatibility, live display/audio, Pokemon Red gameplay, and Super Mario Land early-action smoke coverage are now solid enough for iterative real-ROM testing.

It is still not a broad or cycle-perfect DMG compatibility claim. The main remaining work is expanding ROM-suite coverage, hardening audio accuracy, adding more commercial-game gates, and replacing targeted PPU/performance assumptions with broader hardware-test evidence.
