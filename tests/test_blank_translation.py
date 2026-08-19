import tempfile
import unittest
from pathlib import Path

from rpgds_core import (
    TextEntry,
    _apply_region_entries,
    entry_translation_is_safe,
    load_project,
    save_project,
)


class IntentionalBlankTranslationTests(unittest.TestCase):
    def test_single_space_writes_an_empty_nul_string(self):
        data = bytearray(b"prefixJapanese\0suffix")
        entry = TextEntry(-1, 6, 0x02000006, 8, "Japanese", " ")
        applied = _apply_region_entries(data, 0x02000000, [entry], "test")
        self.assertEqual(applied, 1)
        self.assertEqual(data[6:14], b"\0" * 8)
        self.assertEqual(entry.used_bytes, 0)

    def test_empty_translation_still_means_keep_japanese(self):
        data = bytearray(b"Japanese\0")
        entry = TextEntry(-1, 0, 0x02000000, 8, "Japanese", "")
        self.assertEqual(_apply_region_entries(data, 0x02000000, [entry], "test"), 0)
        self.assertEqual(data, b"Japanese\0")

    def test_blank_cannot_remove_runtime_tokens_or_asset_ids(self):
        self.assertFalse(entry_translation_is_safe(
            TextEntry(1, 0, 0, 8, "%d gold", " ")
        ))
        self.assertFalse(entry_translation_is_safe(
            TextEntry(7, 0, 0, 8, "House:001", " ")
        ))

    def test_project_preserves_blank_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.nds"
            source.write_bytes(b"source")
            project = root / "blank.rpgdsproj"
            entry = TextEntry(-1, 0, 0x02000000, 8, "Japanese", " ")
            save_project(project, source, [entry], {})
            _source, rows, _images, _embedded = load_project(project)
            self.assertEqual(rows[entry.key]["translation"], " ")

    def test_save_dialog_yes_no_labels_stay_packed(self):
        offset = 0x7C6A4
        data = bytearray(offset + 12)
        data[offset:offset + 11] = b"\x82\xcd\x82\xa2\x82\xa2\x82\xa2\x82\xa6\0"
        entries = [
            TextEntry(5, offset, 0x021A07E4, 4, "はい", "YES"),
            TextEntry(5, offset + 4, 0x021A07E8, 6, "いいえ", "NO"),
        ]

        applied = _apply_region_entries(data, 0x02124140, entries, "overlay 5")

        self.assertEqual(applied, 2)
        self.assertEqual(data[offset:offset + 11], b"YES NO    \0")


if __name__ == "__main__":
    unittest.main()
