"""Nintendo DS SDAT audio tools used by RPG Maker DS Toolkit.

The game stores music as SSEQ note data, SBNK instrument definitions and
SWAR/SWAV samples.  This module keeps those relationships intact when
extracting, previewing and converting MIDI data.
"""

from __future__ import annotations

import io
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import mido
import numpy as np
import ndspy.soundBank
import ndspy.soundArchive
import ndspy.soundSequence
import ndspy.soundSequenceArchive
import ndspy.soundWave
import ndspy.soundWaveArchive


_preview_sound = None
_preview_channel = None


SDAT_ROM_PATH = "sound/sound_data.sdat"
TICKS_PER_BEAT = 48
NDS_SOUND_CLOCK = 16_756_991.0


def _ncsf_renderer_path() -> Path:
    """Locate the bundled in_ncsf renderer in source and frozen builds."""
    executable = "rpgds_ncsf_preview.exe" if sys.platform == "win32" else "rpgds_ncsf_preview"
    roots = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(frozen_root))
    module_root = Path(__file__).resolve().parent
    roots.extend((
        module_root / "native" / "ncsf_preview" / "build",
        module_root / "native" / "ncsf_preview" / "bin",
        module_root,
    ))
    for root in roots:
        candidate = root / executable
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "The accurate DS audio renderer is not installed. Run build_exe.ps1 "
        "(Windows) or build_linux.sh (Linux) to build it."
    )


@dataclass(frozen=True)
class AudioAsset:
    kind: str
    index: int
    name: str
    bank_id: int
    player_id: int
    volume: int
    archive_index: int | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.index}"

    @property
    def label(self) -> str:
        return f"{self.kind.upper()} {self.index + 1:03d}  {self.name}"


def list_audio_assets(sdat) -> list[AudioAsset]:
    assets: list[AudioAsset] = []
    for index, (name, sequence) in enumerate(sdat.sequences):
        kind = "bgm" if index < 40 else "bgs" if index < 50 else "me"
        assets.append(AudioAsset(kind, index, name or f"sequence{index}",
                                 sequence.bankID, sequence.playerID, sequence.volume))
    for index, (archive_name, archive) in enumerate(sdat.sequenceArchives):
        entries = archive.sequences or []
        if not entries:
            continue
        name, sequence = entries[0]
        assets.append(AudioAsset("se", index, name or archive_name or f"se{index + 1:03d}",
                                 sequence.bankID, sequence.playerID, sequence.volume, index))
    return assets


def sequence_for_asset(sdat, asset: AudioAsset):
    if asset.kind == "se":
        archive = sdat.sequenceArchives[asset.archive_index][1]
        archive.parse()
        entry = archive.sequences[0][1]
        # Each DS+ SSAR contains exactly one effect. Present it through the
        # SSEQ interface used by the renderer/MIDI converter while retaining
        # the SSAR entry's bank and player metadata.
        return ndspy.soundSequence.SSEQ.fromEvents(
            archive.events, archive.unk02, entry.bankID, entry.volume,
            entry.channelPressure, entry.polyphonicPressure, entry.playerID,
        )
    return sdat.sequences[asset.index][1]


def _event_tracks(sequence, max_ticks: int = TICKS_PER_BEAT * 60 * 8):
    """Return (track, absolute tick, event) while following calls and one loop."""
    sequence.parse()
    events = sequence.events
    positions = {id(event): index for index, event in enumerate(events)}
    begin = [event for event in events
             if isinstance(event, ndspy.soundSequence.BeginTrackSequenceEvent)]
    header_end = 0
    while header_end < len(events) and isinstance(
            events[header_end], (ndspy.soundSequence.DefineTracksSequenceEvent,
                                 ndspy.soundSequence.BeginTrackSequenceEvent)):
        header_end += 1
    starts = {0: header_end}
    for event in begin:
        starts[event.trackNumber] = positions[id(event.firstEvent)]

    for track_number, start in sorted(starts.items()):
        pc, tick, call_stack = start, 0, []
        note_wait = False
        edge_visits: dict[tuple[int, int], int] = {}
        steps = 0
        while 0 <= pc < len(events) and tick <= max_ticks and steps < 250000:
            steps += 1
            event = events[pc]
            yield track_number, tick, event
            if isinstance(event, ndspy.soundSequence.RestSequenceEvent):
                tick += event.duration
            elif isinstance(event, ndspy.soundSequence.MonoPolySequenceEvent):
                # Nitro calls C7 "note wait": when enabled, a note's duration
                # advances the command cursor; when disabled, notes overlap.
                note_wait = bool(event.value)
            elif note_wait and isinstance(event, ndspy.soundSequence.NoteSequenceEvent):
                tick += event.duration
            if isinstance(event, ndspy.soundSequence.JumpSequenceEvent):
                target = positions.get(id(event.destination), len(events))
                edge = (pc, target)
                edge_visits[edge] = edge_visits.get(edge, 0) + 1
                if target <= pc and edge_visits[edge] > 1:
                    break
                pc = target
                continue
            if isinstance(event, ndspy.soundSequence.CallSequenceEvent):
                call_stack.append(pc + 1)
                pc = positions.get(id(event.destination), len(events))
                continue
            if isinstance(event, ndspy.soundSequence.ReturnSequenceEvent):
                if not call_stack:
                    break
                pc = call_stack.pop()
                continue
            if isinstance(event, ndspy.soundSequence.EndTrackSequenceEvent):
                break
            pc += 1


