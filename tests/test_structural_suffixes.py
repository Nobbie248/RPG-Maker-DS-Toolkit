import struct
import unittest

from rpgds_core import TextEntry, _apply_region_entries, fit_translation


CATALOG_ORIGINAL = "一軒家 小01:001"
CATALOG_SUFFIX = ":001"


def read_c_string(data: bytes | bytearray, offset: int) -> str:
    end = data.index(0, offset)
    return bytes(data[offset:end]).decode("cp932")


def compile_one_slot(original: str, translation: str) -> str:
    """Exercise the last text-writing stage without constructing a whole ROM."""
    ram_address = 0x02000000
    text_offset = 0x20
    original_raw = original.encode("cp932")
    data = bytearray(0x80)
    data[text_offset:text_offset + len(original_raw)] = original_raw
    struct.pack_into("<I", data, 0, ram_address + text_offset)
    entry = TextEntry(
        overlay=7,
        offset=text_offset,
        address=ram_address + text_offset,
        max_bytes=len(original_raw),
        original=original,
        translation=translation,
    )

    applied = _apply_region_entries(data, ram_address, [entry], "test overlay")

    if applied != 1:
        raise AssertionError(f"expected one applied translation, got {applied}")
    result_address = struct.unpack_from("<I", data, 0)[0]
    return read_c_string(data, result_address - ram_address)


class StructuralSuffixNormalizationTests(unittest.TestCase):
    def assert_catalog_result(self, result: str, max_bytes: int) -> None:
        self.assertTrue(result, "the protected suffix must not make fitting fail")
        self.assertTrue(result.endswith(CATALOG_SUFFIX), result)
        self.assertEqual(result.count(CATALOG_SUFFIX), 1, result)
        self.assertLessEqual(len(result.encode("cp932")), max_bytes)

    def test_missing_colon_is_restored(self) -> None:
        result = fit_translation(CATALOG_ORIGINAL, "House001", 15)
        self.assertEqual(result, "House:001")

    def test_duplicate_suffix_digits_are_collapsed(self) -> None:
        result = fit_translation(CATALOG_ORIGINAL, "House:001001", 15)
        self.assertEqual(result, "House:001")

    def test_spaced_wrong_suffix_is_replaced_by_exact_original_suffix(self) -> None:
        result = fit_translation(CATALOG_ORIGINAL, "House : 999", 15)
        self.assertEqual(result, "House:001")

    def test_unpadded_id_is_replaced_by_authoritative_suffix(self) -> None:
        original = "Shop:084"
        self.assertEqual(fit_translation(original, "SHOP NODOOR84", 15), "SHOP NODOOR:084")

    def test_only_authoritative_suffix_colon_remains(self) -> None:
        result = fit_translation(CATALOG_ORIGINAL, "House: Small001", 15)
        self.assertEqual(result, "House Small:001")

    def test_abbreviation_reserves_room_for_suffix(self) -> None:
        max_bytes = 12
        result = fit_translation(
            CATALOG_ORIGINAL,
            "An Extremely Large Detached House",
            max_bytes,
        )
        self.assert_catalog_result(result, max_bytes)

    def test_non_metadata_text_is_not_given_a_catalog_suffix(self) -> None:
        original = "Version:001 extra"
        english = "House : 999"
        self.assertEqual(fit_translation(original, english, 20), english)


class StructuralSuffixCompileTests(unittest.TestCase):
    def test_compile_repairs_missing_colon(self) -> None:
        self.assertEqual(compile_one_slot(CATALOG_ORIGINAL, "House001"), "House:001")

    def test_compile_collapses_duplicate_suffix_digits(self) -> None:
        self.assertEqual(
            compile_one_slot(CATALOG_ORIGINAL, "House:001001"),
            "House:001",
        )

    def test_compile_canonicalizes_spaced_wrong_suffix(self) -> None:
        self.assertEqual(
            compile_one_slot(CATALOG_ORIGINAL, "House : 999"),
            "House:001",
        )

    def test_compile_leaves_non_metadata_translation_untouched(self) -> None:
        self.assertEqual(
            compile_one_slot("Version:001 extra", "House : 999"),
            "House : 999",
        )


if __name__ == "__main__":
    unittest.main()
