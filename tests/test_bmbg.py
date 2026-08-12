import struct
import unittest

import ndspy.codeCompression
from PIL import Image

from rpgds_core import decode_bmbg, encode_bmbg, parse_bmbg


def bgr555(rgb):
    r, g, b = rgb
    return ((r * 31 + 127) // 255) | (((g * 31 + 127) // 255) << 5) | (((b * 31 + 127) // 255) << 10)


class BMBGTests(unittest.TestCase):
    def test_embedded_4bpp_round_trip_keeps_fixed_decoded_size(self):
        palette = [(0, 0, 0), (255, 0, 0)] + [(0, 0, 0)] * 14
        header = b"BMBG" + struct.pack("<3H", 8, 2, 0x0104) + b"\0" * 6
        pixels = bytes([0x10, 0x10, 0x10, 0x10] * 2)
        raw = header + struct.pack("<16H", *(bgr555(c) for c in palette)) + pixels
        image = decode_bmbg(raw, False)
        rebuilt = encode_bmbg(image, raw, False)
        self.assertEqual(len(rebuilt), len(raw))
        self.assertEqual(decode_bmbg(rebuilt, False).tobytes(), image.tobytes())

    def test_external_8bpp_compressed_round_trip(self):
        palette = [(0, 0, 0), (255, 255, 255)] + [(0, 0, 0)] * 254
        header = b"BMBG" + struct.pack("<3H", 64, 64, 0x0008) + b"\0" * 6
        decoded = header + bytes([0, 1] * (64 * 64 // 2))
        raw = ndspy.codeCompression.compress(decoded, False)
        layout = parse_bmbg(raw, True, palette)
        self.assertEqual((layout.width, layout.height, layout.colors), (64, 64, 256))
        image = decode_bmbg(raw, True, palette)
        rebuilt = encode_bmbg(image, raw, True, palette)
        self.assertEqual(len(ndspy.codeCompression.decompress(rebuilt)), len(decoded))
        self.assertEqual(decode_bmbg(rebuilt, True, palette).tobytes(), image.tobytes())

    def test_rejects_wrong_dimensions(self):
        palette = [(0, 0, 0)] * 256
        raw = b"BMBG" + struct.pack("<3H", 8, 2, 8) + b"\0" * 6 + bytes(16)
        with self.assertRaisesRegex(ValueError, "exactly 8x2"):
            encode_bmbg(Image.new("RGB", (7, 2)), raw, False, palette)


if __name__ == "__main__":
    unittest.main()