def sequence_to_midi(sequence) -> mido.MidiFile:
    midi = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)
    grouped: dict[int, list[tuple[int, int, mido.Message | mido.MetaMessage]]] = {}
    order = 0
    track_transpose: dict[int, int] = {}
    track_bend_range: dict[int, int] = {}
    for track, tick, event in _event_tracks(sequence):
        target = grouped.setdefault(track, [])
        channel = track & 0x0F
        if isinstance(event, ndspy.soundSequence.NoteSequenceEvent):
            pitch = max(0, min(127, int(event.type) + track_transpose.get(track, 0)))
            velocity = max(1, min(127, event.velocity))
            target.append((tick, order, mido.Message(
                "note_on", channel=channel, note=pitch, velocity=velocity)))
            order += 1
            target.append((tick + max(1, event.duration), order,
                           mido.Message("note_off", channel=channel, note=pitch, velocity=0)))
        elif isinstance(event, ndspy.soundSequence.InstrumentSwitchSequenceEvent):
            target.append((tick, order, mido.Message(
                "program_change", channel=channel,
                program=max(0, min(127, event.instrumentID)))))
        elif isinstance(event, ndspy.soundSequence.PanSequenceEvent):
            target.append((tick, order, mido.Message("control_change", channel=channel, control=10,
                                                     value=max(0, min(127, event.value)))))
        elif isinstance(event, ndspy.soundSequence.TrackVolumeSequenceEvent):
            target.append((tick, order, mido.Message("control_change", channel=channel, control=7,
                                                     value=max(0, min(127, event.value)))))
        elif isinstance(event, ndspy.soundSequence.ExpressionSequenceEvent):
            target.append((tick, order, mido.Message("control_change", channel=channel, control=11,
                                                     value=max(0, min(127, event.value)))))
        elif isinstance(event, ndspy.soundSequence.TransposeSequenceEvent):
            # ndspy exposes the signed byte as 0..255 on some releases.
            value = int(event.value)
            track_transpose[track] = value - 256 if value >= 128 else value
        elif isinstance(event, ndspy.soundSequence.PortamentoRangeSequenceEvent):
            # Despite ndspy's historical name, SSEQ command C5 is the pitch
            # bend range. Emit the standard MIDI RPN 0 sequence so exported
            # MIDI and the native preview agree with the DS track state.
            bend_range = max(0, min(127, int(event.value)))
            track_bend_range[track] = bend_range
            for control, value in ((101, 0), (100, 0), (6, bend_range), (38, 0)):
                target.append((tick, order, mido.Message(
                    "control_change", channel=channel, control=control, value=value)))
                order += 1
        elif isinstance(event, ndspy.soundSequence.PortamentoSequenceEvent):
            # SSEQ C4 is pitch bend (-128..127), although ndspy calls this
            # class PortamentoSequenceEvent. MIDI pitchwheel is -8192..8191.
            value = int(event.value)
            value = value - 256 if value >= 128 else value
            # Nitro applies (signed_bend * range) / 128 for both directions.
            wheel = value * 64
            wheel = max(-8192, min(8191, wheel))
            target.append((tick, order, mido.Message(
                "pitchwheel", channel=channel, pitch=wheel)))
        elif isinstance(event, ndspy.soundSequence.TempoSequenceEvent):
            bpm = max(1, event.value)
            target.append((tick, order, mido.MetaMessage("set_tempo",
                                                         tempo=mido.bpm2tempo(bpm))))
        order += 1
    for track_no in sorted(grouped):
        output = mido.MidiTrack()
        output.append(mido.MetaMessage("track_name", name=f"DS Track {track_no}", time=0))
        previous = 0
        for tick, _, message in sorted(grouped[track_no], key=lambda item: (item[0], item[1])):
            message.time = max(0, tick - previous)
            previous = tick
            output.append(message)
        output.append(mido.MetaMessage("end_of_track", time=0))
        midi.tracks.append(output)
    if not midi.tracks:
        midi.tracks.append(mido.MidiTrack([mido.MetaMessage("end_of_track")]))
    return midi


