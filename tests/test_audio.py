from __future__ import annotations

import io
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from audio import BufferedAudioPlayer, WavAudioWriter, samples_to_pcm16_bytes, write_wav_samples


class FakePCMBackend:
    def __init__(self, sample_rate: int = 1000) -> None:
        self.sample_rate = sample_rate
        self.writes: list[tuple[bytes, int]] = []
        self._submitted_frames = 0
        self._completed_frames = 0
        self.closed = False

    @property
    def submitted_frames(self) -> int:
        return self._submitted_frames

    @property
    def completed_frames(self) -> int:
        return self._completed_frames

    def write_pcm(self, payload: bytes, frame_count: int) -> None:
        self.writes.append((payload, frame_count))
        self._submitted_frames += frame_count

    def queued_frames(self) -> int:
        return self._submitted_frames - self._completed_frames

    def complete(self, frame_count: int) -> None:
        self._completed_frames = min(self._submitted_frames, self._completed_frames + frame_count)

    def close(self) -> None:
        self.closed = True


class AudioTests(unittest.TestCase):
    def test_samples_to_pcm16_bytes_clamps_and_scales_stereo_samples(self) -> None:
        payload = samples_to_pcm16_bytes([(0, 0), (1, -1), (1000, -1000)])

        self.assertEqual(len(payload), 12)
        self.assertEqual(payload[:8], b"\x00\x00\x00\x00@\x00\xc0\xff")

    def test_buffered_audio_player_primes_and_submits_chunks(self) -> None:
        backend = FakePCMBackend(sample_rate=1000)
        player = BufferedAudioPlayer(
            sample_rate=1000,
            chunk_ms=10,
            target_buffer_ms=20,
            max_buffer_ms=50,
            backend=backend,
        )

        player.write([(1, -1)] * 25)

        self.assertEqual([frames for _payload, frames in backend.writes], [10, 10])
        self.assertEqual(player.stats().queued_frames, 25)

    def test_buffered_audio_player_primes_initial_write_only_to_target(self) -> None:
        backend = FakePCMBackend(sample_rate=1000)
        player = BufferedAudioPlayer(
            sample_rate=1000,
            chunk_ms=10,
            target_buffer_ms=20,
            max_buffer_ms=50,
            backend=backend,
        )

        player.write([(1, -1)] * 5)

        self.assertEqual([frames for _payload, frames in backend.writes], [10, 5])
        self.assertEqual(player.stats().queued_frames, 20)

    def test_buffered_audio_player_start_primes_target_buffer(self) -> None:
        backend = FakePCMBackend(sample_rate=1000)
        player = BufferedAudioPlayer(
            sample_rate=1000,
            chunk_ms=10,
            target_buffer_ms=25,
            max_buffer_ms=50,
            backend=backend,
        )

        player.start()

        self.assertEqual([frames for _payload, frames in backend.writes], [10, 10, 5])
        self.assertEqual(player.stats().queued_frames, 25)

    def test_buffered_audio_player_tracks_underruns(self) -> None:
        backend = FakePCMBackend(sample_rate=1000)
        player = BufferedAudioPlayer(
            sample_rate=1000,
            chunk_ms=10,
            target_buffer_ms=20,
            min_buffer_ms=10,
            backend=backend,
        )
        player.start()
        backend.complete(20)

        player.write([])
        player.write([])

        self.assertEqual(player.stats().underruns, 1)
        self.assertEqual(player.stats().low_buffer_events, 1)

    def test_buffered_audio_player_tracks_low_buffer_without_hard_underrun(self) -> None:
        backend = FakePCMBackend(sample_rate=1000)
        player = BufferedAudioPlayer(
            sample_rate=1000,
            chunk_ms=10,
            target_buffer_ms=30,
            min_buffer_ms=20,
            backend=backend,
        )
        player.start()
        backend.complete(15)

        player.write([])

        self.assertEqual(player.stats().queued_frames, 15)
        self.assertEqual(player.stats().underruns, 0)
        self.assertEqual(player.stats().low_buffer_events, 1)

    def test_write_wav_samples_outputs_stereo_pcm(self) -> None:
        output = io.BytesIO()

        write_wav_samples(output, [(0, 0), (1, -1), (600, -600)], sample_rate=8000)

        output.seek(0)
        with wave.open(output, "rb") as wav:
            self.assertEqual(wav.getnchannels(), 2)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 8000)
            self.assertEqual(wav.getnframes(), 3)

    def test_write_wav_samples_requires_positive_sample_rate(self) -> None:
        with self.assertRaises(ValueError):
            write_wav_samples(io.BytesIO(), [], sample_rate=0)

    def test_wav_writer_creates_parent_directories_for_path_output(self) -> None:
        output = Path("nested") / "audio.wav"
        output_file = io.BytesIO()

        with (
            patch.object(Path, "mkdir") as mkdir,
            patch.object(Path, "open", return_value=output_file) as open_file,
        ):
            with WavAudioWriter(output, sample_rate=8000) as writer:
                writer.write([(1, -1)])

        mkdir.assert_called_once_with(parents=True, exist_ok=True)
        open_file.assert_called_once_with("wb")


if __name__ == "__main__":
    unittest.main()
