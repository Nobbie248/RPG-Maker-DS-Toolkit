# ROM-Embedded Project and Direct-Boot Investigation

## Goal

Build an RPG Tsukuru DS+ ROM that contains one authored project and launches that
project as a game immediately after a cold boot, without requiring the player to
visit the editor or choose **Play Game** from the title menu.

## Conclusion

This is implemented and cold-boot verified. The implementation does not invent
a new project format: it embeds one complete, already-valid project slot and
passes that data through the game's existing validation and deserialization
code.

Two changes are required:

1. Make the embedded project appear as a valid project in slot 1.
2. After normal hardware, filesystem, and save-manager initialization, suppress
   the title/menu input waits and enter **Play Game / slot 1** automatically.

The tested boot reaches the embedded game's own first notice in under one
second in DeSmuME. Frame-by-frame capture showed no publisher logo, game title,
main menu, or project-selection screen.

Merely adding a file to NitroFS is not enough: the original executable reads
projects from cartridge save memory, so a small ARM9 boot/storage patch is also
required.

## What is already in the ROM

The ROM has a `sample/` directory, but it does not contain a complete playable
sample project. It contains editor defaults, including:

- `sample/hero.bin`, `job.bin`, `weapon.bin`, `protector.bin`, `item.bin`,
  `ability.bin`, and `monster.bin`
- `sample/field1.bin` through `field6.bin`
- `sample/town1.bin` through `town5.bin`
- `sample/dungeon1.bin` through `dungeon6.bin`
- default fonts and menu parts under `sample/graphic/`

Overlay 6 loads the database defaults and overlay 7 loads the field/town/dungeon
defaults. No complete ROM-backed project package or existing direct-game boot
mode was found.

## Existing project loader

The original loader is useful for the proposed feature:

- ARM9 `0x0207435C` invokes `0x02021BE4` during title/menu initialization.
- `0x02021BE4` scans the save records and validates the four project slots.
- For project slots 1-4 it calls `0x020218CC`.
- `0x020218CC` validates the two redundant safety copies, removes the save
  scrambling, checks the stored size and checksum, and records the usable copy.
- The low-level save read routine is ARM9 `0x0201DE58` with the effective
  interface `(save_offset, destination, byte_count)`.
- ARM9 `0x02021D60` reads later slices of a validated record through the same
  storage path.

The title/menu state machine is in the ARM9 area around `0x02075F8C`. It contains
the existing project/game selection and test-play transitions. A direct-boot
patch must enter the same high-level play handler after initialization rather
than jumping directly into an overlay or map routine.

## Project slot payload to embed

The safest payload is the complete physical slot 1 range:

| Item | Offset/size |
|---|---:|
| Slot 1 start | `0x000000` |
| Slot 1 size | `0x3D4BC` (251,068 bytes) |
| Primary safety copy | `0x000000`, size `0x1EA5E` |
| Mirror safety copy | `0x01EA5E`, size `0x1EA5E` |

Embedding both copies means the existing validator can operate unchanged. A
future optimized version could embed one logical copy and emulate both reads,
but that creates more custom code and has little practical benefit.

The current test project slot is sparse: only 2,148 bytes differ from `0xFF`.
Its full 251,068-byte slot compresses to roughly 1-3 KiB with general-purpose
compression. A large real project may use much more of the slot, so the compile
tool must budget for the full uncompressed maximum.

The current English ROM is 21,199,208 bytes while the original cartridge image
is 33,554,432 bytes. A raw 251,068-byte project therefore fits comfortably in
ROM capacity. It does not need to remain resident in RAM.

## Recommended implementation: first-boot project installer

This is the least invasive and most compatible design.

1. Add `embedded/project-slot.bin` to NitroFS when compiling the ROM.
2. During boot, before the normal slot scan, check a version marker or determine
   whether project slot 1 is empty.
