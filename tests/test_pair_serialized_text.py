import unittest

from rpgds_core import (
    TextEntry,
    _apply_region_entries,
    _fullwidth_event_text,
    entry_translation_is_safe,
    repair_entry_translation,
)


class PairSerializedTextTests(unittest.TestCase):
    def make_entry(self, translation: str = "I got it!") -> TextEntry:
        return TextEntry(
            overlay=9,
            offset=0x5264C,
            address=0x0217678C,
            max_bytes=14,
            original="を手に入れた！",
            translation=translation,
        )

    def test_acquisition_suffix_uses_round_trip_safe_fullwidth_text(self) -> None:
        entry = self.make_entry()
        repaired = repair_entry_translation(entry, entry.translation)

        self.assertEqual(repaired, _fullwidth_event_text(" FOUND!"))
        self.assertEqual(repaired.encode("cp932").hex(), "81408265826e8274826d82638149")
        self.assertEqual(len(repaired.encode("cp932")) % 2, 0)
        # The pairwise reader now sees the NUL at the start of its next pair.
        self.assertEqual((repaired.encode("cp932") + b"\0")[14], 0)

    def test_even_ascii_is_still_not_safe_for_the_private_charset(self) -> None:
        entry = self.make_entry(" I got it!")
        self.assertFalse(entry_translation_is_safe(entry))

    def test_compile_path_repairs_existing_project_translation(self) -> None:
        entry = self.make_entry()
        original = entry.original.encode("cp932")
        data = bytearray(entry.offset + len(original) + 1)
        data[entry.offset : entry.offset + len(original)] = original

        count = _apply_region_entries(data, entry.address, [entry], "test", True)

        self.assertEqual(count, 1)
        expected = _fullwidth_event_text(" FOUND!")
        self.assertEqual(entry.translation, expected)
        self.assertEqual(
            bytes(data[entry.offset : entry.offset + entry.max_bytes]),
            expected.encode("cp932"),
        )

    def test_long_empty_chest_message_gets_slot_safe_fallback(self) -> None:
        entry = TextEntry(
            overlay=9,
            offset=0x51BD2,
            address=0x02175D12,
            max_bytes=22,
            original="宝箱はカラッポだった。",
            translation="The chest was empty.",
        )
        repaired = repair_entry_translation(entry, entry.translation)
        self.assertEqual(repaired, _fullwidth_event_text("CHEST EMPTY"))
        self.assertEqual(len(repaired.encode("cp932")), 22)

    def test_formatted_message_preserves_percent_token(self) -> None:
        entry = TextEntry(
            overlay=9,
            offset=0x51B80,
            address=0x02175CC0,
            max_bytes=16,
            original="一泊%dＧですが、",
            translation="It's%dGOneNght",
        )
        repaired = repair_entry_translation(entry, entry.translation)
        self.assertEqual(repaired, _fullwidth_event_text("NITE %dG"))
        self.assertIn("%d", repaired)
        self.assertTrue(entry_translation_is_safe(TextEntry(
            entry.overlay, entry.offset, entry.address, entry.max_bytes,
            entry.original, repaired,
        )))

    def test_same_words_in_an_unrelated_entry_are_unchanged(self) -> None:
        entry = self.make_entry()
        entry.overlay = 8
        self.assertEqual(repair_entry_translation(entry, entry.translation), "I got it!")


if __name__ == "__main__":
    unittest.main()
