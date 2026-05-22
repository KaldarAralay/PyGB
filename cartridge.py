from __future__ import annotations

import time
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


CARTRIDGE_TYPES = {
    0x00: "ROM ONLY",
    0x01: "MBC1",
    0x02: "MBC1+RAM",
    0x03: "MBC1+RAM+BATTERY",
    0x05: "MBC2",
    0x06: "MBC2+BATTERY",
    0x08: "ROM+RAM",
    0x09: "ROM+RAM+BATTERY",
    0x0B: "MMM01",
    0x0C: "MMM01+RAM",
    0x0D: "MMM01+RAM+BATTERY",
    0x0F: "MBC3+TIMER+BATTERY",
    0x10: "MBC3+TIMER+RAM+BATTERY",
    0x11: "MBC3",
    0x12: "MBC3+RAM",
    0x13: "MBC3+RAM+BATTERY",
    0x19: "MBC5",
    0x1A: "MBC5+RAM",
    0x1B: "MBC5+RAM+BATTERY",
    0x1C: "MBC5+RUMBLE",
    0x1D: "MBC5+RUMBLE+RAM",
    0x1E: "MBC5+RUMBLE+RAM+BATTERY",
    0x20: "MBC6",
    0x22: "MBC7+SENSOR+RUMBLE+RAM+BATTERY",
    0xFC: "POCKET CAMERA",
    0xFD: "BANDAI TAMA5",
    0xFE: "HuC3",
    0xFF: "HuC1+RAM+BATTERY",
}

ROM_SIZES = {
    0x00: "32 KiB (2 banks)",
    0x01: "64 KiB (4 banks)",
    0x02: "128 KiB (8 banks)",
    0x03: "256 KiB (16 banks)",
    0x04: "512 KiB (32 banks)",
    0x05: "1 MiB (64 banks)",
    0x06: "2 MiB (128 banks)",
    0x07: "4 MiB (256 banks)",
    0x08: "8 MiB (512 banks)",
    0x52: "1.1 MiB (72 banks)",
    0x53: "1.2 MiB (80 banks)",
    0x54: "1.5 MiB (96 banks)",
}

RAM_SIZES = {
    0x00: "No RAM",
    0x01: "Unused/unknown",
    0x02: "8 KiB (1 bank)",
    0x03: "32 KiB (4 banks)",
    0x04: "128 KiB (16 banks)",
    0x05: "64 KiB (8 banks)",
}

RAM_SIZE_BYTES = {
    0x00: 0,
    0x01: 2 * 1024,
    0x02: 8 * 1024,
    0x03: 32 * 1024,
    0x04: 128 * 1024,
    0x05: 64 * 1024,
}

MBC2_RAM_SIZE = 0x200
RTC_SAVE_VERSION = 1
NINTENDO_LOGO = bytes.fromhex(
    "CE ED 66 66 CC 0D 00 0B 03 73 00 83 00 0C 00 0D "
    "00 08 11 1F 88 89 00 0E DC CC 6E E6 DD DD D9 99 "
    "BB BB 67 63 6E 0E EC CC DD DC 99 9F BB B9 33 3E"
)


class MapperKind(Enum):
    ROM = "ROM"
    MBC1 = "MBC1"
    MBC2 = "MBC2"
    MBC3 = "MBC3"
    MBC5 = "MBC5"
    HUC1 = "HuC1"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class CartridgeTypeSpec:
    mapper: MapperKind
    ram: bool = False
    battery: bool = False
    timer: bool = False
    rumble: bool = False
    ir: bool = False


