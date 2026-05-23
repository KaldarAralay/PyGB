# Compatibility Matrix

Date: 2026-05-23

This matrix tracks evidence in this repository. `Pass` means the target is currently covered by an automated or repeatable regression check. `Playable smoke` means a commercial ROM is known to boot and run interactively but is not a compatibility oracle. `Partial` means the subsystem is useful but not hardware-complete. `Pending` means there is no current compatibility claim.

## Test Suites And Gates

| Target | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Unit test suite | Pass | `.\.tools\python-3.12.4-embed-amd64\python.exe -B -m unittest discover -v` | Latest run on 2026-05-23: 327 tests passed. Covers CPU, bus/timers, cartridge mappers, runtime, display/window profiling, joypad, PPU, APU/audio, `dmg-acid2`, and exact-vs-fast shadow-OAM helper coverage. |
| Blargg `cpu_instrs` individual ROMs `01` through `11` | Pass | `scripts\verify_cpu.py` | Current verifier passes all individual CPU instruction ROMs. |
| Blargg combined `cpu_instrs.gb` | Pass | `scripts\verify_cpu.py` and direct `main.py --stop-on-serial-result` runs | Current run reaches the serial `Passed` result. |
| `dmg-acid2` | Pass | Unit test and `scripts\verify_ppu.py --strict` | First completed frame matches Matt Currie's official DMG reference hash `2ba8286c29ae381838c71a88614302ce05f2b26102d1ed8dc51e25f83fcccc67`. |
| PPU external strict gate | Pass | `.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_ppu.py --strict --max-steps 3000000` | Latest run on 2026-05-22 passed `dmg-acid2`, current Mooneye acceptance/ppu ROMs, and selected Mealybug mode-3 image cases. |
| Mealybug candidate lane | XFail tracked | `scripts\verify_ppu.py --skip-dmg-acid2 --skip-mooneye --include-mealybug-candidates --max-steps 3000000` | Adjacent FIFO/tile-selection cases remain diagnostic only until a DMG oracle or a CGB PPU path is available for the reference-image ambiguity. |
| Pokemon Red real-ROM gate | Pass | `scripts\verify_pokemon_red.py`; live profiling command in README | Current MBC3 mapper, save-RAM, 600-frame smoke, live audio, and heavy-window frame pacing are repeatable enough to use as the primary real-ROM regression target. |
| Pokemon Red Oak encyclopedia PyBoy oracle | Pass | `python -B scripts\verify_oak_encyclopedia_oracle.py` | Latest run: crop `diff_pixels=0`; GBemu and PyBoy OAM tiles both `7C 7D 7E 7F 7C 7D 7E 7F`. |
| Pokemon Red sprite-heavy PyBoy oracle | Pass | `python -B scripts\verify_pokemon_red_sprite_scene_oracle.py` | Latest run: full-screen `diff_pixels=0`; 28 visible OAM entries match PyBoy for y, x, tile, and attributes. |
| Pokemon Red sprite-heavy performance profile | Pass | `python -B scripts\benchmark_pokemon_red_sprites.py --warmup-frames 6000 --profile-frames 600 --min-fps 30` | Latest headless profile: `run_fps=78.44`, frame range `7.87-19.87 ms`. |
| Pokemon Red 600-frame WAV identity | Pass | Headless `--dump-audio` vs live `--capture-live-audio` | Latest PCM payloads and WAV params are identical. SHA-256 of PCM: `6575f192cdea8ed0bf84c1ee775add94035c7e556a36c2a094a1dbb2f052b10b`. |
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
- Current saved-game sprite-heavy profile: `run_fps=78.44`, `ppu_max_sprites=10`, `ppu_sprite_lines=19200`, and no APU sample drops with audio output disabled for the headless profile.

## Subsystems

