# GBemu

Milestone 1 is a DMG-only Game Boy CPU, memory bus, cartridge header parser, and test-ROM harness written in Python.

Implemented scope:

- `.gb` ROM loading, cartridge header summary, and optional user-supplied DMG boot ROM mapping with one-way unmapping via `FF50`.
- Basic 64 KiB memory bus map with ROM, VRAM placeholder, cartridge RAM window, WRAM, echo RAM, OAM placeholder, I/O, HRAM, IE, hardware-style unused-bit read masks and ignored writes for key unusable MMIO/APU and documented/common-undocumented CGB-only registers on DMG, edge-based DIV/TIMA timer ticking for DIV and TAC writes, DIV reset and timer freeze across STOP/speed-switch handling, delayed TIMA overflow reload behavior including reload maturation before later CPU instruction writes and during common control-flow/stack internal cycles, cycle-stepped OAM DMA with DMG HRAM-only CPU access during active transfers and per-cycle PPU line visibility notification on both sides of dot transitions, and PPU-mode CPU access restrictions for VRAM/OAM.
- Blargg-style serial output: starting an internal-clock transfer through `$FF02` completes after 4096 CPU cycles, emits the byte in `$FF01`, receives `$FF` for the disconnected-link case, clears the transfer-start bit, and requests the serial interrupt.
- Cartridge mapper support is started: header-driven mapper capability profiles and mapper dispatch classes centralize mapper/RAM/battery/timer/rumble/IR feature detection, ROM/RAM control behavior, and CLI mapper support warnings, while ROM-only no-RAM behavior, plain ROM+RAM cartridges, MBC1, MBC1M multi-cart detection/banking, MBC2, MBC3, MBC5, and HuC1 ROM/RAM banking basics plus save-RAM byte dump/load helpers are covered by unit tests. MBC1 raw lower ROM bank register behavior, advanced-mode lower ROM area banking, large-ROM fixed RAM-bank wiring, MBC2 internal 4-bit RAM behavior, MBC3 0-7 RAM-bank selection including 64 KiB RAM and smaller-RAM bank wrapping, RTC latch/timekeeping, halt, day carry, sidecar save/load behavior, invalid RTC selector handling, MBC5 rumble bank/control behavior, and HuC1 IR mode state are covered by unit tests.
- SM83/LR35902 CPU core with all documented non-CB opcodes, all CB-prefixed opcodes, stack operations, calls, jumps, returns, restarts, flags, basic interrupt dispatch, HALT bug behavior, STOP wake on enabled interrupts or selected joypad lines, STOP divider/timer freeze behavior, and a small KEY1/STOP speed-switch shim for Blargg harness compatibility.
- Trace and step modes for CPU debugging.
- `Emulator` runtime wrapper coordinates cartridge, bus, CPU, PPU, buttons, bounded frame runs, save-RAM helpers, reset with preserved cartridge RAM/RTC state, and non-negative run-limit validation; already-satisfied stop targets exit before stepping the CPU.
- Optional Tkinter display window with keyboard input, frame pacing, pause, reset, and quit controls.
- Minimal PPU timing and DMG framebuffer rendering are now started:
  `LY`, `STAT`, line-153 `LY` wrap timing, DMG STAT write quirk, STAT mode-source interrupts for HBlank/VBlank/OAM, VBlank interrupt request, LCD enable/disable with framebuffer and direct scanline blanking, SCX/window/OBJ mode-3 timing penalties, SCX low-bit latching at mode-3 start, initial segmented mode-3 raster handling for palette and same-enable `LCDC` writes plus scroll writes delayed to background fetch boundaries with sprite/window stall-aware write positioning and selected per-fetch SCY tile-map/tile-data-byte sampling, window-restart palette write clamping, segmented window-stall resync for mid-line window disable before the trigger point, mode-3 `WX` writes and window-enable changes before and after the window trigger point including hidden and missed-trigger cases, line-start render-state snapshots for window context, background/window tile rendering including 256x256 scroll wrapping, LCDC background/window and OBJ enable gating, Mode-2-sampled latched window `WY` trigger and `WX=167` hidden behavior, OBJ rendering including the 10-sprites-per-scanline cap, 8x16 sprite tile selection, X/Y flip, OBP0/OBP1 selection, BG-over-OBJ priority masking, line-latched OAM DMA sprite hiding without OBJ fetch stall timing, and PPM/BMP frame dumps are covered by unit tests.