UNSUPPORTED_CARTRIDGE_TYPE = CartridgeTypeSpec(MapperKind.UNSUPPORTED)
CARTRIDGE_TYPE_SPECS = {
    0x00: CartridgeTypeSpec(MapperKind.ROM),
    0x01: CartridgeTypeSpec(MapperKind.MBC1),
    0x02: CartridgeTypeSpec(MapperKind.MBC1, ram=True),
    0x03: CartridgeTypeSpec(MapperKind.MBC1, ram=True, battery=True),
    0x05: CartridgeTypeSpec(MapperKind.MBC2, ram=True),
    0x06: CartridgeTypeSpec(MapperKind.MBC2, ram=True, battery=True),
    0x08: CartridgeTypeSpec(MapperKind.ROM, ram=True),
    0x09: CartridgeTypeSpec(MapperKind.ROM, ram=True, battery=True),
    0x0B: UNSUPPORTED_CARTRIDGE_TYPE,
    0x0C: CartridgeTypeSpec(MapperKind.UNSUPPORTED, ram=True),
    0x0D: CartridgeTypeSpec(MapperKind.UNSUPPORTED, ram=True, battery=True),
    0x0F: CartridgeTypeSpec(MapperKind.MBC3, timer=True, battery=True),
    0x10: CartridgeTypeSpec(MapperKind.MBC3, ram=True, timer=True, battery=True),
    0x11: CartridgeTypeSpec(MapperKind.MBC3),
    0x12: CartridgeTypeSpec(MapperKind.MBC3, ram=True),
    0x13: CartridgeTypeSpec(MapperKind.MBC3, ram=True, battery=True),
    0x19: CartridgeTypeSpec(MapperKind.MBC5),
    0x1A: CartridgeTypeSpec(MapperKind.MBC5, ram=True),
    0x1B: CartridgeTypeSpec(MapperKind.MBC5, ram=True, battery=True),
    0x1C: CartridgeTypeSpec(MapperKind.MBC5, rumble=True),
    0x1D: CartridgeTypeSpec(MapperKind.MBC5, ram=True, rumble=True),
    0x1E: CartridgeTypeSpec(MapperKind.MBC5, ram=True, battery=True, rumble=True),
    0x20: UNSUPPORTED_CARTRIDGE_TYPE,
    0x22: CartridgeTypeSpec(MapperKind.UNSUPPORTED, ram=True, battery=True, rumble=True),
    0xFC: UNSUPPORTED_CARTRIDGE_TYPE,
    0xFD: UNSUPPORTED_CARTRIDGE_TYPE,
    0xFE: UNSUPPORTED_CARTRIDGE_TYPE,
    0xFF: CartridgeTypeSpec(MapperKind.HUC1, ram=True, battery=True, ir=True),
}


@dataclass(frozen=True)
class CartridgeHeader:
    title: str
    cartridge_type_code: int
    rom_size_code: int
    ram_size_code: int
    header_checksum: int
    computed_header_checksum: int

    @property
    def cartridge_type(self) -> str:
        return CARTRIDGE_TYPES.get(
            self.cartridge_type_code, f"Unknown (${self.cartridge_type_code:02X})"
        )

    @property
    def rom_size(self) -> str:
        return ROM_SIZES.get(self.rom_size_code, f"Unknown (${self.rom_size_code:02X})")

    @property
    def ram_size(self) -> str:
        return RAM_SIZES.get(self.ram_size_code, f"Unknown (${self.ram_size_code:02X})")

    @property
    def header_checksum_ok(self) -> bool:
        return self.header_checksum == self.computed_header_checksum

    def summary(self) -> str:
        status = "OK" if self.header_checksum_ok else "BAD"
        return "\n".join(
            [
                f"Title: {self.title or '<empty>'}",
                f"Cartridge type: {self.cartridge_type} (${self.cartridge_type_code:02X})",
                f"ROM size: {self.rom_size} (${self.rom_size_code:02X})",
                f"RAM size: {self.ram_size} (${self.ram_size_code:02X})",
                (
                    "Header checksum: "
                    f"{status} (stored ${self.header_checksum:02X}, "
                    f"computed ${self.computed_header_checksum:02X})"
                ),
            ]
        )


