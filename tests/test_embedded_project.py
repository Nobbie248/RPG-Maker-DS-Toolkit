import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

import ndspy.codeCompression
import ndspy.rom

from rpgds_core import (
    DESMUME_SAVE_FOOTER_MAGIC,
    DS_PLUS_PROJECT_COPY_SIZE,
    DS_PLUS_PROJECT_INTEGRITY_WORD,
    DS_PLUS_PROJECT_SLOT_SIZE,
    DS_PLUS_SAVE_PAYLOAD_SIZE,
    EmbeddedProject,
    DS_PLUS_DIRECT_BOOT_CODE,
    DS_PLUS_DIRECT_BOOT_CODE_ADDRESS,
    DS_PLUS_DIRECT_BOOT_BACKDROP_PATHS,
    DS_PLUS_DIRECT_BOOT_HEADLESS_CODE,
    DS_PLUS_DIRECT_BOOT_HEADLESS_CODE_ADDRESS,
    DS_PLUS_DIRECT_BOOT_TRAMPOLINE,
    DS_PLUS_DIRECT_BOOT_TRAMPOLINE_ADDRESS,
    DS_PLUS_DIRECT_BOOT_OVERLAY,
    _set_embedded_project_file,
    apply_ds_plus_direct_boot,
    decode_chbg,
    embedded_project_from_slot,
    load_project,
    save_project,
    scan_dsplus_project_slots,
)


def valid_slot(seed=b"PROJECT") -> bytes:
    copy = bytearray(b"\xFF" * DS_PLUS_PROJECT_COPY_SIZE)
    copy[:len(seed)] = seed
    struct.pack_into("<I", copy, len(copy) - 4, DS_PLUS_PROJECT_INTEGRITY_WORD)
    return bytes(copy + copy)


