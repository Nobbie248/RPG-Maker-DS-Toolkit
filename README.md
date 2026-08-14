# RPG Maker DS Toolkit

An unofficial Windows development, translation, and modding toolkit for the
Japanese Nintendo DS releases of **RPG Tsukuru DS** and **RPG Tsukuru DS+**.

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
- Browses and previews all 40 BGM, 10 ambience, 10 musical-event, and 136
  sound-effect sequences using the ROM's native Nintendo DS ADPCM instruments.
- Exports MIDI or the complete SSEQ/SBNK/SWAR/SWAV sound library, and imports
  MIDI with per-track DS instrument assignment.
- Reads RPG Tsukuru DS+ `.sav` and DeSmuME `.dsv` files, validates their four
  mirrored created-game slots, and builds a one-time direct-boot ROM from the
  selected project without changing the saved toolkit project.

## Supported games

| Game | Game code | Clean ROM SHA-256 |
| --- | --- | --- |
| RPG Tsukuru DS (Japan) | `V29J` | `5E845B09DA14C8CE80D50ACCFB1EBC6A350F4A4A5CE1DB1D6CF8439416F9D7CF` |
| RPG Tsukuru DS+: Create the New World (Japan) | `VEBJ` | `D1FF98FE4FDE406B004D3C45986216F9EB67D3C765F1B2F16213677E005E216F` |

The ROM must match the appropriate clean SHA-256. A different revision,
modified dump, or damaged file should not be used.

## Using the Windows editor

1. Run `dist\RPG Maker DS Toolkit.exe`.
2. Use the compact navigation tabs for **File**, **Text**, **Graphics**,
   **Compile**, **Direct Boot**, and **Music / SFX**. The most recent valid
   project or ROM is reopened automatically.
3. Open **File**, select **Open Original ROM**, and choose a supported
   clean Japanese ROM when starting new work.
4. Open **Text** and use **Quick Auto** to apply the built-in interface glossary.
5. Choose a Google Translate target language, then optionally use **Auto
   Translate** or **Auto Translate + Shorten** for untranslated text.
6. Review the results in the **Text** workspace and correct them in game context.
7. Use **Graphics** to export, edit, and re-import graphical assets. It exposes
   tiled `CHBG` atlases and fixed-size `BMBG` bitmaps such as `edit/ep67.blz`.
8. Use **Music / SFX** to preview native tracks, export or import MIDI, assign
   DS instruments, or extract the complete sound-bank library.
9. Return to **File** and select **Save Toolkit Project** to preserve
   the translation work.
10. Open **Compile ROM** to create a separate test ROM.

For DS+, **Build Direct-Boot ROM from Save** reads the four created-game slots
from a raw `.sav` or DeSmuME `.dsv`. Only populated slots with matching redundant
safety copies and valid integrity markers can be selected. The selected slot is
used for that build only: it is not attached to or saved inside the loaded
`.rpgdsproj`, and the normal **Compile ROM** button continues producing an
ordinary translation ROM. The one-time build adds the slot to NitroFS as
`embedded/project-slot.bin` and enables a verified cold-boot installer. On a
blank save, the ROM copies that data into project slot 1 using the game's native
save routines and rescans it; an existing valid slot 1 is never overwritten.
The direct-boot ROM displays none of the game's boot logos, title screen, main
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
- imported replacement SSEQ music generated from MIDI;
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

- Python 3.12 or a compatible Python 3 release
- Tkinter (included with standard Windows Python; on Linux, install your
  distribution's Tk package, such as `python3-tk`)

### Windows

From PowerShell in the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

The Windows build script installs the required packages, removes its temporary
PyInstaller files, and replaces the existing application at:

```text
dist\RPG Maker DS Toolkit.exe
```

### Linux

Install Python, venv, and Tk using your distribution's package manager first.
For Debian or Ubuntu:

```bash
sudo apt install python3 python3-venv python3-tk
```

Then, from a terminal in the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name "RPG Maker DS Toolkit" --distpath dist --workpath build rpgds_gui.py
rm -rf build "RPG Maker DS Toolkit.spec"
```

The native Linux executable is created at:

```text
dist/RPG Maker DS Toolkit
```

PyInstaller produces an executable for the operating system on which it runs;
the Linux command does not produce a Windows `.exe`.

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
