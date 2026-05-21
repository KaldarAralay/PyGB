from __future__ import annotations

import io
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from audio import WavAudioWriter, write_wav_samples


class AudioTests(unittest.TestCase):
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
