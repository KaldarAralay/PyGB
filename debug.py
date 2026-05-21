from __future__ import annotations

from pathlib import Path


class TraceLogger:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lines: list[str] = []

    def write(self, line: str) -> None:
        if self.path is None:
            print(line)
        else:
            self._lines.append(line)

    def close(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(self._lines) + ("\n" if self._lines else ""), encoding="utf-8")

    def __enter__(self) -> "TraceLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
