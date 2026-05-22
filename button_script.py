from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from joypad import Joypad


@dataclass(frozen=True)
class ButtonScriptEvent:
    frame: int
    buttons: frozenset[str]
    duration: int = 1

    def __post_init__(self) -> None:
        if self.frame < 0:
            raise ValueError("button script frame must be non-negative")
        if self.duration < 1:
            raise ValueError("button script duration must be positive")

    @property
    def end_frame(self) -> int:
        return self.frame + self.duration


class ButtonScript:
    def __init__(self, events: list[ButtonScriptEvent]) -> None:
        self.events = tuple(sorted(events, key=lambda event: event.frame))

    def buttons_for_frame(
        self,
        frame: int,
        fallback: set[str] | frozenset[str] | None = None,
    ) -> set[str]:
        if frame < 0:
            raise ValueError("button script frame must be non-negative")
        active: set[str] = set()
        matched = False
        for event in self.events:
            if event.frame > frame:
                break
            if frame < event.end_frame:
                active.update(event.buttons)
                matched = True
        if matched:
            return active
        return set(fallback or set())

    @property
    def final_frame(self) -> int:
        if not self.events:
            return 0
        return max(event.end_frame for event in self.events)

    def __bool__(self) -> bool:
        return bool(self.events)


def parse_button_script(script: str) -> ButtonScript:
    events: list[ButtonScriptEvent] = []
    for token in _script_tokens(script):
        parts = [part.strip() for part in token.split(":")]
        if len(parts) not in {2, 3}:
            raise ValueError(
                "button script entries must use frame:buttons[:duration]"
            )
        frame = _parse_non_negative_int(parts[0], "button script frame")
        buttons = _parse_button_group(parts[1])
        duration = (
            _parse_positive_int(parts[2], "button script duration")
            if len(parts) == 3
            else 1
        )
        events.append(ButtonScriptEvent(frame, frozenset(buttons), duration))
    return ButtonScript(events)


def load_button_script(path: str | Path) -> ButtonScript:
    return parse_button_script(Path(path).read_text(encoding="utf-8"))


def _script_tokens(script: str) -> list[str]:
    tokens: list[str] = []
    for line in script.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tokens.extend(token.strip() for token in line.split(",") if token.strip())
    return tokens


def _parse_button_group(raw_buttons: str) -> set[str]:
    value = raw_buttons.strip().lower()
    if value in {"", "-", "none", "release"}:
        return set()
    return Joypad.normalize_buttons(value.replace("+", ",").replace("|", ","))


def _parse_non_negative_int(raw_value: str, label: str) -> int:
    try:
        value = int(raw_value, 0)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{label} must be non-negative")
    return value


def _parse_positive_int(raw_value: str, label: str) -> int:
    value = _parse_non_negative_int(raw_value, label)
    if value < 1:
        raise ValueError(f"{label} must be positive")
    return value