3. Stream the embedded slot into project slot 1 using the game's existing save
   write facilities. Streaming avoids a permanent 251 KiB allocation.
4. Run the original `0x02021BE4` scan and original deserializer.
5. Select slot 1 and enter the original Play Game path automatically.
6. On later boots, do not reinstall unless the embedded project version changed.

Advantages:

- Reuses all original validation, checksum, descrambling, and loading code.
- The runtime behaves as if the project had been created normally.
- Minimal ongoing RAM cost.
- Normal player/game progress can remain in its original save records.
- The editor can remain available behind an optional key combination for
  debugging or advanced users.

Trade-off: the first boot writes the embedded project to the cartridge save. A
clean save device is therefore still required, although the user does not need
to supply or install a separate save file.

## Alternative: read-only ROM-backed virtual project slot

For a cartridge-like standalone game that must never copy the project to save
memory, the storage layer can virtualize slot 1:

1. Load or stream `embedded/project-slot.bin` from NitroFS.
2. Intercept project-slot reads and return bytes from the ROM asset for offsets
   in `0x000000..0x03D4BB`.
3. Leave reads and writes outside that range directed to normal save memory.
4. Mark slot 1 read-only, or redirect attempted project edits to another slot.
5. Auto-enter the original Play Game handler.

This produces a truly ROM-authoritative project and prevents user save
corruption from changing the distributed game, but it is more invasive. The
hook must cover initial validation and all subsequent partial reads, and it must
not interfere with player-progress records elsewhere in the 1 MiB save.

## What must remain separate

The authored RPG project and the player's progress inside that RPG are different
records. The direct-boot feature should virtualize or install only project slot
1 (`0x000000..0x03D4BB`). It must not replace the full 1 MiB save image on every
boot, because that would erase settings, other project slots, and played-game
progress.

The project slot must retain:

- both safety copies, unless the read hook deliberately emulates them;
- the original scrambled representation;
- the stored length/checksum fields;
- the final fixed word and all expected padding.

## Boot-flow rule

Do not patch the reset vector to jump straight into gameplay. The game must first
initialize NitroFS, save hardware, heaps, graphics, overlays, and global project
state. The patch should run after the normal title initialization and call or
branch into the same handler used by **Play Game**, with slot 1 already selected.

For development, a boot override such as holding **Select** should bypass direct
boot and show the normal title/editor interface. This prevents a bad embedded
project from making the ROM impossible to inspect.

## Packaging caveat

The Japanese source is DSi-enhanced and contains TWL/integrity regions after the
ordinary NitroFS data. The current generic `ndspy` rebuild path does not preserve
all of that tail. A production direct-boot builder should either:

- preserve/rebuild the complete DSi layout; or
- explicitly document that the output is an NTR-mode/emulator-oriented build.

This issue is independent of the embedded project design, but it should be fixed
before treating the result as a hardware-quality release.

## Proposed implementation milestones

1. Add a project-slot extractor that accepts `.sav`/`.dsv` and exports exactly
   `0x3D4BC` bytes from one selected project slot.
2. Add structural validation for both copies before allowing compilation.
3. Insert the slot as a new NitroFS asset and report its SHA-256 in the build log.
4. Implement the first-boot installer and a persistent version marker.
5. Patch the post-initialization title flow to auto-launch Play Game slot 1.
6. Add a **Select-held** normal-title escape path.
7. Test all of the following:
   - cold boot with a completely blank save;
   - second boot without reinstalling the project;
   - player progress save and reload;
   - reset and return to direct boot;
   - corrupted project copy and mirror recovery;
   - normal title override;
   - no changes outside slot 1 during installation;
   - emulator and physical NDS/DSi-mode behavior as applicable.

## Feasibility rating

**High, with a moderate ARM9 patch.** Storage capacity and ordinary RAM are not
the limiting factors. The main work is safely joining the existing save loader
to a ROM asset and entering the existing Play Game state only after the normal
boot initialization is complete.
