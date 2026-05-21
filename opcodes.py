from __future__ import annotations

from typing import Protocol


REG8 = ("B", "C", "D", "E", "H", "L", "[HL]", "A")
R16 = ("BC", "DE", "HL", "SP")
R16_STACK = ("BC", "DE", "HL", "AF")
COND = ("NZ", "Z", "NC", "C")
ALU = ("ADD A,", "ADC A,", "SUB A,", "SBC A,", "AND A,", "XOR A,", "OR A,", "CP A,")
INVALID_OPCODES = {0xD3, 0xDB, 0xDD, 0xE3, 0xE4, 0xEB, 0xEC, 0xED, 0xF4, 0xFC, 0xFD}


class ReadableBus(Protocol):
    def read8(self, address: int) -> int:
        ...


def instruction_length(opcode: int) -> int:
    opcode &= 0xFF
    if opcode == 0xCB:
        return 2
    if opcode in {
        0x01,
        0x08,
        0x11,
        0x21,
        0x31,
        0xC2,
        0xC3,
        0xC4,
        0xCA,
        0xCC,
        0xCD,
        0xD2,
        0xD4,
        0xDA,
        0xDC,
        0xEA,
        0xFA,
    }:
        return 3
    if (
        opcode in {0x10, 0x18, 0x20, 0x28, 0x30, 0x38, 0xC6, 0xCE, 0xD6, 0xDE, 0xE0, 0xE6, 0xE8, 0xEE, 0xF0, 0xF6, 0xF8, 0xFE}
        or opcode & 0xC7 == 0x06
    ):
        return 2
    return 1


def disassemble(bus: ReadableBus, pc: int) -> tuple[list[int], str]:
    opcode = bus.read8(pc)
    length = instruction_length(opcode)
    raw = [bus.read8((pc + offset) & 0xFFFF) for offset in range(length)]
    if opcode == 0xCB:
        return raw, _disassemble_cb(raw[1])
    return raw, _disassemble_main(raw)


def _u16(raw: list[int]) -> int:
    return raw[1] | (raw[2] << 8)


def _e8(raw: list[int]) -> int:
    value = raw[1]
    return value - 0x100 if value & 0x80 else value


