# Compatibility Matrix

Date: 2026-05-21

This matrix tracks evidence in this repository. `Pass` means a ROM suite or
component behavior is currently verified by an automated test. `Partial` means
the subsystem has useful coverage but is not hardware-complete. `Pending` means
the emulator has no current compatibility claim for that target.

## Test Suites

| Target | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Unit test suite | Pass | `.\.tools\python-3.12.4-embed-amd64\python.exe -B -m unittest discover -v` | Ran 254 tests on 2026-05-21. Covers CPU helpers, bus timing, PPU unit behavior, cartridge mappers, runtime, display trace toggles, joypad, APU model pieces, and the `dmg-acid2` regression. |
| Blargg `cpu_instrs` individual ROMs `01` through `11` | Pass | `scripts\verify_cpu.py`, direct `main.py --stop-on-serial-result` runs | Current runs passed all individual CPU instruction ROMs. |
| Blargg combined `cpu_instrs.gb` | Pass | Direct `main.py --stop-on-serial-result` run | Current run reached the serial `Passed` result. |
| `dmg-acid2` | Pass | `.\.tools\python-3.12.4-embed-amd64\python.exe -B -m unittest tests.test_dmg_acid2 -v` | The first completed frame matches Matt Currie's official DMG reference image RGB hash `2ba8286c29ae381838c71a88614302ce05f2b26102d1ed8dc51e25f83fcccc67`. Use this as the main PPU visual regression before broad commercial ROM testing. |
| PPU external gate | Pass | `.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_ppu.py --strict --max-steps 3000000` | Strict baseline passes required `dmg-acid2`, all current Mooneye acceptance/ppu ROMs: `stat_irq_blocking`, `stat_lyc_onoff`, `vblank_stat_intr-GS`, `hblank_ly_scx_timing-GS`, `intr_1_2_timing-GS`, `intr_2_0_timing`, `intr_2_mode0_timing`, `intr_2_mode0_timing_sprites`, `intr_2_mode3_timing`, `intr_2_oam_ok_timing`, `lcdon_timing-GS`, and `lcdon_write_timing-GS`, plus selected Mealybug mode-3 image cases: `m3_lcdc_tile_sel_change`, `m3_lcdc_bg_map_change`, `m3_bgp_change`, `m3_window_timing`, `m3_window_timing_wx_0`, `m3_bgp_change_sprites`, `m3_scx_high_5_bits`, `m3_scx_low_3_bits`, `m3_scy_change`, `m3_scx_high_5_bits_change2`, `m3_scy_change2`, `m3_lcdc_win_map_change`, `m3_lcdc_win_map_change2`, `m3_lcdc_tile_sel_win_change`, `m3_lcdc_obj_en_change`, `m3_lcdc_obj_en_change_variant`, `m3_lcdc_obj_size_change`, `m3_lcdc_obj_size_change_scx`, `m3_obp0_change`, `m3_lcdc_bg_en_change`, `m3_wx_4_change`, `m3_wx_4_change_sprites`, `m3_wx_5_change`, `m3_wx_6_change`, `m3_lcdc_win_en_change_multiple`, and `m3_lcdc_win_en_change_multiple_wx`. Mooneye build: `mts-20240926-1737-443f6e1`. |
| Mealybug Round 1 candidate lane | XFail tracked | `.\.tools\python-3.12.4-embed-amd64\python.exe -B scripts\verify_ppu.py --skip-dmg-acid2 --skip-mooneye --include-mealybug-candidates --max-steps 3000000` | Adjacent LCDC/window/tile cases are runnable from the main PPU verifier but are not promoted into the strict gate until they pass. Current pixel diffs: `m3_lcdc_tile_sel_change2` 261 and `m3_lcdc_tile_sel_win_change2` 272. Both are tracked against CPU CGB C reference images because this local archive does not include DMG references for those two cases; upstream notes call out CGB-specific same-cycle `TILE_SEL` behavior, so keep them candidate-only until a DMG oracle or CGB PPU mode exists. |
| Other PPU ROM suites | Pending | Not yet part of the automated gate | Broader FIFO/timing suites still need to be added after the current strict Mooneye and Mealybug gate. |
| Commercial DMG games | Smoke only | `.\.tools\python-3.12.4-embed-amd64\python.exe -B main.py roms\DrMario.gb --frames 3`; user window run on 2026-05-20 | `DrMario.gb` boots and advances 3 headless frames; user-reported interactive window run showed no obvious visual glitches, with FPS still below target. Treat this as a smoke check, not a compatibility oracle. |

## Subsystems