def midi_track_names(midi: mido.MidiFile) -> list[str]:
    names = []
    for index, track in enumerate(midi.tracks):
        name = next((message.name for message in track
                     if message.type == "track_name"), "")
        names.append(name or f"MIDI Track {index + 1}")
    return names


def midi_to_sequence(midi: mido.MidiFile, instrument_ids: list[int], base_sequence,
                     track_volumes: list[int] | None = None,
                     tempo_bpm: int | None = None):
    """Convert MIDI tracks to an SSEQ while retaining the selected DS bank."""
    scale = TICKS_PER_BEAT / max(1, midi.ticks_per_beat)
    track_events: list[list] = []
    for track_index, midi_track in enumerate(midi.tracks[:16]):
        absolute = 0
        notes: dict[int, list[tuple[int, int]]] = {}
        timeline: list[tuple[int, object]] = []
        instrument = instrument_ids[track_index] if track_index < len(instrument_ids) else 0
        timeline.append((0, ndspy.soundSequence.InstrumentSwitchSequenceEvent(0, instrument)))
        if track_volumes is not None and track_index < len(track_volumes):
            timeline.append((0, ndspy.soundSequence.TrackVolumeSequenceEvent(
                max(0, min(127, int(track_volumes[track_index]))))))
        if track_index == 0 and tempo_bpm is not None:
            timeline.append((0, ndspy.soundSequence.TempoSequenceEvent(
                max(1, min(1023, int(tempo_bpm))))))
        for message in midi_track:
            absolute += message.time
            tick = max(0, round(absolute * scale))
            if message.type == "note_on" and message.velocity:
                notes.setdefault(message.note, []).append((tick, message.velocity))
            elif message.type in ("note_off", "note_on"):
                stack = notes.get(message.note)
                if stack:
                    start, velocity = stack.pop(0)
                    timeline.append((start, ndspy.soundSequence.NoteSequenceEvent(
                        message.note, velocity, max(1, tick - start))))
            elif message.type == "set_tempo" and tempo_bpm is None:
                timeline.append((tick, ndspy.soundSequence.TempoSequenceEvent(
                    max(1, round(mido.tempo2bpm(message.tempo))))))
            elif message.type == "control_change" and message.control == 7:
                timeline.append((tick, ndspy.soundSequence.TrackVolumeSequenceEvent(message.value)))
            elif message.type == "control_change" and message.control == 10:
                timeline.append((tick, ndspy.soundSequence.PanSequenceEvent(message.value)))
        end_tick = max([x[0] for x in timeline] + [0]) + TICKS_PER_BEAT
        for note, stack in notes.items():
            for start, velocity in stack:
                timeline.append((start, ndspy.soundSequence.NoteSequenceEvent(
                    note, velocity, max(1, end_tick - start))))
        emitted, cursor = [], 0
        for tick, event in sorted(timeline, key=lambda item: item[0]):
            if tick > cursor:
                emitted.append(ndspy.soundSequence.RestSequenceEvent(tick - cursor))
                cursor = tick
            emitted.append(event)
        emitted.append(ndspy.soundSequence.EndTrackSequenceEvent())
        track_events.append(emitted)
    if not track_events:
        track_events = [[ndspy.soundSequence.EndTrackSequenceEvent()]]
    all_events = []
    if len(track_events) > 1:
        all_events.append(ndspy.soundSequence.DefineTracksSequenceEvent(set(range(len(track_events)))))
        for track_no in range(1, len(track_events)):
            all_events.append(ndspy.soundSequence.BeginTrackSequenceEvent(
                track_no, track_events[track_no][0]))
    for events in track_events:
        all_events.extend(events)
    return ndspy.soundSequence.SSEQ.fromEvents(
        all_events, base_sequence.unk02, base_sequence.bankID, base_sequence.volume,
        base_sequence.channelPressure, base_sequence.polyphonicPressure,
        base_sequence.playerID,
    )