def _disassemble_main(raw: list[int]) -> str:
    opcode = raw[0]
    if opcode in INVALID_OPCODES:
        return f"ILLEGAL ${opcode:02X}"

    if opcode == 0x00:
        return "NOP"
    if opcode == 0x08:
        return f"LD [${_u16(raw):04X}], SP"
    if opcode == 0x10:
        return f"STOP ${raw[1]:02X}"
    if opcode == 0x18:
        return f"JR {_e8(raw):+d}"
    if opcode == 0x27:
        return "DAA"
    if opcode == 0x2F:
        return "CPL"
    if opcode == 0x37:
        return "SCF"
    if opcode == 0x3F:
        return "CCF"
    if opcode == 0x76:
        return "HALT"
    if opcode == 0xC3:
        return f"JP ${_u16(raw):04X}"
    if opcode == 0xC9:
        return "RET"
    if opcode == 0xCD:
        return f"CALL ${_u16(raw):04X}"
    if opcode == 0xD9:
        return "RETI"
    if opcode == 0xE0:
        return f"LDH [$FF00+${raw[1]:02X}], A"
    if opcode == 0xE2:
        return "LDH [$FF00+C], A"
    if opcode == 0xE8:
        return f"ADD SP, {_e8(raw):+d}"
    if opcode == 0xE9:
        return "JP HL"
    if opcode == 0xEA:
        return f"LD [${_u16(raw):04X}], A"
    if opcode == 0xF0:
        return f"LDH A, [$FF00+${raw[1]:02X}]"
    if opcode == 0xF2:
        return "LDH A, [$FF00+C]"
    if opcode == 0xF3:
        return "DI"
    if opcode == 0xF8:
        return f"LD HL, SP{_e8(raw):+d}"
    if opcode == 0xF9:
        return "LD SP, HL"
    if opcode == 0xFA:
        return f"LD A, [${_u16(raw):04X}]"
    if opcode == 0xFB:
        return "EI"

    if opcode in {0x07, 0x0F, 0x17, 0x1F}:
        return {0x07: "RLCA", 0x0F: "RRCA", 0x17: "RLA", 0x1F: "RRA"}[opcode]
    if opcode in {0x20, 0x28, 0x30, 0x38}:
        return f"JR {COND[(opcode - 0x20) // 8]}, {_e8(raw):+d}"
    if opcode & 0xCF == 0x01:
        return f"LD {R16[(opcode >> 4) & 0x03]}, ${_u16(raw):04X}"
    if opcode in {0x02, 0x12, 0x22, 0x32}:
        return {0x02: "LD [BC], A", 0x12: "LD [DE], A", 0x22: "LD [HL+], A", 0x32: "LD [HL-], A"}[opcode]
    if opcode in {0x0A, 0x1A, 0x2A, 0x3A}:
        return {0x0A: "LD A, [BC]", 0x1A: "LD A, [DE]", 0x2A: "LD A, [HL+]", 0x3A: "LD A, [HL-]"}[opcode]
    if opcode & 0xCF == 0x03:
        return f"INC {R16[(opcode >> 4) & 0x03]}"
    if opcode & 0xCF == 0x09:
        return f"ADD HL, {R16[(opcode >> 4) & 0x03]}"
    if opcode & 0xCF == 0x0B:
        return f"DEC {R16[(opcode >> 4) & 0x03]}"
    if opcode & 0xC7 == 0x04:
        return f"INC {REG8[(opcode >> 3) & 0x07]}"
    if opcode & 0xC7 == 0x05:
        return f"DEC {REG8[(opcode >> 3) & 0x07]}"
    if opcode & 0xC7 == 0x06:
        return f"LD {REG8[(opcode >> 3) & 0x07]}, ${raw[1]:02X}"
    if 0x40 <= opcode <= 0x7F:
        return f"LD {REG8[(opcode >> 3) & 0x07]}, {REG8[opcode & 0x07]}"
    if 0x80 <= opcode <= 0xBF:
        operation = ALU[(opcode >> 3) & 0x07]
        return f"{operation} {REG8[opcode & 0x07]}"
    if opcode in {0xC0, 0xC8, 0xD0, 0xD8}:
        return f"RET {COND[(opcode - 0xC0) // 8]}"
    if opcode in {0xC2, 0xCA, 0xD2, 0xDA}:
        return f"JP {COND[(opcode - 0xC2) // 8]}, ${_u16(raw):04X}"
    if opcode in {0xC4, 0xCC, 0xD4, 0xDC}:
        return f"CALL {COND[(opcode - 0xC4) // 8]}, ${_u16(raw):04X}"
    if opcode & 0xC7 == 0xC1:
        return f"POP {R16_STACK[(opcode >> 4) & 0x03]}"
    if opcode & 0xC7 == 0xC5:
        return f"PUSH {R16_STACK[(opcode >> 4) & 0x03]}"
    if opcode & 0xC7 == 0xC7:
        return f"RST ${opcode & 0x38:02X}"
    if opcode in {0xC6, 0xCE, 0xD6, 0xDE, 0xE6, 0xEE, 0xF6, 0xFE}:
        operation = {
            0xC6: "ADD A,",
            0xCE: "ADC A,",
            0xD6: "SUB A,",
            0xDE: "SBC A,",
            0xE6: "AND A,",
            0xEE: "XOR A,",
            0xF6: "OR A,",
            0xFE: "CP A,",
        }[opcode]
        return f"{operation} ${raw[1]:02X}"
    return f"DB ${opcode:02X}"


def _disassemble_cb(opcode: int) -> str:
    reg = REG8[opcode & 0x07]
    if opcode < 0x40:
        operation = ("RLC", "RRC", "RL", "RR", "SLA", "SRA", "SWAP", "SRL")[(opcode >> 3) & 0x07]
        return f"{operation} {reg}"
    bit = (opcode >> 3) & 0x07
    if opcode < 0x80:
        return f"BIT {bit}, {reg}"
    if opcode < 0xC0:
        return f"RES {bit}, {reg}"
    return f"SET {bit}, {reg}"
