# RPG Tsukuru DS+ save-format investigation

This document records the current understanding of the battery-save layout
used by **RPG Tsukuru DS+ — Create the New World** (`VEBJ`). It is intended as
the foundation for later project export, import and editing work.

The findings below concern normal cartridge/battery saves (`.sav` and `.dsv`),
not DeSmuME save states (`.ds0`, `.dst`, and similar files).

## Reference material

The primary save examined was:

```text
G:\desmume-0.9.13-win64\Battery\RPG Tsukuru DS+ (English).dsv
```

Snapshot details at the time of analysis:

| Property | Value |
| --- | --- |
| Complete `.dsv` size | `1,048,698` bytes |
| Raw battery payload size | `1,048,576` bytes (`0x100000`) |
| Complete `.dsv` SHA-256 | `D031C2DE7E057D8816D451F408A51F0C4149CED2AC77E4812742AE13E26695A5` |
| Raw payload SHA-256 | `726FB5540354CD4A808BBC13947D53F962BE68C72AC98EB4E2C659D577465AE2` |

The clean Japanese ROM used to confirm the layout in the executable was:

```text
5968 - RPG Tsukuru DS+ - Create the New World (DSi Enhanced) (J).nds
SHA-256: D1FF98FE4FDE406B004D3C45986216F9EB67D3C765F1B2F16213677E005E216F
```

The snapshot hashes are important because the save will naturally change as
the user edits or saves a project.

## DeSmuME `.dsv` wrapper

The first `0x100000` bytes of the examined `.dsv` are the raw cartridge-save
payload. DeSmuME appends a 122-byte footer after that payload. The footer ends
with:

```text
|-DESMUME SAVE-|
```

All offsets in this document are relative to the start of the raw battery
payload, not the end of the DeSmuME footer. A raw `.sav` normally contains only
the payload. A tool that rewrites a `.dsv` must preserve or regenerate the
DeSmuME footer separately.

## Confirmed project-slot layout

The first `0xF52F0` bytes contain four created-game/project slots:

| Slot | Start | End, inclusive | Reserved size | State in reference save |
| ---: | ---: | ---: | ---: | --- |
| 1 | `0x000000` | `0x03D4BB` | `0x03D4BC` (`251,068`) | Populated |
| 2 | `0x03D4BC` | `0x07A977` | `0x03D4BC` (`251,068`) | Empty |
| 3 | `0x07A978` | `0x0B7E33` | `0x03D4BC` (`251,068`) | Empty |
| 4 | `0x0B7E34` | `0x0F52EF` | `0x03D4BC` (`251,068`) | Empty |

These boundaries were first suggested by the save's occupancy pattern and
then confirmed by constants and selection code in the decompressed ARM9.

### Two safety copies per slot

Each project slot is divided into two equal copies:

```text
Copy size: 0x01EA5E bytes (125,534 bytes)
```

For project slot 1:

| Copy | Start | End, inclusive |
| --- | ---: | ---: |
| Primary | `0x000000` | `0x01EA5D` |
| Mirror/backup | `0x01EA5E` | `0x03D4BB` |

In the reference save, these complete `0x01EA5E`-byte copies are byte-for-byte
identical. The second copy is therefore not a second created game. It is a
redundant copy of the same slot used for integrity/recovery.

The final four bytes of each slot-1 copy are:

```text
01 40 81 99
```

Interpreted as a little-endian word, this is `0x99814001`. The same value is
present in the ARM9 verification code. Until every surrounding field is fully
decoded, this should be described conservatively as an integrity
marker/checksum base rather than merely a file signature.

## Occupied data in the reference project

A scan for bytes other than erased-flash value `0xFF` found the principal
stored record at:

```text
Primary: 0x000000-0x000435
Mirror:  0x01EA5E-0x01EE93
Extent:  1,078 bytes per copy
```

There is also the fixed integrity word at the end of each reserved copy.

The 1,078-byte figure is a **physical occupied extent**, not yet a proven
logical serialized length. The format is compact and partially scrambled,
and `0xFF` may appear inside valid data. Future code must use the format's own
length and validation fields rather than trimming a project at the last
non-`0xFF` byte.

## Evidence that slot 1 is created-game data

Several independent checks identify slot 1 as the created RPG project:

1. Only slot 1 and its exact mirror are populated; the other three
   `0x03D4BC`-byte slots are erased.
2. An older workspace `.sav` and the newer `.dsv` changed in corresponding
   ranges in both copies, consistent with saving one edited project.
3. The clean ROM's ARM9 contains the exact slot and half-slot constants.
4. Running the relevant ARM9 word-decoding routine against the beginning of
   the record exposes project metadata. One visible sequence is:

   ```text
   Plnngame designscenarioPrgrtest play
   ```

   This corresponds to the project-category data previously observed in live
   RAM and is not an ordinary player's progress record.