def _decode_swav(wav) -> np.ndarray:
    data = bytes(wav.data)
    if wav.waveType == ndspy.soundWave.WaveType.PCM8:
        return np.frombuffer(data, dtype=np.int8).astype(np.float32) / 128.0
    if wav.waveType == ndspy.soundWave.WaveType.PCM16:
        return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    if len(data) < 4:
        return np.zeros(1, dtype=np.float32)
    predictor = struct.unpack_from("<h", data, 0)[0]
    step_index = max(0, min(88, data[2]))
    step_table = np.array([
        7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,
        66,73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,
        371,408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,
        1552,1707,1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,
        5358,5894,6484,7132,7845,8630,9493,10442,11487,12635,13899,15289,
        16818,18500,20350,22385,24623,27086,29794,32767], dtype=np.int32)
    index_table = (-1,-1,-1,-1,2,4,6,8)
    output = [predictor / 32768.0]
    for byte in data[4:]:
        for nibble in (byte & 15, byte >> 4):
            step = int(step_table[step_index])
            diff = step >> 3
            if nibble & 1: diff += step >> 2
            if nibble & 2: diff += step >> 1
            if nibble & 4: diff += step
            predictor += -diff if nibble & 8 else diff
            # The DS ADPCM unit clips negative overflow to -0x7FFF, not
            # -0x8000 (GBATEK and VGMTrans both document this quirk).
            predictor = max(-32767, min(32767, predictor))
            step_index = max(0, min(88, step_index + index_table[nibble & 7]))
            output.append(predictor / 32768.0)
    return np.asarray(output, dtype=np.float32)


def _note_definition(instrument, pitch: int):
    if isinstance(instrument, ndspy.soundBank.SingleNoteInstrument):
        return instrument.noteDefinition
    if isinstance(instrument, ndspy.soundBank.RangeInstrument):
        index = pitch - instrument.firstPitch
        if index < 0 or index >= len(instrument.noteDefinitions):
            return None
        return instrument.noteDefinitions[index]
    if isinstance(instrument, ndspy.soundBank.RegionalInstrument):
        for region in instrument.regions:
            if pitch <= region.lastPitch:
                return region.noteDefinition
        return instrument.regions[-1].noteDefinition if instrument.regions else None
    return None


def effect_sample_target(sdat, asset: AudioAsset) -> tuple[int, int]:
    """Return the single SWAR/SWAV slot used by a DS+ SSAR effect."""
    if asset.kind != "se":
        raise ValueError("Select a sound effect before importing a WAV")
    sequence = sequence_for_asset(sdat, asset)
    bank = sdat.banks[sequence.bankID][1]
    programs: dict[int, int] = {}
    targets: set[tuple[int, int]] = set()
    for track, _tick, event in _event_tracks(sequence):
        if isinstance(event, ndspy.soundSequence.InstrumentSwitchSequenceEvent):
            programs[track] = (int(event.bankID) << 7) | int(event.instrumentID)
        elif isinstance(event, ndspy.soundSequence.NoteSequenceEvent):
            program = programs.get(track, 0)
            if program < 0 or program >= len(bank.instruments):
                continue
            instrument = bank.instruments[program]
            if instrument is None:
                continue
            definition = _note_definition(instrument, int(event.type))
            if definition is None or int(definition.type) != 1:
                continue
            if definition.waveArchiveIDID >= len(bank.waveArchiveIDs):
                continue
            archive_index = bank.waveArchiveIDs[definition.waveArchiveIDID]
            targets.add((int(archive_index), int(definition.waveID)))
    if len(targets) != 1:
        raise ValueError(
            f"This effect references {len(targets)} samples; automatic WAV replacement "
            "requires exactly one sample"
        )
    return next(iter(targets))


_IMA_STEP_TABLE = (
    7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,
    66,73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,
    371,408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,
    1552,1707,1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,
    5358,5894,6484,7132,7845,8630,9493,10442,11487,12635,13899,15289,
    16818,18500,20350,22385,24623,27086,29794,32767,
)
_IMA_INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8)


