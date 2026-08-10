import io
import struct
import unittest

from PIL import Image, PngImagePlugin

from rpgds_core import sanitize_import_image, sanitize_png_bytes


ORIENTED_OPAQUE_PIXELS = (
    (255, 255, 0, 255), (255, 0, 0, 255),
    (0, 255, 255, 255), (0, 255, 0, 255),
    (255, 0, 255, 255), (0, 0, 255, 255),
)

FORBIDDEN_METADATA_CHUNKS = {
    b"eXIf",  # EXIF, including orientation
    b"iCCP",  # ICC color profile
    b"iTXt",  # XMP and other international text
    b"pHYs",  # DPI / physical resolution
    b"tEXt",  # Software, IPTC, and other plain text
    b"zTXt",  # Compressed textual metadata
}


def image_pixels(image: Image.Image) -> tuple[tuple[int, ...], ...]:
    """Return pixels without relying on Pillow's deprecated getdata API."""
    return tuple(
        image.getpixel((x, y))
        for y in range(image.height)
        for x in range(image.width)
    )


def png_chunk_types(data: bytes) -> tuple[bytes, ...]:
    """Return the ordered PNG chunk types, rejecting malformed test output."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("not a PNG stream")

    chunk_types: list[bytes] = []
    position = 8
    while position < len(data):
        if position + 12 > len(data):
            raise AssertionError("truncated PNG chunk")
        length = struct.unpack_from(">I", data, position)[0]
        chunk_type = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data):
            raise AssertionError("PNG chunk extends past end of stream")
        chunk_types.append(chunk_type)
        position = end
        if chunk_type == b"IEND":
            break

    if position != len(data) or not chunk_types or chunk_types[-1] != b"IEND":
        raise AssertionError("invalid PNG chunk sequence")
    return tuple(chunk_types)


def make_metadata_heavy_png() -> bytes:
    """Create a PNG with each kind of metadata the importer must discard."""
    image = Image.new("RGB", (3, 2))
    image.putdata((
        (255, 0, 0), (0, 255, 0), (0, 0, 255),
        (255, 255, 0), (0, 255, 255), (255, 0, 255),
    ))

    # Orientation 6 means that the encoded 3x2 image must be displayed as a
    # 2x3 image rotated 90 degrees clockwise.
    exif = Image.Exif()
    exif[274] = 6

    text = PngImagePlugin.PngInfo()
    text.add_itxt(
        "XML:com.adobe.xmp",
        "<x:xmpmeta>" + ("unused-xmp-data" * 64) + "</x:xmpmeta>",
    )
    text.add_text("Raw profile type iptc", "unused-iptc-data" * 64)
    text.add_text("Software", "Paint.NET 5.1.12")
    text.add_text("Comment", "unused-compressed-text" * 64, zip=True)

    output = io.BytesIO()
    image.save(
        output,
        "PNG",
        exif=exif,
        pnginfo=text,
        dpi=(300, 300),
        icc_profile=b"unused-fake-icc-profile" * 64,
        compress_level=0,
    )
    return output.getvalue()


class PNGImportSanitizerTests(unittest.TestCase):
    def test_open_image_is_oriented_and_reduced_to_metadata_free_rgba(self) -> None:
        source_data = make_metadata_heavy_png()
        with Image.open(io.BytesIO(source_data)) as source:
            self.assertEqual(source.getexif().get(274), 6)
            self.assertIn("Software", source.info)
            self.assertIn("icc_profile", source.info)
            self.assertIn("dpi", source.info)

            sanitized = sanitize_import_image(source)

        self.assertEqual(sanitized.mode, "RGBA")
        self.assertEqual(sanitized.size, (2, 3))
        self.assertEqual(image_pixels(sanitized), ORIENTED_OPAQUE_PIXELS)
        self.assertEqual(sanitized.info, {})
        self.assertEqual(len(sanitized.getexif()), 0)
        self.assertIsNone(sanitized.format)
        self.assertIsNone(getattr(sanitized, "filename", None))

    def test_png_bytes_strip_metadata_and_preserve_oriented_pixels(self) -> None:
        source_data = make_metadata_heavy_png()
        source_chunks = set(png_chunk_types(source_data))
        self.assertTrue(FORBIDDEN_METADATA_CHUNKS.issubset(source_chunks))

        sanitized_data = sanitize_png_bytes(source_data)
        sanitized_chunks = set(png_chunk_types(sanitized_data))

        self.assertTrue(FORBIDDEN_METADATA_CHUNKS.isdisjoint(sanitized_chunks))
        self.assertLess(len(sanitized_data), len(source_data))
        with Image.open(io.BytesIO(sanitized_data)) as sanitized:
            sanitized.load()
            # Opaque sanitized PNGs may use RGB to avoid a useless alpha byte.
            self.assertEqual(sanitized.mode, "RGB")
            self.assertEqual(sanitized.size, (2, 3))
            self.assertEqual(
                image_pixels(sanitized.convert("RGBA")),
                ORIENTED_OPAQUE_PIXELS,
            )
            self.assertEqual(sanitized.info, {})
            self.assertEqual(len(sanitized.getexif()), 0)

        # A zero-compression copy provides a stable upper bound proving the
        # pixel-only result was actually compressed, rather than merely having
        # its metadata tags removed.
        oriented = Image.new("RGB", (2, 3))
        oriented.putdata(tuple(pixel[:3] for pixel in ORIENTED_OPAQUE_PIXELS))
        unoptimized = io.BytesIO()
        oriented.save(unoptimized, "PNG", compress_level=0)
        self.assertLess(len(sanitized_data), len(unoptimized.getvalue()))

    def test_png_bytes_keep_rgba_when_real_transparency_is_present(self) -> None:
        source = Image.new("RGBA", (2, 2))
        expected_pixels = (
            (10, 20, 30, 255), (40, 50, 60, 0),
            (70, 80, 90, 128), (100, 110, 120, 255),
        )
        source.putdata(expected_pixels)
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Software", "Paint.NET")
        source_buffer = io.BytesIO()
        source.save(source_buffer, "PNG", pnginfo=metadata, dpi=(37, 37))

        sanitized_data = sanitize_png_bytes(source_buffer.getvalue())

        self.assertTrue(
            FORBIDDEN_METADATA_CHUNKS.isdisjoint(set(png_chunk_types(sanitized_data)))
        )
        with Image.open(io.BytesIO(sanitized_data)) as sanitized:
            sanitized.load()
            self.assertEqual(sanitized.mode, "RGBA")
            self.assertEqual(image_pixels(sanitized), expected_pixels)
            self.assertEqual(sanitized.info, {})


if __name__ == "__main__":
    unittest.main()
