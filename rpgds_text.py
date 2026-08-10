#!/usr/bin/env python3
"""Dump and reinsert RPG Tsukuru DS overlay text.

The game stores most interface strings as null-terminated CP932 (Shift-JIS)
text inside compressed ARM9 overlays. This tool only exports strings whose
addresses are referenced by a 32-bit pointer, which avoids treating ARM code
as text.
"""

from __future__ import annotations

import argparse
import csv
import re
import struct
from pathlib import Path

import ndspy.code
import ndspy.rom


CSV_FIELDS = ("overlay", "offset", "address", "max_bytes", "original", "translation")
FORMAT_TOKEN_RE = re.compile(r"%(?:[-+ #0]*\d*(?:\.\d+)?[diouxXeEfFgGcs%])|~\d+(?:,\d+)*")
SAFE_TEXT_CONTROLS = frozenset("\n")
PACKED_STRINGS = {
    # This confirmation pair has only one base pointer; the game reaches the
    # second label by adding four bytes, so each slot must remain separate.
    "はいいいえ": ((0, "はい"), (4, "いいえ")),
}


def is_japanese(text: str) -> bool:
    return any(
        "\u3040" <= char <= "\u30ff"
        or "\u3400" <= char <= "\u9fff"
        or char in "\u3001\u3002\u3010\u3011"
        for char in text
    )


def has_unsafe_control_chars(text: str) -> bool:
    """Reject binary controls while allowing the game's embedded line breaks."""
    return any(ord(char) < 32 and char not in SAFE_TEXT_CONTROLS for char in text)


def referenced_strings_in_data(data: bytes, ram_address: int):
    """Yield pointer-referenced CP932 strings from a loaded ARM code region."""
    targets: dict[int, int] = {}
    region_end = ram_address + len(data)

    # ARM/Thumb literal pools and pointer tables are word-aligned. Some short
    # labels are packed directly beside one another, so pointer targets (not
    # only NUL bytes) have to define string starts and ends.
    for pointer_offset in range(0, len(data) - 3, 4):
        address = struct.unpack_from("<I", data, pointer_offset)[0]
        if ram_address <= address < region_end:
            target = address - ram_address
            targets[target] = targets.get(target, 0) + 1

    sorted_targets = sorted(targets)
    for index, offset in enumerate(sorted_targets):
        limit = min(len(data), offset + 512)
        nul = data.find(b"\0", offset, limit)
        next_target = sorted_targets[index + 1] if index + 1 < len(sorted_targets) else limit
        end = min(nul if nul >= 0 else limit, next_target)
        if end - offset < 2:
            continue

        raw = data[offset:end]
        try:
            text = raw.decode("cp932")
        except UnicodeDecodeError:
            continue

        if not is_japanese(text) or has_unsafe_control_chars(text):
            continue

        packed = PACKED_STRINGS.get(text)
        if packed:
            for relative_offset, packed_text in packed:
                packed_raw = packed_text.encode("cp932")
                packed_offset = offset + relative_offset
                yield (
                    packed_offset,
                    ram_address + packed_offset,
                    packed_raw,
                    packed_text,
                )
        else:
            address = ram_address + offset
            yield offset, address, raw, text


def referenced_strings(overlay: ndspy.code.Overlay):
    """Yield pointer-referenced strings from a decompressed ARM overlay."""
    yield from referenced_strings_in_data(bytes(overlay.data), overlay.ramAddress)


def dump_strings(rom_path: Path, csv_path: Path) -> int:
    rom = ndspy.rom.NintendoDSRom.fromFile(rom_path)
    overlays = rom.loadArm9Overlays()
    count = 0

    with csv_path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for overlay_id, overlay in sorted(overlays.items()):
            for offset, address, raw, original in referenced_strings(overlay):
                writer.writerow(
                    {
                        "overlay": overlay_id,
                        "offset": f"0x{offset:X}",
                        "address": f"0x{address:X}",
                        "max_bytes": len(raw),
                        "original": original,
                        "translation": "",
                    }
                )
                count += 1
    return count


def format_tokens(text: str) -> list[str]:
    return FORMAT_TOKEN_RE.findall(text)


def apply_strings(rom_path: Path, csv_path: Path, output_path: Path) -> int:
    rom = ndspy.rom.NintendoDSRom.fromFile(rom_path)
    overlays = rom.loadArm9Overlays()
    changed_overlays: set[int] = set()
    applied = 0

    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        for line_number, row in enumerate(csv.DictReader(source), start=2):
            translation = row["translation"]
            if not translation:
                continue

            overlay_id = int(row["overlay"])
            offset = int(row["offset"], 0)
            max_bytes = int(row["max_bytes"])
            original = row["original"]
            overlay = overlays[overlay_id]
            original_raw = original.encode("cp932")
            current = bytes(overlay.data[offset : offset + len(original_raw)])

            if current != original_raw:
                raise ValueError(
                    f"CSV line {line_number}: original text no longer matches "
                    f"overlay {overlay_id} at 0x{offset:X}"
                )

            translated_raw = translation.encode("cp932")
            if len(translated_raw) > max_bytes:
                raise ValueError(
                    f"CSV line {line_number}: translation needs {len(translated_raw)} bytes, "
                    f"but its fixed slot allows {max_bytes}"
                )

            if format_tokens(original) != format_tokens(translation):
                raise ValueError(
                    f"CSV line {line_number}: preserve format/control tokens exactly: "
                    f"{format_tokens(original)!r}"
                )

            replacement = translated_raw + b"\0" * (max_bytes - len(translated_raw))
            overlay.data[offset : offset + max_bytes] = replacement
            changed_overlays.add(overlay_id)
            applied += 1

    if not applied:
        raise ValueError("No non-empty translations were found in the CSV")

    for overlay_id in changed_overlays:
        overlay = overlays[overlay_id]
        rom.files[overlay.fileID] = overlay.save(compress=True)
    rom.arm9OverlayTable = ndspy.code.saveOverlayTable(overlays)
    rom.saveToFile(output_path, updateDeviceCapacity=True)
    return applied


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    dump = subparsers.add_parser("dump", help="export referenced Japanese UI strings")
    dump.add_argument("rom", type=Path)
    dump.add_argument("csv", type=Path)

    apply = subparsers.add_parser("apply", help="apply filled translation cells to a new ROM")
    apply.add_argument("rom", type=Path)
    apply.add_argument("csv", type=Path)
    apply.add_argument("output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "dump":
        count = dump_strings(args.rom, args.csv)
        print(f"Exported {count} strings to {args.csv}")
    else:
        count = apply_strings(args.rom, args.csv, args.output)
        print(f"Applied {count} translations to {args.output}")


if __name__ == "__main__":
    main()
