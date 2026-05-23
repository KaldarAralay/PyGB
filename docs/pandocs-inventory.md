# Pan Docs Inventory

Date: 2026-05-23

This document compares the current GBemu codebase against the major hardware areas in Pan Docs. It is an implementation inventory, not a compatibility guarantee. `Done` means the repo has working code and regression evidence for the listed DMG behavior. `Partial` means the feature is useful but known not to cover every hardware edge case. `Pending` means there is no current implementation claim.

Primary Pan Docs references:

- https://gbdev.io/pandocs/Memory_Map.html
- https://gbdev.io/pandocs/CPU_Registers_and_Flags.html
- https://gbdev.io/pandocs/CPU_Instruction_Set.html
- https://gbdev.io/pandocs/Rendering.html
- https://gbdev.io/pandocs/pixel_fifo.html
- https://gbdev.io/pandocs/OAM_DMA_Transfer.html
- https://gbdev.io/pandocs/Audio_Registers.html
- https://gbdev.io/pandocs/Joypad_Input.html
- https://gbdev.io/pandocs/Serial_Data_Transfer_%28Link_Cable%29.html
- https://gbdev.io/pandocs/CGB_Registers.html

## Snapshot

GBemu is currently a playable, DMG-only emulator with strong evidence for Pokemon Red, Dr. Mario smoke coverage, Blargg CPU coverage, selected PPU suite coverage, Blargg `dmg_sound` APU tracking, live display/audio, and guarded Pokemon Red performance fast paths.

The biggest remaining gaps versus Pan Docs are not "can a ROM boot?" gaps anymore. They are hardware-completeness gaps: full pixel FIFO behavior, broader PPU timing ROM coverage, full APU suite compatibility and analog accuracy, CGB mode, SGB behavior, real serial peer/link behavior, and specialty cartridge hardware.

## Current Evidence

| Gate | Current result |
| --- | --- |
| Unit suite | `336 tests`, `OK` on 2026-05-23. |
| CPU ROM gate | `scripts\verify_cpu.py` passes Blargg individual `cpu_instrs` ROMs and combined `cpu_instrs.gb`. |
| APU ROM gate | `scripts\verify_apu.py` tracks Blargg `dmg_sound`: 4 passing ROMs and 8 known `XFAIL` cases. |
| PPU strict gate | `scripts\verify_ppu.py --strict --max-steps 3000000` covers `dmg-acid2`, current Mooneye PPU tests, and selected Mealybug image cases. |
| Pokemon Red smoke | `scripts\verify_pokemon_red.py` covers headless smoke, mapper probe, and save round-trip. |
| Oak's Lab encyclopedia oracle | `scripts\verify_oak_encyclopedia_oracle.py`: crop `diff_pixels=0`; OAM tiles `7C 7D 7E 7F 7C 7D 7E 7F` match PyBoy. |
| Sprite-heavy scene oracle | `scripts\verify_pokemon_red_sprite_scene_oracle.py`: full-screen `diff_pixels=0`; 28 visible OAM entries match PyBoy for y, x, tile, and attributes. |
| Automated Pokemon Red performance gate | `scripts\verify_pokemon_red_performance.py`: text `run_fps=97.10`; sprites `run_fps=79.57`; sprites with headless audio output `run_fps=66.97`, `apu_dropped_samples=0`; deterministic frame/instruction/cycle totals matched exactly. |

## Pan Docs Coverage Table

