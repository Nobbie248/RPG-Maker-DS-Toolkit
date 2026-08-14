# RPG Tsukuru DS+ audio format and toolkit workflow

## Summary

RPG Tsukuru DS+ stores its main audio library in the standard Nintendo DS
Sound Archive format:

```text
NitroFS: sound/sound_data.sdat
Size in the clean VEBJ ROM: 7,726,208 bytes
```

This is sequenced audio. The ROM does not contain streamed `STRM` songs and a
WAV, MP3 or OGG file cannot be substituted directly for a music entry.

## SDAT contents

The archive has the normal four blocks:

| Block | Role |
| --- | --- |
| `SYMB` | Human-readable resource names |
| `INFO` | Sequence, bank, player and volume metadata |
| `FAT ` | Locations of embedded sound resources |
| `FILE` | SSEQ, SSAR, SBNK and SWAR payloads |

The clean DS+ archive contains 60 top-level SSEQ files (40 BGM, 10 BGS and 10
ME), 136 SSAR sound effects, 196 SBNK instrument banks, 196 SWAR archives and
773 SWAV samples. It has four players and no streamed audio. Player limits are
six simultaneous effects, one BGM, one background sound and one musical event.

## Playback chain

```text
RPG project numeric audio ID
        -> SSEQ / SSAR notes and controllers
        -> SBNK instrument/key regions
        -> SWAR wave archive
        -> SWAV Nintendo DS ADPCM sample
        -> Nintendo DS sound channels
```

The entries map one-to-one to identically numbered banks and wave archives:

```text
bgm01..bgm40 -> BANK_BGM01..40 -> WAVE_BGM01..40
BGS01..BGS10 -> BANK_BGS01..10 -> WAVE_BGS01..10
ME01..ME10   -> BANK_ME01..10   -> WAVE_ME01..10
```

The 136 effects use banks/waves 60 through 195. Each SSAR holds one sequence
(`se001` through `se136`) and uses the six-voice SE player.

## Sequence and sample formats

SSEQ resembles MIDI but is not MIDI. DS+ uses notes, rests, instrument changes,
tempo, volume, pan, expression, portamento, vibrato, calls, returns, jumps and
multiple tracks. Jumps implement most song loops.

All 773 samples use Nintendo DS IMA-style ADPCM. Rates range from 17,000 Hz to
44,100 Hz and 457 samples contain loop metadata. SBNK definitions select a
SWAR/SWAV, root pitch, envelope and pan. The unrelated loose
`edit/shutter_sound_32730.wav` is standard mono 16-bit PCM at 32,730 Hz and is
about 0.249 seconds long.

## Toolkit workflow

The toolkit converts SSEQ into Standard MIDI for editing. MIDI import converts
up to 16 tracks back to SSEQ and asks which existing instrument slot in the
selected track's SBNK each MIDI track should use. The import setup also exposes
per-track volume, the MIDI's detected tempo, and editable loop start/end times.
Changing the tempo overrides the MIDI tempo events in the generated SSEQ. Loop
times are converted through the MIDI tempo map and written as genuine backward
SSEQ jump commands on every generated track, so the compiled ROM uses the same
intro-once/loop-region behavior as native DS music. MIDI cannot represent every
Nintendo-specific event, so other complex controller behavior may still be
simplified by a round trip.

The MIDI setup window can render the current choices before committing them.
**Preview Full MIDI** repeats the complete imported file, while **Preview Loop**
plays the intro once and then repeats only the chosen loop region until the user
presses **Stop**. Native
BGM/BGS/ME preview detects and displays the original SSEQ jump boundary and
uses it the same way. A track without an embedded loop repeats in full.
Sound-effect previews remain one-shot.

For an SSAR sound effect, **Import WAV** follows the sequence's instrument and
note to its exact SBNK -> SWAR -> SWAV target. DS+ has one unique sample target
for each of its 136 effects, so replacement does not change another effect.
The importer accepts uncompressed 8/16/24/32-bit PCM WAV, mixes it to mono,
caps the rate at 32,768 Hz, peak-normalizes it, and encodes it as Nintendo DS
IMA ADPCM. Effects are limited to 15 seconds to avoid unreasonable SDAT and RAM
growth. Replacement SWAV data is stored inside the toolkit project and applied
only when previewing or compiling; the original ROM is never modified.

Top-level BGM, BGS and ME preview uses the established **in_ncsf SSEQ Player**
core rather than the toolkit's original approximate Python synthesizer. The
portable renderer loads the current SDAT, SSEQ, SBNK and SWAR/SWAV data and
recreates the FeOS/Nitro channel engine, including DS envelopes, channel
allocation, pitch/modulation state, PSG/noise channels, looping and hardware
volume/pan behavior. It renders stereo PCM directly to the audio device without
creating a user-visible WAV file. Unsaved MIDI replacements are inserted into a
temporary private SDAT before rendering, so preview does not modify the source
ROM or toolkit project.

For preview, the toolkit converts an SSAR's single sequence into a temporary
top-level SSEQ entry while preserving that effect's bank, player, volume and
controller metadata. This lets in_ncsf render effects through the same accurate
engine without modifying the loaded ROM, archive, or saved toolkit project.

Pitch is derived from the SWAV `time` value and the 16,756,991 Hz DS sound
clock, matching the timer programmed by Nitro's driver; the rounded SWAV
`sampleRate` field is not used for tuning. ADPCM loop points are stored in
32-bit words and include the predictor/index header, so their decoded position
is `loopOffset * 8 - 7` samples. Treating the offset as plain PCM introduces a
click/roughness at every loop and is especially audible in sustained voices.

`Extract Complete Sound Library` creates:

```text
manifest.json
sequences/          SSEQ sequence files
banks/              SBNK instrument banks
wave_archives/      SWAR sample archives
samples/<archive>/  original SWAV plus decoded WAV files
```

The default local extraction is `audio_workspace/`. It is ignored by Git
because it contains copyrighted ROM samples. Imported music is instead stored
as replacement SSEQ data under `audio/` inside `.rpgdsproj`. Compile reparses
the clean SDAT, replaces the selected numeric sequence ID and rebuilds it.

Current limitation: MIDI replacement targets BGM, BGS and ME. Effects can be
previewed, exported and extracted, but SSAR replacement remains disabled until
shared archive event-offset validation is implemented.

## Accurate renderer provenance

The vendored portable core comes from CyberBotX's `in_xsf`/`in_ncsf` project
and is distributed under its BSD 3-Clause license. That player was adapted from
fincs' FeOS Sound System and also incorporates Nintendo DS sample-generation
behavior from DeSmuME. Toolkit-specific code only provides the small command-line
PCM bridge and packages it alongside the Python application; the audio engine's
sequence interpretation remains the upstream implementation.
