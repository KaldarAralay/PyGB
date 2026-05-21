from __future__ import annotations

import struct
import wave
from pathlib import Path
from typing import BinaryIO, Iterable


AudioSample = tuple[int, int]
PCM_SCALE = 64


def _pcm16(value: int) -> int:
    return max(-32768, min(32767, int(value) * PCM_SCALE))


class WavAudioWriter:
    def __init__(self, output: str | Path | BinaryIO, *, sample_rate: int) -> None:
        if sample_rate <= 0:
            raise ValueError("WAV sample rate must be positive")
        self._owned_file: BinaryIO | None = None
        if isinstance(output, str | Path):
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self._owned_file = output_path.open("wb")
            wave_target: str | BinaryIO = self._owned_file
        else:
            wave_target = output
        self._wav = wave.open(wave_target, "wb")
        self._wav.setnchannels(2)
        self._wav.setsampwidth(2)
        self._wav.setframerate(sample_rate)

    def write(self, samples: Iterable[AudioSample]) -> None:
        payload = bytearray()
        for left, right in samples:
            payload.extend(struct.pack("<hh", _pcm16(left), _pcm16(right)))
        if payload:
            self._wav.writeframesraw(payload)

    def close(self) -> None:
        self._wav.close()
        if self._owned_file is not None:
            self._owned_file.close()
            self._owned_file = None

    def __enter__(self) -> "WavAudioWriter":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def write_wav_samples(
    output: str | Path | BinaryIO,
    samples: Iterable[AudioSample],
    *,
    sample_rate: int,
) -> None:
    with WavAudioWriter(output, sample_rate=sample_rate) as writer:
        writer.write(samples)