class CartridgeMapper:
    ram_gate_required = False

    def __init__(self, cartridge: Cartridge) -> None:
        self.cartridge = cartridge

    @property
    def name(self) -> str:
        return self.cartridge.type_spec.mapper.value

    def read_rom(self, address: int) -> int:
        cart = self.cartridge
        address &= 0xFFFF
        if address < 0x4000:
            offset = self.lower_rom_bank() * 0x4000 + address
        elif address < 0x8000:
            offset = self.selected_rom_bank() * 0x4000 + (address - 0x4000)
        else:
            return 0xFF
        if offset < len(cart.data):
            return cart.data[offset]
        return 0xFF

    def lower_rom_bank(self) -> int:
        return 0

    def selected_rom_bank(self) -> int:
        return min(1, self.cartridge.rom_bank_count - 1)

    def write_rom_control(self, address: int, value: int) -> None:
        pass

    def read_ram(self, address: int) -> int:
        if not self.ram_access_enabled():
            return 0xFF
        if not self.cartridge.ram:
            return 0xFF
        return self.cartridge.ram[self.selected_ram_offset(address)]

    def write_ram(self, address: int, value: int) -> None:
        if not self.ram_access_enabled() or not self.cartridge.ram:
            return
        self.cartridge.ram[self.selected_ram_offset(address)] = value & 0xFF

    def ram_access_enabled(self) -> bool:
        return not self.ram_gate_required or self.cartridge.ram_enabled

    def selected_ram_offset(self, address: int) -> int:
        base_offset = (address - 0xA000) & 0x1FFF
        return base_offset % len(self.cartridge.ram)


class ROMMapper(CartridgeMapper):
    pass


class UnsupportedMapper(CartridgeMapper):
    pass


class MBC1Mapper(CartridgeMapper):
    ram_gate_required = True

    def lower_rom_bank(self) -> int:
        cart = self.cartridge
        if cart.banking_mode == 1:
            return self._upper_rom_bank_bits() % cart.rom_bank_count
        return 0

    def selected_rom_bank(self) -> int:
        cart = self.cartridge
        low_mask = 0x0F if cart.has_mbc1m else 0x1F
        bank = (cart.rom_bank_low5 & low_mask) | self._upper_rom_bank_bits()
        if cart.rom_bank_low5 == 0:
            bank += 1
        return bank % cart.rom_bank_count

    def write_rom_control(self, address: int, value: int) -> None:
        cart = self.cartridge
        address &= 0x7FFF
        value &= 0xFF
        if address <= 0x1FFF:
            cart.ram_enabled = (value & 0x0F) == 0x0A
        elif address <= 0x3FFF:
            cart.rom_bank_low5 = value & 0x1F
        elif address <= 0x5FFF:
            cart.bank_high2 = value & 0x03
        else:
            cart.banking_mode = value & 0x01

    def selected_ram_offset(self, address: int) -> int:
        cart = self.cartridge
        base_offset = (address - 0xA000) & 0x1FFF
        bank = 0
        if cart.banking_mode == 1 and self._uses_ram_banking():
            bank = cart.bank_high2 % max(1, cart.ram_bank_count)
        return (bank * 0x2000 + base_offset) % len(cart.ram)

    def _upper_rom_bank_bits(self) -> int:
        cart = self.cartridge
        return cart.bank_high2 << (4 if cart.has_mbc1m else 5)

    def _uses_ram_banking(self) -> bool:
        cart = self.cartridge
        return cart.ram_bank_count > 1 and cart.rom_bank_count <= 32 and not cart.has_mbc1m


class MBC2Mapper(CartridgeMapper):
    ram_gate_required = True

    def selected_rom_bank(self) -> int:
        return self.cartridge.mbc2_rom_bank % self.cartridge.rom_bank_count

    def write_rom_control(self, address: int, value: int) -> None:
        cart = self.cartridge
        address &= 0x7FFF
        value &= 0xFF
        if address > 0x3FFF:
            return
        if address & 0x0100:
            cart.mbc2_rom_bank = value & 0x0F or 1
        else:
            cart.ram_enabled = (value & 0x0F) == 0x0A

    def read_ram(self, address: int) -> int:
        if not self.ram_access_enabled():
            return 0xFF
        return 0xF0 | self.cartridge.ram[(address - 0xA000) & 0x01FF]

    def write_ram(self, address: int, value: int) -> None:
        if not self.ram_access_enabled():
            return
        self.cartridge.ram[(address - 0xA000) & 0x01FF] = value & 0x0F


