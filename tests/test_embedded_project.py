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
    DS_PLUS_DIRECT_BOOT_OVERLAY,
    _set_embedded_project_file,
    apply_ds_plus_direct_boot,
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

        apply_ds_plus_direct_boot(rom)

        arm9 = ndspy.codeCompression.decompress(bytes(rom.arm9))
        base = rom.arm9RamAddress
        self.assertEqual(
            struct.unpack_from("<I", arm9, 0x0207435C - base)[0], 0xEB00FA10,
        )
        overlays = rom.loadArm9Overlays()
        overlay = overlays[DS_PLUS_DIRECT_BOOT_OVERLAY]
        code_offset = DS_PLUS_DIRECT_BOOT_CODE_ADDRESS - base
        self.assertEqual(
            bytes(arm9[code_offset:code_offset + len(DS_PLUS_DIRECT_BOOT_CODE)]),
            DS_PLUS_DIRECT_BOOT_CODE,
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