def _encode_ima_adpcm(samples: np.ndarray) -> bytes:
    pcm = np.asarray(samples, dtype=np.int16)
    if pcm.size == 0:
        pcm = np.zeros(1, dtype=np.int16)
    predictor = int(pcm[0])
    step_index = 0
    nibbles: list[int] = []
    for target in pcm[1:]:
        step = _IMA_STEP_TABLE[step_index]
        difference = int(target) - predictor
        nibble = 8 if difference < 0 else 0
        difference = abs(difference)
        delta = step >> 3
        if difference >= step:
            nibble |= 4; difference -= step; delta += step
        if difference >= step >> 1:
            nibble |= 2; difference -= step >> 1; delta += step >> 1
        if difference >= step >> 2:
            nibble |= 1; delta += step >> 2
        predictor += -delta if nibble & 8 else delta
        predictor = max(-32767, min(32767, predictor))
        step_index = max(0, min(88, step_index + _IMA_INDEX_TABLE[nibble & 7]))
        nibbles.append(nibble)
    output = bytearray(struct.pack("<hBB", int(pcm[0]), 0, 0))
    for index in range(0, len(nibbles), 2):
        low = nibbles[index]
        high = nibbles[index + 1] if index + 1 < len(nibbles) else 0
        output.append(low | (high << 4))
    while len(output) % 4:
        output.append(0)
    return bytes(output)


def wav_bytes_to_swav(data: bytes, max_seconds: float = 15.0) -> bytes:
    """Convert an ordinary WAV into a normalized mono Nintendo DS ADPCM SWAV."""
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            if source.getcomptype() != "NONE":
                raise ValueError("Compressed WAV files are not supported")
            channels = source.getnchannels()
            width = source.getsampwidth()
            source_rate = source.getframerate()
            frame_count = source.getnframes()
            raw = source.readframes(frame_count)
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"Invalid WAV file: {exc}") from exc
    if channels < 1 or channels > 8 or width not in (1, 2, 3, 4) or source_rate <= 0:
        raise ValueError("WAV must use uncompressed 8, 16, 24, or 32-bit PCM")
    if frame_count / source_rate > max_seconds:
        raise ValueError(f"Sound effects must be {max_seconds:g} seconds or shorter")
    if width == 1:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 3:
        packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        integers = (packed[:, 0].astype(np.int32) |
                    (packed[:, 1].astype(np.int32) << 8) |
                    (packed[:, 2].astype(np.int32) << 16))
        integers = np.where(integers & 0x800000, integers - 0x1000000, integers)
        values = integers.astype(np.float64) / 8388608.0
    else:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    if values.size % channels:
        raise ValueError("WAV sample data is truncated")
    values = values.reshape(-1, channels).mean(axis=1)
    target_rate = max(8000, min(32768, source_rate))
    if source_rate != target_rate and values.size > 1:
        output_count = max(1, round(values.size * target_rate / source_rate))
        values = np.interp(
            np.linspace(0, values.size - 1, output_count),
            np.arange(values.size), values,
        )
    peak = float(np.max(np.abs(values))) if values.size else 0.0
    if peak > 0:
        values = values * (0.95 / peak)
    pcm = np.clip(np.rint(values * 32767.0), -32767, 32767).astype("<i2")
    encoded = _encode_ima_adpcm(pcm)
    converted = ndspy.soundWave.SWAV.fromData(
        encoded, waveType=ndspy.soundWave.WaveType.ADPCM, isLooped=False,
        sampleRate=target_rate, time=max(1, round(NDS_SOUND_CLOCK / target_rate)),
        loopOffset=0, totalLength=len(encoded) // 4,
    )
    return converted.save()


def _swav_base_rate(wav) -> float:
    """Return the rate programmed by Nitro's hardware timer, not rounded Hz metadata."""
    timer = int(getattr(wav, "time", 0) or 0)
    return NDS_SOUND_CLOCK / timer if timer else float(wav.sampleRate)


def _swav_loop_start_samples(wav) -> int:
    """Convert the DS 32-bit-word repeat point to decoded PCM sample units."""
    words = max(0, int(wav.loopOffset))
    if wav.waveType == ndspy.soundWave.WaveType.ADPCM:
        # The first word is the predictor/index header and contributes one
        # decoded sample: words*8 - 8 + 1 (VGMTrans's NDS decoder).
        return max(0, words * 8 - 7)
    if wav.waveType == ndspy.soundWave.WaveType.PCM16:
        return words * 2
    return words * 4


