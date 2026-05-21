from __future__ import annotations

from typing import Protocol


BUTTON_BITS = {
    "a": ("action", 0),
    "b": ("action", 1),
    "select": ("action", 2),
    "start": ("action", 3),
    "right": ("direction", 0),
    "left": ("direction", 1),
    "up": ("direction", 2),
    "down": ("direction", 3),
}


class JoypadBus(Protocol):
    io: bytearray

    @property
    def interrupt_flags(self) -> int:
        ...

    @interrupt_flags.setter
    def interrupt_flags(self, value: int) -> None:
        ...


class Joypad:
    def __init__(self, bus: JoypadBus) -> None:
        self.bus = bus
        self.action_state = 0x0F
        self.direction_state = 0x0F

    def read(self) -> int:
        select = self.bus.io[0x00] & 0x30
        low = 0x0F
        if not select & 0x10:
            low &= self.direction_state
        if not select & 0x20:
            low &= self.action_state
        return 0xC0 | select | low

    def write_select(self, value: int) -> None:
        old = self.read()
        self.bus.io[0x00] = 0xC0 | (value & 0x30) | 0x0F
        self._request_interrupt_on_new_low(old, self.read())

    def press(self, button: str) -> None:
        group, bit = self._decode_button(button)
        old = self.read()
        if group == "action":
            self.action_state &= ~(1 << bit)
        else:
            self.direction_state &= ~(1 << bit)
        self._request_interrupt_on_new_low(old, self.read())

    def release(self, button: str) -> None:
        group, bit = self._decode_button(button)
        if group == "action":
            self.action_state |= 1 << bit
        else:
            self.direction_state |= 1 << bit

    def stop_wake_requested(self) -> bool:
        return (self.read() & 0x0F) != 0x0F

    def set_pressed(self, buttons: set[str]) -> None:
        buttons = self._normalize_button_set(buttons)
        for button in BUTTON_BITS:
            if button in buttons:
                self.press(button)
            else:
                self.release(button)

    @staticmethod
    def normalize_buttons(raw_buttons: str) -> set[str]:
        if not raw_buttons.strip():
            return set()
        buttons = {part.strip().lower() for part in raw_buttons.split(",") if part.strip()}
        return Joypad._normalize_button_set(buttons)

    @staticmethod
    def _normalize_button_set(buttons: set[str]) -> set[str]:
        buttons = {button.lower() for button in buttons}
        unknown = buttons - set(BUTTON_BITS)
        if unknown:
            names = ", ".join(sorted(unknown))
            valid = ", ".join(sorted(BUTTON_BITS))
            raise ValueError(f"Unknown button(s): {names}. Valid buttons: {valid}")
        return buttons

    @staticmethod
    def _decode_button(button: str) -> tuple[str, int]:
        try:
            return BUTTON_BITS[button.lower()]
        except KeyError as exc:
            valid = ", ".join(sorted(BUTTON_BITS))
            raise ValueError(f"Unknown button {button!r}. Valid buttons: {valid}") from exc

    def _request_interrupt_on_new_low(self, old: int, new: int) -> None:
        if (old & 0x0F) & ~(new & 0x0F):
            self.bus.interrupt_flags = self.bus.interrupt_flags | 0x10