class MBC3Mapper(CartridgeMapper):
    ram_gate_required = True

    def read_rom(self, address: int) -> int:
        cart = self.cartridge
        data = cart.data
        if address < 0x4000:
            return data[address] if address < len(data) else 0xFF
        if address < 0x8000:
            offset = cart._mbc3_rom_bank_offset + (address - 0x4000)
            return data[offset]
        return 0xFF

    def selected_rom_bank(self) -> int:
        return self.cartridge.mbc3_rom_bank % self.cartridge.rom_bank_count

    def write_rom_control(self, address: int, value: int) -> None:
        cart = self.cartridge
        address &= 0x7FFF
        value &= 0xFF
        if address <= 0x1FFF:
            cart.ram_enabled = (value & 0x0F) == 0x0A
        elif address <= 0x3FFF:
            cart.mbc3_rom_bank = value & 0x7F or 1
            cart._mbc3_rom_bank_offset = (
                cart.mbc3_rom_bank % cart.rom_bank_count
            ) * 0x4000
        elif address <= 0x5FFF:
            cart.mbc3_ram_select = value & 0x0F
        else:
            if cart.mbc3_rtc_latch_previous == 0 and value == 1:
                cart._latch_rtc()
            cart.mbc3_rtc_latch_previous = value

    def read_ram(self, address: int) -> int:
        cart = self.cartridge
        if not self.ram_access_enabled():
            return 0xFF
        if 0x08 <= cart.mbc3_ram_select <= 0x0C:
            if cart.has_mbc3_rtc:
                return cart._read_rtc_register(cart.mbc3_ram_select)
            return 0xFF
        if cart.mbc3_ram_select > 0x07:
            return 0xFF
        return super().read_ram(address)

    def write_ram(self, address: int, value: int) -> None:
        cart = self.cartridge
        if not self.ram_access_enabled():
            return
        if 0x08 <= cart.mbc3_ram_select <= 0x0C:
            if cart.has_mbc3_rtc:
                cart._write_rtc_register(cart.mbc3_ram_select, value)
            return
        if cart.mbc3_ram_select > 0x07:
            return
        super().write_ram(address, value)

    def selected_ram_offset(self, address: int) -> int:
        cart = self.cartridge
        base_offset = (address - 0xA000) & 0x1FFF
        bank = cart.mbc3_ram_select % max(1, cart.ram_bank_count)
        return (bank * 0x2000 + base_offset) % len(cart.ram)


class MBC5Mapper(CartridgeMapper):
    ram_gate_required = True

    def selected_rom_bank(self) -> int:
        return self.cartridge.mbc5_rom_bank % self.cartridge.rom_bank_count

    def write_rom_control(self, address: int, value: int) -> None:
        cart = self.cartridge
        address &= 0x7FFF
        value &= 0xFF
        if address <= 0x1FFF:
            cart.ram_enabled = (value & 0x0F) == 0x0A
        elif address <= 0x2FFF:
            cart.mbc5_rom_bank = (cart.mbc5_rom_bank & 0x100) | value
        elif address <= 0x3FFF:
            cart.mbc5_rom_bank = (cart.mbc5_rom_bank & 0xFF) | ((value & 0x01) << 8)
        elif address <= 0x5FFF:
            if cart.has_mbc5_rumble:
                cart.mbc5_ram_bank = value & 0x07
                cart.rumble_active = bool(value & 0x08)
            else:
                cart.mbc5_ram_bank = value & 0x0F

    def selected_ram_offset(self, address: int) -> int:
        cart = self.cartridge
        base_offset = (address - 0xA000) & 0x1FFF
        bank = cart.mbc5_ram_bank % max(1, cart.ram_bank_count)
        return (bank * 0x2000 + base_offset) % len(cart.ram)


