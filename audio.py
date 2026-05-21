from __future__ import annotations

import ctypes
import sys
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Protocol


AudioSample = tuple[int, int]
PCM_SCALE = 64
WAVE_MAPPER = 0xFFFFFFFF
WAVE_FORMAT_PCM = 1
WHDR_DONE = 0x00000001


def _pcm16(value: int) -> int:
    return max(-32768, min(32767, int(value) * PCM_SCALE))


def samples_to_pcm16_bytes(samples: Iterable[AudioSample]) -> bytes:
    payload = bytearray()
    for left, right in samples:
        payload.extend(struct.pack("<hh", _pcm16(left), _pcm16(right)))
    return bytes(payload)


@dataclass(frozen=True)
class AudioPlaybackStats:
    queued_frames: int
    queued_ms: float
    underruns: int
    dropped_frames: int
    submitted_frames: int
    completed_frames: int


class PCMOutputBackend(Protocol):
    sample_rate: int

    def write_pcm(self, payload: bytes, frame_count: int) -> None:
        ...

    def queued_frames(self) -> int:
        ...

    @property
    def submitted_frames(self) -> int:
        ...

    @property
    def completed_frames(self) -> int:
        ...

    def close(self) -> None:
        ...


class BufferedAudioPlayer:
    def __init__(
        self,
        *,
        sample_rate: int,
        chunk_ms: int = 20,
        target_buffer_ms: int = 100,
        max_buffer_ms: int = 250,
        min_buffer_ms: int = 20,
        backend: PCMOutputBackend | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("audio sample rate must be positive")
        if chunk_ms <= 0:
            raise ValueError("audio chunk size must be positive")
        if target_buffer_ms <= 0:
            raise ValueError("audio target buffer must be positive")
        if max_buffer_ms < target_buffer_ms:
            raise ValueError("audio max buffer must be at least the target buffer")
        if min_buffer_ms < 0:
            raise ValueError("audio minimum buffer cannot be negative")
        self.sample_rate = sample_rate
        self._backend = backend or WaveOutPCMBackend(sample_rate=sample_rate)
        self._chunk_frames = max(1, sample_rate * chunk_ms // 1000)
        self._target_buffer_frames = max(1, sample_rate * target_buffer_ms // 1000)
        self._max_buffer_frames = max(self._target_buffer_frames, sample_rate * max_buffer_ms // 1000)
        self._min_buffer_frames = sample_rate * min_buffer_ms // 1000
        self._pending: list[AudioSample] = []
        self._underruns = 0
        self._dropped_frames = 0
        self._primed = False
        self._below_min_buffer = False

    def start(self) -> None:
        if self._primed:
            return
        remaining = self._target_buffer_frames
        silence = [(0, 0)] * self._chunk_frames
        while remaining > 0:
            frames = min(remaining, self._chunk_frames)
            self._backend.write_pcm(samples_to_pcm16_bytes(silence[:frames]), frames)
            remaining -= frames
        self._primed = True

    def write(self, samples: Iterable[AudioSample]) -> None:
        self.start()
        incoming = list(samples)
        if incoming:
            self._pending.extend(incoming)
            self._submit_pending()
        self._update_underrun_state()

    def stats(self) -> AudioPlaybackStats:
        queued = self.queued_frames()
        return AudioPlaybackStats(
            queued_frames=queued,
            queued_ms=queued / self.sample_rate * 1000,
            underruns=self._underruns,
            dropped_frames=self._dropped_frames,
            submitted_frames=self._backend.submitted_frames,
            completed_frames=self._backend.completed_frames,
        )

    def queued_frames(self) -> int:
        return self._backend.queued_frames() + len(self._pending)

    def close(self) -> None:
        self._backend.close()
        self._pending.clear()

    def _submit_pending(self) -> None:
        while len(self._pending) >= self._chunk_frames:
            queued = self._backend.queued_frames()
            if queued >= self._max_buffer_frames:
                overflow = len(self._pending)
                self._pending.clear()
                self._dropped_frames += overflow
                return
            available = self._max_buffer_frames - queued
            if available < self._chunk_frames:
                return
            chunk = self._pending[: self._chunk_frames]
            del self._pending[: self._chunk_frames]
            self._backend.write_pcm(samples_to_pcm16_bytes(chunk), len(chunk))

    def _update_underrun_state(self) -> None:
        if not self._primed:
            return
        below_min = self.queued_frames() < self._min_buffer_frames
        if below_min and not self._below_min_buffer:
            self._underruns += 1
        self._below_min_buffer = below_min


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint),
        ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [
    ("lpData", ctypes.c_void_p),
    ("dwBufferLength", ctypes.c_uint),
    ("dwBytesRecorded", ctypes.c_uint),
    ("dwUser", ctypes.c_void_p),
    ("dwFlags", ctypes.c_uint),
    ("dwLoops", ctypes.c_uint),
    ("lpNext", ctypes.POINTER(WAVEHDR)),
    ("reserved", ctypes.c_void_p),
]


class WaveOutPCMBackend:
    def __init__(self, *, sample_rate: int) -> None:
        if sys.platform != "win32":
            raise RuntimeError("live audio is currently available only on Windows")
        if sample_rate <= 0:
            raise ValueError("audio sample rate must be positive")
        self.sample_rate = sample_rate
        self._winmm = ctypes.WinDLL("winmm")
        self._configure_winmm_signatures()
        self._handle = ctypes.c_void_p()
        fmt = WAVEFORMATEX(
            WAVE_FORMAT_PCM,
            2,
            sample_rate,
            sample_rate * 4,
            4,
            16,
            0,
        )
        result = self._winmm.waveOutOpen(
            ctypes.byref(self._handle),
            WAVE_MAPPER,
            ctypes.byref(fmt),
            0,
            0,
            0,
        )
        if result != 0:
            raise RuntimeError(f"waveOutOpen failed with MMRESULT {result}")
        self._buffers: list[tuple[ctypes.Array[ctypes.c_char], WAVEHDR, int]] = []
        self._submitted_frames = 0
        self._completed_frames = 0
        self._closed = False

    @property
    def submitted_frames(self) -> int:
        return self._submitted_frames

    @property
    def completed_frames(self) -> int:
        self._release_done_buffers()
        return self._completed_frames

    def write_pcm(self, payload: bytes, frame_count: int) -> None:
        if self._closed:
            return
        if not payload or frame_count <= 0:
            return
        self._release_done_buffers()
        buffer = ctypes.create_string_buffer(payload)
        header = WAVEHDR(
            ctypes.cast(buffer, ctypes.c_void_p),
            len(payload),
            0,
            None,
            0,
            0,
            None,
            None,
        )
        result = self._winmm.waveOutPrepareHeader(
            self._handle,
            ctypes.byref(header),
            ctypes.sizeof(header),
        )
        if result != 0:
            raise RuntimeError(f"waveOutPrepareHeader failed with MMRESULT {result}")
        result = self._winmm.waveOutWrite(
            self._handle,
            ctypes.byref(header),
            ctypes.sizeof(header),
        )
        if result != 0:
            self._winmm.waveOutUnprepareHeader(
                self._handle,
                ctypes.byref(header),
                ctypes.sizeof(header),
            )
            raise RuntimeError(f"waveOutWrite failed with MMRESULT {result}")
        self._buffers.append((buffer, header, frame_count))
        self._submitted_frames += frame_count

    def queued_frames(self) -> int:
        self._release_done_buffers()
        return self._submitted_frames - self._completed_frames

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._winmm.waveOutReset(self._handle)
        self._release_done_buffers(force=True)
        self._winmm.waveOutClose(self._handle)

    def _release_done_buffers(self, *, force: bool = False) -> None:
        remaining: list[tuple[ctypes.Array[ctypes.c_char], WAVEHDR, int]] = []
        for buffer, header, frame_count in self._buffers:
            if force or header.dwFlags & WHDR_DONE:
                self._winmm.waveOutUnprepareHeader(
                    self._handle,
                    ctypes.byref(header),
                    ctypes.sizeof(header),
                )
                self._completed_frames += frame_count
            else:
                remaining.append((buffer, header, frame_count))
        self._buffers = remaining

    def _configure_winmm_signatures(self) -> None:
        self._winmm.waveOutOpen.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint,
            ctypes.POINTER(WAVEFORMATEX),
            ctypes.c_size_t,
            ctypes.c_size_t,
            ctypes.c_uint,
        ]
        self._winmm.waveOutOpen.restype = ctypes.c_uint
        header_args = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        self._winmm.waveOutPrepareHeader.argtypes = header_args
        self._winmm.waveOutPrepareHeader.restype = ctypes.c_uint
        self._winmm.waveOutWrite.argtypes = header_args
        self._winmm.waveOutWrite.restype = ctypes.c_uint
        self._winmm.waveOutUnprepareHeader.argtypes = header_args
        self._winmm.waveOutUnprepareHeader.restype = ctypes.c_uint
        self._winmm.waveOutReset.argtypes = [ctypes.c_void_p]
        self._winmm.waveOutReset.restype = ctypes.c_uint
        self._winmm.waveOutClose.argtypes = [ctypes.c_void_p]
        self._winmm.waveOutClose.restype = ctypes.c_uint


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
        payload = samples_to_pcm16_bytes(samples)
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
