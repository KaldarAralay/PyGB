# GBemu

GBemu is a DMG-first Game Boy emulator written in Python. It now runs real commercial DMG ROMs well enough for interactive testing: Pokemon Red is the primary gameplay regression target, Super Mario Land is an early-action performance smoke target, Dr. Mario is a visual smoke target, and the CPU/PPU/APU subsystems have automated regression gates. A minimal CGB foundation exists for headers, explicit mode selection, CGB-only startup identity, banking, palette registers, KEY1 double-speed switching, CGB VRAM DMA, first-pass CGB BG/tile-attribute rendering, first-pass CGB OBJ palette/priority rendering, Crystal startup/staged-render/oracle coverage, and a deterministic Pokemon Crystal saved-game performance gate, but broad CGB game compatibility is not claimed yet.

Current verified status: May 26, 2026.

## Current Scope

- `.gb` ROM loading with cartridge header reporting, optional user-supplied DMG boot ROM mapping, and one-way `FF50` boot ROM unmapping.
- Header-driven cartridge mapper dispatch for ROM-only, ROM+RAM, MBC1, MBC1M, MBC2, MBC3 with RTC sidecar state, MBC5 including rumble-control behavior, and HuC1 basic banking/IR state. Unsupported cartridge profiles are recognized and warned about.
- CGB header detection for CGB-enhanced and CGB-only ROMs, explicit `--mode dmg|cgb|auto`, forced CGB mode for CGB-only cartridges, CGB post-boot CPU identity basics, Crystal first-frame/window-startup smoke coverage, staged Crystal CGB render-gate coverage, static/dynamic/saved-game-overworld Crystal PyBoy CGB oracle coverage, deterministic Crystal saved-game performance coverage, and a guarded CGB foundation for `FF4F` VRAM banking, `FF70` WRAM banking, `FF51`-`FF55` GDMA/HDMA VRAM DMA, `FF68`-`FF6C` palette/OPRI registers including mode-3 palette-data blocking, KEY1 `FF4D` STOP-triggered double-speed switching, first-pass CGB BG/window rendering with palette attributes, tile VRAM-bank attributes, X/Y flips, and first-pass CGB OBJ rendering with OBJ palette attributes, tile VRAM-bank selection, OAM/OPRI object ordering, BG priority attributes, LCDC.0 priority behavior, and RGB framebuffer output.
- SM83/LR35902 CPU core covering documented non-CB and CB-prefixed opcodes, stack/control flow, interrupts, HALT bug behavior, STOP wake/freeze behavior, and Blargg `cpu_instrs` verification.
- Cycle-aware bus timing for DIV/TIMA edge ticking, delayed TIMA reloads, serial transfer timing, OAM DMA, CGB double-speed CPU/device timing split, PPU access restrictions, hardware-style I/O read masks, and inert CGB-only registers on DMG.
- PPU timing and rendering for DMG background/window/sprite output, LCD modes, `LY`/`STAT`, line-153 behavior, DMG STAT write quirk, mode-3 penalties, selected mid-scanline register effects, OAM DMA sprite hiding, frame dumps, and the strict selected external PPU gate.
- Joypad input through `FF00`, CLI held buttons, STOP wake through selected joypad lines, and Tkinter keyboard controls.
- APU register/channel model with pulse, wave, noise, length, envelope, sweep, DAC gating, mixer, initial high-pass filtering, sample buffering, WAV dumps, live Windows audio playback, deterministic WAV identity checks, and Blargg `dmg_sound` regression coverage.
- Tkinter window mode with frame pacing, live audio, pause/reset/trace toggles, save-RAM lifecycle, and rolling frame/audio spike profiling.
- Pokemon Red performance hot paths for heavy MBC3/LCD-off copy/fill/decompression frames, with exact-vs-fast unit coverage for shadow-OAM/object helpers, PyBoy visual/OAM oracles for current sprite regressions, and an automated headless performance gate.
- Super Mario Land scripted early-1-1 action performance coverage with fixed headless metrics and optional live `window-profile` capture validation.
- Pokemon Crystal saved-game CGB overworld performance coverage with deterministic RTC/save input, per-60-frame FPS windows, CPU/frame drift checks, APU sample-drop checks, and PPU/DMA activity counters.

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

Run Super Mario Land, the current quick action/performance smoke target:

```powershell
python main.py .\roms\SML.gb --window --audio --max-instructions 0
```