| Pan Docs area | Current implementation | Status | Remaining work |
| --- | --- | --- | --- |
| CPU registers, flags, and instruction set | `cpu.py`, `opcodes.py`, Blargg `cpu_instrs`, and unit coverage for flags, control flow, interrupts, HALT bug, STOP wake/freeze behavior, internal cycles, stack timing, and guarded hot paths. | Done for documented DMG instruction behavior covered by the current suites. | Add more timing ROMs over time, especially interrupt/timer/write-order edges. |
| Interrupts | `IE`, `IF`, interrupt entry timing, HALT interactions, delayed `EI`, timer/serial/PPU/joypad requests. | Partial | More hardware-test evidence for obscure interrupt ordering and DMA overlap cases. |
| Memory map | `bus.py` implements cartridge ROM/RAM, VRAM, WRAM, echo RAM, OAM, unusable range behavior, IO, HRAM, IE, boot ROM overlay, and mode-based VRAM/OAM access restrictions. | Partial | FEA0-FEFF currently uses a conservative `$FF` behavior rather than revision-specific DMG/CGB quirks; CGB banked VRAM/WRAM is pending. |
| Cartridge header and mappers | `cartridge.py` parses headers and supports ROM-only, ROM+RAM, MBC1, MBC1M, MBC2, MBC3 with RTC sidecar, MBC5 with rumble-control behavior, and HuC1 basic banking/IR state. | Partial | MMM01, MBC6, MBC7 sensor behavior, Pocket Camera, Bandai TAMA5, HuC3, and more commercial mapper/save gates. |
| Timer and divider | `bus.py` models DIV/TIMA edge ticking, delayed TIMA reload, TAC writes, STOP timer freeze, and timer interrupt behavior. | Partial | More acceptance tests for obscure reload/write boundary behavior. |
| Serial link | Internal-clock transfer timing, transfer completion, serial interrupt, and serial text sink for test ROMs. | Partial | No real second-Game-Boy peer, external clock, or multiplayer/link-cable protocol behavior yet. |
| Joypad | `joypad.py` implements active-low action/direction matrix, selected-line interrupts, held-button non-retriggering, STOP wake, CLI buttons, and Tkinter controls. | Done for single DMG input | SGB command packets and multi-controller behavior are pending. |
| LCD control/status and rendering | `ppu.py` implements LCD modes, LY/LYC/STAT, VBlank, line 153, DMG STAT quirk, LCD enable/disable, BG/window/OBJ rendering, palettes, scroll, priority, 8x16 sprites, flips, OAM selection, and selected mode-3 register effects. | Partial | Full per-dot FIFO behavior and broader raster test coverage remain the largest visual-accuracy gaps. |
| OAM DMA and OAM access | `bus.py` and `ppu.py` model FF46 DMA timing, bus blocking, HRAM exception, OAM access restrictions, sprite hiding during DMA, and selected mid-frame DMA effects. | Partial | More edge coverage for exact corruption behavior and hardware revision differences. |
| Pixel FIFO | The renderer has a segmented/timing-aware model with many targeted mode-3 tests for scroll, window, palette, LCDC, OBJ, and fetch-boundary behavior. | Partial | It is not a complete Pan Docs FIFO implementation; candidate Mealybug cases remain diagnostic. |
| APU/audio | `apu.py` and `audio.py` cover NR52 power, register reads/writes, DAC-gated channels, triggers, length, envelope, sweep, pulse/wave/noise timers, CH3 wave RAM behavior, CH4 LFSR, mixer, high-pass filter, sample buffering, WAV output, live waveOut playback, deterministic WAV identity, and Blargg `dmg_sound` tracking. | Partial | Full `dmg_sound` pass, stricter APU suites, analog filtering accuracy, obscure trigger/sweep/envelope quirks, and latency tuning. |
| Boot ROM and power-up | Optional user-supplied DMG boot ROM mapping and one-way FF50 unmapping exist; post-boot defaults are tested. | Partial | No bundled boot ROM, no exact power-up randomness/boot process modeling, no CGB boot flow. |
| CGB registers and mode | CGB-only IO is mostly inert on DMG; KEY1 has limited STOP/speed-switch state handling. | Pending as a mode | No CGB renderer, palettes, VRAM/WRAM banking, HDMA, double-speed timing model, CGB OAM priority, or CGB boot behavior. |
| SGB | No SGB mode. | Pending | SGB command packets, borders, palettes, multiplayer input, and SNES-side behavior are not implemented. |
| External devices | Basic cartridge RTC support exists for MBC3. | Partial | Game Boy Printer, Camera-specific behavior, MBC7 sensor, HuC3 hardware, and real link accessories are pending. |
| Runtime/frontends | `emulator.py`, `main.py`, and `display.py` provide frame stepping, save lifecycle, reset, CLI tooling, frame/audio dumps, Tkinter windowing, live audio, tracing, profiling, and button scripts. | Partial | Host-dependent pacing remains outside hardware emulation; add automated performance-gate parsing. |

## Risk Inventory

Highest current risk:

- PPU FIFO and raster completeness. The selected gate is strong, but Pan Docs describes many pixel-fetcher/FIFO interactions that are only partially represented.
- Audio accuracy. The digital APU is functional and audible, and now has Blargg `dmg_sound` suite tracking, but still needs full-suite pass work and better analog behavior.
- Optimization correctness outside covered Pokemon Red windows. Current hot paths are guarded and exact-vs-fast tested for important branches, but new hot paths should follow the same standard.

Medium current risk:

- Timer/interrupt edge cases around rare write ordering.
- Mapper edge cases outside common MBC1/MBC2/MBC3/MBC5/HuC1 behavior.
- Commercial compatibility breadth beyond Pokemon Red and Dr. Mario.

Not currently in scope:

- CGB compatibility.
- SGB compatibility.
- Real link-cable multiplayer.
- Specialty peripherals and unusual cartridge hardware.

## Practical Next Goals

1. Expand the Pokemon Red performance gate with captured live-window profile fixtures once the preferred log capture workflow is stable.
2. Harden the APU against the tracked Blargg `dmg_sound` `XFAIL` cases while keeping the current WAV identity checks for regression safety.
3. Broaden the PPU gate one case at a time, especially around FIFO and mid-scanline behavior.
4. Add a second commercial ROM as a real gate, with scripted input, save behavior, visual crop/oracle, and performance criteria.
5. Start CGB only after the DMG visual/audio gates stay stable, because CGB touches memory banking, palettes, DMA, CPU speed, and PPU priority all at once.