class EmbeddedProjectTests(unittest.TestCase):
    def test_scan_raw_save_lists_one_ready_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "game.sav"
            payload = valid_slot() + b"\xFF" * (
                DS_PLUS_SAVE_PAYLOAD_SIZE - DS_PLUS_PROJECT_SLOT_SIZE
            )
            path.write_bytes(payload)

            slots = scan_dsplus_project_slots(path)

            self.assertEqual(len(slots), 4)
            self.assertTrue(slots[0].embeddable)
            self.assertEqual(slots[0].status, "Ready to embed")
            self.assertFalse(slots[1].populated)
            self.assertEqual(slots[1].status, "Empty")

    def test_scan_desmume_dsv_ignores_footer(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "game.dsv"
            payload = valid_slot() + b"\xFF" * (
                DS_PLUS_SAVE_PAYLOAD_SIZE - DS_PLUS_PROJECT_SLOT_SIZE
            )
            path.write_bytes(payload + b"footer metadata" + DESMUME_SAVE_FOOTER_MAGIC)

            slots = scan_dsplus_project_slots(path)

            self.assertTrue(slots[0].embeddable)

    def test_mismatched_safety_copies_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "game.sav"
            slot = bytearray(valid_slot())
            slot[DS_PLUS_PROJECT_COPY_SIZE + 3] ^= 1
            payload = bytes(slot) + b"\xFF" * (
                DS_PLUS_SAVE_PAYLOAD_SIZE - DS_PLUS_PROJECT_SLOT_SIZE
            )
            path.write_bytes(payload)

            candidate = scan_dsplus_project_slots(path)[0]

            self.assertFalse(candidate.embeddable)
            self.assertIn("copies differ", candidate.status)
            with self.assertRaisesRegex(ValueError, "cannot be embedded"):
                embedded_project_from_slot(candidate, path.name)

    def test_project_archive_round_trips_embedded_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.nds"
            source.write_bytes(b"test source")
            project_path = root / "test.rpgdsproj"
            embedded = EmbeddedProject(3, valid_slot(b"SLOT THREE"), "save.dsv")

            save_project(project_path, source, [], {}, embedded)
            loaded_source, rows, images, loaded = load_project(project_path)

            self.assertEqual(loaded_source, source)
            self.assertEqual(rows, {})
            self.assertEqual(images, {})
            self.assertEqual(loaded, embedded)
            with zipfile.ZipFile(project_path) as archive:
                metadata = json.loads(archive.read("project.json"))
                self.assertEqual(metadata["version"], 2)
                self.assertEqual(metadata["embedded_project"]["source_slot"], 3)
                self.assertEqual(
                    archive.read("embedded/project-slot.bin"), embedded.data,
                )

    def test_old_project_without_embedded_member_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project_path = root / "old.rpgdsproj"
            with zipfile.ZipFile(project_path, "w") as archive:
                archive.writestr("project.json", json.dumps({
                    "version": 1,
                    "source_rom": str(root / "source.nds"),
                    "images": {},
                }))
                archive.writestr(
                    "translations.csv",
                    "overlay,offset,address,max_bytes,original,translation,auto\n",
                )

            _source, _rows, _images, embedded = load_project(project_path)

            self.assertIsNone(embedded)

    def test_direct_boot_patch_is_scoped_and_binary_verified(self):
        source = Path(__file__).parents[1] / (
            "5968 - RPG Tsukuru DS+ - Create the New World (DSi Enhanced) (J).nds"
        )
        if not source.exists():
            self.skipTest("clean DS+ regression ROM is not available")
        rom = ndspy.rom.NintendoDSRom.fromFile(source)
        slot = valid_slot(b"DIRECT BOOT")
        _set_embedded_project_file(rom, slot)

        original_backdrops = {
            path: bytes(rom.getFileByName(path))
            for path in DS_PLUS_DIRECT_BOOT_BACKDROP_PATHS
        }

        apply_ds_plus_direct_boot(rom)

        for path, original_backdrop in original_backdrops.items():
            with self.subTest(backdrop=path):
                black_backdrop = bytes(rom.getFileByName(path))
                self.assertEqual(len(black_backdrop), len(original_backdrop))
                self.assertEqual(black_backdrop[:16], original_backdrop[:16])
                black_image = decode_chbg(black_backdrop, compressed=False)
                self.assertEqual(black_image.getbbox(), None)

        arm9 = ndspy.codeCompression.decompress(bytes(rom.arm9))
        base = rom.arm9RamAddress
        # The title-sequence call is replaced, while the title and main-menu
        # input loops remain completely unmodified.  The selector branches
        # into its original project-activation path before drawing its UI.
        self.assertEqual(
            struct.unpack_from("<I", arm9, 0x02072D4C - base)[0], 0xEB00FFB8,
        )
        self.assertEqual(
            struct.unpack_from("<I", arm9, 0x02011904 - base)[0], 0xEB018CFC,
        )
        # The selector enters its cleanup/blank display state rather than the
        # visible city-background state.
        self.assertEqual(
            struct.unpack_from("<I", arm9, 0x020735E8 - base)[0], 0xE3A01004,
        )
        self.assertEqual(
            struct.unpack_from("<I", arm9, 0x020735EC - base)[0], 0xEB000150,
        )
        self.assertEqual(
            struct.unpack_from("<I", arm9, 0x02073618 - base)[0], 0xEB0005AF,
        )
        self.assertEqual(
            struct.unpack_from("<I", arm9, 0x02073650 - base)[0], 0xEB000597,
        )
        for address, clean_word in (
            (0x0207435C, 0xEBFEB620),
            (0x02074D70, 0xEBFE60D4),
            (0x02074D74, 0xE1A04000),
            (0x02075334, 0xE3E0B000),
            (0x02075338, 0xEBFE5F62),
        ):
            self.assertEqual(struct.unpack_from("<I", arm9, address - base)[0], clean_word)
        # The complete original selector constructor remains intact because it
        # also initializes loader state; the hardware black mask hides it.
        for address, clean_word in (
            (0x02054D7C, 0xEBFEDE80),
            (0x02054D80, 0xE3500000),
            (0x02054D84, 0x0A000000),
            (0x02054D88, 0xEBFEF648),
            (0x02054D8C, 0xE3A01001),
        ):
            self.assertEqual(struct.unpack_from("<I", arm9, address - base)[0], clean_word)
        for address, patched_word in (
            (0x02055238, 0xE3A05000),
            (0x0205523C, 0xE3A06000),
            (0x02055240, 0xE3A07000),
            (0x02055244, 0xE58A7080),
            (0x02055248, 0xEA0000D8),
        ):
            self.assertEqual(struct.unpack_from("<I", arm9, address - base)[0], patched_word)
        # The former late bypass is gone: the picker input poll remains clean
        # and is unreachable on direct boot.
        self.assertEqual(
            struct.unpack_from("<I", arm9, 0x020553A4 - base)[0], 0xE3E09000,
        )
        self.assertEqual(
            struct.unpack_from("<I", arm9, 0x020553A8 - base)[0], 0xEBFEDF46,
        )
        overlays = rom.loadArm9Overlays()
        overlay = overlays[DS_PLUS_DIRECT_BOOT_OVERLAY]
        code_offset = DS_PLUS_DIRECT_BOOT_CODE_ADDRESS - base
        self.assertEqual(
            bytes(arm9[code_offset:code_offset + len(DS_PLUS_DIRECT_BOOT_CODE)]),
            DS_PLUS_DIRECT_BOOT_CODE,
        )
        trampoline_offset = DS_PLUS_DIRECT_BOOT_TRAMPOLINE_ADDRESS - base
        self.assertEqual(
            bytes(arm9[
                trampoline_offset:trampoline_offset + len(DS_PLUS_DIRECT_BOOT_TRAMPOLINE)
            ]),
            DS_PLUS_DIRECT_BOOT_TRAMPOLINE,
        )
        headless_offset = DS_PLUS_DIRECT_BOOT_HEADLESS_CODE_ADDRESS - base
        self.assertEqual(
            bytes(arm9[
                headless_offset:headless_offset + len(DS_PLUS_DIRECT_BOOT_HEADLESS_CODE)
            ]),
            DS_PLUS_DIRECT_BOOT_HEADLESS_CODE,
        )
        self.assertEqual(overlay.ramSize, 0x57FC0)
        self.assertEqual(bytes(rom.getFileByName("embedded/project-slot.bin")), slot)

    def test_direct_boot_refuses_double_patching(self):
        source = Path(__file__).parents[1] / (
            "5968 - RPG Tsukuru DS+ - Create the New World (DSi Enhanced) (J).nds"
        )
        if not source.exists():
            self.skipTest("clean DS+ regression ROM is not available")
        rom = ndspy.rom.NintendoDSRom.fromFile(source)
        _set_embedded_project_file(rom, valid_slot())
        apply_ds_plus_direct_boot(rom)

        with self.assertRaisesRegex(ValueError, "refusing to patch an unknown ROM"):
            apply_ds_plus_direct_boot(rom)


if __name__ == "__main__":
    unittest.main()
