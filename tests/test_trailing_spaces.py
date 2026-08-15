import struct
import unittest

from rpgds_core import TextEntry, _apply_region_entries, _fullwidth_event_text
from rpgds_gui import editor_translation_value


class TrailingSpaceTests(unittest.TestCase):
    def test_editor_keeps_trailing_spaces_after_text(self):
        self.assertEqual(editor_translation_value("Equip "), "Equip ")
        self.assertEqual(editor_translation_value("Equip  "), "Equip  ")

    def test_whitespace_only_still_means_blank_in_rom(self):
        self.assertEqual(editor_translation_value(" "), " ")
        self.assertEqual(editor_translation_value("   "), " ")
        self.assertEqual(editor_translation_value(""), "")

    def test_compile_writes_trailing_space_before_nul(self):
        ram_address = 0x02000000
        offset = 0x20
        data = bytearray(0x80)
        original = "テスト"
        original_raw = original.encode("cp932")
        data[offset:offset + len(original_raw)] = original_raw
        struct.pack_into("<I", data, 0, ram_address + offset)
        entry = TextEntry(
            -1, offset, ram_address + offset, len(original_raw), original, "Test ",
        )

        self.assertEqual(_apply_region_entries(data, ram_address, [entry], "test"), 1)
        target = struct.unpack_from("<I", data, 0)[0] - ram_address
        end = data.index(0, target)
        self.assertEqual(bytes(data[target:end]), b"Test ")

    def test_private_event_codec_preserves_trailing_spaces(self):
        self.assertTrue(_fullwidth_event_text("GET! ").endswith("\u3000"))
        self.assertTrue(_fullwidth_event_text(" GET!  ").endswith("\u3000\u3000"))


if __name__ == "__main__":
    unittest.main()