Run a CGB-capable ROM through the foundation path. CGB-only cartridges such as Pokemon Crystal force CGB mode even when the CLI default is used:

```powershell
python main.py path\to\rom.gbc --mode cgb --max-instructions 0 --frames 1
python main.py .\roms\crystal.gbc --max-instructions 0 --frames 0
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

For CGB startup/display ordering work, `--profile-startup` prints one-shot Tk and first-frame diagnostics:

```powershell
python -B main.py .\roms\crystal.gbc --window --max-instructions 0 --frames 1 --profile-startup
```

The startup diagnostics report when Tk is created, when the initial host window is presented, and whether the first emulated frame advanced.

For repeatable Pokemon Crystal saved-game performance work:

```powershell
python -B scripts\verify_pokemon_crystal_performance.py --json-output qa-output\crystal-performance-gate.json
python -B scripts\benchmark_pokemon_crystal_overworld.py --audio-output --cprofile qa-output\crystal-audio-profile.prof --cprofile-text qa-output\crystal-audio-profile.txt
```

The verifier uses `saves\pokemon-crystal-test.sav` plus the halted `saves\pokemon-crystal-test.sav.rtc` fixture, runs fixed wall-frame input, emits 60-frame metric windows, checks deterministic CPU instruction/cycle/frame totals, and fails on low FPS, unpinned RTC state, missing audio samples, or dropped APU samples.

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
Ran 427 tests in 0.657s
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

The default APU lane runs all 12 single `dmg_sound` ROMs. Current baseline: all ROMs `01` through `12` pass, including the CH3 wave-RAM read/retrigger/write cases.

Run the synthetic CGB foundation smoke verifier:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_cgb_foundation.py --json-output qa-output\cgb-foundation.json
```

This verifies the new CGB mode/register foundation, including synthetic GDMA/HDMA data movement and KEY1 double-speed timing-domain checks, and when `roms\crystal.gbc` exists locally checks that Pokemon Crystal is detected as CGB-only and starts with `Mode: CGB` plus CGB post-boot CPU identity. It is not a CGB game compatibility claim.

Run the Pokemon Crystal CGB startup/window smoke verifier:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_crystal_window_startup.py --json-output qa-output\crystal-window-startup-headless.json
python -B scripts\verify_crystal_window_startup.py --run-window --json-output qa-output\crystal-window-startup.json
```

The headless lane verifies Crystal reaches the first frame in CGB mode. The window lane verifies Tk presents before the long first-frame step begins, so a CGB ROM window does not appear only after the program exits.

Run the Pokemon Crystal CGB render gate:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_crystal_cgb_render.py --checkpoint-frames 60,600,2400,3600 --require-crystal-attributes --json-output qa-output\crystal-cgb-render-staged.json --frame-output-dir qa-output\crystal-cgb-stages
```

The staged lane verifies Crystal remains in CGB mode at 60, 600, 2400, and 3600 frames; writes deterministic JSON metrics for CPU cycles, RGB color counts, palette RAM, VRAM DMA, KEY1 state, CGB tile attributes, LCDC/STAT/LY, and WRAM/VRAM bank state; and dumps BMP frames under `qa-output\crystal-cgb-stages`. Attribute assertions are enforced from the 2400-frame stage onward. `--frames 60` remains available for a quick single-checkpoint smoke, and `--button-script` / `--button-script-path` can advance later title/menu paths as those are added. The script also includes synthetic CGB BG palette/bank/flip checks plus CGB OBJ palette, OBJ tile-bank, OAM ordering, OPRI ordering, BG priority, and LCDC.0 priority checks.

Run the Pokemon Crystal CGB PyBoy visual oracle:

```powershell
python -B scripts\verify_crystal_cgb_oracle.py --output-dir qa-output\crystal-cgb-pyboy-oracle --json-output qa-output\crystal-cgb-pyboy-oracle.json
```

Run the dynamic Crystal CGB PyBoy oracle:

```powershell
python -B scripts\verify_crystal_cgb_oracle.py --scenario dynamic --output-dir qa-output\crystal-cgb-pyboy-oracle-dynamic --json-output qa-output\crystal-cgb-pyboy-oracle-dynamic.json
```

Run the saved-game Crystal CGB overworld PyBoy oracle:

```powershell
python -B scripts\verify_crystal_cgb_oracle.py --scenario overworld --output-dir qa-output\crystal-cgb-pyboy-oracle-overworld --json-output qa-output\crystal-cgb-pyboy-oracle-overworld.json
```

