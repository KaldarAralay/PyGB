# Stage 1 CPU Milestone Audit

Date: 2026-05-20

Status note, 2026-05-23: this is a historical audit for the Stage 1 CPU milestone. Current project status, real-ROM evidence, Pan Docs inventory, audio/display status, and active roadmap live in `docs\compatibility.md`, `docs\pandocs-inventory.md`, and `docs\next-stages.md`.

Scope source: `1st stage.txt`

## Requirement Evidence

- ROM loading and header reporting: `cartridge.py`, `main.py`, and unit test `test_header_parsing_and_checksum`.
- Optional boot ROM overlay and one-way `FF50` unmapping: `bus.py` and unit tests `test_boot_rom_overlays_cartridge_until_ff50_disable`, `test_boot_rom_starts_with_power_on_io_state`.
- Memory bus map: `bus.py` and unit tests `test_memory_ranges_and_echo_ram`, `test_div_and_tima_tick`, `test_oam_dma_copy`, `test_cgb_only_io_registers_are_inert_on_dmg`.
- CPU registers, register pairs, flags, fetch/decode/execute, stack, jumps, calls, returns, restarts, HALT/STOP behavior: `cpu.py`, `opcodes.py`, and Blargg `cpu_instrs` verification.
- Full normal and CB-prefixed opcode handling: pattern decoders in `cpu.py`; verified by Blargg individual `cpu_instrs` ROMs and combined `cpu_instrs.gb`.
- Serial output, 4096-cycle internal-clock transfer timing, transfer-start clearing, and interrupt request for test ROMs: `bus.py` and unit tests `test_serial_transfer_hook`, `test_serial_internal_clock_checks_control_bits`.
- STOP wake on selected joypad lines: `cpu.py`, `joypad.py`, and unit test `test_stop_wakes_for_selected_joypad_line_even_without_ie`.
- Debugging support: `main.py`, `debug.py`; trace smoke file `traces/trace-smoke.log`.
- Optional max instruction count: `main.py --max-instructions`; result stop helper `--stop-on-serial-result`.

## Verification Commands

Using the local embeddable Python runtime:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.tools\python-3.12.4-embed-amd64\python.exe scripts\verify_cpu.py
```

Latest verification notes after the mode-3 window missed-trigger, CPU per-access/control-flow timing coverage, strict decoded-instruction cycle accounting, per-cycle bus/PPU scheduling with boundary-cycle DMA visibility, STOP divider/timer freeze coverage, line-latched OAM DMA sprite-hiding/timing unit coverage, header-driven cartridge mapper capability profile/dispatch coverage, ROM-only no-RAM behavior coverage, MBC3 0-7 RAM-bank selection and 64 KiB RAM coverage, signed APU DAC mixer coverage, initial APU high-pass filter coverage, APU DIV-APU frame-step length clocking and `DIV` write falling-edge coverage, APU envelope trigger timing coverage, APU CH1 sweep negate/shift-zero coverage, active CH3 wave RAM access coverage, CH3 playback-delay coverage, and CH4 clock-shift stop coverage:

- Current unit suite at the time of the latest full project verification: 358 unit tests passed with `.\.tools\python-3.12.4-embed-amd64\python.exe -B -m unittest discover -v`.
- Current Blargg CPU verification passed individual `cpu_instrs` ROMs `01` through `11`.
- Current Blargg CPU verification passed combined `cpu_instrs.gb` with serial output:
  `cpu_instrs\n\n01:ok  02:ok  03:ok  04:ok  05:ok  06:ok  07:ok  08:ok  09:ok  10:ok  11:ok  \n\nPassed`

## Out Of Scope For This Stage

- PPU rendering was out of scope for Stage 1, but now has strict selected PPU-gate coverage. See `docs\compatibility.md`.
- Full APU/audio host output was out of scope for Stage 1; live audio now exists, while mature hardware-accurate analog filtering remains pending.
- Full cartridge hardware beyond the minimal MBC1 ROM banking needed by the CPU test ROM was out of scope for Stage 1; common mapper support is now substantially broader.
- Gameplay compatibility was out of scope for Stage 1; Pokemon Red is now the primary real-ROM regression target.