class HuC1Mapper(CartridgeMapper):
    def selected_rom_bank(self) -> int:
        return self.cartridge.huc1_rom_bank % self.cartridge.rom_bank_count

    def write_rom_control(self, address: int, value: int) -> None:
        cart = self.cartridge
        address &= 0x7FFF
        value &= 0xFF
        if address <= 0x1FFF:
            cart.huc1_ir_mode = value == 0x0E
        elif address <= 0x3FFF:
            cart.huc1_rom_bank = value & 0x3F or 1
        elif address <= 0x5FFF:
            cart.huc1_ram_bank = value & 0x03

    def read_ram(self, address: int) -> int:
        cart = self.cartridge
        if cart.huc1_ir_mode:
            return 0xC1 if cart.huc1_ir_input else 0xC0
        return super().read_ram(address)

    def write_ram(self, address: int, value: int) -> None:
        cart = self.cartridge
        if cart.huc1_ir_mode:
            cart.huc1_ir_transmitter_enabled = bool(value & 0x01)
            return
        super().write_ram(address, value)

    def selected_ram_offset(self, address: int) -> int:
        cart = self.cartridge
        base_offset = (address - 0xA000) & 0x1FFF
        bank = cart.huc1_ram_bank % max(1, cart.ram_bank_count)
        return (bank * 0x2000 + base_offset) % len(cart.ram)


MAPPER_CLASSES = {
    MapperKind.ROM: ROMMapper,
    MapperKind.MBC1: MBC1Mapper,
    MapperKind.MBC2: MBC2Mapper,
    MapperKind.MBC3: MBC3Mapper,
    MapperKind.MBC5: MBC5Mapper,
    MapperKind.HUC1: HuC1Mapper,
    MapperKind.UNSUPPORTED: UnsupportedMapper,
}


def create_cartridge_mapper(cartridge: Cartridge) -> CartridgeMapper:
    mapper_class = MAPPER_CLASSES.get(cartridge.type_spec.mapper, UnsupportedMapper)
    return mapper_class(cartridge)