The oracle runs GBemu and PyBoy in CGB mode using named scenarios and the same `frame:buttons[:duration]` input-script format. The static scenario checks 60, 600, 2400, 3600, and a scripted 4800-frame title/menu checkpoint. The dynamic scenario keeps the 2400/3600/4800 static locks and adds title animation/palette, logo transition, gender-menu text, explicit cursor-down/cursor-up movement, dialog transition/text, clock menu, and confirmation-menu checkpoints through frame 7800. The overworld scenario loads `saves\pokemon-crystal-test.sav`, converts the GBemu JSON `.sav.rtc` sidecar into PyBoy's RTC format, freezes both emulators to the sidecar `saved_at` timestamp, continues into a real saved-game map, moves through NPC/object-heavy screens, opens/closes the menu, and captures an NPC text box through frame 11400. GBemu advances by wall-frame CPU-cycle targets instead of raw `ppu.frame_count`, because Crystal can disable LCD and stall display-frame counting during transitions. The JSON contains diff pixels, major-diff pixels, max color delta, unique color counts, nonblack coverage/bounds, palette/DMA/KEY1/CGB-attribute metrics, per-stage pass/fail status, scenario labels, save-file source, RTC source, GBemu frame/input clock metadata, and mismatch class. Source-debug checkpoints classify pixels as BG/window/OBJ with tile id, attr byte, palette, color id, priority, visible mismatch class, source-state class, compact mismatch samples, and OAM first-diff summaries. Current saved-game oracle runs are pixel-exact for the covered checkpoints, but this remains staged evidence rather than a broad CGB compatibility claim.

Run the Pokemon Crystal saved-game performance gate:

```powershell
python -B scripts\verify_pokemon_crystal_performance.py --json-output qa-output\crystal-performance-gate.json
```

The gate runs fixed Crystal overworld and overworld-with-audio slices from the deterministic save/RTC fixture, reports per-60-frame `pokemon-crystal-overworld-window` metrics, enforces FPS thresholds, checks deterministic frame/CPU instruction/cycle totals, and fails if RTC is not halted or APU output drops samples.

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

Run the Super Mario Land early-action performance gate:

```powershell
python -B scripts\verify_super_mario_land_performance.py --json-output qa-output\super-mario-land-performance-gate.json
```

Run the same SML action path with live Tkinter/audio `window-profile` capture:

```powershell
python -B scripts\verify_super_mario_land_performance.py --scenario action-audio --run-live-window --json-output qa-output\super-mario-land-live-performance-gate.json
```

Latest sprite-scene oracle and performance-gate evidence:

