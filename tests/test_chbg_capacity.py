import struct
import unittest

import ndspy.codeCompression
from PIL import Image

from rpgds_core import (
    CHBGCapacityError,
    decode_chbg,
    parse_chbg,
    prepare_chbg_replacement,
)


PALETTE_VALUES = (
    0x0000,  # black / key
    0x001F,  # red
    0x03E0,  # green
    0x7C00,  # blue
    0x03FF,  # yellow
) + (0,) * 11


def uniform_tile(index: int) -> bytes:
    return bytes([index]) * 64


def checker_tile(first: int, second: int) -> bytes:
    return bytes(
        first if (x + y) % 2 == 0 else second
        for y in range(8)
        for x in range(8)
    )


def progressive_tile(changed_pixels: int) -> bytes:
    """Return distinct, palette-exact tiles for capacity-boundary tests."""
    return bytes(2 if position < changed_pixels else 1 for position in range(64))


def pack_4bpp(tile: bytes) -> bytes:
    return bytes(
        (tile[position] & 0xF) | ((tile[position + 1] & 0xF) << 4)
        for position in range(0, 64, 2)
    )


def make_chbg(tile_map: list[int], tiles: list[bytes], compressed: bool = False,
              bpp: int = 8,
              palette_values: tuple[int, ...] = PALETTE_VALUES) -> bytes:
    if not palette_values or len(palette_values) % 16:
        raise ValueError("CHBG test palettes must contain a multiple of 16 colors")
    width = len(tile_map) * 8
    height = 8
    header = bytearray(16)
    header[:4] = b"CHBG"
    fmt = ((len(palette_values) // 16) << 8) | bpp
    struct.pack_into("<4H", header, 4, width, height, fmt, len(tiles))
    raw = bytes(header)
    raw += struct.pack(f"<{len(palette_values)}H", *palette_values)
    raw += struct.pack(f"<{len(tile_map)}H", *tile_map)
    raw += b"".join(tiles)
    return ndspy.codeCompression.compress(raw, False) if compressed else raw


def image_from_tiles(tiles: list[bytes]) -> Image.Image:
    colors = {
        0: (0, 0, 0),
        1: (255, 0, 0),
        2: (0, 255, 0),
        3: (0, 0, 255),
        4: (255, 255, 0),
    }
    image = Image.new("RGB", (len(tiles) * 8, 8))
    for tile_x, tile in enumerate(tiles):
        for y in range(8):
            for x in range(8):
                image.putpixel((tile_x * 8 + x, y), colors[tile[y * 8 + x]])
    return image


def palette_index_at(raw: bytes, x: int, y: int) -> int:
    """Read the stored palette index at a decoded image coordinate."""
    layout = parse_chbg(raw, False)
    tiles_wide = layout.width // 8
    map_index = (y // 8) * tiles_wide + x // 8
    tile_id = layout.tile_map[map_index]
    tile_bytes = layout.bpp * 8
    tile = layout.tile_data[tile_id * tile_bytes:(tile_id + 1) * tile_bytes]
    pixel_in_tile = (y % 8) * 8 + x % 8
    if layout.bpp == 8:
        return tile[pixel_in_tile]
    packed = tile[pixel_in_tile // 2]
    return packed & 0xF if pixel_in_tile % 2 == 0 else packed >> 4


class CHBGCapacityTests(unittest.TestCase):
    def test_unchanged_uncompressed_is_byte_identical(self) -> None:
        original = make_chbg([0, 1], [uniform_tile(1), uniform_tile(2)])
        image = decode_chbg(original, False)
        result = prepare_chbg_replacement(image, original, False)
        self.assertEqual(result.data, original)
        self.assertEqual(result.palette_adjusted_pixels, 0)

    def test_unchanged_compressed_is_byte_identical(self) -> None:
        original = make_chbg([0] * 8, [uniform_tile(1)], compressed=True)
        image = decode_chbg(original, True)
        result = prepare_chbg_replacement(image, original, True)
        self.assertEqual(result.data, original)

    def test_lossless_growth_uses_allowance_and_keeps_original_ids(self) -> None:
        # Tile ID 0 splits into A/B, while IDs 1 and 2 both become C. The
        # duplicate C appearance exists, but the 150% decoded-size allowance
        # lets B use a new tile so the existing IDs remain stable.
        tile_a = uniform_tile(1)
        tile_b = checker_tile(1, 2)
        tile_c = uniform_tile(2)
        original = make_chbg(
            [0, 0, 1, 2],
            [tile_a, tile_c, uniform_tile(3)],
        )
        target = image_from_tiles([tile_a, tile_b, tile_c, tile_c])

        result = prepare_chbg_replacement(target, original, False)
        rebuilt = parse_chbg(result.data, False)

        self.assertEqual(result.required_tiles, 3)
        self.assertEqual(result.capacity_tiles, 4)
        self.assertEqual(result.output_tiles, 4)
        self.assertEqual(rebuilt.tile_count, 4)
        self.assertEqual(len(result.data), len(original) + 64)
        self.assertEqual(decode_chbg(result.data, False).tobytes(), target.tobytes())
        # Unchanged cells retain their original tile IDs; only edited cells
        # may be redirected to a reclaimed slot.
        self.assertEqual(rebuilt.tile_map[0], 0)
        self.assertEqual(rebuilt.tile_map[2], 1)

    def test_impossible_lossless_fit_is_rejected(self) -> None:
        tile_a = checker_tile(1, 2)
        tile_b = checker_tile(2, 1)
        original = make_chbg([0, 0], [tile_a])
        target = image_from_tiles([tile_a, tile_b])

        with self.assertRaises(CHBGCapacityError) as raised:
            prepare_chbg_replacement(target, original, False)

        self.assertEqual(raised.exception.required_tiles, 2)
        self.assertEqual(raised.exception.capacity_tiles, 1)
        self.assertIn("requires 2 distinct 8x8 tiles", str(raised.exception))

    def test_exact_fifty_percent_decoded_growth_is_allowed_for_8bpp(self) -> None:
        # Eight map cells and one 8bpp tile make an exact 128-byte decoded
        # asset: 16-byte header + 32-byte palette + 16-byte map + 64 tile.
        # Adding one 64-byte tile produces exactly 192 bytes (+50%). Although
        # the tile table doubles, this must be accepted because the hard limit
        # applies to total decoded data, not tile count or BLZ/PNG size.
        tile_a = checker_tile(1, 2)
        tile_b = checker_tile(2, 1)
        source_tiles = [tile_a]
        source_map = [0] * 8
        target_tiles = [tile_a, tile_b] + [tile_a] * 6

        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                original = make_chbg(source_map, source_tiles, compressed=compressed)
                target = image_from_tiles(target_tiles)
                result = prepare_chbg_replacement(target, original, compressed)
                rebuilt = parse_chbg(result.data, compressed)

                self.assertEqual(result.required_tiles, 2)
                self.assertEqual(result.original_tiles, 1)
                self.assertEqual(result.capacity_tiles, 2)
                self.assertEqual(result.output_tiles, 2)
                self.assertEqual(result.original_decompressed_size, 128)
                self.assertEqual(result.output_decompressed_size, 192)
                self.assertEqual(
                    result.output_decompressed_size * 100,
                    result.original_decompressed_size * 150,
                )
                self.assertEqual(rebuilt.tile_count, 2)
                self.assertEqual(
                    decode_chbg(result.data, compressed).tobytes(), target.tobytes(),
                )

    def test_growth_preserves_original_id_for_minority_unchanged_cell(self) -> None:
        source_tiles = [progressive_tile(count) for count in range(4)]
        changed = progressive_tile(4)
        source_map = [0, 1, 2, 3, 0, 0, 0, 1]
        # Three of tile 0's four repeated cells change. The one unchanged cell
        # must still retain ID 0 and its exact original payload; the edited
        # majority belongs in the newly allowed fifth tile.
        target_tiles = source_tiles + [
            changed, changed, changed, source_tiles[1],
        ]
        original = make_chbg(source_map, source_tiles)
        result = prepare_chbg_replacement(
            image_from_tiles(target_tiles), original, False,
        )
        rebuilt = parse_chbg(result.data, False)

        self.assertEqual(rebuilt.tile_map[0], 0)
        self.assertEqual(rebuilt.tile_data[:64], source_tiles[0])
        self.assertNotEqual(rebuilt.tile_map[4], 0)
        self.assertEqual(
            decode_chbg(result.data, False).tobytes(),
            image_from_tiles(target_tiles).tobytes(),
        )

    def test_more_than_fifty_percent_decoded_growth_is_rejected_for_8bpp(self) -> None:
        # The same 128-byte asset only has room for one extra 64-byte tile.
        # Three distinct appearances would require 256 decoded bytes (+100%).
        tile_a = checker_tile(1, 2)
        tile_b = checker_tile(2, 1)
        tile_c = progressive_tile(32)
        source_tiles = [tile_a]
        source_map = [0] * 8
        target = image_from_tiles([tile_a, tile_b, tile_c] + [tile_a] * 5)

        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                original = make_chbg(source_map, source_tiles, compressed=compressed)
                with self.assertRaises(CHBGCapacityError) as raised:
                    prepare_chbg_replacement(target, original, compressed)

                self.assertEqual(raised.exception.required_tiles, 3)
                self.assertEqual(raised.exception.capacity_tiles, 2)
                self.assertEqual(raised.exception.original_tiles, 1)
                self.assertIn("50% decoded-data allowance", str(raised.exception))

    def test_exact_fifty_percent_decoded_growth_is_allowed_for_4bpp(self) -> None:
        # Two 4bpp tiles with eight map cells decode to exactly 128 bytes.
        # Two more 32-byte tiles produce exactly 192 bytes (+50%).
        source_pixels = [progressive_tile(count) for count in range(2)]
        source_map = [0, 1, 0, 0, 0, 0, 0, 0]
        target_tiles = source_pixels + [
            progressive_tile(2), progressive_tile(3),
            source_pixels[0], source_pixels[0], source_pixels[0], source_pixels[0],
        ]
        original = make_chbg(
            source_map,
            [pack_4bpp(tile) for tile in source_pixels],
            bpp=4,
        )

        result = prepare_chbg_replacement(image_from_tiles(target_tiles), original, False)
        rebuilt = parse_chbg(result.data, False)

        self.assertEqual(result.required_tiles, 4)
        self.assertEqual(result.original_tiles, 2)
        self.assertEqual(result.capacity_tiles, 4)
        self.assertEqual(result.output_tiles, 4)
        self.assertEqual(result.original_decompressed_size, 128)
        self.assertEqual(result.output_decompressed_size, 192)
        self.assertEqual(
            result.output_decompressed_size * 100,
            result.original_decompressed_size * 150,
        )
        self.assertEqual(rebuilt.bpp, 4)
        self.assertEqual(rebuilt.tile_count, 4)
        self.assertEqual(
            decode_chbg(result.data, False).tobytes(),
            image_from_tiles(target_tiles).tobytes(),
        )

    def test_more_than_fifty_percent_decoded_growth_is_rejected_for_4bpp(self) -> None:
        # The 128-byte 4bpp asset has room for two extra 32-byte tiles. Five
        # distinct appearances would require 224 decoded bytes (+75%).
        source_pixels = [progressive_tile(count) for count in range(2)]
        source_map = [0, 1, 0, 0, 0, 0, 0, 0]
        target_tiles = source_pixels + [
            progressive_tile(2), progressive_tile(3), progressive_tile(4),
            source_pixels[0], source_pixels[0], source_pixels[0],
        ]
        original = make_chbg(
            source_map,
            [pack_4bpp(tile) for tile in source_pixels],
            bpp=4,
        )

        with self.assertRaises(CHBGCapacityError) as raised:
            prepare_chbg_replacement(image_from_tiles(target_tiles), original, False)

        self.assertEqual(raised.exception.required_tiles, 5)
        self.assertEqual(raised.exception.capacity_tiles, 4)
        self.assertEqual(raised.exception.original_tiles, 2)
        self.assertIn("50% decoded-data allowance", str(raised.exception))

    def test_fully_transparent_pixels_use_key_index(self) -> None:
        original = make_chbg([0], [uniform_tile(1)])
        target = Image.new("RGBA", (8, 8), (123, 45, 67, 0))
        result = prepare_chbg_replacement(target, original, False)
        rebuilt = parse_chbg(result.data, False)
        tile_id = rebuilt.tile_map[0]
        tile = rebuilt.tile_data[tile_id * 64:(tile_id + 1) * 64]
        self.assertEqual(tile, bytes(64))


class CHBGPaletteMappingTests(unittest.TestCase):
    def test_animated_normal_bank_does_not_collapse_into_global_grays(self) -> None:
        # Model topmenu's normal/highlight bank relationship: the normal glyph
        # uses 65/66 and the runtime highlight shifts those indices by +96 to
        # 161/162. Global indices 1/2 are visually closer to the replacement,
        # but using them would create the white/black highlight corruption.
        palette = [0] * 256
        palette[0] = 0x03E0       # green key/background
        palette[1] = 0x0000       # global black
        palette[2] = 0x7FFF       # global white
        palette[65] = 0x7C00      # normal-bank blue
        palette[66] = 0x7FFF      # normal-bank white
        palette[161] = 0x001F     # highlighted red/orange
        palette[162] = 0x03FF     # highlighted yellow
        normal_glyph = checker_tile(65, 66)
        original = make_chbg(
            [0] * 16,
            [normal_glyph],
            palette_values=tuple(palette),
        )
        target = Image.new("RGB", (128, 8))
        for y in range(8):
            for x in range(128):
                target.putpixel((x, y), (0, 0, 0) if (x + y) % 2 == 0 else (255, 255, 255))

        result = prepare_chbg_replacement(target, original, False)
        stored_indices = {
            palette_index_at(result.data, x, y)
            for y in range(8)
            for x in range(128)
        }

        self.assertEqual(stored_indices, {65, 66})
        self.assertEqual({index + 96 for index in stored_indices}, {161, 162})

    def test_exact_color_does_not_borrow_another_regions_palette_role(self) -> None:
        # Blue and green model separate animated palette roles. Even though
        # blue is an exact global palette color, edited pixels in the green
        # region must remain in that region's role so runtime recoloring still
        # affects them coherently.
        blue = uniform_tile(3)
        green = uniform_tile(2)
        original = make_chbg([0] * 16 + [1] * 16, [blue, green])
        target = Image.new("RGB", (256, 8), (0, 0, 255))

        result = prepare_chbg_replacement(target, original, False)
        rebuilt = decode_chbg(result.data, False)

        self.assertEqual(rebuilt.getpixel((127, 4)), (0, 0, 255))
        self.assertEqual(rebuilt.getpixel((128, 4)), (0, 255, 0))
        self.assertGreater(result.palette_adjusted_pixels, 0)

    def test_off_palette_color_stays_in_local_animated_role(self) -> None:
        # A nearly-blue source color in the green role is still encoded with
        # the green role. Resting RGB distance is secondary to keeping every
        # pixel in the palette bank that the game animates together.
        blue = uniform_tile(3)
        green = uniform_tile(2)
        original = make_chbg([0] * 16 + [1] * 16, [blue, green])
        target = decode_chbg(original, False)
        target.paste((8, 0, 247), (128, 0, 256, 8))

        result = prepare_chbg_replacement(target, original, False)
        rebuilt = decode_chbg(result.data, False)

        self.assertEqual(rebuilt.getpixel((128, 4)), (0, 255, 0))
        self.assertEqual(rebuilt.getpixel((255, 4)), (0, 255, 0))

    def test_near_key_background_and_empty_region_role_inheritance(self) -> None:
        # The right 128-pixel region is originally background-only. It borrows
        # the blue role from its populated sibling so a longer translation can
        # cross x=128. A slightly shifted near-black editor background still
        # maps uniformly to key index 0 instead of becoming colored blocks.
        key = uniform_tile(0)
        blue = uniform_tile(3)
        original = make_chbg(
            [1] * 8 + [0] * 8 + [0] * 16,
            [key, blue],
        )
        target = Image.new("RGB", (256, 8), (1, 1, 1))
        target.paste((0, 0, 255), (120, 0, 160, 8))

        result = prepare_chbg_replacement(target, original, False)
        rebuilt = decode_chbg(result.data, False)

        self.assertEqual(rebuilt.getpixel((0, 4)), (0, 0, 0))
        self.assertEqual(rebuilt.getpixel((119, 4)), (0, 0, 0))
        self.assertEqual(rebuilt.getpixel((120, 4)), (0, 0, 255))
        self.assertEqual(rebuilt.getpixel((128, 4)), (0, 0, 255))
        self.assertEqual(rebuilt.getpixel((159, 4)), (0, 0, 255))
        self.assertEqual(rebuilt.getpixel((160, 4)), (0, 0, 0))
        self.assertEqual(palette_index_at(result.data, 128, 4), 3)

    def test_duplicate_exact_colors_prefer_each_regions_local_role(self) -> None:
        # Palette indices 1 and 5 contain identical red RGB values but model
        # different in-game palette roles. When a green tile is replaced with
        # exact red, use the duplicate already active in that region: index 1
        # on the left and index 5 on the right.
        duplicate_red_palette = list(PALETTE_VALUES)
        duplicate_red_palette[5] = duplicate_red_palette[1]
        green = uniform_tile(2)
        red_role_one = uniform_tile(1)
        red_role_five = uniform_tile(5)
        original = make_chbg(
            [0] + [1] * 15 + [0] + [2] * 15,
            [green, red_role_one, red_role_five],
            palette_values=tuple(duplicate_red_palette),
        )
        target = Image.new("RGB", (256, 8), (255, 0, 0))

        result = prepare_chbg_replacement(target, original, False)

        self.assertEqual(palette_index_at(result.data, 0, 4), 1)
        self.assertEqual(palette_index_at(result.data, 128, 4), 5)
        self.assertEqual(
            decode_chbg(result.data, False).tobytes(), target.tobytes(),
        )


if __name__ == "__main__":
    unittest.main()