def _midi_note_schedule(midi: mido.MidiFile, limit_seconds: float):
    tempo = 500000
    seconds = 0.0
    programs = [0] * 16
    volumes = [100] * 16
    pans = [64] * 16
    expressions = [127] * 16
    bend_ranges = [2.0] * 16
    bend_wheels = [0] * 16
    bends = [0.0] * 16
    rpn_msb = [127] * 16
    rpn_lsb = [127] * 16
    active: dict[tuple[int, int], list[dict]] = {}
    notes = []
    for message in mido.merge_tracks(midi.tracks):
        seconds += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
        if seconds > limit_seconds:
            break
        channel = getattr(message, "channel", 0)
        if message.type == "set_tempo": tempo = message.tempo
        elif message.type == "program_change": programs[channel] = message.program
        elif message.type == "control_change" and message.control == 7: volumes[channel] = message.value
        elif message.type == "control_change" and message.control == 10: pans[channel] = message.value
        elif message.type == "control_change" and message.control == 11: expressions[channel] = message.value
        elif message.type == "control_change" and message.control == 101: rpn_msb[channel] = message.value
        elif message.type == "control_change" and message.control == 100: rpn_lsb[channel] = message.value
        elif (message.type == "control_change" and message.control == 6
              and rpn_msb[channel] == 0 and rpn_lsb[channel] == 0):
            bend_ranges[channel] = float(message.value)
            bends[channel] = (bend_wheels[channel] / 8192.0) * bend_ranges[channel]
            for (active_channel, _), stack in active.items():
                if active_channel == channel:
                    for note_state in stack:
                        note_state["bends"].append((seconds, bends[channel]))
        elif message.type == "pitchwheel":
            bend_wheels[channel] = message.pitch
            bends[channel] = (message.pitch / 8192.0) * bend_ranges[channel]
            for (active_channel, _), stack in active.items():
                if active_channel == channel:
                    for note_state in stack:
                        note_state["bends"].append((seconds, bends[channel]))
        elif message.type == "note_on" and message.velocity:
            active.setdefault((channel, message.note), []).append({
                "start": seconds, "velocity": message.velocity,
                "program": programs[channel],
                "controls": (volumes[channel], pans[channel], expressions[channel]),
                "bends": [(seconds, bends[channel])],
            })
        elif message.type in ("note_off", "note_on"):
            stack = active.get((channel, message.note))
            if stack:
                state = stack.pop(0)
                notes.append((state["start"], max(0.03, seconds - state["start"]),
                              message.note, state["velocity"], state["program"],
                              *state["controls"], state["bends"]))
    for (channel, pitch), stack in active.items():
        for state in stack:
            notes.append((state["start"], max(0.03, limit_seconds - state["start"]),
                          pitch, state["velocity"], state["program"],
                          *state["controls"], state["bends"]))
    return notes