| Area | Status | Current Coverage | Remaining Risk |
| --- | --- | --- | --- |
| CPU instruction behavior | Pass | All documented non-CB and CB opcodes, flags, stack/control flow, interrupts, HALT bug, STOP wake behavior, STOP divider/timer freeze behavior, and Blargg `cpu_instrs` coverage. | Exact ordering for obscure internal cycles should keep being checked as additional timing ROMs are added. |
| Memory bus and timers | Partial | DIV/TIMA edge ticking, delayed TIMA reload, STOP/speed-switch divider reset and STOP timer freeze behavior, serial transfer timing, OAM DMA bus blocking, boot ROM overlay, IO read masks, and CGB-only DMG inert registers. | More hardware timing tests are needed for rare write-ordering and interrupt-boundary cases. |
| PPU and framebuffer | Partial | LCD modes, `LY`/`STAT`, VBlank, line-153 wrap, DMG STAT write quirk, BG/window/OBJ rendering, palette handling, scroll wrapping, sprite priority, Mode-2-sampled latched `WY`, mode-3 penalties, segmented mid-line register effects including pre-trigger window-disable stall shortening, early window-enable pulse cancellation, window-enable reactivation with updated WX, background fetch-phase restart after window disable, window-restart palette timing, source-bit changes, BG/window enable output-mask timing around sprite fetches, tile-data high-byte fetch-boundary timing, aligned-sprite low-byte boundary splitting, initial window tile-data pulse targeting with aligned sprites, repeated early window tile-data pulse claiming, line-0 non-window tile-data source claiming for repeated very-early OBJ fetch pulses, after-start window tile-data byte splitting, WX=0 window restart timing, WX hidden-edge first-pixel glitch timing, WX=4/WX=5 hidden-edge reactivation zero-pixel timing, high-WX window fetch preservation across later WX writes, line-latched mode-2 sprite selection and sprite tile-data byte-level `OBJ_SIZE` sampling across mid-mode-3 writes, sprite-aware BGP timing including HBlank tail updates after already-emitted sprites and preserved left-edge OBJ fetch stalls after OBJ disable, selected mode-3 SCX/SCY scroll-raster fetch timing, LCD-on access-window timing, OAM DMA sprite hiding, `dmg-acid2` reference-image matching, and the selected strict external Mooneye/Mealybug PPU gate are covered. | The selected external PPU gate is strict-green. Full per-dot pixel FIFO, complete mid-scanline raster effects beyond the selected cases, and broader PPU ROM-suite coverage remain pending. |
| Joypad/input | Partial | Active-low matrix reads, interrupt requests on selected high-to-low transitions, held-button non-retriggering, STOP wake, CLI held buttons, and Tkinter keyboard input. | Host input is basic and has not been validated against broad game menus or real-time gameplay. |
| Cartridge mappers | Partial | ROM-only no-RAM behavior, ROM+RAM, MBC1, MBC1M, MBC2, MBC3 with 0-7 RAM-bank selection/64 KiB RAM and RTC, MBC5 including rumble bit behavior, HuC1 banking/IR state, save RAM helpers, and unsupported-mapper warnings are unit-tested. | Unsupported or unverified specialty hardware includes MMM01, MBC6, MBC7 sensor behavior, Pocket Camera, Bandai TAMA5, and HuC3. |
| APU/audio | Partial | Register model, `NR52`, DAC-gated activity, trigger handling, wave RAM access including active-CH3 blocking, CH3 playback delay and first-sample fetch order, length counters including DIV-APU frame-step extra length clocking and `DIV` write falling-edge clocking, envelope trigger timing before envelope frame steps, CH1 sweep including negate-clear and shift-zero edge cases, pulse/wave/noise timers including CH4 clock-shift stop behavior, signed active-channel DAC output, `NR50`/`NR51` mixing, initial high-pass output filtering, sample buffering, and WAV dumps have unit coverage. | Live host audio, mature hardware-accurate analog filtering, and APU ROM-suite compatibility are pending. |
| Runtime/display | Partial | Frame stepping, stop conditions, save lifecycle, reset preserving cartridge state, frame dumps, Tkinter window mode, keyboard controls, and runtime trace toggles. | Runtime pacing and UI are functional but not a substitute for hardware-level timing compatibility. |

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

As of this matrix, the project is roughly 60% of the way from CPU milestone
to a practical DMG emulator, and much less than that if measured against
cycle-perfect hardware compatibility. The CPU and common mapper base are strong;
the current selected external PPU gate is strict-green, and the main remaining
work is expanding FIFO/timing coverage beyond that gate, broader ROM-suite
compatibility tracking, and audio output/filtering accuracy.