class Cartridge:
    def __init__(
        self,
        data: bytes,
        path: Path | None = None,
        *,
        rtc_time_provider: Callable[[], float] | None = None,
    ) -> None:
        if len(data) < 0x150:
            raise ValueError("ROM is too small to contain a Game Boy cartridge header")
        self.data = bytes(data)
        self._rom_bank_count = max(1, len(self.data) // 0x4000)
        self.path = path
        self.header = self._parse_header()
        self.type_spec = CARTRIDGE_TYPE_SPECS.get(
            self.header.cartridge_type_code, UNSUPPORTED_CARTRIDGE_TYPE
        )
        self.mbc1m = self._detect_mbc1m()
        self.rom_bank_low5 = 0
        self.bank_high2 = 0
        self.banking_mode = 0
        self.mbc2_rom_bank = 1
        self.mbc3_rom_bank = 1
        self._mbc3_rom_bank_offset = (self.mbc3_rom_bank % self._rom_bank_count) * 0x4000
        self.mbc3_ram_select = 0
        self.mbc3_rtc_latch_previous = 0
        self._rtc_time_provider = rtc_time_provider or time.time
        self._rtc_last_timestamp = self._rtc_time_provider()
        self._rtc_seconds = 0
        self._rtc_minutes = 0
        self._rtc_hours = 0
        self._rtc_days = 0
        self._rtc_halt = False
        self._rtc_carry = False
        self.rtc_registers = bytearray(5)
        self.mbc5_rom_bank = 1
        self.mbc5_ram_bank = 0
        self.rumble_active = False
        self.huc1_rom_bank = 1
        self.huc1_ram_bank = 0
        self.huc1_ir_mode = False
        self.huc1_ir_input = False
        self.huc1_ir_transmitter_enabled = False
        self.ram_enabled = False
        self.ram = bytearray(MBC2_RAM_SIZE if self.has_mbc2 else self.ram_size_bytes)
        self._ram_bank_count = 0 if not self.ram else max(1, len(self.ram) // 0x2000)
        self.mapper = create_cartridge_mapper(self)

    @property
    def rom_bank_count(self) -> int:
        return self._rom_bank_count

    @property
    def ram_size_bytes(self) -> int:
        return RAM_SIZE_BYTES.get(self.header.ram_size_code, 0)

    @property
    def ram_bank_count(self) -> int:
        return self._ram_bank_count

    @property
    def mapper_name(self) -> str:
        return self.mapper.name

    @property
    def is_supported_mapper(self) -> bool:
        return self.type_spec.mapper != MapperKind.UNSUPPORTED

    @property
    def mapper_status(self) -> str:
        if self.is_supported_mapper:
            return f"Mapper: {self.mapper_name}"
        return f"Mapper: unsupported ({self.header.cartridge_type})"

    @property
    def has_mbc1(self) -> bool:
        return self.type_spec.mapper == MapperKind.MBC1

    @property
    def has_mbc1m(self) -> bool:
        return self.mbc1m

    @property
    def has_mbc2(self) -> bool:
        return self.type_spec.mapper == MapperKind.MBC2

    @property
    def has_mbc3(self) -> bool:
        return self.type_spec.mapper == MapperKind.MBC3

    @property
    def has_mbc3_rtc(self) -> bool:
        return self.has_mbc3 and self.type_spec.timer

    @property
    def has_mbc5(self) -> bool:
        return self.type_spec.mapper == MapperKind.MBC5

    @property
    def has_mbc5_rumble(self) -> bool:
        return self.has_mbc5 and self.type_spec.rumble

    @property
    def has_huc1(self) -> bool:
        return self.type_spec.mapper == MapperKind.HUC1

    @property
    def has_battery(self) -> bool:
        return self.type_spec.battery

    @property
    def has_persistent_data(self) -> bool:
        return bool(self.ram) or self.type_spec.timer

    @property
    def handles_external_ram(self) -> bool:
        return bool(self.ram) or self.type_spec.ram or self.type_spec.timer

    @classmethod
    def from_file(cls, path: str | Path) -> "Cartridge":
        rom_path = Path(path)
        return cls(rom_path.read_bytes(), rom_path)

    def read_rom(self, address: int) -> int:
        return self.mapper.read_rom(address)

    def write_rom_control(self, address: int, value: int) -> None:
        self.mapper.write_rom_control(address, value)

    def read_ram(self, address: int) -> int:
        return self.mapper.read_ram(address)

    def write_ram(self, address: int, value: int) -> None:
        self.mapper.write_ram(address, value)

    def dump_ram(self) -> bytes:
        return bytes(self.ram)

    def load_ram(self, data: bytes) -> None:
        if not self.ram:
            return
        self.ram[: min(len(data), len(self.ram))] = data[: len(self.ram)]

    def clone_for_reset(self, *, preserve_ram: bool = True) -> "Cartridge":
        clone = Cartridge(self.data, self.path, rtc_time_provider=self._rtc_time_provider)
        if not preserve_ram:
            return clone
        clone.load_ram(self.dump_ram())
        if self.has_mbc3_rtc:
            clone._load_rtc_state(self._dump_rtc_state())
        return clone

    def save_ram_file(self, path: str | Path) -> None:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(self.dump_ram())
        if self.has_mbc3_rtc:
            self._save_rtc_file(self._rtc_sidecar_path(output_path))

    def load_ram_file(self, path: str | Path) -> None:
        ram_path = Path(path)
        if ram_path.exists():
            self.load_ram(ram_path.read_bytes())
        if self.has_mbc3_rtc:
            self._load_rtc_file(self._rtc_sidecar_path(ram_path))

    def _selected_rom_bank(self) -> int:
        return self.mapper.selected_rom_bank()

    def _selected_ram_offset(self, address: int) -> int:
        return self.mapper.selected_ram_offset(address)

    def _detect_mbc1m(self) -> bool:
        if not self.has_mbc1:
            return False
        if self.rom_bank_count != 64:
            return False
        logo_offset = 0x10 * 0x4000 + 0x0104
        return self.data[logo_offset : logo_offset + len(NINTENDO_LOGO)] == NINTENDO_LOGO

    def _read_rtc_register(self, register: int) -> int:
        return self.rtc_registers[register - 0x08]

    def _write_rtc_register(self, register: int, value: int) -> None:
        self._update_rtc()
        value &= 0xFF
        if register == 0x08:
            self._rtc_seconds = min(value, 59)
        elif register == 0x09:
            self._rtc_minutes = min(value, 59)
        elif register == 0x0A:
            self._rtc_hours = min(value, 23)
        elif register == 0x0B:
            self._rtc_days = (self._rtc_days & 0x100) | value
        elif register == 0x0C:
            was_halted = self._rtc_halt
            self._rtc_days = (self._rtc_days & 0x0FF) | ((value & 0x01) << 8)
            self._rtc_halt = bool(value & 0x40)
            self._rtc_carry = bool(value & 0x80)
            if self._rtc_halt != was_halted:
                self._rtc_last_timestamp = self._rtc_time_provider()
        else:
            raise AssertionError(register)
        self._latch_rtc(update=False)

    def _latch_rtc(self, update: bool = True) -> None:
        if update:
            self._update_rtc()
        self.rtc_registers[:] = self._current_rtc_registers()

    def _current_rtc_registers(self) -> bytes:
        day_high = (self._rtc_days >> 8) & 0x01
        if self._rtc_halt:
            day_high |= 0x40
        if self._rtc_carry:
            day_high |= 0x80
        return bytes(
            [
                self._rtc_seconds,
                self._rtc_minutes,
                self._rtc_hours,
                self._rtc_days & 0xFF,
                day_high,
            ]
        )

    def _update_rtc(self) -> None:
        now = self._rtc_time_provider()
        if self._rtc_halt:
            self._rtc_last_timestamp = now
            return

        elapsed = int(now - self._rtc_last_timestamp)
        if elapsed <= 0:
            return
        self._rtc_last_timestamp += elapsed
        self._advance_rtc(elapsed)

    def _advance_rtc(self, elapsed_seconds: int) -> None:
        total_seconds = (
            self._rtc_seconds
            + self._rtc_minutes * 60
            + self._rtc_hours * 3600
            + self._rtc_days * 86400
            + elapsed_seconds
        )
        days, remainder = divmod(total_seconds, 86400)
        self._rtc_hours, remainder = divmod(remainder, 3600)
        self._rtc_minutes, self._rtc_seconds = divmod(remainder, 60)
        if days > 0x1FF:
            self._rtc_carry = True
        self._rtc_days = days & 0x1FF

    @staticmethod
    def _rtc_sidecar_path(path: Path) -> Path:
        return Path(f"{path}.rtc")

    def _save_rtc_file(self, path: Path) -> None:
        path.write_text(json.dumps(self._dump_rtc_state(), sort_keys=True), encoding="utf-8")

    def _load_rtc_file(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            self._load_rtc_state(state)
        except (OSError, TypeError, ValueError, KeyError):
            return

    def _dump_rtc_state(self) -> dict[str, int | float | bool]:
        self._update_rtc()
        return {
            "version": RTC_SAVE_VERSION,
            "saved_at": self._rtc_time_provider(),
            "seconds": self._rtc_seconds,
            "minutes": self._rtc_minutes,
            "hours": self._rtc_hours,
            "days": self._rtc_days,
            "halt": self._rtc_halt,
            "carry": self._rtc_carry,
        }

    def _load_rtc_state(self, state: object) -> None:
        if not isinstance(state, dict):
            raise ValueError("RTC save sidecar must contain a JSON object")

        self._rtc_seconds = min(max(int(state["seconds"]), 0), 59)
        self._rtc_minutes = min(max(int(state["minutes"]), 0), 59)
        self._rtc_hours = min(max(int(state["hours"]), 0), 23)
        self._rtc_days = min(max(int(state["days"]), 0), 0x1FF)
        self._rtc_halt = bool(state["halt"])
        self._rtc_carry = bool(state["carry"])

        saved_at = float(state.get("saved_at", self._rtc_time_provider()))
        now = self._rtc_time_provider()
        if not self._rtc_halt:
            elapsed = int(now - saved_at)
            if elapsed > 0:
                self._advance_rtc(elapsed)
        self._rtc_last_timestamp = now
        self._latch_rtc(update=False)

    def _parse_header(self) -> CartridgeHeader:
        title_bytes = self.data[0x0134:0x0144]
        title = title_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        return CartridgeHeader(
            title=title,
            cartridge_type_code=self.data[0x0147],
            rom_size_code=self.data[0x0148],
            ram_size_code=self.data[0x0149],
            header_checksum=self.data[0x014D],
            computed_header_checksum=compute_header_checksum(self.data),
        )

def compute_header_checksum(rom: bytes) -> int:
    checksum = 0
    for address in range(0x0134, 0x014D):
        checksum = (checksum - rom[address] - 1) & 0xFF
    return checksum
