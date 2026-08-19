import struct
import unittest
from types import SimpleNamespace
from unittest import mock

import rpgds_core


class RuntimeFixTests(unittest.TestCase):
    def test_ds_plus_canvas_work_map_uses_eight_byte_records(self):
        base = 0x02004000
        address, expected, replacement = rpgds_core.DS_PLUS_CANVAS_CAPACITY_PATCH
        arm9 = bytearray(address - base + 4)
        struct.pack_into("<I", arm9, address - base, expected)
        rom = SimpleNamespace(idCode=b"VEBJ", arm9RamAddress=base, arm9=bytearray(arm9))

        with mock.patch.object(
            rpgds_core.ndspy.codeCompression, "decompress", return_value=bytes(arm9)
        ), mock.patch.object(
            rpgds_core, "_compress_arm9", side_effect=lambda data, _base: bytearray(data)
        ):
            rpgds_core.apply_ds_plus_runtime_fixes(rom)

        self.assertEqual(
            struct.unpack_from("<I", rom.arm9, address - base)[0], replacement
        )

    def test_runtime_fix_rejects_unknown_instruction(self):
        base = 0x02004000
        address, _expected, _replacement = rpgds_core.DS_PLUS_CANVAS_CAPACITY_PATCH
        arm9 = bytearray(address - base + 4)
        rom = SimpleNamespace(idCode=b"VEBJ", arm9RamAddress=base, arm9=bytearray(arm9))

        with mock.patch.object(
            rpgds_core.ndspy.codeCompression, "decompress", return_value=bytes(arm9)
        ):
            with self.assertRaisesRegex(ValueError, "refusing to patch an unknown ROM"):
                rpgds_core.apply_ds_plus_runtime_fixes(rom)

    def test_other_games_are_unchanged(self):
        rom = SimpleNamespace(idCode=b"YREJ", arm9RamAddress=0, arm9=bytearray(b"unchanged"))
        rpgds_core.apply_ds_plus_runtime_fixes(rom)
        self.assertEqual(rom.arm9, bytearray(b"unchanged"))


if __name__ == "__main__":
    unittest.main()