## How the layout was found

### 1. Isolate the raw save payload

The DeSmuME footer was excluded, leaving the first `0x100000` bytes for all
save-structure comparisons.

### 2. Map occupied flash sectors

The raw payload was scanned in `0x1000`-byte sectors, counting bytes that were
not `0xFF`. Occupied regions appeared at the beginning of the save, at a
matching region near `0x01EA5E`, and in several high-address system regions.

### 3. Search for exact repeated blocks

The following equality was confirmed across the complete copy size:

```python
payload[0x000000:0x01EA5E] == payload[0x01EA5E:0x03D4BC]
```

This established that the two apparent records were redundant copies rather
than two games.

### 4. Compare save generations

The older workspace save and newer DeSmuME save differed in matching positions
inside both slot-1 copies. This further supported the mirrored-write model.
The workspace `.sav` was not treated as the authoritative current save because
it differed from the newer battery file.

### 5. Confirm boundaries in the ROM

The clean ROM's ARM9 was decompressed and searched for the inferred offsets.
The following relevant constants were found:

| ARM9 address | Value | Meaning |
| ---: | ---: | --- |
| `0x02021B80` | `0x0003D4BC` | Full project-slot size |
| `0x02021B8C` | `0x99814001` | Integrity/checksum base value |
| `0x02021B94` | `0x0001EA5E` | Redundant-copy size |
| `0x02022614` | `0x0003D4BC` | Slot 2 start |
| `0x02022618` | `0x0007A978` | Slot 3 start |
| `0x0202261C` | `0x000B7E34` | Slot 4 start |
| `0x02022620` | `0x000F52F0` | End of project slots/start of smaller records |

The switch-like routine around `0x02022658` selects save-region offsets.

### 6. Trace validation and scrambling code

The project loader around `0x02021900-0x02021B7C` attempts both redundant
copies and validates the selected record. Related routines include:

| ARM9 routine | Observed role |
| ---: | --- |
| `0x0201DA58` | Initializes the deterministic word-scrambling generator |
| `0x0201DB18` | Produces the next scrambling word |
| `0x02021948-0x02021980` | Removes the word scrambling while loading |
| `0x02021A18-0x02021A34` | Restores/applies scrambling after validation |
| `0x020226E0` | Additive byte-sum helper used by validation |

The scrambling is deterministic and is not cryptographic protection. It does,
however, mean that arbitrary edits to the raw bytes will invalidate or corrupt
the project. The exact engine-selected word range and all serialized field
boundaries still need to be documented before implementing an importer.

## Other save regions

The data beginning at `0x0F52F0` is outside the four created-game slots. ARM9
contains a table of smaller region offsets beginning with:

```text
0x0F52F0, 0x0F5C50, 0x0F65B0, 0x0F6F10,
0x0F7870, 0x0F81D0, 0x0F8B30, 0x0F9490,
0x0F9DF0, 0x0FA750, 0x0FB0B0, 0x0FBA10
```

Additional region selectors include `0x0FD37C` and `0x0FD7FC`. Some of these
areas are populated in the reference save, but they have not yet been
classified. They must not be included in an export that claims to contain only
one created RPG project.

## Requirements for a future project exporter/importer

An exporter should:

1. Read only the raw battery payload, excluding any emulator footer.
2. Let the user select one of the four project slots.
3. Validate both redundant copies.
4. Select the valid/newest copy according to the game's own rules.
5. Decode the scrambling and serialization.
6. Export one self-contained project representation with version and checksum
   metadata.

An importer must:

1. Keep a backup of the target save.
2. Confirm the target game and save size.
3. Encode the project using the game's actual serializer.
4. Recalculate every checksum/integrity field.
5. Write both redundant copies consistently.
6. Leave the other three project slots and all high-address save regions
   untouched.
7. Preserve or regenerate the DeSmuME footer when the target is a `.dsv`.

Writing only the visibly occupied 1,078 bytes, writing only one redundant
copy, or copying the entire 1 MB save would all be unsafe approaches.

## Compatibility implications

The English ROM patch has not intentionally changed this save layout. Normal
battery saves are structurally compatible with the original Japanese ROM.
Emulator save states are different: they restore CPU and RAM state from a
specific ROM build and must not be treated as portable project files.

## Unresolved work

The following points still require reverse engineering:

- authoritative logical project length;
- all serialized field types and object/table boundaries;
- exact meaning and placement of each checksum and generation field;
- how the loader chooses between non-identical redundant copies;
- classification of the regions at and above `0x0F52F0`;
- mapping project records to maps, events, database entries and embedded
  resources;
- safe re-encoding and round-trip tests against the game.

Until those items are solved, extraction can be made read-only, but project
injection should remain experimental.