- Oak's Lab encyclopedia crop: `diff_pixels=0`; OAM tiles `7C 7D 7E 7F 7C 7D 7E 7F`.
- Sprite-heavy saved-game scene: full-screen `diff_pixels=0`; 28 visible OAM entries match PyBoy for y, x, tile, and attributes.
- Blargg `dmg_sound`: all 12 single ROMs pass in the default lane, including the CH3 wave-RAM edge cases.
- Pokemon Red performance gate: text `run_fps=98.12`; sprites `run_fps=82.15`; sprites with headless audio output `run_fps=66.03`, `apu_samples=443012`, `apu_dropped_samples=0`.
- Super Mario Land action gate: headless action `run_fps=92.25`; action with headless audio output `run_fps=75.51`, `apu_samples=445592`, `apu_dropped_samples=0`. Latest live action capture checked 10 non-startup profile windows with min `wall_fps=46.84`, min queue `33.5 ms`, and zero audio underruns/drops.
- Pokemon Crystal performance gate: overworld `run_fps=65.51`, worst 60-frame window `64.31`; overworld with headless audio output `run_fps=55.07`, worst 60-frame window `53.43`, `apu_samples=443012`, `apu_dropped_samples=0`; both lanes match `cpu_instr=1624904`, `cpu_cycles=42134400`, and `ppu_frames=600`. The live Crystal window gate is now good enough for the current baseline: 100 profile windows captured, 8 measured windows after warmup, min `wall_fps=46.04`, average `47.17`, min audio queue `33.3 ms`, and zero audio underruns/drops.
- CGB foundation/render smoke: synthetic header/mode/register checks pass for CGB detection, DMG inert behavior, forced CGB-only startup, VRAM/WRAM bank selects, CGB palettes with mode-3 data access blocking, OPRI, GDMA/HDMA, and KEY1 STOP-triggered double-speed switching. Synthetic double-speed checks verify TIMA/serial/OAM DMA stay on the CPU-speed domain while PPU/APU/HDMA stay on the normal-speed device domain. Local Pokemon Crystal smoke detects `CGB only ($C0)`, default `Mode: CGB`, `--mode auto` `Mode: CGB`, CPU `A=$11`, and a staged 60/600/2400/3600-frame CGB render gate. Latest staged evidence: 60 frames has 3 unique RGB colors, 32 HDMA blocks / 512 VRAM-DMA bytes, and no KEY1 arm/toggle; 600 frames is a solid transition frame with nonzero palettes and CGB mode; 2400 frames has 6 unique RGB colors, 607 nonzero attrs, 448 bank attrs, 448 X-flip attrs, and 448 Y-flip attrs; 3600 frames has 18 unique RGB colors, 832 nonzero attrs, 576 bank attrs, 384 X-flip attrs, and 384 Y-flip attrs. Synthetic CGB renderer checks also cover OBJ palette selection, OBJ tile VRAM-bank selection, OAM-order priority, OPRI DMG-style priority, BG priority attributes, and LCDC.0 priority behavior.
- Crystal PyBoy CGB visual oracle: static 60/600/2400/3600/4800 wall-frame comparison passes with 30 PNG artifacts, including targeted crops. Dynamic `--scenario dynamic` now adds 11 title animation, menu movement, text, clock, and confirmation checkpoints through frame 7800; all 11 current dynamic checkpoints have `diff_ratio=0.0000`, `major_diff_ratio=0.0000`, `nonblack_delta_ratio=0.0000`, `visible_mismatch_class=none`, and `mismatch_class=none`. Saved-game `--scenario overworld` loads `saves\pokemon-crystal-test.sav`, freezes GBemu and PyBoy to `saves\pokemon-crystal-test.sav.rtc` `saved_at=1779757132.7976005`, and covers checkpoints 4800, 5400, 7200, 8400, 8580, 9060, 9600, 10200, 10800, and 11400. The latest full overworld run is pixel-exact at all 10 checkpoints with `mismatch_class=none`; the strict 5400/8400/9600 source-state run also passes with `diff_pixels=0`, `major_diff_pixels=0`, and `mismatch_class=none`. The earlier saved-game palette drift was the RTC/time-of-day baseline, not RGB555 conversion or palette-register behavior.

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

- CGB foundation is started, and CGB-only cartridges now enter CGB mode with basic post-boot identity, KEY1 STOP-triggered double-speed switching, GDMA/HDMA VRAM DMA, mode-3 palette-data access blocking, first-pass BG/window palette/tile-attribute rendering, first-pass OBJ palette/priority rendering, PyBoy-compatible RGB555 expansion, staged Crystal metrics, deterministic saved-game RTC oracle support, a deterministic Crystal saved-game performance gate, and a PyBoy CGB visual oracle with source-debug classification. Exact STOP speed-switch blackout timing, exact GDMA/HDMA CPU-stall timing, exact CGB FIFO/raster timing, full CGB boot flow, broader CGB visual oracles, and broad CGB game compatibility are not implemented yet.
- PPU coverage is strong for the selected strict gate, but the emulator still does not model a complete per-dot pixel FIFO or every possible mid-scanline raster edge case.
- APU/audio is functional, deterministic for current PCM identity checks, and covered by a fully passing Blargg `dmg_sound` single-ROM lane, but mature analog filtering and stricter APU-suite compatibility are still pending.
- Commercial compatibility is early. Pokemon Red is the primary tested gameplay target; Super Mario Land is an action/performance smoke target; Dr. Mario is a visual smoke target. Other games should be treated as exploratory until they are added to the compatibility matrix.
- Performance includes several real-ROM-specific hot paths. They preserve current instruction/cycle/audio identity for the covered Pokemon Red, Super Mario Land, and Pokemon Crystal windows. Crystal CGB gameplay performance is good enough for now; the next major workstream should be CGB correctness, especially FIFO/raster accuracy, rather than more FPS tuning.

Detailed compatibility evidence lives in `docs\compatibility.md`; the Pan Docs inventory lives in `docs\pandocs-inventory.md`; the active roadmap lives in `docs\next-stages.md`.
