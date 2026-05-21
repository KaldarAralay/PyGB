from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from emulator import Emulator
from ppu import DMG_GRAYSCALE


DMG_ACID2_ROM = Path("roms/dmg-acid2.gb")
REFERENCE_RGB_SHA256 = "2ba8286c29ae381838c71a88614302ce05f2b26102d1ed8dc51e25f83fcccc67"


def framebuffer_rgb_digest(emulator: Emulator) -> str:
    rgb = bytearray()
    for row in emulator.bus.ppu.framebuffer:
        for shade in row:
            rgb.extend(DMG_GRAYSCALE[shade & 0x03])
    return hashlib.sha256(rgb).hexdigest()


class DmgAcid2RegressionTests(unittest.TestCase):
    def test_dmg_acid2_matches_official_reference_image(self) -> None:
        if not DMG_ACID2_ROM.exists():
            self.skipTest(f"{DMG_ACID2_ROM} is not available")
        emulator = Emulator.from_rom_file(DMG_ACID2_ROM, serial_sink=lambda _: None)

        emulator.run(max_frames=1)

        self.assertEqual(emulator.bus.ppu.frame_count, 1)
        self.assertEqual(framebuffer_rgb_digest(emulator), REFERENCE_RGB_SHA256)


if __name__ == "__main__":
    unittest.main()