def render_sequence_pcm(sdat, asset: AudioAsset, sequence=None,
                        seconds: float = 30.0, sample_rate: int = 32768) -> tuple[bytes, int]:
    """Synthesize a DS sequence directly to signed 16-bit stereo PCM."""
    sequence = sequence or sequence_for_asset(sdat, asset)
    midi = sequence_to_midi(sequence)
    bank = sdat.banks[sequence.bankID][1]
    frames = max(1, int(seconds * sample_rate))
    mix = np.zeros((frames, 2), dtype=np.float32)
    cache: dict[tuple[int, int], np.ndarray] = {}
    for scheduled_note in _midi_note_schedule(midi, seconds):
        start, duration, pitch, velocity, program, volume, pan, expression = scheduled_note[:8]
        bend_events = (scheduled_note[8] if len(scheduled_note) > 8
                       else [(start, 0.0)])
        if not bank.instruments:
            continue
        instrument = bank.instruments[max(0, min(len(bank.instruments) - 1, program))]
        if instrument is None:
            continue
        definition = _note_definition(instrument, pitch)
        if definition is None or int(definition.type) != 1:
            continue
        if definition.waveArchiveIDID >= len(bank.waveArchiveIDs):
            continue
        archive_id = bank.waveArchiveIDs[definition.waveArchiveIDID]
        archive = sdat.waveArchives[archive_id][1]
        if definition.waveID >= len(archive.waves):
            continue
        source_wave = archive.waves[definition.waveID]
        key = (archive_id, definition.waveID)
        source = cache.setdefault(key, _decode_swav(source_wave))
        count = min(int(duration * sample_rate), frames - int(start * sample_rate))
        if count <= 0:
            continue
        semitone_curve = np.zeros(count, dtype=np.float64)
        for event_time, bend in bend_events:
            bend_offset = max(0, min(count, int((event_time - start) * sample_rate)))
            semitone_curve[bend_offset:] = bend
        ratios = ((_swav_base_rate(source_wave) / sample_rate)
                  * np.exp2((pitch + semitone_curve - definition.pitch) / 12.0))
        # Integrate changing playback rates. A cumulative phase is required;
        # multiplying absolute time by the newest rate creates discontinuities.
        source_pos = np.cumsum(ratios, dtype=np.float64) - ratios[0]
        if source_wave.isLooped and len(source) > 2:
            loop_start = min(len(source) - 1, _swav_loop_start_samples(source_wave))
            loop_len = max(1, len(source) - loop_start)
            source_pos = np.where(source_pos < len(source), source_pos,
                                  loop_start + np.mod(source_pos - loop_start, loop_len))
        valid = source_pos < len(source)
        rendered = np.zeros(count, dtype=np.float32)
        rendered[valid] = np.interp(source_pos[valid], np.arange(len(source)), source)
        gain = ((velocity / 127.0) * (volume / 127.0) * (expression / 127.0)
                * (sequence.volume / 127.0) * 0.32)
        fade = min(count // 2, max(1, int(sample_rate * 0.01)))
        # ``rendered[-0:]`` means the entire array in NumPy, so a one-sample
        # DS event used to multiply a shape-(1,) note by a shape-(0,) fade.
        # Tiny percussion/control notes legitimately reach this path.
        if fade:
            rendered[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
            rendered[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        rendered *= gain
        left = math.cos((pan / 127.0) * math.pi / 2)
        right = math.sin((pan / 127.0) * math.pi / 2)
        offset = int(start * sample_rate)
        mix[offset:offset + count, 0] += rendered * left
        mix[offset:offset + count, 1] += rendered * right
    peak = float(np.max(np.abs(mix))) if mix.size else 0
    if peak > 0.98: mix *= 0.98 / peak
    pcm = (np.clip(mix, -1, 1) * 32767).astype("<i2").tobytes()
    return pcm, sample_rate


def render_sequence_wav(sdat, asset: AudioAsset, sequence=None,
                        seconds: float = 30.0, sample_rate: int = 32768) -> bytes:
    """Export-compatible WAV wrapper around the in-memory DS synthesizer."""
    pcm, sample_rate = render_sequence_pcm(sdat, asset, sequence, seconds, sample_rate)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(2); wav_file.setsampwidth(2); wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


def render_sequence_ncsf_pcm(sdat, asset: AudioAsset, sequence=None,
                              seconds: float = 30.0,
                              sample_rate: int = 32768,
                              sample_replacements: dict[str, bytes] | None = None,
                              ) -> tuple[bytes, int]:
    """Render a top-level SSEQ using the proven in_ncsf/FeOS sound core.

    A temporary SDAT is used so unsaved MIDI replacements are previewed without
    changing the loaded toolkit project or source ROM.
    """
    if sequence is None:
        sequence = sequence_for_asset(sdat, asset)

    # Reparse a private SDAT copy before replacing the selected SSEQ. This
    # avoids mutating the GUI's archive while its background worker renders.
    archive = ndspy.soundArchive.SDAT(bytes(sdat.save()))
    for key, raw_wave in (sample_replacements or {}).items():
        parts = key.split(":")
        if len(parts) != 3 or parts[0] != "swar":
            continue
        wave_archive = archive.waveArchives[int(parts[1])][1]
        wave_archive.waves[int(parts[2])] = ndspy.soundWave.SWAV(raw_wave)
    # in_ncsf loads top-level SSEQ records. For a one-sequence SSAR effect,
    # temporarily place its already-parsed event stream in sequence slot 0.
    # Its own bank/player/volume metadata is retained, so the native engine
    # resolves the same SBNK and SWARs as the game does.
    render_index = 0 if asset.kind == "se" else asset.index
    original_name, original = archive.sequences[render_index]
    raw_sequence = bytes(sequence.save()[0])
    replacement = ndspy.soundSequence.SSEQ(
        raw_sequence, getattr(sequence, "unk02", 0), sequence.bankID,
        sequence.volume, sequence.channelPressure, sequence.polyphonicPressure,
        sequence.playerID,
    )
    archive.sequences[render_index] = (original_name, replacement)

    renderer = _ncsf_renderer_path()
    with tempfile.TemporaryDirectory(prefix="rpgds_ncsf_") as folder:
        folder_path = Path(folder)
        sdat_path = folder_path / "preview.sdat"
        pcm_path = folder_path / "preview.pcm"
        sdat_path.write_bytes(bytes(archive.save()))
        result = subprocess.run(
            [str(renderer), str(sdat_path), str(render_index), f"{seconds:.6f}",
             str(sample_rate), str(pcm_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            timeout=max(30.0, seconds * 4.0),
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown renderer error"
            raise RuntimeError(f"Accurate DS audio render failed: {detail}")
        pcm = pcm_path.read_bytes()
    expected = int(seconds * sample_rate) * 4
    if len(pcm) != expected:
        raise RuntimeError(f"Accurate DS audio renderer returned {len(pcm):,} bytes; expected {expected:,}")
    return pcm, sample_rate


def play_pcm_bytes(pcm: bytes, sample_rate: int = 32768, loop: bool = False) -> None:
    """Play synthesized sequence output directly from memory, without WAV/MIDI files."""
    global _preview_sound, _preview_channel
    # Delay pygame import until playback so command-line extraction remains
    # lightweight and Linux users without an audio device can still edit.
    import os
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame
    wanted = (sample_rate, -16, 2)
    if pygame.mixer.get_init() != wanted:
        if pygame.mixer.get_init():
            pygame.mixer.quit()
        pygame.mixer.init(frequency=sample_rate, size=-16, channels=2, buffer=1024)
    if _preview_channel is not None:
        _preview_channel.stop()
    _preview_sound = pygame.mixer.Sound(buffer=pcm)
    _preview_channel = _preview_sound.play(loops=-1 if loop else 0)


def play_wav_bytes(data: bytes) -> Path:
    path = Path(tempfile.gettempdir()) / "rpgds_audio_preview.wav"
    path.write_bytes(data)
    if __import__("os").name == "nt":
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
    else:
        player = shutil.which("paplay") or shutil.which("aplay")
        if not player:
            raise RuntimeError("Install paplay or aplay to preview audio on Linux")
        subprocess.Popen([player, str(path)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    return path


def stop_audio() -> None:
    global _preview_channel
    try:
        import pygame
        if pygame.mixer.get_init():
            pygame.mixer.stop()
        _preview_channel = None
        return
    except (ImportError, RuntimeError):
        pass
    if __import__("os").name == "nt":
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)


def export_audio_workspace(sdat, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    for folder in ("sequences", "sequence_archives", "banks", "wave_archives", "samples"):
        (destination / folder).mkdir(exist_ok=True)
    manifest = {"format": "Nintendo DS SDAT", "assets": [], "banks": [], "wave_archives": []}
    for asset in list_audio_assets(sdat):
        sequence = sequence_for_asset(sdat, asset)
        raw = bytes(sequence.save()[0])
        filename = f"{asset.kind}_{asset.index:03d}_{asset.name}.sseq"
        (destination / "sequences" / filename).write_bytes(raw)
        manifest["assets"].append({**asset.__dict__, "file": f"sequences/{filename}"})
    for index, (name, archive) in enumerate(sdat.sequenceArchives):
        filename = f"{index:03d}_{name or 'sequence_archive'}.ssar"
        (destination / "sequence_archives" / filename).write_bytes(bytes(archive.save()[0]))
    for index, (name, bank) in enumerate(sdat.banks):
        raw = bytes(bank.save()[0])
        filename = f"{index:03d}_{name or 'bank'}.sbnk"
        (destination / "banks" / filename).write_bytes(raw)
        manifest["banks"].append({"index": index, "name": name,
                                  "wave_archive_ids": bank.waveArchiveIDs,
                                  "instruments": len(bank.instruments), "file": f"banks/{filename}"})
    for index, (name, archive) in enumerate(sdat.waveArchives):
        raw = bytes(archive.save()[0])
        filename = f"{index:03d}_{name or 'waves'}.swar"
        (destination / "wave_archives" / filename).write_bytes(raw)
        wave_dir = destination / "samples" / f"{index:03d}_{name or 'waves'}"
        wave_dir.mkdir(exist_ok=True)
        for wave_id, source in enumerate(archive.waves):
            source.saveToFile(wave_dir / f"{wave_id:03d}.swav")
            pcm = _decode_swav(source)
            with wave.open(str(wave_dir / f"{wave_id:03d}.wav"), "wb") as target:
                target.setnchannels(1); target.setsampwidth(2); target.setframerate(source.sampleRate)
                target.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
        manifest["wave_archives"].append({"index": index, "name": name,
                                          "samples": len(archive.waves),
                                          "file": f"wave_archives/{filename}"})
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "audio"
