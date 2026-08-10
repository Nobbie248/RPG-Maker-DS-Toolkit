# RPG Maker DS Translator

A Windows editor for translating both **RPG Tsukuru DS (Japan), game code
V29J**, and **RPG Tsukuru DS+ (Japan), game code VEBJ**. It keeps replacements
inside the original text storage for each loaded code unit, previews the games'
CHBG graphics, and rebuilds a testable `.nds` ROM.

## Use the Windows app

1. Run `dist\RPGDS Translator Build.exe` and choose **Open ROM**.
2. Use **Quick Auto** for the built-in UI glossary. It is also applied when a
   ROM opens.
3. Use **Auto Translate + Shorten (Online)** to machine-translate the remaining
   strings. The app automatically tries compact UI phrases, common
   abbreviations, action/key-phrase extraction, filler-word removal, and
   target-sized RPG label codes. Only CP932 text that fits the original byte
   allowance is accepted, and negative wording and runtime tokens are kept.
4. Review or edit translations in the **Text** tab. The byte meter turns red
   when a replacement is unsafe, and formatting tokens must remain unchanged.
5. In **Images**, select an asset, export its PNG, edit it without changing its
   pixel dimensions, then import it. DS palettes are applied automatically.
6. Save a `.rpgdsproj` project whenever you want, then choose **Compile ROM**.

The app recognizes the analyzed ROM by game code and SHA-256:

- RPG Tsukuru DS / V29J:
  `5E845B09DA14C8CE80D50ACCFB1EBC6A350F4A4A5CE1DB1D6CF8439416F9D7CF`
- RPG Tsukuru DS+ / VEBJ:
  `D1FF98FE4FDE406B004D3C45986216F9EB67D3C765F1B2F16213677E005E216F`

It never edits either source file; compilation always writes a separate ROM.

## Translation constraints

- English that exceeds its original `max_bytes` allowance is repacked into
  verified string slots in the same ARM9/overlay load unit. Overlay sizes, BSS
  addresses, and save-data structures remain unchanged.
- A few packed strings reached through computed offsets cannot be relocated;
  the compiler reports these clearly and keeps their original byte limit.
- Runtime tokens such as `%s`, `%d`, `%02d`, and `~4,10` are preserved.
- The online option uses Google Translate's public web endpoint and therefore
  requires internet access and can change or become unavailable.
- Imported graphics must retain their original width and height. The importer
  applies EXIF orientation, discards EXIF/XMP/IPTC/DPI/ICC/software metadata,
  removes an unused fully opaque alpha channel, and normalizes pixels to an
  8-bit working image before validation. This PNG metadata is never written to
  the ROM or embedded project image. The importer converts the cleaned pixels
  to the asset's original palette, compacts identical 8x8 tiles, and allows at
  most 50% more decompressed CHBG data than the original asset.
  It preserves existing tile IDs where possible and rejects imports above the
  150% hard limit with exact required/available tile counts. PNG and compressed
  `.blz` file sizes may vary; the decoded CHBG size is the enforced safety
  budget because that is the data loaded by the game.
- The original profile exposes 5,198 text records and 660 CHBG graphics. The
  DS+ profile exposes 4,137 text records and 841 CHBG graphics. Sample-game
  record formats are not modified.

Auto-translated text should be reviewed in context. For public distribution,
release a patch rather than Nintendo's ROM data.

## Build from source

Run `build_exe.ps1`. It creates a local virtual environment, installs the
dependencies, and builds `dist\RPGDS Translator Build.exe` with PyInstaller.

The command-line `rpgds_text.py` extractor remains available for CSV workflows.