| Area | Status | Current Coverage | Remaining Risk |
| --- | --- | --- | --- |
| CPU instruction behavior | Pass | Documented non-CB and CB opcodes, flags, stack/control flow, interrupts, HALT bug, STOP wake/freeze behavior, per-access cycle accounting, and Blargg `cpu_instrs`. | Rare internal-cycle ordering should keep being checked as additional timing ROMs are added. |
| Memory bus and timers | Partial | DIV/TIMA edge ticking, delayed TIMA reload, STOP timer freeze, serial transfer timing, OAM DMA bus blocking and line visibility, boot ROM overlay, IO read masks, and CGB-only DMG inert registers. | More hardware timing tests are needed for uncommon write-ordering and interrupt-boundary cases. |
| PPU and framebuffer | Partial | LCD modes, `LY`/`STAT`, VBlank, line-153 wrap, DMG STAT quirk, BG/window/OBJ rendering, palette handling, scroll wrapping, sprite priority, mode-3 penalties, selected segmented mid-line effects, OAM DMA sprite hiding, `dmg-acid2`, Mooneye acceptance/ppu, and selected Mealybug image cases. | Full per-dot FIFO behavior, every mid-scanline raster edge, and broader PPU ROM suites remain pending. |
| Joypad/input | Partial | Active-low matrix reads, selected high-to-low interrupts, held-button non-retriggering, STOP wake, CLI held buttons, and Tkinter keyboard input. | Host input has been exercised in real gameplay, but not yet as a broad automated game-menu/input regression suite. |
| Cartridge mappers | Partial | ROM-only, ROM+RAM, MBC1, MBC1M, MBC2, MBC3 with RTC/save sidecar, MBC5 with rumble-control behavior, HuC1 banking/IR state, save RAM helpers, and unsupported-mapper warnings. | Unsupported or unverified specialty hardware includes MMM01, MBC6, MBC7 sensor behavior, Pocket Camera, Bandai TAMA5, and HuC3. |
| APU/audio | Partial | Register model, `NR52`, DAC-gated activity, trigger handling, CH3 wave RAM behavior/playback delay, length counters, envelopes, CH1 sweep, pulse/wave/noise timers, mixer, high-pass filter, bounded sample buffering, WAV dumps, live Windows audio, and deterministic live/headless PCM identity. | Mature analog filtering, sweep/envelope obscure hardware quirks beyond current unit coverage, and APU ROM-suite compatibility remain pending. |
| Runtime/display | Partial | Frame stepping, stop conditions, save lifecycle, reset preserving cartridge state, frame dumps, Tkinter window mode, keyboard controls, live audio, trace toggle, frame pacing, and rolling spike profiling. | Tkinter pacing is good enough for current Pokemon Red testing but remains host/runtime dependent. |
| Performance | Partial | Pokemon Red heavy windows are optimized with guarded hot paths and verified with instruction/cycle/audio identity checks for the covered runs. | Broader ROMs may expose different hot paths; optimizations should remain guarded and verifier-backed. |

## Pan Docs Inventory

The current codebase was compared against the major Pan Docs hardware areas on 2026-05-23. The detailed inventory is in `docs\pandocs-inventory.md`.

Summary:

| Pan Docs area | Current status |
| --- | --- |
| CPU registers, flags, and documented instruction set | Done for current DMG test coverage. |
| Interrupts, timers, serial, joypad, and memory map | Useful and tested, but still partial for obscure timing/revision details and real link behavior. |
| PPU/LCD/OAM DMA/pixel FIFO | Strong selected DMG gate and real-ROM evidence, but not a complete per-dot FIFO implementation. |
| APU/audio | Functional and deterministic for current gates, but APU ROM-suite coverage and analog accuracy remain pending. |
| Cartridge hardware | Common mappers are supported; specialty mappers/peripherals are pending. |
| CGB and SGB | Not implemented as compatibility modes. |

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

The project has moved past the first "does a real ROM work?" threshold. CPU correctness, common cartridge support, selected PPU compatibility, live display/audio, and Pokemon Red gameplay are now solid enough for iterative real-ROM testing.

It is still not a broad or cycle-perfect DMG compatibility claim. The main remaining work is expanding ROM-suite coverage, hardening audio accuracy, adding more commercial-game gates, and replacing targeted PPU/performance assumptions with broader hardware-test evidence.
