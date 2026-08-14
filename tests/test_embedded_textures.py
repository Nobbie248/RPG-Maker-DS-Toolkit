import struct
import unittest

from PIL import Image
import ndspy.codeCompression

from rpgds_core import (
    ImageAsset,
    _decode_dbz,
    embedded_texture_info,
    image_asset_chbg,
    parse_chbg,
    prepare_chbg_replacement,
    rebuild_embedded_texture,
)


def make_chbg() -> bytes:
    palette = [0] * 16
    palette[1] = 0x001F
    tile = bytes([1] * 64)
    return (
        b"CHBG" + struct.pack("<4H", 8, 8, 0x0108, 1) + b"\0" * 4
        + struct.pack("<16H", *palette) + struct.pack("<H", 0) + tile
    )


def make_daeh(chbg: bytes) -> bytes:
    head = b"DAEH" + struct.pack("<I", 12) + b"HEAD"
    payload = b"PREFIX00" + chbg + b"\xAA\x55"
    txet = b"TXET" + struct.pack("<I", len(payload) + 8) + payload
    pass_chunk = b"SSAP" + struct.pack("<I", 12) + b"PASS"
    return head + txet + pass_chunk


def make_dbz(decoded: bytes, split: int) -> bytes:
    decoded_parts = (decoded[:split], decoded[split:])
    parts = tuple(ndspy.codeCompression.compress(part, False) for part in decoded_parts)
    return (b"DBZ\x02" + struct.pack("<2H", *(len(part) for part in parts))
            + b"".join(parts))


class EmbeddedTextureTests(unittest.TestCase):
    def setUp(self):
        self.chbg = make_chbg()
        self.daeh = make_daeh(self.chbg)

    def _asset(self, wrapper: str) -> ImageAsset:
        return ImageAsset(
            7, "town/test.blz::texture", 8, 8, 8, 16, 1,
            len(self.chbg), wrapper != "RAW", "TXET",
        )

    def test_finds_texture_and_preserves_txet_padding(self):
        info = embedded_texture_info(self.daeh)
        self.assertIsNotNone(info)
        self.assertEqual(info.wrapper, "RAW")
        self.assertEqual(
            info.decoded_container[info.chbg_offset:info.chbg_offset + info.chbg_size],
            self.chbg,
        )
        self.assertEqual(info.decoded_container[info.chbg_offset + info.chbg_size:][:2], b"\xAA\x55")

    def test_blz_container_round_trip_changes_only_chbg(self):
        original = ndspy.codeCompression.compress(self.daeh, False)
        info = embedded_texture_info(original)
        self.assertIsNotNone(info)
        self.assertEqual(info.wrapper, "BLZ")
        source = image_asset_chbg(original, self._asset("BLZ"))
        image = Image.new("RGB", (8, 8), (0, 0, 0))
        prepared = prepare_chbg_replacement(image, source, False, False, 0)
        rebuilt = rebuild_embedded_texture(original, prepared.data)
        after = embedded_texture_info(rebuilt)
        self.assertEqual(len(after.decoded_container), len(info.decoded_container))
        self.assertEqual(after.decoded_container[:info.chbg_offset], info.decoded_container[:info.chbg_offset])
        self.assertEqual(
            after.decoded_container[info.chbg_offset + info.chbg_size:],
            info.decoded_container[info.chbg_offset + info.chbg_size:],
        )
        self.assertEqual(parse_chbg(image_asset_chbg(rebuilt, self._asset("BLZ")), False).tile_count, 1)

    def test_segmented_dbz_keeps_decoded_boundaries_and_other_chunks(self):
        original = make_dbz(self.daeh, 64)
        before = embedded_texture_info(original)
        self.assertEqual(before.wrapper, "DBZ")
        self.assertEqual(before.segment_sizes, (64, len(self.daeh) - 64))
        prepared = prepare_chbg_replacement(
            Image.new("RGB", (8, 8), (0, 0, 0)), self.chbg, False, False, 0,
        )
        rebuilt = rebuild_embedded_texture(original, prepared.data)
        decoded, sizes = _decode_dbz(rebuilt)
        after = embedded_texture_info(rebuilt)
        self.assertEqual(sizes, before.segment_sizes)
        self.assertEqual(len(decoded), len(self.daeh))
        self.assertEqual(decoded[:before.chbg_offset], self.daeh[:before.chbg_offset])
        self.assertEqual(
            decoded[before.chbg_offset + before.chbg_size:],
            self.daeh[before.chbg_offset + before.chbg_size:],
        )
        self.assertEqual(after.chbg_size, len(self.chbg))


if __name__ == "__main__":
    unittest.main()