- ROM-driven PPU coverage includes a small CPU/bus/PPU integration ROM, a `dmg-acid2` visual regression that compares the rendered RGB framebuffer to Matt Currie's official DMG reference image hash, and `scripts\verify_ppu.py` for the current strict external PPU regression gate.
- Joypad input is started: `FF00` active-low action/direction matrix reads, high-to-low interrupt requests, held-button non-retrigger behavior, and host button-name validation are covered by unit tests.
- APU modeling is started: `NR52` power control, wave RAM access including active-CH3 blocking, DAC-gated channel active flags, trigger handling, CH3 playback delay and first-sample fetch order, length counters including DIV-APU frame-step extra length clocking and `DIV` write falling-edge clocking, volume envelopes including trigger timing before envelope frame steps, CH1 frequency sweep including negate-clear and shift-zero edge cases, pulse/wave/noise frequency timers including CH4 clock-shift stop behavior, raw channel samples, signed active-channel DAC output, `NR50`/`NR51` mixing, initial high-pass output filtering, bounded sample buffering, and WAV dumps with parent directory creation are covered by unit tests.

Run a ROM:

```powershell
python main.py path\to\rom.gb --max-instructions 10000000 --stop-on-serial-result
```

Using the local embeddable Python runtime downloaded for verification:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe main.py roms\cpu_instrs\cpu_instrs\cpu_instrs.gb --stop-on-serial-result
```

Run with a user-supplied DMG boot ROM:

```powershell
python main.py path\to\rom.gb --boot-rom path\to\dmg_boot.bin
```

Write a trace file:

```powershell
python main.py roms\cpu_instrs.gb --trace-file traces\cpu_instrs.log --max-instructions 200000
```

Dump the current framebuffer to an ASCII PPM image:

```powershell
python main.py path\to\rom.gb --max-instructions 1000000 --dump-frame traces\frame.ppm
```

Dump the current framebuffer to a Windows-viewable BMP image:

```powershell
python main.py path\to\rom.gb --frames 1 --dump-frame-bmp traces\frame.bmp
```

Dump generated APU output to a stereo 16-bit WAV file:

```powershell
python main.py path\to\rom.gb --frames 60 --dump-audio traces\audio.wav
```

Run for a fixed number of frames with held buttons:

```powershell
python main.py path\to\rom.gb --frames 3 --buttons a,start --dump-frame traces\frame.ppm
```

Run with a Tkinter display window and keyboard input:

```powershell
python main.py path\to\rom.gb --window --scale 3
```

Profile windowed frame pacing and framebuffer upload time:

```powershell
python main.py path\to\rom.gb --window --scale 3 --profile-window
```

Benchmark raw headless emulation speed for a fixed frame count:

```powershell
Measure-Command { .\.tools\python-3.12.4-embed-amd64\python.exe -B main.py path\to\rom.gb --frames 300 --max-instructions 0 }
```

Load and save cartridge RAM and MBC3 RTC state around a run:

```powershell
python main.py path\to\rom.gb --save-file saves\game.sav --frames 60
```

For MBC3 timer cartridges, `game.sav` remains raw cartridge RAM bytes and RTC state is saved beside it as `game.sav.rtc`.

Run tests:

```powershell
python -m unittest discover -v
```

If `tests` is not auto-discovered by your Python install, use:

```powershell
python -m unittest discover -s tests -t . -v
```

Run the full CPU milestone verifier, including unit tests and Blargg
`cpu_instrs` individual plus combined ROMs:

```powershell
python scripts\verify_cpu.py
```

With the local runtime:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe scripts\verify_cpu.py
```

Run the main PPU visual regression:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B -m unittest tests.test_dmg_acid2 -v
```

Run the current strict external PPU gate, including the Mooneye acceptance/ppu ROMs and the selected Mealybug mode-3 image cases:

```powershell
.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_ppu.py --strict --max-steps 3000000
```

The selected external PPU gate currently requires `dmg-acid2`, the current Mooneye acceptance/ppu ROMs, and the Mealybug mode-3 image cases listed in `scripts\verify_ppu.py`'s `MEALYBUG_MODE3_TESTS`, including the promoted Round 1 LCDC/window/OBJ/SCX/WX cases through `m3_lcdc_obj_en_change_variant`, `m3_lcdc_bg_en_change`, `m3_scx_low_3_bits`, `m3_wx_4_change`, `m3_wx_5_change`, `m3_wx_6_change`, `m3_lcdc_win_en_change_multiple`, and `m3_lcdc_win_en_change_multiple_wx`. Keep `--strict` on when validating the current compatibility baseline.

Current limitation: this emulator does not yet include a complete per-dot pixel FIFO, all mid-scanline raster effects, live host audio playback, mature hardware-accurate audio filtering, or broad gameplay compatibility. Treat `dmg-acid2` and `scripts\verify_ppu.py --strict` as the PPU gates before broad commercial ROM testing, then expand the external PPU suite as additional timing gaps are investigated.

Next-stage planning is in `docs\next-stages.md`; current compatibility evidence is tracked in `docs\compatibility.md`.
