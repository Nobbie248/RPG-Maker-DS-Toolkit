# RPG Maker DS Translator

English translation project and Windows editing toolkit for the Japanese
Nintendo DS releases of **RPG Tsukuru DS** and **RPG Tsukuru DS+**.

The current project focuses on **RPG Tsukuru DS+: Create the New World**. It
translates the editor interface, help text, generated event text, menus, and
graphical interface assets while preserving the original game and save-data
formats.

> **Release status:** Active development and testing. This repository does not
> currently provide a public translation patch or any Nintendo ROM data.

## Main features

- Extracts and edits thousands of CP932 text records from ARM9 and overlays.
- Reuses verified text storage and safely relocates longer translations within
  the same loaded code unit.
- Preserves runtime formatting tokens and parser-critical metadata.
- Exports, previews, and imports the games' CHBG graphical assets as PNGs.
- Preserves palette roles and tile IDs used by highlighted or animated UI.
- Reuses genuinely unused palette entries for suitable static artwork.
- Removes PNG metadata and validates dimensions, palette conversion, tile
  capacity, and decoded image size before compiling.
- Provides automatic glossary translation, online translation, abbreviation,
  and sentence-shortening tools.
- Saves text and replacement artwork together in a portable `.rpgdsproj`
  project archive.
- Reads RPG Tsukuru DS+ `.sav` and DeSmuME `.dsv` files, validates their four
  mirrored created-game slots, and attaches one selected project to the archive.
- Rebuilds a separate test ROM without modifying the clean source ROM.

## Supported games

| Game | Game code | Clean ROM SHA-256 |
| --- | --- | --- |
| RPG Tsukuru DS (Japan) | `V29J` | `5E845B09DA14C8CE80D50ACCFB1EBC6A350F4A4A5CE1DB1D6CF8439416F9D7CF` |
| RPG Tsukuru DS+: Create the New World (Japan) | `VEBJ` | `D1FF98FE4FDE406B004D3C45986216F9EB67D3C765F1B2F16213677E005E216F` |

The ROM must match the appropriate clean SHA-256. A different revision,
modified dump, or damaged file should not be used.

## Using the Windows editor

1. Run `dist\RPGDS Translator Build.exe`.
2. Select **Open ROM** and choose a supported clean Japanese ROM.
3. Use **Quick Auto** to apply the built-in interface glossary.
4. Optionally use **Auto Translate + Shorten (Online)** for untranslated text.
5. Review the results in the **Text** tab and correct them in game context.
6. Use the **Images** tab to export, edit, and re-import graphical assets. It exposes
   tiled `CHBG` atlases and fixed-size `BMBG` bitmaps such as `edit/ep67.blz`.
7. Select **Save Project** to preserve the translation work.
8. Select **Compile ROM** to create a separate test ROM.

For DS+, **Embed Project from Save** reads the four created-game slots from a
raw `.sav` or DeSmuME `.dsv`. Only populated slots with matching redundant
safety copies and valid integrity markers can be selected. Save the translation
project afterward to preserve the embedded slot. Compilation adds it to NitroFS
as `embedded/project-slot.bin` and enables a verified cold-boot installer. On a
blank save, the ROM copies that data into project slot 1 using the game's native
save routines and rescans it; an existing valid slot 1 is never overwritten.
The compiled ROM displays none of the game's boot logos, title screen, main
menu, or project picker. It enters the original **Play Game / slot 1** path and
launches the embedded game automatically from a cold boot.

The online translator uses Google Translate's public web endpoint. It requires
internet access and may change or become unavailable. Automatically translated
text should always be reviewed before a public release.

Imported `BMBG` replacements retain their original dimensions, bit depth,
palette relationship, and decoded byte size.

## Project file contents

`RPG Tsukuru DS+ English.rpgdsproj` is a compressed project archive. It stores:

- every extracted text record and its current English translation;
- imported replacement PNG artwork;
- an optional validated DS+ created-game slot selected from a save;
- the expected source-ROM path and SHA-256; and
- metadata linking each replacement to its internal ROM asset.

It does **not** contain the Japanese ROM, a compiled English ROM, DeSmuME save
files, save states, or untouched original graphics. The clean source ROM is
still required when opening and compiling the project.

## Translation and image safeguards

- Longer English text is relocated only within verified string storage in the
  same ARM9 or overlay load unit. Overlay RAM sizes, BSS addresses, executable
  instructions, and save-data structures are not expanded.
- Strings reached through stable layouts or computed offsets are protected from
  unsafe relocation.
- Runtime tokens such as `%s`, `%d`, `%02d`, and `~4,10` are preserved.
- Parser-critical suffixes such as `:NNN` asset identifiers are restored and
  validated during compilation.
- Special generated-event messages use the game's required full-width CP932
  representation instead of unsafe raw ASCII.
- Imported images must retain their exact original pixel dimensions.
- EXIF, XMP, IPTC, DPI, ICC, software tags, and redundant alpha data are
  removed before image validation.
- Existing palette indices and tile IDs are retained where the game may animate
  or recolor them.
- CHBG images may use at most 150% of their original decoded size. Imports over
  that limit are rejected with required and available tile counts.

PNG file size and compressed `.blz` size may differ from the original. The
decoded CHBG allocation is the relevant safety limit for game memory.

## Current project scope

- RPG Tsukuru DS profile: 5,198 exposed text records and 660 CHBG graphics.
- RPG Tsukuru DS+ profile: 4,137 exposed text records and 841 CHBG graphics.
- The DS+ project embeds its current translated text and replacement artwork in
  `RPG Tsukuru DS+ English.rpgdsproj`.

The translation remains a work in progress. It requires continued testing of
editor workflows, sample-game generation, event templates, save/load behavior,
highlighted graphics, and less frequently used menus.

## Build from source

Requirements:

- Windows 10 or later
- Python 3.12 or a compatible Python 3 release

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

The build script creates a local virtual environment, installs the dependencies,
and replaces:

```text
dist\RPGDS Translator Build.exe
```

The command-line `rpgds_text.py` extractor is also available for CSV-based
workflows.

## Public patch distribution

When the translation is ready, distribute an **Xdelta patch**, not a patched
`.nds` file. A release should include:

- the patch file;
- a plain-text README with application instructions;
- the exact clean source-ROM filename, size, SHA-256, and CRC32;
- the expected patched-ROM hashes;
- version number and changelog;
- credits and testing information; and
- native-resolution screenshots.

Users must supply their own legally obtained Japanese ROM dump. Do not upload or
redistribute Nintendo's ROM data.

## Credits and disclosure

Project maintained by **Nobbie248**.

The project uses machine-assisted translation and AI-assisted development.
Automated translations are treated as drafts and are being reviewed and tested
in game.

RPG Maker, RPG Tsukuru DS, and RPG Tsukuru DS+ are properties of their
respective copyright holders. This is an unofficial fan project and is not
affiliated with or endorsed by Nintendo or Enterbrain.
